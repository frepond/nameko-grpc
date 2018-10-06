import pytest
import os
import subprocess
import sys
import time
from functools import partial
from importlib import import_module
from nameko.testing.services import entrypoint_hook, dummy

from nameko_grpc.entrypoint import Grpc
from nameko_grpc.dependency_provider import GrpcProxy
from nameko_grpc.inspection import Inspector
from nameko_grpc.constants import Cardinality

from helpers import receive, send, temp_fifo, Config

last_modified = os.path.getmtime


@pytest.fixture
def compile_proto():
    def codegen(service_name):
        spec_dir = os.path.join(os.path.dirname(__file__), "spec")
        proto_path = os.path.join(spec_dir, "{}.proto".format(service_name))
        proto_last_modified = last_modified(proto_path)

        for generated_file in (
            "{}_pb2.py".format(service_name),
            "{}_pb2_grpc.py".format(service_name),
        ):
            generated_path = os.path.join(spec_dir, generated_file)
            if (
                not os.path.exists(generated_path)
                or last_modified(generated_path) < proto_last_modified
            ):
                protoc_args = [
                    "-I{}".format(spec_dir),
                    "--python_out",
                    spec_dir,
                    "--grpc_python_out",
                    spec_dir,
                    proto_path,
                ]
                # protoc.main is confused by absolute paths, so use subprocess instead
                python_args = ["python", "-m", "grpc_tools.protoc"] + protoc_args
                subprocess.call(python_args)

        if spec_dir not in sys.path:
            sys.path.append(spec_dir)

        protobufs = import_module("{}_pb2".format(service_name))
        stubs = import_module("{}_pb2_grpc".format(service_name))

        return protobufs, stubs

    return codegen


@pytest.fixture
def protobufs(compile_proto):
    protobufs, _ = compile_proto("helloworld")
    return protobufs


@pytest.fixture
def stubs(compile_proto):
    _, stubs = compile_proto("helloworld")
    return stubs


@pytest.fixture
def grpc_server():
    """ Standard GRPC server, running in another process
    """
    server_script = os.path.join(os.path.dirname(__file__), "server.py")
    with subprocess.Popen([sys.executable, server_script]) as proc:
        # wait until server has started
        time.sleep(0.5)
        yield
        proc.terminate()


@pytest.fixture
def grpc_client(stubs, tmpdir):
    """ Standard GRPC client, running in another process
    """
    with temp_fifo(tmpdir.strpath) as fifo_in:
        with temp_fifo(tmpdir.strpath) as fifo_out:

            client_script = os.path.join(os.path.dirname(__file__), "remote_client.py")
            with subprocess.Popen([sys.executable, client_script, fifo_in.path]):

                class Client:
                    def call(self, name, request):
                        send(fifo_in, Config(name, fifo_out.path))
                        send(fifo_in, request)
                        return receive(fifo_out)

                    def __getattr__(self, name):
                        return partial(self.call, name)

                yield Client()
                send(fifo_in, None)


@pytest.fixture
def service(container_factory, protobufs, stubs):

    HelloReply = protobufs.HelloReply

    grpc = Grpc.decorator(stubs.greeterStub)

    class Service:
        name = "greeter"

        @grpc
        def say_hello(self, request, context):
            return HelloReply(message="Hello, %s!" % request.name)

        @grpc
        def say_hello_goodbye(self, request, context):
            yield HelloReply(message="Hello, %s!" % request.name)
            yield HelloReply(message="Goodbye, %s!" % request.name)

        @grpc
        def say_hello_to_many(self, request, context):
            for message in request:
                yield HelloReply(message="Hi " + message.name)

        @grpc
        def say_hello_to_many_at_once(self, request, context):
            names = []
            for message in request:
                names.append(message.name)

            return HelloReply(message="Hi " + ", ".join(names) + "!")

    container = container_factory(Service, {})
    container.start()


@pytest.fixture
def dependency_provider_client(container_factory, stubs):
    class Service:
        name = "caller"

        greeter_grpc = GrpcProxy(stubs.greeterStub)

        @dummy
        def call(self, method_name, request):
            return getattr(self.greeter_grpc, method_name)(request)

    container = container_factory(Service, {})
    container.start()

    class Client:
        def call(self, name, request):
            with entrypoint_hook(container, "call") as hook:
                return hook(name, request)

        def __getattr__(self, name):
            return partial(self.call, name)

    yield Client()


class TestInspection:
    @pytest.fixture
    def inspector(self, stubs):
        return Inspector(stubs.greeterStub)

    def test_path_for_method(self, inspector):
        assert inspector.path_for_method("say_hello") == "/greeter/say_hello"
        assert (
            inspector.path_for_method("say_hello_goodbye")
            == "/greeter/say_hello_goodbye"
        )
        assert (
            inspector.path_for_method("say_hello_to_many")
            == "/greeter/say_hello_to_many"
        )
        assert (
            inspector.path_for_method("say_hello_to_many_at_once")
            == "/greeter/say_hello_to_many_at_once"
        )

    def test_input_type_for_method(self, inspector, protobufs):
        assert inspector.input_type_for_method("say_hello") == protobufs.HelloRequest

    def test_output_type_for_method(self, inspector, protobufs):
        assert inspector.output_type_for_method("say_hello") == protobufs.HelloReply

    def test_cardinality_for_method(self, inspector):
        assert inspector.cardinality_for_method("say_hello") == Cardinality.UNARY_UNARY
        assert (
            inspector.cardinality_for_method("say_hello_goodbye")
            == Cardinality.UNARY_STREAM
        )
        assert (
            inspector.cardinality_for_method("say_hello_to_many")
            == Cardinality.STREAM_STREAM
        )
        assert (
            inspector.cardinality_for_method("say_hello_to_many_at_once")
            == Cardinality.STREAM_UNARY
        )


class TestStandard:
    @pytest.fixture(params=["grpc_server", "nameko_server"])
    def server(self, request):
        if "grpc" in request.param:
            request.getfixturevalue("grpc_server")
        elif "nameko" in request.param:
            request.getfixturevalue("service")

    @pytest.fixture(params=["grpc_client", "nameko_client"])
    def client(self, request, server):
        if "grpc" in request.param:
            return request.getfixturevalue("grpc_client")
        elif "nameko" in request.param:
            return request.getfixturevalue("dependency_provider_client")

    def test_unary_unary(self, client, protobufs):
        response = client.say_hello(protobufs.HelloRequest(name="you"))
        assert response.message == "Hello, you!"

    def test_unary_stream(self, client, protobufs):
        responses = client.say_hello_goodbye(protobufs.HelloRequest(name="you"))
        assert [response.message for response in responses] == [
            "Hello, you!",
            "Goodbye, you!",
        ]

    def test_stream_unary(self, client, protobufs):
        def generate_requests():
            for name in ["Bill", "Bob"]:
                yield protobufs.HelloRequest(name=name)

        response = client.say_hello_to_many_at_once(generate_requests())
        assert response.message == "Hi Bill, Bob!"

    def test_stream_stream(self, client, protobufs):
        def generate_requests():
            for name in ["Bill", "Bob"]:
                yield protobufs.HelloRequest(name=name)

        responses = client.say_hello_to_many(generate_requests())
        assert [response.message for response in responses] == ["Hi Bill", "Hi Bob"]

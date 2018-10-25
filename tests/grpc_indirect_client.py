# -*- coding: utf-8 -*-
import sys
import threading
from importlib import import_module

import grpc

from nameko_grpc.exceptions import GrpcError

from helpers import FifoPipe, receive, send


def call(fifo_in, fifo_out, method):
    request = receive(fifo_in)
    try:
        response = method(request)
    except grpc.RpcError as exc:
        state = exc._state
        response = GrpcError(state.code, state.details, state.debug_error_string)
    send(fifo_out, response)


if __name__ == "__main__":

    spec_path = sys.argv[1]
    sys.path.append(spec_path)

    proto_name = sys.argv[2]
    service_name = sys.argv[3]

    grpc_module = import_module("{}_pb2_grpc".format(proto_name))
    stub_cls = getattr(grpc_module, "{}Stub".format(service_name))

    command_fifo_path = sys.argv[4]
    command_fifo = FifoPipe.wrap(command_fifo_path)

    channel = grpc.insecure_channel("127.0.0.1:50051")
    stub = stub_cls(channel)

    while True:
        config = receive(command_fifo)
        if config is None:
            break

        in_fifo_path = config.in_fifo
        in_fifo = FifoPipe.wrap(in_fifo_path)

        out_fifo_path = config.out_fifo
        out_fifo = FifoPipe.wrap(out_fifo_path)

        method = getattr(stub, config.method_name)

        thread = threading.Thread(
            target=call, name=config.method_name, args=(in_fifo, out_fifo, method)
        )
        thread.start()

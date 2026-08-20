"""Call a raw unary method on an in-process generic gRPC server.

The server binds only to loopback and needs no generated protobuf code:
``python examples/grpc_example.py``.
"""

import asyncio
from typing import Any

# `type: ignore[import-untyped]` -- grpcio ships no PEP 561 typing marker.
import grpc  # type: ignore[import-untyped]

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.utils.envelope import GatewayResponse


async def _ping(body: bytes, context: Any) -> bytes:
    """Return the raw unary response for the local example server.

    Args:
        body: Raw request bytes.
        context: grpcio server call context.
    Returns:
        Raw ``pong`` response bytes.
    """
    return b'pong'


async def call() -> GatewayResponse:
    """Start a local generic server, call it once, and return the envelope.

    Returns:
        The common gateway response envelope.
    Raises:
        asyncio.CancelledError: If the caller cancels the operation.
    """
    server = grpc.aio.server()
    service = grpc.method_handlers_generic_handler(
        'demo.Echo', {'Ping': grpc.unary_unary_rpc_method_handler(_ping)})
    server.add_generic_rpc_handlers((service,))
    port = server.add_insecure_port('127.0.0.1:0')
    await server.start()
    try:
        result = await request(
            url=f'grpc://127.0.0.1:{port}',
            data=b'ping',
            protocol='GRPC',
            protocol_info={'method': '/demo.Echo/Ping'},
        )
    finally:
        await server.stop(None)
    return result


if __name__ == '__main__':
    asyncio.run(call())

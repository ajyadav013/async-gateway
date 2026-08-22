"""Call one JSON-RPC 2.0 method through the public gateway entry point.

Run this against a loopback JSON-RPC endpoint::

    python examples/jsonrpc_example.py http://127.0.0.1:8080/rpc
"""

import asyncio
import sys

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.utils.envelope import GatewayResponse

DEFAULT_URL = 'http://127.0.0.1:8080/rpc'


async def call(url: str = DEFAULT_URL) -> GatewayResponse:
    """Call ``demo.double`` and return the common response envelope.

    Args:
        url: Absolute HTTP(S) JSON-RPC endpoint.
    Returns:
        The common gateway response envelope.
    Raises:
        ConfigurationError: If ``url`` cannot form a JSON-RPC call.
        asyncio.CancelledError: If the caller cancels the operation.
    """
    result = await request(
        url=url,
        data={'value': 2},
        protocol='JSONRPC',
        protocol_info={'method': 'demo.double', 'request_id': 1},
    )
    if result['ok']:
        print(result['protocol_details']['result'])
    else:
        print(result['error'])
    return result


if __name__ == '__main__':
    asyncio.run(call(*sys.argv[1:]))

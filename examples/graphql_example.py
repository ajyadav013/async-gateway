"""Send one GraphQL query through the public gateway entry point.

Run this against a loopback GraphQL endpoint::

    python examples/graphql_example.py http://127.0.0.1:8080/graphql
"""

import asyncio
import sys

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.utils.envelope import GatewayResponse

DEFAULT_URL = 'http://127.0.0.1:8080/graphql'
QUERY = 'query GetWidget($id: ID!) { widget(id: $id) { id } }'


async def call(url: str = DEFAULT_URL) -> GatewayResponse:
    """Query one widget and return the common response envelope.

    Args:
        url: Absolute HTTP(S) GraphQL endpoint.
    Returns:
        The common gateway response envelope.
    Raises:
        ConfigurationError: If ``url`` cannot form a GraphQL call.
        asyncio.CancelledError: If the caller cancels the operation.
    """
    result = await request(
        url=url,
        data={'id': 'w-1'},
        protocol='GRAPHQL',
        protocol_info={'query': QUERY, 'operation_name': 'GetWidget'},
    )
    if result['ok']:
        print(result['protocol_details']['data'])
    else:
        print(result['error'])
    return result


if __name__ == '__main__':
    asyncio.run(call(*sys.argv[1:]))

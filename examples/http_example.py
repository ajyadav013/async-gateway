"""One HTTP GET, and how to read the envelope it returns.

    python examples/http_example.py http://127.0.0.1:8080/orders

``request()`` returns the same key set for every protocol and on both
the success and the failure path, so ``result['ok']`` is the only
success check -- never the status code, never a truthy body.
"""

import asyncio
import sys
from typing import Any, Dict

from async_gateway.async_gateway import request

DEFAULT_URL = 'http://127.0.0.1:8080/orders'


async def fetch(url: str = DEFAULT_URL) -> Dict[str, Any]:
    """Fetch ``url`` with a GET and report what came back.

    Args:
        url: The absolute URL to call. ``protocol='HTTP'`` accepts
            ``http://`` and ``https://``; ``'HTTPS'`` accepts only
            ``https://`` and upgrades a schemeless URL to it.

    Returns:
        The response envelope, whatever the outcome.
    """
    result = await request(
        url=url,
        protocol='HTTP',
        protocol_info={'request_type': 'GET', 'timeout': 10},
    )
    if result['ok']:
        print(result['status_code'], f'{result["latency"]:.3f}s')
        print(result['json'] if result['json'] is not None
              else result['text'])
    else:
        print(result['error']['code'], result['error']['message'])
    return result


if __name__ == '__main__':
    asyncio.run(fetch(*sys.argv[1:]))

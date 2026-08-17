"""The two ways a call fails, and why you must handle both.

Run it against any local endpoint that answers 404::

    python examples/error_handling_example.py http://127.0.0.1:8080/missing

**Most failures are envelopes.** Every remote and transport failure --
a 404, a timeout, a refused connection, a SOAP Fault, an open circuit --
comes back with ``ok=False`` and a populated ``error``. The response is
*not* lost with it: a 404 keeps its ``status_code``, ``headers``,
``text`` and ``json``, which is usually the part you need.

**Some configuration errors escape.** A ``protocol_info`` that cannot
form a valid call at all raises ``ConfigurationError`` synchronously,
before anything is dispatched, because no retry would help. Catch both.
"""

import asyncio
import sys
from typing import Any, Dict

from async_gateway.async_gateway import request
from async_gateway.utils.exceptions import ConfigurationError

DEFAULT_URL = 'http://127.0.0.1:8080/missing'


async def call(url: str = DEFAULT_URL) -> Dict[str, Any]:
    """Call ``url`` and handle both failure shapes.

    Args:
        url: The absolute URL to call.

    Returns:
        The response envelope, or ``{}`` when the call never dispatched.
    """
    try:
        result = await request(
            url=url,
            protocol='HTTP',
            protocol_info={'request_type': 'GET', 'timeout': 10},
        )
    except ConfigurationError as exc:
        print(f'never dispatched: {exc}')
        return {}

    if not result['ok']:
        error = result['error']
        # 'HTTP_STATUS', 'TIMEOUT', 'CONNECT', 'DNS', 'TLS', 'CONFIG',
        # 'CIRCUIT_OPEN', 'SOAP_FAULT', ... -- see utils/exceptions.py.
        print(f'{error["code"]}: {error["message"]} (cause: {error["cause"]})')
        print(f'body survived: {result["status_code"]} {result["json"]}')
        return result

    print(f'ok: {result["status_code"]}')
    return result


if __name__ == '__main__':
    asyncio.run(call(*sys.argv[1:]))

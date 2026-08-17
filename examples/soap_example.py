"""One SOAP 1.1 call, and where the parsed response body lands.

Run it against a local SOAP endpoint::

    python examples/soap_example.py http://127.0.0.1:8080/rates

You supply the request body as **XML text** (or an ``Element``); this
library builds no XML from a dict and generates nothing from a WSDL. The
``<Envelope>`` and ``<Body>`` wrapper, the version-correct Content-Type
and the SOAPAction header are added for you. The verb is always POST.

The parsed reply is an ``Element`` at
``result['protocol_details']['soap_body']``, and a Fault is a failure:
``ok`` is False with ``error['code'] == 'SOAP_FAULT'``.
"""

import asyncio
import sys
from typing import Any, Dict

from async_gateway.async_gateway import request

DEFAULT_URL = 'http://127.0.0.1:8080/rates'
BODY = '<GetRate xmlns="urn:rates"><Pair>EURUSD</Pair></GetRate>'


async def call(url: str = DEFAULT_URL) -> Dict[str, Any]:
    """POST one SOAP envelope to ``url`` and report the parsed reply.

    Args:
        url: The absolute URL of the SOAP endpoint.

    Returns:
        The response envelope, whatever the outcome.
    """
    result = await request(
        url=url,
        data=BODY,
        protocol='SOAP',
        protocol_info={
            'soap_version': '1.1',              # or '1.2'
            'soap_action': 'urn:rates#GetRate',
            'timeout': 30,
        },
    )
    body = result['protocol_details']['soap_body']
    if result['ok'] and body is not None:
        print(body.tag, [(child.tag, child.text) for child in body])
    else:
        print(result['error'], result['protocol_details']['soap_fault'])
    return result


if __name__ == '__main__':
    asyncio.run(call(*sys.argv[1:]))

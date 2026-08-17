"""A minimal SOAP 1.1 / 1.2 endpoint, for integration-testing the client.

No standard SOAP container image exists, so this is the smallest real
server that can answer the four things ``logic/soap_client.py`` actually
has to get right against a live peer:

* a **1.1** reply -- ``text/xml``, the 1.1 envelope namespace, and a
  request that must carry a ``SOAPAction`` header;
* a **1.2** reply -- ``application/soap+xml`` with the action as a
  ``action=`` content-type parameter and *no* ``SOAPAction`` header;
* a **Fault** in each version's own shape, at the status each version
  sends it with (500 for 1.1, 500 for a 1.2 Receiver fault); and
* a non-SOAP error, so the client's "a 4xx that carried no Fault" branch
  sees a real one.

It is deliberately not a SOAP *framework*: it does not read a WSDL, does
not validate against a schema, and answers a fixed set of operations. The
client under test builds no XML from a dict and generates nothing from a
WSDL either, so a fuller server would test code that does not exist.

Every handler echoes what it received into the reply, so a test can assert
the client sent the envelope, the header and the content type it claims
to -- observed at the server, not mocked in-process.
"""

import os
from typing import Dict, Optional, Tuple
from xml.etree.ElementTree import Element, fromstring

from aiohttp import web

SOAP_11_NS = 'http://schemas.xmlsoap.org/soap/envelope/'
SOAP_12_NS = 'http://www.w3.org/2003/05/soap-envelope'

RATES: Dict[str, str] = {
    'EURUSD': '1.0842',
    'GBPUSD': '1.2671',
    'USDJPY': '156.93',
}


def parse_body(text: str) -> Tuple[Optional[Element], str]:
    """Return the first element child of ``<Body>`` and its namespace.

    Args:
        text: The raw request body.

    Returns:
        The first element inside the SOAP ``<Body>`` (or None when the
        envelope has none), and the envelope namespace it was found in.

    Raises:
        web.HTTPBadRequest: If the payload is not a parseable SOAP
            envelope with a ``<Body>``.
    """
    try:
        envelope = fromstring(text)
    except Exception as err:                       # noqa: BLE001
        raise web.HTTPBadRequest(text=f'not XML: {err}') from err

    namespace = envelope.tag.split('}')[0].lstrip('{')
    body = envelope.find(f'{{{namespace}}}Body')
    if body is None:
        raise web.HTTPBadRequest(text='envelope carries no Body')
    children = list(body)
    return (children[0] if children else None), namespace


def fault_11(code: str, reason: str, detail: str = '') -> str:
    """Return a SOAP 1.1 Fault envelope.

    Args:
        code: The ``<faultcode>`` value, e.g. ``soap:Client``.
        reason: The ``<faultstring>`` value.
        detail: Optional free text for ``<detail>``.

    Returns:
        The serialised envelope.
    """
    return (
        f'<soap:Envelope xmlns:soap="{SOAP_11_NS}"><soap:Body>'
        f'<soap:Fault>'
        f'<faultcode>{code}</faultcode>'
        f'<faultstring>{reason}</faultstring>'
        f'<faultactor>urn:rates:actor</faultactor>'
        f'<detail><reason>{detail}</reason></detail>'
        f'</soap:Fault></soap:Body></soap:Envelope>'
    )


def fault_12(code: str, reason: str, detail: str = '') -> str:
    """Return a SOAP 1.2 Fault envelope.

    Args:
        code: The ``<Value>`` of ``<Code>``, e.g. ``soap:Sender``.
        reason: The ``<Text>`` of ``<Reason>``.
        detail: Optional free text for ``<Detail>``.

    Returns:
        The serialised envelope.
    """
    return (
        f'<soap:Envelope xmlns:soap="{SOAP_12_NS}"><soap:Body>'
        f'<soap:Fault>'
        f'<soap:Code><soap:Value>{code}</soap:Value></soap:Code>'
        f'<soap:Reason><soap:Text xml:lang="en">{reason}</soap:Text>'
        f'</soap:Reason>'
        f'<soap:Role>urn:rates:role</soap:Role>'
        f'<soap:Detail><reason>{detail}</reason></soap:Detail>'
        f'</soap:Fault></soap:Body></soap:Envelope>'
    )


def ok_11(pair: str, rate: str, action: str) -> str:
    """Return a successful SOAP 1.1 reply.

    Args:
        pair: The currency pair that was asked for.
        rate: The rate to report.
        action: The ``SOAPAction`` the request carried, echoed back so a
            test can assert it crossed the wire.

    Returns:
        The serialised envelope.
    """
    return (
        f'<soap:Envelope xmlns:soap="{SOAP_11_NS}"><soap:Body>'
        f'<GetRateResponse xmlns="urn:rates">'
        f'<Pair>{pair}</Pair><Rate>{rate}</Rate>'
        f'<EchoedAction>{action}</EchoedAction>'
        f'</GetRateResponse>'
        f'</soap:Body></soap:Envelope>'
    )


def ok_12(pair: str, rate: str, action: str) -> str:
    """Return a successful SOAP 1.2 reply.

    Args:
        pair: The currency pair that was asked for.
        rate: The rate to report.
        action: The action parsed off the request's content type.

    Returns:
        The serialised envelope.
    """
    return (
        f'<soap:Envelope xmlns:soap="{SOAP_12_NS}"><soap:Body>'
        f'<GetRateResponse xmlns="urn:rates">'
        f'<Pair>{pair}</Pair><Rate>{rate}</Rate>'
        f'<EchoedAction>{action}</EchoedAction>'
        f'</GetRateResponse>'
        f'</soap:Body></soap:Envelope>'
    )


async def rates(request: web.Request) -> web.Response:
    """Answer a ``GetRate`` call in whichever SOAP version was used.

    The version is taken from the *envelope namespace* the client
    actually sent, not from a query parameter, so the reply proves the
    client built the envelope for the version it was configured with.

    Args:
        request: The inbound aiohttp request.

    Returns:
        A SOAP reply, or a Fault for an unknown pair.
    """
    text = await request.text()
    first, namespace = parse_body(text)
    is_12 = namespace == SOAP_12_NS

    content_type = request.headers.get('Content-Type', '')
    if is_12:
        # 1.2 carries the action as a content-type parameter.
        action = ''
        for part in content_type.split(';'):
            name, _, value = part.strip().partition('=')
            if name == 'action':
                action = value.strip('"')
    else:
        action = request.headers.get('SOAPAction', '').strip('"')

    if first is None:
        body = fault_12 if is_12 else fault_11
        return web.Response(
            status=500,
            text=body('soap:Sender', 'empty body', 'no operation element'),
            content_type=(
                'application/soap+xml' if is_12 else 'text/xml'))

    pair_el = first.find('{urn:rates}Pair')
    pair = (pair_el.text or '') if pair_el is not None else ''

    if pair not in RATES:
        # The Fault path. Both versions send it at 500, which is also the
        # case that proves "a Fault beats the status" in the client.
        text_out = (fault_12 if is_12 else fault_11)(
            'soap:Sender' if is_12 else 'soap:Client',
            f'unknown currency pair {pair!r}',
            'supported: ' + ', '.join(sorted(RATES)))
        return web.Response(
            status=500,
            text=text_out,
            content_type=(
                'application/soap+xml' if is_12 else 'text/xml'))

    reply = (ok_12 if is_12 else ok_11)(pair, RATES[pair], action)
    return web.Response(
        status=200,
        text=reply,
        content_type='application/soap+xml' if is_12 else 'text/xml')


async def not_soap(request: web.Request) -> web.Response:
    """Answer with a non-SOAP error body.

    Exercises the client's "a 4xx or 5xx that carried no Fault" branch,
    which must report an HTTP status error rather than a Fault.

    Args:
        request: The inbound aiohttp request.

    Returns:
        A 503 carrying plain text.
    """
    return web.Response(
        status=503, text='backend unavailable', content_type='text/plain')


async def health(request: web.Request) -> web.Response:
    """Report readiness so compose can gate dependents on this server.

    Args:
        request: The inbound aiohttp request.

    Returns:
        A 200 with a fixed body.
    """
    return web.Response(text='ok')


def build_app() -> web.Application:
    """Wire the routes this endpoint serves.

    Returns:
        The configured aiohttp application.
    """
    app = web.Application()
    app.router.add_post('/rates', rates)
    app.router.add_post('/not-soap', not_soap)
    app.router.add_get('/health', health)
    return app


if __name__ == '__main__':
    web.run_app(
        build_app(),
        host='0.0.0.0',                                 # noqa: S104
        port=int(os.environ.get('PORT', '8080')))

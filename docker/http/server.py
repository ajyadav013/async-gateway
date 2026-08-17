"""A small HTTP/HTTPS server for integration-testing the gateway client.

Purpose-written rather than pulled from an image, because the properties
under test are not things a static file server can show you. The client's
HTTP path has to be exercised against a peer that will:

* answer every verb in the allowlist and report what it received, so the
  request the client *built* can be asserted from the server side;
* issue **same-origin and cross-origin redirects**, so the credential
  stripping on a cross-origin hop is observable -- the second hop reports
  which headers actually arrived, which is the only way to prove a header
  was dropped rather than merely intended to be;
* serve the same routes over **TLS**, from a certificate signed by the
  throwaway CA in ``docker/pki``; and
* fail in specific ways -- 404, 500, a slow response, a body larger than
  the client's cap -- so the error envelope is exercised against real
  responses rather than constructed ones.

It runs plaintext and TLS in one process on two ports, so a redirect can
cross from one origin to another without a second container.
"""

import asyncio
import os
import ssl
from typing import Any, Dict

from aiohttp import web


def request_report(request: web.Request, body: str = '') -> Dict[str, Any]:
    """Describe an inbound request as JSON the test can assert on.

    Headers are reported lower-cased and in full. That is deliberate:
    the credential-stripping tests need to prove a header is *absent*,
    and an allowlisted report could not distinguish "not forwarded" from
    "not reported".

    Args:
        request: The inbound aiohttp request.
        body: The already-read request body, if any.

    Returns:
        A JSON-serialisable description of the request.
    """
    return {
        'method': request.method,
        'path': request.path,
        'query': dict(request.query),
        'headers': {k.lower(): v for k, v in request.headers.items()},
        'body': body,
        'scheme': request.scheme,
        'host': request.host,
    }


async def echo(request: web.Request) -> web.Response:
    """Echo the request back as JSON.

    Args:
        request: The inbound aiohttp request.

    Returns:
        A 200 carrying the request report.
    """
    body = await request.text()
    return web.json_response(request_report(request, body))


async def status(request: web.Request) -> web.Response:
    """Answer with the status code named in the path.

    The body is still a full request report, which is what proves the
    library's invariant E11 -- an ``ok=False`` envelope must not cost the
    caller the response body.

    Args:
        request: The inbound aiohttp request.

    Returns:
        A response at the requested status.
    """
    code = int(request.match_info['code'])
    return web.json_response(request_report(request), status=code)


async def slow(request: web.Request) -> web.Response:
    """Answer after a delay, to drive the client's timeout path.

    Args:
        request: The inbound aiohttp request.

    Returns:
        A 200, eventually.
    """
    await asyncio.sleep(float(request.query.get('seconds', '5')))
    return web.json_response({'slept': True})


async def large(request: web.Request) -> web.StreamResponse:
    """Stream more bytes than the caller's cap, without buffering them.

    Args:
        request: The inbound aiohttp request.

    Returns:
        A streamed response of the requested size.
    """
    total = int(request.query.get('bytes', '1048576'))
    response = web.StreamResponse(status=200)
    response.content_type = 'application/octet-stream'
    await response.prepare(request)
    chunk = b'x' * 8192
    sent = 0
    while sent < total:
        await response.write(chunk[:min(len(chunk), total - sent)])
        sent += len(chunk)
    await response.write_eof()
    return response


def nested_multipart_body(
    depth: int, leaf: bytes, per_level: int = 0,
) -> tuple[bytes, str]:
    """Build a genuinely nested ``multipart/mixed`` body ``depth`` levels deep.

    Each level gets its own boundary, because a nested part whose boundary
    repeated its parent's would be terminated by the parent's closing
    delimiter rather than nesting under it.

    With ``per_level`` at its default the body is almost entirely
    boundaries, which is the shape NEW-H2 is about: 2000 levels is ~221 KB,
    three orders of magnitude under the client's byte ceiling, so the depth
    cap is the only thing that can refuse it.

    ``per_level`` instead puts an ordinary leaf part of that many bytes at
    *every* level, alongside the nested one. That is what makes a byte
    total genuinely accumulated across levels distinguishable from one
    reset at each: no single level's payload reaches the ceiling, only the
    sum does.

    Args:
        depth: How many reader levels the body should have, counting the
            outermost as 1.
        leaf: The body bytes of the single ordinary part at the bottom.
        per_level: Bytes of additional leaf payload to place at each
            level, or 0 for none.

    Returns:
        The complete body and the outermost boundary, which the response
        ``Content-Type`` has to name.
    """
    boundaries = [f'agwdeep{level}'.encode() for level in range(depth)]

    def leaf_part(boundary: bytes, payload: bytes) -> bytes:
        """Render one ordinary part.

        Args:
            boundary: The boundary of the level the part sits at.
            payload: The part's body bytes.

        Returns:
            The delimiter, headers and body, ready to concatenate.
        """
        return b''.join([
            b'--', boundary, b'\r\n',
            b'Content-Type: text/plain\r\n\r\n', payload, b'\r\n',
        ])

    filler = b'p' * per_level
    body = b''.join([
        leaf_part(boundaries[-1], leaf),
        leaf_part(boundaries[-1], filler) if per_level else b'',
        b'--', boundaries[-1], b'--\r\n',
    ])
    for level in range(depth - 2, -1, -1):
        outer, inner = boundaries[level], boundaries[level + 1]
        body = b''.join([
            b'--', outer, b'\r\n',
            b'Content-Type: multipart/mixed; boundary=', inner, b'\r\n\r\n',
            body, b'\r\n',
            leaf_part(outer, filler) if per_level else b'',
            b'--', outer, b'--\r\n',
        ])
    return body, boundaries[0].decode()


async def nested_multipart(request: web.Request) -> web.StreamResponse:
    """Serve a real nested multipart body at a caller-chosen depth.

    The in-process suite already pins the depth cap against a loopback
    server, but the walk this drives is the one NEW-H2 rewrote from
    recursive to iterative, and the failure it fixed was the interpreter
    refusing to go further -- a resource the wire can influence through
    chunking and arrival timing. So it is worth watching the same cap hold
    over a real socket, at ``MAX_MULTIPART_DEPTH`` and past it.

    Streamed in chunks with no declared ``Content-Length`` on purpose: a
    client that refused deep bodies by reading a length header would pass
    a buffered version of this test while the depth guard itself was
    dead.

    Args:
        request: The inbound aiohttp request.

    Returns:
        A streamed multipart response nested to the requested depth.
    """
    depth = int(request.query.get('depth', '1'))
    leaf = request.query.get('leaf', 'bottom').encode()
    per_level = int(request.query.get('per_level', '0'))
    body, boundary = nested_multipart_body(depth, leaf, per_level)

    response = web.StreamResponse(
        status=200,
        headers={'Content-Type': f'multipart/mixed; boundary={boundary}'},
    )
    await response.prepare(request)
    for at in range(0, len(body), 512):
        await response.write(body[at:at + 512])
    await response.write_eof()
    return response


async def redirect(request: web.Request) -> web.Response:
    """Redirect to an arbitrary absolute URL.

    The target comes in as a query parameter so one route serves both the
    same-origin and the cross-origin case; which one it is depends only
    on what the test asks for.

    Args:
        request: The inbound aiohttp request.

    Returns:
        A redirect at the requested status.
    """
    target = request.query['to']
    code = int(request.query.get('code', '302'))
    return web.Response(status=code, headers={'Location': target})


async def health(request: web.Request) -> web.Response:
    """Report readiness so compose can gate dependents on this server.

    Args:
        request: The inbound aiohttp request.

    Returns:
        A 200 with a fixed body.
    """
    return web.Response(text='ok')


def build_app() -> web.Application:
    """Wire the routes this server offers.

    Returns:
        The configured aiohttp application.
    """
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app.router.add_route('*', '/echo', echo)
    app.router.add_route('*', '/status/{code:\\d+}', status)
    app.router.add_get('/slow', slow)
    app.router.add_get('/large', large)
    app.router.add_get('/nested-multipart', nested_multipart)
    app.router.add_route('*', '/redirect', redirect)
    app.router.add_get('/health', health)
    return app


async def main() -> None:
    """Serve the app on the plaintext port and, if configured, on TLS."""
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get('PORT', '8080'))
    await web.TCPSite(runner, '0.0.0.0', port).start()   # noqa: S104
    print(f'http: listening on {port}', flush=True)

    cert = os.environ.get('TLS_CERT')
    key = os.environ.get('TLS_KEY')
    if cert and key:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(cert, key)
        tls_port = int(os.environ.get('TLS_PORT', '8443'))
        await web.TCPSite(
            runner, '0.0.0.0', tls_port,                 # noqa: S104
            ssl_context=context).start()
        print(f'https: listening on {tls_port}', flush=True)

    # A second TLS port presenting a certificate for a DIFFERENT host,
    # signed by the SAME trusted CA. It is what separates "the client
    # verifies the chain" from "the client verifies the host name" -- a
    # client doing only the former would accept this one.
    wrong_cert = os.environ.get('WRONG_TLS_CERT')
    wrong_key = os.environ.get('WRONG_TLS_KEY')
    if wrong_cert and wrong_key:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(wrong_cert, wrong_key)
        wrong_port = int(os.environ.get('WRONG_TLS_PORT', '9443'))
        await web.TCPSite(
            runner, '0.0.0.0', wrong_port,               # noqa: S104
            ssl_context=context).start()
        print(f'https(wrong-host): listening on {wrong_port}', flush=True)

    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())

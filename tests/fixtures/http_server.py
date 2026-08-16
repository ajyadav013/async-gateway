"""Loopback HTTP server test double built on ``aiohttp.web``.

Starts a real HTTP server on ``127.0.0.1`` through
``aiohttp.test_utils.TestServer``, records every request it receives, and
replays a caller-registered status, headers and body per path. HTTP and SOAP
tests therefore assert against what was actually put on the wire -- method,
path, query, headers, raw body bytes -- read off the server rather than off
a mock's ledger.

No aiohttp-mocking library is used (release Ruling A). ``aioresponses``
0.7.9 is its terminal release and raises ``TypeError:
ClientResponse.__init__() missing 1 required keyword-only argument:
'stream_writer'`` on every mocked request against the aiohttp 3.14.x line
this release targets, while still resolving cleanly against it.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field

from aiohttp import web
from aiohttp.test_utils import TestServer

from multidict import CIMultiDictProxy, MultiDictProxy

import pytest


@dataclass(frozen=True)
class RecordedRequest:
    """One request exactly as the loopback server received it.

    Attributes:
        method: HTTP method in upper case (``GET``, ``POST``, ...).
        path: Request path, without the query string.
        query: Decoded query-string parameters.
        headers: Request headers; lookup is case-insensitive.
        body: Raw request body bytes, unmodified.
    """

    method: str
    path: str
    query: MultiDictProxy[str]
    headers: CIMultiDictProxy[str]
    body: bytes


@dataclass(frozen=True)
class ResponseSpec:
    """The canned response the server returns for one registered path.

    Attributes:
        status: HTTP status code to return.
        body: Response body bytes.
        headers: Response headers to emit (e.g. ``Location`` for a redirect).
    """

    status: int = 200
    body: bytes = b''
    headers: Mapping[str, str] = field(default_factory=dict)


class RecordingHTTPServer:
    """A loopback ``aiohttp.web`` server that records what it receives.

    Register a response per path with :meth:`respond`, point the code under
    test at :meth:`url_for`, then assert against :attr:`requests`. A path
    with no registered response answers ``404`` and is still recorded, so a
    request the code under test should never have sent is visible rather
    than silent.

    Usage:
        server = RecordingHTTPServer()
        await server.start()
        server.respond('/ping', body=b'pong')
        ...
        await server.close()
    """

    def __init__(self) -> None:
        """Build the application and its catch-all recording route."""
        self._responses: dict[str, ResponseSpec] = {}
        self._requests: list[RecordedRequest] = []
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', self._handle)
        self._server = TestServer(app)

    @property
    def requests(self) -> list[RecordedRequest]:
        """Return every request received so far, in arrival order.

        Returns:
            A copy of the recording, so a caller cannot mutate the log.
        """
        return list(self._requests)

    async def start(self) -> None:
        """Bind the server to an ephemeral port on ``127.0.0.1``.

        Returns:
            None.
        """
        await self._server.start_server()

    async def close(self) -> None:
        """Shut the server down and release its port.

        Returns:
            None.
        """
        await self._server.close()

    def url_for(self, path: str) -> str:
        """Return the absolute URL of ``path`` on this server.

        Args:
            path: Path to address, optionally carrying a query string.

        Returns:
            An absolute ``http://127.0.0.1:<port>`` URL as a string.
        """
        return str(self._server.make_url(path))

    def respond(
        self,
        path: str,
        *,
        status: int = 200,
        body: bytes = b'',
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Register the response this server returns for ``path``.

        Each hop of a redirect chain is registered as its own path, so every
        hop the client follows arrives as its own recorded request.

        Args:
            path: Path the response is registered for, e.g. ``/soap``.
            status: HTTP status code to return.
            body: Response body bytes.
            headers: Response headers to emit, or None for none.

        Returns:
            None.
        """
        self._responses[path] = ResponseSpec(
            status=status,
            body=body,
            headers=dict(headers or {}),
        )

    async def _handle(self, request: web.Request) -> web.Response:
        """Record the request, then answer it from the registered specs.

        Args:
            request: The inbound aiohttp server request.

        Returns:
            The registered response for the path, or an empty ``404``.
        """
        self._requests.append(
            RecordedRequest(
                method=request.method,
                path=request.path,
                query=request.query,
                headers=request.headers,
                body=await request.read(),
            ),
        )
        spec = self._responses.get(request.path)
        if spec is None:
            return web.Response(status=404, body=b'')
        return web.Response(
            status=spec.status,
            body=spec.body,
            headers=spec.headers,
        )


@pytest.fixture
async def http_server() -> AsyncIterator[RecordingHTTPServer]:
    """Yield a started :class:`RecordingHTTPServer`, closed on teardown.

    Yields:
        A running loopback server with an empty recording.
    """
    server = RecordingHTTPServer()
    await server.start()
    try:
        yield server
    finally:
        await server.close()

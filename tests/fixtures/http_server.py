"""Loopback HTTP server test double built on ``aiohttp.web``.

Starts a real HTTP server on ``127.0.0.1`` through
``aiohttp.test_utils.TestServer``, records every request it receives, and
replays a caller-registered status, headers and body per method and path.
HTTP and SOAP tests therefore assert against what was actually put on the
wire -- method, raw path, raw query string, headers, raw body bytes -- read
off the server rather than off a mock's ledger.

Wire-exactness is stated precisely because it used not to hold.
:attr:`RecordedRequest.path` and :attr:`RecordedRequest.raw_query` are the
bytes as sent, still percent-encoded; :attr:`RecordedRequest.query` is the
*decoded* parameter mapping, which is a convenience view and says so.
Registration and lookup use the decoded path, so ``respond('/a b')``
answers a request for ``/a%20b``.

No aiohttp-mocking library is used (release Ruling A). ``aioresponses``
0.7.9 is its terminal release and raises ``TypeError:
ClientResponse.__init__() missing 1 required keyword-only argument:
'stream_writer'`` on every mocked request against the aiohttp 3.14.x line
this release targets, while still resolving cleanly against it.

Beyond the canned status/body, the server can answer differently per
method, answer a **sequence** of responses on one path (the shape a retry
test needs), **block** on an ``asyncio.Event`` until a test releases it
(the shape a timeout test needs), answer **late** (the shape a deadline
spanning several hops needs), declare a **Content-Length that does not
match the body**, and stream a **chunked** body with no ``Content-Length``
at all (the two shapes a response-size cap has to be exercised against).
"""

import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from aiohttp import web
from aiohttp.test_utils import TestServer

from multidict import CIMultiDict, CIMultiDictProxy, MultiDictProxy

import pytest

#: The registered method that answers a request of any method. A real
#: method never collides with it, because HTTP methods are tokens.
ANY_METHOD = '*'

#: Header pairs, rather than a mapping, because a mapping cannot express a
#: repeated header and two ``Set-Cookie`` lines is the ordinary case that
#: needs one.
HeaderPairs = Sequence[tuple[str, str]]


def _as_pairs(
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None,
) -> tuple[tuple[str, str], ...]:
    """Normalise a header argument to an ordered tuple of pairs.

    Args:
        headers: A mapping, an iterable of ``(name, value)`` pairs, or
            None. A mapping is still accepted because it is what almost
            every caller wants and reads best at the call site; pairs are
            what a repeated header needs.

    Returns:
        The headers as pairs, in the order given.
    """
    if headers is None:
        return ()
    if isinstance(headers, Mapping):
        return tuple(headers.items())
    return tuple(headers)


@dataclass(frozen=True)
class RecordedRequest:
    """One request exactly as the loopback server received it.

    Attributes:
        method: HTTP method in upper case (``GET``, ``POST``, ...).
        path: Request path as sent, still percent-encoded, without the
            query string.
        raw_query: The query string as sent, still percent-encoded; ``''``
            when there was none.
        query: Decoded query-string parameters -- a convenience view, not
            the wire bytes.
        headers: Request headers; lookup is case-insensitive.
        body: Raw request body bytes, as far as they arrived.
        body_complete: False when the client went away before the body
            ended, in which case ``body`` holds the part that did arrive.
            A partial request is recorded rather than dropped, so a client
            that aborted mid-upload is visible rather than silent.
    """

    method: str
    path: str
    raw_query: str
    query: MultiDictProxy[str]
    headers: CIMultiDictProxy[str]
    body: bytes
    body_complete: bool


@dataclass(frozen=True)
class ResponseSpec:
    """The canned response the server returns for one registered path.

    Attributes:
        status: HTTP status code to return.
        body: Response body bytes. Ignored when ``chunks`` is set.
        headers: Response headers to emit, as ordered pairs, so a repeated
            header (two ``Set-Cookie`` lines) is expressible.
        content_length: A ``Content-Length`` to declare *instead of* the
            one the body implies, or None to let aiohttp compute it. A
            declared length that the body does not match is exactly what a
            pre-read size guard has to be tested against, and it cannot be
            produced by a plain ``web.Response``.
        chunks: Body pieces to stream with chunked transfer-encoding and
            no ``Content-Length``, or None for an ordinary body. This is
            the case a *mid-stream* size guard has to be tested against,
            since there is no declared length to reject up front.
        gate: An event the handler awaits before it answers anything, or
            None to answer immediately. A response that never arrives is
            how a deadline is exercised without a sleep.
        delay: Seconds the handler waits before answering, or 0 to answer
            at once. Distinct from ``gate``, which never answers at all: a
            chain of hops that each answer *slowly* is the only shape that
            can tell a deadline spent once over the whole exchange from
            one handed afresh to every hop, and neither an immediate
            answer nor an absent one can express it.
    """

    status: int = 200
    body: bytes = b''
    headers: HeaderPairs = field(default_factory=tuple)
    content_length: int | None = None
    chunks: Sequence[bytes] | None = None
    gate: asyncio.Event | None = None
    delay: float = 0.0


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
        self._responses: dict[tuple[str, str], list[ResponseSpec]] = {}
        self._requests: list[RecordedRequest] = []
        self._gates: list[asyncio.Event] = []
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
        """Release every gate, then shut the server down.

        The gates are released first, and unconditionally: a handler still
        blocked on one would otherwise keep the shutdown waiting on a test
        that has already finished asserting.

        Returns:
            None.
        """
        for gate in self._gates:
            gate.set()
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
        method: str = ANY_METHOD,
        status: int = 200,
        body: bytes = b'',
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        content_length: int | None = None,
        chunks: Sequence[bytes] | None = None,
        delay: float = 0.0,
    ) -> None:
        """Register the response this server returns for ``path``.

        Each hop of a redirect chain is registered as its own path, so every
        hop the client follows arrives as its own recorded request.

        Args:
            path: Path the response is registered for, e.g. ``/soap``.
            method: The method this response answers, or ``'*'`` for any.
                A method-specific registration wins over ``'*'`` on the
                same path.
            status: HTTP status code to return.
            body: Response body bytes.
            headers: Response headers to emit -- a mapping, or pairs when
                a header repeats -- or None for none.
            content_length: A ``Content-Length`` to declare instead of the
                body's own length, or None for the honest one.
            chunks: Stream these pieces with chunked encoding and no
                ``Content-Length``, or None to send ``body`` as usual.
            delay: Seconds to wait before answering, or 0 to answer at
                once.

        Returns:
            None.
        """
        self.respond_in_sequence(
            path,
            [ResponseSpec(
                status=status,
                body=body,
                headers=_as_pairs(headers),
                content_length=content_length,
                chunks=chunks,
                delay=delay,
            )],
            method=method,
        )

    def respond_in_sequence(
        self,
        path: str,
        specs: Sequence[ResponseSpec],
        *,
        method: str = ANY_METHOD,
    ) -> None:
        """Register responses answered in order, the last one repeating.

        "One failure then one success" is the shape a retry has to be
        driven with, and a single canned response per path cannot express
        it. The final spec repeats rather than falling off the end into a
        404, so a test that retries once more than it meant to fails on
        the assertion it wrote instead of on a surprise status.

        Args:
            path: Path the responses are registered for.
            specs: The responses, in the order they will be answered. Must
                not be empty.
            method: The method they answer, or ``'*'`` for any.

        Returns:
            None.

        Raises:
            ValueError: If ``specs`` is empty -- an empty sequence would
                silently register nothing.
        """
        if not specs:
            raise ValueError('respond_in_sequence needs at least one spec')
        self._responses[(method, path)] = list(specs)

    def gate(
        self,
        path: str,
        *,
        method: str = ANY_METHOD,
        status: int = 200,
        body: bytes = b'',
    ) -> asyncio.Event:
        """Register a handler that answers ``path`` only once released.

        The event is returned so a test can release it deliberately, and it
        is also released by :meth:`close`, so a test that asserts a
        deadline and never releases it still tears down.

        Args:
            path: Path the blocked response is registered for.
            method: The method it answers, or ``'*'`` for any.
            status: The status to answer with, once released.
            body: The body to answer with, once released.

        Returns:
            The event the handler is waiting on.
        """
        event = asyncio.Event()
        self._gates.append(event)
        self.respond_in_sequence(
            path,
            [ResponseSpec(status=status, body=body, gate=event)],
            method=method,
        )
        return event

    def _next_spec(self, request: web.Request) -> ResponseSpec | None:
        """Return the response ``request`` is answered with, if any.

        A method-specific registration is preferred over the any-method
        one. Within a registration the specs are consumed in order and the
        last one repeats.

        Args:
            request: The inbound request.

        Returns:
            The spec to answer with, or None when nothing is registered.
        """
        for key in ((request.method, request.path), (ANY_METHOD,
                                                     request.path)):
            specs = self._responses.get(key)
            if specs is None:
                continue
            return specs.pop(0) if len(specs) > 1 else specs[0]
        return None

    async def _record(self, request: web.Request) -> None:
        """Record ``request``, whether or not its body arrives in full.

        The body is accumulated into a local buffer and the recording is
        written from a ``finally``, so a client that disconnects mid-upload
        is recorded with what did arrive and ``body_complete`` False. The
        read used to happen inside the ``RecordedRequest(...)`` call, which
        meant an aborted request was recorded not at all -- the one case
        the recording exists to make visible.

        Args:
            request: The inbound request.

        Returns:
            None.
        """
        raw_path, _, raw_query = request.raw_path.partition('?')
        body = bytearray()
        complete = False
        try:
            async for chunk in request.content.iter_any():
                body.extend(chunk)
            complete = True
        finally:
            self._requests.append(
                RecordedRequest(
                    method=request.method,
                    path=raw_path,
                    raw_query=raw_query,
                    query=request.query,
                    headers=request.headers,
                    body=bytes(body),
                    body_complete=complete,
                ),
            )

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        """Record the request, then answer it from the registered specs.

        Args:
            request: The inbound aiohttp server request.

        Returns:
            The registered response for the method and path, or an empty
            ``404``.
        """
        await self._record(request)
        spec = self._next_spec(request)
        if spec is None:
            return web.Response(status=404, body=b'')
        if spec.gate is not None:
            await spec.gate.wait()
        if spec.delay:
            await asyncio.sleep(spec.delay)
        if spec.chunks is not None or spec.content_length is not None:
            return await self._stream(request, spec)
        return web.Response(
            status=spec.status,
            body=spec.body,
            headers=CIMultiDict(spec.headers),
        )

    async def _stream(
        self,
        request: web.Request,
        spec: ResponseSpec,
    ) -> web.StreamResponse:
        """Answer with a chunked body, or with a declared length of its own.

        Args:
            request: The request being answered.
            spec: The registered response, whose ``chunks`` or
                ``content_length`` put us on this path.

        Returns:
            The prepared, completed response.
        """
        response = web.StreamResponse(
            status=spec.status, headers=CIMultiDict(spec.headers))
        if spec.chunks is not None:
            response.enable_chunked_encoding()
        else:
            response.content_length = spec.content_length
        await response.prepare(request)
        for piece in (spec.body,) if spec.chunks is None else spec.chunks:
            await response.write(piece)
        await response.write_eof()
        return response


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

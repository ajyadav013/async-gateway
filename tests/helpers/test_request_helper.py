"""Tests for the HTTP transport boundary (spec R13/R20, Steps 11-12).

Multipart handling, the body decode, and the download-config defaults --
the three places ``make_http_request`` used to lose or corrupt what the
server sent (M7, M9, M10).

Every assertion here is made against a real ``aiohttp`` exchange with the
loopback recording server, because each defect lives in the interaction
with a real ``aiohttp`` object rather than in this library's own
arithmetic: ``MultipartReader.next()`` really does return None before
``at_eof()`` goes True on an ordinary well-formed body, and a stub reader
that did not would have let the ``AttributeError`` ship.

Step 12 adds R20's two behavioural proofs for this module: that a streamed
download hands the loop back between chunks, and that a retried upload
sends the whole file on every attempt rather than the file once and
nothing afterwards. The package-wide ban that keeps blocking I/O out of
every other ``async def`` lives in ``tests/test_no_blocking_io.py``.
"""

import ast
import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any, Dict, Text

import aiohttp

from async_gateway.async_gateway import request
from async_gateway.helpers.internal.request_helper import (
    DEFAULT_DOWNLOAD_FILEPATH,
    HttpResult,
    file_upload,
    handle_multipart_response,
    make_http_filters_without_stream_uploads,
    make_http_request,
)
from async_gateway.utils.constants import CHUNK_SIZE_CONSTANT

import pytest

from tests.fixtures.http_server import RecordingHTTPServer

BOUNDARY = 'agwtestboundary'
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
#: ``BodyPartReader.chunk_size``. A part of three chunks plus a remainder is
#: the shape that exposes a reader stopping after one ``read_chunk()``.
CHUNK_SIZE = aiohttp.multipart.BodyPartReader.chunk_size


def _multipart_body(*parts: bytes) -> bytes:
    """Assemble a well-formed multipart body from raw part bodies.

    Args:
        parts: The body bytes of each part, in order.

    Returns:
        The complete multipart body, terminated by the closing boundary.
    """
    boundary = BOUNDARY.encode()
    chunks: list[bytes] = []
    for part in parts:
        chunks += [
            b'--', boundary, b'\r\n',
            b'Content-Type: application/octet-stream\r\n\r\n',
            part, b'\r\n',
        ]
    chunks += [b'--', boundary, b'--\r\n']
    return b''.join(chunks)


def _multipart_headers() -> dict[str, str]:
    """Return the response headers announcing a multipart body.

    Returns:
        A ``Content-Type`` naming the boundary the bodies are built with,
        carrying a parameter so the matcher has one to strip.
    """
    return {'Content-Type': f'multipart/mixed; boundary={BOUNDARY}'}


async def _fetch(url: str, **kwargs: object) -> HttpResult:
    """Make one GET through the transport boundary under test.

    Args:
        url: The absolute URL to fetch.
        kwargs: Per-call configuration forwarded to ``make_http_request``,
            e.g. ``http_file_download_config``.

    Returns:
        The ``HttpResult`` the boundary produced.
    """
    async with aiohttp.ClientSession() as session:
        return await make_http_request(
            session,
            url,
            {},
            'GET',
            redact_params=frozenset(),
            **kwargs,
        )


# --- R13: multipart handling (M7) -----------------------------------------

async def test_a_multipart_part_is_written_to_a_binary_file_as_bytes(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    r"""The file holds ``\x89PNG``, not the text ``b'\x89PNG'``.

    ``response_file.write(str(data))`` into a text-mode file wrote the
    *repr* of the bytes -- twelve ASCII characters where the caller
    expected eight binary ones -- so every multipart download this library
    has ever performed produced an unopenable file.
    """
    target = tmp_path / 'downloaded.png'
    http_server.respond(
        '/multipart',
        body=_multipart_body(PNG_MAGIC),
        headers=_multipart_headers(),
    )

    await _fetch(
        http_server.url_for('/multipart'),
        http_file_download_config={'download_filepath': str(target)},
    )

    assert target.read_bytes() == PNG_MAGIC
    assert target.read_bytes()[:4] == b'\x89PNG'


async def test_a_part_larger_than_the_chunk_size_is_written_in_full(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """A part of ``chunk_size * 3 + 7`` bytes arrives whole.

    One ``read_chunk()`` per part truncated every part over 8 KiB at
    exactly 8192 bytes -- silently, because the short file is a perfectly
    valid file.
    """
    target = tmp_path / 'large.bin'
    part = b'x' * (CHUNK_SIZE * 3 + 7)
    http_server.respond(
        '/multipart',
        body=_multipart_body(part),
        headers=_multipart_headers(),
    )

    result = await _fetch(
        http_server.url_for('/multipart'),
        http_file_download_config={'download_filepath': str(target)},
    )

    assert len(target.read_bytes()) == CHUNK_SIZE * 3 + 7
    assert target.read_bytes() == part
    # The accumulator is the same length as the file, which it could not be
    # if it were built from `str(data)` reprs.
    assert len(result['text']) == CHUNK_SIZE * 3 + 7


async def test_several_parts_are_concatenated_in_order(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """Multiple parts land in the file in the order they arrived."""
    target = tmp_path / 'joined.bin'
    http_server.respond(
        '/multipart',
        body=_multipart_body(b'first', b'second', b'third'),
        headers=_multipart_headers(),
    )

    result = await _fetch(
        http_server.url_for('/multipart'),
        http_file_download_config={'download_filepath': str(target)},
    )

    assert target.read_bytes() == b'firstsecondthird'
    assert result['text'] == 'firstsecondthird'


async def test_a_reader_returning_none_terminates_the_loop_cleanly(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """``reader.next()`` returns None before ``at_eof()`` goes True.

    Not a contrived case: it is what a real ``MultipartReader`` does at the
    end of an ordinary, well-formed body, which is why the unguarded
    ``await part.read_chunk()`` on the next line raised ``AttributeError``
    on *every* multipart response rather than on an exotic one.
    """
    target = tmp_path / 'terminates.bin'
    http_server.respond(
        '/multipart',
        body=_multipart_body(b'only'),
        headers=_multipart_headers(),
    )

    result = await _fetch(
        http_server.url_for('/multipart'),
        http_file_download_config={'download_filepath': str(target)},
    )

    assert result['text'] == 'only'
    assert result['status_code'] == 200


async def test_a_zero_part_multipart_body_writes_an_empty_file(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """A multipart response with no parts is empty, not an error."""
    target = tmp_path / 'empty.bin'
    http_server.respond(
        '/multipart',
        body=_multipart_body(),
        headers=_multipart_headers(),
    )

    result = await _fetch(
        http_server.url_for('/multipart'),
        http_file_download_config={'download_filepath': str(target)},
    )

    assert target.read_bytes() == b''
    assert result['text'] == ''


async def test_multipart_falls_back_to_the_documented_default_path(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``download_filepath`` is a documented default, not a crash."""
    monkeypatch.chdir(tmp_path)
    http_server.respond(
        '/multipart',
        body=_multipart_body(b'defaulted'),
        headers=_multipart_headers(),
    )

    await _fetch(
        http_server.url_for('/multipart'),
        http_file_download_config=None,
    )

    assert (tmp_path / DEFAULT_DOWNLOAD_FILEPATH).read_bytes() == b'defaulted'


async def test_multipart_handling_is_reached_whatever_the_header_casing(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The multipart branch used to be unreachable.

    ``str(headers.get('content-type'))`` on a plain dict built from a
    ``CIMultiDict`` looked up the lower-cased key against the server's
    ``Content-Type``, found nothing, and tested the string ``'None'`` for a
    ``multipart`` prefix -- so a multipart response was read as an ordinary
    body and no file was ever written.
    """
    target = tmp_path / 'reached.bin'
    http_server.respond(
        '/multipart',
        body=_multipart_body(b'reached'),
        headers=_multipart_headers(),
    )

    await _fetch(
        http_server.url_for('/multipart'),
        http_file_download_config={'download_filepath': str(target)},
    )

    assert target.read_bytes() == b'reached'


async def test_a_non_multipart_response_writes_no_multipart_file(
    http_server: RecordingHTTPServer,
) -> None:
    """An ordinary body is decoded, not routed to the multipart reader."""
    http_server.respond(
        '/plain',
        body=b'just text',
        headers={'Content-Type': 'text/plain'},
    )

    result = await _fetch(http_server.url_for('/plain'))

    assert result['text'] == 'just text'
    assert 'decode_error' not in result


# --- R13: the body decode (M9) --------------------------------------------

async def test_an_undecodable_body_sets_both_text_and_the_diagnostic(
    http_server: RecordingHTTPServer,
) -> None:
    """Both, never one (M9).

    ``text`` used to be left unset while the error was raised, and
    ``logic/http.py`` then read it unconditionally -- so the caller got a
    ``KeyError`` and a fabricated ``999`` instead of the diagnostic.
    """
    http_server.respond(
        '/binary',
        body=b'\xff\xfe not utf-8',
        headers={'Content-Type': 'text/plain'},
    )

    result = await _fetch(http_server.url_for('/binary'))

    assert result['text']
    assert 'not decodable text' in result['decode_error']
    assert result['status_code'] == 200


async def test_the_decode_diagnostic_carries_a_redacted_url(
    http_server: RecordingHTTPServer,
) -> None:
    """The message reaches the caller, so its URL is masked first."""
    http_server.respond(
        '/binary',
        body=b'\xff',
        headers={'Content-Type': 'text/plain'},
    )

    result = await _fetch(
        f'{http_server.url_for("/binary")}?api_key=supersecret')

    assert 'supersecret' not in result['decode_error']


async def test_a_decodable_body_records_no_decode_error(
    http_server: RecordingHTTPServer,
) -> None:
    """Absence is the success signal, so success must leave it absent."""
    http_server.respond('/text', body='héllo'.encode())

    result = await _fetch(http_server.url_for('/text'))

    assert result['text'] == 'héllo'
    assert 'decode_error' not in result


# --- R12/R13: download-config defaults (M10) ------------------------------

async def test_a_download_config_with_neither_key_still_downloads(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both keys omitted is the all-defaults call, and it works.

    The README documents ``file_download_chunk_size`` as optional; the code
    read it with a bare ``.get()`` and handed the resulting None to
    ``iter_chunked()``, which raises. Following the documentation was the
    way to break it.
    """
    monkeypatch.chdir(tmp_path)
    http_server.respond('/file', body=b'downloaded bytes')

    result = await _fetch(
        http_server.url_for('/file'),
        http_file_download_config={},
    )

    assert (tmp_path / DEFAULT_DOWNLOAD_FILEPATH).read_bytes() == (
        b'downloaded bytes')
    assert result['status_code'] == 200


async def test_a_download_config_without_a_chunk_size_downloads(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The documented shape: a path, and no chunk size."""
    target = tmp_path / 'out.bin'
    payload = b'y' * 5000
    http_server.respond('/file', body=payload)

    await _fetch(
        http_server.url_for('/file'),
        http_file_download_config={'download_filepath': str(target)},
    )

    assert target.read_bytes() == payload


async def test_an_explicit_none_chunk_size_never_reaches_iter_chunked(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """A key present but None is the same absence, and defaults the same."""
    target = tmp_path / 'out.bin'
    http_server.respond('/file', body=b'still works')

    await _fetch(
        http_server.url_for('/file'),
        http_file_download_config={
            'download_filepath': str(target),
            'file_download_chunk_size': None,
        },
    )

    assert target.read_bytes() == b'still works'


async def test_a_caller_supplied_chunk_size_is_honoured(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """A chunk size smaller than the body still assembles the whole file."""
    target = tmp_path / 'out.bin'
    payload = b'z' * 4096
    http_server.respond('/file', body=payload)

    await _fetch(
        http_server.url_for('/file'),
        http_file_download_config={
            'download_filepath': str(target),
            'file_download_chunk_size': 64,
        },
    )

    assert target.read_bytes() == payload


async def test_no_download_config_writes_no_file(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence, not emptiness, is how a caller says "no download"."""
    monkeypatch.chdir(tmp_path)
    http_server.respond('/plain', body=b'in memory')

    result = await _fetch(http_server.url_for('/plain'))

    assert result['text'] == 'in memory'
    assert not (tmp_path / DEFAULT_DOWNLOAD_FILEPATH).exists()


# --- R12: no un-guarded immediate call on a lookup (H14) ------------------

def _immediate_lookup_calls(source: str) -> list[int]:
    """Find every ``<something>.get(...)(...)`` in one module's source.

    Matched on the parsed tree rather than with the spec's literal grep, so
    that prose describing the banned pattern -- including the docstrings
    that explain why it is banned -- cannot be mistaken for the pattern.

    Args:
        source: The module's source text.

    Returns:
        The line number of each offending call, in file order.
    """
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Call)
        and isinstance(node.func.func, ast.Attribute)
        and node.func.func.attr == 'get'
    )


def test_no_mapping_lookup_is_called_immediately_anywhere() -> None:
    """R12's grep criterion, as a test so it cannot regress.

    ``header_filter_mapping.get(content_type)(...)`` called whatever the
    lookup returned, including None. The pattern is banned outright rather
    than fixed at its one site, because the next one would be written the
    same way.
    """
    package = Path(__file__).resolve().parents[2] / 'async_gateway'

    offenders = [
        f'{path}:{lineno}'
        for path in sorted(package.rglob('*.py'))
        for lineno in _immediate_lookup_calls(
            path.read_text(encoding='utf-8'))
    ]

    assert offenders == []


async def test_the_multipart_helper_defaults_its_own_path(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper is safe to call with no config at all.

    Called directly rather than through ``make_http_request``, because the
    None config is the argument shape the transport passes it and the
    helper must not assume a mapping.
    """
    monkeypatch.chdir(tmp_path)
    http_server.respond(
        '/multipart',
        body=_multipart_body(b'direct'),
        headers=_multipart_headers(),
    )

    async with aiohttp.ClientSession() as session:
        async with session.get(
            http_server.url_for('/multipart'),
        ) as response:
            text = await handle_multipart_response(response, None)

    assert text == 'direct'
    assert (tmp_path / DEFAULT_DOWNLOAD_FILEPATH).read_bytes() == b'direct'


# --- R20: the download hands the loop back between chunks -----------------

#: Chunks in the mocked download, and therefore the floor the observer
#: has to clear. Named because the assertion is "one round-trip per
#: chunk", not "some arbitrary number of round-trips".
DOWNLOAD_CHUNK_COUNT = 256

CHUNK_BYTES = b'chunk'


class _ChunkedContent:
    """A ``resp.content`` double that yields a fixed chunk sequence.

    A real loopback download would work too, but the number of chunks it
    produces is decided by TCP rather than by the test, and the assertion
    below counts them.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        """Record the chunks this content will yield.

        Args:
            chunks: The chunks to emit, in order.
        """
        self._chunks = chunks

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        """Yield the recorded chunks.

        Args:
            size: Requested chunk size, ignored -- the chunking is fixed
                so that the count is the test's to state.

        Yields:
            Each recorded chunk in turn.
        """
        for chunk in self._chunks:
            yield chunk

    async def read(self) -> bytes:
        """Return the body left after the stream was drained.

        Returns:
            ``b''``: a streamed download exhausts the reader.
        """
        return b''


class _ChunkedResponse:
    """A response double exposing just what the transport reads off it."""

    status = 200
    headers = {'Content-Type': 'application/octet-stream'}
    cookies: Dict[Text, Any] = {}

    def __init__(self, chunks: list[bytes]) -> None:
        """Attach a content stream yielding ``chunks``.

        Args:
            chunks: The chunks the download will receive.
        """
        self.content = _ChunkedContent(chunks)

    async def __aenter__(self) -> '_ChunkedResponse':
        """Enter the ``async with`` the transport opens.

        Returns:
            This response.
        """
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        """Leave the context without suppressing anything.

        Args:
            exc_info: The exception triple, ignored.

        Returns:
            False, so any exception propagates.
        """
        return False


class _ChunkedSession:
    """A session double whose verb methods return one canned response."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Build the response this session will hand out.

        Args:
            chunks: The chunks the download will receive.
        """
        self._response = _ChunkedResponse(chunks)

    def get(self, url: Text, **filters: object) -> _ChunkedResponse:
        """Return the canned response.

        Args:
            url: Ignored; nothing is dialled.
            filters: Transport keyword arguments, ignored.

        Returns:
            The response double, ready to be entered.
        """
        return self._response


async def test_a_streamed_download_yields_to_the_loop_between_chunks(
    tmp_path: Path,
) -> None:
    """The observer completes at least one round-trip per chunk.

    Stated as a count rather than a duration: no clock is read and no
    delay is waited on, so the test says exactly the thing R20 cares
    about -- the download suspends between chunks -- and says it
    deterministically.

    With the synchronous ``read_file.write(chunk)`` this replaces, the
    whole loop ran without a single suspension point: ``async for`` over
    an already-buffered stream does not yield by itself, so a concurrent
    coroutine in the consuming process got no turn at all until the last
    byte had landed.
    """
    target = tmp_path / 'streamed.bin'
    session = _ChunkedSession([CHUNK_BYTES] * DOWNLOAD_CHUNK_COUNT)
    finished = asyncio.Event()

    async def download() -> None:
        """Run the streamed download, then release the observer."""
        try:
            await make_http_request(
                session,
                'http://download.test/file',
                {},
                'GET',
                redact_params=frozenset(),
                http_file_download_config={
                    'download_filepath': str(target),
                    'file_download_chunk_size': len(CHUNK_BYTES),
                },
            )
        finally:
            finished.set()

    async def observe() -> int:
        """Count loop round-trips until the download reports it is done.

        Returns:
            The number of times the loop came back to this coroutine.
        """
        ticks = 0
        while not finished.is_set():
            await asyncio.sleep(0)
            ticks += 1
        return ticks

    _, ticks = await asyncio.gather(download(), observe())

    assert target.read_bytes() == CHUNK_BYTES * DOWNLOAD_CHUNK_COUNT
    assert ticks >= DOWNLOAD_CHUNK_COUNT


# --- R14/R20: the upload body is built per attempt ------------------------

#: Deliberately free of ``\r\n--`` so the part extractor below cannot be
#: fooled by the payload containing a boundary-shaped sequence.
UPLOAD_BYTES = b'upload-payload-' * 64


class _RepeatingBreaker:
    """A circuit-breaker double running the callable a fixed no. of times.

    ``failsafe.run`` retries on failure; from the callable's point of
    view an unconditional second call is the same shape, and it needs no
    failing transport to provoke. Both attempts hit a server that answers
    200, which is the point: the defect this guards against is silent, so
    the *response* cannot be what reveals it.
    """

    def __init__(self, attempts: int) -> None:
        """Configure how many times each call is run.

        Args:
            attempts: Number of times to invoke the callable.
        """
        self.failsafe = self
        self.attempts = attempts

    async def run(
        self,
        call: Callable[..., Coroutine[Any, Any, HttpResult]],
        *args: object,
        **kwargs: object,
    ) -> HttpResult:
        """Invoke ``call`` ``attempts`` times and return the last result.

        Args:
            call: The coroutine function under retry.
            args: Positional arguments forwarded to it.
            kwargs: Keyword arguments forwarded to it.

        Returns:
            The result of the final attempt.
        """
        result: HttpResult = HttpResult(
            status_code=0, headers={}, cookies={}, text='')
        for _ in range(self.attempts):
            result = await call(*args, **kwargs)
        return result


def _uploaded_part(body: bytes) -> bytes:
    """Return the single file part's bytes from a multipart form body.

    Args:
        body: The whole request body as the server received it.

    Returns:
        Everything between the part's header separator and the closing
        boundary -- the file content, and nothing else.
    """
    _, _, after_headers = body.partition(b'\r\n\r\n')
    payload, _, _ = after_headers.rpartition(b'\r\n--')
    return payload


async def _upload(
    server: RecordingHTTPServer,
    source: Path,
    *,
    attempts: int,
) -> None:
    """Upload ``source`` through the non-streaming filter method.

    Args:
        server: The loopback server to upload to.
        source: The file to send.
        attempts: How many times the breaker double runs the attempt.

    Returns:
        None.
    """
    async with aiohttp.ClientSession() as session:
        await make_http_filters_without_stream_uploads(
            session,
            server.url_for('/upload'),
            'POST',
            _RepeatingBreaker(attempts=attempts),
            redact_params=frozenset(),
            http_file_upload_config={
                'local_filepath': str(source),
                'file_key': 'attachment',
            },
        )


async def test_a_retried_upload_sends_the_whole_file_every_attempt(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """Attempt two carries the file's real byte length, not zero.

    This is the defect the conversion had to avoid rather than one it
    fixes incidentally. A single file handle -- or any one-shot body --
    opened outside ``failsafe.run`` is consumed by the first attempt, and
    every attempt after it uploads an empty part while the server answers
    200: a wrong-content upload that reports success. The assertion is
    against ``len(UPLOAD_BYTES)`` and not against "non-zero", because
    zero is the correct answer for an empty file and a "non-zero" guard
    would therefore be satisfied by a single stray byte.

    It closes the defect on one of the two upload paths only.
    ``make_http_filters_with_stream_file_upload``, reached from the same
    dispatcher on the ``file_upload_chunk_size`` config key, still hands
    a single ``file_upload(...)`` generator to ``failsafe.run``: it is a
    live instance of the same one-shot-body shape and still sends zero
    bytes on attempt two. That is pre-existing and untouched here; it is
    ticket **AGW-38 (High)** -- the sibling-path instance of registered
    finding **H9**, whose severity it takes -- owned by story **S15**,
    whose R14/H9 criterion is exactly a body built by a factory invoked
    per attempt.
    """
    source = tmp_path / 'attachment.bin'
    source.write_bytes(UPLOAD_BYTES)
    http_server.respond('/upload', body=b'stored')

    await _upload(http_server, source, attempts=2)

    received = [r for r in http_server.requests if r.path == '/upload']
    assert len(received) == 2
    for attempt in received:
        assert len(_uploaded_part(attempt.body)) == len(UPLOAD_BYTES)
        assert _uploaded_part(attempt.body) == UPLOAD_BYTES


async def test_an_empty_file_uploads_as_an_empty_part_every_attempt(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """Zero bytes is the right answer here, and stays right on retry."""
    source = tmp_path / 'empty.bin'
    source.write_bytes(b'')
    http_server.respond('/upload', body=b'stored')

    await _upload(http_server, source, attempts=2)

    received = [r for r in http_server.requests if r.path == '/upload']
    assert len(received) == 2
    for attempt in received:
        assert _uploaded_part(attempt.body) == b''


async def test_the_upload_part_keeps_the_name_and_type_it_used_to_carry(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The streaming body must cost the part neither name nor type.

    aiohttp derived the ``filename`` from the synchronous file object's
    ``.name``. An async generator has no name, so dropping the handle
    without naming the part would have silently turned a file field into
    a plain one -- a different request, for any server that looks for a
    filename.

    The part's own ``Content-Type`` is the half this test used to stop
    short of, and it is the half that broke: aiohttp's
    ``AsyncIterablePayload`` injects ``application/octet-stream`` when the
    caller passes no ``content_type``, which short-circuits the
    filename-based guess the file handle used to reach. A ``.json`` source
    is used rather than the ``.bin`` the other upload tests share
    precisely so the guess is *not* octet-stream and the assertion is
    capable of failing.
    """
    source = tmp_path / 'attachment.json'
    source.write_bytes(UPLOAD_BYTES)
    http_server.respond('/upload', body=b'stored')

    await _upload(http_server, source, attempts=1)

    sent = http_server.requests[-1]
    assert b'filename="attachment.json"' in sent.body
    assert b'name="attachment"' in sent.body
    assert b'Content-Type: application/json' in sent.body
    assert b'application/octet-stream' not in sent.body
    assert sent.headers['Content-Type'].startswith('multipart/form-data')


def _missing_file(tmp_path: Path) -> Path:
    """Return a path nothing was ever written to.

    Args:
        tmp_path: The test's private directory.

    Returns:
        A path that does not exist -- the caller's plain path typo.
    """
    return tmp_path / 'never-written.bin'


def _a_directory(tmp_path: Path) -> Path:
    """Return a directory where the caller meant to name a file.

    Args:
        tmp_path: The test's private directory.

    Returns:
        A path that exists and is a directory. ``stat`` succeeds on it, so
        only actually opening it tells the two apart.
    """
    target = tmp_path / 'a-directory'
    target.mkdir()
    return target


def _an_unreadable_file(tmp_path: Path) -> Path:
    """Return a real file whose mode denies this process every access.

    ``stat`` succeeds here too -- the mode bits are part of what it
    returns rather than something it enforces -- which makes this the
    second errno a presence check silently lets through.

    Args:
        tmp_path: The test's private directory.

    Returns:
        A path to an existing, mode-``000`` file.

    Raises:
        Skipped: Via :func:`pytest.skip` when the mode bits do not in fact
            deny this process -- root bypasses them, and a row that cannot
            fail must say so rather than pass.
    """
    target = tmp_path / 'unreadable.bin'
    target.write_bytes(UPLOAD_BYTES)
    target.chmod(0o000)
    try:
        with open(target, 'rb'):
            pass
    except PermissionError:
        return target
    pytest.skip('mode 000 does not deny this process (running as root?)')


@pytest.mark.parametrize(
    'make_local_filepath, expected',
    [
        pytest.param(_missing_file, FileNotFoundError, id='missing'),
        pytest.param(_a_directory, IsADirectoryError, id='directory'),
        pytest.param(_an_unreadable_file, PermissionError, id='unreadable'),
    ],
)
async def test_an_unopenable_upload_file_raises_rather_than_reporting_502(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    make_local_filepath: Callable[[Path], Path],
    expected: type[OSError],
) -> None:
    """A caller-side file problem is not a remote transport failure.

    Asserted at the entry point, because that is the only layer where the
    distinction is observable: ``request()`` is the one conversion point,
    and a test one layer down would pass while the contract was broken.

    Building the body inside the retried callable put the file open inside
    ``failsafe.run``, where an ``OSError`` is wrapped in
    ``RetriesExhausted`` and classified as a ``ConnectError`` -- an
    ``ok=False`` envelope with status 502 and code ``CONNECT``, telling
    the caller the remote endpoint refused a connection that was never
    dialled, with their absolute local path in the message. The
    zero-requests assertion is what pins that: nothing may reach the wire.

    Three rows, not one, because "the file cannot be opened" has three
    ordinary spellings and only the first of them is a missing file. A
    guard written as a presence check passes the ``missing`` row and
    reports the other two as that same 502, which is the defect verbatim.
    Each row asserts its *exact* errno type, so a guard that collapsed
    them into one class would fail rather than look green.
    """
    http_server.respond('/upload', body=b'stored')
    local_filepath = make_local_filepath(tmp_path)

    with pytest.raises(expected):
        await request(
            url=http_server.url_for('/upload'),
            protocol='HTTP',
            protocol_info={
                'request_type': 'POST',
                'http_file_upload_config': {
                    'local_filepath': str(local_filepath),
                    'file_key': 'attachment',
                },
            },
        )

    assert http_server.requests == []


async def test_a_large_upload_is_not_buffered_whole_in_memory(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file larger than one chunk arrives whole, in counted pieces.

    The received bytes alone cannot say this. A body that did
    ``yield await f.read()`` -- the whole file resident in memory, sent in
    one piece -- produces a byte-identical part, so an assertion on the
    part content is satisfied by exactly the defect R20 names. What
    distinguishes the two is *how many times the body yielded*, so that is
    what is counted.

    ``CHUNK_SIZE_CONSTANT * 2 + 7`` gives three yields: two whole chunks
    and a short remainder. Two would mean the tail was dropped, one means
    the file was read whole.
    """
    payload = b'L' * (CHUNK_SIZE_CONSTANT * 2 + 7)
    source = tmp_path / 'large.bin'
    source.write_bytes(payload)
    http_server.respond('/upload', body=b'stored')
    yield_counts: list[int] = []

    async def counting_file_upload(**kwargs: Any) -> AsyncIterator[bytes]:
        """Delegate to the real body, recording how often it yielded.

        Args:
            kwargs: Forwarded verbatim to the real ``file_upload``.

        Yields:
            Every chunk the real body produced, unmodified.
        """
        yielded = 0
        async for chunk in file_upload(**kwargs):
            yielded += 1
            yield chunk
        yield_counts.append(yielded)

    monkeypatch.setattr(
        'async_gateway.helpers.internal.request_helper.file_upload',
        counting_file_upload)

    await _upload(http_server, source, attempts=1)

    assert _uploaded_part(http_server.requests[-1].body) == payload
    assert yield_counts == [3]

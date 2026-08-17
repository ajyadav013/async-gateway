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

Step 13 adds R14's: the response-size cap on every read path, the owned
redirect loop with its per-hop scheme check (FI-16), and the second half
of H9 -- the *streaming* upload path (ticket AGW-38), whose one-shot body
was still replayed on retry after the non-streaming path was fixed. The
new capabilities the loopback fixture grew for them -- per-method specs, a
response sequence, a gated handler, a lying ``Content-Length``, a chunked
body -- are asserted here too, because a fixture that mis-reports what it
received invalidates every assertion built on it.
"""

import ast
import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any, Dict, Text
from urllib.parse import urlsplit

import aiohttp

from multidict import CIMultiDict

import pytest

from async_gateway.async_gateway import request
from async_gateway.helpers.internal.request_helper import (
    DEFAULT_DOWNLOAD_FILEPATH,
    HttpResult,
    REQUEST_START_KEY,
    after_redirect,
    file_upload,
    handle_multipart_response,
    hop_deadline,
    make_http_filters_with_stream_file_upload,
    make_http_filters_without_stream_uploads,
    make_http_request,
    measure_redirect,
    record_redirect,
    redirect_target,
    same_origin,
    without_credentials,
)
from async_gateway.utils.constants import (
    ALLOWED_SCHEMES,
    CHUNK_SIZE_CONSTANT,
    CREDENTIAL_HEADERS,
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
)
from async_gateway.utils.exceptions import (
    ConfigurationError,
    HttpStatusError,
    PathContainmentError,
    ResponseTooLargeError,
)
from async_gateway.utils.http_file_config import (
    download_file_from_url,
    guard_declared_length,
    iter_capped,
)
from async_gateway.utils.redaction import SENSITIVE_HEADERS

from tests.fixtures.http_server import RecordingHTTPServer, ResponseSpec

BOUNDARY = 'agwtestboundary'
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
#: ``BodyPartReader.chunk_size``. A part of three chunks plus a remainder is
#: the shape that exposes a reader stopping after one ``read_chunk()``.
CHUNK_SIZE = aiohttp.multipart.BodyPartReader.chunk_size

#: Long enough that no assertion below is ever decided by it, short enough
#: that a hang fails the suite rather than stalling it.
TEST_TIMEOUT = aiohttp.ClientTimeout(total=5)

#: The transport policy ``make_http_request`` now requires of every
#: caller. Held in one place so a test overrides the single knob it is
#: about and inherits the documented defaults for the rest.
POLICY: Dict[Text, Any] = {
    'timeout': TEST_TIMEOUT,
    'max_response_bytes': MAX_RESPONSE_BYTES,
    'allowed_schemes': ALLOWED_SCHEMES,
    'allow_redirects': True,
    'max_redirects': MAX_REDIRECTS,
}


async def _no_body() -> Dict[Text, Any]:
    """Return the body factory's answer for a request that carries none.

    ``make_http_request`` takes a factory rather than a body, so the
    bodyless case needs one too.

    Returns:
        An empty mapping of transport keyword arguments.
    """
    return {}


def accepted_bytes(error: ResponseTooLargeError) -> int:
    """Return how many bytes a mid-stream refusal says it had read.

    The download cap used to be asserted by measuring the file on disk.
    R22-AC5 removes a partial download, so there is no file left to
    measure -- and the refusal's own count is the better witness anyway:
    it reports what the *process* accepted, where a file size reports
    only what reached disk. A read that buffered a whole body in memory
    before writing a capped prefix of it would pass a size assertion and
    fails this one.

    Args:
        error: The refusal ``response_too_large`` built. Its wording is
            the one this parses, and the two are deliberately in the
            same package: a change to the message that this cannot read
            fails the tests rather than silently weakening them.

    Returns:
        The byte count named in the message.

    Raises:
        AssertionError: If the message carries no such count, which
            would otherwise make every caller's comparison vacuous.
    """
    found = re.search(r'abandoned after (\d+) bytes', str(error))
    assert found is not None, (
        f'the refusal names no byte count, so nothing can be asserted '
        f'about what was accepted: {error}')
    return int(found.group(1))


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


async def _fetch(url: str, **kwargs: Any) -> HttpResult:
    """Make one GET through the transport boundary under test.

    Args:
        url: The absolute URL to fetch.
        kwargs: Per-call configuration forwarded to ``make_http_request``,
            e.g. ``http_file_download_config``. Any key of :data:`POLICY`
            given here overrides that default.

    Returns:
        The ``HttpResult`` the boundary produced.
    """
    async with aiohttp.ClientSession(timeout=TEST_TIMEOUT) as session:
        return await make_http_request(
            session,
            url,
            _no_body,
            'GET',
            redact_params=frozenset(),
            **{**POLICY, **kwargs},
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


def _multipart_body_without_its_closing_boundary(part: bytes) -> bytes:
    """Assemble a multipart body whose final boundary never arrives.

    A length-declaring part with no closing ``--boundary--`` after it.
    The declared length is what makes it useful: ``BodyPartReader``
    stops the part on its own byte count, and the reader then finds the
    stream exhausted where the terminator should have been -- so
    ``at_eof()`` is already True at the top of the next iteration.

    Args:
        part: The body bytes of the single part.

    Returns:
        The truncated multipart body.
    """
    boundary = BOUNDARY.encode()
    return b''.join([
        b'--', boundary, b'\r\n',
        b'Content-Type: application/octet-stream\r\n',
        b'Content-Length: ', str(len(part)).encode(), b'\r\n\r\n',
        part, b'\r\n',
    ])


async def test_a_truncated_multipart_body_terminates_on_at_eof(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R28: the loop's *other* exit -- the ``while`` condition, not the break.

    ``test_a_reader_returning_none_terminates_the_loop_cleanly`` covers a
    well-formed body, where ``next()`` answers None before ``at_eof()``
    goes True and the ``break`` is what ends the loop. A body whose
    closing boundary never arrives ends the other way round: the part
    declares its own ``Content-Length``, so the reader finishes it and
    then finds the stream exhausted, and ``at_eof()`` is already True
    when the ``while`` is re-tested. ``next()`` is never called again.

    That ordering is the whole point of the guard. Verified against the
    real reader rather than assumed: calling ``next()`` on this body
    after the first part raises ``ValueError: Invalid boundary b'',
    expected b'--agwtestboundary'``. So a loop rewritten as
    ``while True`` with only the None check to end it would turn a
    truncated response -- a connection cut mid-body, which is an ordinary
    network event -- into an unhandled ``ValueError`` escaping
    ``request()`` as this library's own bug rather than as a completed
    read of everything that did arrive.

    Delete this and only the well-formed exit is exercised, so that
    rewrite ships green. The parts that *did* arrive are asserted written
    and returned, because refusing them would be the opposite failure:
    discarding a body the caller received in full up to the cut.
    """
    target = tmp_path / 'truncated.bin'
    http_server.respond(
        '/multipart',
        body=_multipart_body_without_its_closing_boundary(b'arrived'),
        headers=_multipart_headers(),
    )
    observed: list[Text] = []
    build_reader = aiohttp.MultipartReader.from_response

    def recording(response: aiohttp.ClientResponse) -> Any:
        """Wrap the real reader, noting which exit the loop takes.

        The two exits are indistinguishable from the outside: both end
        the loop and both return what arrived. Recording the calls is
        what tells them apart, and without it this row would pass
        against the ``next()``-returned-None exit the well-formed case
        already covers.

        Args:
            response: The response to read the multipart body from.

        Returns:
            The real reader, with its two loop-controlling methods
            recording before they delegate.
        """
        reader = build_reader(response)
        at_eof, next_part = reader.at_eof, reader.next

        def watched_at_eof() -> bool:
            """Note the answer, then give it.

            Returns:
                Whatever the real ``at_eof`` answers.
            """
            answer = at_eof()
            observed.append(f'at_eof={answer}')
            return answer

        async def watched_next() -> Any:
            """Note whether a part came back, then hand it on.

            Returns:
                Whatever the real ``next`` answers.
            """
            part = await next_part()
            observed.append(f'next={part is not None}')
            return part

        reader.at_eof, reader.next = watched_at_eof, watched_next
        return reader

    monkeypatch.setattr(
        aiohttp.MultipartReader, 'from_response', staticmethod(recording))

    async with aiohttp.ClientSession(timeout=TEST_TIMEOUT) as session:
        async with session.get(
            http_server.url_for('/multipart'),
        ) as response:
            text = await handle_multipart_response(
                response, {'download_filepath': str(target)})

    assert text == 'arrived'
    assert target.read_bytes() == b'arrived'
    assert observed == ['at_eof=False', 'next=True', 'at_eof=True'], (
        'the loop must end on the while condition, not on the break: a '
        'trailing next=False would mean this row is re-covering the '
        'well-formed exit instead of the truncated one')


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
        self.chunk_sizes: list[int] = []

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        """Yield the recorded chunks, once, recording the size asked for.

        A real ``StreamReader`` is drained by the first pass and yields
        nothing on a second, which is what makes the in-memory read after
        a streamed download ``b''`` rather than a second copy. The double
        has to do the same or it would certify a transport that read the
        body twice.

        Args:
            size: Requested chunk size. Recorded -- it is what the
                default-chunk-size assertion reads -- but not applied:
                the chunking is fixed so the *count* is the test's to
                state.

        Yields:
            Each recorded chunk in turn, on the first pass only.
        """
        self.chunk_sizes.append(size)
        chunks, self._chunks = self._chunks, []
        for chunk in chunks:
            yield chunk


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
                _no_body,
                'GET',
                redact_params=frozenset(),
                **POLICY,
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
) -> HttpResult:
    """Upload ``source`` through the non-streaming filter method.

    Args:
        server: The loopback server to upload to.
        source: The file to send.
        attempts: How many times the breaker double runs the attempt.

    Returns:
        The result of the final attempt.
    """
    async with aiohttp.ClientSession(timeout=TEST_TIMEOUT) as session:
        return await make_http_filters_without_stream_uploads(
            session,
            server.url_for('/upload'),
            'POST',
            _RepeatingBreaker(attempts=attempts),
            redact_params=frozenset(),
            **POLICY,
            http_file_upload_config={
                'local_filepath': str(source),
                'file_key': 'attachment',
            },
        )


async def _stream_upload(
    server: RecordingHTTPServer,
    source: Path,
    *,
    attempts: int,
    chunk_size: int = CHUNK_SIZE_CONSTANT,
) -> HttpResult:
    """Upload ``source`` through the *streaming* filter method.

    The sibling of :func:`_upload`, reached from the same dispatcher on the
    ``file_upload_chunk_size`` key. Its body is the raw file rather than a
    multipart form, so the recorded request body *is* the file's bytes and
    no part has to be extracted from it.

    Args:
        server: The loopback server to upload to.
        source: The file to send.
        attempts: How many times the breaker double runs the attempt.
        chunk_size: The caller's ``file_upload_chunk_size``.

    Returns:
        The result of the final attempt.
    """
    async with aiohttp.ClientSession(timeout=TEST_TIMEOUT) as session:
        return await make_http_filters_with_stream_file_upload(
            session,
            server.url_for('/upload'),
            'POST',
            _RepeatingBreaker(attempts=attempts),
            redact_params=frozenset(),
            **POLICY,
            http_file_upload_config={
                'local_filepath': str(source),
                'file_key': 'attachment',
                'file_upload_chunk_size': chunk_size,
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
    'extra_config',
    [
        pytest.param({}, id='multipart'),
        pytest.param(
            {'file_upload_chunk_size': 4096}, id='streamed'),
    ],
)
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
    extra_config: Dict[Text, Any],
) -> None:
    """A caller-side file problem is not a remote transport failure.

    Run against *both* dispatch branches. ``file_upload_chunk_size``
    is what routes a call to the streaming filter method, which had no
    guard at all until AGW-38: all three rows reached ``aiohttp`` inside
    ``failsafe.run`` and came back as a 502 ``CONNECT`` naming the
    caller's absolute local path.

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
                    **extra_config,
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


# --- R14/H9 (AGW-38): the *streaming* upload body is built per attempt ----


async def test_a_retried_streaming_upload_sends_the_file_on_attempt_two(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """One failure, then one success, and both carry the whole file.

    This is AGW-38, the sibling-path instance of H9. The non-streaming
    method was converted to a per-attempt body factory in Step 12; this
    one still created a single ``file_upload(...)`` generator *outside*
    ``failsafe.run`` and handed it to the breaker. An async generator is
    one-shot, so attempt one uploaded the file and attempt two uploaded
    **zero bytes** -- measured, not inferred -- while the server answered
    200 and the library reported success.

    The server answers 500 and then 200, so the retry is a real one on
    the wire rather than a bare repetition, and the final result proves
    the second attempt is the one that succeeded. The assertion is on
    ``UPLOAD_BYTES`` rather than on "non-empty": a stray byte would
    satisfy the weaker form, and zero is the *correct* answer for the
    empty-file case below.
    """
    source = tmp_path / 'streamed.bin'
    source.write_bytes(UPLOAD_BYTES)
    http_server.respond_in_sequence('/upload', [
        ResponseSpec(status=500, body=b'try again'),
        ResponseSpec(status=200, body=b'stored'),
    ])

    result = await _stream_upload(http_server, source, attempts=2)

    received = [r for r in http_server.requests if r.path == '/upload']
    assert result['status_code'] == 200
    assert len(received) == 2
    assert [len(r.body) for r in received] == [
        len(UPLOAD_BYTES), len(UPLOAD_BYTES)]
    assert [r.body for r in received] == [UPLOAD_BYTES, UPLOAD_BYTES]


async def test_an_empty_streaming_upload_stays_empty_on_every_attempt(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """Zero bytes is a legitimate upload, and must not read as the bug.

    The pair with the test above is the point: an assertion of
    "non-zero bytes on attempt two" would pass there and be *wrong*
    here, so both are asserted against the file's own real length.
    """
    source = tmp_path / 'empty.bin'
    source.write_bytes(b'')
    http_server.respond('/upload', body=b'stored')

    await _stream_upload(http_server, source, attempts=2)

    received = [r for r in http_server.requests if r.path == '/upload']
    assert len(received) == 2
    assert [r.body for r in received] == [b'', b'']
    assert all(r.body_complete for r in received)


async def test_the_streaming_upload_branch_is_the_one_dispatch_reaches(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """``file_upload_chunk_size`` routes to the streaming body, not a form.

    The dispatch branch that selects it was uncovered, which is the
    mechanical reason AGW-38 survived a story that fixed the same defect
    next door. Asserted on the wire: a raw body and a chunked framing,
    where the multipart method would have sent a ``multipart/form-data``
    content type and a boundary.
    """
    source = tmp_path / 'streamed.bin'
    source.write_bytes(UPLOAD_BYTES)
    http_server.respond('/upload', body=b'stored')

    await request(
        url=http_server.url_for('/upload'),
        protocol='HTTP',
        protocol_info={
            'request_type': 'POST',
            'http_file_upload_config': {
                'local_filepath': str(source),
                'file_key': 'attachment',
                'file_upload_chunk_size': 4096,
            },
        },
    )

    sent = http_server.requests[-1]
    assert sent.body == UPLOAD_BYTES
    assert sent.headers.get('Transfer-Encoding') == 'chunked'
    assert 'multipart/form-data' not in sent.headers.get('Content-Type', '')


# --- R14/H9: a redirected upload re-sends the file, not zero bytes --------


def _redirect_then_store(server: RecordingHTTPServer) -> None:
    """Register a 307 on ``/upload`` pointing at a 200 on ``/moved``.

    307 is the status that repeats both the verb and the body, so the
    second hop is a second *upload* rather than a GET -- which is what
    makes a consumed one-shot body observable as an empty second request.

    Args:
        server: The loopback server to register the pair on.

    Returns:
        None.
    """
    server.respond(
        '/upload', status=307, headers={'Location': '/moved'})
    server.respond('/moved', body=b'stored')


async def test_a_redirected_multipart_upload_re_sends_the_whole_file(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """Both hops carry the file, because both build their own body.

    Owning the redirect loop deleted ``aiohttp``'s own refusal --
    ``ClientPayloadError: Cannot follow redirect with a consumed request
    body`` -- and re-sent the previous hop's ``FormData`` verbatim. A
    ``FormData`` yields its parts once, so the second hop carried the
    boundary and no file at all while the server answered 200: a
    truncated upload reported as a success, which is the exact defect
    AGW-38 exists to eliminate, on the path this story created.
    """
    source = tmp_path / 'attachment.bin'
    source.write_bytes(UPLOAD_BYTES)
    _redirect_then_store(http_server)

    await _upload(http_server, source, attempts=1)

    assert [r.path for r in http_server.requests] == ['/upload', '/moved']
    for hop in http_server.requests:
        assert _uploaded_part(hop.body) == UPLOAD_BYTES


async def test_a_redirected_streaming_upload_re_sends_the_whole_file(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The sibling path, whose one-shot body is the raw async generator.

    Measured on the wire rather than inferred: the second hop's recorded
    body was zero bytes long, the same signature the retry defect had.
    """
    source = tmp_path / 'streamed.bin'
    source.write_bytes(UPLOAD_BYTES)
    _redirect_then_store(http_server)

    await _stream_upload(http_server, source, attempts=1)

    assert [r.path for r in http_server.requests] == ['/upload', '/moved']
    assert [r.body for r in http_server.requests] == [
        UPLOAD_BYTES, UPLOAD_BYTES]


async def test_a_redirected_form_post_re_encodes_its_form(
    http_server: RecordingHTTPServer,
) -> None:
    """The third path also carries a ``FormData``, and now also rebuilds it.

    Stated precisely, because a test name is a claim: this one was
    *measured green before the fix* -- a urlencoded ``FormData`` re-encodes
    on a second send where a multipart one does not, so this path was
    never the leak. It is pinned because the same factory now feeds it:
    the body object that reaches the wire is the one the no-file path
    builds, and nothing else asserts that a second hop still gets a
    complete one.
    """
    _redirect_then_store(http_server)

    await request(
        url=http_server.url_for('/upload'),
        data={'field': 'value'},
        protocol='HTTP',
        protocol_info={
            'request_type': 'POST',
            'headers': {
                'Content-Type': 'application/x-www-form-urlencoded'},
        },
    )

    assert [r.path for r in http_server.requests] == ['/upload', '/moved']
    assert [r.body for r in http_server.requests] == [
        b'field=value', b'field=value']


# --- R14: the chunk-size default ------------------------------------------


def test_the_default_transfer_chunk_size_is_64_kib() -> None:
    """1024 cost one await cycle per kilobyte transferred.

    Pinned as a number rather than left implicit, because "conventional"
    is the criterion and a later edit that quietly restored 1024 would
    otherwise only show up as a performance regression nobody measures.
    """
    assert CHUNK_SIZE_CONSTANT == 65536


async def test_a_download_with_no_chunk_size_asks_for_the_default(
    tmp_path: Path,
) -> None:
    """The documented default is what an unset chunk size resolves to.

    Read off the size the transport actually asked its stream for, which
    is the only place the default is observable -- the assembled file is
    identical whatever chunking produced it.
    """
    target = tmp_path / 'defaulted.bin'
    session = _ChunkedSession([CHUNK_BYTES])

    await make_http_request(
        session,
        'http://download.test/file',
        _no_body,
        'GET',
        redact_params=frozenset(),
        **POLICY,
        http_file_download_config={'download_filepath': str(target)},
    )

    assert session._response.content.chunk_sizes[0] == CHUNK_SIZE_CONSTANT


# --- R14: response reads are capped ---------------------------------------


class _CountingContent:
    """A stream double recording how many chunks it was actually asked for.

    The cap's claim is that it stops *reading*, not merely that it
    reports afterwards, and only the count of chunks pulled off the
    stream can say so deterministically.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        """Record the chunks this stream can produce.

        Args:
            chunks: What the stream would yield if drained.
        """
        self._chunks = chunks
        self.yielded = 0

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        """Yield the recorded chunks, counting each one produced.

        Args:
            size: Requested chunk size, ignored -- the chunking is fixed
                so the count is the test's to state.

        Yields:
            Each recorded chunk in turn.
        """
        for chunk in self._chunks:
            self.yielded += 1
            yield chunk


async def test_iter_capped_stops_on_the_chunk_that_crosses_it() -> None:
    """A cap that read the whole body and complained after would be a lie.

    Three chunks of ten bytes take the running total to 30 against a cap
    of 25, so the third is where it must stop -- and the remaining 97
    chunks must never be pulled off the stream at all.
    """
    content = _CountingContent([b'a' * 10] * 100)

    with pytest.raises(ResponseTooLargeError):
        async for _ in iter_capped(
            content, chunk_size=10, max_response_bytes=25,
        ):
            pass

    assert content.yielded == 3


async def test_a_body_exactly_at_the_cap_is_not_refused() -> None:
    """The cap is a ceiling, not a threshold one byte below itself."""
    content = _CountingContent([b'a' * 10] * 3)

    read = [
        chunk
        async for chunk in iter_capped(
            content, chunk_size=10, max_response_bytes=30)
    ]

    assert b''.join(read) == b'a' * 30


async def test_a_declared_length_over_the_cap_is_refused_before_the_read(
    http_server: RecordingHTTPServer,
) -> None:
    """A ``Content-Length`` that says "too big" costs one header parse.

    The server declares five megabytes and sends sixteen bytes, which is
    what makes this test capable of failing: with the pre-read guard the
    call is refused on the header, and without it the sixteen bytes are
    read happily and the request succeeds -- or, since the declared body
    never arrives, hangs until the half-second deadline and reports
    ``TIMEOUT``. Neither is ``RESPONSE_TOO_LARGE``.
    """
    http_server.respond(
        '/declared', body=b'sixteen bytes!!!', content_length=5_000_000)

    with pytest.raises(ResponseTooLargeError) as caught:
        await _fetch(
            http_server.url_for('/declared'),
            max_response_bytes=1024,
            timeout=aiohttp.ClientTimeout(total=0.5),
        )

    assert 'Content-Length 5000000' in str(caught.value)
    assert 'the body was not read' in str(caught.value)


async def test_a_chunked_body_over_the_cap_is_refused_mid_stream(
    http_server: RecordingHTTPServer,
) -> None:
    """No declared length to reject, so the refusal happens while reading.

    A chunked response carries no ``Content-Length`` at all -- asserted
    here rather than assumed, since a guard that only ever fired on the
    header would pass the test above and leave this body unbounded.
    """
    http_server.respond('/chunked', chunks=[b'x' * 4096] * 64)

    with pytest.raises(ResponseTooLargeError) as caught:
        await _fetch(
            http_server.url_for('/chunked'), max_response_bytes=2048)

    assert 'the read was abandoned after' in str(caught.value)


async def test_a_chunked_download_never_writes_more_than_the_cap(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The streamed-to-disk path is bounded too, and it leaves nothing.

    Two megabytes are offered against an eight-kilobyte cap, and two
    separate claims are made about the outcome.

    The **cap held**: the refusal names how many bytes had been read
    when it fired, and that is what the process actually accepted. The
    count is read off the error rather than off the file's size because
    R22-AC5 now *removes* the partial file, so the ``st_size`` this used
    to assert on raises ``FileNotFoundError`` before it can be compared.
    Taking it from the refusal is the stricter reading anyway: a file
    size measures what reached *disk*, so a read that buffered the whole
    body in memory and wrote eight kilobytes of it would have satisfied
    the old assertion while accepting two megabytes. Verified by
    mutation -- ``iter_capped`` rewritten to accumulate the whole body
    and raise at the end fails this assertion and passes the old one.

    And **nothing was orphaned**: the partial file a failed download
    used to leave behind is gone (M19). Neither claim implies the other.
    """
    target = tmp_path / 'capped.bin'
    http_server.respond('/chunked', chunks=[b'y' * 4096] * 512)

    with pytest.raises(ResponseTooLargeError) as caught:
        await _fetch(
            http_server.url_for('/chunked'),
            max_response_bytes=8192,
            http_file_download_config={
                'download_filepath': str(target),
                'file_download_chunk_size': 1024,
            },
        )

    assert accepted_bytes(caught.value) <= 8192 + 1024
    assert not target.exists()


async def test_a_multipart_body_over_the_cap_is_refused(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The multipart reader accumulates every part in memory as well.

    It is the third read path, and leaving it out would have made
    ``max_response_bytes`` a cap on two of the three ways this module
    reads a body.

    Served **chunked**, and that is what makes the test capable of
    failing. An ordinary multipart response carries an honest
    ``Content-Length``, so the pre-read guard refuses it before the
    multipart reader is ever entered -- and the test passed with the
    reader's own cap deleted. With no declared length there is nothing
    but the reader's counter to catch it.
    """
    target = tmp_path / 'parts.bin'
    body = _multipart_body(b'z' * 4096, b'z' * 4096)
    http_server.respond(
        '/multipart',
        chunks=[body[at:at + 512] for at in range(0, len(body), 512)],
        headers=_multipart_headers(),
    )

    with pytest.raises(ResponseTooLargeError):
        await _fetch(
            http_server.url_for('/multipart'),
            max_response_bytes=1024,
            http_file_download_config={'download_filepath': str(target)},
        )


async def test_a_body_under_the_default_cap_is_read_whole(
    http_server: RecordingHTTPServer,
) -> None:
    """The cap does not truncate an ordinary response.

    The pair to every refusal above: a guard that rejected everything
    would satisfy all of them and break the library.
    """
    payload = b'p' * (CHUNK_SIZE_CONSTANT * 2 + 7)
    http_server.respond('/ordinary', body=payload)

    result = await _fetch(http_server.url_for('/ordinary'))

    assert result['text'] == payload.decode()
    assert result['status_code'] == 200


# --- R14/R21-AC6: the redirect target is resolved and scheme-checked ------


@pytest.mark.parametrize(
    'location, expected',
    [
        pytest.param(
            'https://host/next', 'https://host/next', id='absolute'),
        pytest.param('/next', 'https://host/next', id='root-relative'),
        pytest.param('sibling', 'https://host/a/sibling', id='relative'),
        pytest.param(
            '//other/next', 'https://other/next', id='scheme-relative'),
    ],
)
def test_a_location_is_resolved_against_the_url_that_sent_it(
    location: Text,
    expected: Text,
) -> None:
    """Only the resolved target can be scheme-checked at all.

    A relative ``Location`` has no scheme of its own, so a check applied
    to the header verbatim would wave every one of them through without
    looking at anything.
    """
    assert redirect_target(
        'https://host/a/b',
        location,
        allowed_schemes=ALLOWED_SCHEMES,
        hop=1,
        redact_params=frozenset(),
    ) == expected


@pytest.mark.parametrize(
    'location, scheme',
    [
        pytest.param('ftp://evil/x', 'ftp', id='ftp'),
        pytest.param('file:///etc/passwd', 'file', id='file'),
        pytest.param('javascript:alert(1)', 'javascript', id='javascript'),
    ],
)
def test_a_redirect_to_a_scheme_outside_the_allowlist_is_refused(
    location: Text,
    scheme: Text,
) -> None:
    """The refusal names the scheme and the hop, and it is a CONFIG error.

    Named, because a chain of ten hops that ends in a rejection is
    undiagnosable otherwise.
    """
    with pytest.raises(ConfigurationError) as caught:
        redirect_target(
            'https://host/a',
            location,
            allowed_schemes=ALLOWED_SCHEMES,
            hop=3,
            redact_params=frozenset(),
        )

    assert caught.value.code == 'CONFIG'
    assert f'{scheme!r}' in str(caught.value)
    assert 'hop 3' in str(caught.value)


def test_the_rejection_message_carries_no_credential() -> None:
    """The message names the URL the hop came from, so it is redacted."""
    with pytest.raises(ConfigurationError) as caught:
        redirect_target(
            'https://host/a?api_key=supersecret',
            'ftp://evil/x',
            allowed_schemes=ALLOWED_SCHEMES,
            hop=1,
            redact_params=frozenset(),
        )

    assert 'supersecret' not in str(caught.value)


def test_a_caller_widened_allowlist_admits_what_it_names() -> None:
    """The check reads the allowlist it was handed, not a hard-coded set."""
    assert redirect_target(
        'https://host/a',
        'ftp://elsewhere/x',
        allowed_schemes=frozenset({'https', 'ftp'}),
        hop=1,
        redact_params=frozenset(),
    ) == 'ftp://elsewhere/x'


@pytest.mark.parametrize(
    'left, right, expected',
    [
        pytest.param(
            'https://h/a', 'https://h/b', True, id='same'),
        pytest.param(
            'https://h/a', 'https://other/b', False, id='other-host'),
        pytest.param(
            'https://h/a', 'http://h/b', False, id='other-scheme'),
        pytest.param(
            'https://h/a', 'https://h:8443/b', False, id='other-port'),
    ],
)
def test_same_origin_compares_scheme_host_and_port(
    left: Text,
    right: Text,
    expected: bool,
) -> None:
    """All three parts, because any one of them changing changes the peer."""
    assert same_origin(left, right) is expected


def test_a_cross_origin_hop_drops_every_credential_header() -> None:
    """A hop to another host must not carry the caller's credentials.

    ``aiohttp`` stripped these while it owned the loop. Owning the loop
    without reproducing it would forward an ``Authorization`` header to
    whatever host a hostile endpoint named -- a credential leak
    introduced by the fix for a different one.
    """
    stripped = without_credentials({
        'Authorization': 'Bearer secret',
        'Cookie': 'session=secret',
        'Proxy-Authorization': 'Basic secret',
        'Accept': 'application/json',
    })

    assert stripped == {'Accept': 'application/json'}


def test_credential_stripping_is_case_insensitive() -> None:
    """Header names are case-insensitive, and a leak must not hinge on it."""
    assert without_credentials({'AUTHORIZATION': 'Bearer secret'}) == {}


def test_anything_the_envelope_redacts_a_cross_origin_hop_strips() -> None:
    """The invariant, not the header names -- the drift *was* the leak.

    Two lists answered the same question for two surfaces:
    ``SENSITIVE_HEADERS`` decided what the envelope masks,
    ``CREDENTIAL_HEADERS`` decided what a cross-origin hop strips. Kept
    as independent literals they diverged, and the divergence was H1 --
    ``x-api-key`` had been in the redaction set all along, so the library
    returned ``X-Api-Key: ***redacted***`` to the caller while forwarding
    ``APIKEYSECRET`` to whatever host a hostile ``Location`` named.
    Redacting a value asserts it is secret; forwarding it denies that.

    This asserts the *relation* rather than today's membership, so a
    header added to either set tomorrow is covered without anyone
    remembering this test exists. A test naming the five headers would
    have passed throughout the window in which H1 was exploitable.
    """
    sent = {name: f'{name}-secret' for name in SENSITIVE_HEADERS}

    assert without_credentials(sent) == {}
    assert SENSITIVE_HEADERS <= CREDENTIAL_HEADERS


def test_a_cross_origin_hop_strips_the_common_bearer_token_headers(
) -> None:
    """The headers H1 measured leaking, named so the fix cannot regress.

    The invariant above cannot cover these on its own: ``api-key``,
    ``x-auth-token``, ``x-amz-security-token`` and ``x-csrf-token`` are
    not in the redaction set, so only naming them proves they are
    stripped. Each is a bearer token in the plain sense -- holding it is
    enough to act as the caller.
    """
    sent = {
        'X-Api-Key': 'APIKEYSECRET',
        'X-Amz-Security-Token': 'AWSSECRET',
        'X-Auth-Token': 'AUTHTOKSECRET',
        'Api-Key': 'APIKEY2SECRET',
        'X-Csrf-Token': 'CSRFSECRET',
        'Accept': 'application/json',
    }

    assert without_credentials(sent) == {'Accept': 'application/json'}


#: A body that survives being sent twice, and two that do not. ``b'x'``
#: replays, so a table built only from it proves the repeat rule for the
#: one body shape an upload never sends -- which is how a 307 re-sending a
#: *consumed* form went unnoticed under a green row named ``307-repeats``.
#: The ``FormData`` and the async generator are the real one-shot shapes
#: ``build_upload_form`` and ``file_upload`` produce.
REPLAYABLE_BODY: Dict[Text, Any] = {'data': b'x'}
ONE_SHOT_FORM: Dict[Text, Any] = {'data': aiohttp.FormData()}


@pytest.mark.parametrize(
    'status, verb, body, expected_verb, expected_body',
    [
        pytest.param(
            301, 'POST', REPLAYABLE_BODY, 'GET', {}, id='301-post'),
        pytest.param(
            302, 'POST', REPLAYABLE_BODY, 'GET', {}, id='302-post'),
        pytest.param(
            303, 'POST', REPLAYABLE_BODY, 'GET', {}, id='303-post'),
        pytest.param(
            303, 'PUT', REPLAYABLE_BODY, 'GET', {}, id='303-put'),
        pytest.param(
            302, 'GET', REPLAYABLE_BODY, 'GET', REPLAYABLE_BODY,
            id='302-get-keeps-body'),
        pytest.param(
            307, 'POST', REPLAYABLE_BODY, 'POST', REPLAYABLE_BODY,
            id='307-repeats'),
        pytest.param(
            308, 'POST', REPLAYABLE_BODY, 'POST', REPLAYABLE_BODY,
            id='308-repeats'),
        pytest.param(
            307, 'POST', ONE_SHOT_FORM, 'POST', ONE_SHOT_FORM,
            id='307-repeats-a-form'),
        pytest.param(
            303, 'POST', ONE_SHOT_FORM, 'GET', {}, id='303-drops-a-form'),
        pytest.param(
            303, 'HEAD', REPLAYABLE_BODY, 'HEAD', REPLAYABLE_BODY,
            id='303-head-keeps-its-verb'),
        pytest.param(
            302, ' post ', REPLAYABLE_BODY, 'GET', {}, id='302-untidy-post'),
    ],
)
def test_the_next_hops_verb_and_body_match_what_aiohttp_did(
    status: int,
    verb: Text,
    body: Dict[Text, Any],
    expected_verb: Text,
    expected_body: Dict[Text, Any],
) -> None:
    """Owning the loop must preserve the semantics, not invent new ones.

    Taking the loop off ``aiohttp`` is a means to the per-hop scheme
    check; a 303 that kept re-POSTing the body, or a 307 that dropped it,
    would be a behaviour change nothing asked for.

    The input is its own column. It used to be derived from the expected
    *output* -- ``dict(expected_body) or {'data': b'x'}`` -- so every
    to-GET row read as though it stated an input and was in fact handed
    ``{'data': b'x'}`` by the ``or``, and no row could state an input the
    rule was meant to transform away.
    """
    assert after_redirect(status, verb, body) == (
        expected_verb, expected_body)


# --- AGW-15 r4: the redirect event's base instant, at the unit level ------


async def test_a_collector_is_measured_against_its_own_start_instant(
) -> None:
    """Each tracer's own ``on_request_start``, not one instant for all.

    Two tracers may legitimately disagree about when the current request
    began -- one attached to a caller's long-lived session, one to this
    call -- and this library does not get to pick a winner. Measured
    against a shared instant, the second collector below would be off by
    the whole second the two starts differ by.
    """
    now = asyncio.get_running_loop().time()
    recent: Dict[Text, Any] = {REQUEST_START_KEY: now - 0.25}
    older: Dict[Text, Any] = {REQUEST_START_KEY: now - 1.25}

    measured = measure_redirect([recent, older])

    assert [collector for collector, _ in measured] == [recent, older]
    assert measured[0][1] == pytest.approx(0.25, abs=0.05)
    assert measured[1][1] == pytest.approx(1.25, abs=0.05)


@pytest.mark.parametrize(
    'collector, expected_key',
    [
        pytest.param({}, False, id='no-start-key-at-all'),
        pytest.param(
            {REQUEST_START_KEY: None}, False, id='start-key-holding-none'),
        pytest.param(
            {REQUEST_START_KEY: 'soon'}, False, id='start-key-not-a-number'),
    ],
)
async def test_an_unmeasurable_hop_is_flagged_without_a_fabricated_timing(
    collector: Dict[Text, Any],
    expected_key: bool,
) -> None:
    """A missing base instant withholds the timing, it does not invent 0.

    The hop was provably followed, so ``is_redirect`` is True either way
    -- that is what the flag means. But an ``on_request_redirect`` of 0.0
    written from no base at all reads as "the redirect was instantaneous",
    which is a fabricated measurement, and the key's *absence* is already
    the vocabulary this tracer uses for "did not happen"
    (``test_a_call_that_never_redirects_reports_no_redirect``).
    """
    record_redirect(measure_redirect([collector]))

    assert collector['is_redirect'] is True
    assert ('on_request_redirect' in collector) is expected_key


async def test_no_hop_followed_writes_nothing_at_all() -> None:
    """The refused-hop path: None means the loop never reached a hop.

    ``redirect_target`` raises before the measurement is taken, so the
    ``finally`` reads the None it started with. Writing ``is_redirect``
    here would make the flag mean "a redirect was seen" rather than
    "one was followed", which is not what it meant under ``aiohttp``.
    """
    collector: Dict[Text, Any] = {'is_redirect': False}

    record_redirect(None)

    assert collector == {'is_redirect': False}


async def test_measuring_with_no_tracers_attached_is_not_an_error() -> None:
    """Tracing off is the ordinary case, and it must cost nothing."""
    assert measure_redirect([]) == []
    record_redirect([])


# --- R14: the chain's deadline is divided among its hops, not reissued ----

#: A URL carrying a credential, so the one diagnostic ``hop_deadline``
#: emits is asserted not to leak it.
DEADLINE_URL = 'https://host/p?api_key=supersecret'


def test_a_timeout_with_no_total_is_handed_to_the_hop_unchanged() -> None:
    """With no total there is no chain budget, so there is nothing to divide.

    Returned as-is rather than rebuilt, so a caller who configured only
    ``sock_read`` keeps exactly the deadline they configured.
    """
    timeout = aiohttp.ClientTimeout(sock_read=3)

    got = hop_deadline(
        timeout, None, url=DEADLINE_URL, redact_params=frozenset(), hop=0)

    assert got is timeout


def test_a_hop_gets_what_the_chain_has_left_not_the_whole_budget() -> None:
    """Three of the ten seconds remain, so the hop is allowed three.

    Handing it the full ten is the defect: with ``max_redirects`` at its
    default a chain of slow hops would then cost the caller eleven times
    the deadline they set. The per-connection fields are not the chain's
    to spend and carry through untouched.
    """
    timeout = aiohttp.ClientTimeout(total=10, connect=2, sock_read=4)

    got = hop_deadline(
        timeout,
        time.monotonic() + 3,
        url=DEADLINE_URL,
        redact_params=frozenset(),
        hop=1)

    assert 2.5 < got.total <= 3
    assert (got.connect, got.sock_read) == (2, 4)


def test_a_chain_out_of_time_is_refused_before_the_hop_is_issued() -> None:
    """The deadline is spent, so the next hop is never dialled at all.

    ``asyncio.TimeoutError`` rather than a new exception type: it is what
    ``aiohttp`` raises for its own total, so it is already classified as
    ``TIMEOUT``/504 by the time it reaches the envelope.
    """
    timeout = aiohttp.ClientTimeout(total=10)

    with pytest.raises(asyncio.TimeoutError) as caught:
        hop_deadline(
            timeout,
            time.monotonic() - 1,
            url=DEADLINE_URL,
            redact_params=frozenset(),
            hop=3)

    assert 'supersecret' not in str(caught.value)
    assert '3 redirect hops' in str(caught.value)


async def test_a_308_repeats_a_one_shot_stream_body() -> None:
    """The async generator ``file_upload`` produces survives a repeat hop.

    Its own test rather than a table row, because an async generator is a
    live object: built at module scope for a parametrize table it is held
    -- unstarted, but held -- for the whole session, and closing it there
    is impossible from synchronous code. Built and closed here, it is
    neither.
    """
    body: Dict[Text, Any] = {'data': file_upload(file_name=__file__)}
    try:
        assert after_redirect(308, 'POST', body) == ('POST', body)
    finally:
        await body['data'].aclose()


def test_query_parameters_do_not_survive_a_hop() -> None:
    """The ``Location`` carries its own query string; ours would duplicate."""
    assert after_redirect(307, 'GET', {'params': [('a', '1')]}) == (
        'GET', {})


# --- The loopback fixture's own new capabilities --------------------------


async def _wait_for_requests(
    server: RecordingHTTPServer,
    count: int,
    *,
    timeout: float = 2.0,
) -> None:
    """Wait until ``server`` has recorded ``count`` requests.

    Polls the observable condition rather than sleeping for a guessed
    interval: the recording is written by the server's own task, so a
    fixed delay would be either flaky or slow and this is neither.

    Args:
        server: The loopback server to watch.
        count: How many recorded requests to wait for.
        timeout: Seconds to wait before failing, so a request that never
            arrives fails the test instead of hanging the suite.

    Returns:
        None.

    Raises:
        TimeoutError: If the count is not reached within ``timeout``.
    """
    async def poll() -> None:
        """Re-read the live recording until it is long enough."""
        while len(server.requests) < count:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout)


async def test_the_server_can_answer_one_path_differently_per_method(
    http_server: RecordingHTTPServer,
) -> None:
    """A path is not one response; a retry test needs POST to differ."""
    http_server.respond('/thing', method='GET', body=b'read')
    http_server.respond('/thing', method='POST', status=201, body=b'made')

    got = await _fetch(http_server.url_for('/thing'))
    async with aiohttp.ClientSession(timeout=TEST_TIMEOUT) as session:
        made = await make_http_request(
            session, http_server.url_for('/thing'), _no_body, 'POST',
            redact_params=frozenset(), **POLICY)

    assert (got['status_code'], got['text']) == (200, 'read')
    assert (made['status_code'], made['text']) == (201, 'made')


async def test_a_registered_sequence_is_answered_in_order_then_repeats(
    http_server: RecordingHTTPServer,
) -> None:
    """The last spec repeats rather than falling off into a surprise 404."""
    http_server.respond_in_sequence('/seq', [
        ResponseSpec(status=500, body=b'one'),
        ResponseSpec(status=200, body=b'two'),
    ])

    statuses = [
        (await _fetch(http_server.url_for('/seq')))['status_code']
        for _ in range(3)
    ]

    assert statuses == [500, 200, 200]


def test_a_sequence_registration_refuses_to_register_nothing(
    http_server: RecordingHTTPServer,
) -> None:
    """An empty sequence would silently register no response at all."""
    with pytest.raises(ValueError):
        http_server.respond_in_sequence('/seq', [])


async def test_the_server_can_emit_a_repeated_header(
    http_server: RecordingHTTPServer,
) -> None:
    """Two ``Set-Cookie`` lines is the ordinary case a mapping cannot hold."""
    http_server.respond('/cookies', headers=[
        ('Set-Cookie', 'a=1; Path=/'),
        ('Set-Cookie', 'b=2; Path=/'),
    ])

    result = await _fetch(http_server.url_for('/cookies'))

    assert result['cookies'] == {'a': '1', 'b': '2'}


async def test_the_recorded_path_and_query_are_the_bytes_as_sent(
    http_server: RecordingHTTPServer,
) -> None:
    """Wire-exact means still percent-encoded, and the docstring says so.

    The recording used to hand back the *decoded* path under a module
    docstring promising the wire, so a test asserting on an encoded path
    could not be written -- and one asserting on a decoded path read as
    if it had been.
    """
    http_server.respond('/a b', body=b'ok')

    await _fetch(f'{http_server.url_for("/a%20b")}?q=one%20two')

    recorded = http_server.requests[-1]
    assert recorded.path == '/a%20b'
    assert recorded.raw_query == 'q=one%20two'
    assert recorded.query['q'] == 'one two'


async def test_an_aborted_upload_is_recorded_rather_than_dropped(
    http_server: RecordingHTTPServer,
) -> None:
    """A request that never finished arriving is the point of a recording.

    The body read happened inside the ``RecordedRequest(...)`` call, so a
    client that disconnected mid-upload produced no record at all --
    silent, in a fixture whose stated guarantee is that a request the code
    under test should not have sent is visible.

    Driven over a raw socket rather than through ``aiohttp``'s client: an
    abandoned client-side payload resets the connection before the server
    has parsed anything, so it never reaches the recording at all and the
    test would pass against the defect. Writing the headers and one chunk
    and *then* closing puts the bytes on the wire in order, so the server
    provably enters the handler before it sees the end.
    """
    http_server.respond('/abort', method='POST', body=b'ok')
    parts = urlsplit(http_server.url_for('/abort'))

    _, writer = await asyncio.open_connection(parts.hostname, parts.port)
    writer.write(
        b'POST /abort HTTP/1.1\r\nHost: localhost\r\n'
        b'Transfer-Encoding: chunked\r\n\r\n'
        b'5\r\nhello\r\n')
    await writer.drain()
    writer.close()

    await _wait_for_requests(http_server, 1)

    recorded = http_server.requests[-1]
    assert recorded.path == '/abort'
    assert recorded.body == b'hello'
    assert recorded.body_complete is False


# --- R14: the cap helpers' own edges, and the second read path ------------


def test_a_content_length_that_is_not_a_number_is_no_size_failure(
) -> None:
    """A malformed declared length is the remote side's framing error.

    Reporting it as ``RESPONSE_TOO_LARGE`` would name the wrong failure;
    the transport fails on it in its own way, and the mid-stream cap
    still bounds whatever arrives.
    """
    guard_declared_length({'Content-Length': 'not a number'}, 10)


def test_a_content_length_under_the_cap_is_allowed_through() -> None:
    """The guard refuses an over-sized declaration and nothing else."""
    guard_declared_length({'content-length': '9'}, 10)


def test_a_conflicting_pair_of_declared_lengths_is_judged_on_the_largest(
) -> None:
    """A repeated header must not let the small line speak for the big one.

    The guard read the *first* ``Content-Length`` and returned. Two lines
    is a framing error the remote side committed and the shape a
    smuggling attempt takes, and deciding it on arrival order let a
    declaration well over the cap through behind a small one -- past the
    one check whose entire purpose is to refuse before a byte is read.
    The fixture can emit a repeated header, so this is the pair as a real
    response would carry it.
    """
    with pytest.raises(ResponseTooLargeError) as caught:
        guard_declared_length(
            CIMultiDict([('Content-Length', '9'),
                         ('Content-Length', '99')]),
            10)

    assert '99' in str(caught.value)


def test_an_unparseable_declared_length_no_longer_hides_a_later_one(
) -> None:
    """A malformed first line stopped the guard reading the honest second.

    Ignoring what cannot be parsed is right; returning on it is not, and
    the two were the same statement.
    """
    with pytest.raises(ResponseTooLargeError):
        guard_declared_length(
            CIMultiDict([('Content-Length', 'not a number'),
                         ('Content-Length', '99')]),
            10)


@pytest.mark.parametrize(
    'location',
    [
        pytest.param('http://[::1', id='malformed-ipv6'),
        pytest.param('http://host:99999/next', id='port-out-of-range'),
        pytest.param('http://host:abc/next', id='port-not-a-number'),
    ],
)
def test_a_location_that_is_not_parseable_is_a_configuration_error(
    location: Text,
) -> None:
    """An unparseable ``Location`` is a hostile input, not a crash.

    ``urljoin`` raises ``ValueError`` on a malformed IPv6 authority, and
    letting that escape would leave a redirect target deciding whether
    this library raises a typed error or a bare ``ValueError``.

    The two port cases are the same class arriving *later*: ``urlsplit``
    parses the authority lazily, so they split without complaint and
    raised only once something read ``.port`` -- which was
    :func:`same_origin`, outside this guard, so they escaped ``request()``
    as a bare ``ValueError`` where the IPv6 case did not.
    """
    with pytest.raises(ConfigurationError) as caught:
        redirect_target(
            'https://host/a',
            location,
            allowed_schemes=ALLOWED_SCHEMES,
            hop=1,
            redact_params=frozenset(),
        )

    assert caught.value.code == 'CONFIG'
    assert 'not parseable' in str(caught.value)


@pytest.mark.parametrize(
    'headers',
    [
        pytest.param(None, id='none'),
        pytest.param({}, id='empty'),
    ],
)
def test_stripping_credentials_from_no_headers_is_not_an_error(
    headers: Any,
) -> None:
    """A call with no headers at all still crosses origins."""
    assert without_credentials(headers) == headers


async def test_a_url_download_streams_to_disk_within_the_cap(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The other HTTP read path R14 names, and it is bounded too.

    It used to read the whole body into memory with
    ``response.content.read()`` and only then write it, so the file had
    to fit in memory alongside itself and the endpoint chose how much.
    """
    target = tmp_path / 'downloaded.bin'
    payload = b'w' * 10000
    http_server.respond('/file', body=payload)

    await download_file_from_url(
        http_server.url_for('/file'), str(target), chunk_size=1024)

    assert target.read_bytes() == payload


async def test_a_url_download_over_the_cap_is_refused(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The same ceiling, reported the same way, on the second read path.

    And the same two claims as its sibling above: the cap held, counted
    off the refusal rather than off a file R22-AC5 no longer leaves
    behind, and the partial download was removed. See
    :func:`test_a_chunked_download_never_writes_more_than_the_cap` for
    why the count is the stricter of the two available assertions and
    for the mutation that proves it.
    """
    target = tmp_path / 'downloaded.bin'
    http_server.respond('/file', chunks=[b'w' * 1024] * 64)

    with pytest.raises(ResponseTooLargeError) as caught:
        await download_file_from_url(
            http_server.url_for('/file'),
            str(target),
            max_response_bytes=2048,
            chunk_size=1024,
        )

    assert accepted_bytes(caught.value) <= 2048 + 1024
    assert not target.exists()


async def test_a_forbidden_url_download_raises_before_it_reads_a_byte(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """A 403 is judged first, so its body is never read or written.

    The status used to be judged *after* the whole body had been read
    into memory and discarded, which is the one case where reading the
    body could not possibly help.
    """
    target = tmp_path / 'never-written.bin'
    http_server.respond('/file', status=403, body=b'denied')

    with pytest.raises(HttpStatusError) as caught:
        await download_file_from_url(
            http_server.url_for('/file'), str(target))

    assert caught.value.status_code == 403
    assert not target.exists()


# --- R14: every session construction carries an explicit deadline ---------


def _sessions_without_a_timeout(source: str) -> list[int]:
    """Find every ``ClientSession(...)`` built with no ``timeout=``.

    Matched on the parsed tree rather than with the spec's literal grep,
    so that prose naming the pattern -- including the docstrings that
    explain why it is banned -- cannot be mistaken for the pattern.

    Args:
        source: The module's source text.

    Returns:
        The line number of each offending construction, in file order.
    """
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, 'attr', getattr(node.func, 'id', ''))
        == 'ClientSession'
        and not any(kw.arg == 'timeout' for kw in node.keywords)
    )


def test_no_session_in_the_package_is_built_without_a_deadline() -> None:
    """R14's grep criterion, as a test so it cannot regress.

    Two constructions passed none. ``aiohttp``'s own default bounds the
    connect and leaves the transfer unbounded, so an endpoint that
    dribbled bytes pinned the calling task for as long as it liked.
    """
    package = Path(__file__).resolve().parents[2] / 'async_gateway'
    scanned = sorted(package.rglob('*.py'))

    offenders = [
        f'{path.relative_to(package).as_posix()}:{lineno}'
        for path in scanned
        for lineno in _sessions_without_a_timeout(
            path.read_text(encoding='utf-8'))
    ]

    assert scanned, f'no modules scanned under {package}'
    assert offenders == []


def test_the_session_scan_reports_a_construction_with_no_deadline(
) -> None:
    """A scan that silently matched nothing would report clean forever."""
    source = (
        'async def one():\n'
        '    return aiohttp.ClientSession()\n'
        'async def two():\n'
        '    return ClientSession(headers={})\n'
        'async def three():\n'
        '    return aiohttp.ClientSession(timeout=t)\n'
    )

    assert _sessions_without_a_timeout(source) == [2, 4]


async def test_a_url_download_sends_the_headers_and_deadline_it_was_given(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """Both optional arguments are real, and both are asserted.

    The deadline in particular: it is the argument R14 added, and an
    argument that were accepted and then ignored would leave the session
    on ``aiohttp``'s own connect-only default while looking configured.
    """
    target = tmp_path / 'downloaded.bin'
    http_server.respond('/file', body=b'ok')

    await download_file_from_url(
        http_server.url_for('/file'),
        str(target),
        headers={'X-Trace': 'abc'},
        timeout=2.0,
    )

    assert target.read_bytes() == b'ok'
    assert http_server.requests[-1].headers['X-Trace'] == 'abc'


# --- R22: the caller-supplied download path is guarded ---------------------


async def test_r22_a_symlink_at_the_download_path_is_refused(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """M18's headline case: the README's own fixed ``/tmp`` example.

    A hostile local process pre-creates a symlink at the predictable
    path the documented example downloads to, and the download used to
    follow it -- writing the endpoint's body through the link, into a
    file the caller never named, at whatever ``umask`` allowed.

    The assertion that matters is the second one: the victim file must
    still not exist. Asserting only the refusal would be satisfied by an
    implementation that wrote the bytes first and complained afterwards.
    """
    target = tmp_path / 'test.pdf'
    victim = tmp_path / 'victim'
    target.symlink_to(victim)
    http_server.respond('/file', body=b'through the link')

    with pytest.raises(PathContainmentError):
        await _fetch(
            http_server.url_for('/file'),
            http_file_download_config={'download_filepath': str(target)},
        )

    assert not victim.exists()


async def test_r22_a_download_refuses_to_overwrite_by_default(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """R22-AC3's chosen default, through the config a consumer writes.

    The original bytes are asserted, not merely the exception: a
    refusal that had already truncated the file would satisfy the first
    assertion and lose the caller's data anyway.
    """
    target = tmp_path / 'out.bin'
    target.write_bytes(b'the original')
    http_server.respond('/file', body=b'the replacement')

    with pytest.raises(ConfigurationError):
        await _fetch(
            http_server.url_for('/file'),
            http_file_download_config={'download_filepath': str(target)},
        )

    assert target.read_bytes() == b'the original'


async def test_r22_overwrite_true_is_plumbed_through_the_config(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The opt-in exists, reaches the writer, and is asserted end to end.

    Without this the shipped behaviour is "refuse, full stop" -- a hard
    regression for any consumer re-downloading to a stable path, which
    is exactly what the README's fixed ``/tmp/test.pdf`` example does on
    its second run. Driven through ``http_file_download_config``,
    because the criterion is about the surface a consumer configures and
    a test of the internal writer would prove the plumbing untested.
    """
    target = tmp_path / 'out.bin'
    target.write_bytes(b'the original')
    http_server.respond('/file', body=b'the replacement')

    await _fetch(
        http_server.url_for('/file'),
        http_file_download_config={
            'download_filepath': str(target),
            'overwrite': True,
        },
    )

    assert target.read_bytes() == b'the replacement'


async def test_r22_a_downloaded_file_is_not_world_readable(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """M18's mode half on the streamed path: 0600, whatever the umask.

    The README's examples download to ``/tmp``, which on a shared host
    is where a world-readable file is read by everyone on the box.
    """
    target = tmp_path / 'out.bin'
    http_server.respond('/file', body=b'private bytes')

    await _fetch(
        http_server.url_for('/file'),
        http_file_download_config={'download_filepath': str(target)},
    )

    assert target.stat().st_mode & 0o777 == 0o600


async def test_r22_a_multipart_download_is_guarded_too(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The third write path, which the first attempt at R22 left open.

    ``handle_multipart_response`` writes to the same caller-supplied
    ``download_filepath`` and had no containment at all -- so the
    symlink attack the streamed path now refuses simply moved one
    ``Content-Type`` across.
    """
    target = tmp_path / 'parts.bin'
    victim = tmp_path / 'victim'
    target.symlink_to(victim)
    http_server.respond(
        '/multipart',
        body=_multipart_body(b'part one'),
        headers=_multipart_headers(),
    )

    with pytest.raises(PathContainmentError):
        await _fetch(
            http_server.url_for('/multipart'),
            http_file_download_config={'download_filepath': str(target)},
        )

    assert not victim.exists()


async def test_r22_a_multipart_download_honours_overwrite(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """And the same opt-in reaches the multipart writer.

    Guarding a path but leaving no way to re-download to it would break
    the multipart case in exactly the way the streamed one is guarded
    against breaking.
    """
    target = tmp_path / 'parts.bin'
    target.write_bytes(b'the original')
    http_server.respond(
        '/multipart',
        body=_multipart_body(b'part one'),
        headers=_multipart_headers(),
    )

    await _fetch(
        http_server.url_for('/multipart'),
        http_file_download_config={
            'download_filepath': str(target),
            'overwrite': True,
        },
    )

    assert target.read_bytes() == b'part one'


async def test_r22_download_file_from_url_refuses_to_overwrite(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The standalone helper takes the same default and the same opt-in.

    It is a public coroutine with its own signature, so ``overwrite``
    had to be plumbed there separately -- and a helper that swallowed it
    in ``**kwargs`` would ship as refuse-only while looking configurable.
    """
    target = tmp_path / 'downloaded.bin'
    target.write_bytes(b'the original')
    http_server.respond('/file', body=b'the replacement')

    with pytest.raises(ConfigurationError):
        await download_file_from_url(
            http_server.url_for('/file'), str(target))

    assert target.read_bytes() == b'the original'

    await download_file_from_url(
        http_server.url_for('/file'), str(target), overwrite=True)

    assert target.read_bytes() == b'the replacement'


# --- R21: the verb allowlist on both HTTP getattr sites -------------------


@pytest.mark.parametrize(
    'verb',
    [
        pytest.param('close', id='close'),
        pytest.param('ws_connect', id='ws-connect'),
        pytest.param('detach', id='detach'),
        pytest.param('gett', id='typo'),
        pytest.param('', id='empty'),
        pytest.param(None, id='none'),
    ],
)
async def test_the_transport_refuses_a_verb_outside_the_allowlist(
    http_server: RecordingHTTPServer,
    verb: Any,
) -> None:
    """``make_http_request`` fails closed on a caller-named non-verb.

    ``close``, ``ws_connect`` and ``detach`` are all real attributes of a
    live ``aiohttp.ClientSession``, which is the finding: the unbounded
    ``getattr`` addressed the session's whole public API from a string
    the caller chose (M25). ``close`` is the documented case -- it
    resolved to ``ClientSession.close(url, **filters)`` and raised a
    ``TypeError`` that belonged to no transport family, so the envelope
    reported a fabricated status for what is a configuration error.
    """
    http_server.respond('/p', body=b'ok')

    async with aiohttp.ClientSession(timeout=TEST_TIMEOUT) as session:
        with pytest.raises(ConfigurationError) as caught:
            await make_http_request(
                session,
                http_server.url_for('/p'),
                _no_body,
                verb,
                redact_params=frozenset(),
                **POLICY,
            )

    assert repr(verb) in str(caught.value)
    assert 'get' in str(caught.value)
    assert http_server.requests == [], 'a refused verb was still dispatched'


async def test_a_refused_verb_leaves_the_session_usable(
    http_server: RecordingHTTPServer,
) -> None:
    """``request_type='close'`` must not actually close the session.

    The sharpest reading of the ``close`` row, and the one an envelope
    assertion cannot make. Under the old ``getattr`` the attribute was
    resolved and *called* -- with the URL as its first positional
    argument -- so the failure mode was never merely a bad status: it was
    a caller-supplied string invoking a lifecycle method on the library's
    own transport. A subsequent request over the same session succeeding
    is what proves nothing was invoked.
    """
    http_server.respond('/p', body=b'ok')

    async with aiohttp.ClientSession(timeout=TEST_TIMEOUT) as session:
        with pytest.raises(ConfigurationError):
            await make_http_request(
                session, http_server.url_for('/p'), _no_body, 'close',
                redact_params=frozenset(), **POLICY)

        assert not session.closed
        result = await make_http_request(
            session, http_server.url_for('/p'), _no_body, 'GET',
            redact_params=frozenset(), **POLICY)

    assert result['status_code'] == 200


@pytest.mark.parametrize(
    'verb',
    [
        pytest.param(' get ', id='surrounding-whitespace'),
        pytest.param('GeT', id='mixed-case'),
    ],
)
async def test_the_transport_admits_an_allowlisted_verb_in_any_casing(
    http_server: RecordingHTTPServer,
    verb: Text,
) -> None:
    """Normalisation is the allowlist's, not each call site's own.

    ``request_type`` reaches this function in the caller's own spelling
    -- it is echoed back in the envelope and in error messages -- so the
    allowlist has to do the normalising, and has to do it the same way
    ``is_get`` and ``resolve_protocol`` already do.
    """
    http_server.respond('/p', body=b'ok')

    async with aiohttp.ClientSession(timeout=TEST_TIMEOUT) as session:
        result = await make_http_request(
            session, http_server.url_for('/p'), _no_body, verb,
            redact_params=frozenset(), **POLICY)

    assert result['status_code'] == 200
    assert http_server.requests[0].method == 'GET'


@pytest.mark.parametrize(
    'verb',
    [
        pytest.param('close', id='close'),
        pytest.param('gett', id='typo'),
        pytest.param(None, id='none'),
    ],
)
async def test_a_url_download_refuses_a_verb_outside_the_allowlist(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    verb: Any,
) -> None:
    """The fifth ``getattr`` site, and it fails closed like the others.

    ``download_file_from_url`` builds its own session and reached the
    same unbounded lookup on it. Nothing must be written: a refused verb
    is a call that never happened, and a zero-length file left behind
    would be indistinguishable to the caller from a legitimate empty
    download.
    """
    target = tmp_path / 'never-written.bin'
    http_server.respond('/file', body=b'payload')

    with pytest.raises(ConfigurationError) as caught:
        await download_file_from_url(
            http_server.url_for('/file'), str(target), request_type=verb)

    assert repr(verb) in str(caught.value)
    assert not target.exists()
    assert http_server.requests == []

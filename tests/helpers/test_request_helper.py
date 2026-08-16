"""Tests for the HTTP transport boundary (spec R13, Step 11).

Multipart handling, the body decode, and the download-config defaults --
the three places ``make_http_request`` used to lose or corrupt what the
server sent (M7, M9, M10).

Every assertion here is made against a real ``aiohttp`` exchange with the
loopback recording server, because each defect lives in the interaction
with a real ``aiohttp`` object rather than in this library's own
arithmetic: ``MultipartReader.next()`` really does return None before
``at_eof()`` goes True on an ordinary well-formed body, and a stub reader
that did not would have let the ``AttributeError`` ship.
"""

import ast
from pathlib import Path

import aiohttp

from async_gateway.helpers.internal.request_helper import (
    DEFAULT_DOWNLOAD_FILEPATH,
    HttpResult,
    handle_multipart_response,
    make_http_request,
)

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

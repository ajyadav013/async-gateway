"""Tests for response handling on the HTTP client (spec R13, Step 11).

The one question these answer, in every shape it comes in: can a caller
tell an *empty* answer from a *broken* one? ``except ValueError: text = {}``
said no -- a truncated body, an HTML error page and a server that genuinely
answered ``{}`` all arrived as ``json: {}`` with ``ok`` True, and the caller
processed the third case's code path for all three.

Every case is driven through the public ``request()`` entry point against
the loopback recording server, because the distinction being asserted is a
property of the *envelope* -- the pairing of ``json``, ``ok`` and
``error['code']`` -- and only the entry point produces one.
"""

from typing import Any

from async_gateway.async_gateway import request
from async_gateway.utils.envelope import GatewayResponse

import pytest

from tests.fixtures.http_server import RecordingHTTPServer

JSON = {'Content-Type': 'application/json'}


async def _get(
    http_server: RecordingHTTPServer,
    *,
    body: bytes,
    headers: dict[str, str] | None = None,
    status: int = 200,
    **info: Any,
) -> GatewayResponse:
    """Fetch one canned response through the whole library.

    Args:
        http_server: The loopback recording server fixture.
        body: The response body the server returns.
        headers: The response headers, or None for none.
        status: The response status the server returns.
        info: Extra ``protocol_info`` keys.

    Returns:
        The finalised envelope ``request()`` returned.
    """
    http_server.respond(
        '/body', status=status, body=body, headers=headers or {})
    return await request(
        url=http_server.url_for('/body'),
        protocol='HTTP',
        protocol_info={'request_type': 'GET', **info},
    )


# --- R13: malformed is not empty (M8) -------------------------------------

async def test_a_malformed_json_body_is_reported_as_malformed(
    http_server: RecordingHTTPServer,
) -> None:
    """``json=None``, ``ok=False``, ``SERIALIZATION`` -- never ``{}``."""
    envelope = await _get(
        http_server, body=b'{"unterminated": ', headers=JSON)

    assert envelope['json'] is None
    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'SERIALIZATION'


async def test_a_legitimate_empty_object_is_reported_as_success(
    http_server: RecordingHTTPServer,
) -> None:
    """A server that answered ``{}`` answered, and ``ok`` says so."""
    envelope = await _get(http_server, body=b'{}', headers=JSON)

    assert envelope['json'] == {}
    assert envelope['ok'] is True
    assert envelope['error'] is None


async def test_the_two_are_distinguishable(
    http_server: RecordingHTTPServer,
) -> None:
    """The pair, asserted together, because that is the whole requirement.

    Either envelope read on its own looks plausible; it is only the
    difference between them that the caller needs and used not to have.
    """
    broken = await _get(http_server, body=b'not json at all', headers=JSON)
    empty = await _get(http_server, body=b'{}', headers=JSON)

    assert (broken['json'], broken['ok']) != (empty['json'], empty['ok'])
    assert broken['ok'] is False
    assert empty['ok'] is True


async def test_a_literal_null_body_is_not_a_parse_failure(
    http_server: RecordingHTTPServer,
) -> None:
    """``null`` is valid JSON and decodes to None -- a real answer.

    Its ``json`` is None, exactly as a parse failure's is, so ``ok`` is the
    key that separates them and it must not be spent on anything else.
    """
    envelope = await _get(http_server, body=b'null', headers=JSON)

    assert envelope['json'] is None
    assert envelope['ok'] is True
    assert envelope['error'] is None


async def test_an_empty_body_under_a_json_content_type_is_not_an_error(
    http_server: RecordingHTTPServer,
) -> None:
    """An empty body is absent, not broken, and not ``{}`` either.

    The other half of M8: an empty body used to yield ``json={}``, which
    told the caller the server had sent an object it never sent.
    """
    envelope = await _get(http_server, body=b'', headers=JSON)

    assert envelope['json'] is None
    assert envelope['ok'] is True
    assert envelope['text'] == ''


async def test_a_json_array_body_decodes(
    http_server: RecordingHTTPServer,
) -> None:
    """An array is as valid a JSON body as an object."""
    envelope = await _get(http_server, body=b'[1, 2]', headers=JSON)

    assert envelope['json'] == [1, 2]
    assert envelope['ok'] is True


# --- R13/R12: the response side uses the shared matcher -------------------

async def test_a_charset_parameter_does_not_stop_the_body_parsing(
    http_server: RecordingHTTPServer,
) -> None:
    """The response side normalises its media type like the request side."""
    envelope = await _get(
        http_server,
        body=b'{"a": 1}',
        headers={'Content-Type': 'application/json; charset=utf-8'},
    )

    assert envelope['json'] == {'a': 1}


async def test_a_problem_json_body_still_parses(
    http_server: RecordingHTTPServer,
) -> None:
    """RFC 6839's ``+json`` suffix is JSON, and the old matcher agreed.

    Substring matching accepted it; exact-key matching would not have, so
    keeping it is what makes the unification a fix rather than a trade.
    """
    envelope = await _get(
        http_server,
        body=b'{"title": "gone"}',
        status=410,
        headers={'Content-Type': 'application/problem+json'},
    )

    assert envelope['json'] == {'title': 'gone'}


async def test_a_non_json_body_is_left_in_text_and_is_not_an_error(
    http_server: RecordingHTTPServer,
) -> None:
    """An HTML page is not a broken JSON document; it is not JSON at all.

    Under the old substring matching the absent-header case fell through
    to a ``'default'`` entry and every body was pushed through the JSON
    decoder. Parsing only what the response announces is what keeps this
    from becoming a false ``SERIALIZATION``.
    """
    envelope = await _get(
        http_server,
        body=b'<html>not json</html>',
        headers={'Content-Type': 'text/html; charset=utf-8'},
    )

    assert envelope['json'] is None
    assert envelope['ok'] is True
    assert envelope['text'] == '<html>not json</html>'


# --- R13: an undecodable body sets text as well as error (M9) -------------

async def test_an_undecodable_body_sets_text_and_reports_the_diagnostic(
    http_server: RecordingHTTPServer,
) -> None:
    """No ``KeyError`` escapes, and the specific reason survives.

    ``error_message`` was set and ``text`` was not; ``logic/http.py`` read
    ``text`` unconditionally, so the caller received a ``KeyError`` from
    inside the library and the diagnostic was replaced by a fabricated
    ``999``.
    """
    envelope = await _get(
        http_server,
        body=b'\xff\xfe not utf-8',
        headers={'Content-Type': 'text/plain'},
    )

    assert envelope['text']
    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'SERIALIZATION'
    assert 'not decodable text' in envelope['error']['message']


async def test_an_undecodable_body_never_reports_a_key_error(
    http_server: RecordingHTTPServer,
) -> None:
    """Asserted on the error's identity, not only on its absence.

    A ``KeyError`` from this library's own code is a bug and propagates
    rather than becoming an envelope, so reaching this line at all is half
    the assertion; the other half is that the failure reported is the real
    one.
    """
    envelope = await _get(
        http_server,
        body=b'\x80\x81',
        headers={'Content-Type': 'text/plain'},
    )

    assert envelope['error'] is not None
    assert envelope['error']['type'] == 'SerializationError'
    assert 'KeyError' not in str(envelope['error'])


# --- R13/E11: the remote's own status wins --------------------------------

async def test_a_404_with_a_json_body_keeps_the_status_and_the_body(
    http_server: RecordingHTTPServer,
) -> None:
    """Invariant E11, on the path R13 changed."""
    envelope = await _get(
        http_server,
        body=b'{"error": "no such thing"}',
        status=404,
        headers=JSON,
    )

    assert envelope['status_code'] == 404
    assert envelope['json'] == {'error': 'no such thing'}
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'HTTP_STATUS'


async def test_a_404_with_a_malformed_body_is_still_reported_as_a_404(
    http_server: RecordingHTTPServer,
) -> None:
    """The status the server sent is not spent on the body's condition.

    ``SerializationError`` carries its own 502. Raised during the copy, it
    would have replaced the 404 the server actually returned, and the
    caller would have lost the answer they were given in order to be told
    about the body they could not have parsed anyway.
    """
    envelope = await _get(
        http_server,
        body=b'<html>500-ish error page</html>',
        status=404,
        headers=JSON,
    )

    assert envelope['status_code'] == 404
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'HTTP_STATUS'
    assert envelope['text'] == '<html>500-ish error page</html>'
    assert envelope['json'] is None


async def test_a_serialization_failure_still_carries_the_whole_response(
    http_server: RecordingHTTPServer,
) -> None:
    """``ok=False`` costs the caller nothing else (E11)."""
    envelope = await _get(
        http_server,
        body=b'{"truncated"',
        headers={'Content-Type': 'application/json', 'X-Trace': 'abc'},
    )

    assert envelope['text'] == '{"truncated"'
    assert envelope['headers']['X-Trace'] == 'abc'
    assert envelope['status_code'] == 502
    assert envelope['request_tracer']


@pytest.mark.parametrize(
    'body, expected_json, expected_ok',
    [
        pytest.param(b'{}', {}, True, id='empty-object'),
        pytest.param(b'null', None, True, id='literal-null'),
        pytest.param(b'', None, True, id='no-body'),
        pytest.param(b'{"a": 1}', {'a': 1}, True, id='object'),
        pytest.param(b'{oops}', None, False, id='malformed'),
    ],
)
async def test_every_json_body_shape_has_one_defined_envelope(
    http_server: RecordingHTTPServer,
    body: bytes,
    expected_json: dict[str, Any] | None,
    expected_ok: bool,
) -> None:
    """The five shapes, side by side, so no two of them collapse."""
    envelope = await _get(http_server, body=body, headers=JSON)

    assert envelope['json'] == expected_json
    assert envelope['ok'] is expected_ok

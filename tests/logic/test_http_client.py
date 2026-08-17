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

Step 13 (R14, and R21-AC6 via FI-16) adds the transport-resilience half
for the same reason: "an unresponsive endpoint yields ``TIMEOUT``/504",
"an over-sized body yields ``RESPONSE_TOO_LARGE``" and "a redirect to
``ftp://`` yields ``CONFIG`` and is never followed" are all claims about
the envelope, and the refusals are additionally asserted against the
fixture's **recorded request count**, which is this suite's replacement
for a mock's call count.
"""

from time import monotonic
from typing import Any

import aiohttp

from async_gateway.async_gateway import request
from async_gateway.utils.constants import HTTP_TIMEOUT
from async_gateway.utils.envelope import GatewayResponse
from async_gateway.utils.exceptions import ConfigurationError
from async_gateway.utils.request_tracer import request_tracer

import pytest

from tests.fixtures.http_server import RecordingHTTPServer

JSON = {'Content-Type': 'application/json'}

#: A deadline in tens of milliseconds, per R14's own wording. The gated
#: handler never answers, so this is the *only* thing the timeout test
#: waits on -- it is the behaviour under test, not a sleep placed to let
#: unrelated async work settle (which R28 forbids).
SHORT_DEADLINE = 0.05

#: How long a call configured with :data:`SHORT_DEADLINE` may actually
#: take before the deadline is not being honoured in any useful sense.
#: Two orders of magnitude of slack, so a loaded CI box cannot fail it and
#: a missing deadline cannot pass it.
DEADLINE_TOLERANCE = 5.0


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


# --- R14: the caller may supply the session, and keeps it -----------------


async def _get_with(
    http_server: RecordingHTTPServer,
    path: str = '/body',
    **info: Any,
) -> GatewayResponse:
    """Fetch ``path`` through the entry point with extra protocol_info.

    Args:
        http_server: The loopback recording server fixture.
        path: The path to fetch; the caller registers its response.
        info: Extra ``protocol_info`` keys.

    Returns:
        The finalised envelope ``request()`` returned.
    """
    return await request(
        url=http_server.url_for(path),
        protocol='HTTP',
        protocol_info={'request_type': 'GET', **info},
    )


async def test_a_caller_supplied_session_is_used_and_left_open(
    http_server: RecordingHTTPServer,
) -> None:
    """The library never closes a session it did not create.

    Closing it would defeat the point of accepting one: the caller holds
    it precisely so the next call reuses its connection pool.
    """
    http_server.respond('/body', body=b'ok')
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    try:
        envelope = await _get_with(http_server, session=session)

        assert envelope['ok'] is True
        assert envelope['text'] == 'ok'
        assert session.closed is False
        assert len(http_server.requests) == 1
    finally:
        await session.close()


async def test_two_calls_on_one_supplied_session_reuse_the_connection(
    http_server: RecordingHTTPServer,
) -> None:
    """Pooling is the whole reason to accept a session, so it is measured.

    Read off the tracer's ``on_connection_reuseconn`` event, which fires
    only when a connection is taken from the pool rather than dialled.
    The tracer is attached to the caller's own session because trace
    configs are a session-level thing -- which is exactly why a supplied
    session must bring its own, and why supplying both is refused.
    """
    http_server.respond('/body', body=b'ok')
    tracer = request_tracer()
    session = aiohttp.ClientSession(
        trace_configs=[tracer],
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    try:
        await _get_with(http_server, session=session)
        assert 'on_connection_reuseconn' not in tracer.results_collector

        await _get_with(http_server, session=session)

        assert 'on_connection_reuseconn' in tracer.results_collector
        assert session.closed is False
    finally:
        await session.close()


async def test_two_calls_without_a_supplied_session_dial_twice(
    http_server: RecordingHTTPServer,
) -> None:
    """The pair to the test above, and the reason it is not vacuous.

    Without a supplied session each call builds and closes its own, so
    there is no pool to reuse: an assertion that reuse *happened* means
    nothing unless the same measurement can show it not happening.
    """
    http_server.respond('/body', body=b'ok')
    tracer = request_tracer()

    await _get_with(http_server, trace_config=[tracer])
    await _get_with(http_server, trace_config=[tracer])

    assert 'on_connection_reuseconn' not in tracer.results_collector


async def test_a_session_the_library_created_is_closed_on_the_way_out(
    http_server: RecordingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session this library owns is a leak if it is not closed.

    Asserted on the object itself rather than inferred from the absence
    of a warning: an unclosed ``ClientSession`` only complains when the
    garbage collector eventually gets to it, which is nowhere near the
    test that caused it.
    """
    http_server.respond('/body', body=b'ok')
    created: list[aiohttp.ClientSession] = []

    class _RecordingSession(aiohttp.ClientSession):
        """A session that registers itself so the test can inspect it."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Build the session and record it.

            Args:
                args: Forwarded verbatim to ``aiohttp.ClientSession``.
                kwargs: Forwarded verbatim to ``aiohttp.ClientSession``.
            """
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(
        'async_gateway.logic.http_client.aiohttp.ClientSession',
        _RecordingSession)

    await _get_with(http_server)

    assert len(created) == 1
    assert created[0].closed is True


async def test_a_closed_session_is_refused_as_configuration(
    http_server: RecordingHTTPServer,
) -> None:
    """A closed session is the caller's mistake, named as such.

    Left to ``aiohttp`` it surfaces as ``RuntimeError: Session is
    closed`` from inside the transport -- which reads as a network fault,
    is classified as one, and is retried like one.
    """
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    await session.close()

    with pytest.raises(ConfigurationError) as caught:
        await _get_with(http_server, session=session)

    assert caught.value.code == 'CONFIG'
    assert 'closed' in str(caught.value)
    assert http_server.requests == []


async def test_a_session_that_is_not_a_session_is_refused(
    http_server: RecordingHTTPServer,
) -> None:
    """The other way the key can be wrong, refused the same way."""
    with pytest.raises(ConfigurationError):
        await _get_with(http_server, session=object())

    assert http_server.requests == []


async def test_the_per_request_deadline_outranks_the_sessions_own(
    http_server: RecordingHTTPServer,
) -> None:
    """A supplied session's deadline must not silently replace the call's.

    The session allows five seconds and the call allows fifty
    milliseconds against a handler that never answers. Applying the
    timeout only where the session is built -- which is where it used to
    be applied -- would leave this call waiting on the session's five,
    and the deadline the caller actually configured would be decoration.
    """
    http_server.gate('/slow')
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=5.0))
    started = monotonic()
    try:
        envelope = await _get_with(
            http_server, '/slow', session=session, timeout=SHORT_DEADLINE)
    finally:
        await session.close()

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'TIMEOUT'
    assert monotonic() - started < 5.0


# --- R14: the deadline is honoured on the transfer, not only the connect --


async def test_a_server_that_never_answers_times_out_as_a_504(
    http_server: RecordingHTTPServer,
) -> None:
    """The connect succeeds; it is the *answer* that never comes.

    A connect-only deadline is satisfied instantly here -- the loopback
    listener accepts at once -- so this fails unless the deadline covers
    the whole exchange. The handler waits on an event the fixture
    releases at teardown, so the only thing this test waits on is the
    deadline it is asserting.
    """
    gate = http_server.gate('/slow')
    started = monotonic()

    envelope = await _get_with(http_server, '/slow', timeout=SHORT_DEADLINE)
    elapsed = monotonic() - started

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'TIMEOUT'
    assert envelope['status_code'] == 504
    assert elapsed < DEADLINE_TOLERANCE
    assert gate.is_set() is False


# --- R14: response reads are capped ---------------------------------------


async def test_a_body_over_the_cap_is_reported_as_response_too_large(
    http_server: RecordingHTTPServer,
) -> None:
    """An over-sized body is a named failure, not an unbounded allocation."""
    http_server.respond('/big', chunks=[b'x' * 4096] * 64)

    envelope = await _get_with(
        http_server, '/big', max_response_bytes=2048)

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'RESPONSE_TOO_LARGE'
    assert envelope['status_code'] == 502


@pytest.mark.parametrize(
    'cap, expected_ok',
    [
        pytest.param(4096, True, id='room-to-spare'),
        pytest.param(2000, False, id='too-small'),
    ],
)
async def test_the_cap_is_what_the_caller_set_it_to(
    http_server: RecordingHTTPServer,
    cap: int,
    expected_ok: bool,
) -> None:
    """The same body passes or fails purely on the caller's number.

    Two rows against one body, so the assertion is that the *cap* decides
    rather than that some fixed size happens to be refused.
    """
    http_server.respond('/sized', body=b'z' * 3000)

    envelope = await _get_with(http_server, '/sized', max_response_bytes=cap)

    assert envelope['ok'] is expected_ok


@pytest.mark.parametrize(
    'value',
    [
        pytest.param(0, id='zero'),
        pytest.param(-1, id='negative'),
        pytest.param(None, id='none'),
        pytest.param('64', id='string'),
        pytest.param(True, id='bool'),
    ],
)
async def test_a_cap_that_is_not_a_positive_int_is_refused(
    http_server: RecordingHTTPServer,
    value: object,
) -> None:
    """There is no sentinel that disables the cap, and none is inferred.

    ``0`` and ``-1`` and ``None`` are the three spellings a caller would
    reach for to mean "unlimited"; each is rejected rather than quietly
    granted. ``True`` is rejected with them because it is an ``int`` of
    value 1 and a one-byte ceiling is nobody's intent.
    """
    with pytest.raises(ConfigurationError):
        await _get_with(http_server, max_response_bytes=value)

    assert http_server.requests == []


# --- R14/R21-AC6 (FI-16): the per-hop scheme check ------------------------


@pytest.mark.parametrize(
    'location',
    [
        pytest.param('ftp://evil.invalid/payload', id='ftp'),
        pytest.param('file:///etc/passwd', id='file'),
    ],
)
async def test_a_redirect_out_of_the_allowlist_is_refused_unfollowed(
    http_server: RecordingHTTPServer,
    location: str,
) -> None:
    """R21-AC6: the hop is refused *before* it is issued, not after.

    The recorded request count is the assertion that matters. Under
    ``aiohttp``'s own loop the scheme check saw only the caller's initial
    URL, the redirect was followed transparently, and this count would be
    two -- the guardrail enforced on the one URL the caller chose and on
    none of the ones an attacker chooses.
    """
    http_server.respond(
        '/start', status=302, headers={'Location': location})

    envelope = await _get_with(http_server, '/start')

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'CONFIG'
    assert envelope['status_code'] == 400
    assert len(http_server.requests) == 1


async def test_a_downgrade_to_plain_http_is_refused_when_https_is_required(
    http_server: RecordingHTTPServer,
) -> None:
    """The third hostile target R14 names, and the least obvious one.

    ``http://`` is in the default allowlist, so this drives a caller who
    narrowed it -- the same shape a caller reaches for when the whole
    point of the call was that it went over TLS.
    """
    http_server.respond(
        '/start',
        status=302,
        headers={'Location': http_server.url_for('/end')})
    http_server.respond('/end', body=b'plaintext')

    envelope = await _get_with(
        http_server, '/start', allowed_schemes=frozenset({'https'}))

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'CONFIG'
    assert len(http_server.requests) == 1


@pytest.mark.parametrize(
    'location',
    [
        pytest.param('http://host:99999/next', id='port-out-of-range'),
        pytest.param('http://host:abc/next', id='port-not-a-number'),
    ],
)
async def test_a_location_with_a_malformed_authority_is_enveloped(
    http_server: RecordingHTTPServer,
    location: str,
) -> None:
    """A hostile authority is a CONFIG envelope, not a bare ``ValueError``.

    ``urlsplit`` parses the authority lazily, so these two split without
    complaint and raised only when ``.port`` was first read -- which
    happened in ``same_origin``, past the guard in ``redirect_target``.
    Driven end to end because the escape was end to end: a
    ``builtins.ValueError`` came out of the public ``request()``
    un-enveloped, defeating the one-conversion-point contract.
    """
    http_server.respond(
        '/start', status=302, headers={'Location': location})

    envelope = await _get_with(http_server, '/start')

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'CONFIG'
    assert envelope['status_code'] == 400
    assert len(http_server.requests) == 1


async def test_a_relative_location_is_resolved_and_followed(
    http_server: RecordingHTTPServer,
) -> None:
    """A relative hop is legitimate and must still arrive at the right URL.

    The pair to the refusals above: a loop that rejected everything, or
    one that mis-resolved ``/end`` against the origin, would satisfy them
    and break every ordinary redirect.
    """
    http_server.respond('/start', status=302, headers={'Location': '/end'})
    http_server.respond('/end', body=b'arrived')

    envelope = await _get_with(http_server, '/start')

    assert envelope['ok'] is True
    assert envelope['text'] == 'arrived'
    assert [r.path for r in http_server.requests] == ['/start', '/end']


async def test_a_chain_one_hop_longer_than_the_bound_fails(
    http_server: RecordingHTTPServer,
) -> None:
    """``max_redirects`` bounds the chain, and the last status is reported.

    Three hops against a bound of two: the third redirect response is
    where the loop gives up, so exactly three requests reach the server
    and the 302 it answered with is what the envelope carries.
    """
    for hop in range(4):
        http_server.respond(
            f'/hop{hop}',
            status=302,
            headers={'Location': f'/hop{hop + 1}'})

    envelope = await _get_with(http_server, '/hop0', max_redirects=2)

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['status_code'] == 302
    assert [r.path for r in http_server.requests] == [
        '/hop0', '/hop1', '/hop2']


async def test_a_chain_exactly_at_the_bound_still_arrives(
    http_server: RecordingHTTPServer,
) -> None:
    """The bound is a maximum followed, not one hop fewer."""
    http_server.respond('/hop0', status=302, headers={'Location': '/hop1'})
    http_server.respond('/hop1', status=302, headers={'Location': '/hop2'})
    http_server.respond('/hop2', body=b'arrived')

    envelope = await _get_with(http_server, '/hop0', max_redirects=2)

    assert envelope['ok'] is True
    assert envelope['text'] == 'arrived'


@pytest.mark.parametrize(
    'location',
    [
        pytest.param('', id='empty'),
        pytest.param('   ', id='whitespace'),
    ],
)
async def test_a_blank_location_is_absent_and_is_not_a_hop(
    http_server: RecordingHTTPServer,
    location: str,
) -> None:
    """A 302 with a blank ``Location`` is answered, not re-requested.

    The header is a ``str``, so ``''`` is not None and the loop entered on
    it -- and ``urljoin(target, '')`` is ``target``, so the library
    re-issued the *same* request until ``max_redirects``: one recorded
    request became eleven, and forty-four with retries configured. That is
    a request amplifier a hostile endpoint controls with one empty header.
    ``aiohttp`` hands the 302 back, which is what the recorded count of one
    and the ``ok=True`` envelope below assert this loop now does too.
    """
    http_server.respond(
        '/start', status=302, headers={'Location': location}, body=b'moved')

    envelope = await _get_with(http_server, '/start')

    assert envelope['ok'] is True
    assert envelope['status_code'] == 302
    assert envelope['text'] == 'moved'
    assert [r.path for r in http_server.requests] == ['/start']


# --- R14: the deadline bounds the chain, not each hop of it ---------------

#: Seconds each hop of the slow chain below takes to answer, and how many
#: redirect hops there are. Seven responses at a tenth of a second is
#: ``SLOW_CHAIN_SECONDS`` of server time -- several times
#: :data:`CHAIN_DEADLINE`, which is what makes a budget spent once over
#: the chain observably different from one handed afresh to every hop.
SLOW_HOP_DELAY = 0.1
SLOW_HOP_COUNT = 6
SLOW_CHAIN_SECONDS = SLOW_HOP_COUNT * SLOW_HOP_DELAY
CHAIN_DEADLINE = 0.15

#: How long the slow hop of the two-redirect chains below takes to answer.
#: Large enough that an ``on_request_redirect`` timed from the last
#: redirecting hop's own start cannot be confused with one timed from the
#: start of the chain: on the discriminating shape the two differ by this
#: whole delay.
LATE_HOP_DELAY = 0.3

#: The ceiling the redirect event must come in under on the shape where
#: the delay precedes the last redirecting hop. Two orders of magnitude
#: below :data:`LATE_HOP_DELAY`, so it separates "the hop's own start"
#: (measured 0.0003-0.0021s) from "the chain's start" (0.3036-0.3064s)
#: with room for a loaded CI machine in between.
PROMPT_HOP_CEILING = 0.05


def _register_slow_chain(http_server: RecordingHTTPServer) -> None:
    """Register a redirect chain whose every hop answers slowly.

    Args:
        http_server: The loopback recording server fixture.

    Returns:
        None.
    """
    for hop in range(SLOW_HOP_COUNT):
        http_server.respond(
            f'/slow{hop}',
            status=302,
            headers={'Location': f'/slow{hop + 1}'},
            delay=SLOW_HOP_DELAY)
    http_server.respond(
        f'/slow{SLOW_HOP_COUNT}', body=b'arrived', delay=SLOW_HOP_DELAY)


async def test_the_deadline_bounds_the_whole_chain_not_each_hop(
    http_server: RecordingHTTPServer,
) -> None:
    """R14: "a caller-set ``timeout`` is honoured on every protocol".

    While ``aiohttp`` owned the redirect loop its ``ClientTimeout(total=)``
    bounded the *chain*. Owning the loop and passing the caller's timeout
    unchanged to every hop hands each one a fresh budget, so this chain --
    seven slow answers against a deadline of a seventh of what they cost
    -- ran to completion and returned ``ok=True`` after the full
    ``SLOW_CHAIN_SECONDS``, pinning the caller's task for far longer than
    the deadline it set. With ``max_redirects`` at its default of ten that
    is an eleven-fold overrun, which is precisely the hazard R14's user
    story names.

    Asserted structurally as well as temporally: the chain must not have
    run to its end, whatever the clock on a loaded box says.
    """
    _register_slow_chain(http_server)
    started = monotonic()

    envelope = await _get_with(http_server, '/slow0', timeout=CHAIN_DEADLINE)
    elapsed = monotonic() - started

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'TIMEOUT'
    assert envelope['status_code'] == 504
    assert len(http_server.requests) < SLOW_HOP_COUNT + 1
    assert elapsed < SLOW_CHAIN_SECONDS


async def test_a_slow_chain_inside_its_deadline_still_arrives(
    http_server: RecordingHTTPServer,
) -> None:
    """The pair to the test above, and the reason it is not vacuous.

    A deadline subtracted wrongly -- or one re-read from a clock that
    never advances the budget -- could refuse every chain and satisfy the
    assertion above. The same slow chain against a deadline that
    comfortably covers it must still arrive at the last hop.
    """
    _register_slow_chain(http_server)

    envelope = await _get_with(http_server, '/slow0', timeout=HTTP_TIMEOUT)

    assert envelope['ok'] is True
    assert envelope['text'] == 'arrived'
    assert len(http_server.requests) == SLOW_HOP_COUNT + 1


async def test_a_followed_hop_is_recorded_on_the_envelopes_tracer(
    http_server: RecordingHTTPServer,
) -> None:
    """``request_tracer['is_redirect']`` must not go dead when we own the loop.

    ``aiohttp``'s ``on_request_redirect`` callback cannot fire once the
    transport is called with ``allow_redirects=False``, so the two keys it
    used to set reported "no redirect happened" on every chain this
    library followed for itself -- a top-level ``GatewayResponse`` key
    consumers branch on, quietly lying.
    """
    http_server.respond('/start', status=302, headers={'Location': '/end'})
    http_server.respond('/end', body=b'arrived')

    envelope = await _get_with(http_server, '/start')

    assert envelope['ok'] is True
    trace = envelope['request_tracer'][0]
    assert trace['is_redirect'] is True
    assert trace['on_request_redirect'] >= 0


async def test_a_call_that_never_redirects_reports_no_redirect(
    http_server: RecordingHTTPServer,
) -> None:
    """The pair: a field set unconditionally would be just as useless."""
    http_server.respond('/body', body=b'ok')

    envelope = await _get_with(http_server)

    trace = envelope['request_tracer'][0]
    assert trace['is_redirect'] is False
    assert 'on_request_redirect' not in trace


async def test_a_refused_hop_is_not_recorded_as_a_followed_one(
    http_server: RecordingHTTPServer,
) -> None:
    """A hop rejected by the scheme check was never followed.

    Recording it would make ``is_redirect`` mean "a redirect was seen",
    which is not what it meant while ``aiohttp`` set it.
    """
    http_server.respond(
        '/start', status=302, headers={'Location': 'ftp://example.invalid/x'})
    tracer = request_tracer()

    envelope = await _get_with(http_server, '/start', trace_config=[tracer])

    assert envelope['ok'] is False
    assert tracer.results_collector['is_redirect'] is False


async def test_a_chain_that_times_out_still_reports_the_hops_it_followed(
    http_server: RecordingHTTPServer,
) -> None:
    """The trace event must survive the exits that are not the return.

    Recording only where the loop returns a response left ``is_redirect``
    False on every chain that ended in a timeout -- the same dead flag
    the event was written to fix, surviving on the paths nothing
    measured. Measured against raw ``aiohttp`` on this exact shape: two
    hops followed and then a stall, ``aiohttp`` reports True, this
    library reported False.
    """
    http_server.respond('/a', status=302, headers={'Location': '/b'})
    http_server.respond('/b', status=302, headers={'Location': '/c'})
    http_server.gate('/c')
    tracer = request_tracer()

    envelope = await _get_with(
        http_server,
        '/a',
        timeout=SHORT_DEADLINE,
        trace_config=[tracer])

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'TIMEOUT'
    assert [r.path for r in http_server.requests] == ['/a', '/b', '/c']
    assert tracer.results_collector['is_redirect'] is True
    assert tracer.results_collector['on_request_redirect'] >= 0


async def test_a_chain_that_outran_its_bound_still_reports_its_hops(
    http_server: RecordingHTTPServer,
) -> None:
    """The second error exit, and the one with a status of its own.

    ``max_redirects`` exceeded raises before the loop returns, so the
    three hops this chain provably followed were reported as no redirect
    at all. Raw ``aiohttp``, driven with ``max_redirects=2`` over the
    identical chain, reports ``is_redirect`` True.
    """
    for hop in range(4):
        http_server.respond(
            f'/hop{hop}', status=302, headers={'Location': f'/hop{hop + 1}'})
    tracer = request_tracer()

    envelope = await _get_with(
        http_server, '/hop0', max_redirects=2, trace_config=[tracer])

    assert envelope['ok'] is False
    assert envelope['status_code'] == 302
    assert [r.path for r in http_server.requests] == [
        '/hop0', '/hop1', '/hop2']
    assert tracer.results_collector['is_redirect'] is True
    assert tracer.results_collector['on_request_redirect'] >= 0


async def test_the_redirect_event_is_timed_from_the_hop_not_the_chain(
    http_server: RecordingHTTPServer,
) -> None:
    """The delay-on-the-FIRST-hop shape, which is the discriminating one.

    ``aiohttp`` timed ``on_request_redirect`` from the redirecting
    request's own ``on_request_start``; this library owns the loop and
    must reproduce that instant, not the start of the whole chain. Two
    earlier attempts to test that picked shapes on which **both**
    hypotheses predict the same number, and each read the agreement as
    evidence:

    * one redirect with the delay on it -- the only hop *is* the first
      hop, so the hop's start and the chain's start coincide;
    * two redirects with the delay on the **second** -- the delay falls
      inside the last redirecting hop, so it lands in both measures
      alike (both read ~0.30s).

    Here the 0.30s sits on the **first** hop and the last redirecting hop
    answers at once, so the two predictions are two orders of magnitude
    apart: chain-based gives ~0.30s, hop-based ~0.001s. Measured on it,
    the chain-based code read 0.3036-0.3064s where raw ``aiohttp`` on the
    same fixture read 0.0007-0.0011s -- a real divergence the earlier
    shapes hid. After re-basing, 0.0003-0.0021s against ``aiohttp``'s
    0.0002-0.0008s.
    """
    http_server.respond(
        '/a', status=302, headers={'Location': '/b'}, delay=LATE_HOP_DELAY)
    http_server.respond('/b', status=302, headers={'Location': '/c'})
    http_server.respond('/c', body=b'arrived')

    envelope = await _get_with(http_server, '/a')

    assert envelope['ok'] is True
    assert envelope['text'] == 'arrived'
    trace = envelope['request_tracer'][0]
    assert trace['is_redirect'] is True
    assert trace['on_request_redirect'] < PROMPT_HOP_CEILING


async def test_a_delay_inside_the_last_hop_is_still_counted(
    http_server: RecordingHTTPServer,
) -> None:
    """The pair: the base instant moved, the measurement did not vanish.

    Round 3's shape -- the delay on the **second** of two redirects --
    cannot separate the two hypotheses, because the delay is inside the
    last redirecting hop and so lands in both. That makes it useless as
    the pin, and exactly right as its counterweight: a "fix" that read
    the event as zero whenever it was asked would pass the test above
    and fail here. Both libraries put the event after the delay on this
    shape (measured, after the fix: this library 0.3015-0.3026s, raw
    ``aiohttp`` 0.3011-0.3021s).
    """
    http_server.respond('/a', status=302, headers={'Location': '/b'})
    http_server.respond(
        '/b', status=302, headers={'Location': '/c'}, delay=LATE_HOP_DELAY)
    http_server.respond('/c', body=b'arrived')

    envelope = await _get_with(http_server, '/a')

    assert envelope['ok'] is True
    trace = envelope['request_tracer'][0]
    assert trace['is_redirect'] is True
    assert trace['on_request_redirect'] >= LATE_HOP_DELAY


async def test_allow_redirects_false_returns_the_redirect_itself(
    http_server: RecordingHTTPServer,
) -> None:
    """Not following is a caller's choice, and it stops at one request."""
    http_server.respond('/start', status=302, headers={'Location': '/end'})
    http_server.respond('/end', body=b'never reached')

    envelope = await _get_with(
        http_server, '/start', allow_redirects=False)

    assert envelope['status_code'] == 302
    assert [r.path for r in http_server.requests] == ['/start']


async def test_credentials_do_not_cross_an_origin_on_a_redirect(
    http_server: RecordingHTTPServer,
) -> None:
    """Owning the loop must not leak what aiohttp's loop stripped.

    ``aiohttp`` drops ``Authorization`` and ``Cookie`` when a redirect
    changes origin. Taking the loop over to add a scheme check, and
    forwarding the caller's bearer token to whatever host a hostile
    endpoint named, would trade one hole for a worse one -- so the header
    is asserted absent on the second origin and present on the first.
    """
    elsewhere = RecordingHTTPServer()
    await elsewhere.start()
    try:
        elsewhere.respond('/end', body=b'arrived')
        http_server.respond(
            '/start',
            status=302,
            headers={'Location': elsewhere.url_for('/end')})

        envelope = await _get_with(
            http_server,
            '/start',
            headers={'Authorization': 'Bearer supersecret'},
        )

        assert envelope['ok'] is True
        assert envelope['text'] == 'arrived'
        assert http_server.requests[-1].headers.get(
            'Authorization') == 'Bearer supersecret'
        assert 'Authorization' not in elsewhere.requests[-1].headers
    finally:
        await elsewhere.close()


async def test_a_same_origin_redirect_keeps_the_callers_headers(
    http_server: RecordingHTTPServer,
) -> None:
    """The pair to the test above: stripping everything would break auth.

    A redirect within one origin is the ordinary case, and dropping the
    caller's credentials on it would turn a working call into a 401.
    """
    http_server.respond('/start', status=302, headers={'Location': '/end'})
    http_server.respond('/end', body=b'arrived')

    await _get_with(
        http_server,
        '/start',
        headers={'Authorization': 'Bearer supersecret'},
    )

    assert http_server.requests[-1].path == '/end'
    assert http_server.requests[-1].headers.get(
        'Authorization') == 'Bearer supersecret'


async def test_a_303_is_re_issued_as_a_bodyless_get(
    http_server: RecordingHTTPServer,
) -> None:
    """Owning the loop preserves aiohttp's semantics rather than new ones.

    A 303 answering a POST means "go and GET this instead". Re-POSTing
    the body would be a behaviour change nothing in R14 asked for, and it
    would re-send the caller's payload to a URL they never named.
    """
    http_server.respond(
        '/submit', method='POST', status=303,
        headers={'Location': '/result'})
    http_server.respond('/result', method='GET', body=b'done')

    envelope = await request(
        url=http_server.url_for('/submit'),
        data={'field': 'value'},
        protocol='HTTP',
        protocol_info={'request_type': 'POST'},
    )

    assert envelope['ok'] is True
    assert envelope['text'] == 'done'
    assert [(r.method, r.path) for r in http_server.requests] == [
        ('POST', '/submit'), ('GET', '/result')]
    assert http_server.requests[-1].body == b''


# --- R14: what moved from the session to the request still arrives --------


async def test_caller_headers_reach_the_wire(
    http_server: RecordingHTTPServer,
) -> None:
    """``protocol_info['headers']`` used to be applied to the session.

    It is applied per request now, so that a caller-supplied session
    honours it and so the redirect loop can withhold it on a hop that
    crosses an origin. Both are improvements only if the ordinary case
    still works, which is what this pins.
    """
    http_server.respond('/body', body=b'ok')

    await _get_with(http_server, headers={'X-Trace': 'abc'})

    assert http_server.requests[-1].headers['X-Trace'] == 'abc'


async def test_caller_cookies_reach_the_wire(
    http_server: RecordingHTTPServer,
) -> None:
    """The same move, for ``protocol_info['cookies']``."""
    http_server.respond('/body', body=b'ok')

    await _get_with(http_server, cookies={'session': 'value'})

    assert 'session=value' in http_server.requests[-1].headers['Cookie']


async def test_caller_auth_reaches_the_wire(
    http_server: RecordingHTTPServer,
) -> None:
    """The same move, for the entry point's ``auth`` argument.

    ``auth`` had to move for the redirect loop to be able to *withhold*
    it: a session default cannot be suppressed on one request, so an
    ``auth`` left on the session would have followed a hostile redirect
    to another host no matter what the loop did about the headers.
    """
    http_server.respond('/body', body=b'ok')

    await request(
        url=http_server.url_for('/body'),
        auth=aiohttp.BasicAuth('user', 'password'),
        protocol='HTTP',
        protocol_info={'request_type': 'GET'},
    )

    assert http_server.requests[-1].headers['Authorization'].startswith(
        'Basic ')


async def test_auth_does_not_cross_an_origin_on_a_redirect(
    http_server: RecordingHTTPServer,
) -> None:
    """The reason ``auth`` had to leave the session, asserted directly."""
    elsewhere = RecordingHTTPServer()
    await elsewhere.start()
    try:
        elsewhere.respond('/end', body=b'arrived')
        http_server.respond(
            '/start',
            status=302,
            headers={'Location': elsewhere.url_for('/end')})

        await request(
            url=http_server.url_for('/start'),
            auth=aiohttp.BasicAuth('user', 'password'),
            protocol='HTTP',
            protocol_info={'request_type': 'GET'},
        )

        assert 'Authorization' in http_server.requests[-1].headers
        assert 'Authorization' not in elsewhere.requests[-1].headers
    finally:
        await elsewhere.close()


# --- R14: a supplied session may not carry a credential of its own --------


@pytest.mark.parametrize(
    'header',
    [
        pytest.param('Authorization', id='authorization'),
        pytest.param('Cookie', id='cookie'),
        pytest.param('Proxy-Authorization', id='proxy-authorization'),
        pytest.param('authorization', id='lower-cased'),
    ],
)
async def test_a_session_carrying_a_credential_header_is_refused(
    http_server: RecordingHTTPServer,
    header: str,
) -> None:
    """The leak accepting a session opened, refused where it opens.

    ``aiohttp`` merges a session's default headers into every request it
    issues, and the redirect loop can only withhold the headers *it*
    passes. A ``ClientSession(headers={'Authorization': ...})`` therefore
    sent the caller's token to whatever host a hostile ``Location``
    named -- measured on two loopback servers, and a regression against
    raw ``aiohttp``, which strips it. Rejecting the session is what keeps
    ``make_http_request``'s documented promise -- that crossing an origin
    strips the credential headers -- true on every path rather than on
    all but this one.
    """
    session = aiohttp.ClientSession(
        headers={header: 'Bearer sessionsecret'},
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    try:
        with pytest.raises(ConfigurationError) as caught:
            await _get_with(http_server, session=session)
    finally:
        await session.close()

    assert caught.value.code == 'CONFIG'
    assert header.lower() in str(caught.value)
    assert 'protocol_info["headers"]' in str(caught.value)
    assert http_server.requests == []


async def test_a_session_carrying_its_own_auth_is_refused(
    http_server: RecordingHTTPServer,
) -> None:
    """``auth`` on the session is the same leak in its other spelling.

    A session-level ``auth`` becomes an ``Authorization`` header inside
    ``aiohttp``, below the layer this library can withhold on a hop.
    """
    session = aiohttp.ClientSession(
        auth=aiohttp.BasicAuth('user', 'password'),
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    try:
        with pytest.raises(ConfigurationError) as caught:
            await _get_with(http_server, session=session)
    finally:
        await session.close()

    assert caught.value.code == 'CONFIG'
    assert 'auth' in str(caught.value)
    assert http_server.requests == []


async def test_a_credential_free_session_still_strips_on_a_cross_origin_hop(
    http_server: RecordingHTTPServer,
) -> None:
    """The route the refusal names, driven end to end through ``session=``.

    The pair to the refusals above, and the reason they are a redirect to
    the supported route rather than a removal of a feature: a caller who
    supplies a session *and* passes credentials per call gets both the
    connection pooling and the stripping. No redirect test went through
    ``session=`` before, which is why the leak on that path was invisible
    to a suite that covered the same hop without one.
    """
    elsewhere = RecordingHTTPServer()
    session = aiohttp.ClientSession(
        headers={'X-Trace': 'kept'},
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    await elsewhere.start()
    try:
        elsewhere.respond('/end', body=b'arrived')
        http_server.respond(
            '/start',
            status=302,
            headers={'Location': elsewhere.url_for('/end')})

        envelope = await _get_with(
            http_server,
            '/start',
            session=session,
            headers={'Authorization': 'Bearer supersecret'},
        )

        assert envelope['ok'] is True
        assert envelope['text'] == 'arrived'
        assert http_server.requests[-1].headers.get(
            'Authorization') == 'Bearer supersecret'
        assert 'Authorization' not in elsewhere.requests[-1].headers
        # A non-credential session default is untouched by any of this.
        assert elsewhere.requests[-1].headers.get('X-Trace') == 'kept'
    finally:
        await elsewhere.close()
        await session.close()


async def test_a_serializer_cannot_be_combined_with_a_supplied_session(
    http_server: RecordingHTTPServer,
) -> None:
    """``json_serialize`` is session-only, so the pair is refused.

    It used to be validated and then silently dropped: the caller's
    serialiser had no effect at all, and nothing said so. An undocumented
    no-op on the key that decides the bytes of the body is the shape of
    defect this whole story exists to remove.
    """
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    try:
        with pytest.raises(ConfigurationError) as caught:
            await _get_with(
                http_server, session=session, serialization=str)
    finally:
        await session.close()

    assert caught.value.code == 'CONFIG'
    assert 'json_serialize' in str(caught.value)
    assert http_server.requests == []


async def test_a_tracer_cannot_be_combined_with_a_supplied_session(
    http_server: RecordingHTTPServer,
) -> None:
    """The same defect class as the serialiser, on the same key.

    ``trace_configs`` is a ``ClientSession`` constructor argument, so a
    caller who supplies the session leaves this library nowhere to attach
    one. The value used to be accepted, dropped, and then read back into
    ``request_tracer`` -- which came out empty, since the collector it
    read was never wired to anything.
    """
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    try:
        with pytest.raises(ConfigurationError) as caught:
            await _get_with(
                http_server, session=session, trace_config=[request_tracer()])
    finally:
        await session.close()

    assert caught.value.code == 'CONFIG'
    assert 'trace_configs' in str(caught.value)
    assert http_server.requests == []


async def test_a_supplied_session_without_a_tracer_is_accepted(
    http_server: RecordingHTTPServer,
) -> None:
    """The refusal is on the *pair*, not on supplying a session at all.

    A guard written one condition too wide would reject every caller
    session, since ``trace_config`` has a default this library supplies
    for itself.
    """
    http_server.respond('/body', body=b'ok')
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    try:
        envelope = await _get_with(http_server, session=session)
    finally:
        await session.close()

    assert envelope['ok'] is True
    assert envelope['request_tracer'] == []


async def test_the_workaround_the_refusal_names_actually_receives_the_event(
    http_server: RecordingHTTPServer,
) -> None:
    """The refusal's advice must not lead to the hollow output it prevents.

    ``validated_trace_config`` refuses ``trace_config`` + ``session`` and
    tells the caller to "pass trace_configs to your own ClientSession".
    On that route this library derived no collectors from the supplied
    session at all, so :func:`record_redirect` wrote nowhere: measured on
    a two-hop chain the library provably followed, the caller's own
    collector reported ``is_redirect`` False and ``on_request_redirect``
    absent -- the exact hollowness the refusal exists to prevent, reached
    by following the refusal.
    """
    http_server.respond('/a', status=302, headers={'Location': '/b'})
    http_server.respond('/b', status=302, headers={'Location': '/c'})
    http_server.respond('/c', body=b'arrived')
    tracer = request_tracer()
    session = aiohttp.ClientSession(
        trace_configs=[tracer],
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    try:
        envelope = await _get_with(http_server, '/a', session=session)
    finally:
        await session.close()

    assert envelope['ok'] is True
    assert envelope['text'] == 'arrived'
    assert tracer.results_collector['is_redirect'] is True
    assert tracer.results_collector['on_request_redirect'] >= 0
    # The envelope's own contract is unchanged: a session this library
    # did not create reports no tracer, because that collector outlives
    # the call and every envelope the session served would alias it.
    assert envelope['request_tracer'] == []


async def test_a_foreign_tracer_on_a_supplied_session_is_skipped_not_fatal(
    http_server: RecordingHTTPServer,
) -> None:
    """A caller's session may carry tracers this library cannot write to.

    ``aiohttp.TraceConfig`` has no ``results_collector`` of its own --
    ``request_tracer`` attaches one -- so a caller who added a plain
    tracer for their own callbacks must not have their call fail on it.
    It is skipped; the usable one beside it is still written.
    """
    http_server.respond('/a', status=302, headers={'Location': '/b'})
    http_server.respond('/b', body=b'arrived')
    foreign = aiohttp.TraceConfig()
    ours = request_tracer()
    session = aiohttp.ClientSession(
        trace_configs=[foreign, ours],
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    try:
        envelope = await _get_with(http_server, '/a', session=session)
    finally:
        await session.close()

    assert envelope['ok'] is True
    assert envelope['text'] == 'arrived'
    assert not hasattr(foreign, 'results_collector')
    assert ours.results_collector['is_redirect'] is True


# --- R14: the redirect keys and the deadline are validated at construction -


@pytest.mark.parametrize(
    'info',
    [
        pytest.param({'max_redirects': 'three'}, id='max-redirects-str'),
        pytest.param({'max_redirects': -1}, id='max-redirects-negative'),
        pytest.param({'max_redirects': True}, id='max-redirects-bool'),
        pytest.param({'max_redirects': None}, id='max-redirects-none'),
        pytest.param({'allow_redirects': 'no'}, id='allow-redirects-str'),
        pytest.param({'allow_redirects': 1}, id='allow-redirects-int'),
        pytest.param({'allow_redirects': None}, id='allow-redirects-none'),
        pytest.param({'allowed_schemes': 'https'}, id='schemes-bare-str'),
        pytest.param({'allowed_schemes': frozenset()}, id='schemes-empty'),
        pytest.param({'allowed_schemes': [443]}, id='schemes-not-str'),
        pytest.param({'allowed_schemes': None}, id='schemes-none'),
        pytest.param({'timeout': 'ten'}, id='timeout-str'),
        pytest.param({'timeout': True}, id='timeout-bool'),
        pytest.param({'timeout': None}, id='timeout-none'),
        pytest.param({'timeout': 0}, id='timeout-zero'),
        pytest.param({'timeout': -5}, id='timeout-negative'),
    ],
)
async def test_a_redirect_key_that_cannot_form_a_call_is_refused(
    http_server: RecordingHTTPServer,
    info: dict[str, Any],
) -> None:
    """All shipped as bare ``.get()`` beside two validated siblings.

    Each row is a measured failure, not a hypothetical.
    ``max_redirects='three'`` reached the comparison in the transport and
    raised ``TypeError: '>=' not supported between instances of 'int' and
    'str'`` out of ``request()`` un-enveloped -- and only on the calls
    whose endpoint happened to redirect, so the same configuration passed
    or crashed depending on the remote side. ``allowed_schemes='https'``
    became ``{'h', 't', 'p', 's'}``, an allowlist admitting no real
    scheme. ``allow_redirects='no'`` followed redirects.

    ``timeout`` was the last of them, and the owned redirect loop gave it
    a crash site the transport had absorbed: the chain deadline is
    ``time.monotonic() + timeout.total``, so ``timeout='ten'`` raised
    ``TypeError: unsupported operand type(s) for +: 'float' and 'str'``
    un-enveloped, and ``timeout=True`` was silently taken as a
    one-second deadline and answered ``ok=True``.

    The refusal is at construction, so nothing is dispatched: the
    recorded request count is the assertion that the rejection is early
    rather than a nicer message for the same late failure.
    """
    http_server.respond('/body', status=302, headers={'Location': '/end'})
    http_server.respond('/end', body=b'arrived')

    with pytest.raises(ConfigurationError) as caught:
        await _get_with(http_server, **info)

    key, = info
    assert caught.value.code == 'CONFIG'
    assert key in str(caught.value)
    assert http_server.requests == []


@pytest.mark.parametrize(
    'timeout',
    [
        pytest.param(5, id='int'),
        pytest.param(0.05, id='sub-second-float'),
    ],
)
async def test_a_numeric_deadline_is_accepted_int_or_float(
    http_server: RecordingHTTPServer,
    timeout: Any,
) -> None:
    """The pair to the rejections: a guard too wide would refuse both.

    A sub-second ``float`` is the row that matters -- rejecting anything
    but ``int`` would break every deadline this suite's own timeout tests
    are written with.
    """
    http_server.respond('/body', body=b'ok')

    envelope = await _get_with(http_server, timeout=timeout)

    assert envelope['ok'] is True
    assert envelope['text'] == 'ok'


@pytest.mark.parametrize(
    'value',
    [
        pytest.param('tracer', id='bare-str'),
        pytest.param(aiohttp.TraceConfig(), id='bare-trace-config'),
        pytest.param([aiohttp.TraceConfig()], id='no-results-collector'),
        pytest.param([object()], id='not-a-trace-config'),
        pytest.param(7, id='not-a-collection'),
    ],
)
async def test_a_trace_config_that_cannot_be_read_is_refused(
    http_server: RecordingHTTPServer,
    value: Any,
) -> None:
    """The last ``protocol_info`` key that shipped unvalidated.

    Every row is a measured escape from ``request()`` **un-enveloped**,
    not a hypothetical: ``[aiohttp.TraceConfig()]`` and ``'tracer'`` both
    raised ``AttributeError: ... has no attribute 'results_collector'``
    out of the constructor's list comprehension, and a bare
    ``aiohttp.TraceConfig()`` raised ``TypeError: not iterable``. The
    first is the likeliest mistake of the three -- a plain
    ``TraceConfig`` looks exactly like what this key wants, and the
    ``results_collector`` this library reads is attached by
    ``request_tracer`` rather than by ``aiohttp``.

    The refusal is at construction, so the recorded request count is the
    assertion that nothing was dispatched.
    """
    http_server.respond('/body', body=b'ok')

    with pytest.raises(ConfigurationError) as caught:
        await _get_with(http_server, trace_config=value)

    assert caught.value.code == 'CONFIG'
    assert 'trace_config' in str(caught.value)
    assert http_server.requests == []


async def test_an_empty_trace_config_list_is_tracing_off_not_a_rejection(
    http_server: RecordingHTTPServer,
) -> None:
    """The pair: a guard one condition too wide would refuse "no tracing".

    Unlike ``allowed_schemes``, an empty ``trace_config`` refuses
    nothing -- it asks for no tracing, which is a legitimate thing to
    ask for and the envelope reports it as ``[]``.
    """
    http_server.respond('/body', body=b'ok')

    envelope = await _get_with(http_server, trace_config=[])

    assert envelope['ok'] is True
    assert envelope['request_tracer'] == []


async def test_zero_redirects_is_a_bound_not_a_rejection(
    http_server: RecordingHTTPServer,
) -> None:
    """``max_redirects=0`` is a legal bound, not a rejected sentinel.

    Unlike the response cap, there is nothing unsafe about the zero here,
    so it is accepted -- and the first redirect it meets is where the
    chain gives up.
    """
    http_server.respond('/start', status=302, headers={'Location': '/end'})
    http_server.respond('/end', body=b'never reached')

    envelope = await _get_with(http_server, '/start', max_redirects=0)

    assert envelope['ok'] is False
    assert envelope['status_code'] == 302
    assert [r.path for r in http_server.requests] == ['/start']


async def test_zero_redirects_and_no_redirects_are_different_asks(
    http_server: RecordingHTTPServer,
) -> None:
    """``max_redirects=0`` does not hand the caller the redirect response.

    ``validated_max_redirects``'s docstring used to claim zero was "the
    natural spelling of a call that wants the redirect response itself",
    which the code does not do: zero is a bound the chain *overran*, so
    the loop raises where it would have followed and the 302's body is
    never read. The key that does what the prose described is
    ``allow_redirects=False``. Both are driven here against one server so
    the difference is a measurement rather than a claim.
    """
    http_server.respond(
        '/start', status=302, body=b'moved-body',
        headers={'Location': '/end'})
    http_server.respond('/end', body=b'never reached')

    bounded = await _get_with(http_server, '/start', max_redirects=0)
    unfollowed = await _get_with(
        http_server, '/start', allow_redirects=False)

    assert (bounded['ok'], bounded['status_code'], bounded['text']) == (
        False, 302, '')
    assert bounded['error'] is not None
    assert bounded['error']['code'] == 'TRANSPORT'
    assert (
        unfollowed['ok'], unfollowed['status_code'], unfollowed['text']
    ) == (True, 302, 'moved-body')


async def test_an_uppercased_scheme_allowlist_admits_the_scheme_it_names(
    http_server: RecordingHTTPServer,
) -> None:
    """The per-hop check compares a lower-cased scheme, so the set is one.

    Left un-normalised, ``allowed_schemes={'HTTP'}`` refused every hop it
    was written to permit -- a rejection that reads as the guardrail
    working and is the caller's own allowlist turned inside out.
    """
    http_server.respond('/start', status=302, headers={'Location': '/end'})
    http_server.respond('/end', body=b'arrived')

    envelope = await _get_with(
        http_server, '/start', allowed_schemes={'HTTP'})

    assert envelope['ok'] is True
    assert envelope['text'] == 'arrived'

"""Tests for the response envelope, its clocks and its redaction (Step 5).

Home of Part B's invariants **E1-E11**, which are the contract every later
protocol story is written against: one key set, one success predicate, an
error message that is never empty, a body that survives a failure status,
timestamps in UTC, a latency that cannot go backwards, and no credential
anywhere in what the caller logs or stores.

Covers R8 (all but AC2, which S9 owns, and AC12, the README), R9 in full,
and R10's logger and redaction criteria. Each test names the invariant or
acceptance criterion it proves.
"""

import asyncio
import json
import logging
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from aiohttp import BasicAuth

from failsafe import RetriesExhausted

import pytest

from async_gateway import async_gateway as entrypoint
from async_gateway.async_gateway import request
from async_gateway.helpers.common import date_helper
from async_gateway.helpers.common.date_helper import (
    elapsed_since,
    monotonic_now,
    utc_now_iso,
)
from async_gateway.logic.http_client import HttpRequest, transport_error_for
from async_gateway.utils.envelope import (
    GatewayResponse,
    finalise_error,
    finalise_ok,
    new_envelope,
)
from async_gateway.utils.exceptions import (
    AsyncGatewayError,
    CircuitOpenError,
    ConnectError,
    GatewayTimeoutError,
    HttpStatusError,
    SerializationError,
)
from async_gateway.utils.redaction import (
    PAYLOAD_REDACTION_DEPTH,
    REDACTED,
    normalise_param_names,
    redact_cookies,
    redact_headers,
    redact_payload,
    redact_text,
    redact_url,
    redact_value,
)

from tests.fixtures.http_server import RecordingHTTPServer
from tests.fixtures.protocol_transports import (
    contract_call,
    install_transport,
)

# The envelope's public key set. Named here so that adding a key is a
# deliberate edit to this list rather than something a test silently
# accepts. S9 parametrises the same set across all five protocols.
EXPECTED_KEYS = frozenset({
    'ok',
    'status_code',
    'protocol',
    'url',
    'request_time',
    'latency',
    'payload',
    'text',
    'json',
    'headers',
    'cookies',
    'error',
    'protocol_details',
    'request_tracer',
    'pre_processor_response',
    'post_processor_response',
})

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / 'async_gateway'

JSON_HEADERS = {'Content-Type': 'application/json'}


def package_sources() -> Iterator[tuple[str, str]]:
    """Yield every module in the installed package with its source.

    Returns:
        Pairs of package-relative POSIX path and file contents, so a test
        can make the same assertion a ``grep -rn`` over the package makes.
    """
    for path in sorted(PACKAGE_ROOT.rglob('*.py')):
        yield (
            path.relative_to(PACKAGE_ROOT).as_posix(),
            path.read_text(encoding='utf-8'),
        )


def files_containing(pattern: str) -> list[str]:
    """Return the package modules whose source matches ``pattern``.

    Args:
        pattern: A regular expression applied to each module's source.

    Returns:
        The package-relative paths that match, sorted.
    """
    expression = re.compile(pattern)
    return [
        name for name, source in package_sources()
        if expression.search(source)
    ]


def closed_port() -> int:
    """Return a TCP port on the loopback interface with nothing behind it.

    Binding and immediately releasing a port is the cheapest way to get one
    that is free, so a connection to it is refused rather than answered.

    Returns:
        A port number no listener is bound to.
    """
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return int(probe.getsockname()[1])


def contains_self(value: Any, envelope: GatewayResponse) -> bool:
    """Report whether ``envelope`` appears anywhere inside ``value``.

    Args:
        value: The value to search, descended recursively.
        envelope: The envelope that must not be reachable from itself.

    Returns:
        True if the envelope is ``value`` or is nested inside it.
    """
    if value is envelope:
        return True
    if isinstance(value, dict):
        return any(
            contains_self(item, envelope) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(contains_self(item, envelope) for item in value)
    return False


async def http_call(
    url: str,
    *,
    data: Any = None,
    request_type: str = 'GET',
    headers: dict[str, str] | None = None,
) -> GatewayResponse:
    """Drive one HTTP request through the public entry point.

    Args:
        url: The absolute URL to call.
        data: The request payload, or None for an empty one.
        request_type: The HTTP verb to use.
        headers: Request headers, or None for a JSON content type.

    Returns:
        The envelope ``request()`` returned.
    """
    return await request(
        url,
        data={} if data is None else data,
        protocol='HTTP',
        protocol_info={
            'request_type': request_type,
            'headers': dict(headers or JSON_HEADERS),
        },
    )


def render_record(record: logging.LogRecord) -> str:
    """Render every channel one log record can carry a secret through.

    ``log_failure`` renders the traceback itself and carries it in
    ``extra``, so the chained exception's own text reaches the log
    through that field as well as through the message. A redaction
    assertion that reads one attribute proves nothing about the others,
    and the formatter is still run in case a future change puts
    ``exc_info`` back.

    Args:
        record: The captured record to flatten.

    Returns:
        The formatted message and any ``exc_info`` concatenated with the
        record's whole attribute dictionary, so a substring check over
        the result covers message, ``extra`` and traceback alike.
    """
    formatted = logging.Formatter('%(message)s').format(record)
    return f'{formatted} {record.__dict__}'


@pytest.fixture
async def ok_envelope(
    http_server: RecordingHTTPServer,
) -> GatewayResponse:
    """Return the envelope from one successful HTTP call.

    Returns:
        A finalised success envelope from the loopback server.
    """
    http_server.respond(
        '/ok', status=200, body=b'{"value": 1}', headers=JSON_HEADERS)
    return await http_call(http_server.url_for('/ok'))


@pytest.fixture
async def failed_envelope(
    http_server: RecordingHTTPServer,
) -> GatewayResponse:
    """Return the envelope from one HTTP call that failed remotely.

    Returns:
        A finalised failure envelope carrying a 404 and its JSON body.
    """
    http_server.respond(
        '/missing',
        status=404,
        body=b'{"error": "no such thing"}',
        headers=JSON_HEADERS,
    )
    return await http_call(http_server.url_for('/missing'))


# --- E1: one key set -------------------------------------------------------


def test_e1_the_skeleton_carries_every_documented_key() -> None:
    """A fresh envelope already has the whole key set (E1)."""
    envelope = new_envelope(url='http://host/p', protocol='HTTP', payload={})

    assert set(envelope) == EXPECTED_KEYS


async def test_e1_success_and_failure_return_the_same_key_set(
    ok_envelope: GatewayResponse,
    failed_envelope: GatewayResponse,
) -> None:
    """One shape for both paths, so a caller writes one handler (E1).

    S9 widens the same assertion to five protocols; HTTP is the protocol
    Step 5 rewrites, so it is the one that can be asserted here.
    """
    assert set(ok_envelope) == EXPECTED_KEYS
    assert set(failed_envelope) == EXPECTED_KEYS


async def test_e1_a_transport_failure_returns_the_same_key_set() -> None:
    """A failure with no response at all still fills every key (E1)."""
    envelope = await http_call(f'http://127.0.0.1:{closed_port()}/x')

    assert set(envelope) == EXPECTED_KEYS
    assert envelope['headers'] == {}
    assert envelope['cookies'] == {}


# --- R8-AC2: one key set, five protocols, both paths -----------------------

# The five contract rows, shared by the success and the failure test below
# so that a protocol's marker was one line to remove rather than two.
#
# **The list is now unmarked, and that is the criterion, not a tidy-up.**
# Three rows began as `xfail(strict=True)` because their clients did not
# satisfy the contract yet -- which is why these were written before the
# protocol work rather than after it. `strict` is what made a marker a
# ratchet instead of a note: the moment a protocol story fixed its client,
# both of that protocol's rows passed unexpectedly, the suite failed, and
# the marker had to come off in the story that earned it. S10 took FTP,
# S11 took SFTP, and S22 -- writing `logic/soap_client.py` from nothing --
# took the last one. All five protocols satisfy E1 and E11 here.
CONTRACT_ROWS = [
    pytest.param('HTTP', id='HTTP'),
    pytest.param('HTTPS', id='HTTPS'),
    pytest.param('FTP', id='FTP'),
    pytest.param('SFTP', id='SFTP'),
    pytest.param('SOAP', id='SOAP'),
]


@pytest.mark.parametrize('protocol', CONTRACT_ROWS)
async def test_r8_ac2_every_protocol_returns_one_key_set_on_success(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    """One shape for a success, whichever protocol produced it (R8-AC2).

    The criterion a caller actually feels: one handler, written once,
    works for every protocol this library dispatches. Without it each
    protocol is free to invent its own success shape, and the uniform
    envelope this release exists to deliver is uniform only for the
    protocol whoever last touched the code happened to run.

    No socket is opened. Each row drives ``request()`` with only its own
    transport seam doubled, so everything between that seam and the
    caller -- the protocol client's envelope mapping, the entry point's
    single conversion point -- is the real code under test.

    The key set is asserted first because it is the criterion verbatim,
    but it cannot be the only assertion. A client that swallows its own
    failure and returns the skeleton it was handed satisfies
    ``set(result.keys()) == EXPECTED_KEYS`` exactly, while having reached
    neither finaliser -- which is what both currently-broken clients do,
    and what would let their rows pass this test while claiming success
    for a call that failed. So ``ok``/``error`` are asserted too: E1's
    other half is that exactly one of ``finalise_ok`` and
    ``finalise_error`` closes every envelope, and "the success path"
    means nothing if the call never took it.
    """
    install_transport(monkeypatch, protocol, succeeds=True)

    result = await request(**contract_call(protocol))

    assert set(result.keys()) == EXPECTED_KEYS
    assert result['ok'] is True
    assert result['error'] is None


@pytest.mark.parametrize('protocol', CONTRACT_ROWS)
async def test_r8_ac2_every_protocol_returns_one_key_set_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    """The same shape for a failure, whichever protocol failed (R8-AC2).

    The half that rots first. A success path is exercised by every
    example anyone writes, so it stays honest on its own; a refused
    connection over SFTP is exercised by nobody until it happens in
    production, which is where a protocol that reports its failures in a
    shape of its own devising is discovered.

    Every row's transport refuses the connection, so each protocol
    reaches its own failure path rather than a shared pre-dispatch
    rejection -- and reaches it without a socket, because the refusal is
    the double's.

    ``error is not None`` carries the same weight here as ``ok is True``
    does in the success test above, and for the same reason: an envelope
    both broken clients return has the whole key set and a null ``error``
    on a call that failed, so a caller reading ``error`` to find out what
    went wrong learns nothing. The key set alone would accept that.
    """
    install_transport(monkeypatch, protocol, succeeds=False)

    result = await request(**contract_call(protocol))

    assert set(result.keys()) == EXPECTED_KEYS
    assert result['ok'] is False
    assert result['error'] is not None


# --- E2: ok is False exactly when error is set -----------------------------


async def test_e2_success_is_ok_with_no_error(
    ok_envelope: GatewayResponse,
) -> None:
    """``ok`` is True only when nothing failed (E2)."""
    assert ok_envelope['ok'] is True
    assert ok_envelope['error'] is None


@pytest.mark.parametrize(
    'exc',
    [
        pytest.param(HttpStatusError('not found', 404), id='HTTP_STATUS'),
        pytest.param(GatewayTimeoutError('timed out'), id='TIMEOUT'),
        pytest.param(ConnectError('refused'), id='CONNECT'),
        pytest.param(CircuitOpenError('open'), id='CIRCUIT_OPEN'),
        pytest.param(SerializationError('unparseable'), id='SERIALIZATION'),
    ],
)
def test_e2_every_failure_mode_is_not_ok_and_carries_an_error(
    exc: AsyncGatewayError,
) -> None:
    """``ok is False`` if and only if ``error`` is populated (E2)."""
    envelope = finalise_error(
        new_envelope(url='http://host/p', protocol='HTTP', payload={}),
        exc,
        started=monotonic_now(),
    )

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == exc.code
    assert envelope['error']['type'] == type(exc).__name__
    assert envelope['status_code'] == exc.status_code


# --- E3: the message is never empty ----------------------------------------


@pytest.mark.parametrize(
    'exc',
    [
        pytest.param(HttpStatusError('not found', 404), id='with-message'),
        pytest.param(ConnectError(''), id='empty-message'),
        pytest.param(GatewayTimeoutError(''), id='empty-timeout'),
    ],
)
def test_e3_the_error_message_is_never_empty(
    exc: AsyncGatewayError,
) -> None:
    """A blank message reads as success, so there is never one (E3).

    The fallback is the exception's own class name, which is the worst case
    the design permits -- and is still strictly more than ``''``.
    """
    envelope = finalise_error(
        new_envelope(url='http://host/p', protocol='HTTP', payload={}),
        exc,
        started=monotonic_now(),
    )

    assert envelope['error'] is not None
    assert envelope['error']['message']


@pytest.mark.parametrize(
    'kind',
    ['remote-status', 'connect-refused'],
)
async def test_e3_a_live_failure_reports_something_readable(
    kind: str,
    http_server: RecordingHTTPServer,
) -> None:
    """The message survives the real code path, not just a unit call (E3)."""
    if kind == 'remote-status':
        http_server.respond('/boom', status=500, body=b'{}')
        envelope = await http_call(http_server.url_for('/boom'))
    else:
        envelope = await http_call(f'http://127.0.0.1:{closed_port()}/x')

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['message'].strip()


# --- E4: never self-referential --------------------------------------------


async def test_e4_the_envelope_is_never_reachable_from_itself(
    ok_envelope: GatewayResponse,
    failed_envelope: GatewayResponse,
) -> None:
    """No key, at any depth, is the envelope itself (E4).

    The shape this replaces aliased the whole response under one of its own
    keys, so the natural success check read the container and was truthy on
    every path.
    """
    for envelope in (ok_envelope, failed_envelope):
        for key, value in envelope.items():
            assert value is not envelope, key
            assert not contains_self(value, envelope), key


# --- E5 / E6: serialisable, and `text` is always text ----------------------


async def test_e5_every_path_is_json_serialisable(
    ok_envelope: GatewayResponse,
    failed_envelope: GatewayResponse,
) -> None:
    """``json.dumps`` succeeds on success and on failure alike (E5)."""
    transport = await http_call(f'http://127.0.0.1:{closed_port()}/x')

    for envelope in (ok_envelope, failed_envelope, transport):
        assert json.loads(json.dumps(envelope)) is not None


async def test_e6_text_is_always_a_string_never_an_exception(
    ok_envelope: GatewayResponse,
    failed_envelope: GatewayResponse,
) -> None:
    """``text`` is decoded body text or ``''``, never an exception (E6)."""
    transport = await http_call(f'http://127.0.0.1:{closed_port()}/x')

    for envelope in (ok_envelope, failed_envelope, transport):
        assert isinstance(envelope['text'], str)
    assert transport['text'] == ''


# --- E7: the fabricated status is gone -------------------------------------


@pytest.mark.parametrize(
    'exc',
    [
        pytest.param(GatewayTimeoutError('t'), id='TIMEOUT'),
        pytest.param(ConnectError('c'), id='CONNECT'),
        pytest.param(CircuitOpenError('o'), id='CIRCUIT_OPEN'),
        pytest.param(HttpStatusError('h', 404), id='HTTP_STATUS'),
    ],
)
def test_e7_no_failure_reports_the_fabricated_status(
    exc: AsyncGatewayError,
) -> None:
    """Every failure carries a status a consumer already knows (E7)."""
    envelope = finalise_error(
        new_envelope(url='http://host/p', protocol='HTTP', payload={}),
        exc,
        started=monotonic_now(),
    )

    assert envelope['status_code'] != 999
    assert 100 <= envelope['status_code'] <= 599


def test_e7_the_fabricated_status_appears_nowhere_in_the_package() -> None:
    """The whole package is free of the invented status code (E7)."""
    assert files_containing(r'999') == []


@pytest.mark.parametrize(
    'pattern, criterion',
    [
        pytest.param(r'999', 'E7', id='fabricated-status'),
        pytest.param(r"'tat'", 'R8-AC5', id='tat'),
        pytest.param(r'except Exception', 'R10-AC2', id='blanket-except'),
        pytest.param(r'time\.time\(\)', 'R9-AC4', id='wall-clock-duration'),
    ],
)
def test_the_banned_patterns_appear_nowhere_in_the_package(
    pattern: str,
    criterion: str,
) -> None:
    """No banned pattern appears in any module of the package.

    This began as a *containment* check: Step 5 could not delete the FTP
    and SFTP occurrences -- those files belonged to later stories -- so it
    guaranteed only that the residue was exactly those two files rather
    than a growing set. Both have now been rewritten (S10, S11, S12), the
    residue is empty, and the allowance goes with it. A subset assertion
    against an empty set is a ban, but an *implicit* one: it would have
    kept reading as an allowance to any reader, and it left ``'tat'``,
    ``except Exception`` and ``time.time()`` reintroducible into either
    client with the suite still green. The release-level criterion was
    always zero; this is where it is stated as zero.
    """
    assert files_containing(pattern) == [], criterion


def test_the_transport_boundary_no_longer_invents_a_response(
) -> None:
    """FI-7: the helper's fresh response dict is gone from the package."""
    assert files_containing(r"kwargs\.get\('response'") == []


# --- E8 / R9: honest timestamps, monotonic durations -----------------------


def test_r9_request_time_is_utc_iso8601_with_an_explicit_offset() -> None:
    """The timestamp parses and says which zone it is in (R9-AC1)."""
    envelope = new_envelope(url='http://host/p', protocol='HTTP', payload={})

    parsed = datetime.fromisoformat(envelope['request_time'])

    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_r9_utc_now_iso_is_the_one_source_of_that_timestamp() -> None:
    """The helper itself produces an offset-aware UTC value (R9-AC3)."""
    parsed = datetime.fromisoformat(utc_now_iso())

    assert parsed.utcoffset().total_seconds() == 0


def test_r9_no_hardcoded_timezone_survives_anywhere() -> None:
    """The import-time regional timezone binding is gone (R9-AC2, AC3)."""
    assert files_containing(r'Asia/Kolkata|TIMEZONE|pytz') == []


def test_r9_the_dependency_set_no_longer_declares_the_timezone_library(
) -> None:
    """The declaration goes in the same change as the import (R6-AC2).

    Separating them breaks the clean-venv job in one direction or the
    other: an undeclared import fails to resolve, and a declared-but-unused
    dependency ships weight nothing needs.
    """
    manifest = (
        PACKAGE_ROOT.parent / 'pyproject.toml'
    ).read_text(encoding='utf-8')

    assert 'pytz' not in manifest


def test_e8_latency_is_never_negative_when_the_clock_steps_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clock reading that goes backwards still yields ``latency >= 0``.

    Durations are measured monotonically precisely so a wall-clock step
    cannot produce a negative value on the metric users chart. The clamp
    keeps that true even for a substituted clock (E8, R9-AC4).
    """
    readings = iter([100.0, 40.0])
    monkeypatch.setattr(
        date_helper.time, 'monotonic', lambda: next(readings))

    started = date_helper.monotonic_now()
    envelope = finalise_ok(
        new_envelope(url='http://host/p', protocol='HTTP', payload={}),
        status_code=200,
        started=started,
    )

    assert envelope['latency'] >= 0


def test_e8_elapsed_since_a_future_reference_point_is_zero() -> None:
    """The same clamp, asserted at the helper it lives in (E8)."""
    assert elapsed_since(monotonic_now() + 60) == 0.0


async def test_e8_a_real_call_reports_a_non_negative_latency(
    ok_envelope: GatewayResponse,
) -> None:
    """Latency is a measured duration, not a placeholder (R9-AC4)."""
    assert ok_envelope['latency'] >= 0
    assert isinstance(ok_envelope['latency'], float)


# --- E9: no credential in what the caller keeps ----------------------------


async def test_e9_no_credential_reaches_the_envelope(
    http_server: RecordingHTTPServer,
) -> None:
    """Header, query parameter, payload and cookie are all masked (E9).

    One request carrying all four kinds of secret at once, because the
    invariant is about the envelope as a whole and a per-field test would
    not catch a secret that leaked through a fifth field.
    """
    http_server.respond(
        '/secrets',
        status=200,
        body=b'{"value": 1}',
        headers={
            'Content-Type': 'application/json',
            'Set-Cookie': 'session=COOKIESECRET; Path=/',
        },
    )

    envelope = await http_call(
        http_server.url_for('/secrets') + '?api_key=QUERYSECRET',
        data={'password': 'PAYLOADSECRET', 'kept': 'visible'},
        request_type='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer HEADERSECRET',
        },
    )

    rendered = repr(envelope)
    for secret in (
        'QUERYSECRET', 'PAYLOADSECRET', 'HEADERSECRET', 'COOKIESECRET',
    ):
        assert secret not in rendered, secret
    assert envelope['headers']['Set-Cookie'] == REDACTED
    assert envelope['cookies']['session'] == REDACTED
    assert envelope['payload']['password'] == REDACTED
    assert envelope['payload']['kept'] == 'visible'
    assert REDACTED in envelope['url']


def test_e9_there_is_no_opt_out_of_redaction() -> None:
    """No switch exists whose only function is to unmask credentials.

    A ``redact=False`` parameter would put credentials back into the
    envelope on request, which is not a feature this release ships.
    """
    assert files_containing(r'redact\s*[=:]\s*False') == []


def test_redact_url_strips_userinfo_and_masks_sensitive_parameters(
) -> None:
    """The URL redactor covers both halves E9 names (R8, redaction)."""
    redacted = redact_url('https://user:pw@host/p?token=abc&page=2')

    assert 'user' not in redacted
    assert 'pw' not in redacted
    assert 'abc' not in redacted
    assert 'page=2' in redacted
    assert redacted.startswith('https://host/p?')


def test_redact_text_masks_a_url_embedded_anywhere_in_a_string() -> None:
    """Foreign text is a sentence with a URL in it, not a bare URL.

    ``redact_url`` needs the whole string to be a URL, so a whole-string
    test sees nothing to mask in
    ``'RetriesExhausted: https://...?api_key=...'`` -- which is exactly
    the shape ``unwrap_cause`` returns.
    """
    redacted = redact_text(
        'RetriesExhausted: https://host/p?api_key=SECRETVALUE happened')

    assert 'SECRETVALUE' not in redacted
    assert redacted.startswith('RetriesExhausted: ')
    assert redacted.endswith(' happened')
    assert REDACTED in redacted


def test_redact_text_leaves_a_string_with_no_url_alone() -> None:
    """It is applied to every message, so it must be a no-op on most."""
    assert redact_text('connection reset by peer') == (
        'connection reset by peer')


def test_redact_text_masks_a_sensitive_pair_that_carries_no_scheme(
) -> None:
    """The seam of S8-H1: masking may not depend on URL-ness at all.

    A caller who writes ``host/p?api_key=...`` hands this function a
    string with no ``scheme://`` anywhere in it, and an embedded-URL pass
    matches nothing there. Its three consumers therefore carried the
    secret in the clear: ``error['message']``, ``error['cause']`` and the
    log record's ``extra['traceback']``, all of which are masked by
    ``redact_text`` alone. The end-to-end guard further down proves the
    symptom is gone; this one names the seam, so reinstating a pass that
    asks whether the text is a URL fails here first.

    The insensitive pair is asserted *present* in the same breath. It is
    what separates this rule from a blanket that blanks every query
    string, and it is why the accepted cost is over-masking prose rather
    than losing the diagnostics the envelope exists for.
    """
    masked = redact_text('host/p?api_key=SECRETVALUE&page=2')

    assert masked == f'host/p?api_key={REDACTED}&page=2'


def test_redact_text_masks_a_scheme_less_url_exactly_as_a_schemed_one(
) -> None:
    """Symmetry is the contract; the absence of one secret is not.

    A predicate deciding "is this a URL?" has been narrowed three times on
    this module -- scheme *and* netloc, then scheme alone -- and each
    narrowing still left a shape it classified wrongly. Each also passed a
    test that asserted only that some particular secret was gone, which is
    a bar a fourth predicate could clear while disagreeing about the next
    shape along. Asserting that the two strings mask *identically* leaves
    no such room: the only way to satisfy it is to stop asking.
    """
    schemeless = redact_text('host/p?api_key=SECRETVALUE')
    schemed = redact_text('https://host/p?api_key=SECRETVALUE')

    assert schemeless == f'host/p?api_key={REDACTED}'
    assert schemed == f'https://{schemeless}'


def test_redact_text_applies_caller_supplied_names_without_a_scheme_too(
) -> None:
    """The extension point cannot be narrower than the built-in set.

    ``redact_query_params`` is the only way a caller names a secret this
    library could not guess, and on the ``cause`` and ``traceback``
    surfaces it is honoured by ``redact_text`` alone. A scheme-less URL
    that masked ``api_key`` but not the caller's own ``session_id`` would
    leak precisely the name the caller took the trouble to declare.
    """
    masked = redact_text(
        'host/p?session_id=SESSIONSECRET&api_key=DEFAULTSECRET',
        extra_params=('session_id',))

    assert masked == f'host/p?session_id={REDACTED}&api_key={REDACTED}'


def test_redact_text_is_idempotent_on_a_string_it_has_already_masked(
) -> None:
    """``redact_value`` composes maskers, so passes must not compound.

    A rule that treated the sentinel as a fresh value to rewrite would
    make the *number* of passes observable, and the number of passes is an
    implementation detail no caller of the envelope can see.
    """
    once = redact_text('host/p?api_key=SECRETVALUE&page=2')

    assert once == f'host/p?api_key={REDACTED}&page=2'
    assert redact_text(once) == once


def test_redact_text_runs_the_pair_pass_before_the_embedded_url_pass(
) -> None:
    """Order is load-bearing, so it is pinned rather than left to reading.

    Both passes mask the same secret here, so the secret's absence proves
    nothing about which ran first. The caller's own rendering does.
    ``redact_url`` rebuilds a query only when its own masking changed
    something, so with the pair pass first it finds nothing left to change
    and every untouched character survives. Run the other way round,
    ``urlsplit`` normalises the scheme and ``urlencode`` re-encodes the
    remainder -- ``HTTP`` becomes ``http`` and ``a%20b`` becomes ``a+b``
    -- rewriting text this library did not author and was asked only to
    mask.
    """
    masked = redact_text('HTTP://H/p?api_key=SECRETVALUE&x=a%20b')

    assert masked == f'HTTP://H/p?api_key={REDACTED}&x=a%20b'


def test_redact_text_masks_a_sensitive_pair_in_prose_that_is_not_a_url(
) -> None:
    """The ratified cost of the rule, asserted rather than assumed.

    Matching on the ``?``/``&`` structure alone cannot tell a URL from a
    sentence that merely contains one, so a sentence like this loses its
    ``hunter2`` as well. That is the accepted trade: the alternative is a
    predicate deciding where URL-ness begins, and three of those have now
    failed open here. Pinning the cost means the next reader meets it as a
    decision with a reason behind it rather than as a surprise.
    """
    masked = redact_text('try the form at /login?password=hunter2 first')

    assert masked == f'try the form at /login?password={REDACTED} first'


def test_redact_text_masks_a_percent_encoded_sensitive_name() -> None:
    """The same seam as S8-H1, one layer down: an encoded name.

    ``api%5Fkey`` is ``api_key`` to every server that will parse it, and
    a caller who wrote it that way has named exactly the secret this
    module already knows. Comparing the raw run of characters against the
    sensitive set sees two different names and masks neither, so the
    value reached ``error['message']``, ``error['cause']`` and
    ``extra['traceback']`` in the clear -- the three surfaces
    ``redact_text`` masks alone. Decoding the name before the lookup
    closes that without reinstating any predicate about the surrounding
    text: it normalises a key, and asks nothing about what the key is
    embedded in.

    The malformed ``%zz`` rides along because the docstring claims
    decoding cannot fail. ``unquote`` returns it verbatim rather than
    raising, so the pair is simply left alone -- a redactor that could
    raise on a caller's own text would turn a logging call into an
    outage, which is the one thing this module promises never to do.
    """
    assert redact_text(
        'Traceback: host/p?api%5Fkey=SECRETVALUE in prose'
    ) == f'Traceback: host/p?api%5Fkey={REDACTED} in prose'

    assert redact_text('host/p?api%zzkey=NOTASECRET') == (
        'host/p?api%zzkey=NOTASECRET')


def test_redact_text_masks_a_declared_name_spelled_with_a_percent(
) -> None:
    """Decoding may widen the sensitive set; it may not narrow it.

    ``redact_query_params`` is the only way a caller names a secret this
    library could not guess, and ``normalise_param_names`` is careful to
    never return fewer names than it was handed. A lookup that tested
    *only* the decoded name would quietly break that promise for a caller
    whose parameter genuinely contains a ``%``: the declared ``x%5Fy``
    would be decoded to ``x_y``, match nothing, and leak the one value
    the caller took the trouble to declare. Testing the name both as
    written and decoded can only ever mask more, which is the direction
    every other bound in this module errs in.
    """
    masked = redact_text(
        'host/p?x%5Fy=SECRETVALUE', extra_params=('x%5Fy',))

    assert masked == f'host/p?x%5Fy={REDACTED}'


def test_redact_url_honours_caller_supplied_parameter_names() -> None:
    """``protocol_info['redact_query_params']`` extends the name set."""
    redacted = redact_url(
        'https://host/p?tenant=acme', extra_params=('tenant',))

    assert 'acme' not in redacted


async def test_a_caller_supplied_parameter_name_is_masked_in_the_envelope(
    http_server: RecordingHTTPServer,
) -> None:
    """The extension point is reachable from ``request()``, not only a unit.

    A caller whose secret rides in a name outside the default set --
    ``?session_id=`` -- must not get it echoed back in ``url``.
    """
    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)

    envelope = await request(
        http_server.url_for('/ok') + '?session_id=SESSIONSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': ['session_id'],
        },
    )

    assert 'SESSIONSECRET' not in envelope['url']
    assert REDACTED in envelope['url']


async def test_caller_supplied_names_extend_the_defaults_they_do_not_replace(
    http_server: RecordingHTTPServer,
) -> None:
    """The set is a union: supplying one name never unmasks ``api_key``."""
    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)

    envelope = await request(
        http_server.url_for('/ok')
        + '?session_id=SESSIONSECRET&api_key=DEFAULTSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': ['session_id'],
        },
    )

    assert 'SESSIONSECRET' not in envelope['url']
    assert 'DEFAULTSECRET' not in envelope['url']


async def test_a_caller_supplied_parameter_name_is_matched_case_insensitively(
    http_server: RecordingHTTPServer,
) -> None:
    """The default set is casefolded, so a caller's addition must be too."""
    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)

    envelope = await request(
        http_server.url_for('/ok') + '?Session-ID=SESSIONSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': ['SESSION-id'],
        },
    )

    assert 'SESSIONSECRET' not in envelope['url']


@pytest.mark.parametrize(
    'malformed',
    [
        pytest.param(None, id='none'),
        pytest.param([], id='empty-list'),
        pytest.param('session_id', id='bare-string-not-a-list'),
        pytest.param(['session_id', None, 7], id='non-string-elements'),
        pytest.param(42, id='not-iterable'),
        pytest.param({'session_id': True}, id='mapping'),
    ],
)
async def test_a_malformed_redact_query_params_never_costs_the_defaults(
    http_server: RecordingHTTPServer,
    malformed: Any,
) -> None:
    """Caller input on the redaction path fails safe: mask more, never less.

    A value of the wrong shape must not raise and must not silently turn
    the default name set off -- that would be a leak caused by a typo.
    """
    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)

    envelope = await request(
        http_server.url_for('/ok') + '?api_key=DEFAULTSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': malformed,
        },
    )

    assert 'DEFAULTSECRET' not in envelope['url']
    assert envelope['ok'] is True


@pytest.mark.parametrize(
    'raw, expected',
    [
        pytest.param(None, frozenset(), id='none'),
        pytest.param([], frozenset(), id='empty-list'),
        pytest.param(
            'Session_ID', frozenset({'session_id'}), id='bare-string'),
        pytest.param(
            ['A', None, 7],
            frozenset({'a', 'none', '7'}),
            id='non-string-elements',
        ),
        pytest.param(42, frozenset({'42'}), id='not-iterable'),
        pytest.param(
            {'Session': True}, frozenset({'session'}), id='mapping-keys'),
    ],
)
def test_normalise_param_names_fails_safe_on_every_shape(
    raw: Any,
    expected: frozenset[str],
) -> None:
    """The one boundary that reads caller input never raises on it."""
    assert normalise_param_names(raw) == expected


def test_redact_url_returns_a_url_it_cannot_parse_unchanged() -> None:
    """Diagnostic data is kept: the URL is not a security boundary.

    Kept, now, is not the same as echoed -- an unparseable URL carrying a
    credential is masked by the test below. This one holds the other half
    of that bargain: one carrying *no* credential still comes back whole,
    so the fallback did not turn a diagnostic into a row of asterisks.
    """
    unparseable = 'http://[oops'

    assert redact_url(unparseable) == unparseable


def test_redact_url_masks_userinfo_in_a_url_it_cannot_parse() -> None:
    """The unparseable URL is the *most* exposed one, not an obscure one.

    ``redact_url`` echoed its input on a parse failure, and M1/AGW-34
    accepted that only on the condition that no caller-visible surface
    reached it uncomposed. The condition was false: ``validated_url``
    builds its "url is not parseable" message with this function, so the
    single input that reaches this branch is by definition the one the
    caller is about to be shown. The unclosed IPv6 bracket is what
    defeats ``urlsplit``; the fallback is regex and does not care.
    """
    unparseable = 'http://user:SUPERSECRET123@[::1/p'

    masked = redact_url(unparseable)

    assert 'SUPERSECRET123' not in masked
    assert masked == f'http://user:{REDACTED}@[::1/p'


def test_redact_url_masks_a_sensitive_pair_in_a_url_it_cannot_parse(
) -> None:
    """The fallback carries the query rule into the failure path too.

    Not only userinfo: a URL that fails to parse can carry its secret in
    a query parameter just as readily, and the branch that gave up on
    one gave up on both.
    """
    unparseable = 'http://[::1/p?api_key=QUERYSECRET'

    masked = redact_url(unparseable)

    assert 'QUERYSECRET' not in masked
    assert masked == f'http://[::1/p?api_key={REDACTED}'


@pytest.mark.parametrize(
    'text, expected',
    [
        pytest.param(
            'https://user:PASS@host/p',
            'https://host/p',
            id='parseable-url-loses-userinfo-to-the-third-pass'),
        pytest.param(
            'RetriesExhausted: http://u:PASS@h/p failed, retrying',
            'RetriesExhausted: http://h/p failed, retrying',
            id='inside-prose'),
        pytest.param(
            "see http://user:PA'SS@host/p here",
            'see http://host/p here',
            id='password-holding-a-quote'),
        pytest.param(
            'see http://user:PA"SS@host/p here',
            'see http://host/p here',
            id='password-holding-a-double-quote'),
        pytest.param(
            'error: http://user:UNPARSESECRET@[::1/p failed',
            f'error: http://user:{REDACTED}@[::1/p failed',
            id='unparseable-url-keeps-the-shape-and-masks'),
        pytest.param(
            'error: http://BARETOKEN@[::1/p failed',
            f'error: http://{REDACTED}@[::1/p failed',
            id='bare-userinfo-is-masked-whole'),
        pytest.param(
            'no scheme here user:PASS@host',
            'no scheme here user:PASS@host',
            id='an-email-shaped-string-is-not-userinfo'),
    ],
)
def test_redact_text_masks_url_userinfo(text: str, expected: str) -> None:
    """Userinfo is invisible to a query-pair rule, so it needs its own.

    It carries no ``?`` and no ``=``, and ``redact_text`` is the masker
    the three surfaces that get no second pass rely on:
    ``error['message']``, ``error['cause']`` and the logged traceback.

    The embedded-URL pass already covered the *plain* case, and the
    first two rows are it working -- it drops userinfo outright, which
    is stronger than masking. The two rows that earn this pass are the
    ones that pass cannot reach. ``_EMBEDDED_URL`` stops at a quote and
    a backtick, so a password containing one truncates the match before
    the ``@`` and the tail is left in the clear; ``urlsplit`` gives up
    on the unclosed bracket entirely. Both were measured leaking with
    this pass disabled.

    The last row is the bound: a rule that fired without a scheme would
    mask every ``user@host`` in prose, so the scheme is required.
    """
    assert redact_text(text) == expected


def test_redact_text_masks_a_pair_after_a_legacy_semicolon_separator(
) -> None:
    """``;`` separates query parameters too, and servers still read it.

    Once recommended by HTML 4.01 and still parsed by PHP and servlet
    containers, so ``?a=1;api_key=S`` is two parameters to the server
    that receives it and the second is secret. The pair rule split on
    ``?`` and ``&`` alone, so the secret stayed in the clear in
    ``error['message']`` and the logged traceback (L1).
    """
    text = 'GET failed for host/p?a=1;api_key=LEGACYSECRET now'

    masked = redact_text(text)

    assert 'LEGACYSECRET' not in masked
    assert masked == f'GET failed for host/p?a=1;api_key={REDACTED} now'


def test_redact_text_keeps_the_semicolon_it_masks_after() -> None:
    """Masking reports what was sent; it does not rewrite it to ``&``.

    A caller reading the redacted string is reading a record of their own
    request, so normalising the separator would misreport it.
    """
    assert redact_text('?token=A;api_key=B') == (
        f'?token={REDACTED};api_key={REDACTED}')


def test_redact_url_leaves_a_url_with_nothing_to_mask_alone() -> None:
    """A clean URL is not re-encoded, so it round-trips exactly."""
    clean = 'https://host/p?page=2&sort=name'

    assert redact_url(clean) == clean


def test_redact_headers_keeps_names_and_masks_only_the_values() -> None:
    """A caller can still see *that* a credential header was sent."""
    redacted = redact_headers({
        'Authorization': 'Bearer x',
        'X-API-KEY': 'k',
        'Accept': 'application/json',
    })

    assert redacted['Authorization'] == REDACTED
    assert redacted['X-API-KEY'] == REDACTED
    assert redacted['Accept'] == 'application/json'


def test_redact_cookies_masks_every_value() -> None:
    """A cookie name carries no signal about whether its value is secret."""
    assert redact_cookies({'session': 's', 'theme': 'dark'}) == {
        'session': REDACTED,
        'theme': REDACTED,
    }


def test_redact_payload_masks_by_key_name_down_to_the_documented_depth(
) -> None:
    """Masking is by key name, recursive, and bounded (R8, E9)."""
    payload: dict[str, Any] = {
        'password': 'p0',
        'nested': {'token': 't1', 'items': [{'secret': 's2'}]},
        'kept': 'visible',
    }

    redacted = redact_payload(payload)

    assert redacted['password'] == REDACTED
    assert redacted['nested']['token'] == REDACTED
    assert redacted['nested']['items'][0]['secret'] == REDACTED
    assert redacted['kept'] == 'visible'
    assert payload['password'] == 'p0'


def test_redact_payload_echoes_data_below_the_depth_bound_verbatim(
) -> None:
    """The bound is part of the contract, so it is asserted, not implied."""
    deep: dict[str, Any] = {'password': 'top'}
    for _ in range(PAYLOAD_REDACTION_DEPTH + 2):
        deep = {'level': deep}

    redacted = redact_payload(deep)
    probe = redacted
    for _ in range(PAYLOAD_REDACTION_DEPTH + 2):
        probe = probe['level']

    assert probe['password'] == 'top'


@pytest.mark.parametrize(
    'payload',
    [
        pytest.param('a raw string body', id='str'),
        pytest.param(b'raw bytes body', id='bytes'),
        pytest.param(42, id='int'),
        pytest.param(None, id='none'),
    ],
)
def test_redact_payload_returns_a_non_mapping_payload_unchanged(
    payload: Any,
) -> None:
    """A non-mapping payload has no key names to match, so it is echoed."""
    assert redact_payload(payload) == payload


def test_redact_value_dispatches_on_type_for_the_logger() -> None:
    """One dispatcher, so a logged value and an envelope value agree."""
    assert redact_value('https://host/p?token=x').endswith(REDACTED)
    assert redact_value('not a url') == 'not a url'
    assert redact_value({'Authorization': 'Bearer x'}) == {
        'Authorization': REDACTED,
    }
    assert redact_value(200) == 200


def test_redact_value_treats_an_unparseable_string_as_plain_text() -> None:
    """A string that will not parse as a URL is logged as it came in."""
    assert redact_value('http://[oops') == 'http://[oops'


def test_redact_value_masks_a_url_that_has_no_netloc() -> None:
    """The seam itself, not the symptom: a netloc-less URL is masked.

    ``redact_value`` is the single entry point every ``extra`` value in
    ``log_failure`` passes through, and it once gated on a whole-string
    "has a scheme *and* a netloc" test that a URL like this fails -- so
    the string was echoed into the log in the clear. The end-to-end tests
    below prove the symptom is gone; this one names the seam, so
    restoring that gate fails here rather than only somewhere downstream.
    """
    masked = redact_value(
        'http:///p?session_id=SESSIONSECRET&api_key=APIKEYSECRET',
        extra_params=('session_id',),
    )

    assert 'SESSIONSECRET' not in masked
    assert 'APIKEYSECRET' not in masked
    assert masked == f'http:///p?session_id={REDACTED}&api_key={REDACTED}'


def test_redact_value_masks_a_string_identically_to_the_envelope() -> None:
    """The by-construction property, not one more predicate outcome.

    ``redact_value`` twice guarded masking behind a predicate asking "is
    this a URL?", and the predicate twice failed open: first demanding a
    scheme *and* a netloc, then demanding a scheme, which a URL the caller
    wrote without one does not have. Both times the log got the secret in
    the clear while the envelope masked it.

    So the assertion is *equality with the envelope's own masker* rather
    than the mere absence of the secret. Absence is what a third predicate
    could pass while still disagreeing with the envelope somewhere else;
    equality is the contract, and it holds only because ``redact_value``
    composes :func:`redact_url` unconditionally.
    """
    url = 'host/p?api_key=APIKEYSECRET&session_id=SESSIONSECRET'

    assert redact_value(url, extra_params=('session_id',)) == redact_url(
        url, extra_params=('session_id',))
    assert redact_value(url, extra_params=('session_id',)) == (
        f'host/p?api_key={REDACTED}&session_id={REDACTED}')


@pytest.mark.parametrize(
    'delimiter', [' ', "'", '"', '`', '<', '>'],
    ids=['space', 'single-quote', 'double-quote', 'backtick', 'lt', 'gt'])
def test_redact_value_masks_a_secret_that_contains_a_url_delimiter(
    delimiter: str,
) -> None:
    """The embedded-URL pass stops at these; the whole-string pass does not.

    ``redact_text`` ends a match at the first character that cannot appear
    unescaped in a URL, so a secret *value* carrying one had its tail left
    outside the match and echoed -- two thirds of ``?api_key=my secret
    key``. Nothing covered a delimiter inside the value, so both the leaking
    and the fixed code passed. The second, whole-string ``redact_url`` pass
    is what closes it, and this is what fails if it is removed.
    """
    masked = redact_value(f'http://host/p?api_key=SECRET{delimiter}TAIL')

    assert 'TAIL' not in masked
    assert masked == f'http://host/p?api_key={REDACTED}'


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        pytest.param(
            'ValueError: http://host/p?api_key=SECRETVALUE',
            f'ValueError: http://host/p?api_key={REDACTED}',
            id='the-exception-name-parses-as-the-scheme'),
        pytest.param(
            'ValueError: bad /a?debug=1 at http://host/p?api_key=SECRETVALUE',
            f'ValueError: bad /a?debug=1 at http://host/p?api_key={REDACTED}',
            id='the-whole-string-query-name-is-not-sensitive'),
    ])
def test_redact_value_runs_the_text_pass_before_the_whole_string_pass(
    text: str,
    expected: str,
) -> None:
    """Order is load-bearing, so it is pinned rather than left to reading.

    A traceback line beginning ``ValueError:`` has a truthy URL scheme, so
    the whole-string pass will happily consume the lot. Run *second*, after
    the embedded URL is already masked, that is harmless. The two ways of
    getting it wrong both fail here: reaching for the whole-string pass
    *instead of* the text pass because the scheme is truthy leaks the
    second case entirely -- it parses under the insensitive name ``debug``
    and masks nothing -- and running it first loses the exception's
    capitalisation in the first case to scheme normalisation.
    """
    assert redact_value(text) == expected


@pytest.mark.filterwarnings(
    # aiohttp 3.14 deprecates the type, but `request(auth=...)` still accepts
    # it and E9 still names it, so the redactor must keep handling it. The
    # filter covers constructing one here, not any behaviour under test.
    'ignore:BasicAuth is deprecated:DeprecationWarning',
)
@pytest.mark.parametrize(
    'wrap',
    [
        pytest.param(lambda auth: auth, id='bare'),
        pytest.param(lambda auth: {'credentials': auth}, id='nested'),
    ],
)
def test_a_basic_auth_object_never_survives_redaction(
    wrap: Any,
) -> None:
    """An auth object carries a password in a field no key name reveals."""
    secret = BasicAuth('user', 'AUTHSECRET')

    assert 'AUTHSECRET' not in repr(redact_value(wrap(secret)))
    assert 'AUTHSECRET' not in repr(redact_payload(wrap(secret)))


@pytest.mark.filterwarnings(
    'ignore:BasicAuth is deprecated:DeprecationWarning',
)
def test_a_basic_auth_object_at_the_depth_bound_is_still_masked() -> None:
    """The bound stops matching *key names*, not recognising an auth object.

    At exactly the bound the recursion stops descending and echoes what it
    is holding. A ``BasicAuth`` carries its password in a field no key name
    reveals, so echoing it there was an E9 hole: the type check runs before
    the bound, not after it. (Deeper still, the documented bound applies as
    ``test_redact_payload_echoes_data_below_the_depth_bound_verbatim``
    asserts -- unbounded recursion over caller data is the thing the bound
    exists to refuse.)
    """
    deep: Any = BasicAuth('user', 'AUTHSECRET')
    for _ in range(PAYLOAD_REDACTION_DEPTH):
        deep = {'level': deep}

    assert 'AUTHSECRET' not in repr(redact_payload(deep))


# --- E10: the trap keys are gone -------------------------------------------


async def test_e10_the_truthiness_trap_key_is_absent(
    ok_envelope: GatewayResponse,
) -> None:
    """Old code reading the removed alias fails loudly (E10, R8-AC6).

    Its removal is the point: a shim would preserve the trap, because the
    natural check against it was truthy on every path including failure.
    """
    with pytest.raises(KeyError):
        ok_envelope['api_response']


async def test_e10_the_renamed_duration_key_is_absent(
    ok_envelope: GatewayResponse,
) -> None:
    """``tat`` is `latency` now, with no alias left behind (E10)."""
    assert 'tat' not in ok_envelope
    assert 'latency' in ok_envelope


async def test_the_removed_keys_are_gone_not_reshaped(
    ok_envelope: GatewayResponse,
) -> None:
    """Every key the release removed is absent from the envelope."""
    for removed in (
        'api_response', 'tat', 'external_call_request_time', 'error_message',
    ):
        assert removed not in ok_envelope


# --- E11: a failure still carries the response -----------------------------


async def test_e11_a_404_with_a_json_body_keeps_the_whole_response(
    failed_envelope: GatewayResponse,
) -> None:
    """``ok=False`` never costs the caller the response body (E11).

    The single most common way a consumer uses a failure response, and the
    case an exception-only control flow drops silently.
    """
    assert failed_envelope['ok'] is False
    assert failed_envelope['status_code'] == 404
    assert failed_envelope['text']
    assert failed_envelope['json'] == {'error': 'no such thing'}
    assert failed_envelope['headers']
    assert failed_envelope['error'] is not None
    assert failed_envelope['error']['code'] == 'HTTP_STATUS'


def test_e11_finalise_error_only_adds_the_error_and_flips_ok() -> None:
    """Nothing the protocol already wrote is cleared (E11).

    Asserted at the function rather than only end-to-end, because this is
    the one place a future change could quietly reset the envelope to a
    failure skeleton.
    """
    envelope = new_envelope(url='http://host/p', protocol='HTTP', payload={})
    envelope['status_code'] = 404
    envelope['headers'] = {'Content-Type': 'application/json'}
    envelope['cookies'] = {'session': REDACTED}
    envelope['text'] = '{"error": "gone"}'
    envelope['json'] = {'error': 'gone'}
    envelope['protocol_details'] = {'hops': 1}

    finalised = finalise_error(
        envelope, HttpStatusError('gone', 404), started=monotonic_now())

    assert finalised is envelope
    assert finalised['status_code'] == 404
    assert finalised['headers'] == {'Content-Type': 'application/json'}
    assert finalised['cookies'] == {'session': REDACTED}
    assert finalised['text'] == '{"error": "gone"}'
    assert finalised['json'] == {'error': 'gone'}
    assert finalised['protocol_details'] == {'hops': 1}
    assert finalised['ok'] is False
    assert finalised['error'] is not None


# --- the constructors are the only ones ------------------------------------


def test_the_response_shape_is_declared_exactly_once() -> None:
    """One definition, so no protocol can drift from the contract."""
    assert files_containing(r'class GatewayResponse') == ['utils/envelope.py']


def test_only_the_envelope_module_constructs_an_envelope() -> None:
    """``new_envelope`` and the two finalisers are the only builders.

    Everything else fills the envelope it was handed. That rule is what
    makes the key set invariant, and it is the rule the transport helper
    used to break by returning a dict of its own.
    """
    assert files_containing(r'GatewayResponse\(') == ['utils/envelope.py']


def test_finalise_ok_clears_any_error_and_marks_the_call_successful(
) -> None:
    """The success finaliser is the only way ``ok`` becomes True."""
    envelope = new_envelope(url='http://host/p', protocol='HTTP', payload={})

    finalised = finalise_ok(envelope, status_code=201, started=monotonic_now())

    assert finalised is envelope
    assert finalised['ok'] is True
    assert finalised['error'] is None
    assert finalised['status_code'] == 201


def test_an_unfinalised_envelope_reads_as_a_failure() -> None:
    """A skeleton that somehow escaped must not read as success."""
    assert new_envelope(
        url='http://host/p', protocol='HTTP', payload={})['ok'] is False


# --- R10: the logger tree --------------------------------------------------


def test_r10_the_library_attaches_exactly_one_null_handler() -> None:
    """A library that adds a real handler duplicates its host's output."""
    handlers = logging.getLogger('async_gateway').handlers

    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.NullHandler)


async def test_r10_a_remote_failure_logs_one_warning_with_the_exception(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A status the caller may legitimately expect logs at warning.

    Exactly one record, at the one conversion point -- not one per layer.
    """
    http_server.respond('/missing', status=404, body=b'{}',
                        headers=JSON_HEADERS)
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    await http_call(http_server.url_for('/missing'))

    records = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert 'HttpStatusError' in records[0].traceback
    assert records[0].code == 'HTTP_STATUS'
    assert records[0].status_code == 404


async def test_r10_a_transport_failure_logs_one_error_with_the_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transport or configuration failure is this library's problem."""
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    await http_call(f'http://127.0.0.1:{closed_port()}/x')

    records = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert 'ConnectError' in records[0].traceback
    assert records[0].code == 'CONNECT'


async def test_r10_a_successful_call_logs_nothing(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing failed, so there is nothing for an operator to read."""
    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    await http_call(http_server.url_for('/ok'))

    assert [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ] == []


async def test_r10_the_logger_leaks_no_credential(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log and the envelope share one redactor, so they agree.

    A URL masked in the returned envelope but written to the log in the
    clear would defeat the point of masking it at all.
    """
    http_server.respond('/missing', status=404, body=b'{}',
                        headers=JSON_HEADERS)
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    await http_call(
        http_server.url_for('/missing') + '?api_key=LOGGEDSECRET',
        data={'password': 'PAYLOADSECRET'},
        request_type='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer HEADERSECRET',
        },
    )

    for record in caplog.records:
        rendered = f'{record.getMessage()} {record.__dict__}'
        for secret in (
            'LOGGEDSECRET', 'PAYLOADSECRET', 'HEADERSECRET',
        ):
            assert secret not in rendered, secret


async def test_r10_a_caller_supplied_parameter_name_is_masked_in_the_log(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log and the envelope share *one* set, extensions included.

    Asserted over the *whole* rendered record -- message, ``extra`` and
    the redacted ``extra['traceback']`` string -- rather than over the
    ``url`` field alone. A log that disagreed with the envelope about
    what is a secret would break the shared-redactor contract in exactly
    the place nobody looks, and an assertion narrower than that claim is
    how the exception-message channel survived the first fix.
    """
    http_server.respond('/missing', status=404, body=b'{}',
                        headers=JSON_HEADERS)
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    await request(
        http_server.url_for('/missing') + '?session_id=SESSIONSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': ['session_id'],
        },
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ]
    assert len(records) == 1
    assert 'SESSIONSECRET' not in render_record(records[0])
    assert REDACTED in records[0].url


async def test_r10_a_caller_declared_secret_reaches_no_surface_on_a_404(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A caller-declared secret survives nowhere, on the commonest failure.

    An ordinary 404 is the single most likely failure a consumer handles,
    and it reaches the caller through four channels at once: the
    envelope's ``url``, ``error['message']``, ``error['cause']`` and the
    log record including its redacted ``extra['traceback']`` string. Each
    is fed from a different call site, so this asserts the *absence of
    the raw secret across the whole surface* rather than the redaction of
    one field -- a per-field assertion is exactly what let the
    exception-message channel keep leaking after ``url`` was fixed.
    """
    http_server.respond('/missing', status=404, body=b'{}',
                        headers=JSON_HEADERS)
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    result = await request(
        http_server.url_for('/missing') + '?session_id=SESSIONSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': ['session_id'],
        },
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ]
    assert len(records) == 1
    error = result['error']
    assert error is not None

    surfaces = {
        'repr(result)': repr(result),
        "envelope['url']": result['url'],
        "error['message']": error['message'],
        "error['cause']": str(error['cause']),
        'log record': render_record(records[0]),
    }
    for surface_name, rendered in surfaces.items():
        assert 'SESSIONSECRET' not in rendered, surface_name

    # The secret is masked, not merely absent: a redactor that dropped the
    # parameter entirely would pass every assertion above and lose the
    # diagnostic value the envelope exists for.
    assert REDACTED in result['url']
    assert REDACTED in error['message']


# A URL with no netloc. aiohttp refuses it before it opens a connection,
# so the call needs no server and the exception that reaches
# `transport_error_for` is a real `InvalidUrlClientError` rather than a
# stand-in. Its `str()` is the whole URL, query string included -- which is
# the property under test. The shape is chosen deliberately: a netloc-less
# URL is exactly what used to defeat `redact_value`, whose whole-string
# gate demanded both a scheme and a netloc before it would mask at all and
# echoed anything else straight into the log. These two tests therefore
# also pin that fix.
NO_NETLOC_URL = 'http:///p?session_id=SESSIONSECRET&api_key=APIKEYSECRET'


async def test_r10_a_transport_failure_leaks_no_foreign_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure whose text this library did not author is redacted too.

    The 404 guard below it proves nothing about this: on a 404 the chain
    is a single ``HttpStatusError`` this library raised with an
    already-redacted message, and ``error['cause']`` is None. The
    transport path is the opposite -- ``transport_error_for`` lifts the
    message straight out of ``aiohttp``'s own exception, and the whole
    chain is then rendered into the log -- so every surface has to be
    asserted against a *foreign* string.
    """
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    result = await request(
        NO_NETLOC_URL,
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'redact_query_params': ['session_id'],
        },
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ]
    assert len(records) == 1
    error = result['error']
    assert error is not None
    # The chain really did reach aiohttp. Without this the test would pass
    # against a message this library wrote, proving nothing.
    assert 'InvalidUrlClientError' in str(error['cause'])

    surfaces = {
        'repr(result)': repr(result),
        "error['message']": error['message'],
        "error['cause']": str(error['cause']),
        'log record': render_record(records[0]),
    }
    for surface_name, rendered in surfaces.items():
        for secret in ('SESSIONSECRET', 'APIKEYSECRET'):
            assert secret not in rendered, f'{secret} in {surface_name}'

    assert REDACTED in error['message']
    assert REDACTED in str(error['cause'])


async def test_r10_a_default_secret_is_masked_with_no_redact_query_params(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The built-in names are the base contract, not a caller opt-in.

    ``redact_query_params`` is absent entirely here. A leak on this path
    would not be the extension feature failing -- it would be E9 itself
    failing for every caller who never asked for anything.
    """
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    result = await request(
        NO_NETLOC_URL,
        data={},
        protocol='HTTP',
        protocol_info={'request_type': 'GET'},
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ]
    assert len(records) == 1
    error = result['error']
    assert error is not None

    surfaces = {
        'repr(result)': repr(result),
        "error['message']": error['message'],
        "error['cause']": str(error['cause']),
        'log record': render_record(records[0]),
    }
    for surface_name, rendered in surfaces.items():
        assert 'APIKEYSECRET' not in rendered, surface_name

    assert REDACTED in error['message']
    # `session_id` is not a built-in name and was not declared, so it is
    # echoed. Asserted, not merely unasserted: it is what proves the two
    # halves of the set are distinct rather than the whole query string
    # being blanked.
    assert 'SESSIONSECRET' in error['message']


async def test_r10_a_scheme_less_url_reaches_the_log_url_field_masked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``extra['url']`` -- and only that field -- is clean end to end.

    Under ``'HTTP'`` a scheme-less URL is dispatched as the caller wrote
    it, so it is the string ``log_failure`` puts in ``extra['url']``.
    Nothing about that string carries a ``scheme://``, which is precisely
    why ``redact_value`` must not gate its ``redact_url`` pass behind a
    predicate.

    The assertion is deliberately **scoped to that one field**, because
    that field alone is masked by ``redact_value``. The surfaces masked by
    ``redact_text`` alone -- ``error['message']``, ``error['cause']`` and
    ``extra['traceback']`` -- reach the caller through a different route
    and are covered by the whole-record guard immediately below. Read this
    test as pinning the ``redact_value`` composition, not as a claim about
    the record as a whole.
    """
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    await request(
        'host/p?session_id=SESSIONSECRET&api_key=APIKEYSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'redact_query_params': ['session_id'],
        },
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ]
    assert len(records) == 1
    logged_url = records[0].url
    assert 'SESSIONSECRET' not in logged_url
    assert 'APIKEYSECRET' not in logged_url
    assert logged_url == (
        f'host/p?session_id={REDACTED}&api_key={REDACTED}')


async def test_r10_a_scheme_less_url_leaks_on_no_surface_at_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole envelope and the whole record, not one field (S8-H1).

    The test above is scoped to ``extra['url']`` because ``redact_value``
    composes ``redact_url`` over that field unconditionally. Three
    surfaces get no such second pass: ``error['message']``,
    ``error['cause']`` and ``extra['traceback']`` are masked by
    ``redact_text`` alone. While that function matched only strings
    carrying an explicit ``scheme://``, a caller who wrote
    ``host/p?api_key=...`` had the secret echoed into all three in the
    clear -- masked in the ``url`` sitting right beside them, which is
    what made it survive a green suite for so long.

    So this asserts the *whole* rendered record and the *whole* envelope
    rather than any one attribute: a per-field assertion is exactly the
    shape that let the leak hide. Both a built-in name and a
    caller-declared one ride along, because the two halves of the
    sensitive set reach ``redact_text`` by different routes and only one
    of them is this library's own.
    """
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    result = await request(
        'host/p?api_key=APIKEYSECRET&session_id=SESSIONSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'redact_query_params': ['session_id'],
        },
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ]
    assert len(records) == 1
    error = result['error']
    assert error is not None
    # The chain really did reach aiohttp, so the strings under test are
    # foreign text rather than a message this library wrote and redacted
    # at the point it authored it.
    assert 'InvalidUrlClientError' in str(error['cause'])

    surfaces = {
        'repr(result)': repr(result),
        "envelope['url']": result['url'],
        "error['message']": error['message'],
        "error['cause']": str(error['cause']),
        "extra['traceback']": records[0].traceback,
        'log record': render_record(records[0]),
    }
    for surface_name, rendered in surfaces.items():
        for secret in ('APIKEYSECRET', 'SESSIONSECRET'):
            assert secret not in rendered, f'{secret} in {surface_name}'

    # Masked, not merely absent: a redactor that dropped the query string
    # would satisfy every assertion above and lose the diagnostic the
    # operator reading this record actually needs.
    for surface_name in (
        "error['message']", "error['cause']", "extra['traceback']",
    ):
        assert REDACTED in surfaces[surface_name], surface_name


async def test_r10_a_percent_encoded_secret_leaks_on_no_surface_either(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recurring seam, re-checked end to end on an encoded name.

    S8-H1 was a leak into ``error['message']``, ``error['cause']`` and
    ``extra['traceback']`` -- the three surfaces no second masking pass
    ever reaches. Its first fix compared the parameter name exactly as it
    was written, so ``api%5Fkey`` was a name this module had never heard
    of, and the same three surfaces carried the secret again for the same
    reason as before.

    The failure is injected rather than driven through a real URL, and
    the reason is the point of the test. Hand ``request()`` an encoded
    name and *yarl* normalises ``%5F`` back to ``_`` long before any
    exception text is built, so aiohttp's own errors never carry the
    encoded spelling and a test written that way passes whether the name
    is decoded before the lookup or not. But ``describe_failure``
    redacts whatever the chain stringifies to, and that text is mostly
    *not* this library's: a client that echoes the request line it was
    given, or a protocol whose stack never parses a URL at all, hands
    these surfaces the caller's own encoding intact. This raises exactly
    such a foreign exception so the guard covers the case yarl happens to
    hide on the HTTP path.

    The whole record and the whole envelope are asserted, not one field
    of each, because a per-field assertion is the shape that let the
    original leak survive a green suite.
    """
    async def leaky(self: HttpRequest) -> GatewayResponse:
        cause = RuntimeError(
            'upstream rejected GET host/p?api%5Fkey=APIKEYSECRET')
        # An empty wrapper message is the shape that puts foreign text on
        # `message` as well as on `cause`: `describe_failure` falls back
        # to the deepest link when the wrapper has nothing of its own.
        raise ConnectError('') from cause

    monkeypatch.setattr(HttpRequest, 'handle_request', leaky)
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    result = await http_call(http_server.url_for('/anything'))

    records = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ]
    assert len(records) == 1
    error = result['error']
    assert error is not None

    surfaces = {
        'repr(result)': repr(result),
        "error['message']": error['message'],
        "error['cause']": str(error['cause']),
        "extra['traceback']": records[0].traceback,
        'log record': render_record(records[0]),
    }
    for surface_name, rendered in surfaces.items():
        assert 'APIKEYSECRET' not in rendered, surface_name

    # Masked, not merely absent: a redactor that dropped the query string
    # would satisfy every assertion above and lose the diagnostic the
    # operator reading this record actually needs.
    for surface_name in (
        "error['message']", "error['cause']", "extra['traceback']",
    ):
        assert REDACTED in surfaces[surface_name], surface_name


async def test_r10_cancellation_propagates_rather_than_becoming_an_envelope(
    http_server: RecordingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CancelledError`` is not a failed request and must not be caught.

    It is a ``BaseException`` on every supported interpreter; swallowing it
    would make a cancelled task look like it completed.
    """
    async def cancelled(self: HttpRequest) -> GatewayResponse:
        raise asyncio.CancelledError

    monkeypatch.setattr(HttpRequest, 'handle_request', cancelled)

    with pytest.raises(asyncio.CancelledError):
        await http_call(http_server.url_for('/anything'))


def test_r10_every_module_logs_through_the_package_tree() -> None:
    """No module names its logger by hand, so the tree stays one tree."""
    for name, source in package_sources():
        for match in re.finditer(r'logging\.getLogger\(([^)]*)\)', source):
            assert match.group(1) == '__name__', name


async def test_the_processor_slots_carry_what_the_caller_returned(
    http_server: RecordingHTTPServer,
) -> None:
    """Each processor's result lands in its own key, never in the body.

    The slots exist so a caller's own hook cannot reshape the envelope: it
    is handed the envelope and its return value is filed beside it.
    """
    async def pre(response: GatewayResponse, tag: str) -> str:
        return f'pre-{tag}'

    async def post(response: GatewayResponse) -> str:
        return f'post-{response["status_code"]}'

    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)

    envelope = await request(
        http_server.url_for('/ok'),
        data={},
        protocol='HTTP',
        protocol_info={'request_type': 'GET', 'headers': dict(JSON_HEADERS)},
        pre_processor_config={'function': pre, 'params': {'tag': 'x'}},
        post_processor_config={'function': post},
    )

    assert envelope['pre_processor_response'] == 'pre-x'
    assert envelope['post_processor_response'] == 'post-200'
    assert set(envelope) == EXPECTED_KEYS


# --- classification of what the resilience layer wraps ---------------------


def test_an_open_circuit_is_reported_as_such_not_as_a_transport_failure(
) -> None:
    """The breaker's own signal keeps its identity through conversion."""
    envelope = finalise_error(
        new_envelope(url='http://host/p', protocol='HTTP', payload={}),
        CircuitOpenError('circuit open for http://host/p'),
        started=monotonic_now(),
    )

    assert envelope['status_code'] == 503
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'CIRCUIT_OPEN'


def test_a_library_bug_wrapped_by_the_resilience_layer_still_propagates(
) -> None:
    """A wrapped ``KeyError`` is a bug, not a transport failure (R10).

    The resilience layer wraps *every* exception the call raised, so
    classifying on the wrapper would turn this library's own defects into
    ``ok=False`` envelopes -- which is exactly what hid most of the audit.
    """
    try:
        raise RetriesExhausted() from KeyError('missing_key')
    except RetriesExhausted as wrapper:
        with pytest.raises(KeyError):
            transport_error_for(wrapper)


def test_a_bare_transport_exception_is_classified_without_a_wrapper(
) -> None:
    """The classifier reads the cause when there is one, else the error."""
    classified = transport_error_for(asyncio.TimeoutError())

    assert classified.code == 'TIMEOUT'
    assert classified.status_code == 504
    assert str(classified)


def test_an_empty_resilience_wrapper_is_still_a_transport_failure() -> None:
    """A wrapper with no cause at all cannot be blamed on a library bug."""
    classified = transport_error_for(RetriesExhausted())

    assert classified.code == 'TRANSPORT'
    assert str(classified) == 'RetriesExhausted'


async def test_the_conversion_point_is_the_only_place_that_converts(
    http_server: RecordingHTTPServer,
) -> None:
    """One place turns a typed error into an envelope (R10, the one rule).

    The entry point holds the only ``except AsyncGatewayError`` in the
    package; every layer below it raises and none builds a failure shape.
    """
    converters = files_containing(r'except AsyncGatewayError')

    assert converters == ['async_gateway.py']
    assert hasattr(entrypoint, 'log_failure')

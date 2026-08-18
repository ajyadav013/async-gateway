"""The mechanical guard on the one-conversion-point invariant (N6, N7).

The library's central promise is a single sentence: **every failure
reaches the caller as an ``ok=False`` envelope, and the only exception
that may escape ``request()`` is an ``AsyncGatewayError`` subclass.**
Everything else -- a ``ValueError`` from ``aiohttp``'s header serialiser,
an ``aioftp.InvalidCommand``, a ``TypeError`` from ``orjson`` -- is by
definition a bug in this library, because a caller cannot be asked to
catch an exception the contract does not name.

This file exists because that invariant kept being broken *one input at
a time*. Two consecutive security reviews each found a different bare
exception escaping the same entry point: N6 (a CR or an LF in a
caller-supplied header, escaping as a bare ``ValueError`` from
``aiohttp.http_writer._safe_header``) and N7
(``aioftp.errors.InvalidCommand``, a ``ValueError`` subclass omitted from
FTP's ``TRANSPORT_ERRORS``). Both were patched where they were found.
Neither patch made the *next* one impossible, and a third review would
have found a third.

So the deliverable is not another pair of regression tests -- those live
beside their protocols -- but a **matrix**: a broad, deliberately hostile
set of caller inputs driven through the public ``request()``, asserting
only the invariant and nothing about any particular input's outcome. The
matrix is the enforceable form of the contract. An input may legitimately
produce a success envelope, a failure envelope or a typed exception, and
which of the three it produces is the protocol's business; what no input
may produce is a bare builtin.

**Why the HTTP family runs against a real socket.** N6 is the reason,
and getting this wrong was measured rather than reasoned about. The
first version of this file doubled *every* protocol at
``handle_http_request`` (``tests/fixtures/protocol_transports.py``), and
a mutation that disabled the header validator outright left all 570 rows
green -- because that seam sits **above** ``aiohttp``, so a header never
reached the serialiser that refuses it and the escape had nowhere to
happen. A matrix that cannot observe the defect it was written for is
worse than no matrix, since it reports safety it never checked.

So the HTTP family and SOAP run over the loopback ``aiohttp`` server in
``tests/fixtures/http_server.py``: a real connection, a real request, a
real ``_safe_header`` call. That placement matters twice over, because
``aiohttp`` refuses a CR-bearing header only at serialisation -- *after*
the connection is open. Against an unreachable host the connect fails
first, the headers are never serialised, and the call yields a tidy
``CONNECT`` envelope that hides the defect completely. Reachability is
what makes the escape observable, and it is exactly the nondeterminism
the invariant exists to remove.

FTP and SFTP keep the doubled transport. Their equivalent refusal
(``aioftp``'s own CR/LF check, N7) happens in code the double still runs
through, and standing up a real FTP and SSH server per row would cost
seconds apiece for no additional reach. The mutation proof below is run
against both lanes.

**What this file does not do.** It does not assert that a given input is
*rejected*. Tightening a validator so that it refuses more is a
behavioural change that belongs in the protocol's own suite, where the
message and the error code are asserted. Here, a stricter library and a
laxer one both pass, provided neither leaks a bare exception -- which is
what keeps the matrix stable enough to be worth extending.

Property-based generation (``hypothesis``) would suit this well and is
deliberately not used: it is not a dependency of this project, and adding
one to the dev set to reach inputs an explicit matrix already reaches is
a poor trade. The matrix is parametrised instead, and each family below
names the *reason* it is hostile so that the next reviewer extends it
rather than replacing it.
"""

from typing import Any, Final

import pytest

from async_gateway.async_gateway import request
from async_gateway.utils.exceptions import AsyncGatewayError

from tests.fixtures.http_server import RecordingHTTPServer
from tests.fixtures.protocol_transports import (
    CONTRACT_CALL,
    SOAP_BODY,
    contract_call,
    install_transport,
)

#: Every protocol the library registers. The invariant is a property of
#: the entry point, so it is asserted on all of them rather than on the
#: one whose defect prompted the file -- N6 landed on HTTP and SOAP, N7
#: on FTP, and the next one has no reason to respect that split.
PROTOCOLS: Final[tuple[str, ...]] = tuple(CONTRACT_CALL)

#: The protocols whose rows go over a real loopback socket, for the
#: reason the module docstring gives: their refusal lives inside
#: ``aiohttp``'s header serialiser, which a doubled transport never
#: reaches.
LIVE_PROTOCOLS: Final[tuple[str, ...]] = ('HTTP', 'SOAP')

#: The path the loopback server answers on for every live row.
LIVE_PATH: Final[str] = '/invariant'

#: Header mappings that are hostile, malformed, or the wrong type
#: outright. The first four are N6 itself: a CR or an LF in a name or a
#: value is header injection, and the fact that ``aiohttp`` refuses it
#: correctly was never the issue -- the issue was that it refused with a
#: bare ``ValueError`` after the socket was already open. The C0/DEL rows
#: are the rest of the character class ``aiohttp`` refuses in the same
#: breath. The remaining rows are the type confusions that reached the
#: same serialiser as a bare ``TypeError`` or ``AttributeError``: three
#: exception types for three spellings of one configuration mistake.
HOSTILE_HEADERS: Final[tuple[Any, ...]] = (
    {'X-Injected': 'value\r\nX-Smuggled: 1'},
    {'X-Injected': 'value\nX-Smuggled: 1'},
    {'X-Injected': 'value\rX-Smuggled: 1'},
    {'X-Injected\r\nX-Smuggled': 'value'},
    {'X-Injected': 'value\x00null'},
    {'X-Injected': 'value\x01soh'},
    {'X-Injected': 'value\x1funit'},
    {'X-Injected': 'value\x7fdel'},
    {'X-Injected': 1},
    {'X-Injected': None},
    {'X-Injected': b'bytes'},
    {'X-Injected': ['a', 'b']},
    {'X-Injected': {'nested': 'mapping'}},
    {1: 'value'},
    {None: 'value'},
    {b'bytes': 'value'},
    {(): 'value'},
    'a bare string, which iterates as characters',
    ['a', 'list'],
    42,
    object(),
)

#: Cookie mappings, hostile for the same reasons one key over. A cookie
#: is serialised by ``http.cookies`` rather than by ``aiohttp``, so the
#: same caller mistake arrived as a *different* bare exception --
#: ``CookieError`` for a control character, ``AttributeError`` for a
#: non-str name, ``ValueError: not enough values to unpack`` for a bare
#: string. A cookie name is additionally an RFC 6265 token and cannot be
#: quoted, so the space/semicolon/comma rows have no representation at
#: all rather than an escaped one.
HOSTILE_COOKIES: Final[tuple[Any, ...]] = (
    {'session': 'value\r\nX-Smuggled: 1'},
    {'session': 'value\nnewline'},
    {'session': 'value\x00null'},
    {'session\r\nX-Smuggled': 'value'},
    {'session name': 'value'},
    {'session;name': 'value'},
    {'session,name': 'value'},
    {'': 'value'},
    {'session': 1},
    {'session': None},
    {1: 'value'},
    {None: 'value'},
    'a bare string',
    ['not', 'a', 'mapping'],
    42,
)

#: Verbs and operation names. Each protocol reads its own key -- HTTP's
#: ``request_type``, FTP's ``command``, SFTP's ``mode`` -- and all three
#: resolve it through ``getattr`` on a live client object, which is the
#: shape that made ``request_type='close'`` call
#: ``ClientSession.close(url, **filters)`` and report the resulting
#: ``TypeError`` as a fabricated status (M25).
HOSTILE_VERBS: Final[tuple[Any, ...]] = (
    'close',
    'ws_connect',
    'detach',
    '__init__',
    '__class__',
    'quit',
    '',
    '   ',
    'get\r\nNOOP',
    None,
    42,
    b'get',
    ['get'],
    {'verb': 'get'},
    object(),
)

#: URLs. The type rows are the ones that mattered: ``url`` is annotated
#: ``str``, an annotation is not enforcement, and a non-str reached
#: ``redact_url`` and ``yarl.URL`` as a bare ``TypeError`` or
#: ``AttributeError`` on *every* protocol. The malformed-but-str rows
#: cover the parser's own edges.
HOSTILE_URLS: Final[tuple[Any, ...]] = (
    None,
    42,
    b'http://host/p',
    ['http://host/p'],
    {'url': 'http://host/p'},
    object(),
    '',
    '   ',
    '///',
    'http://',
    'ht!tp://host',
    'http://[::1',
    'http://host:notaport/p',
    'http://host/p\r\nX-Smuggled: 1',
    'javascript:alert(1)',
    'file:///etc/passwd',
    'gopher://host/p',
    'http://user:pw@host/p',
)

#: ``protocol_info`` values that are not the mapping the entry point
#: requires, plus mappings whose *values* are the wrong type. The second
#: group is the interesting one: a well-shaped ``protocol_info`` carrying
#: a nonsense value for a key the protocol reads is the shape most likely
#: to slip past a shape check and crash deeper in.
HOSTILE_PROTOCOL_INFO: Final[tuple[Any, ...]] = (
    'a bare string',
    42,
    ['a', 'list'],
    (('request_type', 'get'),),
    object(),
    {'timeout': 'ten seconds'},
    {'timeout': None},
    {'timeout': -1},
    {'max_response_bytes': 'lots'},
    {'max_response_bytes': 0},
    {'max_redirects': 'many'},
    {'max_redirects': -1},
    {'allow_redirects': 'yes'},
    {'allowed_schemes': 'http'},
    {'allowed_schemes': []},
    {'serialization': 'not callable'},
    {'session': 'not a session'},
    {'trace_config': 'not a config'},
    {'cross_origin_headers': 'x-id'},
    {'cross_origin_headers': ['authorization']},
    {'certificate': 'not a pair'},
    {'redact_query_params': 42},
    {'circuit_breaker_config': 'not a mapping'},
    {'retry_config': 'not a mapping'},
    {'port': 'not a port'},
    {'server_path': 42},
    {'client_path': 42},
    {'remote_path': 42},
    {'local_path': 42},
    {'overwrite': 'yes'},
    {'verify_ssl': 'yes'},
)

#: Request payloads. ``data`` is forwarded to a JSON encoder for most
#: verbs, and ``orjson`` refuses what it cannot serialise with a bare
#: ``TypeError`` raised from deep inside ``aiohttp``'s payload
#: construction -- far from the caller's mistake and outside any handler.
HOSTILE_DATA: Final[tuple[Any, ...]] = (
    object(),
    {'key': object()},
    {1: 'non-str key'},
    {'nested': {'deeper': object()}},
    [object()],
    lambda: None,
    b'\xff\xfe raw bytes',
    float('nan'),
    float('inf'),
)

#: ``auth`` objects that carry no usable credentials. FTP and SFTP
#: interpolate a login and a password into their connect call and cannot
#: form one without, which is why the documented-default ``auth=None``
#: call was an ``AttributeError`` on ``None.login`` (H5).
HOSTILE_AUTH: Final[tuple[Any, ...]] = (
    None,
    'not an auth object',
    42,
    object(),
)


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    http_server: RecordingHTTPServer,
) -> dict[str, Any]:
    """Return a valid call for ``protocol`` with its transport ready.

    The one place the two lanes diverge, so no test body has to know
    which lane it is on. ``HTTP``/``HTTPS``/``SOAP`` are pointed at the
    started loopback server and left to dial it for real;
    ``FTP``/``SFTP`` get their seam doubled.

    ``HTTPS`` keeps its doubled transport rather than joining the live
    lane, and the reason is not incidental: the entry point requires a
    ``https://`` URL for that protocol (R11-AC4), so pointing it at the
    plaintext loopback server would have every row refused at the scheme
    check -- before any header was built. Those rows would still pass,
    and would still prove nothing, which is the failure mode this whole
    file was rewritten to escape. ``HTTP`` and ``HTTPS`` share one
    protocol class, so the live ``HTTP`` rows already exercise the code
    ``HTTPS`` would.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        protocol: The protocol under test.
        http_server: The started loopback server.

    Returns:
        Keyword arguments for ``request()``, valid for this protocol.
    """
    call = contract_call(protocol)
    if protocol in LIVE_PROTOCOLS:
        body = SOAP_BODY if protocol == 'SOAP' else b'{"value": 1}'
        media = 'text/xml' if protocol == 'SOAP' else 'application/json'
        http_server.respond(
            LIVE_PATH, body=body, headers={'Content-Type': media})
        call['url'] = http_server.url_for(LIVE_PATH)
    else:
        install_transport(monkeypatch, protocol, succeeds=True)
    return call


async def _assert_only_typed_escapes(**call: Any) -> None:
    """Drive one ``request()`` call and hold it to the invariant.

    The single assertion this whole file makes. Deliberately narrow: the
    call may succeed, may return an ``ok=False`` envelope, or may raise
    an ``AsyncGatewayError``, and all three satisfy the contract. Only a
    fourth outcome -- an exception that is not an ``AsyncGatewayError``
    -- is a failure, because that is the one a caller following the
    documented contract cannot catch.

    ``asyncio.CancelledError`` inherits from ``BaseException`` and is
    re-raised untouched: cancelling a task is the caller's own act, not
    a failure of this library, and swallowing it here would make the
    matrix hang rather than fail.

    Args:
        **call: Keyword arguments forwarded verbatim to ``request()``.

    Returns:
        None.

    Raises:
        AssertionError: If anything other than an ``AsyncGatewayError``
            escapes, naming the exception and the call that produced it.
    """
    try:
        envelope = await request(**call)
    except AsyncGatewayError:
        return
    except Exception as err:
        raise AssertionError(
            f'{type(err).__module__}.{type(err).__name__} escaped '
            f'request() un-enveloped: {err!r}. Only an AsyncGatewayError '
            f'subclass may reach the caller as an exception; every other '
            f'failure must arrive as an ok=False envelope. Call was: '
            f'{call!r}') from err
    assert 'ok' in envelope, (
        f'request() returned an object with no "ok" key for {call!r}')


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('headers', HOSTILE_HEADERS)
async def test_no_header_shape_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    headers: Any,
) -> None:
    """N6's class: no header mapping produces a bare exception.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        headers: One hostile ``protocol_info['headers']`` value.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    call['protocol_info']['headers'] = headers
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('cookies', HOSTILE_COOKIES)
async def test_no_cookie_shape_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    cookies: Any,
) -> None:
    """The same class, reached through ``http.cookies`` instead.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        cookies: One hostile ``protocol_info['cookies']`` value.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    call['protocol_info']['cookies'] = cookies
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('verb', HOSTILE_VERBS)
async def test_no_verb_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    verb: Any,
) -> None:
    """No operation name resolves to something that crashes bare.

    Each protocol's verb lives under a different key, and all three are
    set so that one row covers whichever the protocol actually reads.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        verb: One hostile verb, command or mode.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    call['protocol_info'].update(
        request_type=verb, command=verb, mode=verb)
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('url', HOSTILE_URLS)
async def test_no_url_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    url: Any,
) -> None:
    """No URL -- of any type or spelling -- produces a bare exception.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        url: One hostile URL.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    call['url'] = url
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('info', HOSTILE_PROTOCOL_INFO)
async def test_no_protocol_info_shape_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    info: Any,
) -> None:
    """No ``protocol_info`` shape or value produces a bare exception.

    A mapping row is merged onto the protocol's valid configuration
    rather than replacing it, so the required keys stay present and the
    hostile *value* is what the call is actually judged on. A non-mapping
    row replaces it outright, which is the shape being tested there.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        info: One hostile ``protocol_info``.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    if isinstance(info, dict):
        call['protocol_info'].update(info)
    else:
        call['protocol_info'] = info
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('data', HOSTILE_DATA)
async def test_no_payload_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    data: Any,
) -> None:
    """No unserialisable payload produces a bare encoder exception.

    Sent on a POST where the protocol takes a verb, because a GET routes
    the payload to the query string and never reaches the JSON encoder
    that is the point of this row.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        data: One hostile payload.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    if 'request_type' in call['protocol_info']:
        call['protocol_info']['request_type'] = 'POST'
    call['data'] = data
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('auth', HOSTILE_AUTH)
async def test_no_auth_object_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    auth: Any,
) -> None:
    """No ``auth`` object produces a bare attribute error.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        auth: One credential-less ``auth`` value.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    call['auth'] = auth
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
async def test_the_valid_call_still_succeeds_on_every_protocol(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
) -> None:
    """The matrix's control row, and the reason it can be trusted.

    Every assertion above passes trivially if ``request()`` rejects
    *everything* -- a library that refuses all input leaks no bare
    exceptions either. This row is what makes the others mean something:
    the same doubled transport, the same call shape, with nothing
    hostile in it, must still reach a success envelope.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.

    Returns:
        None.
    """
    envelope = await request(**_prepare(monkeypatch, protocol, http_server))
    assert envelope['ok'] is True, (
        f'the valid {protocol} call did not succeed against a transport '
        f'double that completes the operation: {envelope!r}')

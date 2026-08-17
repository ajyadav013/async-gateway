"""Request construction: serialisation (R3), dispatch (R12), TLS (R23).

Covers three of the four former ``ujson`` call sites: the two in
``filters_helper`` that build a request body, and the ``json_serialize``
wiring in ``logic/http_client.py`` that hands a serialiser to ``aiohttp``. (The
fourth, the response decoder, is in ``test_response_helper.py``.)

The R12 section that follows them covers the media-type matcher both sides
of the exchange now share, the filter table the spec names, the verb
comparison, query-parameter coercion, and the promise that the caller's own
payload dict is unchanged after a call.

``orjson.dumps`` returns ``bytes`` where ``ujson.dumps`` returned ``str``,
and every one of these sites feeds something that treats the two
differently. The assertions are therefore made on what actually reaches the
wire -- read off the loopback recording server -- not on the shape of the
intermediate object, because the failure mode being guarded against is
precisely one that a shape assertion would wave through. For the same
reason the ``json_serialize=`` wiring is asserted through a dispatched
request and not only through a constructed object.

The **R23** section at the end owns this module's other half,
``get_ssl_config``: the client context that was built with the *server*
constructor (H28), the ``verify_ssl`` flag that was inoperative (M1), the
blocking CA-bundle read sitting on the event loop (AGW-36), and the live
mutual-TLS handshake FI-2 makes non-optional. Its handshake assertions run
against a real loopback TLS server and a real throwaway PKI, for the same
reason the serialisation assertions read the wire: this fix makes client
certificates connect for the first time ever, and a double that never
handshakes would have passed the broken context as happily as the fixed one.
"""

import ast
import asyncio
import inspect
import json
import logging
import socket
import ssl
import subprocess
import sys
import threading
from copy import deepcopy
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import aiohttp

from async_gateway import async_gateway as entrypoint
from async_gateway.helpers.internal import (
    RequestFilter,
    filter_for_media_type,
    is_json_media_type,
    media_type_of,
    normalise_media_type,
)
from async_gateway.helpers.internal.filters_helper import (
    application_json_filters,
    build_client_ssl_context,
    build_query_params,
    coerce_query_value,
    form_x_www_form_urlencoded_filters,
    get_ssl_config,
    is_get,
    normalised_certificate,
    raw_body_filters,
)
from async_gateway.logic.http_client import (
    HttpRequest,
    default_json_serialize,
)
from async_gateway.utils.exceptions import ConfigurationError

import orjson

import pytest

from tests.fixtures.http_server import RecordingHTTPServer
from tests.fixtures.tls import (
    CertificateFiles,
    MutualTlsServer,
    TlsWorld,
    mutual_tls_server,
    tls_world,
)


def _http_request(**info: object) -> HttpRequest:
    """Build an ``HttpRequest`` with the given ``protocol_info`` overrides.

    Args:
        info: ``protocol_info`` keys to set; ``request_type`` defaults to
            ``POST``.

    Returns:
        A constructed ``HttpRequest``. Construction is the boundary under
        test, so this deliberately does not dispatch anything.
    """
    return HttpRequest(
        'http://127.0.0.1/unused',
        None,
        {'payload': {}},
        info={'request_type': 'POST', **info},
        redact_params=frozenset(),
    )


# --- site 2: filters_helper.py, FormData.add_field ------------------------

async def _post_form(
    http_server: RecordingHTTPServer,
    data: dict,
) -> object:
    """Send ``data`` through the form filter and return what arrived.

    Args:
        http_server: The loopback recording server fixture.
        data: The mapping to hand to the form filter.

    Returns:
        The single ``RecordedRequest`` the server received.
    """
    filters = await form_x_www_form_urlencoded_filters(
        data, request_type='POST')
    http_server.respond('/form', body=b'ok')

    async with aiohttp.ClientSession() as session:
        async with session.post(
            http_server.url_for('/form'),
            **filters,
        ) as response:
            assert response.status == 200

    recorded, = http_server.requests
    return recorded


async def test_a_dict_form_field_reaches_the_wire_as_compact_json(
    http_server: RecordingHTTPServer,
) -> None:
    """A dict form value is sent as the JSON text ``ujson`` produced."""
    recorded = await _post_form(
        http_server,
        {'meta': {'a': 1, 'b': 'two'}, 'plain': 'value'},
    )

    assert parse_qs(recorded.body.decode()) == {
        'meta': ['{"a":1,"b":"two"}'],
        'plain': ['value'],
    }


async def test_a_dict_form_field_keeps_the_urlencoded_content_type(
    http_server: RecordingHTTPServer,
) -> None:
    """The request stays ``x-www-form-urlencoded``.

    This is the regression the ``.decode()`` exists for, and it is invisible
    in the field value alone. ``FormData.add_field`` accepts ``bytes``, but a
    non-text field forces the whole request to ``multipart/form-data`` -- so
    handing it ``orjson.dumps``' raw bytes silently changes the encoding of
    every form request the library makes, in a helper whose name promises
    otherwise.
    """
    recorded = await _post_form(http_server, {'meta': {'a': 1}})

    assert recorded.headers['Content-Type'] == (
        'application/x-www-form-urlencoded'
    )


# --- site 3: filters_helper.py, the non-str `data` branch -----------------

@pytest.mark.parametrize(
    'data, expected',
    [
        pytest.param([1, 2], '[1,2]', id='list'),
        pytest.param(17, '17', id='int'),
        pytest.param(None, 'null', id='none'),
    ],
)
async def test_a_non_string_json_body_becomes_a_string(data, expected) -> None:
    """A non-``str`` body is serialised to ``str``, never left as bytes."""
    filters = await application_json_filters(data, request_type='POST')

    assert isinstance(filters['data'], str)
    assert filters['data'] == expected


async def test_a_string_json_body_is_passed_through_untouched() -> None:
    """An already-serialised body is not re-encoded."""
    filters = await application_json_filters(
        '{"already":"text"}',
        request_type='POST',
    )

    assert filters['data'] == '{"already":"text"}'


async def test_a_dict_json_body_still_takes_the_json_branch() -> None:
    """A dict body is handed to aiohttp as ``json=``, not pre-encoded."""
    filters = await application_json_filters({'a': 1}, request_type='POST')

    assert filters == {'json': {'a': 1}}


# --- site 4: logic/http_client.py, the `json_serialize` wiring ------------

def test_the_default_serializer_returns_text_not_bytes() -> None:
    """The default is the decoding wrapper, never bare ``orjson.dumps``."""
    assert default_json_serialize({'a': 1}) == '{"a":1}'
    assert _http_request().serialization is default_json_serialize


async def test_the_default_serializer_puts_the_expected_json_on_the_wire(
    http_server: RecordingHTTPServer,
) -> None:
    """A JSON body posted through the wired default arrives intact.

    The serialiser is taken off a constructed ``HttpRequest`` rather than
    imported directly, so this asserts the *wiring* as well as the wrapper.
    """
    serializer = _http_request().serialization
    http_server.respond('/json', body=b'ok')
    payload = {'a': 1, 'nested': {'b': [1, 2]}}

    async with aiohttp.ClientSession(json_serialize=serializer) as session:
        async with session.post(
            http_server.url_for('/json'),
            json=payload,
        ) as response:
            assert response.status == 200

    recorded, = http_server.requests
    assert orjson.loads(recorded.body) == payload
    assert recorded.headers['Content-Type'] == 'application/json'


async def test_bare_orjson_dumps_really_does_break_aiohttp(
    http_server: RecordingHTTPServer,
) -> None:
    """Passing ``orjson.dumps`` bare to aiohttp fails, deep and late.

    The evidence that the boundary check below is load-bearing rather than
    decorative. Without it, this is what a caller gets: an ``AttributeError``
    raised from inside ``aiohttp.payload.JsonPayload``, naming neither the
    library nor the configuration key at fault, and only on the first request
    -- long after the migration has appeared to succeed.
    """
    http_server.respond('/json', body=b'ok')

    async with aiohttp.ClientSession(
        json_serialize=orjson.dumps,
    ) as session:
        with pytest.raises(AttributeError) as raised:
            await session.post(
                http_server.url_for('/json'),
                json={'a': 1},
            )

    assert "'bytes' object has no attribute 'encode'" in str(raised.value)
    assert not http_server.requests


@pytest.mark.parametrize(
    'serializer',
    [
        pytest.param(orjson.dumps, id='orjson-dumps'),
        pytest.param(lambda obj: b'{}', id='hand-rolled-bytes'),
    ],
)
def test_a_bytes_returning_serializer_is_rejected_at_the_boundary(
    serializer,
) -> None:
    """A ``bytes``-returning serialiser fails at construction, not on send."""
    with pytest.raises(ConfigurationError) as raised:
        _http_request(serialization=serializer)

    assert 'must return str' in str(raised.value)
    assert 'bytes' in str(raised.value)


async def test_an_explicit_stdlib_serializer_keeps_working(
    http_server: RecordingHTTPServer,
) -> None:
    """``serialization=json.dumps`` is accepted and used as given.

    The wrapper applies only to the default; a caller who supplied their own
    ``str``-returning serialiser before the migration must be unaffected.
    """
    request = _http_request(serialization=json.dumps)
    assert request.serialization is json.dumps

    http_server.respond('/json', body=b'ok')
    async with aiohttp.ClientSession(
        json_serialize=request.serialization,
    ) as session:
        async with session.post(
            http_server.url_for('/json'),
            json={'a': 1},
        ) as response:
            assert response.status == 200

    recorded, = http_server.requests
    # `json.dumps` spaces its separators where `orjson` does not; the point
    # is that the caller's choice survives, byte for byte.
    assert recorded.body == b'{"a": 1}'


# --- site 4, the kwarg itself: FI-6's guard -------------------------------

def _sentinel_serializer(obj: object) -> str:
    """Serialise anything to one fixed, unmistakable JSON document.

    Args:
        obj: Ignored -- the output has to be recognisable, not correct.

    Returns:
        A JSON object no default serialiser could ever produce.
    """
    return '{"sentinel":"caller"}'


async def _dispatch_json(
    http_server: RecordingHTTPServer,
    payload: dict,
    **info: object,
) -> object:
    """Drive a whole ``HttpRequest.handle_request`` at the loopback server.

    Args:
        http_server: The loopback recording server fixture.
        payload: The JSON body to post.
        info: Extra ``protocol_info`` keys, e.g. ``serialization``.

    Returns:
        The single ``RecordedRequest`` the server received.
    """
    http_server.respond('/json', body=b'{}')
    request = HttpRequest(
        http_server.url_for('/json'),
        None,
        {'payload': payload},
        info={
            'request_type': 'POST',
            'headers': {'Content-Type': 'application/json'},
            **info,
        },
        redact_params=frozenset(),
    )
    await request.handle_request()

    recorded, = http_server.requests
    return recorded


async def test_the_session_serialises_the_body_with_the_wired_default(
    http_server: RecordingHTTPServer,
) -> None:
    """``handle_request`` hands its own serialiser to the session.

    The guard on ``json_serialize=self.serialization``. Drop that kwarg and
    ``aiohttp`` silently falls back to ``json.dumps``, whose spaced
    separators put different bytes on the wire -- a misconfigured session
    that every construction-time assertion in this file would wave through,
    because none of them dispatches anything.
    """
    recorded = await _dispatch_json(http_server, {'a': 1, 'b': 'two'})

    assert recorded.body == b'{"a":1,"b":"two"}'


async def test_the_session_serialises_the_body_with_the_callers_choice(
    http_server: RecordingHTTPServer,
) -> None:
    """A caller's serialiser is the one that reaches ``ClientSession``.

    The validated callable, and only the validated callable, encodes the
    body: the sentinel output can arrive on the wire by no other route. So
    a ``bytes``-returning serialiser cannot reach the session either -- it
    is rejected at construction, and nothing else is wired in its place.
    """
    recorded = await _dispatch_json(
        http_server,
        {'a': 1},
        serialization=_sentinel_serializer,
    )

    assert recorded.body == b'{"sentinel":"caller"}'


@pytest.mark.parametrize(
    'serializer',
    [
        pytest.param('orjson.dumps', id='str'),
        pytest.param(None, id='none'),
        pytest.param(17, id='int'),
    ],
)
def test_a_non_callable_serializer_is_rejected_at_the_boundary(
    serializer,
) -> None:
    """A non-callable fails as a configuration error, not a ``TypeError``.

    Same class of caller mistake as a ``bytes``-returning callable, so it
    gets the same treatment: named at the boundary rather than surfacing as
    a bare ``'str' object is not callable`` from the constructor.
    """
    with pytest.raises(ConfigurationError) as raised:
        _http_request(serialization=serializer)

    assert 'must be callable' in str(raised.value)
    assert type(serializer).__name__ in str(raised.value)


# --- R12: the shared media-type matcher (H14) -----------------------------

async def _dispatch(
    http_server: RecordingHTTPServer,
    payload: object,
    *,
    request_type: str = 'POST',
    headers: dict | None = None,
    path: str = '/echo',
) -> object:
    """Drive one whole request at the loopback server and read it back.

    Every R12 assertion that can be made on the wire is made here rather
    than on a filter's return value: the filter dict is an intermediate
    shape, and the defects being guarded against -- a payload attached as a
    body instead of a query string, a bool rendered ``'True'``, a JSON
    document sent under a form content type -- are all invisible in it.

    Args:
        http_server: The loopback recording server fixture.
        payload: The request payload.
        request_type: The verb, spelled however the test needs it.
        headers: Request headers, or None for none.
        path: The path to register and dispatch to.

    Returns:
        The last ``RecordedRequest`` the server received.
    """
    http_server.respond(path, body=b'')
    request = HttpRequest(
        http_server.url_for(path),
        None,
        {'payload': payload},
        info={'request_type': request_type, 'headers': headers or {}},
        redact_params=frozenset(),
    )
    await request.handle_request()

    return http_server.requests[-1]


@pytest.mark.parametrize(
    'header_value, expected',
    [
        pytest.param('application/json', 'application/json', id='plain'),
        pytest.param(
            'application/json; charset=utf-8',
            'application/json',
            id='charset-parameter',
        ),
        pytest.param('Application/JSON', 'application/json', id='mixed-case'),
        pytest.param(
            'APPLICATION/X-WWW-FORM-URLENCODED',
            'application/x-www-form-urlencoded',
            id='upper-case',
        ),
        pytest.param(
            '  text/xml ; charset=utf-8 ', 'text/xml', id='whitespace'),
        pytest.param(
            'multipart/mixed; boundary=abc',
            'multipart/mixed',
            id='multipart-boundary',
        ),
        pytest.param('', '', id='empty'),
        pytest.param(None, '', id='absent'),
    ],
)
def test_a_content_type_reduces_to_its_bare_media_type(
    header_value: str | None,
    expected: str,
) -> None:
    """Parameters are stripped and case is folded, as HTTP requires."""
    assert normalise_media_type(header_value) == expected


@pytest.mark.parametrize(
    'headers, expected',
    [
        pytest.param(
            {'Content-Type': 'application/json'},
            'application/json',
            id='canonical-key',
        ),
        pytest.param(
            {'content-type': 'application/x-www-form-urlencoded'},
            'application/x-www-form-urlencoded',
            id='lower-case-key',
        ),
        pytest.param(
            {'CONTENT-TYPE': 'Application/JSON; charset=utf-8'},
            'application/json',
            id='upper-case-key-and-parameter',
        ),
        pytest.param({'Accept': 'application/json'}, '', id='no-such-header'),
        pytest.param({}, '', id='no-headers'),
        pytest.param(None, '', id='none'),
    ],
)
def test_the_content_type_key_is_matched_without_regard_to_case(
    headers: dict[str, str] | None,
    expected: str,
) -> None:
    """A caller's plain dict is not case-insensitive; the matcher is.

    ``{'content-type': '...form-urlencoded'}`` used to find nothing and
    silently JSON-encode a form payload, because the lookup was a dict key
    lookup and the caller had spelled the key the other way.
    """
    assert media_type_of(headers) == expected


@pytest.mark.parametrize(
    'media_type, is_json',
    [
        pytest.param('application/json', True, id='json'),
        pytest.param('application/problem+json', True, id='problem-json'),
        pytest.param('text/xml', False, id='xml'),
        pytest.param('', False, id='absent'),
    ],
)
def test_json_is_recognised_by_media_type_and_by_rfc_6839_suffix(
    media_type: str,
    is_json: bool,
) -> None:
    """``+json`` counts, and nothing else does.

    The substring matching this replaces accepted ``+json`` suffixes, so
    dropping them would be a regression; it also accepted anything that
    merely *contained* the string, which is the half being dropped.
    """
    assert is_json_media_type(media_type) is is_json


# --- R12: the filter table the spec names ---------------------------------

@pytest.mark.parametrize(
    'media_type, payload, expected',
    [
        pytest.param(
            'application/json', {'a': 1}, application_json_filters, id='json'),
        pytest.param(
            'application/x-www-form-urlencoded',
            {'a': 1},
            form_x_www_form_urlencoded_filters,
            id='form',
        ),
        pytest.param('text/xml', '<x/>', raw_body_filters, id='text-xml'),
        pytest.param(
            'application/soap+xml', '<x/>', raw_body_filters, id='soap-xml'),
        pytest.param(
            'application/xml', '<x/>', raw_body_filters, id='application-xml'),
        pytest.param(
            'application/octet-stream',
            'raw text',
            raw_body_filters,
            id='unknown-with-str',
        ),
        pytest.param(
            'application/octet-stream',
            b'raw bytes',
            raw_body_filters,
            id='unknown-with-bytes',
        ),
        pytest.param('', 'raw text', raw_body_filters, id='absent-with-str'),
        pytest.param(
            'application/octet-stream',
            {'a': 1},
            application_json_filters,
            id='unknown-with-dict',
        ),
        pytest.param(
            '', {'a': 1}, application_json_filters, id='absent-with-dict'),
    ],
)
def test_the_filter_table_is_exactly_the_one_the_spec_names(
    media_type: str,
    payload: Any,
    expected: RequestFilter,
) -> None:
    """R12's table, as a test.

    The row that matters most is ``unknown-with-str``: the replaced
    ``'default': application_json_filters`` entry routed every unknown
    media type to the JSON filter, which is what would send a SOAP
    envelope through a JSON encoder.
    """
    assert filter_for_media_type(media_type, payload) is expected


def test_no_media_type_resolves_to_a_filter_that_is_none() -> None:
    """The dispatch can never call ``None`` (H14).

    ``header_filter_mapping.get(content_type)(...)`` raised
    ``TypeError: 'NoneType' object is not callable`` for every media type
    absent from the mapping -- which included the extremely common
    ``application/json; charset=utf-8``. There is no lookup left that can
    miss.
    """
    for media_type in ('', 'application/json; charset=utf-8', 'nonsense/x'):
        for payload in ({'a': 1}, 'text', b'bytes', None, 17):
            assert callable(filter_for_media_type(media_type, payload))


@pytest.mark.parametrize(
    'request_filter, payload',
    [
        pytest.param(application_json_filters, {'a': 1}, id='json'),
        pytest.param(
            form_x_www_form_urlencoded_filters, {'a': 1}, id='form'),
        pytest.param(raw_body_filters, '<x/>', id='raw'),
    ],
)
async def test_every_filter_takes_the_same_minimum_argument_set(
    request_filter: RequestFilter,
    payload: Any,
) -> None:
    """The payload and the verb, named -- never ``kwargs['request_type']``.

    ``application_json_filters`` used to read ``kwargs['request_type']``,
    a hard ``KeyError`` for any caller that supplied none, which is every
    SOAP call. The uniform signature is what lets the dispatch treat the
    three interchangeably.
    """
    assert await request_filter(payload, request_type='POST')


async def test_the_raw_filter_does_not_touch_the_body(
    http_server: RecordingHTTPServer,
) -> None:
    """An XML body reaches the wire byte-identical, never re-encoded.

    The property AGW-22 dispatches its SOAP envelope through.
    """
    envelope = (
        '<?xml version="1.0"?><soap:Envelope '
        'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body><Ping/></soap:Body></soap:Envelope>'
    )
    recorded = await _dispatch(
        http_server,
        envelope,
        headers={'Content-Type': 'text/xml; charset=utf-8'},
    )

    assert recorded.body == envelope.encode()


async def test_an_unknown_media_type_with_a_dict_body_is_json_encoded(
    http_server: RecordingHTTPServer,
) -> None:
    """The other half of the replaced default: a mapping still gets JSON."""
    recorded = await _dispatch(
        http_server,
        {'a': 1},
        headers={'Content-Type': 'application/vnd.acme+widget'},
    )

    assert orjson.loads(recorded.body) == {'a': 1}


async def test_a_charset_parameter_no_longer_breaks_json_encoding(
    http_server: RecordingHTTPServer,
) -> None:
    """H14, end to end.

    ``application/json; charset=utf-8`` found no entry in the exact-key
    mapping, so the dispatch called ``None`` and the resulting
    ``TypeError`` was swallowed into a fabricated ``999``.
    """
    recorded = await _dispatch(
        http_server,
        {'a': 1},
        headers={'Content-Type': 'application/json; charset=utf-8'},
    )

    assert orjson.loads(recorded.body) == {'a': 1}


async def test_a_lower_case_content_type_key_still_selects_the_form_filter(
    http_server: RecordingHTTPServer,
) -> None:
    """The spec's own edge case, asserted on the wire.

    ``{'content-type': '...form-urlencoded'}`` silently JSON-encoded the
    payload, so the server received a JSON document under a form content
    type and parsed nothing.
    """
    recorded = await _dispatch(
        http_server,
        {'a': 'one', 'b': 'two'},
        headers={'content-type': 'application/x-www-form-urlencoded'},
    )

    assert parse_qs(recorded.body.decode()) == {'a': ['one'], 'b': ['two']}


# --- R12: the verb comparison (M5) ----------------------------------------

@pytest.mark.parametrize(
    'request_type, expected',
    [
        pytest.param('GET', True, id='upper'),
        pytest.param('get', True, id='lower'),
        pytest.param('Get', True, id='mixed'),
        pytest.param(' get ', True, id='padded'),
        pytest.param('POST', False, id='post'),
        pytest.param('DELETE', False, id='delete'),
    ],
)
def test_the_verb_comparison_ignores_case_and_padding(
    request_type: str,
    expected: bool,
) -> None:
    """M5: ``request_type == 'GET'`` was the one case-sensitive comparison."""
    assert is_get(request_type) is expected


@pytest.mark.parametrize(
    'request_type',
    [
        pytest.param('GET', id='upper'),
        pytest.param('get', id='lower'),
        pytest.param('Get', id='mixed'),
    ],
)
async def test_every_spelling_of_get_sends_the_payload_as_query_params(
    http_server: RecordingHTTPServer,
    request_type: str,
) -> None:
    """Query parameters, never a JSON body, whatever the casing.

    Read off the server rather than off the filter dict, because the bug
    is about what was put on the wire: ``"get"`` attached the payload as a
    request body on a verb that has none.
    """
    recorded = await _dispatch(
        http_server,
        {'a': 'one'},
        request_type=request_type,
        headers={'Content-Type': 'application/json'},
    )

    assert dict(recorded.query) == {'a': 'one'}
    assert recorded.body == b''


# --- R12: query-parameter coercion (M6) -----------------------------------

@pytest.mark.parametrize(
    'value, expected',
    [
        pytest.param(True, 'true', id='true'),
        pytest.param(False, 'false', id='false'),
        pytest.param(None, '', id='none'),
        pytest.param('plain', 'plain', id='str'),
        pytest.param(17, '17', id='int'),
        pytest.param(1.5, '1.5', id='float'),
        pytest.param(
            date(2026, 8, 16), '2026-08-16', id='date'),
        pytest.param(
            datetime(2026, 8, 16, 9, 30, tzinfo=timezone.utc),
            '2026-08-16T09:30:00+00:00',
            id='datetime',
        ),
        pytest.param(time(9, 30), '09:30:00', id='time'),
        pytest.param({'deep': True}, '{"deep":true}', id='nested-dict'),
        pytest.param([1, 2], '[1,2]', id='nested-list'),
    ],
)
def test_every_query_value_type_has_a_defined_rendering(
    value: Any,
    expected: str,
) -> None:
    """``str(True)`` is ``'True'``; every API on earth expects ``'true'``."""
    assert coerce_query_value(value) == expected


def test_an_int_subclass_renders_as_its_digits_not_as_a_bool() -> None:
    """A value that is int-like and truthy still takes the ``int`` branch.

    ``bool`` is checked before ``int`` because ``bool`` *is* an ``int``;
    this pins that an ``int`` subclass does not fall into the bool branch
    and render as ``'true'``.

    It deliberately claims nothing about ``isinstance(x, bool)`` versus
    ``type(x) in [bool]``. That sub-point of M6 is unfalsifiable in
    CPython: ``bool`` cannot be subclassed, and ``numpy.bool_`` derives
    from ``np.generic`` rather than from ``bool``, so no value exists for
    which the two spellings differ and no test can distinguish them.
    """
    class Flag(int):
        """An int-like truthy value that is not a ``bool``."""

    assert coerce_query_value(True) == 'true'
    assert coerce_query_value(Flag(1)) == '1'


def test_a_top_level_list_becomes_a_repeated_parameter() -> None:
    """``?tag=a&tag=b`` -- what a multi-valued query parameter means."""
    assert build_query_params({'tag': ['a', 'b'], 'n': 1}) == [
        ('tag', 'a'), ('tag', 'b'), ('n', '1'),
    ]


async def test_a_bool_nested_two_levels_deep_is_rendered_as_json_true(
    http_server: RecordingHTTPServer,
) -> None:
    """The spec's nested-bool edge case, asserted on the wire."""
    recorded = await _dispatch(
        http_server,
        {'outer': {'inner': {'flag': True}}},
        request_type='GET',
        headers={'Content-Type': 'application/json'},
    )

    assert dict(recorded.query) == {'outer': '{"inner":{"flag":true}}'}


async def test_the_coerced_values_are_the_ones_that_reach_the_wire(
    http_server: RecordingHTTPServer,
) -> None:
    """Coercion asserted where it matters: the query string the server saw.

    ``aiohttp`` rejects a bare ``bool`` outright, so a filter that returned
    one would fail here rather than quietly send ``True``.
    """
    recorded = await _dispatch(
        http_server,
        {
            'flag': True,
            'off': False,
            'nothing': None,
            'when': date(2026, 8, 16),
            'tags': ['a', 'b'],
        },
        request_type='GET',
        headers={'Content-Type': 'application/json'},
    )

    assert sorted(recorded.query.items()) == [
        ('flag', 'true'),
        ('nothing', ''),
        ('off', 'false'),
        ('tags', 'a'),
        ('tags', 'b'),
        ('when', '2026-08-16'),
    ]


# --- R12: the caller's dict is not mutated (M6) ---------------------------

async def test_the_callers_payload_dict_is_unchanged_after_a_get(
    http_server: RecordingHTTPServer,
) -> None:
    """M6 mutated the caller's payload in place while coercing its bools.

    Asserted byte-for-byte as well as by equality, because the mutation
    replaced ``True`` with the *string* ``'True'`` and a looser comparison
    could be argued past.
    """
    payload = {
        'flag': True,
        'nothing': None,
        'nested': {'deep': {'flag': False}},
        'tags': ['a', 'b'],
    }
    before = deepcopy(payload)
    before_bytes = orjson.dumps(payload)

    await _dispatch(
        http_server,
        payload,
        request_type='GET',
        headers={'Content-Type': 'application/json'},
    )

    assert payload == before
    assert orjson.dumps(payload) == before_bytes


async def test_the_same_payload_survives_two_consecutive_calls(
    http_server: RecordingHTTPServer,
) -> None:
    """A caller reusing one config object gets the same request twice.

    The consequence of in-place mutation that a single-call test cannot
    see: the second call would have sent ``flag=True`` where the first
    sent ``flag=true``.
    """
    payload = {'flag': True}

    first = await _dispatch(
        http_server,
        payload,
        request_type='GET',
        headers={'Content-Type': 'application/json'},
    )
    second = await _dispatch(
        http_server,
        payload,
        request_type='GET',
        headers={'Content-Type': 'application/json'},
        path='/second',
    )

    assert dict(first.query) == dict(second.query) == {'flag': 'true'}


# --- R12: a GET with an upload config (L8) --------------------------------

async def test_a_get_with_a_file_upload_config_escapes_the_entry_point(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The dead ``if ...: pass`` branch, decided explicitly -- and escaping.

    Dropping a file the caller asked to send is the one outcome that is
    silently wrong, so the combination is rejected rather than guessed at.

    Asserted through ``request()`` rather than through
    ``HttpRequest.handle_request()``, because the layer below the entry
    point is not the layer the contract is written for: ``request()``
    catches ``AsyncGatewayError``, and ``ConfigurationError`` is one, so a
    rejection raised any later than construction would reach the caller as
    an ``ok=False`` envelope while every sibling configuration error
    escapes. A ``handle_request`` assertion cannot see that difference --
    it reads the answer one layer above where it is decided.

    The three properties the entry point documents for a configuration
    error are therefore asserted together: it escapes, it is not also
    logged, and nothing was dispatched.
    """
    upload = tmp_path / 'payload.bin'
    upload.write_bytes(b'data')
    http_server.respond('/upload', body=b'{}')
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    with pytest.raises(ConfigurationError) as raised:
        await entrypoint.request(
            url=http_server.url_for('/upload'),
            protocol='HTTP',
            protocol_info={
                'request_type': 'get',
                'http_file_upload_config': {
                    'local_filepath': str(upload), 'file_key': 'f'},
            },
        )

    assert 'http_file_upload_config' in str(raised.value)
    assert not http_server.requests
    assert [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ] == []


async def test_a_get_with_a_file_upload_config_is_refused_at_construction(
    tmp_path: Path,
) -> None:
    """The unit-level companion: the rejection is the constructor's.

    Pins *where* the check lives, which is what makes the escape above a
    property of the design rather than a coincidence -- the entry point
    builds the protocol object outside its own ``except`` block, so a
    constructor raise is the only one guaranteed to reach the caller. It
    also pins that nothing needs to be dispatched to find the mistake.
    """
    upload = tmp_path / 'payload.bin'
    upload.write_bytes(b'data')

    with pytest.raises(ConfigurationError) as raised:
        HttpRequest(
            'http://127.0.0.1/upload',
            None,
            {'payload': {}},
            info={
                'request_type': 'get',
                'http_file_upload_config': {
                    'local_filepath': str(upload), 'file_key': 'f'},
            },
            redact_params=frozenset(),
        )

    assert 'http_file_upload_config' in str(raised.value)


# --- R23: a client TLS context that is actually a client context ----------
#
# Six acceptance criteria, three findings (H28, M1, AGW-36) and one flagged
# integration risk (FI-2). The order below is the order the code runs in:
# normalise the caller's value, build the context, check the guard, decide
# the flag, then prove the whole thing against a real handshake.


@pytest.fixture
def tls() -> Any:
    """Yield a throwaway PKI for one test.

    Minting costs a handful of EC keypairs, so it is per-test rather than
    session-scoped: a shared PKI would let one test's
    ``load_verify_locations`` leak into another's context, and these tests
    assert on exactly what a context does and does not trust.

    Yields:
        The :class:`tests.fixtures.tls.TlsWorld` for this test.
    """
    with tls_world() as world:
        yield world


@pytest.fixture
def tls_server(tls: TlsWorld) -> Any:
    """Yield a running loopback server that requires client certificates.

    Args:
        tls: The PKI the server serves from and verifies clients against.

    Yields:
        The running :class:`tests.fixtures.tls.MutualTlsServer`.
    """
    with mutual_tls_server(tls) as server:
        yield server


async def _handshake(
    world: TlsWorld,
    server: MutualTlsServer,
    identity: CertificateFiles,
) -> aiohttp.ClientResponse:
    """Connect to ``server`` with ``identity``, through ``get_ssl_config``.

    The context is the library's own -- built by the code under test, not
    hand-assembled here -- which is the whole point: a test that built its
    own context would have passed against the ``CLIENT_AUTH`` defect.

    Args:
        world: The PKI, read for the CA the client must trust.
        server: The running loopback server.
        identity: The client certificate and key to present.

    Returns:
        The response, already read and released.

    Raises:
        aiohttp.ClientError: Whatever the handshake or request raised.
    """
    config = await get_ssl_config(identity.as_pair(), True)
    context = config['ssl']
    # The one thing the library cannot supply: this PKI's CA is not in any
    # system trust store, so the *server* side of verification has to be
    # told about it here. Every other property of the context -- purpose,
    # `check_hostname`, `verify_mode`, the loaded client chain -- comes
    # from `get_ssl_config`.
    context.load_verify_locations(cafile=world.ca_file)

    async with aiohttp.ClientSession() as session:
        async with session.get(server.url, ssl=context) as response:
            await response.read()
            return response


# --- R23-AC1: the purpose is SERVER_AUTH, and it can open a client socket -


async def test_r23_ac1_the_context_is_built_for_the_server_purpose(
    tls: TlsWorld,
) -> None:
    """H28, at the unit: a client context, not a server one.

    ``ssl.Purpose.CLIENT_AUTH`` yields ``PROTOCOL_TLS_SERVER``, and
    CPython refuses outright to build a client socket from one -- so the
    client-certificate path did not merely verify weakly, it had never
    once completed. The protocol is asserted rather than the purpose,
    because ``Purpose`` is not recoverable from a built context and the
    protocol is the property that actually decides whether a client
    socket can exist.
    """
    config = await get_ssl_config(tls.client.as_pair(), True)
    context = config['ssl']

    assert isinstance(context, ssl.SSLContext)
    assert context.protocol == ssl.PROTOCOL_TLS_CLIENT
    assert context.get_ca_certs() != []


def test_r23_ac1_a_client_socket_can_be_built_from_the_context(
    tls: TlsWorld,
) -> None:
    """The failure H28 actually produced, reproduced on both contexts.

    ``wrap_socket(server_side=False)`` is what every outbound connection
    does, and it is the exact call CPython refuses for a
    ``PROTOCOL_TLS_SERVER`` context: ``ssl.SSLError: Cannot create a
    client socket with a PROTOCOL_TLS_SERVER context``. The pre-fix
    context is built here alongside the real one so the discrimination is
    visible in a single test -- the fixed context wraps, the defective
    one raises -- rather than the "it works" half being asserted against
    nothing.

    No handshake is attempted: an unconnected ``socketpair`` end is
    enough to prove the context is the right *kind*, and connecting would
    be testing the network.
    """
    context = build_client_ssl_context(
        tls.client.certificate, tls.client.key)
    defective = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)

    fixed_end, defective_end = socket.socketpair()
    try:
        wrapped = context.wrap_socket(
            fixed_end,
            server_side=False,
            do_handshake_on_connect=False,
            server_hostname='127.0.0.1')
        assert wrapped.server_side is False
        wrapped.detach()

        with pytest.raises(ssl.SSLError) as raised:
            defective.wrap_socket(
                defective_end,
                server_side=False,
                do_handshake_on_connect=False,
                server_hostname='127.0.0.1')
        assert 'PROTOCOL_TLS_SERVER' in str(raised.value)
    finally:
        fixed_end.close()
        defective_end.close()


# --- R23-AC2: the guard, and why it is a raise rather than an assert ------


def test_r23_ac2_the_returned_context_verifies_the_peer(
    tls: TlsWorld,
) -> None:
    """Both properties the guard checks, asserted on the real return.

    ``check_hostname`` and ``verify_mode`` are what stand between a
    client certificate and handing that identity to whoever answered the
    socket. They are asserted here so the *criterion* has a test, and
    enforced in the code so a future refactor cannot quietly weaken them.
    """
    context = build_client_ssl_context(
        tls.client.certificate, tls.client.key)

    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_r23_ac2_a_weakened_context_is_refused_before_it_is_returned(
    monkeypatch: pytest.MonkeyPatch,
    tls: TlsWorld,
) -> None:
    """The guard fires, rather than merely being written down.

    A guard nothing has ever tripped is a comment. This drives the exact
    regression it exists for -- a future edit that turns verification off
    after building the context -- by substituting a factory that returns
    an unverifying context, and requires the refusal to happen *before*
    the context is returned to anyone.
    """
    # Captured before the patch: the substitute calls the real factory,
    # and reading it through the module after patching would recurse.
    real_factory = ssl.create_default_context

    def unverifying_context(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        """Return a context that authenticates no peer.

        Args:
            args: The purpose, ignored.
            kwargs: Unused.

        Returns:
            A client context with verification switched off, which is
            what a weakening refactor would leave behind.
        """
        context = real_factory(ssl.Purpose.SERVER_AUTH)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    monkeypatch.setattr(
        'async_gateway.helpers.internal.filters_helper.'
        'ssl.create_default_context',
        unverifying_context)

    with pytest.raises(ConfigurationError) as raised:
        build_client_ssl_context(tls.client.certificate, tls.client.key)

    assert 'check_hostname' in str(raised.value)
    assert 'verify_mode' in str(raised.value)


#: A program that reports two facts about the interpreter running it:
#: whether ``assert`` statements survive, and whether the R23 guard
#: fires. Run twice by
#: :func:`test_r23_ac2_the_guard_survives_python_dash_o`, once optimised
#: and once not, so the second fact can be shown to be independent of the
#: first. It weakens the context by substituting the factory
#: ``filters_helper`` calls -- the module holds ``ssl`` itself, so
#: rebinding the attribute is what the code under test then sees.
GUARD_PROBE = """
import ssl
import sys

from async_gateway.helpers.internal.filters_helper import (
    build_client_ssl_context)
from async_gateway.utils.exceptions import ConfigurationError

asserts_live = False
try:
    assert False
except AssertionError:
    asserts_live = True
print('ASSERTS-LIVE' if asserts_live else 'ASSERTS-STRIPPED')

real = ssl.create_default_context


def weakened(*args, **kwargs):
    context = real(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


ssl.create_default_context = weakened
try:
    build_client_ssl_context(sys.argv[1], sys.argv[2])
except ConfigurationError:
    print('GUARD-HELD')
else:
    print('GUARD-STRIPPED')
"""


def test_r23_ac2_the_guard_survives_python_dash_o(tls: TlsWorld) -> None:
    """The deviation from AC2's literal wording, proven necessary.

    R23-AC2 asks the implementation to **assert** ``check_hostname`` and
    ``verify_mode``. It raises instead, which is strictly stronger, and
    this is the evidence for that claim rather than an assurance of it:
    ``python -O`` strips ``assert`` statements from the bytecode
    entirely, so a security guard written as one is present in
    development and **absent in exactly the optimised deployments that
    most need it**.

    Both interpreters are run because either result alone proves nothing.
    ``GUARD-HELD`` under ``-O`` could just mean ``-O`` was not in effect;
    ``ASSERTS-STRIPPED`` alongside it is what rules that out. The
    unoptimised run is the control: it shows the same program reports
    ``ASSERTS-LIVE``, so the difference is the flag and not the program.

    A subprocess is unavoidable -- optimisation is fixed at interpreter
    start and the pytest process is not optimised.
    """
    plain = _run_guard_probe([], tls.client)
    optimised = _run_guard_probe(['-O'], tls.client)

    assert plain == ['ASSERTS-LIVE', 'GUARD-HELD']
    assert optimised == ['ASSERTS-STRIPPED', 'GUARD-HELD']


def _run_guard_probe(
    flags: list[str],
    identity: CertificateFiles,
) -> list[str]:
    """Run :data:`GUARD_PROBE` in a subprocess and return its output lines.

    Args:
        flags: Interpreter flags, e.g. ``['-O']``.
        identity: The certificate pair the probe builds a context from.

    Returns:
        The probe's stdout, split into stripped lines.
    """
    completed = subprocess.run(
        [sys.executable, *flags, '-c', GUARD_PROBE,
         identity.certificate, identity.key],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(Path(__file__).resolve().parents[2]),
    )

    assert completed.returncode == 0, completed.stderr
    return completed.stdout.split()


# --- R23-AC3: the answer is under `ssl=`, never `ssl_context=` ------------


async def test_r23_ac3_the_certificate_branch_answers_under_the_ssl_key(
    tls: TlsWorld,
) -> None:
    """The deprecated key is gone from the branch that emitted it.

    ``ssl_context=`` is the only key the certificate branch ever used, and
    it is the one ``aiohttp`` deprecated. Asserting the exact key set --
    not merely that ``'ssl'`` is present -- is what makes this a
    migration rather than an addition.
    """
    config = await get_ssl_config(tls.client.as_pair(), True)

    assert set(config) == {'ssl'}
    assert isinstance(config['ssl'], ssl.SSLContext)


@pytest.mark.parametrize(
    'verify_ssl',
    [
        pytest.param(True, id='verification-on'),
        pytest.param(False, id='verification-off'),
        pytest.param(None, id='flag-absent'),
    ],
)
async def test_r23_ac3_every_branch_answers_under_the_same_one_key(
    verify_ssl: Any,
) -> None:
    """One key, always ``ssl``, whichever way the decision goes.

    The transport unpacks this mapping straight into the request call, so
    a branch that answered under a different key would silently
    contribute a keyword nobody reads -- which is precisely how the
    ``ssl_context``/``ssl`` split let FTP negotiate plaintext (M2).
    """
    config = await get_ssl_config(None, verify_ssl)

    assert set(config) == {'ssl'}


async def test_r23_ac3_the_context_reaches_the_connector_under_ssl(
    tls: TlsWorld,
    tls_server: MutualTlsServer,
) -> None:
    """The keyword actually passed to the transport, not just returned.

    AC3 asks for an assertion on what the connector receives. A recorded
    keyword would prove the call shape; a completed handshake proves the
    connector *used* it, which is strictly more -- an ignored keyword
    cannot authenticate anybody.
    """
    seen: dict[str, Any] = {}
    real_request = aiohttp.ClientSession._request

    async def record(self: Any, *args: Any, **kwargs: Any) -> Any:
        """Record the TLS keywords, then dispatch unchanged.

        Args:
            self: The session.
            args: The method and URL.
            kwargs: The request keywords, recorded.

        Returns:
            Whatever the real ``_request`` returned.
        """
        seen.update(kwargs)
        return await real_request(self, *args, **kwargs)

    config = await get_ssl_config(tls.client.as_pair(), True)
    config['ssl'].load_verify_locations(cafile=tls.ca_file)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(aiohttp.ClientSession, '_request', record)
        async with aiohttp.ClientSession() as session:
            async with session.get(tls_server.url, **config) as response:
                assert response.status == 200

    assert isinstance(seen['ssl'], ssl.SSLContext)
    assert 'ssl_context' not in seen


# --- R23-AC4: verify_ssl is a decision, three ways ------------------------


async def test_r23_ac4_an_absent_flag_verifies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default is on, and silent.

    ``verify_ssl or True`` also produced ``True`` here, so this is the one
    row that did not change behaviour -- which is exactly why it needs
    pinning. The obvious M1 cleanup, ``{'ssl': verify_ssl}``, turns this
    row into ``None``, and ``ssl=None`` is how aiohttp is told to use its
    default: harmless there, but the same value is how ``aioftp`` is told
    to speak *plaintext*. A test that only covered ``True`` and ``False``
    would have waved that through.
    """
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    assert await get_ssl_config() == {'ssl': True}
    assert await get_ssl_config(None, None) == {'ssl': True}
    assert [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ] == []


async def test_r23_ac4_verify_ssl_true_verifies() -> None:
    """The explicit ``True`` is honoured as itself."""
    assert await get_ssl_config(None, True) == {'ssl': True}


async def test_r23_ac4_verify_ssl_false_disables_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M1: the flag now does what it says, and says that it did.

    ``{'ssl': verify_ssl or True}`` returned ``True`` for ``False`` --
    the documented flag was inoperative, and the library's safe posture
    was an accident of Python truthiness rather than a decision. Turning
    verification off is the one configuration that leaves the connection
    open to an intercepting peer, so it is logged at ``warning``: an
    operator reading the log is the only person who can notice it.
    """
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    assert await get_ssl_config(None, False) == {'ssl': False}

    warnings = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
        and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert 'verify_ssl' in warnings[0].getMessage()


@pytest.mark.parametrize(
    'falsy',
    [
        pytest.param(0, id='zero'),
        pytest.param('', id='empty-string'),
        pytest.param([], id='empty-list'),
        pytest.param(None, id='none'),
    ],
)
async def test_r23_ac4_only_the_false_singleton_disables_verification(
    falsy: Any,
) -> None:
    """Falsy is not consent: the test is identity, not truthiness.

    This is the trap M1 sets for the fix. The natural repair of
    ``verify_ssl or True`` is ``not verify_ssl``, which reads the same and
    hands an unauthenticated connection to every caller who passed ``0``,
    ``''`` or ``None`` -- none of whom asked for one. Only the ``False``
    singleton turns verification off, and there is no other path to
    ``check_hostname=False`` or ``CERT_NONE`` anywhere in the module.
    """
    assert await get_ssl_config(None, falsy) == {'ssl': True}


async def test_r23_ac4_a_certificate_overrides_verify_ssl_false(
    tls: TlsWorld,
) -> None:
    """Fail-secure where the two options contradict each other.

    ``certificate`` plus ``verify_ssl=False`` asks for two incompatible
    things: present my identity, and do not check whose hands I am
    presenting it into. Honouring the flag would hand a client
    certificate to whoever answered the socket, which is worse than the
    plain unverified session the flag asked for -- so the certificate
    wins and the connection verifies. A caller who genuinely wants no
    verification gets it by supplying no certificate.
    """
    config = await get_ssl_config(tls.client.as_pair(), False)

    assert isinstance(config['ssl'], ssl.SSLContext)
    assert config['ssl'].verify_mode == ssl.CERT_REQUIRED
    assert config['ssl'].check_hostname is True


# --- R23-AC5 / FI-2: the live mutual-TLS handshake ------------------------


async def test_r23_ac5_a_client_certificate_completes_a_real_handshake(
    tls: TlsWorld,
    tls_server: MutualTlsServer,
) -> None:
    """FI-2: the path that had never once succeeded, succeeding.

    A real loopback server with ``verify_mode=CERT_REQUIRED``, a real
    throwaway PKI, and the context the library itself built. Nothing here
    is mocked, because the defect was invisible to every level above the
    socket: the pre-fix context is a perfectly ordinary object, and only
    OpenSSL knows it cannot be a client.
    """
    response = await _handshake(tls, tls_server, tls.client)

    assert response.status == 200


async def test_r23_ac5_a_certificate_from_another_ca_fails_as_tls(
    tls: TlsWorld,
    tls_server: MutualTlsServer,
) -> None:
    """The server's rejection is real, and classified as ``TLS``.

    Without this row the success above proves only that *something*
    connected -- a server that ignored client certificates entirely would
    pass it. The wrong certificate must be refused, and refused as a TLS
    failure rather than as a connection problem, because those route
    differently: TLS is the caller's certificate, ``CONNECT`` suggests
    the network.
    """
    with pytest.raises(aiohttp.ClientSSLError):
        await _handshake(tls, tls_server, tls.rogue)


async def test_r23_ac5_an_expired_certificate_fails_as_tls(
    tls: TlsWorld,
    tls_server: MutualTlsServer,
) -> None:
    """Signed by the right CA, and still refused, because it has expired.

    Distinct from the rogue row: this certificate chains to the trusted
    CA, so it isolates *validity* from *provenance*. A server that
    checked only the issuer would pass this and reject the rogue, and the
    two rows together are what rule that out.
    """
    with pytest.raises(aiohttp.ClientSSLError):
        await _handshake(tls, tls_server, tls.expired)


async def test_r23_ac5_a_tls_failure_reaches_the_caller_as_a_tls_envelope(
    tls: TlsWorld,
    tls_server: MutualTlsServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the rejected handshake becomes ``TLS``/502.

    The unit rows above prove the handshake behaves; this proves nothing
    downstream swallows or reclassifies it. Driven through the public
    entry point, because ``error['code']`` is what a caller actually
    branches on.

    ``SSL_CERT_FILE`` is how the throwaway CA reaches a context the
    library builds internally -- the entry point offers no seam for a
    trust store, and inventing one for a test would be testing a
    different library.
    """
    monkeypatch.setenv('SSL_CERT_FILE', tls.ca_file)

    result = await entrypoint.request(
        url=tls_server.url,
        protocol='HTTPS',
        protocol_info={
            'request_type': 'GET',
            'certificate': tls.rogue.as_pair(),
            'timeout': 10,
        },
    )

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'TLS'
    assert result['status_code'] == 502


async def test_r23_ac5_a_good_certificate_reaches_the_caller_as_success(
    tls: TlsWorld,
    tls_server: MutualTlsServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole feature, from ``request()`` to a 200, over real mTLS.

    The single most load-bearing test of this story: client-certificate
    authentication has never worked, and this is the assertion that says
    it does.
    """
    monkeypatch.setenv('SSL_CERT_FILE', tls.ca_file)

    result = await entrypoint.request(
        url=tls_server.url,
        protocol='HTTPS',
        protocol_info={
            'request_type': 'GET',
            'certificate': tls.client.as_pair(),
            'timeout': 10,
        },
    )

    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['error'] is None


# --- R23-AC6: an unusable certificate is the caller's mistake -------------


def test_r23_ac6_a_missing_file_is_a_configuration_error_naming_it(
    tmp_path: Path,
) -> None:
    """``ConfigurationError`` naming the file, not a bare ``SSLError``.

    OpenSSL's own answer to a missing file is ``FileNotFoundError(2, 'No
    such file or directory')`` with ``filename`` unset -- it does not say
    *which* file, and there are two candidates. The message therefore
    names both paths itself.
    """
    missing = tmp_path / 'absent.pem'

    with pytest.raises(ConfigurationError) as raised:
        build_client_ssl_context(str(missing), str(missing))

    assert str(missing) in str(raised.value)


def test_r23_ac6_an_unreadable_file_is_a_configuration_error(
    tls: TlsWorld,
    tmp_path: Path,
) -> None:
    """A permission failure is configuration too, not a transport fault.

    ``PermissionError`` is an ``OSError`` and would otherwise be read by
    the FTP client's transport table as a refused *connection* -- a
    ``CONNECT`` envelope for a call that never left the process.
    """
    unreadable = tmp_path / 'locked.pem'
    unreadable.write_bytes(Path(tls.client.certificate).read_bytes())
    unreadable.chmod(0)
    try:
        with pytest.raises(ConfigurationError) as raised:
            build_client_ssl_context(str(unreadable), tls.client.key)
    finally:
        unreadable.chmod(0o600)

    assert str(unreadable) in str(raised.value)


def test_r23_ac6_a_mismatched_key_is_a_configuration_error(
    tls: TlsWorld,
) -> None:
    """A valid certificate and a valid key that are not a pair.

    Both files load individually; only together are they wrong, which is
    why this cannot be caught by checking either one exists. OpenSSL
    reports ``[X509: KEY_VALUES_MISMATCH]``, which is an ``ssl.SSLError``
    -- itself an ``OSError``, so one ``except OSError`` catches it and
    the missing-file case both.
    """
    with pytest.raises(ConfigurationError) as raised:
        build_client_ssl_context(
            tls.mismatched.certificate, tls.mismatched.key)

    assert tls.mismatched.key in str(raised.value)


def test_r23_ac6_a_file_that_is_not_a_pem_is_a_configuration_error(
    tls: TlsWorld,
) -> None:
    """A file that exists and is not a certificate."""
    with pytest.raises(ConfigurationError) as raised:
        build_client_ssl_context(tls.garbage.certificate, tls.garbage.key)

    assert tls.garbage.certificate in str(raised.value)


def test_r23_ac6_a_passphrase_protected_key_names_the_limitation(
    tls: TlsWorld,
) -> None:
    """The unsupported case says it is unsupported.

    OpenSSL's answer to an encrypted key with no passphrase is
    ``ssl.SSLError: [SSL] PEM lib`` -- byte-identical to what a *corrupt*
    PEM produces, so a caller reading it cannot tell "decrypt your key"
    from "your file is damaged". The ``password`` callback fires only for
    a key OpenSSL cannot decode unaided, so its invocation is a reliable
    detector, and it lets the message name the actual limitation.
    """
    with pytest.raises(ConfigurationError) as raised:
        build_client_ssl_context(
            tls.encrypted_key.certificate, tls.encrypted_key.key)

    assert 'passphrase' in str(raised.value)
    assert tls.encrypted_key.key in str(raised.value)


def test_r23_ac6_an_unencrypted_key_never_invokes_the_callback(
    tls: TlsWorld,
) -> None:
    """The detector must not fire on the ordinary case.

    If OpenSSL called the ``password`` callback unconditionally, every
    plain key would be reported as passphrase-protected and the feature
    would be unusable. Pinning the negative is what makes the positive
    above meaningful.
    """
    context = build_client_ssl_context(
        tls.client.certificate, tls.client.key)

    assert context.verify_mode == ssl.CERT_REQUIRED


async def test_r23_ac6_an_unloadable_certificate_is_config_not_tls(
    tmp_path: Path,
) -> None:
    """The error contract, at the boundary the callers see.

    ``ConfigurationError`` is 400 and never retried; ``TlsError`` is 502
    and sits in the retriable transport family. A certificate path that
    does not exist cannot be fixed by trying again, so it must be the
    former -- and this is the assertion that keeps a future refactor from
    quietly moving it back.
    """
    missing = str(tmp_path / 'absent.pem')

    with pytest.raises(ConfigurationError) as raised:
        await get_ssl_config((missing, missing), True)

    assert raised.value.code == 'CONFIG'
    assert raised.value.status_code == 400


# --- R23 edge cases: what `certificate` may and may not be ----------------


@pytest.mark.parametrize(
    ('certificate', 'expected'),
    [
        pytest.param('/one/path.pem', 'str', id='a-single-string-path'),
        pytest.param(b'/one/path.pem', 'bytes', id='a-single-bytes-path'),
        pytest.param(Path('/one/path.pem'), 'PosixPath', id='a-single-path'),
    ],
)
def test_a_single_path_is_rejected_by_type_not_by_length(
    certificate: Any,
    expected: str,
) -> None:
    """The type is named, because the length is a lie for these.

    A string and a ``bytes`` both have a length and both iterate, so a
    length check alone answers ``b'ab'`` with "got 2 value(s)" -- which
    reads as *accepted* -- and answers a 17-character path with "got 17
    value(s)", which names the wrong problem entirely. Reporting the type
    tells the caller what to change; reporting the arity tells them to
    count their filename.
    """
    with pytest.raises(ConfigurationError) as raised:
        normalised_certificate(certificate)

    assert expected in str(raised.value)


@pytest.mark.parametrize(
    'certificate',
    [
        pytest.param(('only.pem',), id='one-element'),
        pytest.param(('a.pem', 'b.key', 'c.pem'), id='three-elements'),
        pytest.param((), id='empty'),
    ],
)
def test_a_pair_that_is_not_a_pair_reports_how_many_it_got(
    certificate: Any,
) -> None:
    """For a genuine sequence, the arity *is* the useful fact."""
    with pytest.raises(ConfigurationError) as raised:
        normalised_certificate(certificate)

    assert f'{len(certificate)} value(s)' in str(raised.value)


@pytest.mark.parametrize(
    'certificate',
    [
        pytest.param(5, id='an-int'),
        pytest.param({'cert': 'a.pem'}, id='a-mapping'),
        pytest.param(object(), id='an-arbitrary-object'),
    ],
)
def test_a_non_sequence_certificate_is_rejected_by_type(
    certificate: Any,
) -> None:
    """Nothing indexable-looking is assumed to be indexable.

    The pre-fix code indexed ``certificate[0]`` and ``[1]`` unguarded, so
    an int raised ``TypeError`` and a one-element tuple raised
    ``IndexError`` -- neither of which is an ``OSError``, so both escaped
    the FTP client's TLS handler raw and reached the caller as an
    exception rather than an envelope.
    """
    with pytest.raises(ConfigurationError) as raised:
        normalised_certificate(certificate)

    assert type(certificate).__name__ in str(raised.value)


def test_an_element_that_is_not_a_path_names_which_end_is_wrong() -> None:
    """A two-element pair can still hold the wrong kind of thing."""
    with pytest.raises(ConfigurationError) as raised:
        normalised_certificate(('cert.pem', 42))

    assert 'key path' in str(raised.value)
    assert 'int' in str(raised.value)


def test_a_pathlib_path_pair_is_accepted(tls: TlsWorld) -> None:
    """``load_cert_chain`` takes ``os.PathLike`` natively, so this does.

    Refusing a ``pathlib.Path`` would invent a restriction OpenSSL does
    not have, and ``pathlib`` is how most modern code spells a path.
    ``os.fspath`` converts it -- pure string manipulation, no filesystem
    access, which is why it sits in the blocking-call scan's
    ``PURE_PATH_HELPERS`` allowlist.
    """
    pair = normalised_certificate(
        (Path(tls.client.certificate), Path(tls.client.key)))

    assert pair == (tls.client.certificate, tls.client.key)
    assert all(isinstance(path, str) for path in pair)


async def test_a_pathlib_path_pair_survives_the_whole_call(
    tls: TlsWorld,
) -> None:
    """And the acceptance is end-to-end, not only in the normaliser."""
    config = await get_ssl_config(
        (Path(tls.client.certificate), Path(tls.client.key)), True)

    assert isinstance(config['ssl'], ssl.SSLContext)


def test_a_single_pem_holding_both_certificate_and_key_is_accepted(
    tls: TlsWorld,
    tmp_path: Path,
) -> None:
    """One file named twice, which is a shape OpenSSL supports.

    A combined PEM is common enough that refusing it would be a
    regression against a working configuration; naming it as both
    elements of the pair is how ``load_cert_chain`` is told to read both
    from one file.
    """
    combined = tmp_path / 'combined.pem'
    combined.write_bytes(
        Path(tls.client.certificate).read_bytes()
        + Path(tls.client.key).read_bytes())

    context = build_client_ssl_context(str(combined), str(combined))

    assert context.verify_mode == ssl.CERT_REQUIRED


# --- AGW-36: the blocking reads are off the event loop --------------------


async def test_agw36_the_certificate_is_loaded_off_the_event_loop(
    tls: TlsWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thread identity is the assertion, because nothing else is.

    ``ssl.create_default_context`` reads the whole system CA bundle (194
    certificates) and ``load_cert_chain`` reads the caller's two files.
    On HTTP this coroutine runs inside ``failsafe.run``, so on the loop
    that CA-bundle read repeated on *every retried attempt* of every
    certificate-configured request.

    ``asyncio.to_thread`` is what moves it, and no observable output
    changes when it is removed -- the context is identical either way.
    The only thing that differs is which thread the work ran on, so that
    is what is measured. The blocking-call scan in
    ``tests/test_no_blocking_io.py`` covers the sibling half (the
    function must stay a plain ``def``); this covers the call site, which
    the scan structurally cannot see.
    """
    ran_on: list[int] = []
    real_build = build_client_ssl_context

    def record_thread(
        certificate_path: str,
        key_path: str,
    ) -> ssl.SSLContext:
        """Record the running thread, then build normally.

        Args:
            certificate_path: Forwarded unchanged.
            key_path: Forwarded unchanged.

        Returns:
            The context the real builder produced.
        """
        ran_on.append(threading.get_ident())
        return real_build(certificate_path, key_path)

    monkeypatch.setattr(
        'async_gateway.helpers.internal.filters_helper.'
        'build_client_ssl_context',
        record_thread)

    await get_ssl_config(tls.client.as_pair(), True)

    assert len(ran_on) == 1
    assert ran_on[0] != threading.get_ident()


async def test_agw36_the_loop_keeps_running_while_the_context_is_built(
    tls: TlsWorld,
) -> None:
    """The property the thread identity exists to buy.

    Thread identity is a proxy; this is the thing itself. Another task
    scheduled alongside the build must get to run before it finishes --
    which is exactly what a blocking read on the loop prevents, and what
    made the CA-bundle read cost every other in-flight request.
    """
    progressed = asyncio.Event()

    async def other_work() -> None:
        """Set the flag as soon as the loop gives this task a turn.

        Returns:
            None.
        """
        progressed.set()

    companion = asyncio.create_task(other_work())
    await get_ssl_config(tls.client.as_pair(), True)
    await companion

    assert progressed.is_set()


def test_agw36_the_blocking_builder_is_a_plain_def() -> None:
    """The placement the executor contract depends on, pinned.

    ``asyncio.to_thread`` on a coroutine function does not run it -- it
    returns the coroutine object from the thread, unawaited, and the
    blocking work never happens at all. Rewriting this as an ``async
    def`` would also put a banned ``ssl.`` call back inside a coroutine,
    which ``tests/test_no_blocking_io.py`` fails on; the two checks
    approach the same regression from opposite sides.
    """
    assert not inspect.iscoroutinefunction(build_client_ssl_context)


async def test_agw36_the_system_ca_failure_is_not_reclassified(
    tls: TlsWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken trust store is the environment's fault, not the caller's.

    ``ssl.create_default_context`` sits deliberately *outside* the
    builder's ``try``: everything inside reads the caller's own two
    files, and every way those fail is configuration. The system CA
    bundle is neither, so its ``OSError`` propagates and the protocol
    clients report it as ``TLS`` -- which is the one ``OSError`` the FTP
    client's handler still exists to catch.
    """
    def broken_trust_store(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        """Fail the way an unreadable CA bundle fails.

        Args:
            args: The purpose, ignored.
            kwargs: Unused.

        Returns:
            Never; this always raises.

        Raises:
            OSError: Always.
        """
        raise OSError(2, 'No such file or directory')

    monkeypatch.setattr(
        'async_gateway.helpers.internal.filters_helper.'
        'ssl.create_default_context',
        broken_trust_store)

    with pytest.raises(OSError) as raised:
        await get_ssl_config(tls.client.as_pair(), True)

    assert not isinstance(raised.value, ConfigurationError)


# --- R23: the absence criteria, as greps -----------------------------------


def test_the_server_purpose_appears_in_no_executable_code() -> None:
    """R23-AC1's grep criterion, as a check that runs.

    ``Purpose.CLIENT_AUTH`` survives in two ``ftp_client.py`` docstrings
    that *explain* the defect, and deleting the explanation would be a
    loss. So the criterion is enforced where it means something: no
    executable statement in the package may name it. Parsing rather than
    grepping is what distinguishes the two.
    """
    package = Path(__file__).resolve().parents[2] / 'async_gateway'
    scanned = sorted(package.rglob('*.py'))
    offenders = [
        f'{path.relative_to(package).as_posix()}:{node.lineno}'
        for path in scanned
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
        if isinstance(node, ast.Attribute) and node.attr == 'CLIENT_AUTH'
    ]

    assert scanned, f'no modules scanned under {package}'
    assert offenders == []


def test_no_expression_in_the_package_is_or_ed_with_a_literal_true() -> None:
    """M1's absence criterion, generalised from a string to a shape.

    A grep for the literal ``verify_ssl or True`` would pass while the
    same bug lived under any other name, and would *fail* on the two
    docstrings that explain the defect -- ``filters_helper``'s module
    header and ``get_ssl_config``'s own, both of which are worth keeping.

    So the criterion is enforced as the defect's shape instead: ``<any
    expression> or True`` is unconditionally ``True``, which makes it a
    bug wherever it is written and never a legitimate construct. The AST
    sees only executable code, so the prose survives.
    """
    package = Path(__file__).resolve().parents[2] / 'async_gateway'
    scanned = sorted(package.rglob('*.py'))
    offenders = [
        f'{path.relative_to(package).as_posix()}:{node.lineno}'
        for path in scanned
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
        and any(
            isinstance(value, ast.Constant) and value.value is True
            for value in node.values[1:])
    ]

    assert scanned, f'no modules scanned under {package}'
    assert offenders == []

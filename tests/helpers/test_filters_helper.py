"""Tests for request construction: serialisation (R3) and dispatch (R12).

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
"""

import json
import logging
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
    build_query_params,
    coerce_query_value,
    form_x_www_form_urlencoded_filters,
    is_get,
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

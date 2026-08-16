"""Tests for JSON serialisation on the request side (spec R3, Step 4).

Covers three of the four former ``ujson`` call sites: the two in
``filters_helper`` that build a request body, and the ``json_serialize``
wiring in ``logic/http_client.py`` that hands a serialiser to ``aiohttp``. (The
fourth, the response decoder, is in ``test_response_helper.py``.)

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
from urllib.parse import parse_qs

import aiohttp

from async_gateway.helpers.internal.filters_helper import (
    application_json_filters,
    form_x_www_form_urlencoded_filters,
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
    filters = await form_x_www_form_urlencoded_filters(data)
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

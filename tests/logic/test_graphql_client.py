"""Contract tests for the GraphQL query/mutation adapter."""

import asyncio
from typing import Any

import aiohttp

import orjson

import pytest

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.logic import protocol_mapping
from asyncio_gateway.logic.graphql_client import (
    GRAPHQL_ACCEPT,
    GraphqlRequest,
)
from asyncio_gateway.logic.http_client import default_json_serialize
from asyncio_gateway.utils.envelope import GatewayResponse, new_envelope
from asyncio_gateway.utils.exceptions import ConfigurationError
from asyncio_gateway.utils.redaction import REDACTED

from tests.fixtures.http_server import RecordingHTTPServer, ResponseSpec

JSON = {'Content-Type': 'application/json'}
GRAPHQL_JSON = {'Content-Type': 'application/graphql-response+json'}


@pytest.fixture(autouse=True)
def register_graphql(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the strategy for focused tests without owning the registry."""
    monkeypatch.setitem(protocol_mapping, 'GRAPHQL', GraphqlRequest)


async def _graphql(
    http_server: RecordingHTTPServer,
    response_body: Any,
    *,
    data: Any = None,
    info: dict[str, Any] | None = None,
    status: int = 200,
    headers: dict[str, str] | None = None,
    path: str = '/graphql',
) -> GatewayResponse:
    """Make one focused GraphQL call against a canned peer response.

    Args:
        http_server: The loopback recording server.
        response_body: JSON-safe peer response, or bytes to send verbatim.
        data: Caller variables.
        info: Extra/overridden protocol options.
        status: Peer HTTP status.
        headers: Peer headers, defaulting to legacy JSON.
        path: Target path.

    Returns:
        The final gateway envelope.
    """
    body = (response_body if isinstance(response_body, bytes)
            else orjson.dumps(response_body))
    http_server.respond(
        path,
        method='POST',
        status=status,
        body=body,
        headers=JSON if headers is None else headers,
    )
    protocol_info: dict[str, Any] = {
        'query': 'query { viewer { id } }',
        **({} if info is None else info),
    }
    return await request(
        http_server.url_for(path),
        protocol='GRAPHQL',
        data=data,
        protocol_info=protocol_info,
    )


async def test_graphql_posts_the_exact_query_variables_and_operation(
    http_server: RecordingHTTPServer,
) -> None:
    """The adapter emits the frozen query/mutation JSON envelope."""
    http_server.respond(
        '/graphql',
        method='POST',
        body=orjson.dumps({'data': {'widget': {'id': 'w1'}}}),
        headers=JSON,
    )

    result = await request(
        http_server.url_for('/graphql'),
        protocol='GRAPHQL',
        data={'id': 'w1'},
        protocol_info={
            'query': 'query Widget($id: ID!) { widget(id: $id) { id } }',
            'operation_name': 'Widget',
        },
    )

    assert result['ok'] is True
    assert orjson.loads(http_server.requests[0].body) == {
        'query': 'query Widget($id: ID!) { widget(id: $id) { id } }',
        'operationName': 'Widget',
        'variables': {'id': 'w1'},
    }
    assert http_server.requests[0].headers.getall('Content-Type') == [
        'application/json']
    assert http_server.requests[0].headers.getall('Accept') == [
        GRAPHQL_ACCEPT]
    assert result['protocol_details'] == {
        'data': {'widget': {'id': 'w1'}},
    }


@pytest.mark.parametrize(
    'data, info, expected',
    [
        pytest.param(
            None,
            {'query': ' query { ping } '},
            {'query': ' query { ping } '},
            id='omit-optionals',
        ),
        pytest.param(
            {},
            {'query': 'mutation M { run }', 'operation_name': 'M'},
            {
                'query': 'mutation M { run }',
                'operationName': 'M',
                'variables': {},
            },
            id='empty-variables',
        ),
    ],
)
async def test_optional_members_use_presence_not_truthiness(
    http_server: RecordingHTTPServer,
    data: Any,
    info: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    """None omits optional members while an empty mapping is transmitted."""
    result = await _graphql(
        http_server,
        {'data': {'ok': True}},
        data=data,
        info=info,
    )

    assert result['ok'] is True
    assert orjson.loads(http_server.requests[0].body) == expected


async def test_graphql_http_features_and_media_headers_are_canonical(
    http_server: RecordingHTTPServer,
) -> None:
    """Auth/cookies survive while duplicate media headers are collapsed."""
    http_server.respond(
        '/graphql',
        method='POST',
        body=orjson.dumps({'data': {'ok': True}}),
        headers=GRAPHQL_JSON,
    )

    result = await request(
        http_server.url_for('/graphql'),
        protocol='GRAPHQL',
        auth=aiohttp.BasicAuth('user', 'password'),
        data={},
        protocol_info={
            'query': 'query { ok }',
            'headers': {
                'content-type': 'Application/JSON; charset=utf-8',
                'Content-Type': 'application/json',
                'accept': (
                    ', application/json;profile=default;q=0.8, '
                    'application/graphql-response+json;q=1.0'),
                'Accept': GRAPHQL_ACCEPT,
                'X-Caller': 'kept',
            },
            'cookies': {'session': 'cookie'},
        },
    )

    sent = http_server.requests[0]
    assert result['ok'] is True
    assert sent.headers.getall('Content-Type') == ['application/json']
    assert sent.headers.getall('Accept') == [GRAPHQL_ACCEPT]
    assert sent.headers['X-Caller'] == 'kept'
    assert sent.headers['Authorization'].startswith('Basic ')
    assert sent.headers['Cookie'] == 'session=cookie'


@pytest.mark.parametrize('query', [None, 1, '', '   '])
async def test_invalid_queries_fail_before_io(
    http_server: RecordingHTTPServer,
    query: Any,
) -> None:
    """A GraphQL document is required but deliberately not parsed."""
    with pytest.raises(ConfigurationError, match='query'):
        await request(
            http_server.url_for('/graphql'),
            protocol='GRAPHQL',
            protocol_info={'query': query},
        )
    assert http_server.requests == []


@pytest.mark.parametrize('operation_name', [None, 1, '', '   '])
async def test_invalid_supplied_operation_names_fail_before_io(
    http_server: RecordingHTTPServer,
    operation_name: Any,
) -> None:
    """An explicitly supplied operation name must be non-empty text."""
    with pytest.raises(ConfigurationError, match='operation_name'):
        await request(
            http_server.url_for('/graphql'),
            protocol='GRAPHQL',
            protocol_info={
                'query': 'query { ok }',
                'operation_name': operation_name,
            },
        )
    assert http_server.requests == []


@pytest.mark.parametrize('data', [[], 1, True, 'x', b'x'])
async def test_invalid_variables_fail_before_io(
    http_server: RecordingHTTPServer,
    data: Any,
) -> None:
    """Variables are a mapping or omitted, never positional or scalar."""
    with pytest.raises(ConfigurationError, match='mapping or None'):
        await request(
            http_server.url_for('/graphql'),
            protocol='GRAPHQL',
            data=data,
            protocol_info={'query': 'query { ok }'},
        )
    assert http_server.requests == []


async def test_nested_nonserialisable_variables_fail_before_io(
    http_server: RecordingHTTPServer,
) -> None:
    """Variable serialization is preflighted before opening a socket."""
    with pytest.raises(ConfigurationError, match='JSON-serialisable'):
        await request(
            http_server.url_for('/graphql'),
            protocol='GRAPHQL',
            data={'nested': {'bad': object()}},
            protocol_info={'query': 'query { ok }'},
        )
    assert http_server.requests == []


@pytest.mark.parametrize(
    'headers',
    [
        pytest.param({'Content-Type': 'text/plain'}, id='content-type'),
        pytest.param({'Accept': 'application/json'}, id='legacy-only'),
        pytest.param(
            {'Accept': 'application/graphql-response+json'},
            id='preferred-only',
        ),
        pytest.param(
            {'Accept': (
                'application/graphql-response+json;q=0, '
                'application/json;q=0')},
            id='both-quality-zero',
        ),
        pytest.param(
            {'Accept': (
                'application/graphql-response+json;q=0, '
                'application/json')},
            id='preferred-quality-zero',
        ),
        pytest.param(
            {'Accept': (
                'application/graphql-response+json;q=bogus, '
                'application/json')},
            id='malformed-quality',
        ),
        pytest.param(
            {'Accept': (
                'application/graphql-response+json;q=1.1, '
                'application/json')},
            id='out-of-range-quality',
        ),
        pytest.param(
            {'Accept': (
                'text/plain;profile="x,application/json;y=z,'
                'application/graphql-response+json;y=z"')},
            id='quoted-media-name-bypass',
        ),
        pytest.param(
            {'Accept': (
                'application/json;profile=";q=0", '
                'application/graphql-response+json')},
            id='quoted-q-like-parameter',
        ),
        pytest.param(
            {'Accept': (
                'application/json;profile=x\\y, '
                'application/graphql-response+json')},
            id='escaped-parameter',
        ),
    ],
)
async def test_conflicting_media_headers_fail_before_io(
    http_server: RecordingHTTPServer,
    headers: dict[str, str],
) -> None:
    """Caller headers cannot weaken the fixed request/response contract."""
    with pytest.raises(ConfigurationError):
        await request(
            http_server.url_for('/graphql'),
            protocol='GRAPHQL',
            protocol_info={'query': 'query { ok }', 'headers': headers},
        )
    assert http_server.requests == []


async def test_split_accept_occurrences_are_combined_then_canonicalized(
    http_server: RecordingHTTPServer,
) -> None:
    """Case-variant fields jointly advertise the two response types."""
    result = await _graphql(
        http_server,
        {'data': {'ok': True}},
        info={'headers': {
            'Accept': 'application/json;q=0.5',
            'accept': 'application/graphql-response+json;q=0.75',
        }},
    )

    assert result['ok'] is True
    assert http_server.requests[0].headers.getall('Accept') == [
        GRAPHQL_ACCEPT]


@pytest.mark.parametrize(
    'forbidden_key, value',
    [
        pytest.param('session', object(), id='session'),
        pytest.param('serialization', orjson.dumps, id='serializer'),
        pytest.param('allow_redirects', True, id='redirect-policy'),
        pytest.param('request_type', 'GET', id='verb'),
        pytest.param('subscriptions', True, id='subscriptions'),
    ],
)
async def test_transport_and_unsupported_mode_knobs_fail_before_io(
    http_server: RecordingHTTPServer,
    forbidden_key: str,
    value: Any,
) -> None:
    """Caller session defaults cannot alter the semantic exchange."""
    with pytest.raises(ConfigurationError, match='unknown key'):
        await request(
            http_server.url_for('/graphql'),
            protocol='GRAPHQL',
            protocol_info={
                'query': 'query { ok }',
                forbidden_key: value,
            },
        )
    assert http_server.requests == []


async def test_missing_query_and_url_userinfo_fail_before_io(
    http_server: RecordingHTTPServer,
) -> None:
    """The required document and credential boundary are synchronous."""
    with pytest.raises(ConfigurationError, match='missing required'):
        await request(
            http_server.url_for('/graphql'),
            protocol='GRAPHQL',
            protocol_info={},
        )
    target = http_server.url_for('/graphql').replace(
        'http://', 'http://user:secret@', 1)
    with pytest.raises(ConfigurationError, match='must not contain user info'):
        await request(
            target,
            protocol='GRAPHQL',
            protocol_info={'query': 'query { ok }'},
        )
    assert http_server.requests == []


def test_direct_none_info_is_a_typed_configuration_refusal() -> None:
    """The annotated optional constructor input never leaks a KeyError."""
    envelope = new_envelope(
        url='https://host/graphql',
        protocol='GRAPHQL',
        payload=None,
    )
    with pytest.raises(ConfigurationError):
        GraphqlRequest(
            'https://host/graphql',
            None,
            envelope,
            None,
            redact_params=(),
        )


@pytest.mark.parametrize('headers', [JSON, GRAPHQL_JSON])
async def test_valid_data_succeeds_under_both_supported_media_types(
    http_server: RecordingHTTPServer,
    headers: dict[str, str],
) -> None:
    """Preferred and legacy JSON response media types both parse at 2xx."""
    data = {'viewer': {'id': 'u1'}}
    result = await _graphql(
        http_server,
        {'data': data},
        headers={**headers, 'X-Response': 'kept',
                 'Authorization': 'secret', 'Set-Cookie': 'a=1'},
    )

    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['json'] == {'data': data}
    assert result['protocol_details'] == {'data': data}
    assert result['headers']['X-Response'] == 'kept'
    assert result['headers']['Authorization'] == REDACTED
    assert result['cookies'] == {'a': REDACTED}


@pytest.mark.parametrize(
    'body',
    [
        pytest.param({'data': {'ok': True}}, id='data'),
        pytest.param(
            {'errors': [{'message': 'failed'}]},
            id='errors',
        ),
    ],
)
async def test_unsupported_json_media_type_is_graphql_protocol_at_2xx(
    http_server: RecordingHTTPServer,
    body: dict[str, Any],
) -> None:
    """A generic ``+json`` type cannot masquerade as GraphQL at 2xx."""
    result = await _graphql(
        http_server,
        body,
        headers={'Content-Type': 'application/problem+json'},
    )

    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error'] is not None
    assert result['error']['code'] == 'GRAPHQL_PROTOCOL'
    assert result['json'] == body


@pytest.mark.parametrize(
    'response',
    [
        pytest.param(
            {'errors': [{'message': 'failed'}]},
            id='errors-only',
        ),
        pytest.param(
            {'data': None, 'errors': [{'message': 'not found'}]},
            id='null-data-with-error',
        ),
        pytest.param(
            {
                'data': {'widget': None},
                'errors': [{
                    'message': 'resolver failed',
                    'path': ['widget'],
                    'locations': [{'line': 1, 'column': 3}],
                    'extensions': {'code': 'UPSTREAM'},
                }],
            },
            id='partial-data-rich-error',
        ),
    ],
)
async def test_graphql_errors_preserve_partial_data_and_safe_details(
    http_server: RecordingHTTPServer,
    response: dict[str, Any],
) -> None:
    """Any valid error list is semantic while retaining the whole body."""
    result = await _graphql(http_server, response)

    assert result['ok'] is False
    assert result['status_code'] == 200
    assert result['error'] is not None
    assert result['error']['code'] == 'GRAPHQL_ERROR'
    assert result['json'] == response
    expected = {key: response[key] for key in ('data', 'errors')
                if key in response}
    assert result['protocol_details'] == expected


@pytest.mark.parametrize('status', [400, 500])
async def test_preferred_media_type_error_wins_over_http_failure(
    http_server: RecordingHTTPServer,
    status: int,
) -> None:
    """The new GraphQL response type gives valid errors semantic priority."""
    response = {
        'data': None,
        'errors': [{'message': 'failed'}],
    }
    result = await _graphql(
        http_server,
        response,
        status=status,
        headers=GRAPHQL_JSON,
    )

    assert result['error'] is not None
    assert result['error']['code'] == 'GRAPHQL_ERROR'
    assert result['status_code'] == status
    assert result['protocol_details'] == response


@pytest.mark.parametrize(
    'headers, body',
    [
        pytest.param(
            JSON,
            {'data': None, 'errors': [{'message': 'legacy'}]},
            id='legacy-valid-error',
        ),
        pytest.param(
            GRAPHQL_JSON,
            {'data': {'ok': True}},
            id='preferred-valid-data',
        ),
        pytest.param(
            GRAPHQL_JSON,
            {'errors': []},
            id='preferred-malformed-error',
        ),
        pytest.param(
            {'Content-Type': 'text/plain'},
            b'not graphql json',
            id='other-media',
        ),
        pytest.param(
            {'Content-Type': 'application/problem+json'},
            {'errors': [{'message': 'not a GraphQL media type'}]},
            id='other-json-media',
        ),
    ],
)
async def test_non_2xx_without_preferred_valid_errors_is_http_status(
    http_server: RecordingHTTPServer,
    headers: dict[str, str],
    body: Any,
) -> None:
    """Legacy, success, malformed, and other bodies never mask HTTP failure."""
    result = await _graphql(
        http_server,
        body,
        status=500,
        headers=headers,
    )

    assert result['error'] is not None
    assert result['error']['code'] == 'HTTP_STATUS'
    assert result['status_code'] == 500


@pytest.mark.parametrize(
    'body',
    [
        pytest.param([], id='array'),
        pytest.param(None, id='null'),
        pytest.param('scalar', id='scalar'),
        pytest.param({}, id='neither-key'),
        pytest.param({'data': None}, id='null-data-without-errors'),
        pytest.param({'data': []}, id='list-data'),
        pytest.param({'data': 'value'}, id='scalar-data'),
        pytest.param({'errors': []}, id='empty-errors'),
        pytest.param({'errors': None}, id='null-errors'),
        pytest.param({'errors': {}}, id='object-errors'),
        pytest.param({'errors': ['bad']}, id='non-object-error'),
        pytest.param({'errors': [{}]}, id='missing-message'),
        pytest.param({'errors': [{'message': ''}]}, id='empty-message'),
        pytest.param({'errors': [{'message': '   '}]}, id='blank-message'),
        pytest.param({'errors': [{'message': 3}]}, id='non-string-message'),
        pytest.param({'data': {'x': 1}, 'errors': []},
                     id='data-with-malformed-errors'),
    ],
)
async def test_malformed_2xx_responses_are_graphql_protocol_errors(
    http_server: RecordingHTTPServer,
    body: Any,
) -> None:
    """Every invalid GraphQL envelope branch is typed and status 502."""
    result = await _graphql(http_server, body)

    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error'] is not None
    assert result['error']['code'] == 'GRAPHQL_PROTOCOL'


@pytest.mark.parametrize(
    'body, headers',
    [
        pytest.param(b'{broken', JSON, id='malformed-json'),
        pytest.param(b'not-json', {'Content-Type': 'text/plain'},
                     id='non-json-media'),
        pytest.param(b'', GRAPHQL_JSON, id='empty-json-body'),
    ],
)
async def test_invalid_2xx_bodies_are_graphql_protocol_errors(
    http_server: RecordingHTTPServer,
    body: bytes,
    headers: dict[str, str],
) -> None:
    """Decode failures and absent semantic JSON are protocol faults."""
    result = await _graphql(http_server, body, headers=headers)

    assert result['error'] is not None
    assert result['error']['code'] == 'GRAPHQL_PROTOCOL'
    assert result['text'] == body.decode()


async def test_malformed_json_at_non_2xx_remains_http_status(
    http_server: RecordingHTTPServer,
) -> None:
    """A decode failure cannot mask the failed HTTP status."""
    result = await _graphql(
        http_server,
        b'{broken',
        status=500,
        headers=GRAPHQL_JSON,
    )

    assert result['error'] is not None
    assert result['error']['code'] == 'HTTP_STATUS'
    assert result['status_code'] == 500


async def test_redirect_is_not_followed_and_preferred_error_is_semantic(
    http_server: RecordingHTTPServer,
) -> None:
    """A 307 cannot replay a mutation at the Location target."""
    http_server.respond(
        '/start',
        method='POST',
        status=307,
        body=orjson.dumps({'errors': [{'message': 'move'}]}),
        headers={
            **GRAPHQL_JSON,
            'Location': http_server.url_for('/target'),
        },
    )
    http_server.respond('/target', method='POST', body=b'wrong')

    result = await request(
        http_server.url_for('/start'),
        protocol='GRAPHQL',
        protocol_info={'query': 'mutation { run }'},
    )

    assert result['error'] is not None
    assert result['error']['code'] == 'GRAPHQL_ERROR'
    assert result['status_code'] == 307
    assert [seen.path for seen in http_server.requests] == ['/start']


async def test_legacy_redirect_response_is_http_status_and_not_followed(
    http_server: RecordingHTTPServer,
) -> None:
    """The 3xx HTTP branch is explicit even though base HTTP starts at 4xx."""
    http_server.respond(
        '/start',
        method='POST',
        status=307,
        body=orjson.dumps({'errors': [{'message': 'move'}]}),
        headers={**JSON, 'Location': http_server.url_for('/target')},
    )

    result = await request(
        http_server.url_for('/start'),
        protocol='GRAPHQL',
        protocol_info={'query': 'mutation { run }'},
    )

    assert result['error'] is not None
    assert result['error']['code'] == 'HTTP_STATUS'
    assert result['status_code'] == 307
    assert [seen.path for seen in http_server.requests] == ['/start']


async def test_malformed_redirect_body_is_still_http_status(
    http_server: RecordingHTTPServer,
) -> None:
    """A 3xx decode failure cannot escape as SERIALIZATION."""
    result = await _graphql(
        http_server,
        b'{broken',
        status=307,
        headers=GRAPHQL_JSON,
    )

    assert result['error'] is not None
    assert result['error']['code'] == 'HTTP_STATUS'
    assert result['status_code'] == 307


@pytest.mark.parametrize('replacement', [{}])
async def test_preprocessor_can_supply_empty_variables(
    http_server: RecordingHTTPServer,
    replacement: dict[str, Any],
) -> None:
    """The semantic wrapper reads variables after the preprocessor."""
    async def replace(response: GatewayResponse) -> str:
        """Replace the raw payload while preserving the envelope shape.

        Args:
            response: The live request envelope.

        Returns:
            A marker proving the callback ran.
        """
        response['payload'] = replacement
        return 'replaced'

    http_server.respond(
        '/graphql',
        method='POST',
        body=orjson.dumps({'data': {'ok': True}}),
        headers=JSON,
    )

    result = await request(
        http_server.url_for('/graphql'),
        protocol='GRAPHQL',
        data=None,
        protocol_info={'query': 'query { ok }'},
        pre_processor_config={'function': replace},
    )

    assert result['pre_processor_response'] == 'replaced'
    assert orjson.loads(http_server.requests[0].body)['variables'] == {}


async def test_response_cap_is_inherited_from_http_transport(
    http_server: RecordingHTTPServer,
) -> None:
    """The semantic adapter cannot bypass the streaming byte cap."""
    result = await _graphql(
        http_server,
        {'data': {'large': 'response'}},
        info={'max_response_bytes': 8},
    )

    assert result['error'] is not None
    assert result['error']['code'] == 'RESPONSE_TOO_LARGE'


async def test_retryable_timeout_replays_one_identical_post(
    http_server: RecordingHTTPServer,
) -> None:
    """One configured transport retry yields exactly two identical sends."""
    answer = orjson.dumps({'data': {'ok': True}})
    http_server.respond_in_sequence('/graphql', [
        ResponseSpec(
            body=answer,
            headers=tuple(JSON.items()),
            delay=0.05,
        ),
        ResponseSpec(body=answer, headers=tuple(JSON.items())),
    ], method='POST')

    result = await request(
        http_server.url_for('/graphql'),
        protocol='GRAPHQL',
        data={'id': 1},
        protocol_info={
            'query': 'mutation Run($id: Int!) { run(id: $id) }',
            'timeout': 0.01,
            'circuit_breaker_config': {
                'retry_config': {
                    'name': 'graphql-timeout',
                    'allowed_retries': 1,
                    'delay': 0,
                },
            },
        },
    )

    attempts = http_server.requests
    assert result['ok'] is True
    assert len(attempts) == 2
    assert attempts[0].body == attempts[1].body


async def test_graphql_error_is_not_retried_or_counted(
    http_server: RecordingHTTPServer,
) -> None:
    """Application errors occur after the breaker and leave it healthy."""
    http_server.respond_in_sequence('/graphql', [
        ResponseSpec(
            body=orjson.dumps({'errors': [{'message': 'first'}]}),
            headers=tuple(JSON.items()),
        ),
        ResponseSpec(
            body=orjson.dumps({'data': {'ok': True}}),
            headers=tuple(JSON.items()),
        ),
    ], method='POST')
    breaker = {
        'maximum_failures': 1,
        'retry_config': {
            'name': 'graphql-app-error',
            'allowed_retries': 3,
            'delay': 0,
        },
    }
    info = {
        'query': 'query { ok }',
        'circuit_breaker_config': breaker,
    }

    failed = await request(
        http_server.url_for('/graphql'),
        protocol='GRAPHQL',
        protocol_info=info,
    )
    healthy = await request(
        http_server.url_for('/graphql'),
        protocol='GRAPHQL',
        protocol_info=info,
    )

    assert failed['error'] is not None
    assert failed['error']['code'] == 'GRAPHQL_ERROR'
    assert healthy['ok'] is True
    assert len(http_server.requests) == 2


async def test_owned_session_uses_library_serializer_and_closes(
    http_server: RecordingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter creates one policy-owned session and closes it."""
    created: list[aiohttp.ClientSession] = []
    constructor_options: list[dict[str, Any]] = []

    class RecordingSession(aiohttp.ClientSession):
        """Session double recording constructor policy and lifecycle."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Forward construction and retain its options.

            Args:
                args: Positional session arguments.
                kwargs: Keyword session arguments.
            """
            constructor_options.append(dict(kwargs))
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(
        'asyncio_gateway.logic.http_client.aiohttp.ClientSession',
        RecordingSession,
    )

    result = await _graphql(http_server, {'data': {'ok': True}})

    assert result['ok'] is True
    assert len(created) == 1
    assert created[0].closed is True
    assert constructor_options[0]['json_serialize'] is default_json_serialize
    assert getattr(created[0], '_raise_for_status') is False


async def test_cancellation_propagates_and_closes_owned_session(
    http_server: RecordingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation is not converted into an envelope or session leak."""
    created: list[aiohttp.ClientSession] = []

    class RecordingSession(aiohttp.ClientSession):
        """Session double retained so closure can be asserted."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Forward construction and retain the session.

            Args:
                args: Positional session arguments.
                kwargs: Keyword session arguments.
            """
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(
        'asyncio_gateway.logic.http_client.aiohttp.ClientSession',
        RecordingSession,
    )
    http_server.gate('/graphql', method='POST')
    task = asyncio.create_task(request(
        http_server.url_for('/graphql'),
        protocol='GRAPHQL',
        protocol_info={'query': 'query { wait }'},
    ))

    async def wait_for_request() -> None:
        """Yield until the loopback server receives the request."""
        while not http_server.requests:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_request(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(created) == 1
    assert created[0].closed is True


async def test_query_and_nested_variable_secrets_are_redacted(
    http_server: RecordingHTTPServer,
) -> None:
    """Semantic wrapping does not create a credential echo surface."""
    http_server.respond(
        '/graphql',
        method='POST',
        body=orjson.dumps({'data': {'ok': True}}),
        headers=JSON,
    )

    result = await request(
        f'{http_server.url_for("/graphql")}?token=query-secret',
        protocol='GRAPHQL',
        data={'nested': {'password': 'variable-secret'}},
        protocol_info={
            'query': 'query($nested: Input!) { ok }',
            'redact_query_params': ['token'],
        },
    )

    rendered = repr(result)
    assert result['ok'] is True
    assert 'query-secret' not in rendered
    assert 'variable-secret' not in rendered
    assert result['payload']['variables']['nested']['password'] == REDACTED

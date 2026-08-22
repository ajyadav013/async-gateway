"""Contract tests for the JSON-RPC 2.0 single-call adapter."""

import asyncio
import json
from typing import Any

import aiohttp

import orjson

import pytest

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.logic import protocol_mapping
from asyncio_gateway.logic.http_client import default_json_serialize
from asyncio_gateway.logic.jsonrpc_client import (
    JsonRpcRequest,
    validated_jsonrpc_response,
)
from asyncio_gateway.utils.envelope import GatewayResponse, new_envelope
from asyncio_gateway.utils.exceptions import (
    ConfigurationError,
    JsonRpcProtocolError,
)
from asyncio_gateway.utils.redaction import REDACTED

from tests.fixtures.http_server import RecordingHTTPServer

JSON = {'Content-Type': 'application/json'}


@pytest.fixture(autouse=True)
def register_jsonrpc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the strategy for focused tests without owning the registry."""
    monkeypatch.setitem(protocol_mapping, 'JSONRPC', JsonRpcRequest)


async def _rpc(
    http_server: RecordingHTTPServer,
    response_body: Any,
    *,
    data: Any = None,
    info: dict[str, Any] | None = None,
    status: int = 200,
    headers: dict[str, str] | None = None,
    path: str = '/rpc',
) -> GatewayResponse:
    """Make one focused JSON-RPC call against a canned peer response.

    Args:
        http_server: The loopback recording server.
        response_body: JSON-safe peer response, or bytes to send verbatim.
        data: Caller params.
        info: Extra/overridden protocol options.
        status: Peer HTTP status.
        headers: Peer headers, defaulting to JSON.
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
        'method': 'sum',
        'request_id': 7,
        **({} if info is None else info),
    }
    return await request(
        url=http_server.url_for(path),
        protocol='JSONRPC',
        data=data,
        protocol_info=protocol_info,
    )


async def test_jsonrpc_posts_the_exact_single_call_envelope(
    http_server: RecordingHTTPServer,
) -> None:
    """The selector sends one POST with version, method, id, and params."""
    http_server.respond(
        '/rpc',
        method='POST',
        body=orjson.dumps({'jsonrpc': '2.0', 'id': 7, 'result': 'ok'}),
        headers=JSON,
    )

    result: GatewayResponse = await request(
        url=http_server.url_for('/rpc'),
        protocol='JSONRPC',
        data={'value': 3},
        protocol_info={'method': 'sum', 'request_id': 7},
    )

    assert result['ok'] is True
    assert len(http_server.requests) == 1
    recorded = http_server.requests[0]
    assert recorded.method == 'POST'
    assert orjson.loads(recorded.body) == {
        'jsonrpc': '2.0',
        'method': 'sum',
        'id': 7,
        'params': {'value': 3},
    }
    assert recorded.headers.getall('Content-Type') == ['application/json']
    assert result['protocol_details'] == {'id': 7, 'result': 'ok'}


@pytest.mark.parametrize(
    'data, request_id, expected_params',
    [
        pytest.param(None, 0, None, id='omitted'),
        pytest.param({}, '', {}, id='empty-mapping'),
        pytest.param([], 'items', [], id='empty-list'),
        pytest.param([1, 2], 9, [1, 2], id='positional-list'),
    ],
)
async def test_params_presence_uses_none_not_truthiness(
    http_server: RecordingHTTPServer,
    data: Any,
    request_id: str | int,
    expected_params: Any,
) -> None:
    """Empty params and empty/zero ids remain distinct from omission."""
    result = await _rpc(
        http_server,
        {'jsonrpc': '2.0', 'id': request_id, 'result': None},
        data=data,
        info={'method': ' spaced.method ', 'request_id': request_id},
    )

    sent = orjson.loads(http_server.requests[0].body)
    assert sent['method'] == ' spaced.method '
    assert sent['id'] == request_id
    if expected_params is None:
        assert 'params' not in sent
    else:
        assert sent['params'] == expected_params
    assert result['protocol_details'] == {
        'id': request_id,
        'result': None,
    }


async def test_json_headers_auth_and_cookies_reach_the_wire_canonically(
    http_server: RecordingHTTPServer,
) -> None:
    """HTTP features survive while duplicate media-type spellings do not."""
    http_server.respond(
        '/rpc',
        method='POST',
        body=orjson.dumps({'jsonrpc': '2.0', 'id': 7, 'result': True}),
        headers=JSON,
    )

    result = await request(
        http_server.url_for('/rpc'),
        protocol='JSONRPC',
        auth=aiohttp.BasicAuth('user', 'password'),
        data={},
        protocol_info={
            'method': 'ping',
            'request_id': 7,
            'headers': {
                'content-type': 'Application/JSON; charset=utf-8',
                'Content-Type': 'application/json',
                'X-Caller': 'kept',
            },
            'cookies': {'session': 'cookie'},
        },
    )

    sent = http_server.requests[0]
    assert result['ok'] is True
    assert sent.headers.getall('Content-Type') == ['application/json']
    assert sent.headers['X-Caller'] == 'kept'
    assert sent.headers['Authorization'].startswith('Basic ')
    assert sent.headers['Cookie'] == 'session=cookie'


@pytest.mark.parametrize(
    'method',
    [None, 1, '', '   ', 'rpc.discover'],
)
async def test_invalid_methods_fail_before_io(
    http_server: RecordingHTTPServer,
    method: Any,
) -> None:
    """Method shape and the reserved prefix are constructor refusals."""
    with pytest.raises(ConfigurationError):
        await request(
            http_server.url_for('/rpc'),
            protocol='JSONRPC',
            protocol_info={'method': method, 'request_id': 1},
        )
    assert http_server.requests == []


@pytest.mark.parametrize(
    'request_id',
    [None, True, False, 1.5, [], {}],
)
async def test_invalid_request_ids_fail_before_io(
    http_server: RecordingHTTPServer,
    request_id: Any,
) -> None:
    """Notifications, booleans, and compound ids are outside the contract."""
    with pytest.raises(ConfigurationError):
        await request(
            http_server.url_for('/rpc'),
            protocol='JSONRPC',
            protocol_info={'method': 'ping', 'request_id': request_id},
        )
    assert http_server.requests == []


@pytest.mark.parametrize('request_id', [-(2 ** 63) - 1, 2 ** 64])
async def test_integer_ids_outside_the_json_wire_domain_fail_before_io(
    http_server: RecordingHTTPServer,
    request_id: int,
) -> None:
    """Integers that cannot round-trip through the JSON engine are refused."""
    with pytest.raises(ConfigurationError, match='64-bit JSON wire range'):
        await request(
            http_server.url_for('/rpc'),
            protocol='JSONRPC',
            protocol_info={'method': 'ping', 'request_id': request_id},
        )
    assert http_server.requests == []


@pytest.mark.parametrize('request_id', [-(2 ** 63), (2 ** 64) - 1])
async def test_integer_id_wire_boundaries_round_trip_exactly(
    http_server: RecordingHTTPServer,
    request_id: int,
) -> None:
    """Both inclusive 64-bit JSON serializer boundaries remain integers."""
    result = await _rpc(
        http_server,
        {'jsonrpc': '2.0', 'id': request_id, 'result': True},
        info={'request_id': request_id},
    )

    assert result['ok'] is True
    assert orjson.loads(http_server.requests[0].body)['id'] == request_id


@pytest.mark.parametrize('data', [1, True, 'x', b'x', (1, 2)])
async def test_invalid_params_fail_before_io(
    http_server: RecordingHTTPServer,
    data: Any,
) -> None:
    """Only named or positional JSON-RPC params containers are admitted."""
    with pytest.raises(ConfigurationError, match='mapping, list, or None'):
        await request(
            http_server.url_for('/rpc'),
            protocol='JSONRPC',
            data=data,
            protocol_info={'method': 'ping', 'request_id': 1},
        )
    assert http_server.requests == []


async def test_nested_nonserialisable_params_fail_before_io(
    http_server: RecordingHTTPServer,
) -> None:
    """Serialization is preflighted before aiohttp opens a connection."""
    with pytest.raises(ConfigurationError, match='JSON-serialisable'):
        await request(
            http_server.url_for('/rpc'),
            protocol='JSONRPC',
            data={'nested': {'bad': object()}},
            protocol_info={'method': 'ping', 'request_id': 1},
        )
    assert http_server.requests == []


async def test_missing_required_or_unknown_options_fail_before_io(
    http_server: RecordingHTTPServer,
) -> None:
    """The closed selector contract refuses missing ids and raw body knobs."""
    with pytest.raises(ConfigurationError, match='missing required'):
        await request(
            http_server.url_for('/rpc'),
            protocol='JSONRPC',
            protocol_info={'method': 'ping'},
        )
    with pytest.raises(ConfigurationError, match='unknown key'):
        await request(
            http_server.url_for('/rpc'),
            protocol='JSONRPC',
            protocol_info={
                'method': 'ping',
                'request_id': 1,
                'batch': True,
            },
        )
    assert http_server.requests == []


@pytest.mark.parametrize(
    'forbidden_key, value',
    [
        pytest.param('session', object(), id='session'),
        pytest.param('serialization', orjson.dumps, id='serializer'),
        pytest.param('allow_redirects', True, id='redirect-policy'),
        pytest.param('request_type', 'PUT', id='verb'),
        pytest.param('batch', True, id='batch-mode'),
    ],
)
async def test_transport_override_knobs_are_refused_before_io(
    http_server: RecordingHTTPServer,
    forbidden_key: str,
    value: Any,
) -> None:
    """Session defaults cannot alter or replay the semantic request."""
    with pytest.raises(ConfigurationError, match='unknown key'):
        await request(
            http_server.url_for('/rpc'),
            protocol='JSONRPC',
            protocol_info={
                'method': 'ping',
                'request_id': 1,
                forbidden_key: value,
            },
        )
    assert http_server.requests == []


async def test_userinfo_and_conflicting_content_type_fail_before_io(
    http_server: RecordingHTTPServer,
) -> None:
    """Credentials stay out of URLs and the body cannot be mislabeled."""
    target = http_server.url_for('/rpc').replace(
        'http://', 'http://user:secret@', 1)
    with pytest.raises(ConfigurationError, match='must not contain user info'):
        await request(
            target,
            protocol='JSONRPC',
            protocol_info={'method': 'ping', 'request_id': 1},
        )
    with pytest.raises(ConfigurationError, match='Content-Type'):
        await request(
            http_server.url_for('/rpc'),
            protocol='JSONRPC',
            protocol_info={
                'method': 'ping',
                'request_id': 1,
                'headers': {'Content-Type': 'text/plain'},
            },
        )
    assert http_server.requests == []


def test_direct_none_info_is_a_typed_configuration_refusal() -> None:
    """The annotated optional constructor input never leaks a KeyError."""
    envelope = new_envelope(
        url='https://host/rpc',
        protocol='JSONRPC',
        payload=None,
    )
    with pytest.raises(ConfigurationError):
        JsonRpcRequest(
            'https://host/rpc',
            None,
            envelope,
            None,
            redact_params=(),
        )


@pytest.mark.parametrize('rpc_result', [{'x': 1}, 'value', 3, None])
async def test_valid_results_preserve_all_http_material(
    http_server: RecordingHTTPServer,
    rpc_result: Any,
) -> None:
    """Object, scalar, and null results are successful peer answers."""
    result = await _rpc(
        http_server,
        {'jsonrpc': '2.0', 'id': 7, 'result': rpc_result},
        headers={**JSON, 'X-Response': 'kept', 'Set-Cookie': 'a=1'},
    )

    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['json'] == {
        'jsonrpc': '2.0',
        'id': 7,
        'result': rpc_result,
    }
    assert result['headers']['X-Response'] == 'kept'
    assert result['cookies'] == {'a': REDACTED}
    assert result['protocol_details'] == {'id': 7, 'result': rpc_result}


@pytest.mark.parametrize(
    'status, error',
    [
        pytest.param(200, {'code': 0, 'message': ''}, id='empty-values'),
        pytest.param(
            500,
            {'code': -32000, 'message': 'failed', 'data': None},
            id='http-failure-with-null-data',
        ),
        pytest.param(
            200,
            {'code': 9, 'message': 'bad', 'data': {'why': 'x'}},
            id='structured-data',
        ),
    ],
)
async def test_valid_rpc_errors_win_and_preserve_the_complete_error(
    http_server: RecordingHTTPServer,
    status: int,
    error: dict[str, Any],
) -> None:
    """A valid error is semantic at both successful and failed HTTP status."""
    result = await _rpc(
        http_server,
        {'jsonrpc': '2.0', 'id': 7, 'error': error},
        status=status,
    )

    assert result['ok'] is False
    assert result['status_code'] == status
    assert result['error'] is not None
    assert result['error']['code'] == 'JSONRPC_ERROR'
    assert result['protocol_details'] == {'id': 7, 'error': error}


@pytest.mark.parametrize('code', [-(2 ** 63), (2 ** 64) - 1])
async def test_rpc_error_code_wire_boundaries_are_valid(
    http_server: RecordingHTTPServer,
    code: int,
) -> None:
    """Both inclusive exact-integer boundaries retain error semantics."""
    error = {'code': code, 'message': 'boundary'}
    result = await _rpc(
        http_server,
        {'jsonrpc': '2.0', 'id': 7, 'error': error},
    )

    assert result['error'] is not None
    assert result['error']['code'] == 'JSONRPC_ERROR'
    assert result['protocol_details'] == {'id': 7, 'error': error}


@pytest.mark.parametrize('code', [-(2 ** 63) - 1, 2 ** 64])
async def test_rpc_error_codes_outside_the_wire_domain_are_protocol_errors(
    http_server: RecordingHTTPServer,
    code: int,
) -> None:
    """A peer number the shared JSON engine cannot retain is malformed."""
    body = json.dumps({
        'jsonrpc': '2.0',
        'id': 7,
        'error': {'code': code, 'message': 'outside'},
    }).encode()
    result = await _rpc(http_server, body)

    assert result['error'] is not None
    assert result['error']['code'] == 'JSONRPC_PROTOCOL'


@pytest.mark.parametrize('code', [-(2 ** 63) - 1, 2 ** 64])
def test_response_validator_refuses_out_of_range_python_integer_codes(
    code: int,
) -> None:
    """Direct helper use enforces the same domain as decoded wire JSON."""
    with pytest.raises(JsonRpcProtocolError, match='64-bit JSON wire range'):
        validated_jsonrpc_response({
            'jsonrpc': '2.0',
            'id': 7,
            'error': {'code': code, 'message': 'outside'},
        }, 7)


@pytest.mark.parametrize(
    'body',
    [
        pytest.param({'jsonrpc': '2.0', 'id': 7, 'result': 'ok'},
                     id='valid-result'),
        pytest.param({'jsonrpc': '2.0', 'id': 8,
                      'error': {'code': 1, 'message': 'bad'}},
                     id='wrong-id-error'),
        pytest.param(b'{broken', id='malformed-json'),
    ],
)
async def test_http_failure_wins_without_a_valid_matching_rpc_error(
    http_server: RecordingHTTPServer,
    body: Any,
) -> None:
    """Only a structurally valid matching error can outrank HTTP failure."""
    result = await _rpc(http_server, body, status=500)

    assert result['ok'] is False
    assert result['status_code'] == 500
    assert result['error'] is not None
    assert result['error']['code'] == 'HTTP_STATUS'


@pytest.mark.parametrize(
    'body',
    [
        pytest.param([], id='array'),
        pytest.param(None, id='null'),
        pytest.param('scalar', id='scalar'),
        pytest.param({}, id='missing-version'),
        pytest.param({'jsonrpc': '1.0', 'id': 7, 'result': 1},
                     id='wrong-version'),
        pytest.param({'jsonrpc': '2.0', 'result': 1}, id='missing-id'),
        pytest.param({'jsonrpc': '2.0', 'id': True, 'result': 1},
                     id='equal-value-wrong-id-type'),
        pytest.param({'jsonrpc': '2.0', 'id': 8, 'result': 1},
                     id='wrong-id-value'),
        pytest.param({'jsonrpc': '2.0', 'id': 7}, id='neither-member'),
        pytest.param({'jsonrpc': '2.0', 'id': 7, 'result': 1,
                      'error': {'code': 1, 'message': 'x'}},
                     id='both-members'),
        pytest.param({'jsonrpc': '2.0', 'id': 7, 'error': 'bad'},
                     id='error-not-object'),
        pytest.param({'jsonrpc': '2.0', 'id': 7,
                      'error': {'message': 'bad'}}, id='missing-code'),
        pytest.param({'jsonrpc': '2.0', 'id': 7,
                      'error': {'code': True, 'message': 'bad'}},
                     id='boolean-code'),
        pytest.param({'jsonrpc': '2.0', 'id': 7,
                      'error': {'code': '1', 'message': 'bad'}},
                     id='string-code'),
        pytest.param({'jsonrpc': '2.0', 'id': 7,
                      'error': {'code': 1}}, id='missing-message'),
        pytest.param({'jsonrpc': '2.0', 'id': 7,
                      'error': {'code': 1, 'message': 2}},
                     id='non-string-message'),
    ],
)
async def test_malformed_success_envelopes_are_protocol_errors(
    http_server: RecordingHTTPServer,
    body: Any,
) -> None:
    """Every invalid peer-envelope branch maps to JSONRPC_PROTOCOL/502."""
    result = await _rpc(http_server, body)

    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error'] is not None
    assert result['error']['code'] == 'JSONRPC_PROTOCOL'


@pytest.mark.parametrize(
    'body, headers',
    [
        pytest.param(b'{broken', JSON, id='malformed-json'),
        pytest.param(b'not-json', {'Content-Type': 'text/plain'},
                     id='non-json-media'),
        pytest.param(b'', JSON, id='empty-json-body'),
    ],
)
async def test_invalid_success_bodies_are_protocol_errors(
    http_server: RecordingHTTPServer,
    body: bytes,
    headers: dict[str, str],
) -> None:
    """Decode failures and absent semantic JSON are typed protocol faults."""
    result = await _rpc(http_server, body, headers=headers)

    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error'] is not None
    assert result['error']['code'] == 'JSONRPC_PROTOCOL'
    assert result['text'] == body.decode()


async def test_redirects_are_not_followed_or_replayed(
    http_server: RecordingHTTPServer,
) -> None:
    """A redirect response never sends the POST to its target."""
    http_server.respond(
        '/start',
        method='POST',
        status=307,
        body=orjson.dumps({
            'jsonrpc': '2.0',
            'id': 7,
            'error': {'code': 1, 'message': 'move'},
        }),
        headers={**JSON, 'Location': http_server.url_for('/target')},
    )
    http_server.respond('/target', method='POST', body=b'wrong')

    result = await request(
        http_server.url_for('/start'),
        protocol='JSONRPC',
        protocol_info={'method': 'ping', 'request_id': 7},
    )

    assert result['error'] is not None
    assert result['error']['code'] == 'JSONRPC_ERROR'
    assert [seen.path for seen in http_server.requests] == ['/start']


@pytest.mark.parametrize('replacement', [{}, []])
async def test_preprocessor_can_supply_empty_params_without_omitting_them(
    http_server: RecordingHTTPServer,
    replacement: Any,
) -> None:
    """The semantic wrapper reads params after the caller preprocessor."""
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
        '/rpc',
        method='POST',
        body=orjson.dumps({'jsonrpc': '2.0', 'id': 7, 'result': True}),
        headers=JSON,
    )

    result = await request(
        http_server.url_for('/rpc'),
        protocol='JSONRPC',
        data=None,
        protocol_info={'method': 'ping', 'request_id': 7},
        pre_processor_config={'function': replace},
    )

    assert result['ok'] is True
    assert result['pre_processor_response'] == 'replaced'
    assert orjson.loads(http_server.requests[0].body)['params'] == replacement


async def test_response_cap_is_inherited_from_http_transport(
    http_server: RecordingHTTPServer,
) -> None:
    """The semantic adapter cannot bypass the proven streaming byte cap."""
    result = await _rpc(
        http_server,
        {'jsonrpc': '2.0', 'id': 7, 'result': 'too large'},
        info={'max_response_bytes': 8},
    )

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'RESPONSE_TOO_LARGE'


async def test_retryable_timeout_replays_at_most_one_identical_post(
    http_server: RecordingHTTPServer,
) -> None:
    """One configured retry produces exactly two byte-identical attempts."""
    from tests.fixtures.http_server import ResponseSpec

    answer = orjson.dumps({'jsonrpc': '2.0', 'id': 7, 'result': 'ok'})
    http_server.respond_in_sequence('/rpc', [
        ResponseSpec(
            body=answer,
            headers=tuple(JSON.items()),
            delay=0.05,
        ),
        ResponseSpec(body=answer, headers=tuple(JSON.items())),
    ], method='POST')

    result = await request(
        http_server.url_for('/rpc'),
        protocol='JSONRPC',
        data={'value': 1},
        protocol_info={
            'method': 'sum',
            'request_id': 7,
            'timeout': 0.01,
            'circuit_breaker_config': {
                'retry_config': {
                    'name': 'jsonrpc-timeout',
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


async def test_owned_session_uses_library_serializer_and_closes(
    http_server: RecordingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON-RPC creates exactly one session and closes it after success."""
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

    result = await _rpc(
        http_server,
        {'jsonrpc': '2.0', 'id': 7, 'result': True},
    )

    assert result['ok'] is True
    assert len(created) == 1
    assert created[0].closed is True
    assert constructor_options[0]['json_serialize'] is default_json_serialize
    assert getattr(created[0], '_raise_for_status') is False


async def test_cancellation_propagates_and_closes_the_owned_session(
    http_server: RecordingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task cancellation is not converted into an envelope or session leak."""
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
    http_server.gate('/rpc', method='POST')
    task = asyncio.create_task(request(
        http_server.url_for('/rpc'),
        protocol='JSONRPC',
        protocol_info={'method': 'wait', 'request_id': 7},
    ))

    async def wait_for_request() -> None:
        """Yield until the loopback server has received the request."""
        while not http_server.requests:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_request(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(created) == 1
    assert created[0].closed is True


async def test_rpc_error_is_not_retried_or_counted_as_transport_failure(
    http_server: RecordingHTTPServer,
) -> None:
    """Application errors happen after the breaker and leave it healthy."""
    from tests.fixtures.http_server import ResponseSpec

    http_server.respond_in_sequence('/rpc', [
        ResponseSpec(
            status=200,
            body=orjson.dumps({
                'jsonrpc': '2.0',
                'id': 7,
                'error': {'code': 1, 'message': 'first'},
            }),
            headers=tuple(JSON.items()),
        ),
        ResponseSpec(
            status=200,
            body=orjson.dumps({
                'jsonrpc': '2.0',
                'id': 7,
                'result': 'healthy',
            }),
            headers=tuple(JSON.items()),
        ),
    ], method='POST')
    breaker = {
        'maximum_failures': 1,
        'retry_config': {
            'name': 'jsonrpc-app-error',
            'allowed_retries': 3,
            'delay': 0,
        },
    }

    failed = await request(
        http_server.url_for('/rpc'),
        protocol='JSONRPC',
        protocol_info={
            'method': 'ping',
            'request_id': 7,
            'circuit_breaker_config': breaker,
        },
    )
    healthy = await request(
        http_server.url_for('/rpc'),
        protocol='JSONRPC',
        protocol_info={
            'method': 'ping',
            'request_id': 7,
            'circuit_breaker_config': breaker,
        },
    )

    assert failed['error'] is not None
    assert failed['error']['code'] == 'JSONRPC_ERROR'
    assert healthy['ok'] is True
    assert len(http_server.requests) == 2


async def test_query_and_nested_payload_secrets_are_redacted(
    http_server: RecordingHTTPServer,
) -> None:
    """Semantic wrapping does not create a new credential echo surface."""
    http_server.respond(
        '/rpc',
        method='POST',
        body=orjson.dumps({'jsonrpc': '2.0', 'id': 7, 'result': True}),
        headers=JSON,
    )

    result = await request(
        f'{http_server.url_for("/rpc")}?token=query-secret',
        protocol='JSONRPC',
        data={'nested': {'password': 'payload-secret'}},
        protocol_info={
            'method': 'ping',
            'request_id': 7,
            'redact_query_params': ['token'],
        },
    )

    rendered = repr(result)
    assert result['ok'] is True
    assert 'query-secret' not in rendered
    assert 'payload-secret' not in rendered
    assert result['payload']['params']['nested']['password'] == REDACTED

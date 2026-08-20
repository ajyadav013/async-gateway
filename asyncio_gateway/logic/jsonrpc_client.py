"""JSON-RPC 2.0 single-call strategy over the shared HTTP transport.

The adapter owns only JSON-RPC envelope validation and semantic precedence.
HTTP sessions, retries, TLS, tracing, response caps, response copying, and
finalisation remain in :class:`~asyncio_gateway.logic.http_client.HttpRequest`.
"""

from collections.abc import Collection, Mapping
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Union
from urllib.parse import urlsplit

from asyncio_gateway.helpers.internal import (
    CONTENT_TYPE_HEADER,
    normalise_media_type,
)
from asyncio_gateway.logic.http_client import (
    HttpRequest,
    default_json_serialize,
)
from asyncio_gateway.utils.envelope import GatewayResponse, JsonBody
from asyncio_gateway.utils.exceptions import (
    AsyncGatewayError,
    ConfigurationError,
    JsonRpcError,
    JsonRpcProtocolError,
    SerializationError,
)

JsonRpcId = Union[str, int]
JsonRpcParams = Optional[Union[Dict[str, Any], List[Any]]]
MIN_JSONRPC_INTEGER = -(2 ** 63)
MAX_JSONRPC_INTEGER = (2 ** 64) - 1


def validated_jsonrpc_method(value: object) -> str:
    """Return a usable, non-reserved JSON-RPC method name.

    Args:
        value: The caller's ``protocol_info['method']`` value.

    Returns:
        The exact caller spelling, without trimming or case folding.

    Raises:
        ConfigurationError: If the method is empty, non-text, or begins
            with JSON-RPC's reserved ``rpc.`` prefix.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(
            'protocol_info["method"] must be a non-empty string')
    if value.startswith('rpc.'):
        raise ConfigurationError(
            'protocol_info["method"] uses the reserved "rpc." prefix')
    return value


def validated_jsonrpc_id(value: object) -> JsonRpcId:
    """Return a request identifier accepted by the single-call contract.

    Args:
        value: The caller's ``protocol_info['request_id']`` value.

    Returns:
        The string or integer unchanged. Empty strings and zero are valid.

    Raises:
        ConfigurationError: If the value is a boolean or is neither a
            string nor an integer, or if an integer falls outside the exact
            64-bit range supported by the shared JSON engine.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConfigurationError(
            'protocol_info["request_id"] must be a string or integer, '
            'not a boolean or null')
    if isinstance(value, int) and not (
        MIN_JSONRPC_INTEGER <= value <= MAX_JSONRPC_INTEGER
    ):
        raise ConfigurationError(
            'protocol_info["request_id"] must fit the 64-bit JSON wire '
            f'range {MIN_JSONRPC_INTEGER}..{MAX_JSONRPC_INTEGER}')
    return value


def validated_jsonrpc_params(value: object) -> JsonRpcParams:
    """Copy JSON-RPC params into one of the two standard container shapes.

    Args:
        value: The preprocessed envelope payload.

    Returns:
        None to omit ``params``, or a shallow container copy.

    Raises:
        ConfigurationError: If the value is not a mapping, list, or None.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    raise ConfigurationError(
        'JSON-RPC data must be a mapping, list, or None')


def build_jsonrpc_request(
    method: str,
    request_id: JsonRpcId,
    params: JsonRpcParams,
) -> Dict[str, Any]:
    """Build and preflight the exact JSON-RPC 2.0 request object.

    Args:
        method: The validated method name.
        request_id: The validated request identifier.
        params: Validated params, or None to omit the member.

    Returns:
        A fresh, serialisable JSON-RPC request mapping.

    Raises:
        ConfigurationError: If nested params are not JSON serialisable.
    """
    body: Dict[str, Any] = {
        'jsonrpc': '2.0',
        'method': method,
        'id': request_id,
    }
    if params is not None:
        body['params'] = params
    default_json_serialize(body)
    return body


def canonical_jsonrpc_headers(
    headers: Mapping[str, str],
) -> Dict[str, str]:
    """Return headers with one canonical JSON content type.

    Args:
        headers: Headers already checked by the shared HTTP validator.

    Returns:
        A fresh mapping with exactly one ``Content-Type`` spelling.

    Raises:
        ConfigurationError: If any case-insensitive caller occurrence names
            a media type other than ``application/json``.
    """
    checked: Dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() == CONTENT_TYPE_HEADER:
            if normalise_media_type(value) != 'application/json':
                raise ConfigurationError(
                    'JSON-RPC Content-Type must be application/json')
            continue
        checked[name] = value
    checked['Content-Type'] = 'application/json'
    return checked


def validated_jsonrpc_response(
    body: JsonBody,
    expected_id: JsonRpcId,
) -> Tuple[Dict[str, Any], bool]:
    """Validate one JSON-RPC response and normalize semantic details.

    Args:
        body: The decoded response JSON from the shared HTTP copier.
        expected_id: The request identifier the peer must echo exactly.

    Returns:
        ``(protocol_details, is_error)``.

    Raises:
        JsonRpcProtocolError: If the peer envelope is structurally invalid.
    """
    if not isinstance(body, dict):
        raise JsonRpcProtocolError(
            'JSON-RPC response must be a JSON object')
    if body.get('jsonrpc') != '2.0':
        raise JsonRpcProtocolError(
            'JSON-RPC response must declare version 2.0')
    if 'id' not in body:
        raise JsonRpcProtocolError(
            'JSON-RPC response must contain an id')
    peer_id = body['id']
    if type(peer_id) is not type(expected_id) or peer_id != expected_id:
        raise JsonRpcProtocolError(
            'JSON-RPC response id does not match the request id')
    has_result = 'result' in body
    has_error = 'error' in body
    if has_result == has_error:
        raise JsonRpcProtocolError(
            'JSON-RPC response must contain exactly one of result or error')
    if has_result:
        return {'id': peer_id, 'result': body['result']}, False

    error = body['error']
    if not isinstance(error, dict):
        raise JsonRpcProtocolError(
            'JSON-RPC response error must be an object')
    code = error.get('code')
    if isinstance(code, bool) or not isinstance(code, int):
        raise JsonRpcProtocolError(
            'JSON-RPC response error code must be an integer')
    if not MIN_JSONRPC_INTEGER <= code <= MAX_JSONRPC_INTEGER:
        raise JsonRpcProtocolError(
            'JSON-RPC response error code exceeds the 64-bit JSON wire '
            'range')
    if 'message' not in error or not isinstance(error['message'], str):
        raise JsonRpcProtocolError(
            'JSON-RPC response error message must be a string')
    return {'id': peer_id, 'error': dict(error)}, True


class JsonRpcRequest(HttpRequest):
    """Perform one JSON-RPC 2.0 request over HTTP or HTTPS."""

    REQUIRED_INFO_KEYS: ClassVar[frozenset[str]] = frozenset(
        {'method', 'request_id'})
    ALLOWED_INFO_KEYS: ClassVar[frozenset[str]] = frozenset({
        'method',
        'request_id',
        'headers',
        'cookies',
        'certificate',
        'verify_ssl',
        'trace_config',
        'timeout',
        'max_response_bytes',
        'circuit_breaker_config',
        'redact_query_params',
    })

    def __init__(
        self,
        url: str,
        auth: Any,
        response: GatewayResponse,
        info: Optional[Mapping[str, Any]],
        *,
        redact_params: Collection[str],
    ) -> None:
        """Validate JSON-RPC configuration and build the private HTTP call.

        Args:
            url: Validated HTTP(S) target.
            auth: Optional shared HTTP basic authentication.
            response: The entry point's mutable response envelope.
            info: Boundary-validated JSON-RPC options.
            redact_params: Additional normalized sensitive query names.

        Raises:
            ConfigurationError: If JSON-RPC-specific configuration, params,
                URL user info, media type, or serialization is invalid.
        """
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            raise ConfigurationError(
                'JSON-RPC URLs must not contain user info')

        private_info: Dict[str, Any] = (
            {} if info is None else dict(info))
        method = validated_jsonrpc_method(private_info.get('method'))
        request_id = validated_jsonrpc_id(private_info.get('request_id'))
        params = validated_jsonrpc_params(response['payload'])
        wire_body = build_jsonrpc_request(method, request_id, params)

        private_info['request_type'] = 'POST'
        private_info['allow_redirects'] = False
        response['payload'] = wire_body
        super().__init__(
            url,
            auth,
            response,
            private_info,
            redact_params=redact_params,
        )
        self.method: str = method
        self.request_id: JsonRpcId = request_id
        self.headers = canonical_jsonrpc_headers(self.headers)

    def _response_error(
        self,
        status: int,
        body_error: Optional[SerializationError],
    ) -> Optional[AsyncGatewayError]:
        """Apply JSON-RPC semantic precedence after HTTP copied the answer.

        Args:
            status: The peer's HTTP status.
            body_error: Shared JSON decoding failure, or None.

        Returns:
            A semantic/protocol/HTTP error, or None for a valid result.
        """
        if body_error is not None:
            if status >= 400:
                return super()._response_error(status, body_error)
            return JsonRpcProtocolError(
                'JSON-RPC response body is not valid JSON')

        try:
            details, is_rpc_error = validated_jsonrpc_response(
                self.response['json'], self.request_id)
        except JsonRpcProtocolError as err:
            if status >= 400:
                return super()._response_error(status, body_error)
            return err

        self.response['protocol_details'] = details
        if is_rpc_error:
            return JsonRpcError(
                'JSON-RPC peer returned an error object', status)
        if status >= 400:
            return super()._response_error(status, body_error)
        return None

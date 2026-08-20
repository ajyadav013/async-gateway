"""GraphQL query/mutation strategy over the shared HTTP transport.

This module validates GraphQL-over-HTTP request and response envelopes only.
The existing HTTP strategy remains the sole owner of sessions, TLS, retries,
redirect refusal, tracing, byte caps, response copying, and finalisation.
"""

import re
from collections.abc import Collection, Mapping
from typing import Any, ClassVar, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from asyncio_gateway.helpers.internal import (
    CONTENT_TYPE_HEADER,
    media_type_of,
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
    GraphqlError,
    GraphqlProtocolError,
    HttpStatusError,
    SerializationError,
)
from asyncio_gateway.utils.redaction import redact_url

GRAPHQL_RESPONSE_MEDIA_TYPE = 'application/graphql-response+json'
LEGACY_GRAPHQL_MEDIA_TYPE = 'application/json'
GRAPHQL_ACCEPT = (
    f'{GRAPHQL_RESPONSE_MEDIA_TYPE}, {LEGACY_GRAPHQL_MEDIA_TYPE}')
SUPPORTED_GRAPHQL_RESPONSE_MEDIA_TYPES = frozenset({
    GRAPHQL_RESPONSE_MEDIA_TYPE,
    LEGACY_GRAPHQL_MEDIA_TYPE,
})
_QVALUE_PATTERN = re.compile(
    r'(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)\Z')


def validated_graphql_query(value: object) -> str:
    """Return a non-empty GraphQL document without parsing it.

    Args:
        value: The caller's ``protocol_info['query']`` value.

    Returns:
        The exact caller spelling.

    Raises:
        ConfigurationError: If the query is not non-empty text.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(
            'protocol_info["query"] must be a non-empty string')
    return value


def validated_operation_name(value: object) -> str:
    """Return a supplied non-empty GraphQL operation name.

    Args:
        value: The caller's supplied ``operation_name`` value.

    Returns:
        The exact caller spelling.

    Raises:
        ConfigurationError: If the supplied name is not non-empty text.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(
            'protocol_info["operation_name"] must be a non-empty string')
    return value


def validated_graphql_variables(
    value: object,
) -> Optional[Dict[str, Any]]:
    """Copy GraphQL variables or preserve omission.

    Args:
        value: The preprocessed envelope payload.

    Returns:
        None to omit ``variables``, or a shallow mapping copy.

    Raises:
        ConfigurationError: If variables are neither a mapping nor None.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ConfigurationError(
            'GraphQL data must be a mapping or None')
    return dict(value)


def build_graphql_request(
    query: str,
    operation_name: Optional[str],
    variables: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build and preflight the exact GraphQL request object.

    Args:
        query: The validated document.
        operation_name: A validated name, or None to omit it.
        variables: Validated variables, or None to omit them.

    Returns:
        A fresh JSON-serialisable request mapping.

    Raises:
        ConfigurationError: If nested variables are not serialisable.
    """
    body: Dict[str, Any] = {'query': query}
    if operation_name is not None:
        body['operationName'] = operation_name
    if variables is not None:
        body['variables'] = variables
    default_json_serialize(body)
    return body


def _accepted_media_types(value: str) -> frozenset[str]:
    """Return positively weighted media types from HTTP Accept fields.

    Args:
        value: The comma-joined values of all supplied Accept fields.

    Returns:
        The non-empty normalized media types whose quality is greater than
        zero.

    Raises:
        ConfigurationError: If caller syntax is quoted/escaped, or a quality
            parameter is duplicated, malformed, or outside the HTTP qvalue
            range.
    """
    if '"' in value or '\\' in value:
        raise ConfigurationError(
            'GraphQL Accept does not support quoted or escaped parameters')
    accepted: set[str] = set()
    for item in value.split(','):
        parts = item.split(';')
        media_type = normalise_media_type(parts[0])
        if not media_type:
            continue
        quality = 1.0
        has_quality = False
        for parameter in parts[1:]:
            name, separator, raw_value = parameter.partition('=')
            if name.strip().lower() != 'q':
                continue
            quality_value = raw_value.strip()
            if (has_quality or not separator
                    or _QVALUE_PATTERN.fullmatch(quality_value) is None):
                raise ConfigurationError(
                    'GraphQL Accept contains an invalid quality value')
            quality = float(quality_value)
            has_quality = True
        if quality > 0:
            accepted.add(media_type)
    return frozenset(accepted)


def canonical_graphql_headers(
    headers: Mapping[str, str],
) -> Dict[str, str]:
    """Canonicalize GraphQL request Content-Type and Accept headers.

    Args:
        headers: Headers already checked by the shared HTTP validator.

    Returns:
        A new mapping advertising exactly the two supported response types.

    Raises:
        ConfigurationError: If a caller media-type occurrence conflicts with
            the fixed GraphQL-over-HTTP contract.
    """
    checked: Dict[str, str] = {}
    accept_values: List[str] = []
    for name, value in headers.items():
        lower_name = name.lower()
        if lower_name == CONTENT_TYPE_HEADER:
            if normalise_media_type(value) != LEGACY_GRAPHQL_MEDIA_TYPE:
                raise ConfigurationError(
                    'GraphQL Content-Type must be application/json')
            continue
        if lower_name == 'accept':
            accept_values.append(value)
            continue
        checked[name] = value
    if (accept_values
            and not SUPPORTED_GRAPHQL_RESPONSE_MEDIA_TYPES
            <= _accepted_media_types(','.join(accept_values))):
        raise ConfigurationError(
            'GraphQL Accept must advertise '
            f'{GRAPHQL_ACCEPT}')
    checked['Content-Type'] = LEGACY_GRAPHQL_MEDIA_TYPE
    checked['Accept'] = GRAPHQL_ACCEPT
    return checked


def validated_graphql_response(
    body: JsonBody,
) -> Tuple[Dict[str, Any], bool]:
    """Validate a GraphQL response and normalize semantic details.

    Args:
        body: The decoded response JSON from the shared HTTP copier.

    Returns:
        ``(protocol_details, has_errors)``.

    Raises:
        GraphqlProtocolError: If the peer response violates the GraphQL
            response envelope contract.
    """
    if not isinstance(body, dict):
        raise GraphqlProtocolError(
            'GraphQL response must be a JSON object')
    has_data = 'data' in body
    has_errors = 'errors' in body
    if not has_data and not has_errors:
        raise GraphqlProtocolError(
            'GraphQL response must contain data, errors, or both')

    details: Dict[str, Any] = {}
    if has_data:
        data = body['data']
        if data is not None and not isinstance(data, dict):
            raise GraphqlProtocolError(
                'GraphQL response data must be an object or null')
        details['data'] = None if data is None else dict(data)

    if not has_errors:
        if body['data'] is None:
            raise GraphqlProtocolError(
                'GraphQL null data requires at least one error')
        return details, False

    errors = body['errors']
    if not isinstance(errors, list) or not errors:
        raise GraphqlProtocolError(
            'GraphQL response errors must be a non-empty list')
    copied_errors: List[Dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, dict):
            raise GraphqlProtocolError(
                'Each GraphQL response error must be an object')
        message = error.get('message')
        if not isinstance(message, str) or not message.strip():
            raise GraphqlProtocolError(
                'Each GraphQL response error needs a non-empty message')
        copied_errors.append(dict(error))
    details['errors'] = copied_errors
    return details, True


class GraphqlRequest(HttpRequest):
    """Perform one GraphQL query or mutation over HTTP or HTTPS."""

    REQUIRED_INFO_KEYS: ClassVar[frozenset[str]] = frozenset({'query'})
    ALLOWED_INFO_KEYS: ClassVar[frozenset[str]] = frozenset({
        'query',
        'operation_name',
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
        """Validate GraphQL configuration and build the private HTTP call.

        Args:
            url: Validated HTTP(S) target.
            auth: Optional shared HTTP basic authentication.
            response: The entry point's mutable response envelope.
            info: Boundary-validated GraphQL options.
            redact_params: Additional normalized sensitive query names.

        Raises:
            ConfigurationError: If GraphQL-specific configuration, variables,
                URL user info, media headers, or serialization is invalid.
        """
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            raise ConfigurationError(
                'GraphQL URLs must not contain user info')

        private_info: Dict[str, Any] = (
            {} if info is None else dict(info))
        query = validated_graphql_query(private_info.get('query'))
        operation_name = (
            validated_operation_name(private_info['operation_name'])
            if 'operation_name' in private_info else None)
        variables = validated_graphql_variables(response['payload'])
        wire_body = build_graphql_request(
            query, operation_name, variables)

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
        self.query: str = query
        self.operation_name: Optional[str] = operation_name
        self.headers = canonical_graphql_headers(self.headers)

    def _http_status_error(
        self,
        status: int,
    ) -> AsyncGatewayError:
        """Return HTTP failure semantics for any non-2xx GraphQL response.

        Args:
            status: The peer's non-2xx HTTP status.

        Returns:
            An HTTP status error carrying the peer's real non-2xx status.
        """
        return HttpStatusError(
            f'POST {redact_url(self.url, extra_params=self.redact_params)}'
            f' returned HTTP {status}',
            status,
        )

    def _response_error(
        self,
        status: int,
        body_error: Optional[SerializationError],
    ) -> Optional[AsyncGatewayError]:
        """Apply GraphQL media-type and HTTP semantic precedence.

        Args:
            status: The peer's HTTP status.
            body_error: Shared JSON decoding failure, or None.

        Returns:
            A GraphQL/protocol/HTTP error, or None for valid 2xx data.
        """
        is_success_status = 200 <= status < 300
        response_media_type = media_type_of(self.response['headers'])
        if (is_success_status
                and response_media_type
                not in SUPPORTED_GRAPHQL_RESPONSE_MEDIA_TYPES):
            return GraphqlProtocolError(
                'GraphQL response has an unsupported Content-Type')
        if body_error is not None:
            if not is_success_status:
                return self._http_status_error(status)
            return GraphqlProtocolError(
                'GraphQL response body is not valid JSON')

        try:
            details, has_errors = validated_graphql_response(
                self.response['json'])
        except GraphqlProtocolError as err:
            if not is_success_status:
                return self._http_status_error(status)
            return err

        self.response['protocol_details'] = details
        if has_errors:
            if (is_success_status
                    or response_media_type == GRAPHQL_RESPONSE_MEDIA_TYPE):
                return GraphqlError(
                    'GraphQL peer returned one or more errors', status)
            return self._http_status_error(status)
        if not is_success_status:
            return self._http_status_error(status)
        return None

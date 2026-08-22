"""Strict public-boundary strategy for Google Cloud Storage requests."""

import math
import re
from collections.abc import Mapping
from typing import Any, ClassVar, Final, Optional, Tuple, cast
from urllib.parse import urlsplit

from asyncio_gateway.helpers.internal.base import BaseRequestClass
from asyncio_gateway.utils.constants import (HTTP_TIMEOUT, MAX_RESPONSE_BYTES)
from asyncio_gateway.utils.envelope import GatewayResponse
from asyncio_gateway.utils.exceptions import ConfigurationError


GCS_COMMANDS: Final[frozenset[str]] = frozenset({
    'download', 'upload', 'head', 'list', 'signed_url',
})
GCS_OPERATION_INFO_KEYS: Final[Mapping[str, frozenset[str]]] = {
    'download': frozenset({
        'command', 'local_path', 'max_response_bytes',
        'if_generation_match', 'timeout', 'circuit_breaker_config',
        'redact_query_params',
    }),
    'upload': frozenset({
        'command', 'local_path', 'max_upload_bytes',
        'if_generation_match', 'timeout', 'circuit_breaker_config',
        'redact_query_params',
    }),
    'head': frozenset({
        'command', 'if_generation_match', 'timeout',
        'circuit_breaker_config', 'redact_query_params',
    }),
    'list': frozenset({
        'command', 'max_items', 'page_token', 'timeout',
        'circuit_breaker_config', 'redact_query_params',
    }),
    'signed_get': frozenset({
        'command', 'method', 'expires_in_seconds',
        'signing_service_account', 'timeout', 'redact_query_params',
    }),
    'signed_put': frozenset({
        'command', 'method', 'content_type', 'max_upload_bytes',
        'expires_in_seconds', 'signing_service_account',
        'if_generation_match', 'timeout', 'redact_query_params',
    }),
}
GCS_ALLOWED_INFO_KEYS: Final[frozenset[str]] = frozenset().union(
    *GCS_OPERATION_INFO_KEYS.values())
_SIGNING_ACCOUNT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r'[a-z][a-z0-9-]{4,28}[a-z0-9]@'
    r'[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com')


def _nonempty_string(value: object, *, setting: str) -> str:
    """Return one non-empty caller string.

    Args:
        value: Proposed text.
        setting: Public option named in a safe refusal.

    Returns:
        The caller's original string.

    Raises:
        ConfigurationError: If the value is not non-whitespace text.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f'{setting} must be a non-empty string')
    return value


def _positive_int(value: object, *, setting: str) -> int:
    """Return one positive, non-boolean integer.

    Args:
        value: Proposed integer.
        setting: Public option named in a safe refusal.

    Returns:
        The validated integer.

    Raises:
        ConfigurationError: If the value is not a positive integer.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f'{setting} must be a positive integer')
    return value


def _nonnegative_int(value: object, *, setting: str) -> int:
    """Return one non-negative, non-boolean integer.

    Args:
        value: Proposed integer.
        setting: Public option named in a safe refusal.

    Returns:
        The validated integer.

    Raises:
        ConfigurationError: If the value is not a non-negative integer.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigurationError(
            f'{setting} must be a non-negative integer')
    return value


def _positive_timeout(value: object) -> int | float:
    """Return one finite positive request timeout.

    Args:
        value: Proposed timeout in seconds.

    Returns:
        The validated numeric timeout.

    Raises:
        ConfigurationError: If the value is boolean, non-numeric,
            non-finite, or non-positive.
    """
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ConfigurationError('timeout must be a finite positive number')
    return value


def _bounded_int(
    value: object,
    *,
    setting: str,
    maximum: int,
) -> int:
    """Return one positive integer no greater than a frozen maximum.

    Args:
        value: Proposed integer.
        setting: Public option named in a safe refusal.
        maximum: Inclusive upper bound.

    Returns:
        The validated integer.

    Raises:
        ConfigurationError: If the value lies outside ``1..maximum``.
    """
    result = _positive_int(value, setting=setting)
    if result > maximum:
        raise ConfigurationError(
            f'{setting} must be in 1..{maximum}')
    return result


def _page_token(value: object) -> str:
    """Return one non-empty opaque token within the UTF-8 byte cap.

    Args:
        value: Proposed caller page token.

    Returns:
        The exact original token without normalization.

    Raises:
        ConfigurationError: If the token is empty, non-string,
            unencodable, or larger than 4096 UTF-8 bytes.
    """
    if not isinstance(value, str) or value == '':
        raise ConfigurationError('page_token must be a non-empty string')
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError as error:
        raise ConfigurationError(
            'page_token must be valid UTF-8 text') from error
    if len(encoded) > 4096:
        raise ConfigurationError(
            'page_token must encode to at most 4096 UTF-8 bytes')
    return value


def _content_type(value: object) -> str:
    """Return one bounded visible-ASCII media type.

    Args:
        value: Proposed signed-PUT content type.

    Returns:
        The validated original content type.

    Raises:
        ConfigurationError: If the media type is ambiguous or unsafe.
    """
    if (not isinstance(value, str) or not 1 <= len(value) <= 255
            or any(not 0x21 <= ord(char) <= 0x7e for char in value)):
        raise ConfigurationError(
            'content_type must be 1..255 visible-ASCII characters')
    media_type = value.partition(';')[0]
    if media_type.count('/') != 1:
        raise ConfigurationError(
            'content_type must contain one type/subtype separator')
    type_name, subtype = media_type.split('/', 1)
    if not type_name or not subtype:
        raise ConfigurationError(
            'content_type requires a non-empty type and subtype')
    return value


def _signing_account(value: object) -> str:
    """Return one syntactically valid service-account identity.

    Args:
        value: Proposed signing-service-account address.

    Returns:
        The validated original address.

    Raises:
        ConfigurationError: If the address does not match the frozen form.
    """
    if (not isinstance(value, str) or not 1 <= len(value) <= 253
            or _SIGNING_ACCOUNT_PATTERN.fullmatch(value) is None):
        raise ConfigurationError(
            'signing_service_account must be a valid service-account '
            'address')
    return value


def _gcs_target(url: str) -> Tuple[str, str]:
    """Parse one unambiguous ``gs://bucket[/object]`` target.

    Args:
        url: Caller target after the public scheme guard.

    Returns:
        The normalized bucket and opaque object key or prefix.

    Raises:
        ConfigurationError: If the target contains an unsupported shape.
    """
    if '?' in url or '#' in url:
        raise ConfigurationError(
            'GCS target does not support a query string or fragment')
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise ConfigurationError('GCS target is not a parseable URI') \
            from error
    if parts.scheme.lower() != 'gs':
        raise ConfigurationError('GCS target must use the gs scheme')
    if not parts.netloc or parts.hostname is None:
        raise ConfigurationError('GCS target requires a non-empty bucket')
    if (parts.username is not None or parts.password is not None
            or port is not None or '@' in parts.netloc
            or ':' in parts.netloc):
        raise ConfigurationError(
            'GCS target does not support user info or a port')
    key = parts.path[1:] if parts.path.startswith('/') else parts.path
    return parts.hostname.lower(), key


class GcsRequest(BaseRequestClass):
    """Represent one validated Google Cloud Storage request."""

    REQUIRED_INFO_KEYS: ClassVar[frozenset[str]] = frozenset({'command'})
    ALLOWED_INFO_KEYS: ClassVar[frozenset[str]] = GCS_ALLOWED_INFO_KEYS
    ACCEPTED_INFO_KEYS: ClassVar[frozenset[str]] = ALLOWED_INFO_KEYS

    @classmethod
    def validate_protocol_info(
        cls,
        info: Optional[Mapping[str, Any]],
        *,
        protocol: Optional[str] = None,
    ) -> dict[str, Any]:
        """Validate and copy GCS's command-specific public options.

        Args:
            info: Caller-supplied protocol mapping or None.
            protocol: Normalized selector used only in diagnostics.

        Returns:
            A fresh mapping containing normalized values and frozen defaults.

        Raises:
            ConfigurationError: If an option is missing, malformed, unknown,
                or belongs to a different GCS command.
        """
        validated = super().validate_protocol_info(
            info, protocol=protocol)
        raw_command = validated['command']
        command = (
            raw_command.strip().lower()
            if isinstance(raw_command, str) else None)
        if command not in GCS_COMMANDS:
            raise ConfigurationError(
                "protocol_info['command'] must name one of "
                f'{sorted(GCS_COMMANDS)}')
        normalized_command = cast(str, command)
        validated['command'] = normalized_command

        operation = normalized_command
        if normalized_command == 'signed_url':
            raw_method = validated.get('method')
            method = (
                raw_method.strip().upper()
                if isinstance(raw_method, str) else None)
            if method not in {'GET', 'PUT'}:
                raise ConfigurationError(
                    "protocol_info['method'] must be GET or PUT")
            validated['method'] = method
            operation = f'signed_{method.lower()}'

        allowed = GCS_OPERATION_INFO_KEYS[operation]
        disallowed = sorted(set(validated) - set(allowed))
        if disallowed:
            raise ConfigurationError(
                f'protocol_info key(s) {disallowed} are not accepted for '
                f'GCS {operation!r}')

        validated['timeout'] = _positive_timeout(
            validated.get('timeout', HTTP_TIMEOUT))
        if normalized_command in {'download', 'upload'}:
            if 'local_path' not in validated:
                raise ConfigurationError(
                    f'GCS {normalized_command} requires local_path')
            validated['local_path'] = _nonempty_string(
                validated['local_path'], setting='local_path')
        if normalized_command == 'download':
            validated['max_response_bytes'] = _positive_int(
                validated.get('max_response_bytes', MAX_RESPONSE_BYTES),
                setting='max_response_bytes')
        if normalized_command == 'upload':
            validated['max_upload_bytes'] = _positive_int(
                validated.get('max_upload_bytes', MAX_RESPONSE_BYTES),
                setting='max_upload_bytes')
            validated['if_generation_match'] = _nonnegative_int(
                validated.get('if_generation_match', 0),
                setting='if_generation_match')
        elif 'if_generation_match' in validated:
            validated['if_generation_match'] = _nonnegative_int(
                validated['if_generation_match'],
                setting='if_generation_match')
        if normalized_command == 'list':
            validated['max_items'] = _bounded_int(
                validated.get('max_items', 1000),
                setting='max_items', maximum=1000)
            if 'page_token' in validated:
                validated['page_token'] = _page_token(
                    validated['page_token'])
        if normalized_command == 'signed_url':
            validated['expires_in_seconds'] = _bounded_int(
                validated.get('expires_in_seconds', 900),
                setting='expires_in_seconds', maximum=3600)
            if 'signing_service_account' in validated:
                validated['signing_service_account'] = _signing_account(
                    validated['signing_service_account'])
            if validated['method'] == 'PUT':
                if 'content_type' not in validated:
                    raise ConfigurationError(
                        'GCS signed PUT requires content_type')
                if 'max_upload_bytes' not in validated:
                    raise ConfigurationError(
                        'GCS signed PUT requires max_upload_bytes')
                validated['content_type'] = _content_type(
                    validated['content_type'])
                validated['max_upload_bytes'] = _positive_int(
                    validated['max_upload_bytes'],
                    setting='max_upload_bytes')
                validated['if_generation_match'] = _nonnegative_int(
                    validated.get('if_generation_match', 0),
                    setting='if_generation_match')
        return validated

    def __init__(
        self,
        url: str,
        auth: Any,
        response: GatewayResponse,
        info: Optional[Mapping[str, Any]],
        *,
        redact_params: frozenset[str],
    ) -> None:
        """Validate target and ADC-only auth before breaker lookup.

        Args:
            url: Scheme-guarded GCS target.
            auth: Raw caller authentication, which must be exactly None.
            response: Shared gateway response envelope.
            info: Already-validated GCS protocol options.
            redact_params: Normalized sensitive query-parameter names.

        Raises:
            ConfigurationError: If auth or target shape violates the frozen
                GCS contract, including an absent non-list object key.
        """
        bucket, key = _gcs_target(url)
        if auth is not None:
            raise ConfigurationError(
                'GCS auth must be exactly None; use ADC')
        command = cast(str, info['command'] if info is not None else '')
        if command != 'list' and not key:
            raise ConfigurationError(
                f'GCS {command} requires a non-empty object key')

        super().__init__(
            url, auth, response, info=info, redact_params=redact_params)
        self.bucket = bucket
        self.key = key
        self.command = command

    async def handle_request(self) -> GatewayResponse:
        """Reject execution until a bounded GCS operation is implemented.

        Returns:
            The shared gateway response after a future bounded operation.

        Raises:
            NotImplementedError: Always in the selector-boundary story.
        """
        raise NotImplementedError

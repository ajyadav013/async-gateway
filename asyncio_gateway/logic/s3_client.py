"""First-class bounded S3 strategy.

The strategy exposes exactly four operations (download, upload, head, and one
list page), validates every caller-controlled value before constructing an SDK
session, and leaves replay ownership exclusively with the gateway breaker.
SDK objects and credential-bearing response metadata never enter the public
envelope.
"""

import asyncio
import socket
import ssl
from collections.abc import Collection, Mapping
from datetime import datetime
from types import TracebackType
from typing import (Any, ClassVar, Dict, NoReturn, Optional, Tuple, TypedDict,
                    Union, cast)
from urllib.parse import urlsplit

import aioboto3

from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    HTTPClientError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
    SSLError as BotocoreSslError,
)

from failsafe import CircuitOpen, RetriesExhausted

from asyncio_gateway.helpers.internal.base import BaseRequestClass
from asyncio_gateway.helpers.internal.circuit_breaker_helper import (
    AbortableServiceError,
)
from asyncio_gateway.utils.constants import MAX_RESPONSE_BYTES
from asyncio_gateway.utils.envelope import GatewayResponse, finalise_ok
from asyncio_gateway.utils.exceptions import (
    AsyncGatewayError,
    CircuitOpenError,
    ConfigurationError,
    ConnectError,
    DnsError,
    GatewayTimeoutError,
    S3StatusError,
    TlsError,
    TransportError,
)
from asyncio_gateway.utils.http_file_config import stream_s3_download
from asyncio_gateway.utils.paths import read_guarded_file
from asyncio_gateway.utils.redaction import REDACTED, redact_text

S3_COMMANDS = frozenset({'download', 'upload', 'head', 'list'})
S3_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
S3_RETRYABLE_CODES = frozenset({
    'RequestTimeout',
    'RequestTimeoutException',
    'Throttling',
    'ThrottlingException',
    'SlowDown',
    'InternalError',
    'ServiceUnavailable',
})
S3_BASE_INFO_KEYS = frozenset({
    'command',
    'region',
    'circuit_breaker_config',
    'redact_query_params',
})
S3_OPERATION_INFO_KEYS: Mapping[str, frozenset[str]] = {
    'download': S3_BASE_INFO_KEYS | frozenset({
        'local_path', 'max_response_bytes'}),
    'upload': S3_BASE_INFO_KEYS | frozenset({
        'local_path', 'max_upload_bytes'}),
    'head': S3_BASE_INFO_KEYS,
    'list': S3_BASE_INFO_KEYS | frozenset({
        'max_items', 'continuation_token'}),
}
S3_ALLOWED_INFO_KEYS = frozenset().union(*S3_OPERATION_INFO_KEYS.values())
_SERVER_TOKEN_ABSENT = object()


class S3OperationResult(TypedDict):
    """One SDK operation result retained for safe normalization."""

    response: Mapping[str, Any]
    byte_count: Optional[int]


class _ExceptionState(TypedDict):
    """Exception attributes that cleanup must not be allowed to replace."""

    args: Tuple[Any, ...]
    cause: Optional[BaseException]
    context: Optional[BaseException]
    suppress_context: bool
    traceback: Optional[TracebackType]


class _ClientExitOutcome(TypedDict):
    """Non-raising result produced by the dedicated cleanup task."""

    suppressed: bool
    error: Optional[BaseException]
    error_state: Optional[_ExceptionState]


class _RetryableS3ServiceError(AsyncGatewayError):
    """Private service response the gateway breaker may count and retry."""

    def __init__(self, response: Mapping[str, Any]) -> None:
        """Retain the original SDK response without stringifying it.

        Args:
            response: Original botocore service response mapping.
        """
        super().__init__('retryable S3 service response')
        self.sdk_response = response


class _AbortableS3ServiceError(AbortableServiceError):
    """Private permanent service response the breaker must not count."""

    def __init__(self, response: Mapping[str, Any]) -> None:
        """Retain the original SDK response without stringifying it.

        Args:
            response: Original botocore service response mapping.
        """
        super().__init__('abortable S3 service response')
        self.sdk_response = response


class _KnownSecretRedactor:
    """Apply one exact known-secret rule to every S3-owned surface."""

    def __init__(
        self,
        secrets: Tuple[str, ...],
        *,
        redact_params: Collection[str],
    ) -> None:
        """Retain longest-first secrets and the shared query-name policy.

        Args:
            secrets: Validated credential and caller-token values.
            redact_params: Normalized caller query names from the entrypoint.
        """
        self.secrets = secrets
        self.redact_params = redact_params

    def text(self, value: str) -> str:
        """Return text with URL/query credentials and known values masked.

        Args:
            value: Public-facing text.

        Returns:
            Redacted text.
        """
        safe = redact_text(value, extra_params=self.redact_params)
        for secret in self.secrets:
            safe = safe.replace(secret, REDACTED)
        return safe

    def fixed_mapping_values(
        self,
        value: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Redact only string values in one fixed-schema mapping.

        Args:
            value: A normalized mapping whose keys are structural schema.

        Returns:
            A shallow copy with byte-for-byte identical keys.
        """
        return {
            key: self.text(item) if isinstance(item, str) else item
            for key, item in value.items()
        }

    def dynamic_mapping(self, value: Mapping[str, str]) -> Dict[str, str]:
        """Redact both sides of one genuinely dynamic remote mapping.

        Args:
            value: Validated S3 user-metadata keys and values.

        Returns:
            A copied mapping with known values removed from keys and values.
        """
        return {
            self.text(key): self.text(item)
            for key, item in value.items()
        }

    def exception(self, error: BaseException) -> None:
        """Make one escaping exception safe and drop its foreign chain.

        The public exception already contains the useful S3 classification.
        A retained SDK/filesystem chain adds only foreign prose and, for an
        ``OSError``, a separately formatted filename that changing ``args``
        cannot mask. Replacing the public message through the same redactor
        and detaching that chain prevents both ``unwrap_cause`` and the
        common traceback logger from rediscovering a known secret.

        Args:
            error: Exception about to leave the S3 strategy.
        """
        message = str(error)
        error.args = (self.text(message),) if message else ()
        error.__cause__ = None
        error.__context__ = None


def _nonempty_string(value: object, *, setting: str) -> str:
    """Return one caller string after rejecting empty/whitespace values.

    Args:
        value: Proposed string.
        setting: Public setting named in the refusal.

    Returns:
        The caller's original string.

    Raises:
        ConfigurationError: If the value is not non-empty text.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f'{setting} must be a non-empty string')
    return value


def _positive_int(value: object, *, setting: str) -> int:
    """Return one positive non-boolean integer.

    Args:
        value: Proposed number.
        setting: Public setting named in the refusal.

    Returns:
        The validated integer.

    Raises:
        ConfigurationError: If the value is not a positive integer.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(
            f'{setting} must be a positive int, got {value!r}')
    return value


def _max_items(value: object) -> int:
    """Return a one-page S3 item bound in ``1..1000``.

    Args:
        value: Proposed bound.

    Returns:
        The validated integer.

    Raises:
        ConfigurationError: If the value is outside the exact range.
    """
    maximum = _positive_int(value, setting='max_items')
    if maximum > 1000:
        raise ConfigurationError(
            f'max_items must be in 1..1000, got {maximum!r}')
    return maximum


def _s3_target(url: str) -> Tuple[str, str]:
    """Parse one unambiguous ``s3://bucket[/key]`` target.

    Args:
        url: Caller target after the public scheme guard.

    Returns:
        Bucket and key/prefix, with exactly one leading path slash removed.

    Raises:
        ConfigurationError: For an ambiguous or unsupported target shape.
    """
    if '?' in url or '#' in url:
        raise ConfigurationError(
            'S3 target does not support a query string or fragment')
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise ConfigurationError('S3 target is not a parseable URI') from error
    if parts.scheme.lower() != 's3':
        raise ConfigurationError('S3 target must use the s3 scheme')
    if not parts.netloc:
        raise ConfigurationError('S3 target requires a non-empty bucket')
    if (parts.username is not None or parts.password is not None
            or port is not None or '@' in parts.netloc
            or ':' in parts.netloc):
        raise ConfigurationError(
            'S3 target does not support user info or a port')
    key = parts.path[1:] if parts.path.startswith('/') else parts.path
    return parts.netloc, key


def _capture_exception_state(
    error: BaseException,
    traceback: Optional[TracebackType],
) -> _ExceptionState:
    """Snapshot exception state before foreign cleanup can inspect it.

    Args:
        error: Body, parent-cancellation, or cleanup failure.
        traceback: Traceback paired with the failure at its boundary.

    Returns:
        The exact public exception attributes and boundary traceback.
    """
    return {
        'args': error.args,
        'cause': error.__cause__,
        'context': error.__context__,
        'suppress_context': error.__suppress_context__,
        'traceback': traceback,
    }


def _restore_exception_state(
    error: BaseException,
    state: _ExceptionState,
) -> None:
    """Restore attributes that an SDK exit hook may have mutated.

    Args:
        error: Failure that will propagate.
        state: Previously captured exact state.
    """
    error.args = state['args']
    error.__cause__ = state['cause']
    error.__context__ = state['context']
    error.__suppress_context__ = state['suppress_context']


def _raise_exact(
    error: BaseException,
    state: _ExceptionState,
) -> NoReturn:
    """Re-raise one captured failure without changing its public state.

    Args:
        error: Exact failure object captured at a manual context boundary.
        state: Its state before SDK cleanup ran.

    Raises:
        BaseException: The supplied object with its original attributes.
    """
    _restore_exception_state(error, state)
    try:
        raise error.with_traceback(state['traceback'])
    finally:
        _restore_exception_state(error, state)


async def _capture_client_exit(
    client_context: Any,
    body_error: Optional[BaseException],
    body_traceback: Optional[TracebackType],
) -> _ClientExitOutcome:
    """Run ``__aexit__`` once and turn every failure into task data.

    The dedicated cleanup task must finish normally even when a hostile exit
    implementation raises :class:`BaseException`. That makes every
    ``CancelledError`` observed by its shielded parent await provenance for
    cancellation of the parent itself.

    Args:
        client_context: Entered aioboto3 client context.
        body_error: Exact body failure, or None after a successful body.
        body_traceback: Traceback paired with ``body_error``.

    Returns:
        Suppression state or an exact captured cleanup failure.
    """
    error_type = type(body_error) if body_error is not None else None
    try:
        suppressed = bool(await client_context.__aexit__(
            error_type, body_error, body_traceback))
    except BaseException as error:
        return {
            'suppressed': False,
            'error': error,
            'error_state': _capture_exception_state(
                error, error.__traceback__),
        }
    return {
        'suppressed': suppressed,
        'error': None,
        'error_state': None,
    }


async def _drain_client_exit(
    client_context: Any,
    body_error: Optional[BaseException],
    body_traceback: Optional[TracebackType],
    pending_cancellation: Optional[asyncio.CancelledError],
    cancellation_state: Optional[_ExceptionState],
) -> Tuple[
    _ClientExitOutcome,
    Optional[asyncio.CancelledError],
    Optional[_ExceptionState],
]:
    """Shield and drain one cleanup task while recording parent cancels.

    Args:
        client_context: Entered aioboto3 client context.
        body_error: Exact body failure passed to ``__aexit__``.
        body_traceback: Exact body traceback passed to ``__aexit__``.
        pending_cancellation: Body cancellation, when already captured.
        cancellation_state: Exact state of that pending cancellation.

    Returns:
        Cleanup outcome plus the first proven pending cancellation and state.
    """
    exit_task = asyncio.create_task(_capture_client_exit(
        client_context, body_error, body_traceback))
    while True:
        try:
            outcome = await asyncio.shield(exit_task)
        except asyncio.CancelledError as error:
            if pending_cancellation is None:
                pending_cancellation = error
                cancellation_state = _capture_exception_state(
                    error, error.__traceback__)
        else:
            return outcome, pending_cancellation, cancellation_state


def _service_components(
    response: Mapping[str, Any],
) -> Tuple[Optional[int], Optional[str]]:
    """Read only the status and code needed for retry classification.

    Args:
        response: Original botocore service response.

    Returns:
        Valid integer status and string AWS code where present.
    """
    metadata = response.get('ResponseMetadata')
    raw_status = (
        metadata.get('HTTPStatusCode') if isinstance(metadata, Mapping)
        else None)
    status = (
        raw_status
        if isinstance(raw_status, int) and not isinstance(raw_status, bool)
        else None)
    error = response.get('Error')
    raw_code = error.get('Code') if isinstance(error, Mapping) else None
    code = raw_code if isinstance(raw_code, str) else None
    return status, code


def _is_retryable_service_response(response: Mapping[str, Any]) -> bool:
    """Classify one AWS failure by the frozen status/code allowlist.

    Args:
        response: Original botocore service response.

    Returns:
        True only for an explicitly allowlisted status or code.
    """
    status, code = _service_components(response)
    return status in S3_RETRYABLE_STATUSES or code in S3_RETRYABLE_CODES


def _sdk_transport_error(
    error: BaseException,
    *,
    command: str,
    bucket: str,
) -> AsyncGatewayError:
    """Map an SDK transport failure without exposing its foreign text.

    Args:
        error: SDK/OS transport exception.
        command: S3 command being attempted.
        bucket: Destination bucket.

    Returns:
        An existing public transport-vocabulary exception.
    """
    error.args = ('S3 SDK transport failure',)
    message = f'S3 {command} transport failed for bucket {bucket!r}'
    if isinstance(error, socket.gaierror):
        return DnsError(message)
    if isinstance(error, (
        ConnectTimeoutError,
        ReadTimeoutError,
        asyncio.TimeoutError,
        TimeoutError,
    )):
        return GatewayTimeoutError(message)
    if isinstance(error, (BotocoreSslError, ssl.SSLError)):
        return TlsError(message)
    if isinstance(error, (
        EndpointConnectionError,
        ConnectionClosedError,
        ConnectionError,
        OSError,
    )):
        return ConnectError(message)
    return TransportError(message)


SDK_TRANSPORT_FAILURES: Tuple[type[BaseException], ...] = (
    ConnectTimeoutError,
    ReadTimeoutError,
    BotocoreSslError,
    EndpointConnectionError,
    ConnectionClosedError,
    HTTPClientError,
    BotoCoreError,
    socket.gaierror,
    asyncio.TimeoutError,
    TimeoutError,
    ssl.SSLError,
    OSError,
)


def _malformed_success(reason: str) -> S3StatusError:
    """Build a safe public error for malformed SDK success metadata.

    Args:
        reason: Library-authored description containing no SDK object.

    Returns:
        A stable ``S3_STATUS``/502 error.
    """
    return S3StatusError(f'malformed S3 success response: {reason}', 502)


def _response_mapping(value: object) -> Mapping[str, Any]:
    """Require an SDK response mapping without coercing foreign objects.

    Args:
        value: SDK operation result.

    Returns:
        The mapping.

    Raises:
        S3StatusError: If the SDK returned another shape.
    """
    if not isinstance(value, Mapping):
        raise _malformed_success('response is not a mapping')
    return value


def _success_status(response: Mapping[str, Any]) -> int:
    """Read a valid SDK 2xx status or the test-double default.

    Args:
        response: SDK success mapping.

    Returns:
        A status in ``200..299``, defaulting to 200 only when response
        metadata is wholly omitted.

    Raises:
        S3StatusError: If supplied metadata/status is malformed.
    """
    if 'ResponseMetadata' not in response:
        return 200
    metadata = response['ResponseMetadata']
    if not isinstance(metadata, Mapping):
        raise _malformed_success('ResponseMetadata is not a mapping')
    status = metadata.get('HTTPStatusCode')
    if (isinstance(status, bool) or not isinstance(status, int)
            or not 200 <= status <= 299):
        raise _malformed_success('HTTPStatusCode is not a valid 2xx integer')
    return status


def _optional_string(
    value: object,
    *,
    field: str,
) -> Optional[str]:
    """Normalize an optional SDK string scalar.

    Args:
        value: SDK value.
        field: Field name for a safe refusal.

    Returns:
        The string or None.

    Raises:
        S3StatusError: If another type was supplied.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise _malformed_success(f'{field} is not a string or null')
    return value


def _optional_timestamp(
    value: object,
    *,
    field: str,
) -> Optional[str]:
    """Normalize an optional SDK datetime/ISO string without leaking it.

    Args:
        value: SDK datetime, ISO string, or None.
        field: Field name for a safe refusal.

    Returns:
        An ISO-8601 string or None.

    Raises:
        S3StatusError: If the value is not a supported timestamp.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as error:
            raise _malformed_success(
                f'{field} is not an ISO-8601 timestamp') from error
        return value
    raise _malformed_success(f'{field} is not a timestamp or null')


def _optional_size(value: object, *, field: str) -> Optional[int]:
    """Normalize an optional non-negative byte count.

    Args:
        value: SDK integer or None.
        field: Field name for a safe refusal.

    Returns:
        Non-negative integer or None.

    Raises:
        S3StatusError: If the count is malformed.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _malformed_success(
            f'{field} is not a non-negative integer or null')
    return value


class S3Request(BaseRequestClass):
    """Execute one validated bounded S3 operation."""

    REQUIRED_INFO_KEYS: ClassVar[frozenset[str]] = frozenset({'command'})
    ALLOWED_INFO_KEYS: ClassVar[frozenset[str]] = S3_ALLOWED_INFO_KEYS
    ACCEPTED_INFO_KEYS: ClassVar[frozenset[str]] = ALLOWED_INFO_KEYS

    @classmethod
    def validate_protocol_info(
        cls,
        info: Optional[Mapping[str, Any]],
        *,
        protocol: Optional[str] = None,
    ) -> dict[str, Any]:
        """Validate S3's command-dependent option inventory at the boundary.

        Args:
            info: Caller-supplied protocol mapping or None.
            protocol: Normalized public selector used in diagnostics.

        Returns:
            A copied mapping with a normalized command and validated values.

        Raises:
            ConfigurationError: For an unknown command, an option belonging
            to another command, a missing local path, or a malformed option.
        """
        validated = super().validate_protocol_info(
            info, protocol=protocol)
        raw_command = validated['command']
        command = (
            raw_command.strip().lower()
            if isinstance(raw_command, str) else None)
        if command not in S3_COMMANDS:
            raise ConfigurationError(
                "protocol_info['command'] must name one of "
                f'{sorted(S3_COMMANDS)}, got {raw_command!r}')
        normalized_command = cast(str, command)
        validated['command'] = normalized_command

        allowed = S3_OPERATION_INFO_KEYS[normalized_command]
        disallowed = sorted(set(validated) - set(allowed))
        if disallowed:
            raise ConfigurationError(
                f'protocol_info key(s) {disallowed} are not accepted for '
                f'S3 command {normalized_command!r}')

        if 'region' in validated:
            validated['region'] = _nonempty_string(
                validated['region'], setting='region')
        if normalized_command in {'download', 'upload'}:
            if 'local_path' not in validated:
                raise ConfigurationError(
                    f'S3 {normalized_command} requires '
                    "protocol_info['local_path']")
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
        if normalized_command == 'list':
            validated['max_items'] = _max_items(
                validated.get('max_items', 1000))
            if 'continuation_token' in validated:
                validated['continuation_token'] = _nonempty_string(
                    validated['continuation_token'],
                    setting='continuation_token')
        return validated

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Validate the complete request before SDK Session construction.

        Args:
            *args: Forwarded to :class:`BaseRequestClass`.
            **kwargs: Forwarded to :class:`BaseRequestClass`.

        Raises:
            ConfigurationError: For any malformed target, credential,
            command, region, path, bound, or operation-specific option.
        """
        super().__init__(*args, **kwargs)
        self.bucket, self.key = _s3_target(self.url)
        self.command = cast(str, self.info['command'])
        self.region = cast(Optional[str], self.info.get('region'))

        self.credentials: Optional[Tuple[str, str]] = None
        if self.auth is not None:
            login = getattr(self.auth, 'login', None)
            password = getattr(self.auth, 'password', None)
            if (not isinstance(login, str) or not login.strip()
                    or not isinstance(password, str) or not password.strip()):
                raise ConfigurationError(
                    'S3 auth must expose non-empty string login and password')
            self.credentials = (login, password)

        self.local_path = cast(Optional[str], self.info.get('local_path'))

        if self.command != 'list' and not self.key:
            raise ConfigurationError(
                f'S3 {self.command} requires a non-empty object key')

        self.max_response_bytes = cast(
            int, self.info.get('max_response_bytes', MAX_RESPONSE_BYTES))
        self.max_upload_bytes = cast(
            int, self.info.get('max_upload_bytes', MAX_RESPONSE_BYTES))
        self.max_items = cast(int, self.info.get('max_items', 1000))
        self.continuation_token = cast(
            Optional[str], self.info.get('continuation_token'))

        secrets = list(self.credentials or ())
        if self.continuation_token is not None:
            secrets.append(self.continuation_token)
        self._secret_values = tuple(
            sorted(set(secrets), key=len, reverse=True))
        self._redactor = _KnownSecretRedactor(
            self._secret_values,
            redact_params=self.redact_params,
        )
        self._details_kind: Optional[str] = None
        self._server_next_token: object = _SERVER_TOKEN_ABSENT

    async def handle_request(self) -> GatewayResponse:
        """Run one operation and redact every S3-owned public surface.

        Returns:
            The finalized shared response envelope.

        Raises:
            AsyncGatewayError: For typed service, transport, circuit, or
            local-policy failures. Cancellation and library bugs propagate.
        """
        try:
            return await self._execute()
        except AsyncGatewayError as error:
            self._redactor.exception(error)
            raise
        finally:
            self._redact_response_surfaces()

    async def _execute(self) -> GatewayResponse:
        """Execute the validated SDK operation before final redaction.

        Returns:
            The finalized shared response envelope.

        Raises:
            AsyncGatewayError: For typed service, transport, circuit, or
            local-policy failures. Cancellation and library bugs propagate.
        """
        upload_body: Optional[bytes] = None
        if self.command == 'upload':
            upload_body = await read_guarded_file(
                cast(str, self.local_path),
                max_bytes=self.max_upload_bytes,
            )

        session_kwargs: Dict[str, Any] = {}
        if self.credentials is not None:
            session_kwargs['aws_access_key_id'] = self.credentials[0]
            session_kwargs['aws_secret_access_key'] = self.credentials[1]
        if self.region is not None:
            session_kwargs['region_name'] = self.region

        try:
            session = aioboto3.Session(**session_kwargs)
            client_config = Config(retries={'total_max_attempts': 1})
            client_context = session.client('s3', config=client_config)
            operation = await self._run_client_context(
                client_context, upload_body)
        except (NoCredentialsError, PartialCredentialsError) as error:
            error.args = ('S3 credential resolution failed',)
            raise ConfigurationError(
                f'S3 credentials could not be resolved for bucket '
                f'{self.bucket!r}') from None
        except SDK_TRANSPORT_FAILURES as error:
            raise _sdk_transport_error(
                error, command=self.command, bucket=self.bucket) from None

        response = _response_mapping(operation['response'])
        status = _success_status(response)
        details = self._success_details(
            response, byte_count=operation['byte_count'])
        self.response['protocol_details'] = details
        result = finalise_ok(
            self.response, status_code=status, started=self.start_time)
        self._details_kind = 'success'
        if self.command == 'list':
            self._server_next_token = details['next_continuation_token']
        return result

    async def _run_client_context(
        self,
        client_context: Any,
        upload_body: Optional[bytes],
    ) -> S3OperationResult:
        """Manually enter, operate, and provenance-safely exit one client.

        Args:
            client_context: aioboto3 async client context object.
            upload_body: One pre-read bounded upload body, when applicable.

        Returns:
            The exact successful operation result.

        Raises:
            BaseException: Exact body, cleanup, or parent-cancellation failure.
            RuntimeError: If a hostile context suppresses a body failure.
        """
        client = await client_context.__aenter__()
        operation: S3OperationResult
        body_error: Optional[BaseException] = None
        body_state: Optional[_ExceptionState] = None
        try:
            try:
                operation = await self.circuit_breaker.run(
                    self._attempt, client, upload_body)
            except CircuitOpen as error:
                raise CircuitOpenError(
                    f"circuit open for {self.response['url']}"
                ) from error
            except _AbortableS3ServiceError as error:
                raise self._public_service_error(error) from error
            except RetriesExhausted as error:
                cause = error.__cause__
                if isinstance(cause, _RetryableS3ServiceError):
                    raise self._public_service_error(cause) from cause
                if isinstance(cause, AsyncGatewayError):
                    raise cause from error
                raise TransportError(
                    f'S3 {self.command} failed for bucket '
                    f'{self.bucket!r}') from error
        except BaseException as error:
            body_error = error
            body_state = _capture_exception_state(
                error, error.__traceback__)

        pending_cancellation = (
            body_error
            if isinstance(body_error, asyncio.CancelledError) else None)
        cancellation_state = (
            body_state if pending_cancellation is not None else None)
        outcome, pending_cancellation, cancellation_state = (
            await _drain_client_exit(
                client_context,
                body_error,
                body_state['traceback'] if body_state is not None else None,
                pending_cancellation,
                cancellation_state,
            )
        )

        if pending_cancellation is not None:
            assert cancellation_state is not None
            _raise_exact(pending_cancellation, cancellation_state)

        exit_error = outcome['error']
        if exit_error is not None:
            exit_state = outcome['error_state']
            assert exit_state is not None
            _raise_exact(exit_error, exit_state)

        if body_error is not None:
            assert body_state is not None
            if not outcome['suppressed']:
                _raise_exact(body_error, body_state)
            raise RuntimeError(
                'S3 client context suppressed a body failure without an '
                'operation') from None

        return operation

    def _redact_response_surfaces(self) -> None:
        """Redact S3 URL/details while preserving only a server token.

        ``next_continuation_token`` is an opaque server value the contract
        explicitly returns verbatim. It is the sole exception even when its
        bytes collide with a credential or the caller's request token.
        Caller payload is intentionally untouched here: the shared envelope
        sealer owns its bounded depth-4 sensitive-key policy.

        Returns:
            None.
        """
        self.response['url'] = self._redactor.text(self.response['url'])
        details = self.response['protocol_details']
        redacted = self._redactor.fixed_mapping_values(details)
        if self._details_kind == 'success' and self.command == 'head':
            metadata = details['metadata']
            redacted['metadata'] = self._redactor.dynamic_mapping(
                cast(Mapping[str, str], metadata))
        if self._details_kind == 'success' and self.command == 'list':
            items = cast(list[Mapping[str, Any]], details['items'])
            redacted['items'] = [
                self._redactor.fixed_mapping_values(item)
                for item in items
            ]
        if self._details_kind == 'service':
            metadata = cast(
                Mapping[str, Any], details['response_metadata'])
            redacted['response_metadata'] = (
                self._redactor.fixed_mapping_values(metadata))
        if self._server_next_token is not _SERVER_TOKEN_ABSENT:
            redacted['next_continuation_token'] = self._server_next_token
        self.response['protocol_details'] = redacted

    async def _attempt(
        self,
        client: Any,
        upload_body: Optional[bytes],
    ) -> S3OperationResult:
        """Perform one SDK attempt, converting failures for the breaker.

        Args:
            client: Open aioboto3 S3 client.
            upload_body: One pre-read bounded body for uploads.

        Returns:
            Raw SDK response plus an optional observed transfer byte count.

        Raises:
            _RetryableS3ServiceError: For allowlisted service failures.
            _AbortableS3ServiceError: For every other service failure.
            AsyncGatewayError: For safe transport/configuration mappings.
        """
        try:
            if self.command == 'download':
                result = await stream_s3_download(
                    client,
                    bucket_name=self.bucket,
                    s3_filepath=self.key,
                    local_filepath=cast(str, self.local_path),
                    overwrite=False,
                    max_response_bytes=self.max_response_bytes,
                )
                return S3OperationResult(
                    response=result['response'],
                    byte_count=result['bytes_written'],
                )
            if self.command == 'upload':
                body = cast(bytes, upload_body)
                response = await client.put_object(
                    Bucket=self.bucket, Key=self.key, Body=body)
                return S3OperationResult(
                    response=response, byte_count=len(body))
            if self.command == 'head':
                response = await client.head_object(
                    Bucket=self.bucket, Key=self.key)
                return S3OperationResult(response=response, byte_count=None)
            response = await client.list_objects_v2(
                **self._list_arguments())
            return S3OperationResult(response=response, byte_count=None)
        except ClientError as error:
            sdk_response = _response_mapping(error.response)
            # ``unwrap_cause`` deliberately walks suppressed contexts too.
            # Keep the original structured response on our private failure,
            # but make the foreign exception's string safe before it can
            # become that context and reach an envelope/log traceback.
            error.args = ('S3 service response',)
            if _is_retryable_service_response(sdk_response):
                raise _RetryableS3ServiceError(sdk_response) from None
            raise _AbortableS3ServiceError(sdk_response) from None
        except (NoCredentialsError, PartialCredentialsError) as error:
            error.args = ('S3 credential resolution failed',)
            raise ConfigurationError(
                f'S3 credentials could not be resolved for bucket '
                f'{self.bucket!r}') from None
        except SDK_TRANSPORT_FAILURES as error:
            raise _sdk_transport_error(
                error, command=self.command, bucket=self.bucket) from None

    def _list_arguments(self) -> Dict[str, Any]:
        """Build the exact one-page SDK list arguments.

        Returns:
            Bucket, prefix, page bound, and optional continuation token.
        """
        arguments: Dict[str, Any] = {
            'Bucket': self.bucket,
            'Prefix': self.key,
            'MaxKeys': self.max_items,
        }
        if self.continuation_token is not None:
            arguments['ContinuationToken'] = self.continuation_token
        return arguments

    def _safe_text(self, value: object) -> Optional[str]:
        """Return one SDK string with known credentials/tokens removed.

        Args:
            value: Candidate SDK string.

        Returns:
            Redacted text, or None for a non-string value.
        """
        if not isinstance(value, str):
            return None
        return self._redactor.text(value)

    def _public_service_error(
        self,
        error: Union[
            _RetryableS3ServiceError,
            _AbortableS3ServiceError,
        ],
    ) -> S3StatusError:
        """Populate safe AWS failure details and build public S3_STATUS.

        Args:
            error: Private breaker-control failure retaining the SDK response.

        Returns:
            Stable public service error with the real valid AWS status.
        """
        sdk_response = error.sdk_response
        raw_error = sdk_response.get('Error')
        error_fields = raw_error if isinstance(raw_error, Mapping) else {}
        raw_metadata = sdk_response.get('ResponseMetadata')
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        status, _ = _service_components(sdk_response)
        public_status = (
            status if status is not None and 100 <= status <= 599 else None)
        code = self._safe_text(error_fields.get('Code'))
        message = self._safe_text(error_fields.get('Message'))
        request_id = self._safe_text(metadata.get('RequestId'))
        host_id = self._safe_text(metadata.get('HostId'))
        retry_attempts = metadata.get('RetryAttempts')
        if (isinstance(retry_attempts, bool)
                or not isinstance(retry_attempts, int)
                or retry_attempts < 0):
            retry_attempts = None
        target_field = 'prefix' if self.command == 'list' else 'key'
        self.response['protocol_details'] = {
            'command': self.command,
            'bucket': self.bucket,
            target_field: self.key,
            'aws_error_code': code,
            'aws_error_message': message,
            'request_id': request_id,
            'response_metadata': {
                'http_status_code': public_status,
                'request_id': request_id,
                'host_id': host_id,
                'retry_attempts': retry_attempts,
            },
        }
        self._details_kind = 'service'
        safe_code = code or 'Unknown'
        return S3StatusError(
            f'S3 {self.command} failed for bucket {self.bucket!r} with '
            f'AWS code {safe_code!r}',
            public_status,
        )

    def _success_details(
        self,
        response: Mapping[str, Any],
        *,
        byte_count: Optional[int],
    ) -> Dict[str, Any]:
        """Normalize the exact success schema for the selected operation.

        Args:
            response: SDK success mapping.
            byte_count: Observed transfer count for download/upload.

        Returns:
            JSON-safe exact protocol details.

        Raises:
            S3StatusError: If any SDK success field is malformed.
        """
        if self.command == 'download':
            return {
                'command': self.command,
                'bucket': self.bucket,
                'key': self.key,
                'local_path': self.local_path,
                'bytes_written': self._required_byte_count(byte_count),
                'etag': _optional_string(
                    response.get('ETag'), field='ETag'),
            }
        if self.command == 'upload':
            return {
                'command': self.command,
                'bucket': self.bucket,
                'key': self.key,
                'local_path': self.local_path,
                'bytes_read': self._required_byte_count(byte_count),
                'etag': _optional_string(
                    response.get('ETag'), field='ETag'),
            }
        if self.command == 'head':
            return self._head_details(response)
        return self._list_details(response)

    @staticmethod
    def _required_byte_count(value: Optional[int]) -> int:
        """Require a non-negative observed transfer count.

        Args:
            value: Count returned by the guarded transfer seam.

        Returns:
            Non-negative count.

        Raises:
            S3StatusError: If the seam returned a malformed count.
        """
        normalized = _optional_size(value, field='transfer byte count')
        if normalized is None:
            raise _malformed_success('transfer byte count is missing')
        return normalized

    def _head_details(self, response: Mapping[str, Any]) -> Dict[str, Any]:
        """Normalize one head response.

        Args:
            response: SDK head response.

        Returns:
            Exact JSON-safe head details.

        Raises:
            S3StatusError: If metadata is not a string mapping.
        """
        raw_metadata = response.get('Metadata', {})
        if not isinstance(raw_metadata, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_metadata.items()
        ):
            raise _malformed_success('Metadata is not a string mapping')
        return {
            'command': self.command,
            'bucket': self.bucket,
            'key': self.key,
            'content_length': _optional_size(
                response.get('ContentLength'), field='ContentLength'),
            'content_type': _optional_string(
                response.get('ContentType'), field='ContentType'),
            'etag': _optional_string(response.get('ETag'), field='ETag'),
            'last_modified': _optional_timestamp(
                response.get('LastModified'), field='LastModified'),
            'metadata': dict(raw_metadata),
        }

    def _list_details(self, response: Mapping[str, Any]) -> Dict[str, Any]:
        """Normalize one bounded list page without following its token.

        Args:
            response: SDK list response.

        Returns:
            Exact JSON-safe list details preserving service order.

        Raises:
            S3StatusError: If page or item metadata is malformed.
        """
        raw_items = response.get('Contents', [])
        if not isinstance(raw_items, list):
            raise _malformed_success('Contents is not a list')
        items = [self._list_item(item) for item in raw_items]
        is_truncated = response.get('IsTruncated', False)
        if not isinstance(is_truncated, bool):
            raise _malformed_success('IsTruncated is not a boolean')
        next_token = _optional_string(
            response.get('NextContinuationToken'),
            field='NextContinuationToken')
        if next_token == '':
            raise _malformed_success('NextContinuationToken is empty')
        return {
            'command': self.command,
            'bucket': self.bucket,
            'prefix': self.key,
            'items': items,
            'key_count': len(items),
            'is_truncated': is_truncated,
            'next_continuation_token': next_token,
        }

    @staticmethod
    def _list_item(value: object) -> Dict[str, Any]:
        """Normalize one exact list-item schema.

        Args:
            value: Raw SDK item.

        Returns:
            JSON-safe item mapping.

        Raises:
            S3StatusError: If any required/optional field is malformed.
        """
        if not isinstance(value, Mapping):
            raise _malformed_success('list item is not a mapping')
        key = value.get('Key')
        if not isinstance(key, str) or not key:
            raise _malformed_success('list item Key is not a non-empty string')
        size = _optional_size(value.get('Size'), field='list item Size')
        if size is None:
            raise _malformed_success('list item Size is missing')
        return {
            'key': key,
            'size': size,
            'etag': _optional_string(
                value.get('ETag'), field='list item ETag'),
            'last_modified': _optional_timestamp(
                value.get('LastModified'), field='list item LastModified'),
            'storage_class': _optional_string(
                value.get('StorageClass'), field='list item StorageClass'),
        }

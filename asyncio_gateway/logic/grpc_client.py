"""First-class bounded unary-unary gRPC strategy.

The adapter deliberately stays at gRPC's generic bytes layer: it creates one
``grpc.aio`` channel per gateway call, sends one unary request, and exposes no
generated-stub, reflection, streaming, custom-root, or arbitrary channel-option
surface.  Caller configuration is validated before the channel exists and
peer status data is normalized before it crosses the breaker boundary.
"""

import asyncio
import base64
import inspect
import math
import re
import ssl
from collections.abc import (Awaitable, Callable, Collection, Mapping,
                             Sequence)
from types import TracebackType
from typing import (Any, ClassVar, NoReturn, Optional, Protocol, Tuple,
                    TypedDict, Union, cast)
from urllib.parse import urlsplit

from failsafe import CircuitOpen, RetriesExhausted

# `type: ignore[import-untyped]` -- grpcio has no py.typed marker.
import grpc  # type: ignore[import-untyped]

from asyncio_gateway.helpers.internal.base import BaseRequestClass
from asyncio_gateway.helpers.internal.circuit_breaker_helper import (
    _GrpcAbortableStatusFailure,
)
from asyncio_gateway.logic.http_client import (
    validated_max_response_bytes,
    validated_timeout,
)
from asyncio_gateway.utils.constants import HTTP_TIMEOUT, MAX_RESPONSE_BYTES
from asyncio_gateway.utils.envelope import GatewayResponse, finalise_ok
from asyncio_gateway.utils.exceptions import (
    AsyncGatewayError,
    CircuitOpenError,
    ConfigurationError,
    ConnectError,
    GatewayTimeoutError,
    GrpcStatusError,
    SerializationError,
    TlsError,
    TransportError,
)
from asyncio_gateway.utils.redaction import (
    SENSITIVE_HEADERS,
    SENSITIVE_NAMES,
    redact_text,
)


GRPC_INFO_KEYS = frozenset({
    'method',
    'metadata',
    'request_serializer',
    'response_deserializer',
    'timeout',
    'max_response_bytes',
    'circuit_breaker_config',
    'redact_query_params',
})
GRPC_RETRYABLE_STATUSES = frozenset({
    grpc.StatusCode.UNKNOWN,
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.INTERNAL,
    grpc.StatusCode.UNAVAILABLE,
})
GRPC_HTTP_STATUS = {
    grpc.StatusCode.CANCELLED: 499,
    grpc.StatusCode.UNKNOWN: 502,
    grpc.StatusCode.INVALID_ARGUMENT: 400,
    grpc.StatusCode.DEADLINE_EXCEEDED: 504,
    grpc.StatusCode.NOT_FOUND: 404,
    grpc.StatusCode.ALREADY_EXISTS: 409,
    grpc.StatusCode.PERMISSION_DENIED: 403,
    grpc.StatusCode.RESOURCE_EXHAUSTED: 429,
    grpc.StatusCode.FAILED_PRECONDITION: 412,
    grpc.StatusCode.ABORTED: 409,
    grpc.StatusCode.OUT_OF_RANGE: 400,
    grpc.StatusCode.UNIMPLEMENTED: 501,
    grpc.StatusCode.INTERNAL: 500,
    grpc.StatusCode.UNAVAILABLE: 503,
    grpc.StatusCode.DATA_LOSS: 500,
    grpc.StatusCode.UNAUTHENTICATED: 401,
}

_METHOD = re.compile(
    r'/[A-Za-z_][A-Za-z0-9_]*'
    r'(?:\.[A-Za-z_][A-Za-z0-9_]*)+'
    r'/[A-Za-z_][A-Za-z0-9_]*\Z')
_METADATA_KEY = re.compile(r'[0-9a-z_.-]{1,64}\Z')
_MAX_REQUEST_METADATA_PAIRS = 64
_MAX_REQUEST_METADATA_VALUE = 8192
_MAX_REQUEST_METADATA_TOTAL = 32768
_MAX_PEER_METADATA_PAIRS = 64
_PEER_REDACTED = '***'
_GRPC_MAX_RECEIVE_BYTES = 2 ** 31 - 1

RequestMetadata = Tuple[Tuple[str, str], ...]
PeerMetadata = Optional[Sequence[Tuple[str, Union[str, bytes]]]]


class _UnaryCall(Awaitable[bytes], Protocol):
    """The small part of grpcio's unary call used by this adapter."""

    def cancel(self) -> bool:
        """Cancel the in-flight RPC.

        Returns:
            Whether grpcio accepted the cancellation request.
        """

    def initial_metadata(self) -> Awaitable[PeerMetadata]:
        """Return the peer's initial metadata.

        Returns:
            An awaitable resolving to the optional ordered metadata.
        """

    def trailing_metadata(self) -> Awaitable[PeerMetadata]:
        """Return the peer's trailing metadata.

        Returns:
            An awaitable resolving to the optional ordered metadata.
        """


class _UnaryCallable(Protocol):
    """A generic raw-bytes unary-unary callable."""

    def __call__(
        self,
        body: bytes,
        *,
        timeout: float,
        metadata: RequestMetadata,
    ) -> _UnaryCall:
        """Start one unary call.

        Args:
            body: Frozen serialized request bytes.
            timeout: Positive call deadline in seconds.
            metadata: Validated ordered request metadata.

        Returns:
            The awaitable, cancellable unary call.
        """


class _Channel(Protocol):
    """The portion of an asynchronous gRPC channel the strategy needs."""

    def unary_unary(
        self,
        method: str,
        request_serializer: Any = None,
        response_deserializer: Any = None,
    ) -> _UnaryCallable:
        """Create a generic unary-unary callable.

        Args:
            method: Fully qualified method path.
            request_serializer: Must stay None for raw bytes.
            response_deserializer: Must stay None for raw bytes.

        Returns:
            The raw generic unary callable.
        """

    def close(self, grace: Optional[float] = None) -> Awaitable[None]:
        """Close the channel.

        Args:
            grace: Optional graceful-shutdown duration.

        Returns:
            An awaitable that completes when channel cleanup is finished.
        """


class _GrpcStatusSnapshot(TypedDict):
    """Safe status and metadata retained across the breaker seam."""

    status: Any
    status_name: str
    details: Optional[str]
    initial_metadata: list[dict[str, Any]]
    trailing_metadata: list[dict[str, Any]]
    initial_metadata_omitted: int
    trailing_metadata_omitted: int


class _GrpcOperationResult(TypedDict):
    """One received raw result and its already-normalized metadata."""

    response: object
    initial_metadata: list[dict[str, Any]]
    trailing_metadata: list[dict[str, Any]]
    initial_metadata_omitted: int
    trailing_metadata_omitted: int


class _ExceptionState(TypedDict):
    """Exception attributes cleanup must not be allowed to replace."""

    args: Tuple[Any, ...]
    cause: Optional[BaseException]
    context: Optional[BaseException]
    suppress_context: bool
    traceback: Optional[TracebackType]


class _CloseOutcome(TypedDict):
    """A non-raising result returned by the dedicated close task."""

    error: Optional[BaseException]
    error_state: Optional[_ExceptionState]


class _RetryableGrpcStatusFailure(AsyncGatewayError):
    """Private transport-like status the breaker may count and retry."""

    def __init__(self, status: _GrpcStatusSnapshot) -> None:
        """Retain a normalized status without native grpcio data.

        Args:
            status: Safe status and peer-metadata snapshot.
        """
        super().__init__('retryable gRPC status')
        self.status = status


def _is_async_callable(value: object) -> bool:
    """Report whether calling ``value`` is declared asynchronous."""
    return bool(
        inspect.iscoroutinefunction(value)
        or inspect.iscoroutinefunction(getattr(value, '__call__', None))
    )


def _validated_sync_callable(
    value: object,
    *,
    setting: str,
) -> Callable[..., Any]:
    """Return a supplied synchronous callable or reject its shape.

    Args:
        value: Candidate hook.
        setting: Public option name used in the refusal.

    Returns:
        The callable after validation.

    Raises:
        ConfigurationError: If the value is not callable or is asynchronous.
    """
    if not callable(value):
        raise ConfigurationError(
            f'protocol_info["{setting}"] must be callable, got '
            f'{type(value).__name__}')
    if _is_async_callable(value):
        raise ConfigurationError(
            f'protocol_info["{setting}"] must be synchronous')
    return value


def _validated_method(value: object) -> str:
    """Return one fully qualified unary method path.

    Args:
        value: Caller-supplied method.

    Returns:
        The unchanged valid path.

    Raises:
        ConfigurationError: If the path is not ``/package.Service/Method``.
    """
    if not isinstance(value, str) or _METHOD.fullmatch(value) is None:
        raise ConfigurationError(
            'protocol_info["method"] must match '
            "'/package.Service/Method'")
    return value


def _validated_metadata(value: object) -> RequestMetadata:
    """Validate and freeze caller request metadata.

    Args:
        value: Optional sequence of ``(key, value)`` pairs.

    Returns:
        An immutable pair sequence preserving order and duplicates.

    Raises:
        ConfigurationError: If any count, grammar, type, or size bound fails.
    """
    if value is None:
        return ()
    if (isinstance(value, (str, bytes, bytearray, Mapping))
            or not isinstance(value, Sequence)):
        raise ConfigurationError(
            'protocol_info["metadata"] must be a sequence of '
            '(key, value) pairs')
    if len(value) > _MAX_REQUEST_METADATA_PAIRS:
        raise ConfigurationError(
            'protocol_info["metadata"] may contain at most 64 pairs')

    result: list[Tuple[str, str]] = []
    total = 0
    for pair in value:
        if (isinstance(pair, (str, bytes, bytearray))
                or not isinstance(pair, Sequence) or len(pair) != 2):
            raise ConfigurationError(
                'protocol_info["metadata"] entries must be '
                '(key, value) pairs')
        key, metadata_value = pair
        if (not isinstance(key, str)
                or _METADATA_KEY.fullmatch(key) is None
                or key.startswith('grpc-')
                or key.endswith('-bin')):
            raise ConfigurationError(
                'protocol_info["metadata"] keys must be 1..64 lowercase '
                'ASCII [0-9a-z_.-]+ names excluding grpc-* and *-bin')
        if not isinstance(metadata_value, str):
            raise ConfigurationError(
                'protocol_info["metadata"] values must be printable '
                'ASCII strings')
        if (len(metadata_value) > _MAX_REQUEST_METADATA_VALUE
                or any(not 0x20 <= ord(char) <= 0x7e
                       for char in metadata_value)):
            raise ConfigurationError(
                'protocol_info["metadata"] values must be at most 8192 '
                'printable ASCII characters')
        total += len(metadata_value)
        if total > _MAX_REQUEST_METADATA_TOTAL:
            raise ConfigurationError(
                'protocol_info["metadata"] aggregate value length must '
                'be at most 32768 characters')
        result.append((key, metadata_value))
    return tuple(result)


def _validated_grpc_timeout(value: object) -> float:
    """Apply gRPC's finite-deadline requirement after shared validation.

    Args:
        value: Candidate timeout in the shared validator's public shape.

    Returns:
        A positive finite number of seconds.

    Raises:
        ConfigurationError: If the shared validation fails or the resulting
            number is NaN or infinite.
    """
    timeout = validated_timeout(value)
    if not math.isfinite(timeout):
        raise ConfigurationError(
            'protocol_info["timeout"] must be finite for gRPC')
    return timeout


def _validated_grpc_max_response_bytes(value: object) -> int:
    """Apply grpcio's signed-C-int ceiling after shared cap validation.

    Args:
        value: Candidate positive receive ceiling.

    Returns:
        A positive byte count representable by grpcio's channel option.

    Raises:
        ConfigurationError: If shared validation fails or the value exceeds
            signed C ``INT_MAX``.
    """
    maximum = validated_max_response_bytes(value)
    if maximum > _GRPC_MAX_RECEIVE_BYTES:
        raise ConfigurationError(
            'protocol_info["max_response_bytes"] must be <= '
            f'{_GRPC_MAX_RECEIVE_BYTES} for gRPC')
    return maximum


def _grpc_target(url: str) -> Tuple[str, str]:
    """Return the scheme and explicit ``host:port`` channel target.

    Args:
        url: Caller target after the public selector scheme guard.

    Returns:
        ``('grpc'|'grpcs', target)``.

    Raises:
        ConfigurationError: If any ambiguous or unsupported URI component is
            present, including an empty query or fragment delimiter.
    """
    if '?' in url or '#' in url:
        raise ConfigurationError(
            'gRPC target does not support a query string or fragment')
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise ConfigurationError(
            'gRPC target is not a parseable URI') from error
    scheme = parts.scheme.lower()
    if scheme not in {'grpc', 'grpcs'}:
        raise ConfigurationError(
            'gRPC target must use the grpc or grpcs scheme')
    if not parts.hostname:
        raise ConfigurationError('gRPC target requires a host')
    if (parts.username is not None or parts.password is not None
            or '@' in parts.netloc):
        raise ConfigurationError('gRPC target does not support user info')
    if parts.path:
        raise ConfigurationError('gRPC target does not support a path')
    if port is None or not 1 <= port <= 65535:
        raise ConfigurationError(
            'gRPC target requires an explicit numeric port in 1..65535')
    return scheme, parts.netloc


def _discard_awaitable(value: object) -> None:
    """Close a rejected coroutine-like value to avoid a runtime warning."""
    close = getattr(value, 'close', None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _json_safe(value: object) -> bool:
    """Validate finite JSON-safe data iteratively and cycle-safely."""
    stack: list[Tuple[object, bool]] = [(value, False)]
    active: set[int] = set()
    while stack:
        current, leaving = stack.pop()
        if isinstance(current, (list, Mapping)):
            identity = id(current)
            if leaving:
                active.remove(identity)
                continue
            if identity in active:
                return False
            active.add(identity)
            stack.append((current, True))
            if isinstance(current, list):
                stack.extend((item, False) for item in reversed(current))
            else:
                items = list(current.items())
                if any(not isinstance(key, str) for key, _ in items):
                    return False
                stack.extend((item, False) for _, item in reversed(items))
            continue
        if current is None or isinstance(current, (str, bool, int)):
            continue
        if isinstance(current, float) and math.isfinite(current):
            continue
        return False
    return True


def _capture_exception_state(
    error: BaseException,
    traceback: Optional[TracebackType],
) -> _ExceptionState:
    """Capture exception identity-adjacent state before channel cleanup."""
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
    """Restore an exception's arguments and chaining attributes."""
    error.args = state['args']
    error.__cause__ = state['cause']
    error.__context__ = state['context']
    error.__suppress_context__ = state['suppress_context']


def _raise_exact(error: BaseException, state: _ExceptionState) -> NoReturn:
    """Raise an exact captured exception with its original public state."""
    _restore_exception_state(error, state)
    try:
        raise error.with_traceback(state['traceback'])
    finally:
        _restore_exception_state(error, state)


async def _capture_channel_close(channel: _Channel) -> _CloseOutcome:
    """Close one channel and turn every cleanup failure into task data."""
    try:
        await channel.close()
    except BaseException as error:
        return {
            'error': error,
            'error_state': _capture_exception_state(
                error, error.__traceback__),
        }
    return {'error': None, 'error_state': None}


async def _drain_channel_close(
    channel: _Channel,
    pending_cancellation: Optional[asyncio.CancelledError],
    cancellation_state: Optional[_ExceptionState],
) -> Tuple[
    _CloseOutcome,
    Optional[asyncio.CancelledError],
    Optional[_ExceptionState],
]:
    """Shield and drain cleanup while retaining the first parent cancel."""
    close_task = asyncio.create_task(_capture_channel_close(channel))
    while True:
        try:
            outcome = await asyncio.shield(close_task)
        except asyncio.CancelledError as error:
            if pending_cancellation is None:
                pending_cancellation = error
                cancellation_state = _capture_exception_state(
                    error, error.__traceback__)
        else:
            return outcome, pending_cancellation, cancellation_state


class GrpcRequest(BaseRequestClass):
    """Execute one validated raw unary-unary gRPC call."""

    REQUIRED_INFO_KEYS: ClassVar[frozenset[str]] = frozenset({'method'})
    ALLOWED_INFO_KEYS: ClassVar[frozenset[str]] = GRPC_INFO_KEYS
    ACCEPTED_INFO_KEYS: ClassVar[frozenset[str]] = ALLOWED_INFO_KEYS

    @classmethod
    def validate_protocol_info(
        cls,
        info: Optional[Mapping[str, Any]],
        *,
        protocol: Optional[str] = None,
    ) -> dict[str, Any]:
        """Validate and normalize the closed gRPC option inventory.

        Args:
            info: Caller-supplied protocol mapping or None.
            protocol: Normalized public selector used in diagnostics.

        Returns:
            A copied mapping containing normalized method, metadata, deadline,
            receive ceiling, and validated optional hooks.

        Raises:
            ConfigurationError: If any caller-controlled option is invalid.
        """
        validated = super().validate_protocol_info(
            info, protocol=protocol)
        validated['method'] = _validated_method(validated['method'])
        validated['metadata'] = _validated_metadata(
            validated.get('metadata'))
        validated['timeout'] = _validated_grpc_timeout(
            validated.get('timeout', HTTP_TIMEOUT))
        validated['max_response_bytes'] = (
            _validated_grpc_max_response_bytes(
                validated.get('max_response_bytes', MAX_RESPONSE_BYTES))
        )
        for setting in ('request_serializer', 'response_deserializer'):
            if setting in validated:
                validated[setting] = _validated_sync_callable(
                    validated[setting], setting=setting)
        return validated

    def __init__(
        self,
        url: str,
        auth: Any,
        response: GatewayResponse,
        info: Optional[Mapping[str, Any]],
        *,
        redact_params: Collection[str],
    ) -> None:
        """Preflight target, auth, and request bytes before breaker lookup.

        Args:
            url: Explicit gRPC or gRPC-over-TLS target.
            auth: Must be None; metadata is the sole call credential surface.
            response: Shared gateway response skeleton.
            info: Already validated protocol options.
            redact_params: Normalized additional sensitive names.

        Raises:
            ConfigurationError: For an invalid target, auth object, or request
                serializer/result.
        """
        self.scheme, self.target = _grpc_target(url)
        if auth is not None:
            raise ConfigurationError('gRPC auth must be None')
        normalized_info = {} if info is None else dict(info)
        payload = response['payload']
        serializer = normalized_info.get('request_serializer')
        if serializer is None:
            try:
                request_body = memoryview(payload).tobytes()
            except (TypeError, ValueError):
                raise ConfigurationError(
                    'gRPC data must be bytes-like without '
                    'request_serializer') from None
        else:
            serializer_failed = False
            serialized: object = None
            try:
                serialized = cast(Callable[[object], object], serializer)(
                    payload)
            except Exception:
                serializer_failed = True
            if serializer_failed:
                raise ConfigurationError(
                    'gRPC request_serializer failed') from None
            if inspect.isawaitable(serialized):
                _discard_awaitable(serialized)
                raise ConfigurationError(
                    'gRPC request_serializer must be synchronous')
            if not isinstance(serialized, bytes):
                raise ConfigurationError(
                    'gRPC request_serializer must return bytes')
            request_body = bytes(serialized)

        super().__init__(
            url, auth, response, normalized_info,
            redact_params=redact_params)
        self.method = cast(str, self.info['method'])
        self.metadata = cast(RequestMetadata, self.info['metadata'])
        self.request_body = request_body
        self.deadline = cast(float, self.info['timeout'])
        self.max_response_bytes = cast(
            int, self.info['max_response_bytes'])
        self.response_deserializer = cast(
            Optional[Callable[[bytes], object]],
            self.info.get('response_deserializer'))
        sensitive_names = (
            SENSITIVE_NAMES | SENSITIVE_HEADERS | self.redact_params)
        self._sensitive_names = frozenset(
            name.casefold() for name in sensitive_names)
        self._secret_values = tuple(sorted({
            value for key, value in self.metadata
            if value and self._metadata_name_is_sensitive(key)
        }, key=len, reverse=True))

    def _metadata_name_is_sensitive(self, key: str) -> bool:
        """Report whether one metadata name denotes a credential value."""
        folded = key.casefold()
        if folded.endswith('-bin'):
            folded = folded[:-4]
        return folded in self._sensitive_names

    def _safe_text(self, value: str) -> str:
        """Apply shared query redaction and exact request-secret masking."""
        safe = redact_text(value, extra_params=self.redact_params)
        for secret in self._secret_values:
            safe = safe.replace(secret, _PEER_REDACTED)
        return safe

    def _peer_metadata(
        self,
        metadata: PeerMetadata,
    ) -> Tuple[list[dict[str, Any]], int]:
        """Normalize and independently bound one peer metadata collection."""
        entries = list(metadata or ())
        normalized: list[dict[str, Any]] = []
        for key, value in entries[:_MAX_PEER_METADATA_PAIRS]:
            sensitive = self._metadata_name_is_sensitive(key)
            if sensitive:
                public_value = _PEER_REDACTED
                encoding: Optional[str] = None
            elif isinstance(value, bytes):
                public_value = base64.b64encode(value).decode('ascii')
                encoding = 'base64'
            else:
                public_value = self._safe_text(value)
                encoding = None
            normalized.append({
                'key': self._safe_text(key),
                'value': public_value,
                'encoding': encoding,
            })
        return normalized, max(0, len(entries) - _MAX_PEER_METADATA_PAIRS)

    def _details(
        self,
        *,
        status_name: str,
        grpc_details: Optional[str],
        response_encoding: Optional[str],
        initial_metadata: list[dict[str, Any]],
        trailing_metadata: list[dict[str, Any]],
        initial_metadata_omitted: int,
        trailing_metadata_omitted: int,
    ) -> dict[str, Any]:
        """Build the exact fixed gRPC protocol-details schema."""
        return {
            'method': self._safe_text(self.method),
            'grpc_status': status_name,
            'grpc_details': grpc_details,
            'response_encoding': response_encoding,
            'initial_metadata': initial_metadata,
            'trailing_metadata': trailing_metadata,
            'initial_metadata_omitted': initial_metadata_omitted,
            'trailing_metadata_omitted': trailing_metadata_omitted,
        }

    def _status_snapshot(self, error: Any) -> _GrpcStatusSnapshot:
        """Read only safe canonical fields from one native status error."""
        status = error.code()
        if status not in GRPC_HTTP_STATUS:
            status = grpc.StatusCode.UNKNOWN
        raw_details = error.details()
        details = (
            self._safe_text(raw_details)
            if isinstance(raw_details, str) else None)
        initial, initial_omitted = self._peer_metadata(
            error.initial_metadata())
        trailing, trailing_omitted = self._peer_metadata(
            error.trailing_metadata())
        return {
            'status': status,
            'status_name': status.name,
            'details': details,
            'initial_metadata': initial,
            'trailing_metadata': trailing,
            'initial_metadata_omitted': initial_omitted,
            'trailing_metadata_omitted': trailing_omitted,
        }

    def _local_resource_exhausted(
        self,
        operation: _GrpcOperationResult,
    ) -> _GrpcStatusSnapshot:
        """Build the uncounted local receive-ceiling status snapshot."""
        return {
            'status': grpc.StatusCode.RESOURCE_EXHAUSTED,
            'status_name': 'RESOURCE_EXHAUSTED',
            'details': 'response exceeded max_response_bytes',
            'initial_metadata': operation['initial_metadata'],
            'trailing_metadata': operation['trailing_metadata'],
            'initial_metadata_omitted': (
                operation['initial_metadata_omitted']),
            'trailing_metadata_omitted': (
                operation['trailing_metadata_omitted']),
        }

    async def _attempt(self, rpc: _UnaryCallable) -> _GrpcOperationResult:
        """Execute one breaker attempt and normalize any native status."""
        call: Optional[_UnaryCall] = None
        native_error: Any = None
        try:
            call = rpc(
                self.request_body,
                timeout=self.deadline,
                metadata=self.metadata,
            )
            response = await call
            initial_raw = await call.initial_metadata()
            trailing_raw = await call.trailing_metadata()
        except asyncio.CancelledError:
            if call is not None:
                try:
                    call.cancel()
                except BaseException:
                    pass
            raise
        except grpc.aio.AioRpcError as error:
            native_error = error

        if native_error is not None:
            snapshot = self._status_snapshot(native_error)
            if snapshot['status'] in GRPC_RETRYABLE_STATUSES:
                raise _RetryableGrpcStatusFailure(snapshot) from None
            raise _GrpcAbortableStatusFailure(snapshot) from None

        initial, initial_omitted = self._peer_metadata(initial_raw)
        trailing, trailing_omitted = self._peer_metadata(trailing_raw)
        operation = _GrpcOperationResult(
            response=response,
            initial_metadata=initial,
            trailing_metadata=trailing,
            initial_metadata_omitted=initial_omitted,
            trailing_metadata_omitted=trailing_omitted,
        )
        if (isinstance(response, bytes)
                and len(response) > self.max_response_bytes):
            raise _GrpcAbortableStatusFailure(
                self._local_resource_exhausted(operation)) from None
        return operation

    def _public_status_error(
        self,
        snapshot: _GrpcStatusSnapshot,
    ) -> GrpcStatusError:
        """Populate the failure schema and build the stable public error."""
        self.response['text'] = ''
        self.response['json'] = None
        self.response['protocol_details'] = self._details(
            status_name=snapshot['status_name'],
            grpc_details=snapshot['details'],
            response_encoding=None,
            initial_metadata=snapshot['initial_metadata'],
            trailing_metadata=snapshot['trailing_metadata'],
            initial_metadata_omitted=(
                snapshot['initial_metadata_omitted']),
            trailing_metadata_omitted=(
                snapshot['trailing_metadata_omitted']),
        )
        return GrpcStatusError(
            self._safe_text(
                f'gRPC status {snapshot["status_name"]} for method '
                f'{self.method}'),
            GRPC_HTTP_STATUS[snapshot['status']],
        )

    def _transport_error(self, error: BaseException) -> AsyncGatewayError:
        """Map a non-status transport failure without foreign prose."""
        message = self._safe_text(
            f'gRPC transport failed for {self.response["url"]}')
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return GatewayTimeoutError(message)
        if isinstance(error, ssl.SSLError):
            return TlsError(message)
        if isinstance(error, OSError):
            return ConnectError(message)
        return TransportError(message)

    async def _execute_channel(self, channel: _Channel) -> GatewayResponse:
        """Run the RPC under the destination breaker and normalize output."""
        rpc = channel.unary_unary(self.method)
        status_snapshot: Optional[_GrpcStatusSnapshot] = None
        public_error: Optional[AsyncGatewayError] = None
        try:
            operation = await self.circuit_breaker.run(self._attempt, rpc)
        except CircuitOpen:
            public_error = CircuitOpenError(
                self._safe_text(
                    f'circuit open for {self.response["url"]}'))
        except _GrpcAbortableStatusFailure as error:
            status_snapshot = cast(_GrpcStatusSnapshot, error.status)
        except RetriesExhausted as error:
            cause = error.__cause__
            if isinstance(cause, _RetryableGrpcStatusFailure):
                status_snapshot = cause.status
            elif isinstance(cause, AsyncGatewayError):
                public_error = cause
            else:
                public_error = self._transport_error(
                    cast(BaseException, cause))

        if status_snapshot is not None:
            raise self._public_status_error(status_snapshot) from None
        if public_error is not None:
            raise public_error from None

        raw = operation['response']
        if not isinstance(raw, bytes):
            self.response['protocol_details'] = self._details(
                status_name='OK',
                grpc_details=None,
                response_encoding=None,
                initial_metadata=operation['initial_metadata'],
                trailing_metadata=operation['trailing_metadata'],
                initial_metadata_omitted=(
                    operation['initial_metadata_omitted']),
                trailing_metadata_omitted=(
                    operation['trailing_metadata_omitted']),
            )
            raise SerializationError(
                'gRPC peer returned a non-bytes response') from None

        self.response['text'] = base64.b64encode(raw).decode('ascii')
        self.response['json'] = None
        self.response['protocol_details'] = self._details(
            status_name='OK',
            grpc_details=None,
            response_encoding='base64',
            initial_metadata=operation['initial_metadata'],
            trailing_metadata=operation['trailing_metadata'],
            initial_metadata_omitted=operation['initial_metadata_omitted'],
            trailing_metadata_omitted=operation['trailing_metadata_omitted'],
        )

        if self.response_deserializer is not None:
            deserializer_failed = False
            normalized: object = None
            try:
                normalized = self.response_deserializer(raw)
            except Exception:
                deserializer_failed = True
            if deserializer_failed:
                raise SerializationError(
                    'gRPC response_deserializer failed') from None
            if inspect.isawaitable(normalized):
                _discard_awaitable(normalized)
                raise SerializationError(
                    'gRPC response_deserializer must be synchronous')
            json_validation_failed = False
            try:
                normalized_is_safe = _json_safe(normalized)
            except Exception:
                json_validation_failed = True
                normalized_is_safe = False
            if json_validation_failed or not normalized_is_safe:
                raise SerializationError(
                    'gRPC response_deserializer returned non-JSON-safe '
                    'data')
            self.response['json'] = cast(Any, normalized)

        return finalise_ok(
            self.response, status_code=200, started=self.start_time)

    def _new_channel(self) -> _Channel:
        """Create the sole channel using the explicit target scheme."""
        options = [(
            'grpc.max_receive_message_length', self.max_response_bytes)]
        if self.scheme == 'grpc':
            return cast(_Channel, grpc.aio.insecure_channel(
                self.target, options=options))
        credentials = grpc.ssl_channel_credentials()
        return cast(_Channel, grpc.aio.secure_channel(
            self.target, credentials, options=options))

    async def _run_channel_lifecycle(
        self,
        channel: _Channel,
    ) -> GatewayResponse:
        """Run the body and provenance-safely close the channel once."""
        result: GatewayResponse
        body_error: Optional[BaseException] = None
        body_state: Optional[_ExceptionState] = None
        try:
            result = await self._execute_channel(channel)
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
            await _drain_channel_close(
                channel, pending_cancellation, cancellation_state))

        if pending_cancellation is not None:
            assert cancellation_state is not None
            _raise_exact(pending_cancellation, cancellation_state)
        close_error = outcome['error']
        if close_error is not None:
            close_state = outcome['error_state']
            assert close_state is not None
            _raise_exact(close_error, close_state)
        if body_error is not None:
            assert body_state is not None
            _raise_exact(body_error, body_state)
        return result

    async def handle_request(self) -> GatewayResponse:
        """Create, use, and close one channel for this gateway call.

        Returns:
            The finalized shared response envelope.

        Raises:
            AsyncGatewayError: For normalized status, serialization,
                transport, or circuit failures.  Cancellation and programming
                errors propagate unchanged after cleanup.
        """
        try:
            channel_error: Optional[BaseException] = None
            try:
                channel = self._new_channel()
            except (asyncio.TimeoutError, OSError) as error:
                channel_error = error
            if channel_error is not None:
                raise self._transport_error(channel_error) from None
            return await self._run_channel_lifecycle(channel)
        finally:
            self.response['url'] = self._safe_text(self.response['url'])

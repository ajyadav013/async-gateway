"""Strict public-boundary strategy for Google Cloud Storage requests."""

import asyncio
import logging
import math
import re
import socket
import ssl
import threading
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import TracebackType
from typing import (
    Any,
    Callable,
    ClassVar,
    Final,
    NoReturn,
    Optional,
    Tuple,
    TypeVar,
    TypedDict,
    cast,
)
from urllib.parse import urlsplit

from failsafe import CircuitOpen, RetriesExhausted

from google import auth as google_auth
from google.api_core import exceptions as google_api_exceptions
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport.requests import Request as GoogleAuthRequest
# google-cloud-storage does not publish a py.typed marker.
from google.cloud import storage  # type: ignore[import-untyped]

from asyncio_gateway.helpers.internal.base import BaseRequestClass
from asyncio_gateway.helpers.internal.circuit_breaker_helper import (
    AbortableServiceError,
)
from asyncio_gateway.utils.constants import (HTTP_TIMEOUT, MAX_RESPONSE_BYTES)
from asyncio_gateway.utils.envelope import GatewayResponse, finalise_ok
from asyncio_gateway.utils.exceptions import (
    AsyncGatewayError,
    CircuitOpenError,
    ConfigurationError,
    ConnectError,
    DnsError,
    GatewayTimeoutError,
    GcsCapacityError,
    GcsStatusError,
    ResponseTooLargeError,
    TlsError,
    TransportError,
)
from asyncio_gateway.utils.paths import read_guarded_file, stream_to_path
from asyncio_gateway.utils.redaction import redact_text


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
_GCS_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({
    408, 429, 500, 502, 503, 504,
})
_GCS_PROVIDER_SECRET_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(
    r'(?:authorization|credential|password|secret|token|api[_-]?key)\s*[:=]',
    re.IGNORECASE,
)
_GCS_TRANSPORT_FAILURES: Final[Tuple[type[BaseException], ...]] = (
    socket.gaierror,
    ssl.SSLError,
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionError,
    OSError,
)
_GCS_CREDENTIAL_FAILURES: Final[Tuple[type[BaseException], ...]] = (
    google_auth_exceptions.DefaultCredentialsError,
    google_auth_exceptions.RefreshError,
)
_GCS_MAX_LEASES: Final[int] = 4
_GCS_DOWNLOAD_CHUNK_SIZE: Final[int] = 64 * 1024
_GCS_EXECUTOR: Final[ThreadPoolExecutor] = ThreadPoolExecutor(
    max_workers=_GCS_MAX_LEASES,
    thread_name_prefix='asyncio-gateway-gcs',
)
_GCS_PERMITS: Final[threading.BoundedSemaphore] = (
    threading.BoundedSemaphore(_GCS_MAX_LEASES))
_GCS_STATE_LOCK = threading.Lock()
_GCS_ACTIVE_LEASES = 0
_GCS_ADMISSION_CLOSING = False
_GCS_EXECUTOR_SHUTDOWN = False

logger = logging.getLogger(__name__)

_ResultT = TypeVar('_ResultT')
_NO_RESULT: Final[object] = object()


class _ExceptionState(TypedDict):
    """Exception attributes cleanup must not be allowed to replace."""

    args: Tuple[Any, ...]
    cause: Optional[BaseException]
    context: Optional[BaseException]
    suppress_context: bool
    traceback: Optional[TracebackType]


class _DownloadPin(TypedDict):
    """Immutable normalized metadata for one exact object generation."""

    generation: int
    size: int
    etag: Optional[str]
    crc32c: Optional[str]


class _GcsServiceFailure(AsyncGatewayError):
    """Retain one safe retryable GCS service-failure snapshot."""

    def __init__(self, status_code: int, details: Mapping[str, Any]) -> None:
        """Store only normalized service fields for later conversion."""
        super().__init__('retryable GCS service response', status_code)
        self.details = dict(details)


class _AbortableGcsServiceFailure(AbortableServiceError):
    """Retain one safe non-retryable GCS service-failure snapshot."""

    def __init__(self, status_code: int, details: Mapping[str, Any]) -> None:
        """Store only normalized service fields for later conversion."""
        super().__init__('abortable GCS service response', status_code)
        self.details = dict(details)


def _capture_exception_state(error: BaseException) -> _ExceptionState:
    """Capture exception identity-adjacent state before client cleanup."""
    return {
        'args': error.args,
        'cause': error.__cause__,
        'context': error.__context__,
        'suppress_context': error.__suppress_context__,
        'traceback': error.__traceback__,
    }


def _restore_exception_state(
    error: BaseException,
    state: _ExceptionState,
) -> None:
    """Restore exception state that foreign cleanup could have disturbed."""
    error.args = state['args']
    error.__cause__ = state['cause']
    error.__context__ = state['context']
    error.__suppress_context__ = state['suppress_context']


def _raise_exact(error: BaseException, state: _ExceptionState) -> NoReturn:
    """Raise one captured failure with its original identity and state."""
    _restore_exception_state(error, state)
    try:
        raise error.with_traceback(state['traceback'])
    finally:
        _restore_exception_state(error, state)


async def _drain_provider_future(
    future: 'asyncio.Future[_ResultT]',
    cancellation: Optional[asyncio.CancelledError],
) -> tuple[object, Optional[asyncio.CancelledError]]:
    """Drain one shielded provider future while retaining first cancel."""
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
        except BaseException:
            break
    try:
        return future.result(), cancellation
    except BaseException:
        return _NO_RESULT, cancellation


class _GcsLease:
    """Serialize private provider calls under one retained capacity permit."""

    def __init__(self) -> None:
        """Create one live lease with no outstanding provider future."""
        self._operation_lock = asyncio.Lock()
        self._released = False

    async def run(
        self,
        function: Callable[..., _ResultT],
        *args: Any,
        timeout: int | float,
        close_result: Optional[Callable[[_ResultT], Any]] = None,
        **kwargs: Any,
    ) -> _ResultT:
        """Run one synchronous provider call with deadline-safe draining."""
        accepted_timeout = _positive_timeout(timeout)
        async with self._operation_lock:
            if self._released:
                raise GcsCapacityError()

            loop = asyncio.get_running_loop()
            started = threading.Event()

            def invoke() -> tuple[bool, object]:
                """Mark provider execution started before calling it."""
                started.set()
                try:
                    return True, function(*args, **kwargs)
                except BaseException as error:
                    return False, error

            future = loop.run_in_executor(
                _GCS_EXECUTOR, invoke)
            cancellation: Optional[asyncio.CancelledError] = None
            timed_out = False
            try:
                while not started.is_set() and not future.done():
                    await asyncio.sleep(0)
                if accepted_timeout < 0.001:
                    await asyncio.sleep(0)
                    if future.done():
                        completed, payload = future.result()
                        if completed:
                            return cast(_ResultT, payload)
                        raise cast(BaseException, payload)
                deadline = loop.time() + float(accepted_timeout)
                completed, payload = await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=max(0.0, deadline - loop.time()),
                )
                if loop.time() < deadline:
                    if completed:
                        return cast(_ResultT, payload)
                    raise cast(BaseException, payload)
                timed_out = True
            except asyncio.CancelledError as error:
                cancellation = error
            except (asyncio.TimeoutError, TimeoutError):
                timed_out = True

            drain_started = loop.time()
            logger.debug('gcs_drain_started')
            late_outcome, cancellation = await _drain_provider_future(
                future, cancellation)
            if (
                late_outcome is not _NO_RESULT
                and cast(tuple[bool, object], late_outcome)[0]
                and close_result is not None
            ):
                late_result = cast(tuple[bool, object], late_outcome)[1]

                def cleanup_invoke() -> tuple[bool, object]:
                    """Turn late-resource cleanup failure into task data."""
                    try:
                        close_result(cast(_ResultT, late_result))
                    except BaseException as error:
                        return False, error
                    return True, None

                cleanup = loop.run_in_executor(
                    _GCS_EXECUTOR,
                    cleanup_invoke,
                )
                _, cancellation = await _drain_provider_future(
                    cleanup, cancellation)
            duration_seconds = max(0.0, loop.time() - drain_started)
            if not math.isfinite(duration_seconds):
                duration_seconds = 0.0
            logger.debug(
                'gcs_drain_finished',
                extra={
                    'active_leases': _active_gcs_leases(),
                    'duration_seconds': duration_seconds,
                },
            )

            if cancellation is not None:
                raise cancellation
            if timed_out:
                raise GatewayTimeoutError(
                    'GCS provider result acceptance deadline expired')
            raise TransportError('GCS provider operation failed')

    async def release(self) -> None:
        """Release this lease once all serialized provider work has drained."""
        async with self._operation_lock:
            if self._released:
                return
            self._released = True
            _release_gcs_lease()


def _active_gcs_leases() -> int:
    """Return the current active-lease count under the state lock."""
    with _GCS_STATE_LOCK:
        return _GCS_ACTIVE_LEASES


def _release_gcs_lease() -> None:
    """Release one permit and close an idle, closing executor once."""
    global _GCS_ACTIVE_LEASES, _GCS_EXECUTOR_SHUTDOWN

    should_shutdown = False
    with _GCS_STATE_LOCK:
        _GCS_PERMITS.release()
        _GCS_ACTIVE_LEASES -= 1
        logger.debug(
            'gcs_capacity_state',
            extra={
                'active_leases': _GCS_ACTIVE_LEASES,
                'max_leases': _GCS_MAX_LEASES,
            },
        )
        if (
            _GCS_ADMISSION_CLOSING
            and _GCS_ACTIVE_LEASES == 0
            and not _GCS_EXECUTOR_SHUTDOWN
        ):
            _GCS_EXECUTOR_SHUTDOWN = True
            should_shutdown = True

    if should_shutdown:
        _GCS_EXECUTOR.shutdown(wait=False, cancel_futures=True)


def _acquire_gcs_lease() -> _GcsLease:
    """Acquire one GCS lifecycle permit immediately or refuse capacity."""
    global _GCS_ACTIVE_LEASES

    with _GCS_STATE_LOCK:
        if _GCS_ADMISSION_CLOSING:
            logger.debug(
                'gcs_capacity_rejected',
                extra={
                    'active_leases': _GCS_ACTIVE_LEASES,
                    'reason': 'closing',
                },
            )
            raise GcsCapacityError()
        if not _GCS_PERMITS.acquire(blocking=False):
            logger.debug(
                'gcs_capacity_rejected',
                extra={
                    'active_leases': _GCS_ACTIVE_LEASES,
                    'reason': 'saturated',
                },
            )
            raise GcsCapacityError()
        _GCS_ACTIVE_LEASES += 1
        logger.debug(
            'gcs_capacity_state',
            extra={
                'active_leases': _GCS_ACTIVE_LEASES,
                'max_leases': _GCS_MAX_LEASES,
            },
        )
    return _GcsLease()


def _shutdown_gcs_offloader() -> None:
    """Close admission and shut down the idle private executor once."""
    global _GCS_ADMISSION_CLOSING, _GCS_EXECUTOR_SHUTDOWN

    should_shutdown = False
    with _GCS_STATE_LOCK:
        if not _GCS_ADMISSION_CLOSING:
            _GCS_ADMISSION_CLOSING = True
            logger.debug(
                'gcs_capacity_closing',
                extra={'active_leases': _GCS_ACTIVE_LEASES},
            )
        if _GCS_ACTIVE_LEASES == 0 and not _GCS_EXECUTOR_SHUTDOWN:
            _GCS_EXECUTOR_SHUTDOWN = True
            should_shutdown = True

    if should_shutdown:
        _GCS_EXECUTOR.shutdown(wait=False, cancel_futures=True)


def _operational_sdk_kwargs(timeout: object) -> dict[str, object]:
    """Return the frozen timeout and retry controls for storage calls."""
    return {'retry': None, 'timeout': _positive_timeout(timeout)}


def _refresh_credentials_if_needed(credentials: Any) -> None:
    """Refresh one invalid ADC credential inside the provider worker."""
    if not credentials.valid:
        credentials.refresh(GoogleAuthRequest())


def _close_storage_client(client: Any) -> None:
    """Close one owned synchronous storage client."""
    client.close()


def _upload_blob(
    blob: Any,
    data: bytes,
    *,
    if_generation_match: int,
    sdk_timeout: int | float,
) -> dict[str, Any]:
    """Upload exact guarded bytes and snapshot the success metadata."""
    blob.upload_from_string(
        data,
        if_generation_match=if_generation_match,
        **_operational_sdk_kwargs(sdk_timeout),
    )
    metadata = {
        'etag': blob.etag,
        'generation': blob.generation,
        'metageneration': blob.metageneration,
        'crc32c': blob.crc32c,
    }
    if any(
        value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        )
        for value in (
            metadata['generation'], metadata['metageneration'],
        )
    ):
        raise _AbortableGcsServiceFailure(502, {})
    if any(
        value is not None and not isinstance(value, str)
        for value in (metadata['etag'], metadata['crc32c'])
    ):
        raise _AbortableGcsServiceFailure(502, {})
    return metadata


def _reload_download_pin(
    blob: Any,
    *,
    if_generation_match: Optional[int],
    sdk_timeout: int | float,
) -> _DownloadPin:
    """Reload and validate one exact stored-object metadata snapshot."""
    arguments = _operational_sdk_kwargs(sdk_timeout)
    if if_generation_match is not None:
        arguments['if_generation_match'] = if_generation_match
    blob.reload(**arguments)

    generation = blob.generation
    size = blob.size
    etag = blob.etag
    crc32c = blob.crc32c
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or if_generation_match is not None
        and generation != if_generation_match
        or etag is not None
        and not isinstance(etag, str)
        or crc32c is not None
        and not isinstance(crc32c, str)
    ):
        raise _AbortableGcsServiceFailure(502, {})
    return {
        'generation': generation,
        'size': size,
        'etag': etag,
        'crc32c': crc32c,
    }


def _download_blob_range(
    blob: Any,
    *,
    start: int,
    end: int,
    generation: int,
    sdk_timeout: int | float,
) -> bytes:
    """Fetch and validate one exact inclusive stored/raw byte range."""
    data = blob.download_as_bytes(
        start=start,
        end=end,
        if_generation_match=generation,
        raw_download=True,
        **_operational_sdk_kwargs(sdk_timeout),
    )
    if not isinstance(data, bytes) or len(data) != end - start + 1:
        raise _AbortableGcsServiceFailure(502, {})
    return data


def _optional_metadata_string(value: object) -> Optional[str]:
    """Normalize one optional meaningful provider string without coercion."""
    if value is None:
        return None
    if not isinstance(value, str) or value == '':
        raise _AbortableGcsServiceFailure(502, {})
    return value


def _optional_metadata_int(value: object) -> Optional[int]:
    """Normalize one optional non-negative provider integer."""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _AbortableGcsServiceFailure(502, {})
    return value


def _metadata_timestamp(value: object) -> Optional[str]:
    """Normalize one optional timezone-aware provider timestamp."""
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise _AbortableGcsServiceFailure(502, {})
    return value.isoformat()


def _reload_head_metadata(
    blob: Any,
    *,
    if_generation_match: Optional[int],
    sdk_timeout: int | float,
) -> dict[str, Any]:
    """Reload and normalize one exact object metadata snapshot."""
    arguments = _operational_sdk_kwargs(sdk_timeout)
    if if_generation_match is not None:
        arguments['if_generation_match'] = if_generation_match
    blob.reload(**arguments)

    raw_metadata = blob.metadata
    if raw_metadata is None:
        metadata: dict[str, str] = {}
    elif (
        isinstance(raw_metadata, Mapping)
        and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in raw_metadata.items()
        )
    ):
        metadata = dict(raw_metadata)
    else:
        raise _AbortableGcsServiceFailure(502, {})
    return {
        'content_length': _optional_metadata_int(blob.size),
        'content_type': _optional_metadata_string(blob.content_type),
        'etag': _optional_metadata_string(blob.etag),
        'generation': _optional_metadata_int(blob.generation),
        'metageneration': _optional_metadata_int(blob.metageneration),
        'last_modified': _metadata_timestamp(blob.updated),
        'crc32c': _optional_metadata_string(blob.crc32c),
        'metadata': metadata,
    }


def _normalize_list_item(blob: Any) -> dict[str, Any]:
    """Normalize one list item into the closed public item schema."""
    key = blob.name
    size = blob.size
    if not isinstance(key, str) or key == '':
        raise _AbortableGcsServiceFailure(502, {})
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise _AbortableGcsServiceFailure(502, {})
    return {
        'key': key,
        'size': size,
        'content_type': _optional_metadata_string(blob.content_type),
        'etag': _optional_metadata_string(blob.etag),
        'generation': _optional_metadata_int(blob.generation),
        'last_modified': _metadata_timestamp(blob.updated),
        'crc32c': _optional_metadata_string(blob.crc32c),
    }


def _list_one_page(
    client: Any,
    bucket: Any,
    *,
    prefix: str,
    max_items: int,
    page_token: Optional[str],
    sdk_timeout: int | float,
) -> dict[str, Any]:
    """Fetch and normalize exactly one bounded service page."""
    arguments: dict[str, object] = {
        'prefix': prefix,
        'max_results': max_items,
        **_operational_sdk_kwargs(sdk_timeout),
    }
    if page_token is not None:
        arguments['page_token'] = page_token
    iterator = client.list_blobs(bucket, **arguments)
    try:
        pages = iter(iterator.pages)
        page = next(pages)
        page_items = iter(page)
    except (AttributeError, TypeError):
        raise _AbortableGcsServiceFailure(502, {}) from None

    items: list[dict[str, Any]] = []
    while True:
        try:
            blob = next(page_items)
        except StopIteration:
            break
        if len(items) >= max_items:
            raise _AbortableGcsServiceFailure(502, {})
        items.append(_normalize_list_item(blob))

    try:
        next_page_token = iterator.next_page_token
    except AttributeError:
        raise _AbortableGcsServiceFailure(502, {}) from None
    if (
        next_page_token is not None
        and (
            not isinstance(next_page_token, str)
            or next_page_token == ''
        )
    ):
        raise _AbortableGcsServiceFailure(502, {})
    return {
        'items': items,
        'item_count': len(items),
        'is_truncated': next_page_token is not None,
        'next_page_token': next_page_token,
    }


def _safe_provider_text(value: object) -> Optional[str]:
    """Return redacted provider text without stringifying foreign objects."""
    if not isinstance(value, str):
        return None
    if _GCS_PROVIDER_SECRET_ASSIGNMENT.search(value) is not None:
        return None
    return redact_text(value)


def _service_failure_for(
    error: BaseException,
    *,
    command: str,
    bucket: str,
    target: str,
) -> AsyncGatewayError:
    """Structurally normalize and classify one Google service refusal."""
    response = getattr(error, 'response', None)
    response_mapping = response if isinstance(response, Mapping) else {}
    raw_status = response_mapping.get('status_code')
    if raw_status is None and response is not None:
        raw_status = getattr(response, 'status_code', None)
    if raw_status is None:
        raw_status = getattr(error, 'code', None)
    status = (
        raw_status
        if isinstance(raw_status, int)
        and not isinstance(raw_status, bool)
        and 100 <= raw_status <= 599
        else 502
    )
    headers = response_mapping.get('headers')
    if headers is None and response is not None:
        headers = getattr(response, 'headers', None)
    header_mapping = headers if isinstance(headers, Mapping) else {}
    details = {
        'command': command,
        'bucket': bucket,
        'target': target,
        'gcs_error_code': _safe_provider_text(getattr(error, 'code', None)),
        'gcs_error_message': _safe_provider_text(
            getattr(error, 'message', None)),
        'response_metadata': {
            'http_status_code': status,
            'request_id': _safe_provider_text(
                header_mapping.get('x-goog-request-id')),
        },
    }
    failure_type = (
        _GcsServiceFailure
        if status in _GCS_RETRYABLE_STATUSES
        else _AbortableGcsServiceFailure
    )
    return failure_type(status, details)


def _transport_error_for(error: BaseException) -> TransportError:
    """Map a transport exception by type without exposing foreign prose."""
    message = 'GCS provider transport failed'
    if isinstance(error, socket.gaierror):
        return DnsError(message)
    if isinstance(error, ssl.SSLError):
        return TlsError(message)
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return GatewayTimeoutError(message)
    if isinstance(error, ConnectionError):
        return ConnectError(message)
    return TransportError(message)


def _is_service_failure(error: BaseException) -> bool:
    """Recognize one provider service exception without parsing its prose."""
    return (
        isinstance(error, google_api_exceptions.GoogleAPICallError)
        or isinstance(getattr(error, 'response', None), Mapping)
    )


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
        self._download_pin: Optional[_DownloadPin] = None

    async def handle_request(self) -> GatewayResponse:
        """Dispatch the implemented bounded GCS operation.

        Returns:
            The finalized shared response after a bounded transfer.

        Raises:
            AsyncGatewayError: For a typed local or lifecycle failure.
            NotImplementedError: If the selected operation is unavailable.
        """
        if self.command not in {'download', 'upload', 'head', 'list'}:
            raise NotImplementedError

        lease = _acquire_gcs_lease()
        try:
            if self.command == 'download':
                return await self._handle_download(lease)
            if self.command in {'head', 'list'}:
                return await self._handle_head_or_list(lease)
            local_path = cast(str, self.info['local_path'])
            upload_body = await read_guarded_file(
                local_path,
                max_bytes=cast(int, self.info['max_upload_bytes']),
            )
            failure: Optional[AsyncGatewayError] = None
            try:
                metadata = await self.circuit_breaker.run(
                    self._upload_attempt,
                    lease,
                    upload_body,
                    cast(int, self.info['if_generation_match']),
                )
            except CircuitOpen:
                failure = CircuitOpenError('GCS provider circuit is open')
            except _AbortableGcsServiceFailure as error:
                failure = self._public_service_error(error)
            except RetriesExhausted as error:
                cause = error.__cause__
                if isinstance(cause, _GcsServiceFailure):
                    failure = self._public_service_error(cause)
                elif isinstance(cause, AsyncGatewayError):
                    failure = cause
                else:
                    failure = TransportError(
                        'GCS provider operation failed')
            if failure is not None:
                raise failure from None
            self.response['protocol_details'] = {
                'command': self.command,
                'bucket': self.bucket,
                'key': self.key,
                'local_path': local_path,
                'bytes_read': len(upload_body),
                **metadata,
            }
            return finalise_ok(
                self.response, status_code=200, started=self.start_time)
        finally:
            await lease.release()

    async def _handle_head_or_list(
        self,
        lease: _GcsLease,
    ) -> GatewayResponse:
        """Run one bounded metadata command under a retained lease."""
        failure: Optional[AsyncGatewayError] = None
        try:
            details = await self.circuit_breaker.run(
                self._head_or_list_attempt,
                lease,
            )
        except CircuitOpen:
            failure = CircuitOpenError('GCS provider circuit is open')
        except _AbortableGcsServiceFailure as error:
            failure = self._public_service_error(error)
        except RetriesExhausted as error:
            cause = error.__cause__
            if isinstance(cause, _GcsServiceFailure):
                failure = self._public_service_error(cause)
            elif isinstance(cause, AsyncGatewayError):
                failure = cause
            else:
                failure = TransportError('GCS provider operation failed')
        if failure is not None:
            raise failure from None
        self.response['protocol_details'] = details
        return finalise_ok(
            self.response, status_code=200, started=self.start_time)

    async def _head_or_list_attempt(
        self,
        lease: _GcsLease,
    ) -> dict[str, Any]:
        """Fetch one normalized head or list result and close its client."""
        client: Any = None
        details: Optional[dict[str, Any]] = None
        body_error: Optional[BaseException] = None
        body_state: Optional[_ExceptionState] = None
        try:
            try:
                credentials, project = await lease.run(
                    google_auth.default, timeout=self.timeout)
                await lease.run(
                    _refresh_credentials_if_needed,
                    credentials,
                    timeout=self.timeout,
                )
                client = await lease.run(
                    storage.Client,
                    credentials=credentials,
                    project=project,
                    timeout=self.timeout,
                    close_result=_close_storage_client,
                )
                bucket = await lease.run(
                    client.bucket, self.bucket, timeout=self.timeout)
                if self.command == 'head':
                    blob = await lease.run(
                        bucket.blob, self.key, timeout=self.timeout)
                    metadata = await lease.run(
                        _reload_head_metadata,
                        blob,
                        if_generation_match=cast(
                            Optional[int],
                            self.info.get('if_generation_match'),
                        ),
                        sdk_timeout=self.timeout,
                        timeout=self.timeout,
                    )
                    details = {
                        'command': self.command,
                        'bucket': self.bucket,
                        'key': self.key,
                        **metadata,
                    }
                else:
                    page = await lease.run(
                        _list_one_page,
                        client,
                        bucket,
                        prefix=self.key,
                        max_items=cast(int, self.info['max_items']),
                        page_token=cast(
                            Optional[str], self.info.get('page_token')),
                        sdk_timeout=self.timeout,
                        timeout=self.timeout,
                    )
                    details = {
                        'command': self.command,
                        'bucket': self.bucket,
                        'prefix': self.key,
                        **page,
                    }
            except _GCS_CREDENTIAL_FAILURES as error:
                error.args = ('GCS credential resolution failed',)
                raise ConfigurationError(
                    'GCS application default credentials are unavailable'
                ) from None
            except _GCS_TRANSPORT_FAILURES as error:
                error.args = ('GCS provider transport failure',)
                raise _transport_error_for(error) from None
            except BaseException as error:
                if not _is_service_failure(error):
                    raise
                failure = _service_failure_for(
                    error,
                    command=self.command,
                    bucket=self.bucket,
                    target=self.key,
                )
                error.args = ('GCS provider service response',)
                raise failure from None
        except BaseException as error:
            body_error = error
            body_state = _capture_exception_state(error)

        cleanup_error: Optional[BaseException] = None
        cleanup_state: Optional[_ExceptionState] = None
        if client is not None:
            try:
                await lease.run(
                    _close_storage_client, client, timeout=self.timeout)
            except BaseException as error:
                cleanup_error = error
                cleanup_state = _capture_exception_state(error)

        if body_error is not None:
            assert body_state is not None
            _raise_exact(body_error, body_state)
        if cleanup_error is not None:
            assert cleanup_state is not None
            if isinstance(cleanup_error, asyncio.CancelledError):
                _raise_exact(cleanup_error, cleanup_state)
            if isinstance(cleanup_error, _GCS_CREDENTIAL_FAILURES):
                cleanup_error.args = ('GCS credential cleanup failed',)
                raise ConfigurationError(
                    'GCS application default credentials are unavailable'
                ) from None
            if isinstance(cleanup_error, _GCS_TRANSPORT_FAILURES):
                cleanup_error.args = ('GCS provider cleanup failed',)
                raise _transport_error_for(cleanup_error) from None
            if _is_service_failure(cleanup_error):
                status_failure = _service_failure_for(
                    cleanup_error,
                    command=self.command,
                    bucket=self.bucket,
                    target=self.key,
                )
                cleanup_error.args = ('GCS provider cleanup response',)
                raise _AbortableGcsServiceFailure(
                    status_failure.status_code, {}) from None
            _raise_exact(cleanup_error, cleanup_state)
        assert details is not None
        return details

    async def _handle_download(self, lease: _GcsLease) -> GatewayResponse:
        """Run one generation-pinned download under a retained lease."""
        local_path = cast(str, self.info['local_path'])
        failure: Optional[AsyncGatewayError] = None
        try:
            metadata = await self.circuit_breaker.run(
                self._download_attempt,
                lease,
                local_path,
                cast(int, self.info['max_response_bytes']),
            )
        except CircuitOpen:
            failure = CircuitOpenError('GCS provider circuit is open')
        except _AbortableGcsServiceFailure as error:
            failure = self._public_service_error(error)
        except RetriesExhausted as error:
            cause = error.__cause__
            if isinstance(cause, _GcsServiceFailure):
                failure = self._public_service_error(cause)
            elif isinstance(cause, AsyncGatewayError):
                failure = cause
            else:
                failure = TransportError('GCS provider operation failed')
        if failure is not None:
            raise failure from None
        self.response['protocol_details'] = {
            'command': self.command,
            'bucket': self.bucket,
            'key': self.key,
            'local_path': local_path,
            **metadata,
        }
        return finalise_ok(
            self.response, status_code=200, started=self.start_time)

    def _public_service_error(
        self,
        error: _GcsServiceFailure | _AbortableGcsServiceFailure,
    ) -> GcsStatusError:
        """Populate exact safe details and build the public GCS failure."""
        self.response['protocol_details'] = dict(error.details)
        return GcsStatusError(
            'GCS provider service request failed', error.status_code)

    async def _download_chunks(
        self,
        lease: _GcsLease,
        blob: Any,
        pin: _DownloadPin,
    ) -> AsyncIterator[bytes]:
        """Yield sequential exact stored/raw ranges for one frozen pin."""
        offset = 0
        while offset < pin['size']:
            end = min(offset + _GCS_DOWNLOAD_CHUNK_SIZE, pin['size']) - 1
            try:
                chunk = await lease.run(
                    _download_blob_range,
                    blob,
                    start=offset,
                    end=end,
                    generation=pin['generation'],
                    sdk_timeout=self.timeout,
                    timeout=self.timeout,
                )
            except _GCS_TRANSPORT_FAILURES as error:
                error.args = ('GCS provider transport failure',)
                raise _transport_error_for(error) from None
            yield chunk
            offset = end + 1

    async def _download_attempt(
        self,
        lease: _GcsLease,
        local_path: str,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        """Pin and atomically stream one exact stored object generation."""
        client: Any = None
        result: Optional[dict[str, Any]] = None
        body_error: Optional[BaseException] = None
        body_state: Optional[_ExceptionState] = None
        try:
            try:
                credentials, project = await lease.run(
                    google_auth.default, timeout=self.timeout)
                await lease.run(
                    _refresh_credentials_if_needed,
                    credentials,
                    timeout=self.timeout,
                )
                client = await lease.run(
                    storage.Client,
                    credentials=credentials,
                    project=project,
                    timeout=self.timeout,
                    close_result=_close_storage_client,
                )
                bucket = await lease.run(
                    client.bucket, self.bucket, timeout=self.timeout)
                blob = await lease.run(
                    bucket.blob, self.key, timeout=self.timeout)
                if self._download_pin is None:
                    caller_generation = cast(
                        Optional[int], self.info.get('if_generation_match'))
                    self._download_pin = await lease.run(
                        _reload_download_pin,
                        blob,
                        if_generation_match=caller_generation,
                        sdk_timeout=self.timeout,
                        timeout=self.timeout,
                    )
                pin = self._download_pin
                assert pin is not None
                if pin['size'] > max_response_bytes:
                    raise ResponseTooLargeError(
                        f'response declares {pin["size"]} bytes, which is '
                        f'over max_response_bytes={max_response_bytes}; '
                        'the body was not read')
                bytes_written = await stream_to_path(
                    local_path,
                    self._download_chunks(lease, blob, pin),
                    overwrite=True,
                    max_bytes=max_response_bytes,
                    advertised_bytes=pin['size'],
                )
                if bytes_written != pin['size']:
                    raise _AbortableGcsServiceFailure(502, {})
                result = {
                    'bytes_written': bytes_written,
                    'etag': pin['etag'],
                    'generation': pin['generation'],
                    'crc32c': pin['crc32c'],
                }
            except _GCS_CREDENTIAL_FAILURES as error:
                error.args = ('GCS credential resolution failed',)
                raise ConfigurationError(
                    'GCS application default credentials are unavailable'
                ) from None
            except _GCS_TRANSPORT_FAILURES as error:
                error.args = ('GCS provider transport failure',)
                raise _transport_error_for(error) from None
            except BaseException as error:
                if not _is_service_failure(error):
                    raise
                failure = _service_failure_for(
                    error,
                    command=self.command,
                    bucket=self.bucket,
                    target=self.key,
                )
                error.args = ('GCS provider service response',)
                raise failure from None
        except BaseException as error:
            body_error = error
            body_state = _capture_exception_state(error)

        cleanup_error: Optional[BaseException] = None
        cleanup_state: Optional[_ExceptionState] = None
        if client is not None:
            try:
                await lease.run(
                    _close_storage_client, client, timeout=self.timeout)
            except BaseException as error:
                cleanup_error = error
                cleanup_state = _capture_exception_state(error)

        pending_cancellation = (
            body_error
            if isinstance(body_error, asyncio.CancelledError)
            else cleanup_error
            if isinstance(cleanup_error, asyncio.CancelledError)
            else None
        )
        if pending_cancellation is not None:
            state = (
                body_state
                if pending_cancellation is body_error
                else cleanup_state
            )
            assert state is not None
            _raise_exact(pending_cancellation, state)
        if body_error is not None:
            assert body_state is not None
            _raise_exact(body_error, body_state)
        if cleanup_error is not None:
            assert cleanup_state is not None
            if isinstance(cleanup_error, _GCS_CREDENTIAL_FAILURES):
                cleanup_error.args = ('GCS credential cleanup failed',)
                raise ConfigurationError(
                    'GCS application default credentials are unavailable'
                ) from None
            if isinstance(cleanup_error, _GCS_TRANSPORT_FAILURES):
                cleanup_error.args = ('GCS provider cleanup failed',)
                raise _transport_error_for(cleanup_error) from None
            if _is_service_failure(cleanup_error):
                status_failure = _service_failure_for(
                    cleanup_error,
                    command=self.command,
                    bucket=self.bucket,
                    target=self.key,
                )
                cleanup_error.args = ('GCS provider cleanup response',)
                raise _AbortableGcsServiceFailure(
                    status_failure.status_code, {}) from None
            _raise_exact(cleanup_error, cleanup_state)
        assert result is not None
        return result

    async def _upload_attempt(
        self,
        lease: _GcsLease,
        upload_body: bytes,
        if_generation_match: int,
    ) -> dict[str, Any]:
        """Run one replay-safe upload attempt on the private provider pool.

        Args:
            lease: Retained request lifecycle lease.
            upload_body: Exact bytes returned by the guarded local read.
            if_generation_match: Frozen optimistic-write precondition.

        Returns:
            The upload blob's success metadata snapshot.
        """
        client: Any = None
        metadata: Optional[dict[str, Any]] = None
        body_error: Optional[BaseException] = None
        body_state: Optional[_ExceptionState] = None
        try:
            try:
                credentials, project = await lease.run(
                    google_auth.default, timeout=self.timeout)
                await lease.run(
                    _refresh_credentials_if_needed,
                    credentials,
                    timeout=self.timeout,
                )
                client = await lease.run(
                    storage.Client,
                    credentials=credentials,
                    project=project,
                    timeout=self.timeout,
                    close_result=_close_storage_client,
                )
                bucket = await lease.run(
                    client.bucket, self.bucket, timeout=self.timeout)
                blob = await lease.run(
                    bucket.blob, self.key, timeout=self.timeout)
                metadata = await lease.run(
                    _upload_blob,
                    blob,
                    upload_body,
                    if_generation_match=if_generation_match,
                    sdk_timeout=self.timeout,
                    timeout=self.timeout,
                )
            except _GCS_CREDENTIAL_FAILURES as error:
                error.args = ('GCS credential resolution failed',)
                raise ConfigurationError(
                    'GCS application default credentials are unavailable'
                ) from None
            except _GCS_TRANSPORT_FAILURES as error:
                error.args = ('GCS provider transport failure',)
                raise _transport_error_for(error) from None
            except BaseException as error:
                if not _is_service_failure(error):
                    raise
                failure = _service_failure_for(
                    error,
                    command=self.command,
                    bucket=self.bucket,
                    target=self.key,
                )
                error.args = ('GCS provider service response',)
                raise failure from None
        except BaseException as error:
            body_error = error
            body_state = _capture_exception_state(error)

        cleanup_error: Optional[BaseException] = None
        cleanup_state: Optional[_ExceptionState] = None
        if client is not None:
            try:
                await lease.run(
                    _close_storage_client, client, timeout=self.timeout)
            except BaseException as error:
                cleanup_error = error
                cleanup_state = _capture_exception_state(error)

        pending_cancellation = (
            body_error
            if isinstance(body_error, asyncio.CancelledError)
            else cleanup_error
            if isinstance(cleanup_error, asyncio.CancelledError)
            else None
        )
        if pending_cancellation is not None:
            state = (
                body_state
                if pending_cancellation is body_error
                else cleanup_state
            )
            assert state is not None
            _raise_exact(pending_cancellation, state)
        if body_error is not None:
            assert body_state is not None
            _raise_exact(body_error, body_state)
        if cleanup_error is not None:
            assert cleanup_state is not None
            if isinstance(cleanup_error, _GCS_CREDENTIAL_FAILURES):
                cleanup_error.args = ('GCS credential cleanup failed',)
                raise ConfigurationError(
                    'GCS application default credentials are unavailable'
                ) from None
            if isinstance(cleanup_error, _GCS_TRANSPORT_FAILURES):
                cleanup_error.args = ('GCS provider cleanup failed',)
                raise _transport_error_for(cleanup_error) from None
            if _is_service_failure(cleanup_error):
                status_failure = _service_failure_for(
                    cleanup_error,
                    command=self.command,
                    bucket=self.bucket,
                    target=self.key,
                )
                cleanup_error.args = ('GCS provider cleanup response',)
                raise _AbortableGcsServiceFailure(
                    status_failure.status_code, {}) from None
            _raise_exact(cleanup_error, cleanup_state)
        assert metadata is not None
        return metadata

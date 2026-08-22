"""Contract tests for the first-class bounded S3 strategy.

Every SDK interaction in this module goes through a deterministic handwritten
double. The focused lane temporarily installs :class:`S3Request` in the
mutable protocol registry once the implementation exists; PE-80 owns the
production registry assertion.
"""

import asyncio
import importlib.util
import logging
import socket
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping, Optional, Union

import aioboto3

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    SSLError,
)

from failsafe import RetriesExhausted

import pytest

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.helpers.internal.breaker_registry import get_breaker
from asyncio_gateway.helpers.internal.circuit_breaker_helper import (
    CircuitBreakerHelper,
)
from asyncio_gateway.logic import protocol_mapping
from asyncio_gateway.utils.constants import UNKNOWN_PORT
from asyncio_gateway.utils.envelope import GatewayResponse
from asyncio_gateway.utils.exceptions import ConfigurationError

from tests.cancellation import (
    assert_cancelled_error_survives_task_boundary,
)


Outcome = Union[
    Mapping[str, Any],
    BaseException,
    Callable[..., Awaitable[Mapping[str, Any]]],
]


@pytest.fixture(autouse=True)
def focused_s3_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install the lane class only after its module exists.

    Before GREEN this deliberately does nothing, leaving the original public
    selector failure as the RED checkpoint. Production registration belongs
    to PE-80 rather than this lane.

    Args:
        monkeypatch: Pytest's scoped mutation helper.
    """
    module_name = 'asyncio_gateway.logic.s3_client'
    if importlib.util.find_spec(module_name) is None:
        return
    from asyncio_gateway.logic.s3_client import S3Request

    monkeypatch.setitem(protocol_mapping, 'S3', S3Request)


class ChunkedBody:
    """Observable async S3 streaming-body double."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Store chunks and start open.

        Args:
            chunks: Byte chunks yielded to the shared download primitive.
        """
        self.chunks = chunks
        self.requested_sizes: list[int] = []
        self.closed = False

    async def iter_chunks(self, *, chunk_size: int) -> Any:
        """Yield each configured chunk in order.

        Args:
            chunk_size: The strategy-selected read size.

        Yields:
            Configured byte chunks.
        """
        self.requested_sizes.append(chunk_size)
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk

    async def close(self) -> None:
        """Record deterministic cleanup."""
        self.closed = True


class RecordingS3Client:
    """Four-operation S3 double with scripted outcomes."""

    def __init__(self) -> None:
        """Start with empty scripts and call records."""
        self.outcomes: dict[str, list[Outcome]] = {
            'download': [],
            'upload': [],
            'head': [],
            'list': [],
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def script(self, command: str, *outcomes: Outcome) -> None:
        """Replace one operation's ordered outcomes.

        Args:
            command: Gateway command name.
            *outcomes: Responses, failures, or async callables.
        """
        self.outcomes[command] = list(outcomes)

    async def _dispatch(
        self,
        command: str,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]:
        """Record one SDK call and return its next scripted outcome.

        Args:
            command: Gateway command name.
            arguments: SDK keyword arguments.

        Returns:
            The scripted SDK response.

        Raises:
            BaseException: The scripted failure.
        """
        self.calls.append((command, dict(arguments)))
        outcomes = self.outcomes[command]
        if not outcomes:
            raise AssertionError(f'no scripted {command} outcome remains')
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome(**arguments)
        return outcome

    async def get_object(self, **kwargs: Any) -> Mapping[str, Any]:
        """Dispatch the gateway download operation."""
        return await self._dispatch('download', kwargs)

    async def put_object(self, **kwargs: Any) -> Mapping[str, Any]:
        """Dispatch the gateway upload operation."""
        return await self._dispatch('upload', kwargs)

    async def head_object(self, **kwargs: Any) -> Mapping[str, Any]:
        """Dispatch the gateway head operation."""
        return await self._dispatch('head', kwargs)

    async def list_objects_v2(self, **kwargs: Any) -> Mapping[str, Any]:
        """Dispatch the gateway one-page list operation."""
        return await self._dispatch('list', kwargs)


class ClientContext:
    """Observable async context manager for one S3 client."""

    def __init__(self, sdk: 'SdkDouble') -> None:
        """Retain the owning double.

        Args:
            sdk: Owning SDK double.
        """
        self.sdk = sdk

    async def __aenter__(self) -> RecordingS3Client:
        """Record entry and yield the client."""
        self.sdk.enters += 1
        if self.sdk.enter_error is not None:
            raise self.sdk.enter_error
        return self.sdk.client

    async def __aexit__(self, *exc_info: Any) -> bool:
        """Record exit without suppressing failures.

        Args:
            *exc_info: In-flight exception information.

        Returns:
            False.
        """
        self.sdk.exits += 1
        self.sdk.exit_infos.append((exc_info[0], exc_info[1], exc_info[2]))
        self.sdk.exit_exceptions.append(exc_info[1])
        current = asyncio.current_task()
        if current is not None:
            self.sdk.exit_tasks.append(current)
        if self.sdk.exit_started is not None:
            self.sdk.exit_started.set()
        if self.sdk.exit_release is not None:
            await self.sdk.exit_release.wait()
        if self.sdk.exit_inner_error is not None:
            try:
                raise self.sdk.exit_inner_error
            except BaseException:
                if self.sdk.exit_error is not None:
                    raise self.sdk.exit_error
                raise
        if self.sdk.exit_error is not None:
            raise self.sdk.exit_error
        return self.sdk.exit_result


class SessionDouble:
    """One aioboto3 Session double."""

    def __init__(self, sdk: 'SdkDouble') -> None:
        """Retain the owning SDK recorder.

        Args:
            sdk: Owning recorder.
        """
        self.sdk = sdk

    def client(self, service_name: str, **kwargs: Any) -> ClientContext:
        """Record client construction and return its context.

        Args:
            service_name: Requested SDK service.
            **kwargs: Client configuration.

        Returns:
            The deterministic client context.
        """
        self.sdk.client_arguments.append((service_name, dict(kwargs)))
        return ClientContext(self.sdk)


class SdkDouble:
    """Session factory and S3 operation recorder."""

    def __init__(self) -> None:
        """Create one reusable client and empty lifecycle records."""
        self.client = RecordingS3Client()
        self.session_arguments: list[dict[str, Any]] = []
        self.client_arguments: list[tuple[str, dict[str, Any]]] = []
        self.enters = 0
        self.exits = 0
        self.enter_error: Optional[BaseException] = None
        self.exit_infos: list[tuple[Any, Any, Any]] = []
        self.exit_exceptions: list[Optional[BaseException]] = []
        self.exit_error: Optional[BaseException] = None
        self.exit_inner_error: Optional[BaseException] = None
        self.exit_result = False
        self.exit_started: Optional[asyncio.Event] = None
        self.exit_release: Optional[asyncio.Event] = None
        self.exit_tasks: list[asyncio.Task[Any]] = []

    def session(self, **kwargs: Any) -> SessionDouble:
        """Record Session construction.

        Args:
            **kwargs: aioboto3 Session options.

        Returns:
            A deterministic session.
        """
        self.session_arguments.append(dict(kwargs))
        return SessionDouble(self)


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> SdkDouble:
    """Patch aioboto3 with a deterministic SDK boundary.

    Args:
        monkeypatch: Pytest's scoped mutation helper.

    Returns:
        The SDK recorder.
    """
    double = SdkDouble()
    monkeypatch.setattr(aioboto3, 'Session', double.session)
    return double


def service_error(
    *,
    status: int,
    code: str,
    message: str = 'remote refusal',
    request_id: str = 'request-1',
) -> ClientError:
    """Build a realistic botocore service error.

    Args:
        status: AWS HTTP status.
        code: AWS error code.
        message: AWS error message.
        request_id: AWS request id.

    Returns:
        A deterministic ``ClientError``.
    """
    return ClientError(
        {
            'Error': {'Code': code, 'Message': message},
            'ResponseMetadata': {
                'HTTPStatusCode': status,
                'RequestId': request_id,
                'HostId': 'host-1',
                'RetryAttempts': 0,
                'HTTPHeaders': {'authorization': 'must-not-escape'},
            },
        },
        'HeadObject',
    )


def retrying_config(retries: int = 1) -> dict[str, Any]:
    """Return a breaker config with an exact retry budget.

    Args:
        retries: Attempts after the first.

    Returns:
        Public breaker configuration.
    """
    return {
        'maximum_failures': 99,
        'retry_config': {
            'allowed_retries': retries,
            'delay': 0,
            'jitter': False,
        },
    }


async def test_s3_head_is_reachable_through_the_public_entrypoint(
    sdk: SdkDouble,
) -> None:
    """The S3 selector dispatches one normalized head operation."""
    sdk.client.script(
        'head',
        {
            'ContentLength': 3,
            'ContentType': 'text/plain',
            'ETag': 'etag-1',
            'LastModified': '2026-08-20T00:00:00+00:00',
            'Metadata': {'source': 'test'},
            'ResponseMetadata': {'HTTPStatusCode': 204},
        },
    )

    result = await request(
        's3://contract-bucket/path/to/object',
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['ok'] is True
    assert result['status_code'] == 204
    assert result['protocol_details'] == {
        'command': 'head',
        'bucket': 'contract-bucket',
        'key': 'path/to/object',
        'content_length': 3,
        'content_type': 'text/plain',
        'etag': 'etag-1',
        'last_modified': '2026-08-20T00:00:00+00:00',
        'metadata': {'source': 'test'},
    }
    assert sdk.client.calls == [
        ('head', {'Bucket': 'contract-bucket', 'Key': 'path/to/object'}),
    ]
    assert sdk.session_arguments == [{}]
    assert sdk.enters == sdk.exits == 1


async def test_client_disables_sdk_retries(
    sdk: SdkDouble,
) -> None:
    """Botocore receives ``total_max_attempts=1`` without an endpoint."""
    sdk.client.script('head', {})

    result = await request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['ok'] is True
    service_name, arguments = sdk.client_arguments[0]
    assert service_name == 's3'
    assert set(arguments) == {'config'}
    assert arguments['config'].retries['total_max_attempts'] == 1


async def test_download_uses_shared_stream_and_exact_success_schema(
    sdk: SdkDouble,
    tmp_path: Path,
) -> None:
    """A capped download closes its body and reports committed bytes."""
    body = ChunkedBody([b'ab', b'c'])
    sdk.client.script(
        'download',
        {
            'Body': body,
            'ContentLength': 3,
            'ETag': 'download-etag',
            'ResponseMetadata': {'HTTPStatusCode': 206},
        },
    )
    target = tmp_path / 'download.bin'

    result = await request(
        's3://bucket/folder/object',
        protocol='S3',
        protocol_info={
            'command': 'download',
            'local_path': str(target),
            'max_response_bytes': 3,
        },
    )

    assert target.read_bytes() == b'abc'
    assert body.closed is True
    assert body.requested_sizes
    assert result['status_code'] == 206
    assert result['protocol_details'] == {
        'command': 'download',
        'bucket': 'bucket',
        'key': 'folder/object',
        'local_path': str(target),
        'bytes_written': 3,
        'etag': 'download-etag',
    }


async def test_upload_reads_once_before_breaker_and_reuses_bytes(
    sdk: SdkDouble,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A gateway retry never reopens or rereads the upload source."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'bounded upload')
    from asyncio_gateway.logic import s3_client

    original = s3_client.read_guarded_file
    reads: list[tuple[object, int]] = []

    async def recording_read(
        path: object,
        *,
        max_bytes: int,
        chunk_size: int = 65536,
    ) -> bytes:
        """Record and delegate one guarded read."""
        reads.append((path, max_bytes))
        return await original(
            path, max_bytes=max_bytes, chunk_size=chunk_size)

    monkeypatch.setattr(s3_client, 'read_guarded_file', recording_read)
    sdk.client.script(
        'upload',
        service_error(status=503, code='Unavailable'),
        {
            'ETag': 'upload-etag',
            'ResponseMetadata': {'HTTPStatusCode': 201},
        },
    )

    result = await request(
        's3://bucket/key',
        auth=SimpleNamespace(login='ACCESS', password='SECRET'),
        protocol='S3',
        protocol_info={
            'command': 'upload',
            'local_path': str(source),
            'max_upload_bytes': len(b'bounded upload'),
            'region': 'ap-south-1',
            'circuit_breaker_config': retrying_config(),
        },
    )

    assert reads == [(str(source), len(b'bounded upload'))]
    bodies = [arguments['Body'] for _, arguments in sdk.client.calls]
    assert bodies == [b'bounded upload', b'bounded upload']
    assert bodies[0] is bodies[1]
    assert sdk.session_arguments == [{
        'aws_access_key_id': 'ACCESS',
        'aws_secret_access_key': 'SECRET',
        'region_name': 'ap-south-1',
    }]
    assert result['protocol_details'] == {
        'command': 'upload',
        'bucket': 'bucket',
        'key': 'key',
        'local_path': str(source),
        'bytes_read': len(b'bounded upload'),
        'etag': 'upload-etag',
    }


async def test_head_normalizes_optional_scalars_and_datetime(
    sdk: SdkDouble,
) -> None:
    """Head never leaks a datetime or a missing SDK scalar."""
    instant = datetime(2026, 8, 20, tzinfo=timezone.utc)
    sdk.client.script(
        'head',
        {
            'LastModified': instant,
            'ResponseMetadata': {'HTTPStatusCode': 200},
        },
    )

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['protocol_details'] == {
        'command': 'head',
        'bucket': 'bucket',
        'key': 'key',
        'content_length': None,
        'content_type': None,
        'etag': None,
        'last_modified': instant.isoformat(),
        'metadata': {},
    }


async def test_list_returns_one_normalized_page_in_service_order(
    sdk: SdkDouble,
) -> None:
    """List is bounded to one caller-controlled page and token."""
    sdk.client.script(
        'list',
        {
            'Contents': [
                {
                    'Key': 'prefix/b',
                    'Size': 2,
                    'ETag': 'b-tag',
                    'LastModified': datetime(
                        2026, 8, 20, tzinfo=timezone.utc),
                    'StorageClass': 'STANDARD',
                },
                {'Key': 'prefix/a', 'Size': 0},
            ],
            'IsTruncated': True,
            'NextContinuationToken': 'server-next-token',
            'ResponseMetadata': {'HTTPStatusCode': 200},
        },
    )

    result = await request(
        's3://bucket/prefix/',
        protocol='S3',
        protocol_info={
            'command': 'list',
            'max_items': 2,
            'continuation_token': 'caller-token',
        },
    )

    assert sdk.client.calls == [(
        'list',
        {
            'Bucket': 'bucket',
            'Prefix': 'prefix/',
            'MaxKeys': 2,
            'ContinuationToken': 'caller-token',
        },
    )]
    assert result['protocol_details'] == {
        'command': 'list',
        'bucket': 'bucket',
        'prefix': 'prefix/',
        'items': [
            {
                'key': 'prefix/b',
                'size': 2,
                'etag': 'b-tag',
                'last_modified': '2026-08-20T00:00:00+00:00',
                'storage_class': 'STANDARD',
            },
            {
                'key': 'prefix/a',
                'size': 0,
                'etag': None,
                'last_modified': None,
                'storage_class': None,
            },
        ],
        'key_count': 2,
        'is_truncated': True,
        'next_continuation_token': 'server-next-token',
    }


async def test_list_defaults_to_one_thousand_without_following_pages(
    sdk: SdkDouble,
) -> None:
    """No implicit paginator or second page is reachable."""
    sdk.client.script('list', {'Contents': [], 'IsTruncated': False})

    result = await request(
        's3://bucket', protocol='S3',
        protocol_info={'command': 'list'},
    )

    assert result['status_code'] == 200
    assert sdk.client.calls == [(
        'list', {'Bucket': 'bucket', 'Prefix': '', 'MaxKeys': 1000})]
    assert result['protocol_details']['next_continuation_token'] is None


@pytest.mark.parametrize(
    'url',
    [
        's3:///key',
        's3://user:password@bucket/key',
        's3://bucket:443/key',
        's3://bucket:not-a-port/key',
        's3://bucket/key?version=1',
        's3://bucket/key#fragment',
        'https://bucket/key',
        'bucket/key',
    ],
)
async def test_invalid_s3_targets_fail_before_session(
    sdk: SdkDouble,
    url: str,
) -> None:
    """Ambiguous/non-S3 authorities never reach credential discovery."""
    with pytest.raises(ConfigurationError):
        await request(
            url, protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert sdk.session_arguments == []


def test_s3_parser_itself_rejects_a_wrong_scheme() -> None:
    """The strategy parser remains fail-closed when called directly."""
    from asyncio_gateway.logic.s3_client import _s3_target

    with pytest.raises(ConfigurationError):
        _s3_target('https://bucket/key')


@pytest.mark.parametrize(
    'auth',
    [
        object(),
        SimpleNamespace(login='', password='secret'),
        SimpleNamespace(login='access', password=''),
        SimpleNamespace(login=1, password='secret'),
        SimpleNamespace(login='access', password=None),
    ],
)
async def test_invalid_credentials_fail_before_session(
    sdk: SdkDouble,
    auth: object,
) -> None:
    """Explicit auth is a complete non-empty access/secret pair."""
    with pytest.raises(ConfigurationError):
        await request(
            's3://bucket/key', auth=auth, protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert sdk.session_arguments == []


@pytest.mark.parametrize('region', ['', '  ', 7, False])
async def test_invalid_region_fails_before_session(
    sdk: SdkDouble,
    region: object,
) -> None:
    """Region is optional but non-empty text when supplied."""
    with pytest.raises(ConfigurationError):
        await request(
            's3://bucket/key', protocol='S3',
            protocol_info={'command': 'head', 'region': region},
        )

    assert sdk.session_arguments == []


@pytest.mark.parametrize(
    'url, info',
    [
        ('s3://bucket/key', {'command': 'delete'}),
        ('s3://bucket/key', {'command': None}),
        ('s3://bucket', {'command': 'head'}),
        ('s3://bucket', {'command': 'download', 'local_path': '/tmp/x'}),
        ('s3://bucket', {'command': 'upload', 'local_path': '/tmp/x'}),
        ('s3://bucket/key', {'command': 'download'}),
        ('s3://bucket/key', {'command': 'upload', 'local_path': ''}),
        ('s3://bucket/key', {
            'command': 'download', 'local_path': '/tmp/x',
            'max_response_bytes': 0,
        }),
        ('s3://bucket/key', {
            'command': 'upload', 'local_path': '/tmp/x',
            'max_upload_bytes': True,
        }),
        ('s3://bucket', {'command': 'list', 'max_items': 0}),
        ('s3://bucket', {'command': 'list', 'max_items': 1001}),
        ('s3://bucket', {
            'command': 'list', 'continuation_token': '',
        }),
        ('s3://bucket/key', {
            'command': 'head', 'max_items': 2,
        }),
        ('s3://bucket/key', {
            'command': 'upload', 'local_path': '/tmp/x',
            'max_response_bytes': 2,
        }),
        ('s3://bucket/key', {
            'command': 'download', 'local_path': '/tmp/x',
            'max_upload_bytes': 2,
        }),
    ],
)
async def test_operation_contract_failures_precede_session(
    sdk: SdkDouble,
    url: str,
    info: dict[str, Any],
) -> None:
    """Keys, paths, caps, and command-specific options fail closed."""
    with pytest.raises(ConfigurationError):
        await request(url, protocol='S3', protocol_info=info)

    assert sdk.session_arguments == []


@pytest.mark.parametrize('maximum', [1, 1000])
async def test_list_item_boundaries_are_accepted(
    sdk: SdkDouble,
    maximum: int,
) -> None:
    """The inclusive one-page bounds dispatch unchanged."""
    sdk.client.script('list', {})

    result = await request(
        's3://bucket', protocol='S3',
        protocol_info={'command': 'list', 'max_items': maximum},
    )

    assert result['ok'] is True
    assert sdk.client.calls[0][1]['MaxKeys'] == maximum


async def test_missing_upload_source_fails_before_session_and_breaker(
    sdk: SdkDouble,
    tmp_path: Path,
) -> None:
    """Local read failures neither discover credentials nor count."""
    missing = tmp_path / 'missing.bin'

    with pytest.raises(FileNotFoundError):
        await request(
            's3://bucket/key', protocol='S3',
            protocol_info={
                'command': 'upload', 'local_path': str(missing),
            },
        )

    assert sdk.session_arguments == []
    assert get_breaker('s3', 'bucket', UNKNOWN_PORT).failures == 0


async def test_access_denied_is_abortable_and_preserves_safe_metadata(
    sdk: SdkDouble,
) -> None:
    """A permanent service response is public S3_STATUS and uncounted."""
    sdk.client.script(
        'head', service_error(
            status=403, code='AccessDenied', message='not allowed'),
    )

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={
            'command': 'head',
            'circuit_breaker_config': retrying_config(3),
        },
    )

    assert result['ok'] is False
    assert result['status_code'] == 403
    assert result['error']['code'] == 'S3_STATUS'
    assert len(sdk.client.calls) == 1
    assert get_breaker('s3', 'bucket', UNKNOWN_PORT).failures == 0
    assert result['protocol_details']['aws_error_code'] == 'AccessDenied'
    assert result['protocol_details']['aws_error_message'] == 'not allowed'
    assert result['protocol_details']['request_id'] == 'request-1'
    assert result['protocol_details']['response_metadata'] == {
        'http_status_code': 403,
        'request_id': 'request-1',
        'host_id': 'host-1',
        'retry_attempts': 0,
    }
    assert 'HTTPHeaders' not in repr(result)
    assert 'must-not-escape' not in repr(result)


@pytest.mark.parametrize(
    'failure',
    [
        service_error(status=503, code='UnlistedTransient'),
        service_error(status=400, code='SlowDown'),
    ],
)
async def test_allowlisted_service_failures_use_gateway_retry_budget(
    sdk: SdkDouble,
    failure: ClientError,
) -> None:
    """Retry status/code classification is exact and succeeds on retry."""
    sdk.client.script(
        'head', failure,
        {'ResponseMetadata': {'HTTPStatusCode': 200}},
    )

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={
            'command': 'head',
            'circuit_breaker_config': retrying_config(),
        },
    )

    assert result['ok'] is True
    assert len(sdk.client.calls) == 2


async def test_retry_exhaustion_is_exact_n_plus_one_and_public_s3_status(
    sdk: SdkDouble,
) -> None:
    """Only the gateway replays and the final AWS status survives."""
    sdk.client.script(
        'head',
        service_error(status=503, code='SlowDown'),
        service_error(
            status=504, code='RequestTimeout', request_id='request-2'),
        service_error(
            status=429, code='Throttling', request_id='request-3'),
    )

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={
            'command': 'head',
            'circuit_breaker_config': retrying_config(2),
        },
    )

    assert len(sdk.client.calls) == 3
    assert result['error']['code'] == 'S3_STATUS'
    assert result['status_code'] == 429
    assert result['protocol_details']['request_id'] == 'request-3'
    assert get_breaker('s3', 'bucket', UNKNOWN_PORT).failures == 3


@pytest.mark.parametrize(
    'status, code',
    [
        (400, 'AccessDenied'),
        (404, 'NoSuchBucket'),
        (404, 'NoSuchKey'),
        (301, 'PermanentRedirect'),
        (409, 'Conflict'),
    ],
)
async def test_nonallowlisted_service_failures_abort_uncounted(
    sdk: SdkDouble,
    status: int,
    code: str,
) -> None:
    """Permanent AWS responses never consume the explicit retry budget."""
    sdk.client.script('head', service_error(status=status, code=code))

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={
            'command': 'head',
            'circuit_breaker_config': retrying_config(3),
        },
    )

    assert result['error']['code'] == 'S3_STATUS'
    assert result['status_code'] == status
    assert len(sdk.client.calls) == 1
    assert get_breaker('s3', 'bucket', UNKNOWN_PORT).failures == 0


@pytest.mark.parametrize(
    'failure, expected_code',
    [
        (ConnectTimeoutError(endpoint_url='https://s3.invalid'), 'TIMEOUT'),
        (SSLError(endpoint_url='https://s3.invalid', error='tls'), 'TLS'),
        (EndpointConnectionError(
            endpoint_url='https://s3.invalid'), 'CONNECT'),
        (socket.gaierror('dns failed'), 'DNS'),
        (BotoCoreError(), 'TRANSPORT'),
    ],
)
async def test_transport_failures_map_safely_and_count(
    sdk: SdkDouble,
    failure: BaseException,
    expected_code: str,
) -> None:
    """SDK transport types map without exposing foreign exception text."""
    sdk.client.script('head', failure)

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['error']['code'] == expected_code
    assert 's3.invalid' not in repr(result)
    assert 'dns failed' not in repr(result)
    assert get_breaker('s3', 'bucket', UNKNOWN_PORT).failures == 1


async def test_session_credential_failure_is_safe_and_uncounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential failure during Session construction stays local."""
    def failing_session(**kwargs: Any) -> None:
        """Raise before any client can exist."""
        raise PartialCredentialsError(
            provider='provider-never-log', cred_var='secret-never-log')

    monkeypatch.setattr(aioboto3, 'Session', failing_session)

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['error']['code'] == 'CONFIG'
    assert 'provider-never-log' not in repr(result)
    assert 'secret-never-log' not in repr(result)
    assert get_breaker('s3', 'bucket', UNKNOWN_PORT).failures == 0


async def test_session_transport_failure_maps_without_sdk_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-client SDK transport failure uses the safe vocabulary."""
    def failing_session(**kwargs: Any) -> None:
        """Raise a generic SDK transport failure."""
        raise BotoCoreError()

    monkeypatch.setattr(aioboto3, 'Session', failing_session)

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['error']['code'] == 'TRANSPORT'


@pytest.mark.parametrize(
    'failure',
    [
        NoCredentialsError(),
        PartialCredentialsError(
            provider='test-provider', cred_var='aws_secret_access_key'),
    ],
)
async def test_sdk_credential_failures_are_configuration_and_uncounted(
    sdk: SdkDouble,
    failure: BaseException,
) -> None:
    """Credential-chain failures abort without provider-detail leakage."""
    sdk.client.script('head', failure)

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={
            'command': 'head',
            'circuit_breaker_config': retrying_config(),
        },
    )

    assert result['error']['code'] == 'CONFIG'
    assert 'test-provider' not in repr(result)
    assert 'aws_secret_access_key' not in repr(result)
    assert len(sdk.client.calls) == 1
    assert get_breaker('s3', 'bucket', UNKNOWN_PORT).failures == 0


async def test_credentials_and_caller_token_are_redacted_everywhere(
    sdk: SdkDouble,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Known credential/token values never reach envelopes or logs."""
    access = 'ACCESS-VALUE-NEVER-LOG'
    secret = 'SECRET-VALUE-NEVER-LOG'
    token = 'CALLER-TOKEN-NEVER-LOG'
    sdk.client.script(
        'list',
        service_error(
            status=403,
            code='AccessDenied',
            message=f'echo {access} {secret} {token}',
        ),
    )
    caplog.set_level(logging.WARNING)

    result = await request(
        's3://bucket/prefix',
        auth=SimpleNamespace(login=access, password=secret),
        protocol='S3',
        protocol_info={
            'command': 'list',
            'continuation_token': token,
        },
    )

    surfaces = repr(result) + ''.join(
        str(record.__dict__) for record in caplog.records)
    assert access not in surfaces
    assert secret not in surfaces
    assert token not in surfaces
    assert '***redacted***' in surfaces


async def test_malformed_service_metadata_normalizes_to_safe_nulls(
    sdk: SdkDouble,
) -> None:
    """Foreign objects in service-error fields never enter the envelope."""
    sdk.client.script(
        'head',
        ClientError(
            {
                'Error': {'Code': 7, 'Message': object()},
                'ResponseMetadata': {
                    'HTTPStatusCode': 403,
                    'RequestId': object(),
                    'HostId': 8,
                    'RetryAttempts': True,
                },
            },
            'HeadObject',
        ),
    )

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['error']['code'] == 'S3_STATUS'
    assert result['protocol_details']['aws_error_code'] is None
    assert result['protocol_details']['aws_error_message'] is None
    assert result['protocol_details']['request_id'] is None
    assert result['protocol_details']['response_metadata'] == {
        'http_status_code': 403,
        'request_id': None,
        'host_id': None,
        'retry_attempts': None,
    }


@pytest.mark.parametrize(
    'response',
    [
        {'ResponseMetadata': {'HTTPStatusCode': True}},
        {'ResponseMetadata': {'HTTPStatusCode': 301}},
        {'ResponseMetadata': 'not-a-mapping'},
        {'ContentLength': -1},
        {'ContentType': 7},
        {'ETag': object()},
        {'LastModified': object()},
        {'LastModified': 'not-a-timestamp'},
        {'Metadata': {'ok': 7}},
    ],
)
async def test_malformed_head_success_becomes_s3_status_502(
    sdk: SdkDouble,
    response: Mapping[str, Any],
) -> None:
    """Malformed SDK success values never leak Python objects."""
    sdk.client.script('head', response)

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['ok'] is False
    assert result['error']['code'] == 'S3_STATUS'
    assert result['status_code'] == 502
    assert 'object at 0x' not in repr(result)


async def test_nonmapping_sdk_success_becomes_s3_status_502(
    sdk: SdkDouble,
) -> None:
    """The adapter never assumes the SDK returned a mapping."""
    # `type: ignore[arg-type]` -- inject a malformed non-mapping SDK result.
    sdk.client.script('head', [])  # type: ignore[arg-type]

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['error']['code'] == 'S3_STATUS'
    assert result['status_code'] == 502


@pytest.mark.parametrize(
    'contents',
    [
        'not-a-list',
        [None],
        [{'Key': '', 'Size': 0}],
        [{'Key': 'key', 'Size': -1}],
        [{'Key': 'key', 'Size': True}],
        [{'Key': 'key', 'Size': 0, 'StorageClass': 7}],
        [{'Key': 'key'}],
    ],
)
async def test_malformed_list_item_becomes_s3_status_502(
    sdk: SdkDouble,
    contents: object,
) -> None:
    """Every list item must satisfy the exact normalized schema."""
    sdk.client.script('list', {'Contents': contents})

    result = await request(
        's3://bucket', protocol='S3',
        protocol_info={'command': 'list'},
    )

    assert result['error']['code'] == 'S3_STATUS'
    assert result['status_code'] == 502


@pytest.mark.parametrize(
    'extra',
    [
        {'IsTruncated': 'yes'},
        {'NextContinuationToken': ''},
    ],
)
async def test_malformed_list_page_becomes_s3_status_502(
    sdk: SdkDouble,
    extra: Mapping[str, Any],
) -> None:
    """Page-level booleans and tokens are type checked."""
    sdk.client.script('list', {'Contents': [], **extra})

    result = await request(
        's3://bucket', protocol='S3',
        protocol_info={'command': 'list'},
    )

    assert result['error']['code'] == 'S3_STATUS'
    assert result['status_code'] == 502


async def test_missing_download_byte_count_is_malformed(
    sdk: SdkDouble,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The shared seam must return an observed byte count."""
    from asyncio_gateway.logic import s3_client

    async def missing_count(*args: Any, **kwargs: Any) -> Any:
        """Return a deliberately malformed shared-seam result."""
        return {'response': {}, 'bytes_written': None}

    monkeypatch.setattr(s3_client, 'stream_s3_download', missing_count)

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={
            'command': 'download',
            'local_path': str(tmp_path / 'download'),
        },
    )

    assert result['error']['code'] == 'S3_STATUS'
    assert result['status_code'] == 502


async def test_open_bucket_circuit_refuses_the_next_call(
    sdk: SdkDouble,
) -> None:
    """The strategy converts the shared breaker's open state."""
    sdk.client.script(
        'head', service_error(status=503, code='SlowDown'))
    config = {'maximum_failures': 1}

    failed = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={
            'command': 'head', 'circuit_breaker_config': config},
    )
    refused = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={
            'command': 'head', 'circuit_breaker_config': config},
    )

    assert failed['error']['code'] == 'S3_STATUS'
    assert refused['error']['code'] == 'CIRCUIT_OPEN'
    assert len(sdk.client.calls) == 1


async def test_causeless_retry_exhaustion_maps_to_transport(
    sdk: SdkDouble,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted breaker wrapper cannot escape as a bare failsafe type."""
    async def exhausted(
        breaker: CircuitBreakerHelper,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Raise the defensive no-cause shape."""
        raise RetriesExhausted()

    monkeypatch.setattr(CircuitBreakerHelper, 'run', exhausted)

    result = await request(
        's3://bucket/key', protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['error']['code'] == 'TRANSPORT'


async def test_each_bucket_has_an_independent_breaker(
    sdk: SdkDouble,
) -> None:
    """A transient failure is counted only against its bucket."""
    sdk.client.script(
        'head',
        service_error(status=503, code='SlowDown'),
        {'ResponseMetadata': {'HTTPStatusCode': 200}},
    )

    failed = await request(
        's3://bad-bucket/key', protocol='S3',
        protocol_info={'command': 'head'},
    )
    healthy = await request(
        's3://good-bucket/key', protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert failed['error']['code'] == 'S3_STATUS'
    assert healthy['ok'] is True
    assert get_breaker(
        's3', 'bad-bucket', UNKNOWN_PORT).failures == 1
    assert get_breaker(
        's3', 'good-bucket', UNKNOWN_PORT).failures == 0


async def test_cancellation_propagates_and_closes_client_context(
    sdk: SdkDouble,
) -> None:
    """Cancellation is never converted or counted and cleanup completes."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(**kwargs: Any) -> Mapping[str, Any]:
        """Block one head call until its task is cancelled."""
        entered.set()
        await release.wait()
        return {}

    sdk.client.script('head', blocked)
    task = asyncio.create_task(request(
        's3://bucket/key', protocol='S3',
        protocol_info={'command': 'head'},
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sdk.exits == 1
    assert get_breaker('s3', 'bucket', UNKNOWN_PORT).failures == 0


# --- PE-60 review iteration 1 --------------------------------------------


async def test_wrong_command_option_fails_before_preprocessor_or_session(
    sdk: SdkDouble,
) -> None:
    """Operation-specific R12 keys are an earliest-boundary contract."""
    processor_calls: list[str] = []

    async def record(*, response: dict[str, Any]) -> None:
        """Record a preprocessor invocation that must never happen."""
        processor_calls.append(response['protocol'])

    with pytest.raises(ConfigurationError, match='not accepted'):
        await request(
            's3://bucket/key',
            protocol='S3',
            protocol_info={
                'command': 'download',
                'local_path': '/tmp/unused',
                'max_upload_bytes': 1,
            },
            pre_processor_config={'function': record},
        )

    assert processor_calls == []
    assert sdk.session_arguments == []


@pytest.mark.parametrize(
    'url',
    [
        's3://bucket?',
        's3://bucket#',
        's3://bucket/key?',
        's3://bucket/key#',
    ],
)
async def test_empty_query_or_fragment_delimiter_fails_before_session(
    sdk: SdkDouble,
    url: str,
) -> None:
    """Syntactically present empty URI components remain forbidden."""
    with pytest.raises(ConfigurationError, match='query string or fragment'):
        await request(
            url,
            protocol='S3',
            protocol_info={'command': 'list'},
        )

    assert sdk.session_arguments == []


async def test_percent_encoded_key_delimiters_remain_valid(
    sdk: SdkDouble,
) -> None:
    """Encoded key data is not mistaken for URI syntax."""
    sdk.client.script('head', {})

    result = await request(
        's3://bucket/folder%3Fpart%23tail',
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['ok'] is True
    assert sdk.client.calls == [(
        'head',
        {'Bucket': 'bucket', 'Key': 'folder%3Fpart%23tail'},
    )]


async def test_cleanup_transport_error_cannot_replace_cancellation(
    sdk: SdkDouble,
) -> None:
    """A cancelling task restores CancelledError after SDK cleanup fails."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(**kwargs: Any) -> Mapping[str, Any]:
        """Wait until cancellation enters the client context cleanup."""
        entered.set()
        await release.wait()
        return {}

    sdk.client.script('head', blocked)
    sdk.exit_error = EndpointConnectionError(
        endpoint_url='https://cleanup.invalid')
    task = asyncio.create_task(request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sdk.exits == 1
    assert get_breaker('s3', 'bucket', UNKNOWN_PORT).failures == 0


async def test_non_cancelled_cleanup_transport_error_is_still_mapped(
    sdk: SdkDouble,
) -> None:
    """The cancellation restoration does not hide a genuine exit failure."""
    sdk.client.script('head', {})
    sdk.exit_error = EndpointConnectionError(
        endpoint_url='https://cleanup.invalid')

    result = await request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['error']['code'] == 'CONNECT'
    assert sdk.exits == 1


async def test_success_exit_receives_empty_exc_info(
    sdk: SdkDouble,
) -> None:
    """A successful body is paired with the exact empty exit triple."""
    sdk.client.script('head', {})

    result = await request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['ok'] is True
    assert sdk.exit_infos == [(None, None, None)]


async def test_body_error_exit_receives_exact_exc_info(
    sdk: SdkDouble,
) -> None:
    """Manual lowering passes the exact body object and traceback to exit."""
    failure = RuntimeError('exact body exc_info')
    sdk.client.script('head', failure)

    with pytest.raises(RuntimeError) as caught:
        await request(
            's3://bucket/key',
            protocol='S3',
            protocol_info={'command': 'head'},
        )

    error_type, error, body_traceback = sdk.exit_infos[0]
    assert caught.value is failure
    assert error_type is RuntimeError
    assert error is failure
    assert body_traceback is not None
    propagated = failure.__traceback__
    while propagated is not None and propagated is not body_traceback:
        propagated = propagated.tb_next
    assert propagated is body_traceback


def _logged_surfaces(caplog: pytest.LogCaptureFixture) -> str:
    """Render every captured logging field for secret-leak assertions."""
    return ''.join(str(record.__dict__) for record in caplog.records)


async def test_known_secrets_are_removed_from_all_success_surfaces(
    sdk: SdkDouble,
    tmp_path: Path,
) -> None:
    """Target, local path, payload, and SDK strings share one redactor."""
    access = 'access-collision-41'
    secret = 'secret-collision-41'
    body = ChunkedBody([b'x'])
    sdk.client.script(
        'download',
        {
            'Body': body,
            'ContentLength': 1,
            'ETag': f'etag-{access}-{secret}',
        },
    )
    target_parent = tmp_path / f's3-{access}-{secret}'
    target_parent.mkdir()
    target = target_parent / f'local-{access}-{secret}.bin'

    result = await request(
        f's3://bucket-{access}/key-{secret}',
        data={
            'innocent': f'{access}:{secret}',
            'password': f'{access}:{secret}',
        },
        auth=SimpleNamespace(login=access, password=secret),
        protocol='S3',
        protocol_info={
            'command': 'download',
            'local_path': str(target),
        },
    )

    assert result['ok'] is True
    assert target.read_bytes() == b'x'
    assert result['payload'] == {
        'innocent': f'{access}:{secret}',
        'password': '***redacted***',
    }
    s3_surfaces = dict(result)
    s3_surfaces['payload'] = '<shared payload policy checked separately>'
    assert access not in repr(s3_surfaces)
    assert secret not in repr(s3_surfaces)
    assert '***redacted***' in repr(s3_surfaces)


async def test_sdk_success_maps_and_list_items_use_known_secret_redaction(
    sdk: SdkDouble,
) -> None:
    """Normalized SDK maps/strings are safe except the server next token."""
    access = 'access-collision-42'
    secret = 'secret-collision-42'
    server_token = secret
    sdk.client.script(
        'list',
        {
            'Contents': [{
                'Key': f'key-{access}',
                'Size': 1,
                'ETag': f'etag-{secret}',
                'StorageClass': f'class-{access}',
            }],
            'IsTruncated': True,
            'NextContinuationToken': server_token,
        },
    )

    result = await request(
        f's3://bucket-{access}/prefix-{secret}',
        auth=SimpleNamespace(login=access, password=secret),
        protocol='S3',
        protocol_info={'command': 'list'},
    )

    assert (
        result['protocol_details']['next_continuation_token']
        == server_token)
    safe_details = dict(result['protocol_details'])
    safe_details['next_continuation_token'] = '<allowed opaque token>'
    safe_result = dict(result)
    safe_result['protocol_details'] = safe_details
    assert access not in repr(safe_result)
    assert secret not in repr(safe_result)


async def test_head_metadata_keys_and_values_use_known_secret_redaction(
    sdk: SdkDouble,
) -> None:
    """Both sides of an SDK metadata mapping are redacted."""
    access = 'access-collision-43'
    secret = 'secret-collision-43'
    sdk.client.script(
        'head',
        {
            'ContentType': f'type-{access}',
            'ETag': f'etag-{secret}',
            'Metadata': {
                f'meta-{access}': f'value-{secret}',
            },
        },
    )

    result = await request(
        's3://bucket/key',
        auth=SimpleNamespace(login=access, password=secret),
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert access not in repr(result)
    assert secret not in repr(result)
    assert '***redacted***' in repr(result['protocol_details'])


async def test_service_failure_and_common_log_use_safe_s3_surfaces(
    sdk: SdkDouble,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Service details, URL, message, cause, and log traceback are safe."""
    access = 'access-collision-44'
    secret = 'secret-collision-44'
    sdk.client.script(
        'head',
        service_error(
            status=403,
            code=f'AccessDenied-{access}',
            message=f'echo {access} {secret}',
            request_id=f'request-{secret}',
        ),
    )
    caplog.set_level(logging.WARNING)

    result = await request(
        f's3://bucket-{access}/key-{secret}',
        auth=SimpleNamespace(login=access, password=secret),
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    surfaces = repr(result) + _logged_surfaces(caplog)
    assert result['error']['code'] == 'S3_STATUS'
    assert access not in surfaces
    assert secret not in surfaces
    assert '***redacted***' in surfaces


async def test_transport_failure_and_common_log_use_safe_s3_surfaces(
    sdk: SdkDouble,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Foreign transport text and target collisions cannot reach the log."""
    access = 'access-collision-45'
    secret = 'secret-collision-45'
    sdk.client.script(
        'head',
        EndpointConnectionError(
            endpoint_url=f'https://{access}.invalid/{secret}'),
    )
    caplog.set_level(logging.ERROR)

    result = await request(
        f's3://bucket-{access}/key-{secret}',
        auth=SimpleNamespace(login=access, password=secret),
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    surfaces = repr(result) + _logged_surfaces(caplog)
    assert result['error']['code'] == 'CONNECT'
    assert access not in surfaces
    assert secret not in surfaces
    assert '***redacted***' in surfaces


# --- PE-60 review iteration 2 --------------------------------------------


@pytest.mark.parametrize(
    'access, secret',
    [
        ('protocol_details', 'bucket'),
        ('command', 'response_metadata'),
        ('metadata', 'content_type'),
    ],
)
async def test_structural_secret_collisions_preserve_exact_schemas(
    sdk: SdkDouble,
    access: str,
    secret: str,
) -> None:
    """Known values never rewrite fixed envelope/protocol field names."""
    sdk.client.script(
        'head',
        {
            'ContentLength': 1,
            'ContentType': f'remote-{access}',
            'ETag': f'remote-{secret}',
            'Metadata': {access: secret},
        },
    )

    result = await request(
        f's3://remote-{access}/remote-{secret}',
        data={'password': f'{access}:{secret}'},
        auth=SimpleNamespace(login=access, password=secret),
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert set(result) == set(GatewayResponse.__annotations__)
    details = result['protocol_details']
    assert set(details) == {
        'command',
        'bucket',
        'key',
        'content_length',
        'content_type',
        'etag',
        'last_modified',
        'metadata',
    }
    dynamic_values = [
        result['url'],
        result['payload']['password'],
        details['bucket'],
        details['key'],
        details['content_type'],
        details['etag'],
        details['metadata'],
    ]
    assert access not in repr(dynamic_values)
    assert secret not in repr(dynamic_values)
    assert set(details['metadata']) == {'***redacted***'}
    assert details['metadata']['***redacted***'] == '***redacted***'


@pytest.mark.parametrize(
    'cleanup_error',
    [
        NoCredentialsError(),
        RuntimeError('cleanup runtime must not win'),
    ],
    ids=['credential', 'runtime'],
)
async def test_cancellation_wins_over_every_cleanup_exception(
    sdk: SdkDouble,
    cleanup_error: BaseException,
) -> None:
    """SDK cleanup cannot replace the task's original cancellation."""
    entered = asyncio.Event()

    async def blocked(**kwargs: Any) -> Mapping[str, Any]:
        """Wait until the test cancels this operation."""
        entered.set()
        await asyncio.Event().wait()
        return {}

    sdk.client.script('head', blocked)
    sdk.exit_error = cleanup_error
    task = asyncio.create_task(request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel('original cancellation')

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert_cancelled_error_survives_task_boundary(
        caught.value,
        ('original cancellation',),
        original=sdk.exit_exceptions[0],
    )
    assert sdk.exits == 1


async def test_non_cancelled_cleanup_credential_error_is_classified(
    sdk: SdkDouble,
) -> None:
    """A genuine cleanup credential failure keeps its CONFIG mapping."""
    sdk.client.script('head', {})
    failure = NoCredentialsError()
    sdk.exit_error = failure

    result = await request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['error']['code'] == 'CONFIG'
    assert failure.args == ('S3 credential resolution failed',)


async def test_non_cancelled_cleanup_runtime_error_is_unchanged(
    sdk: SdkDouble,
) -> None:
    """A programming failure from cleanup is never converted or mutated."""
    sdk.client.script('head', {})
    failure = RuntimeError('runtime identity marker')
    cause = ValueError('cause identity marker')
    context = LookupError('context identity marker')
    failure.__cause__ = cause
    failure.__context__ = context
    sdk.exit_error = failure

    with pytest.raises(RuntimeError) as caught:
        await request(
            's3://bucket/key',
            protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert caught.value is failure
    assert failure.args == ('runtime identity marker',)
    assert failure.__cause__ is cause
    assert failure.__context__ is context


@pytest.mark.parametrize(
    'failure',
    [
        asyncio.CancelledError('secret-marker'),
        RuntimeError('secret-marker'),
        KeyboardInterrupt('secret-marker'),
        SystemExit('secret-marker'),
    ],
    ids=['cancelled', 'runtime', 'keyboard-interrupt', 'system-exit'],
)
async def test_non_public_base_exceptions_propagate_unchanged(
    sdk: SdkDouble,
    failure: BaseException,
) -> None:
    """Only typed public gateway failures may be sanitized by S3."""
    cause = ValueError('cause identity marker')
    context = LookupError('context identity marker')
    original_args = failure.args
    failure.__cause__ = cause
    failure.__context__ = context
    sdk.client.script('head', failure)

    with pytest.raises(type(failure)) as caught:
        await request(
            's3://bucket/key',
            auth=SimpleNamespace(
                login='access-marker', password='secret-marker'),
            protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert caught.value is failure
    assert failure.args == original_args
    assert failure.__cause__ is cause
    assert failure.__context__ is context


async def test_transport_mapping_sanitizes_foreign_exception_at_source(
    sdk: SdkDouble,
) -> None:
    """A mapped SDK transport object retains no foreign endpoint prose."""
    failure = EndpointConnectionError(
        endpoint_url='https://foreign-secret.invalid/raw-secret')
    sdk.client.script('head', failure)

    result = await request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['error']['code'] == 'CONNECT'
    assert 'foreign-secret' not in str(failure)
    assert 'raw-secret' not in str(failure)


# --- PE-60 review iteration 3 --------------------------------------------


@pytest.mark.parametrize(
    'outcome, expected_code',
    [
        (
            EndpointConnectionError(
                endpoint_url='https://transport.invalid'),
            'CONNECT',
        ),
        (
            service_error(status=403, code='AccessDenied'),
            'S3_STATUS',
        ),
    ],
    ids=['transport', 'service'],
)
async def test_failed_list_never_restores_preprocessor_token_field(
    sdk: SdkDouble,
    caplog: pytest.LogCaptureFixture,
    outcome: BaseException,
    expected_code: str,
) -> None:
    """Only a normalized successful server token has restore provenance."""
    caller_token = 'caller-token-provenance-secret'

    async def seed_details(*, response: dict[str, Any]) -> None:
        """Seed a same-named untrusted field before S3 dispatch."""
        response['protocol_details'] = {
            'next_continuation_token': caller_token,
        }

    sdk.client.script('list', outcome)
    caplog.set_level(logging.ERROR)

    result = await request(
        's3://bucket/prefix',
        protocol='S3',
        protocol_info={
            'command': 'list',
            'continuation_token': caller_token,
        },
        pre_processor_config={'function': seed_details},
    )

    surfaces = repr(result) + _logged_surfaces(caplog)
    assert result['error']['code'] == expected_code
    assert caller_token not in surfaces


async def test_nested_cleanup_chain_restores_original_cancellation(
    sdk: SdkDouble,
) -> None:
    """Live nested cleanup failures cannot bury task cancellation."""
    entered = asyncio.Event()

    async def blocked(**kwargs: Any) -> Mapping[str, Any]:
        """Wait for cancellation inside the SDK context."""
        entered.set()
        await asyncio.Event().wait()
        return {}

    sdk.client.script('head', blocked)
    sdk.exit_inner_error = ValueError('inner cleanup')
    sdk.exit_error = RuntimeError('outer cleanup')
    task = asyncio.create_task(request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel('nested original cancellation')

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert_cancelled_error_survives_task_boundary(
        caught.value,
        ('nested original cancellation',),
        original=sdk.exit_exceptions[0],
    )


async def test_successful_body_may_ignore_a_truthy_exit_result(
    sdk: SdkDouble,
) -> None:
    """A truthy exit result has no effect when there is no body failure."""
    sdk.client.script('head', {})
    sdk.exit_result = True

    result = await request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['ok'] is True
    assert sdk.exits == 1


@pytest.mark.parametrize(
    'failure',
    [
        KeyboardInterrupt('cleanup keyboard interrupt'),
        SystemExit('cleanup system exit'),
    ],
    ids=['keyboard-interrupt', 'system-exit'],
)
async def test_cleanup_base_exception_is_exact(
    sdk: SdkDouble,
    failure: BaseException,
) -> None:
    """The outcome task captures every cleanup BaseException unchanged."""
    sdk.client.script('head', {})
    cause = ValueError('cleanup cause')
    context = LookupError('cleanup context')
    failure.__cause__ = cause
    failure.__context__ = context
    sdk.exit_error = failure

    with pytest.raises(type(failure)) as caught:
        await request(
            's3://bucket/key',
            protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert caught.value is failure
    assert failure.__cause__ is cause
    assert failure.__context__ is context
    assert sdk.exit_tasks[0].cancelled() is False


async def test_cyclic_cleanup_chain_propagates_without_graph_inference(
    sdk: SdkDouble,
) -> None:
    """A cyclic stale chain remains an ordinary exact cleanup failure."""
    sdk.client.script('head', {})
    failure = RuntimeError('cyclic cleanup failure')
    marker = ValueError('cyclic cleanup marker')
    failure.__context__ = marker
    marker.__cause__ = failure
    sdk.exit_error = failure

    with pytest.raises(RuntimeError) as caught:
        await request(
            's3://bucket/key',
            protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert caught.value is failure
    assert failure.__context__ is marker
    assert marker.__cause__ is failure


async def test_body_cancellation_preserves_complete_exception_state(
    sdk: SdkDouble,
) -> None:
    """Captured body cancellation retains all public exception attributes."""
    entered = asyncio.Event()
    observed: list[asyncio.CancelledError] = []
    original_cause = ValueError('original cancellation cause')
    original_context = LookupError('original cancellation context')

    async def blocked(**kwargs: Any) -> Mapping[str, Any]:
        """Attach state to the exact cancellation injected by the parent."""
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            error.__cause__ = original_cause
            error.__context__ = original_context
            error.__suppress_context__ = True
            observed.append(error)
            raise

    sdk.client.script('head', blocked)
    task = asyncio.create_task(request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel('original cancellation args')

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert_cancelled_error_survives_task_boundary(
        caught.value,
        ('original cancellation args',),
        original=observed[0],
    )
    assert sdk.exit_exceptions[0] is observed[0]
    assert observed[0].__cause__ is original_cause
    assert observed[0].__context__ is original_context
    assert observed[0].__suppress_context__ is True


async def test_cyclic_payload_success_stays_success(
    sdk: SdkDouble,
) -> None:
    """S3 never recursively walks arbitrary caller payloads."""
    payload: dict[str, Any] = {}
    payload['self'] = payload
    sdk.client.script('head', {})

    result = await request(
        's3://bucket/key',
        data=payload,
        protocol='S3',
        protocol_info={'command': 'head'},
    )

    assert result['ok'] is True
    assert result['error'] is None


async def test_cyclic_payload_cannot_replace_programming_failure(
    sdk: SdkDouble,
) -> None:
    """A runtime error's identity wins over any cyclic payload echo."""
    payload: dict[str, Any] = {}
    payload['self'] = payload
    failure = RuntimeError('original programming failure')
    sdk.client.script('head', failure)

    with pytest.raises(RuntimeError) as caught:
        await request(
            's3://bucket/key',
            data=payload,
            protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert caught.value is failure


# --- PE-60 review iteration 4 --------------------------------------------


@pytest.mark.parametrize('branch', ['cause', 'context'])
async def test_modern_zero_cancellation_ignores_stale_cleanup_link(
    sdk: SdkDouble,
    branch: str,
) -> None:
    """Task state overrides stale indirect cancellation on modern Python."""
    sdk.client.script('head', {})
    stale = asyncio.CancelledError(f'stale {branch} cancellation')
    marker = ValueError(f'original {branch} marker')
    failure = RuntimeError(f'original {branch} runtime')
    if branch == 'cause':
        failure.__cause__ = stale
        failure.__context__ = marker
    else:
        failure.__cause__ = marker
        failure.__context__ = stale
    original_args = failure.args
    original_cause = failure.__cause__
    original_context = failure.__context__
    sdk.exit_error = failure

    with pytest.raises(RuntimeError) as caught:
        await request(
            's3://bucket/key',
            protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert caught.value is failure
    assert failure.args == original_args
    assert failure.__cause__ is original_cause
    assert failure.__context__ is original_context


async def test_direct_cleanup_cancellation_ignores_zero_task_count(
    sdk: SdkDouble,
) -> None:
    """A directly raised CancelledError always propagates unchanged."""
    sdk.client.script('head', {})
    failure = asyncio.CancelledError('direct cleanup cancellation')
    cause = ValueError('direct cancellation cause')
    context = LookupError('direct cancellation context')
    failure.__cause__ = cause
    failure.__context__ = context
    sdk.exit_error = failure

    with pytest.raises(asyncio.CancelledError) as caught:
        await request(
            's3://bucket/key',
            protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert caught.value is failure
    assert failure.args == ('direct cleanup cancellation',)
    assert failure.__cause__ is cause
    assert failure.__context__ is context


# --- PE-60 cancellation provenance redesign -------------------------------


async def test_actual_body_cancellation_beats_stale_cleanup_cause(
    sdk: SdkDouble,
) -> None:
    """A stale cleanup cause cannot replace the captured body cancel."""
    entered = asyncio.Event()

    async def blocked(**kwargs: Any) -> Mapping[str, Any]:
        """Block until the parent task is cancelled."""
        entered.set()
        await asyncio.Event().wait()
        return {}

    stale = asyncio.CancelledError('stale cleanup cancellation')
    cleanup = RuntimeError('cleanup failure')
    cleanup.__cause__ = stale
    sdk.exit_error = cleanup
    sdk.client.script('head', blocked)
    task = asyncio.create_task(request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel('actual body cancellation')

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert_cancelled_error_survives_task_boundary(
        caught.value,
        ('actual body cancellation',),
        original=sdk.exit_exceptions[0],
    )
    assert sdk.exits == 1


@pytest.mark.parametrize('exit_shape', ['suppress', 'error'])
async def test_body_cancellation_wins_over_every_exit_outcome(
    sdk: SdkDouble,
    exit_shape: str,
) -> None:
    """Suppression and cleanup error both lose to body cancellation."""
    entered = asyncio.Event()

    async def blocked(**kwargs: Any) -> Mapping[str, Any]:
        """Block until the parent task is cancelled."""
        entered.set()
        await asyncio.Event().wait()
        return {}

    sdk.client.script('head', blocked)
    if exit_shape == 'suppress':
        sdk.exit_result = True
    else:
        sdk.exit_error = RuntimeError('exit must lose')
    task = asyncio.create_task(request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel('body cancellation wins')

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert_cancelled_error_survives_task_boundary(
        caught.value,
        ('body cancellation wins',),
        original=sdk.exit_exceptions[0],
    )
    assert sdk.exits == 1


async def test_ordinary_body_error_with_false_exit_is_exact(
    sdk: SdkDouble,
) -> None:
    """A non-suppressing exit re-raises the exact body failure."""
    body_error = RuntimeError('ordinary body failure')
    cause = ValueError('body cause')
    context = LookupError('body context')
    body_error.__cause__ = cause
    body_error.__context__ = context
    sdk.client.script('head', body_error)

    with pytest.raises(RuntimeError) as caught:
        await request(
            's3://bucket/key',
            protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert caught.value is body_error
    assert body_error.args == ('ordinary body failure',)
    assert body_error.__cause__ is cause
    assert body_error.__context__ is context


async def test_hostile_exit_suppression_is_a_programming_defect(
    sdk: SdkDouble,
) -> None:
    """Suppressing aioboto3's body error never yields an unbound result."""
    sdk.client.script('head', RuntimeError('suppressed body failure'))
    sdk.exit_result = True

    with pytest.raises(RuntimeError) as caught:
        await request(
            's3://bucket/key',
            protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert type(caught.value) is RuntimeError
    assert str(caught.value) == (
        'S3 client context suppressed a body failure without an operation')
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize('exit_shape', ['success', 'error'])
async def test_parent_cancellation_during_blocked_exit_drains_cleanup(
    sdk: SdkDouble,
    exit_shape: str,
) -> None:
    """Shielded exit finishes before a parent cancellation is restored."""
    sdk.client.script('head', {})
    sdk.exit_started = asyncio.Event()
    sdk.exit_release = asyncio.Event()
    if exit_shape == 'error':
        sdk.exit_error = RuntimeError('blocked exit error')
    task = asyncio.create_task(request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    ))
    await asyncio.wait_for(sdk.exit_started.wait(), timeout=1)
    task.cancel('parent cancellation during exit')
    for _ in range(3):
        await asyncio.sleep(0)
    remained_pending = not task.done()
    sdk.exit_release.set()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert remained_pending is True
    assert_cancelled_error_survives_task_boundary(
        caught.value,
        ('parent cancellation during exit',),
    )
    assert sdk.exits == 1
    assert len(sdk.exit_tasks) == 1
    assert sdk.exit_tasks[0] is not task
    assert sdk.exit_tasks[0].done() is True
    assert sdk.exit_tasks[0].cancelled() is False
    assert sdk.exit_tasks[0] not in asyncio.all_tasks()


async def test_repeated_parent_cancellation_keeps_first_and_exits_once(
    sdk: SdkDouble,
) -> None:
    """Later cancels cannot replace the first while cleanup is draining."""
    sdk.client.script('head', {})
    sdk.exit_started = asyncio.Event()
    sdk.exit_release = asyncio.Event()
    task = asyncio.create_task(request(
        's3://bucket/key',
        protocol='S3',
        protocol_info={'command': 'head'},
    ))
    await asyncio.wait_for(sdk.exit_started.wait(), timeout=1)
    assert task.cancel('first parent cancellation') is True
    for _ in range(2):
        await asyncio.sleep(0)
    still_draining = not task.done()
    second_requested = task.cancel('second parent cancellation')
    for _ in range(2):
        await asyncio.sleep(0)
    sdk.exit_release.set()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert still_draining is True
    assert second_requested is True
    assert_cancelled_error_survives_task_boundary(
        caught.value,
        ('first parent cancellation',),
    )
    assert sdk.exits == 1
    assert len(sdk.exit_tasks) == 1
    assert sdk.exit_tasks[0].done() is True
    assert sdk.exit_tasks[0].cancelled() is False


@pytest.mark.parametrize('kind', ['error', 'cancel'])
async def test_enter_failure_is_exact_and_never_invokes_exit(
    sdk: SdkDouble,
    kind: str,
) -> None:
    """Failed context entry has no resource whose exit may run."""
    if kind == 'cancel':
        failure: BaseException = asyncio.CancelledError('enter cancellation')
    else:
        failure = RuntimeError('enter failure')
    cause = ValueError('enter cause')
    context = LookupError('enter context')
    failure.__cause__ = cause
    failure.__context__ = context
    sdk.enter_error = failure

    with pytest.raises(type(failure)) as caught:
        await request(
            's3://bucket/key',
            protocol='S3',
            protocol_info={'command': 'head'},
        )

    assert caught.value is failure
    assert failure.__cause__ is cause
    assert failure.__context__ is context
    assert sdk.enters == 1
    assert sdk.exits == 0

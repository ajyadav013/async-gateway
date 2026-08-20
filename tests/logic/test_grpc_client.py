"""Contract tests for the unary-unary gRPC strategy.

The unit layer uses deterministic handwritten channel/call doubles. A small
loopback generic-bytes server later proves that the adapter also matches the
real ``grpc.aio`` runtime without generated stubs or protobuf tooling.
"""

import asyncio
import importlib.util
import logging
import math
import ssl
from collections.abc import Awaitable, Callable, Sequence
from types import SimpleNamespace
from typing import Any, Optional, Union

import aiohttp

import grpc

import pytest

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.helpers.internal.breaker_registry import (
    get_breaker,
    registry_size,
)
from asyncio_gateway.logic import protocol_mapping
from asyncio_gateway.utils.constants import HTTP_TIMEOUT, MAX_RESPONSE_BYTES
from asyncio_gateway.utils.envelope import GatewayResponse
from asyncio_gateway.utils.exceptions import (
    ConfigurationError,
    SerializationError,
)


PeerMetadata = Sequence[tuple[str, Union[str, bytes]]]
Outcome = Union[
    Any,
    BaseException,
    Callable[[], Awaitable[Any]],
]


class CallScript:
    """One scripted unary result and its peer metadata."""

    def __init__(
        self,
        outcome: Outcome,
        *,
        initial_metadata: Union[PeerMetadata, BaseException] = (),
        trailing_metadata: Union[PeerMetadata, BaseException] = (),
    ) -> None:
        """Retain the deterministic call outcome and metadata."""
        self.outcome = outcome
        self.initial = initial_metadata
        self.trailing = trailing_metadata


class CallDouble:
    """One awaitable raw-bytes unary call."""

    def __init__(self, script: CallScript) -> None:
        """Store the script and observable cancellation state."""
        self.script = script
        self.cancel_calls = 0

    def __await__(self) -> Any:
        """Delegate awaiting to the deterministic response coroutine."""
        return self._response().__await__()

    async def _response(self) -> Any:
        """Return or raise the scripted outcome."""
        outcome = self.script.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome()
        return outcome

    async def initial_metadata(self) -> PeerMetadata:
        """Return or raise the scripted initial metadata."""
        if isinstance(self.script.initial, BaseException):
            raise self.script.initial
        return self.script.initial

    async def trailing_metadata(self) -> PeerMetadata:
        """Return or raise the scripted trailing metadata."""
        if isinstance(self.script.trailing, BaseException):
            raise self.script.trailing
        return self.script.trailing

    def cancel(self) -> bool:
        """Record explicit RPC cancellation."""
        self.cancel_calls += 1
        return True


class ChannelDouble:
    """One observable unary-only channel."""

    def __init__(self, sdk: 'GrpcDouble') -> None:
        """Store the owning recorder and lifecycle records."""
        self.sdk = sdk
        self.close_calls = 0

    def unary_unary(
        self,
        method: str,
        request_serializer: Any = None,
        response_deserializer: Any = None,
    ) -> Any:
        """Return the sole raw unary callable."""
        self.sdk.methods.append(
            (method, request_serializer, response_deserializer))
        if self.sdk.unary_error is not None:
            raise self.sdk.unary_error

        def invoke(
            body: bytes,
            *,
            timeout: float,
            metadata: Sequence[tuple[str, str]],
        ) -> CallDouble:
            self.sdk.requests.append((body, timeout, tuple(metadata)))
            if self.sdk.invoke_error is not None:
                raise self.sdk.invoke_error
            if not self.sdk.scripts:
                raise AssertionError('no scripted gRPC outcome remains')
            call = CallDouble(self.sdk.scripts.pop(0))
            self.sdk.calls.append(call)
            return call

        return invoke

    async def close(self, grace: Optional[float] = None) -> None:
        """Record channel cleanup."""
        self.close_calls += 1
        current = asyncio.current_task()
        if current is not None:
            self.sdk.close_tasks.append(current)
        if self.sdk.close_started is not None:
            self.sdk.close_started.set()
        if self.sdk.close_release is not None:
            await self.sdk.close_release.wait()
        if self.sdk.close_error is not None:
            raise self.sdk.close_error


class GrpcDouble:
    """Record channel selection and supply deterministic bytes."""

    def __init__(self, response: bytes = b'response') -> None:
        """Create an empty channel-selection record."""
        self.scripts: list[CallScript] = [CallScript(response)]
        self.insecure: list[tuple[str, Any]] = []
        self.secure: list[tuple[str, Any, Any]] = []
        self.credentials_calls: list[
            tuple[tuple[Any, ...], dict[str, Any]]
        ] = []
        self.credentials = object()
        self.channels: list[ChannelDouble] = []
        self.methods: list[tuple[str, Any, Any]] = []
        self.requests: list[
            tuple[bytes, float, tuple[tuple[str, str], ...]]
        ] = []
        self.calls: list[CallDouble] = []
        self.factory_error: Optional[BaseException] = None
        self.unary_error: Optional[BaseException] = None
        self.invoke_error: Optional[BaseException] = None
        self.close_error: Optional[BaseException] = None
        self.close_started: Optional[asyncio.Event] = None
        self.close_release: Optional[asyncio.Event] = None
        self.close_tasks: list[asyncio.Task[Any]] = []

    def script(self, *scripts: Union[CallScript, Outcome]) -> None:
        """Replace the remaining call scripts."""
        self.scripts = [
            item if isinstance(item, CallScript) else CallScript(item)
            for item in scripts
        ]

    def insecure_channel(
        self,
        target: str,
        *,
        options: Any = None,
    ) -> ChannelDouble:
        """Record explicit plaintext channel creation."""
        if self.factory_error is not None:
            raise self.factory_error
        self.insecure.append((target, options))
        channel = ChannelDouble(self)
        self.channels.append(channel)
        return channel

    def ssl_channel_credentials(self, *args: Any, **kwargs: Any) -> object:
        """Record platform-root credential creation."""
        self.credentials_calls.append((args, dict(kwargs)))
        return self.credentials

    def secure_channel(
        self,
        target: str,
        credentials: object,
        *,
        options: Any = None,
    ) -> ChannelDouble:
        """Record TLS channel creation."""
        if self.factory_error is not None:
            raise self.factory_error
        self.secure.append((target, credentials, options))
        channel = ChannelDouble(self)
        self.channels.append(channel)
        return channel


@pytest.fixture(autouse=True)
def focused_grpc_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the lane class temporarily once its module exists."""
    module_name = 'asyncio_gateway.logic.grpc_client'
    if importlib.util.find_spec(module_name) is None:
        return
    from asyncio_gateway.logic.grpc_client import GrpcRequest

    monkeypatch.setitem(protocol_mapping, 'GRPC', GrpcRequest)


@pytest.fixture()
def grpc_double(monkeypatch: pytest.MonkeyPatch) -> GrpcDouble:
    """Patch all gRPC channel constructors with a deterministic double."""
    double = GrpcDouble()
    monkeypatch.setattr(grpc.aio, 'insecure_channel', double.insecure_channel)
    monkeypatch.setattr(grpc.aio, 'secure_channel', double.secure_channel)
    monkeypatch.setattr(
        grpc, 'ssl_channel_credentials', double.ssl_channel_credentials)
    return double


def grpc_info(**overrides: Any) -> dict[str, Any]:
    """Build the minimum valid gRPC option mapping plus overrides."""
    info: dict[str, Any] = {'method': '/package.Service/Method'}
    info.update(overrides)
    return info


def rpc_error(
    code: grpc.StatusCode,
    *,
    details: Optional[str] = 'remote gRPC failure',
    initial: PeerMetadata = (),
    trailing: PeerMetadata = (),
    debug: str = 'debug string must remain private',
) -> grpc.aio.AioRpcError:
    """Build one genuine grpcio status failure without a remote server."""
    return grpc.aio.AioRpcError(
        code,
        grpc.aio.Metadata(*initial) if initial else None,
        grpc.aio.Metadata(*trailing) if trailing else None,
        details,
        debug,
    )


def retrying_config(retries: int) -> dict[str, Any]:
    """Build an immediate deterministic gateway retry configuration."""
    return {
        'retry_config': {
            'allowed_retries': retries,
            'delay': 0,
            'jitter': False,
        },
    }


def logged_surfaces(caplog: pytest.LogCaptureFixture) -> str:
    """Render every logging field for secret non-disclosure assertions."""
    return ''.join(str(record.__dict__) for record in caplog.records)


def test_private_target_guard_and_awaitable_cleanup_defences() -> None:
    """Direct-only guards reject wrong schemes and tolerate odd awaitables."""
    from asyncio_gateway.logic.grpc_client import (
        _discard_awaitable,
        _grpc_target,
    )

    with pytest.raises(ConfigurationError, match='scheme'):
        _grpc_target('http://service.test:50051')

    _discard_awaitable(object())

    class RaisingClose:
        """A deceptive awaitable whose optional close hook fails."""

        def close(self) -> None:
            """Raise a failure the warning-suppression path must ignore."""
            raise RuntimeError('close hook failure')

    _discard_awaitable(RaisingClose())


async def test_raw_plaintext_unary_success_uses_public_entrypoint(
    grpc_double: GrpcDouble,
) -> None:
    """Send and return raw GRPC bytes through the common envelope."""
    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info={'method': '/package.Service/Method'},
    )

    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['text'] == 'cmVzcG9uc2U='
    assert result['json'] is None
    assert result['protocol_details'] == {
        'method': '/package.Service/Method',
        'grpc_status': 'OK',
        'grpc_details': None,
        'response_encoding': 'base64',
        'initial_metadata': [],
        'trailing_metadata': [],
        'initial_metadata_omitted': 0,
        'trailing_metadata_omitted': 0,
    }
    assert grpc_double.insecure == [(
        'service.test:50051',
        [('grpc.max_receive_message_length', MAX_RESPONSE_BYTES)],
    )]
    assert grpc_double.secure == []
    assert grpc_double.methods == [(
        '/package.Service/Method', None, None)]
    assert grpc_double.requests == [(
        b'request', float(HTTP_TIMEOUT), (),
    )]
    assert grpc_double.channels[0].close_calls == 1


@pytest.mark.parametrize(
    'url',
    [
        'http://service.test:50051',
        'grpc://service.test',
        'grpc://service.test:0',
        'grpc://service.test:65536',
        'grpc://service.test:not-a-port',
        'grpc://:50051',
        'grpc://user@service.test:50051',
        'grpc://user:password@service.test:50051',
        'grpc://service.test:50051/',
        'grpc://service.test:50051/path',
        'grpc://service.test:50051?',
        'grpc://service.test:50051?x=1',
        'grpc://service.test:50051#',
        'grpc://service.test:50051#fragment',
        'grpc://[::1:50051',
    ],
)
async def test_invalid_target_is_rejected_before_base_or_channel(
    grpc_double: GrpcDouble,
    url: str,
) -> None:
    """Every ambiguous target shape fails before breaker lookup or I/O."""
    with pytest.raises(ConfigurationError):
        await request(
            url,
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(),
        )

    assert registry_size() == 0
    assert grpc_double.channels == []


@pytest.mark.parametrize(
    'method',
    [
        None,
        b'/package.Service/Method',
        '',
        '   ',
        'package.Service/Method',
        '/package.Service',
        '/package.Service/Method/Stream',
        '/package/Method',
        '/Package.Service/method-name',
        '/package..Service/Method',
    ],
)
async def test_invalid_method_is_rejected_before_channel(
    grpc_double: GrpcDouble,
    method: object,
) -> None:
    """The generic callable accepts only a fully qualified unary path."""
    with pytest.raises(ConfigurationError, match='method'):
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info={'method': method},
        )

    assert grpc_double.channels == []


async def test_missing_method_is_rejected_before_channel(
    grpc_double: GrpcDouble,
) -> None:
    """The required-key inventory rejects omission at the entrypoint."""
    with pytest.raises(ConfigurationError, match='method'):
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info={},
        )

    assert grpc_double.channels == []


async def test_unknown_option_fails_before_processor_or_channel(
    grpc_double: GrpcDouble,
) -> None:
    """R12's closed inventory runs before caller code or transport I/O."""
    processor_calls: list[str] = []

    async def processor(*, response: GatewayResponse) -> None:
        """Record an invocation that must never occur."""
        processor_calls.append(response['protocol'])

    with pytest.raises(ConfigurationError, match='unknown'):
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(compression='gzip'),
            pre_processor_config={'function': processor},
        )

    assert processor_calls == []
    assert grpc_double.channels == []


@pytest.mark.parametrize('auth', [object(), SimpleNamespace(token='secret')])
async def test_auth_is_rejected_before_base_or_channel(
    grpc_double: GrpcDouble,
    auth: object,
) -> None:
    """The narrow adapter has no hidden auth-to-metadata conversion."""
    with pytest.raises(ConfigurationError, match='auth'):
        await request(
            'grpc://service.test:50051',
            data=b'request',
            auth=auth,
            protocol='GRPC',
            protocol_info=grpc_info(),
        )

    assert registry_size() == 0
    assert grpc_double.channels == []


@pytest.mark.parametrize(
    'metadata',
    [
        'x=y',
        {'x': 'y'},
        (('x', 'y', 'z'),),
        ((1, 'value'),),
        (('', 'value'),),
        (('A', 'value'),),
        (('naïve', 'value'),),
        (('has/slash', 'value'),),
        (('x' * 65, 'value'),),
        (('grpc-timeout', 'value'),),
        (('token-bin', 'value'),),
        (('name', b'value'),),
        (('name', 'line\nfeed'),),
        (('name', '\x7f'),),
        (('name', 'é'),),
        (('name', 'x' * 8193),),
        tuple(('name', '') for _ in range(65)),
        tuple(('name', 'x' * 513) for _ in range(64)),
    ],
)
async def test_invalid_request_metadata_is_rejected_before_channel(
    grpc_double: GrpcDouble,
    metadata: object,
) -> None:
    """Metadata count, grammar, type, and size bounds are preflighted."""
    with pytest.raises(ConfigurationError, match='metadata'):
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(metadata=metadata),
        )

    assert grpc_double.channels == []


async def test_metadata_preserves_order_duplicates_and_boundary_lengths(
    grpc_double: GrpcDouble,
) -> None:
    """The maximum valid request metadata is forwarded byte-for-byte."""
    metadata = tuple(
        ('name' if index % 2 else 'token', 'x' * 512)
        for index in range(64)
    )

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(metadata=metadata),
    )

    assert result['ok'] is True
    assert grpc_double.requests[0][2] == metadata
    assert repr(metadata) not in repr(result)


@pytest.mark.parametrize('key,value', [
    ('timeout', 0),
    ('timeout', True),
    ('max_response_bytes', 0),
    ('max_response_bytes', True),
    ('max_response_bytes', 1.5),
])
async def test_shared_timeout_and_cap_validators_run_before_channel(
    grpc_double: GrpcDouble,
    key: str,
    value: object,
) -> None:
    """The adapter reuses the positive deadline and byte-cap rules."""
    with pytest.raises(ConfigurationError, match=key):
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(**{key: value}),
        )

    assert grpc_double.channels == []


@pytest.mark.parametrize(
    'timeout',
    [math.nan, math.inf, -math.inf],
    ids=['nan', 'positive-infinity', 'negative-infinity'],
)
async def test_grpc_timeout_rejects_non_finite_values_before_base(
    grpc_double: GrpcDouble,
    timeout: float,
) -> None:
    """Require a finite gRPC deadline after the shared type check."""
    with pytest.raises(ConfigurationError, match='timeout'):
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(timeout=timeout),
        )

    assert registry_size() == 0
    assert grpc_double.channels == []


async def test_grpc_receive_ceiling_rejects_signed_c_int_overflow_pre_base(
    grpc_double: GrpcDouble,
) -> None:
    """The grpcio channel option cannot represent values above C INT_MAX."""
    with pytest.raises(ConfigurationError, match='max_response_bytes'):
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(max_response_bytes=2 ** 31),
        )

    assert registry_size() == 0
    assert grpc_double.channels == []


async def test_grpc_receive_ceiling_accepts_signed_c_int_maximum(
    grpc_double: GrpcDouble,
) -> None:
    """The largest signed C integer remains a usable grpcio channel option."""
    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(max_response_bytes=2 ** 31 - 1),
    )

    assert result['ok'] is True
    assert grpc_double.insecure == [(
        'service.test:50051',
        [('grpc.max_receive_message_length', 2 ** 31 - 1)],
    )]


async def test_tls_target_uses_platform_roots_and_only_secure_channel(
    grpc_double: GrpcDouble,
) -> None:
    """The grpcs scheme has one platform-root TLS path and no downgrade."""
    result = await request(
        'grpcs://service.test:443',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(max_response_bytes=4096, timeout=0.25),
    )

    assert result['ok'] is True
    assert grpc_double.credentials_calls == [((), {})]
    assert grpc_double.insecure == []
    assert grpc_double.secure == [(
        'service.test:443',
        grpc_double.credentials,
        [('grpc.max_receive_message_length', 4096)],
    )]
    assert grpc_double.requests == [(b'request', 0.25, ())]


@pytest.mark.parametrize(
    'payload',
    [b'bytes', bytearray(b'bytearray'), memoryview(b'memoryview')],
    ids=['bytes', 'bytearray', 'memoryview'],
)
async def test_bytes_like_request_is_frozen_before_channel(
    grpc_double: GrpcDouble,
    payload: object,
) -> None:
    """The raw path copies any buffer-protocol input into immutable bytes."""
    result = await request(
        'grpc://service.test:50051',
        data=payload,  # type: ignore[arg-type]
        protocol='GRPC',
        protocol_info=grpc_info(),
    )

    assert result['ok'] is True
    assert type(grpc_double.requests[0][0]) is bytes
    assert grpc_double.requests[0][0] == bytes(payload)


async def test_non_bytes_request_without_serializer_is_rejected_pre_channel(
    grpc_double: GrpcDouble,
) -> None:
    """Generic Python objects need an explicit serializer."""
    with pytest.raises(ConfigurationError, match='bytes-like'):
        await request(
            'grpc://service.test:50051',
            data={'message': 'hello'},
            protocol='GRPC',
            protocol_info=grpc_info(),
        )

    assert grpc_double.channels == []
    assert registry_size() == 0


async def test_request_serializer_runs_once_before_channel_and_reuses_bytes(
    grpc_double: GrpcDouble,
) -> None:
    """One synchronous serialization feeds the first and every retry."""
    calls: list[object] = []

    def serializer(value: object) -> bytes:
        calls.append(value)
        return b'serialized-once'

    grpc_double.script(
        rpc_error(grpc.StatusCode.UNAVAILABLE),
        b'response',
    )
    payload = {'message': 'hello'}

    result = await request(
        'grpc://service.test:50051',
        data=payload,
        protocol='GRPC',
        protocol_info=grpc_info(
            request_serializer=serializer,
            circuit_breaker_config=retrying_config(1),
        ),
    )

    assert result['ok'] is True
    assert calls == [payload]
    assert [item[0] for item in grpc_double.requests] == [
        b'serialized-once', b'serialized-once']


@pytest.mark.parametrize(
    'serializer',
    [None, 'not-callable', lambda value: bytearray(b'not-bytes')],
    ids=['none', 'not-callable', 'returns-bytearray'],
)
async def test_invalid_request_serializer_is_rejected_before_channel(
    grpc_double: GrpcDouble,
    serializer: object,
) -> None:
    """Serializer shape and exact bytes result are caller configuration."""
    with pytest.raises(ConfigurationError, match='request_serializer'):
        await request(
            'grpc://service.test:50051',
            data={'message': 'hello'},
            protocol='GRPC',
            protocol_info=grpc_info(request_serializer=serializer),
        )

    assert grpc_double.channels == []
    assert registry_size() == 0


async def test_async_request_serializer_is_rejected_without_invocation(
    grpc_double: GrpcDouble,
) -> None:
    """An async serializer cannot smuggle event-loop work into preflight."""
    calls: list[object] = []

    async def serializer(value: object) -> bytes:
        calls.append(value)
        return b'never'

    with pytest.raises(ConfigurationError, match='synchronous'):
        await request(
            'grpc://service.test:50051',
            data={'message': 'hello'},
            protocol='GRPC',
            protocol_info=grpc_info(request_serializer=serializer),
        )

    assert calls == []
    assert grpc_double.channels == []


async def test_request_serializer_exception_is_safe_and_pre_channel(
    grpc_double: GrpcDouble,
) -> None:
    """A caller serializer failure becomes CONFIG without breaker or I/O."""
    secret = 'serializer-exception-secret'

    def serializer(value: object) -> bytes:
        raise RuntimeError(secret)

    with pytest.raises(ConfigurationError) as caught:
        await request(
            'grpc://service.test:50051',
            data={'password': secret},
            protocol='GRPC',
            protocol_info=grpc_info(request_serializer=serializer),
        )

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert grpc_double.channels == []
    assert registry_size() == 0


async def test_request_serializer_returning_awaitable_is_rejected_pre_channel(
    grpc_double: GrpcDouble,
) -> None:
    """A deceptive synchronous wrapper cannot return a coroutine."""
    calls: list[object] = []

    async def serialized() -> bytes:
        return b'never'

    def serializer(value: object) -> Any:
        calls.append(value)
        return serialized()

    with pytest.raises(ConfigurationError, match='synchronous'):
        await request(
            'grpc://service.test:50051',
            data={'message': 'hello'},
            protocol='GRPC',
            protocol_info=grpc_info(request_serializer=serializer),
        )

    assert len(calls) == 1
    assert grpc_double.channels == []


async def test_response_deserializer_retains_base64_and_exact_json(
    grpc_double: GrpcDouble,
) -> None:
    """A finite JSON-safe result augments rather than replaces raw bytes."""
    calls: list[bytes] = []
    normalized = {
        'message': ['hello', 1, 1.5, True, None],
    }

    def deserialize(value: bytes) -> object:
        calls.append(value)
        return normalized

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(response_deserializer=deserialize),
    )

    assert result['ok'] is True
    assert result['text'] == 'cmVzcG9uc2U='
    assert result['json'] is normalized
    assert calls == [b'response']


@pytest.mark.parametrize('deserializer', [None, 'not-callable'])
async def test_invalid_response_deserializer_is_rejected_pre_channel(
    grpc_double: GrpcDouble,
    deserializer: object,
) -> None:
    """A supplied response deserializer must be callable and synchronous."""
    with pytest.raises(ConfigurationError, match='response_deserializer'):
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(response_deserializer=deserializer),
        )

    assert grpc_double.channels == []


async def test_async_response_deserializer_is_rejected_pre_channel(
    grpc_double: GrpcDouble,
) -> None:
    """The response hook cannot be an async callable."""
    calls: list[bytes] = []

    async def deserialize(value: bytes) -> object:
        calls.append(value)
        return {'never': True}

    with pytest.raises(ConfigurationError, match='synchronous'):
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(response_deserializer=deserialize),
        )

    assert calls == []
    assert grpc_double.channels == []


def invalid_json_values() -> list[object]:
    """Build non-finite, unsupported, and cyclic deserializer results."""
    class ExplodingMapping(dict[str, object]):
        """A mapping whose traversal fails like a hostile hook result."""

        def items(self) -> Any:
            """Raise instead of exposing caller-controlled contents."""
            raise RuntimeError('mapping traversal secret')

    cyclic_list: list[object] = []
    cyclic_list.append(cyclic_list)
    cyclic_mapping: dict[str, object] = {}
    cyclic_mapping['self'] = cyclic_mapping
    return [
        math.nan,
        math.inf,
        (1, 2),
        {1, 2},
        object(),
        {1: 'non-string-key'},
        cyclic_list,
        cyclic_mapping,
        ExplodingMapping(),
    ]


@pytest.mark.parametrize('normalized', invalid_json_values())
async def test_non_json_safe_deserializer_result_preserves_response(
    grpc_double: GrpcDouble,
    normalized: object,
) -> None:
    """Invalid normalized data is SERIALIZATION/502 after receipt."""
    grpc_double.script(CallScript(
        b'response', initial_metadata=(('peer', 'initial'),)))

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(
            response_deserializer=lambda value: normalized),
    )

    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error']['code'] == 'SERIALIZATION'
    assert result['text'] == 'cmVzcG9uc2U='
    assert result['json'] is None
    assert result['protocol_details']['initial_metadata'] == [{
        'key': 'peer', 'value': 'initial', 'encoding': None}]
    assert grpc_double.channels[0].close_calls == 1


async def test_response_deserializer_exception_is_safe_and_retains_response(
    grpc_double: GrpcDouble,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deserializer prose cannot leak while raw response material survives."""
    secret = 'deserializer-exception-secret'

    def deserialize(value: bytes) -> object:
        raise RuntimeError(secret)

    caplog.set_level(logging.ERROR)
    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(response_deserializer=deserialize),
    )

    surfaces = repr(result) + logged_surfaces(caplog)
    assert result['error']['code'] == 'SERIALIZATION'
    assert result['status_code'] == 502
    assert result['text'] == 'cmVzcG9uc2U='
    assert secret not in surfaces


async def test_deserializer_returning_awaitable_is_serialization_failure(
    grpc_double: GrpcDouble,
) -> None:
    """A deceptive response wrapper fails only after preserving bytes."""
    async def normalized() -> object:
        return {'never': True}

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(
            response_deserializer=lambda value: normalized()),
    )

    assert result['error']['code'] == 'SERIALIZATION'
    assert result['text'] == 'cmVzcG9uc2U='


async def test_non_bytes_peer_response_is_serialization_failure(
    grpc_double: GrpcDouble,
) -> None:
    """The raw generic callable may only yield bytes."""
    grpc_double.script('not bytes')

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(),
    )

    assert result['error']['code'] == 'SERIALIZATION'
    assert result['status_code'] == 502
    assert result['text'] == ''
    assert result['json'] is None


STATUS_HTTP = [
    (grpc.StatusCode.CANCELLED, 499),
    (grpc.StatusCode.UNKNOWN, 502),
    (grpc.StatusCode.INVALID_ARGUMENT, 400),
    (grpc.StatusCode.DEADLINE_EXCEEDED, 504),
    (grpc.StatusCode.NOT_FOUND, 404),
    (grpc.StatusCode.ALREADY_EXISTS, 409),
    (grpc.StatusCode.PERMISSION_DENIED, 403),
    (grpc.StatusCode.RESOURCE_EXHAUSTED, 429),
    (grpc.StatusCode.FAILED_PRECONDITION, 412),
    (grpc.StatusCode.ABORTED, 409),
    (grpc.StatusCode.OUT_OF_RANGE, 400),
    (grpc.StatusCode.UNIMPLEMENTED, 501),
    (grpc.StatusCode.INTERNAL, 500),
    (grpc.StatusCode.UNAVAILABLE, 503),
    (grpc.StatusCode.DATA_LOSS, 500),
    (grpc.StatusCode.UNAUTHENTICATED, 401),
]
RETRYABLE_STATUSES = [
    grpc.StatusCode.UNKNOWN,
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.INTERNAL,
    grpc.StatusCode.UNAVAILABLE,
]
ABORTABLE_STATUSES = [
    status for status, _ in STATUS_HTTP
    if status not in RETRYABLE_STATUSES
]


@pytest.mark.parametrize('status, expected_http', STATUS_HTTP)
async def test_every_grpc_status_has_the_frozen_http_mapping(
    grpc_double: GrpcDouble,
    status: grpc.StatusCode,
    expected_http: int,
) -> None:
    """Every canonical non-OK status maps deterministically."""
    grpc_double.script(rpc_error(status))

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(),
    )

    assert result['ok'] is False
    assert result['status_code'] == expected_http
    assert result['error']['code'] == 'GRPC_STATUS'
    assert result['protocol_details']['grpc_status'] == status.name
    breaker = get_breaker('grpc', 'service.test', 50051)
    assert breaker.failures == (1 if status in RETRYABLE_STATUSES else 0)


async def test_failure_schema_bounds_and_redacts_each_peer_collection(
    grpc_double: GrpcDouble,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failure metadata is independently bounded and never exposes secrets."""
    request_secret = 'request-metadata-secret-70'
    debug_secret = 'native-debug-secret-70'
    initial: list[tuple[str, Union[str, bytes]]] = [
        ('authorization', request_secret),
        ('visible', 'hello'),
        ('blob-bin', b'\x00\xff'),
    ]
    initial.extend(('item', str(index)) for index in range(63))
    trailing: list[tuple[str, Union[str, bytes]]] = [
        ('custom', request_secret),
    ]
    trailing.extend(('trail', str(index)) for index in range(64))
    grpc_double.script(rpc_error(
        grpc.StatusCode.PERMISSION_DENIED,
        details=(
            f'reflected {request_secret} '
            f'?custom={request_secret}'),
        initial=initial,
        trailing=trailing,
        debug=f'debug {debug_secret} {request_secret}',
    ))
    caplog.set_level(logging.WARNING)

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(
            metadata=(('authorization', request_secret),),
            redact_query_params=['custom'],
        ),
    )

    details = result['protocol_details']
    assert set(details) == {
        'method',
        'grpc_status',
        'grpc_details',
        'response_encoding',
        'initial_metadata',
        'trailing_metadata',
        'initial_metadata_omitted',
        'trailing_metadata_omitted',
    }
    assert details['grpc_status'] == 'PERMISSION_DENIED'
    assert details['response_encoding'] is None
    assert details['initial_metadata_omitted'] == 2
    assert details['trailing_metadata_omitted'] == 1
    assert details['initial_metadata'][:3] == [
        {'key': 'authorization', 'value': '***', 'encoding': None},
        {'key': 'visible', 'value': 'hello', 'encoding': None},
        {'key': 'blob-bin', 'value': 'AP8=', 'encoding': 'base64'},
    ]
    assert details['trailing_metadata'][0] == {
        'key': 'custom', 'value': '***', 'encoding': None}
    surfaces = repr(result) + logged_surfaces(caplog)
    assert request_secret not in surfaces
    assert debug_secret not in surfaces
    assert 'debug string must remain private' not in surfaces
    assert result['error']['cause'] is None
    assert result['text'] == ''
    assert result['json'] is None


async def test_sensitive_metadata_collisions_are_removed_from_owned_surfaces(
    grpc_double: GrpcDouble,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Known metadata secrets also mask target, method, peer key, and logs."""
    method_secret = '/package.Service/Method'
    host_secret = 'service.test'
    peer_key_secret = 'peer'
    grpc_double.script(rpc_error(
        grpc.StatusCode.PERMISSION_DENIED,
        details=f'{method_secret} at {host_secret}',
        initial=((peer_key_secret, 'visible'),),
    ))
    caplog.set_level(logging.WARNING)

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(metadata=(
            ('authorization', method_secret),
            ('x-api-key', host_secret),
            ('token', peer_key_secret),
        )),
    )

    surfaces = repr(result) + logged_surfaces(caplog)
    assert method_secret not in surfaces
    assert host_secret not in surfaces
    assert peer_key_secret not in surfaces
    assert result['protocol_details']['initial_metadata'][0]['key'] == '***'


async def test_absent_peer_metadata_and_details_have_exact_empty_values(
    grpc_double: GrpcDouble,
) -> None:
    """None from grpcio normalizes without inventing diagnostic strings."""
    grpc_double.script(rpc_error(
        grpc.StatusCode.NOT_FOUND, details=None))

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(),
    )

    assert result['protocol_details']['grpc_details'] is None
    assert result['protocol_details']['initial_metadata'] == []
    assert result['protocol_details']['trailing_metadata'] == []
    assert result['protocol_details']['initial_metadata_omitted'] == 0
    assert result['protocol_details']['trailing_metadata_omitted'] == 0


async def test_unknown_native_status_is_safely_normalized_to_unknown(
    grpc_double: GrpcDouble,
) -> None:
    """A malformed native code cannot escape the deterministic status map."""
    grpc_double.script(grpc.aio.AioRpcError(
        None, None, None, None, 'private debug'))

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(),
    )

    assert result['status_code'] == 502
    assert result['protocol_details']['grpc_status'] == 'UNKNOWN'


@pytest.mark.parametrize('status', RETRYABLE_STATUSES)
async def test_transport_like_status_retries_exactly_n_plus_one(
    grpc_double: GrpcDouble,
    status: grpc.StatusCode,
) -> None:
    """Only the four transport-like statuses replay under gateway policy."""
    grpc_double.script(
        rpc_error(status),
        rpc_error(status),
        b'recovered',
    )

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(
            circuit_breaker_config=retrying_config(2)),
    )

    assert result['ok'] is True
    assert result['text'] == 'cmVjb3ZlcmVk'
    assert len(grpc_double.requests) == 3
    assert get_breaker('grpc', 'service.test', 50051).failures == 0


@pytest.mark.parametrize('status', ABORTABLE_STATUSES)
async def test_application_status_aborts_without_count_or_retry(
    grpc_double: GrpcDouble,
    status: grpc.StatusCode,
) -> None:
    """Application/quota statuses are never replayed or breaker-counted."""
    grpc_double.script(rpc_error(status), b'must remain unused')

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(
            circuit_breaker_config=retrying_config(3)),
    )

    assert result['error']['code'] == 'GRPC_STATUS'
    assert result['protocol_details']['grpc_status'] == status.name
    assert len(grpc_double.requests) == 1
    assert get_breaker('grpc', 'service.test', 50051).failures == 0


async def test_retryable_exhaustion_uses_last_status_snapshot(
    grpc_double: GrpcDouble,
) -> None:
    """Internal breaker wrappers never alter the public status contract."""
    grpc_double.script(
        rpc_error(grpc.StatusCode.UNKNOWN, details='first'),
        rpc_error(grpc.StatusCode.UNAVAILABLE, details='last'),
    )

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(
            circuit_breaker_config=retrying_config(1)),
    )

    assert result['status_code'] == 503
    assert result['protocol_details']['grpc_status'] == 'UNAVAILABLE'
    assert result['protocol_details']['grpc_details'] == 'last'
    assert len(grpc_double.requests) == 2
    assert get_breaker('grpc', 'service.test', 50051).failures == 2


async def test_peer_resource_exhausted_is_always_abortable(
    grpc_double: GrpcDouble,
) -> None:
    """Peer quota RESOURCE_EXHAUSTED cannot poison or replay the circuit."""
    grpc_double.script(
        rpc_error(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            details='peer quota exceeded'),
        b'must remain unused',
    )

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(
            circuit_breaker_config=retrying_config(3)),
    )

    assert result['status_code'] == 429
    assert result['protocol_details']['grpc_status'] == 'RESOURCE_EXHAUSTED'
    assert len(grpc_double.requests) == 1
    assert get_breaker('grpc', 'service.test', 50051).failures == 0


async def test_local_receive_cap_is_resource_exhausted_and_abortable(
    grpc_double: GrpcDouble,
) -> None:
    """A double that ignores the channel option is still locally bounded."""
    grpc_double.script(b'too large', b'must remain unused')

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(
            max_response_bytes=3,
            circuit_breaker_config=retrying_config(3),
        ),
    )

    assert result['status_code'] == 429
    assert result['error']['code'] == 'GRPC_STATUS'
    assert result['protocol_details']['grpc_status'] == 'RESOURCE_EXHAUSTED'
    assert len(grpc_double.requests) == 1
    assert get_breaker('grpc', 'service.test', 50051).failures == 0


async def test_retryable_failure_can_open_destination_circuit(
    grpc_double: GrpcDouble,
) -> None:
    """One configured counted failure opens only this destination."""
    grpc_double.script(rpc_error(grpc.StatusCode.UNAVAILABLE))
    info = grpc_info(circuit_breaker_config={'maximum_failures': 1})

    failed = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=info,
    )
    blocked = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=info,
    )

    assert failed['error']['code'] == 'GRPC_STATUS'
    assert blocked['error']['code'] == 'CIRCUIT_OPEN'
    assert len(grpc_double.requests) == 1
    assert len(grpc_double.channels) == 2
    assert all(channel.close_calls == 1 for channel in grpc_double.channels)


async def test_task_cancellation_cancels_rpc_and_closes_channel_exactly(
    grpc_double: GrpcDouble,
) -> None:
    """Cancellation is never enveloped, counted, or allowed to leak a call."""
    entered = asyncio.Event()
    observed: list[asyncio.CancelledError] = []

    async def blocked() -> bytes:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            observed.append(error)
            raise

    grpc_double.script(blocked)
    task = asyncio.create_task(request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(),
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel('caller cancelled gRPC')

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert caught.value is observed[0]
    assert caught.value.args == ('caller cancelled gRPC',)
    assert grpc_double.calls[0].cancel_calls == 1
    assert grpc_double.channels[0].close_calls == 1
    assert get_breaker('grpc', 'service.test', 50051).failures == 0


async def test_cleanup_error_cannot_replace_body_cancellation(
    grpc_double: GrpcDouble,
) -> None:
    """A failing close loses to the exact already-captured cancellation."""
    entered = asyncio.Event()
    observed: list[asyncio.CancelledError] = []

    async def blocked() -> bytes:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            observed.append(error)
            raise

    grpc_double.script(blocked)
    grpc_double.close_error = RuntimeError('close must lose')
    task = asyncio.create_task(request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(),
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel('body cancellation wins')

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert caught.value is observed[0]
    assert caught.value.args == ('body cancellation wins',)
    assert grpc_double.channels[0].close_calls == 1


async def test_cancellation_before_call_object_still_closes_channel_exactly(
    grpc_double: GrpcDouble,
) -> None:
    """A synchronous invocation cancellation has no call object to cancel."""
    failure = asyncio.CancelledError('cancelled during invocation')
    grpc_double.invoke_error = failure

    with pytest.raises(asyncio.CancelledError) as caught:
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(),
        )

    assert caught.value is failure
    assert grpc_double.calls == []
    assert grpc_double.channels[0].close_calls == 1


async def test_rpc_cancel_hook_failure_cannot_replace_parent_cancellation(
    grpc_double: GrpcDouble,
) -> None:
    """Even a hostile synchronous cancel hook loses to task cancellation."""
    entered = asyncio.Event()
    observed: list[asyncio.CancelledError] = []

    async def blocked() -> bytes:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            observed.append(error)
            raise

    def failing_cancel() -> bool:
        raise RuntimeError('cancel hook failure')

    grpc_double.script(blocked)
    task = asyncio.create_task(request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(),
    ))
    await asyncio.wait_for(entered.wait(), timeout=1)
    grpc_double.calls[0].cancel = failing_cancel  # type: ignore[method-assign]
    task.cancel('parent cancellation')

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert caught.value is observed[0]
    assert caught.value.args == ('parent cancellation',)
    assert grpc_double.channels[0].close_calls == 1


@pytest.mark.parametrize('close_shape', ['success', 'error'])
async def test_cancellation_during_close_drains_cleanup_without_leak(
    grpc_double: GrpcDouble,
    close_shape: str,
) -> None:
    """A parent cancel waits for the shielded channel close task."""
    grpc_double.close_started = asyncio.Event()
    grpc_double.close_release = asyncio.Event()
    if close_shape == 'error':
        grpc_double.close_error = RuntimeError('close error must lose')
    task = asyncio.create_task(request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(),
    ))
    await asyncio.wait_for(grpc_double.close_started.wait(), timeout=1)
    task.cancel('cancel during close')
    for _ in range(3):
        await asyncio.sleep(0)
    remained_pending = not task.done()
    grpc_double.close_release.set()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    close_task = grpc_double.close_tasks[0]
    assert remained_pending is True
    assert caught.value.args == ('cancel during close',)
    assert close_task is not task
    assert close_task.done() is True
    assert close_task.cancelled() is False
    assert close_task not in asyncio.all_tasks()
    assert grpc_double.channels[0].close_calls == 1


async def test_repeated_cancellation_during_close_keeps_first_request(
    grpc_double: GrpcDouble,
) -> None:
    """Later parent cancellations do not replace the first provenance."""
    grpc_double.close_started = asyncio.Event()
    grpc_double.close_release = asyncio.Event()
    task = asyncio.create_task(request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(),
    ))
    await asyncio.wait_for(grpc_double.close_started.wait(), timeout=1)
    assert task.cancel('first close cancellation') is True
    for _ in range(2):
        await asyncio.sleep(0)
    assert task.cancel('second close cancellation') is True
    for _ in range(2):
        await asyncio.sleep(0)
    grpc_double.close_release.set()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert caught.value.args == ('first close cancellation',)
    assert grpc_double.channels[0].close_calls == 1


@pytest.mark.parametrize(
    'location',
    ['factory', 'unary', 'invoke', 'await', 'initial', 'trailing'],
)
async def test_direct_channel_and_call_failures_close_when_channel_exists(
    grpc_double: GrpcDouble,
    location: str,
) -> None:
    """Programming failures propagate exactly and clean any created channel."""
    failure = RuntimeError(f'{location} failure')
    if location == 'factory':
        grpc_double.factory_error = failure
    elif location == 'unary':
        grpc_double.unary_error = failure
    elif location == 'invoke':
        grpc_double.invoke_error = failure
    elif location == 'await':
        grpc_double.script(failure)
    elif location == 'initial':
        grpc_double.script(CallScript(
            b'response', initial_metadata=failure))
    else:
        grpc_double.script(CallScript(
            b'response', trailing_metadata=failure))

    with pytest.raises(RuntimeError) as caught:
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(),
        )

    assert caught.value is failure
    if location == 'factory':
        assert grpc_double.channels == []
    else:
        assert grpc_double.channels[0].close_calls == 1


async def test_direct_cleanup_failure_wins_over_success_exactly(
    grpc_double: GrpcDouble,
) -> None:
    """A channel-close programming error is not hidden in an envelope."""
    failure = RuntimeError('direct close failure')
    cause = ValueError('close cause')
    context = LookupError('close context')
    failure.__cause__ = cause
    failure.__context__ = context
    grpc_double.close_error = failure

    with pytest.raises(RuntimeError) as caught:
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(),
        )

    assert caught.value is failure
    assert failure.__cause__ is cause
    assert failure.__context__ is context
    assert grpc_double.channels[0].close_calls == 1


async def test_direct_cleanup_cancellation_propagates_exactly(
    grpc_double: GrpcDouble,
) -> None:
    """A cleanup task turns its own CancelledError into an exact outcome."""
    failure = asyncio.CancelledError('direct close cancellation')
    cause = ValueError('close cancellation cause')
    failure.__cause__ = cause
    grpc_double.close_error = failure

    with pytest.raises(asyncio.CancelledError) as caught:
        await request(
            'grpc://service.test:50051',
            data=b'request',
            protocol='GRPC',
            protocol_info=grpc_info(),
        )

    assert caught.value is failure
    assert failure.args == ('direct close cancellation',)
    assert failure.__cause__ is cause
    assert grpc_double.close_tasks[0].cancelled() is False


@pytest.mark.parametrize(
    'failure, expected_code',
    [
        pytest.param(
            asyncio.TimeoutError('factory timeout'),
            'TIMEOUT',
            id='timeout',
        ),
        pytest.param(ssl.SSLError('factory tls'), 'TLS', id='tls'),
        pytest.param(OSError('factory connect'), 'CONNECT', id='connect'),
    ],
)
async def test_channel_factory_transport_failures_are_safely_mapped(
    grpc_double: GrpcDouble,
    failure: BaseException,
    expected_code: str,
) -> None:
    """Known channel-opening transport families use stable public errors."""
    grpc_double.factory_error = failure

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(),
    )

    assert result['error']['code'] == expected_code
    assert str(failure) not in repr(result)
    assert grpc_double.channels == []


@pytest.mark.parametrize(
    'failure, expected_code',
    [
        pytest.param(
            SerializationError('typed attempt failure'),
            'SERIALIZATION',
            id='typed',
        ),
        pytest.param(
            aiohttp.ClientError('generic retryable transport'),
            'TRANSPORT',
            id='generic-transport',
        ),
    ],
)
async def test_retry_exhaustion_maps_typed_and_generic_failures(
    grpc_double: GrpcDouble,
    failure: BaseException,
    expected_code: str,
) -> None:
    """Defensive breaker causes retain typed errors or map transport safely."""
    grpc_double.script(failure)

    result = await request(
        'grpc://service.test:50051',
        data=b'request',
        protocol='GRPC',
        protocol_info=grpc_info(),
    )

    assert result['error']['code'] == expected_code
    assert grpc_double.channels[0].close_calls == 1


class LocalGrpcServer:
    """A running generic-bytes grpc.aio server without generated stubs."""

    def __init__(self, server: Any, port: int) -> None:
        """Retain the server and its allocated loopback port."""
        self.server = server
        self.port = port
        self.calls: list[str] = []

    @property
    def url(self) -> str:
        """Return the explicit plaintext loopback URL."""
        return f'grpc://127.0.0.1:{self.port}'


@pytest.fixture()
async def local_grpc_server() -> Any:
    """Start one local generic unary server and stop it after the test."""
    server = grpc.aio.server()
    holder: dict[str, LocalGrpcServer] = {}

    async def echo(request_body: bytes, context: Any) -> bytes:
        holder['value'].calls.append('Method')
        await context.send_initial_metadata((('peer', 'initial'),))
        context.set_trailing_metadata((('tail', 'done'),))
        return b'echo:' + request_body

    async def oversized(request_body: bytes, context: Any) -> bytes:
        holder['value'].calls.append('Oversized')
        return b'x' * 4096

    async def denied(request_body: bytes, context: Any) -> bytes:
        holder['value'].calls.append('Denied')
        await context.send_initial_metadata((('peer', 'denied'),))
        context.set_trailing_metadata((('tail', 'refused'),))
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, 'no access')
        raise AssertionError('context.abort must not return')

    handlers = {
        'Method': grpc.unary_unary_rpc_method_handler(
            echo,
            request_deserializer=lambda value: value,
            response_serializer=lambda value: value,
        ),
        'Oversized': grpc.unary_unary_rpc_method_handler(
            oversized,
            request_deserializer=lambda value: value,
            response_serializer=lambda value: value,
        ),
        'Denied': grpc.unary_unary_rpc_method_handler(
            denied,
            request_deserializer=lambda value: value,
            response_serializer=lambda value: value,
        ),
    }
    server.add_generic_rpc_handlers((
        grpc.method_handlers_generic_handler('package.Service', handlers),
    ))
    port = server.add_insecure_port('127.0.0.1:0')
    local = LocalGrpcServer(server, port)
    holder['value'] = local
    await server.start()
    try:
        yield local
    finally:
        await server.stop(None)


async def test_real_generic_bytes_server_round_trip(
    local_grpc_server: LocalGrpcServer,
) -> None:
    """The adapter interoperates with grpcio without protobuf or stubs."""
    result = await request(
        local_grpc_server.url,
        data=b'live',
        protocol='GRPC',
        protocol_info=grpc_info(),
    )

    assert result['ok'] is True
    assert result['text'] == 'ZWNobzpsaXZl'
    assert result['protocol_details']['initial_metadata'] == [{
        'key': 'peer', 'value': 'initial', 'encoding': None}]
    assert result['protocol_details']['trailing_metadata'] == [{
        'key': 'tail', 'value': 'done', 'encoding': None}]
    assert local_grpc_server.calls == ['Method']


async def test_real_generic_server_status_and_metadata(
    local_grpc_server: LocalGrpcServer,
) -> None:
    """A real AioRpcError follows the same normalized failure path."""
    result = await request(
        local_grpc_server.url,
        data=b'live',
        protocol='GRPC',
        protocol_info=grpc_info(method='/package.Service/Denied'),
    )

    assert result['ok'] is False
    assert result['status_code'] == 403
    assert result['error']['code'] == 'GRPC_STATUS'
    assert result['protocol_details']['grpc_status'] == 'PERMISSION_DENIED'
    assert result['protocol_details']['initial_metadata'] == [{
        'key': 'peer', 'value': 'denied', 'encoding': None}]
    assert result['protocol_details']['trailing_metadata'] == [{
        'key': 'tail', 'value': 'refused', 'encoding': None}]
    assert local_grpc_server.calls == ['Denied']


async def test_real_receive_ceiling_is_uncounted_resource_exhausted(
    local_grpc_server: LocalGrpcServer,
) -> None:
    """The grpcio receive-limit status aborts without replay or counting."""
    result = await request(
        local_grpc_server.url,
        data=b'live',
        protocol='GRPC',
        protocol_info=grpc_info(
            method='/package.Service/Oversized',
            max_response_bytes=16,
            circuit_breaker_config=retrying_config(2),
        ),
    )

    assert result['ok'] is False
    assert result['status_code'] == 429
    assert result['protocol_details']['grpc_status'] == 'RESOURCE_EXHAUSTED'
    assert local_grpc_server.calls == ['Oversized']
    assert get_breaker(
        'grpc', '127.0.0.1', local_grpc_server.port).failures == 0

"""Boundary-contract tests for the bounded Google Cloud Storage selector.

These tests stop at public configuration and constructor validation. They do
not construct credentials, contact Google Cloud, touch local paths, or execute
an object operation.
"""

import asyncio
import concurrent.futures
import importlib.util
import inspect
import itertools
import logging
import math
import socket
import ssl
import sys
import threading
import traceback
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

from google import auth as google_auth
from google.auth import exceptions as google_auth_exceptions
from google.cloud import storage

import pytest

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.helpers.internal import base
from asyncio_gateway.helpers.internal.circuit_breaker_helper import (
    AbortableServiceError,
)
from asyncio_gateway.logic import gcs_client
from asyncio_gateway.logic.gcs_client import GcsRequest
from asyncio_gateway.utils.constants import (
    HTTP_TIMEOUT,
    MAX_RESPONSE_BYTES,
    UNKNOWN_PORT,
)
from asyncio_gateway.utils.envelope import new_envelope
from asyncio_gateway.utils.exceptions import (
    ConfigurationError,
    ConnectError,
    DnsError,
    GatewayTimeoutError,
    GcsCapacityError,
    TlsError,
    TransportError,
)
from asyncio_gateway.utils.paths import stream_to_path


VALID_SIGNING_ACCOUNT = (
    'signer@example-project.iam.gserviceaccount.com')

OPTION_VALUES: dict[str, Any] = {
    'circuit_breaker_config': {},
    'content_type': 'application/octet-stream',
    'expires_in_seconds': 60,
    'if_generation_match': 7,
    'local_path': '/tmp/gcs-boundary',
    'max_items': 12,
    'max_response_bytes': 13,
    'max_upload_bytes': 14,
    'method': 'GET',
    'page_token': ' opaque-page-token ',
    'redact_query_params': {'signature'},
    'signing_service_account': VALID_SIGNING_ACCOUNT,
    'timeout': 2.5,
}

COMMAND_CASES: tuple[tuple[str, dict[str, Any], frozenset[str]], ...] = (
    (
        'download',
        {
            'command': 'download',
            'local_path': OPTION_VALUES['local_path'],
            'max_response_bytes': OPTION_VALUES['max_response_bytes'],
            'if_generation_match': OPTION_VALUES['if_generation_match'],
            'timeout': OPTION_VALUES['timeout'],
            'circuit_breaker_config': {},
            'redact_query_params': OPTION_VALUES['redact_query_params'],
        },
        frozenset({
            'command', 'local_path', 'max_response_bytes',
            'if_generation_match', 'timeout', 'circuit_breaker_config',
            'redact_query_params',
        }),
    ),
    (
        'upload',
        {
            'command': 'upload',
            'local_path': OPTION_VALUES['local_path'],
            'max_upload_bytes': OPTION_VALUES['max_upload_bytes'],
            'if_generation_match': OPTION_VALUES['if_generation_match'],
            'timeout': OPTION_VALUES['timeout'],
            'circuit_breaker_config': {},
            'redact_query_params': OPTION_VALUES['redact_query_params'],
        },
        frozenset({
            'command', 'local_path', 'max_upload_bytes',
            'if_generation_match', 'timeout', 'circuit_breaker_config',
            'redact_query_params',
        }),
    ),
    (
        'head',
        {
            'command': 'head',
            'if_generation_match': OPTION_VALUES['if_generation_match'],
            'timeout': OPTION_VALUES['timeout'],
            'circuit_breaker_config': {},
            'redact_query_params': OPTION_VALUES['redact_query_params'],
        },
        frozenset({
            'command', 'if_generation_match', 'timeout',
            'circuit_breaker_config', 'redact_query_params',
        }),
    ),
    (
        'list',
        {
            'command': 'list',
            'max_items': OPTION_VALUES['max_items'],
            'page_token': OPTION_VALUES['page_token'],
            'timeout': OPTION_VALUES['timeout'],
            'circuit_breaker_config': {},
            'redact_query_params': OPTION_VALUES['redact_query_params'],
        },
        frozenset({
            'command', 'max_items', 'page_token', 'timeout',
            'circuit_breaker_config', 'redact_query_params',
        }),
    ),
    (
        'signed-get',
        {
            'command': 'signed_url',
            'method': 'GET',
            'expires_in_seconds': OPTION_VALUES['expires_in_seconds'],
            'signing_service_account': VALID_SIGNING_ACCOUNT,
            'timeout': OPTION_VALUES['timeout'],
            'redact_query_params': OPTION_VALUES['redact_query_params'],
        },
        frozenset({
            'command', 'method', 'expires_in_seconds',
            'signing_service_account', 'timeout', 'redact_query_params',
        }),
    ),
    (
        'signed-put',
        {
            'command': 'signed_url',
            'method': 'PUT',
            'content_type': OPTION_VALUES['content_type'],
            'max_upload_bytes': OPTION_VALUES['max_upload_bytes'],
            'expires_in_seconds': OPTION_VALUES['expires_in_seconds'],
            'signing_service_account': VALID_SIGNING_ACCOUNT,
            'if_generation_match': OPTION_VALUES['if_generation_match'],
            'timeout': OPTION_VALUES['timeout'],
            'redact_query_params': OPTION_VALUES['redact_query_params'],
        },
        frozenset({
            'command', 'method', 'content_type', 'max_upload_bytes',
            'expires_in_seconds', 'signing_service_account',
            'if_generation_match', 'timeout', 'redact_query_params',
        }),
    ),
)

ALL_OPTION_KEYS = frozenset({
    'command',
    *OPTION_VALUES,
})


def _validate(info: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one caller mapping through the public strategy hook.

    Args:
        info: Caller-owned protocol options.

    Returns:
        A fresh normalized option mapping.

    Raises:
        ConfigurationError: If the mapping violates the frozen contract.
    """
    return GcsRequest.validate_protocol_info(info, protocol='GCS')


def _response(url: str = 'gs://bucket/key') -> dict[str, Any]:
    """Build the shared envelope used for direct constructor tests.

    Args:
        url: GCS target to seed.

    Returns:
        A new shared gateway envelope.
    """
    return new_envelope(
        url=url,
        protocol='GCS',
        payload={},
    )


@pytest.mark.parametrize(
    ('case_name', 'info', 'expected_keys'),
    COMMAND_CASES,
    ids=[case[0] for case in COMMAND_CASES],
)
def test_each_command_accepts_only_its_exact_option_inventory(
    case_name: str,
    info: dict[str, Any],
    expected_keys: frozenset[str],
) -> None:
    """Every valid row is copied and every mis-scoped key is refused.

    Args:
        case_name: Human-readable command/method row.
        info: Complete valid row, including all optional fields.
        expected_keys: Exact accepted option names for that row.

    Returns:
        None.
    """
    validated = _validate(info)

    assert validated is not info
    assert set(validated) == set(expected_keys), case_name
    for misplaced in sorted(ALL_OPTION_KEYS - expected_keys):
        hostile = dict(info)
        hostile[misplaced] = OPTION_VALUES[misplaced]
        with pytest.raises(ConfigurationError):
            _validate(hostile)


def test_defaults_are_command_specific_and_caller_mapping_is_unchanged(
) -> None:
    """Validation adds only frozen defaults to a new mapping."""
    supplied = {
        'command': '  UpLoAd  ',
        'local_path': '/tmp/upload.bin',
    }

    validated = _validate(supplied)

    assert supplied == {
        'command': '  UpLoAd  ',
        'local_path': '/tmp/upload.bin',
    }
    assert validated is not supplied
    assert validated == {
        'command': 'upload',
        'local_path': '/tmp/upload.bin',
        'timeout': HTTP_TIMEOUT,
        'max_upload_bytes': MAX_RESPONSE_BYTES,
        'if_generation_match': 0,
    }


@pytest.mark.parametrize(
    ('info', 'expected'),
    [
        (
            {'command': 'download', 'local_path': '/tmp/download.bin'},
            {
                'command': 'download',
                'local_path': '/tmp/download.bin',
                'timeout': HTTP_TIMEOUT,
                'max_response_bytes': MAX_RESPONSE_BYTES,
            },
        ),
        (
            {'command': 'head'},
            {'command': 'head', 'timeout': HTTP_TIMEOUT},
        ),
        (
            {'command': 'list'},
            {
                'command': 'list',
                'timeout': HTTP_TIMEOUT,
                'max_items': 1000,
            },
        ),
        (
            {'command': 'signed_url', 'method': ' get '},
            {
                'command': 'signed_url',
                'method': 'GET',
                'timeout': HTTP_TIMEOUT,
                'expires_in_seconds': 900,
            },
        ),
        (
            {
                'command': 'signed_url',
                'method': 'put',
                'content_type': 'text/plain',
                'max_upload_bytes': 1,
            },
            {
                'command': 'signed_url',
                'method': 'PUT',
                'content_type': 'text/plain',
                'max_upload_bytes': 1,
                'timeout': HTTP_TIMEOUT,
                'expires_in_seconds': 900,
                'if_generation_match': 0,
            },
        ),
    ],
    ids=['download', 'head', 'list', 'signed-get', 'signed-put'],
)
def test_each_minimum_command_receives_only_its_defaults(
    info: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    """Each minimum valid command expands to the exact frozen defaults.

    Args:
        info: Minimum caller mapping.
        expected: Exact normalized mapping.

    Returns:
        None.
    """
    assert _validate(info) == expected


@pytest.mark.parametrize(
    'command',
    [None, True, 1, '', '   ', 'delete'],
    ids=['none', 'bool', 'int', 'empty', 'whitespace', 'unknown'],
)
def test_command_must_be_nonempty_supported_text(command: object) -> None:
    """Non-string, blank, and unknown commands fail at the boundary.

    Args:
        command: Hostile command value.

    Returns:
        None.
    """
    with pytest.raises(ConfigurationError):
        _validate({'command': command})


@pytest.mark.parametrize(
    ('info', 'field'),
    [
        ({'command': 'download'}, 'local_path'),
        ({'command': 'upload'}, 'local_path'),
        ({'command': 'download', 'local_path': ''}, 'local_path'),
        ({'command': 'upload', 'local_path': 3}, 'local_path'),
        ({'command': 'signed_url', 'method': 'GET',
          'content_type': 'text/plain'}, 'content_type'),
        ({'command': 'signed_url', 'method': 'PUT',
          'max_upload_bytes': 1}, 'content_type'),
        ({'command': 'signed_url', 'method': 'PUT',
          'content_type': 'text/plain'}, 'max_upload_bytes'),
    ],
)
def test_required_command_fields_are_present_and_nonempty(
    info: dict[str, Any],
    field: str,
) -> None:
    """Required path and signed PUT fields cannot be omitted.

    Args:
        info: Incomplete or malformed command mapping.
        field: Expected failing field.

    Returns:
        None.
    """
    with pytest.raises(ConfigurationError, match=field):
        _validate(info)


@pytest.mark.parametrize(
    ('field', 'info', 'hostile'),
    [
        ('timeout', {'command': 'head'}, value)
        for value in (0, -1, math.inf, -math.inf, math.nan, True, '1', [])
    ] + [
        (
            'if_generation_match',
            {'command': 'head'},
            value,
        )
        for value in (-1, 1.0, True, '0', [])
    ] + [
        (
            'max_response_bytes',
            {'command': 'download', 'local_path': '/tmp/download.bin'},
            value,
        )
        for value in (0, -1, 1.0, True, '1', [])
    ] + [
        (
            'max_upload_bytes',
            {'command': 'upload', 'local_path': '/tmp/upload.bin'},
            value,
        )
        for value in (0, -1, 1.0, True, '1', [])
    ] + [
        ('max_items', {'command': 'list'}, value)
        for value in (0, -1, 1001, 1.0, True, '1', [])
    ] + [
        (
            'expires_in_seconds',
            {'command': 'signed_url', 'method': 'GET'},
            value,
        )
        for value in (0, -1, 3601, 1.0, True, '1', [])
    ],
)
def test_numeric_options_reject_nonfinite_bool_and_out_of_range_values(
    field: str,
    info: dict[str, Any],
    hostile: object,
) -> None:
    """Every numeric boundary rejects bool and malformed values.

    Args:
        field: Option under test.
        info: Otherwise-valid command mapping.
        hostile: Invalid value.

    Returns:
        None.
    """
    info[field] = hostile
    with pytest.raises(ConfigurationError, match=field):
        _validate(info)


@pytest.mark.parametrize(
    ('field', 'info', 'accepted'),
    [
        ('timeout', {'command': 'head'}, 0.25),
        ('if_generation_match', {'command': 'head'}, 0),
        ('max_response_bytes', {
            'command': 'download', 'local_path': '/tmp/download.bin'}, 1),
        ('max_upload_bytes', {
            'command': 'upload', 'local_path': '/tmp/upload.bin'}, 1),
        ('max_items', {'command': 'list'}, 1),
        ('max_items', {'command': 'list'}, 1000),
        ('expires_in_seconds', {
            'command': 'signed_url', 'method': 'GET'}, 1),
        ('expires_in_seconds', {
            'command': 'signed_url', 'method': 'GET'}, 3600),
    ],
)
def test_numeric_inclusive_boundaries_are_preserved(
    field: str,
    info: dict[str, Any],
    accepted: int | float,
) -> None:
    """Valid numeric edges survive validation unchanged.

    Args:
        field: Option under test.
        info: Otherwise-valid command mapping.
        accepted: Boundary value.

    Returns:
        None.
    """
    info[field] = accepted
    assert _validate(info)[field] == accepted


@pytest.mark.parametrize(
    'token',
    [' ' * 4096, 'é' * 2048, ' \t\n '],
    ids=['ascii-4096-bytes', 'multibyte-4096-bytes', 'whitespace'],
)
def test_page_token_is_opaque_and_preserved_exactly(token: str) -> None:
    """Valid page tokens are copied byte-for-byte without normalization.

    Args:
        token: Exact 4096-byte or whitespace-only token.

    Returns:
        None.
    """
    supplied = {'command': 'list', 'page_token': token}

    validated = _validate(supplied)

    assert validated is not supplied
    assert validated['page_token'] == token
    assert validated['page_token'].encode('utf-8') == token.encode('utf-8')
    assert supplied['page_token'] == token


@pytest.mark.parametrize(
    'token',
    ['', 'a' * 4097, '\ud800', 7, True],
    ids=['empty', '4097-bytes', 'encoding-failure', 'int', 'bool'],
)
def test_page_token_refusals_never_echo_the_token(token: object) -> None:
    """Invalid tokens fail before dispatch without entering diagnostics.

    Args:
        token: Empty, oversized, unencodable, or non-string token.

    Returns:
        None.
    """
    with pytest.raises(ConfigurationError) as raised:
        _validate({'command': 'list', 'page_token': token})

    if isinstance(token, str) and token:
        assert token not in str(raised.value)


@pytest.mark.parametrize(
    ('method', 'normalized'),
    [('GET', 'GET'), (' get ', 'GET'), ('PUT', 'PUT'), ('put', 'PUT')],
)
def test_signed_methods_are_case_insensitive_and_normalized(
    method: str,
    normalized: str,
) -> None:
    """Only signed GET and PUT normalize to their uppercase spelling.

    Args:
        method: Caller spelling.
        normalized: Frozen normalized spelling.

    Returns:
        None.
    """
    info: dict[str, Any] = {
        'command': 'signed_url',
        'method': method,
    }
    if normalized == 'PUT':
        info.update({
            'content_type': 'text/plain',
            'max_upload_bytes': 1,
        })
    assert _validate(info)['method'] == normalized


@pytest.mark.parametrize(
    'method',
    [None, True, 1, '', 'POST', 'DELETE', 'HEAD'],
)
def test_signed_method_refuses_every_value_except_get_and_put(
    method: object,
) -> None:
    """Unsupported and non-string signed methods fail closed.

    Args:
        method: Hostile method value.

    Returns:
        None.
    """
    with pytest.raises(ConfigurationError, match='method'):
        _validate({'command': 'signed_url', 'method': method})


@pytest.mark.parametrize(
    'content_type',
    ['application/octet-stream', 'text/plain;charset=utf-8'],
)
def test_signed_put_accepts_constrained_content_types(
    content_type: str,
) -> None:
    """Visible-ASCII type/subtype values pass unchanged.

    Args:
        content_type: Valid media type.

    Returns:
        None.
    """
    validated = _validate({
        'command': 'signed_url',
        'method': 'PUT',
        'content_type': content_type,
        'max_upload_bytes': 1,
    })
    assert validated['content_type'] == content_type


@pytest.mark.parametrize(
    'content_type',
    [
        '', 'text', '/plain', 'text/', 'text/plain/more', 'text /plain',
        'text/plain; charset=utf-8', 'text/plain\r\nheader:x', 'text/pläin',
        'a' * 256, 7, True,
    ],
)
def test_signed_put_refuses_ambiguous_or_unsafe_content_types(
    content_type: object,
) -> None:
    """Malformed media types cannot become signed request metadata.

    Args:
        content_type: Invalid media type value.

    Returns:
        None.
    """
    with pytest.raises(ConfigurationError, match='content_type'):
        _validate({
            'command': 'signed_url',
            'method': 'PUT',
            'content_type': content_type,
            'max_upload_bytes': 1,
        })


@pytest.mark.parametrize(
    'account',
    [
        'signer@example-project.iam.gserviceaccount.com',
        'service-account@project-name.iam.gserviceaccount.com',
    ],
)
def test_signing_service_account_accepts_only_the_frozen_pattern(
    account: str,
) -> None:
    """A syntactically valid impersonation target passes unchanged.

    Args:
        account: Valid service-account address.

    Returns:
        None.
    """
    validated = _validate({
        'command': 'signed_url',
        'method': 'GET',
        'signing_service_account': account,
    })
    assert validated['signing_service_account'] == account


@pytest.mark.parametrize(
    'account',
    [
        '', 'short@p.iam.gserviceaccount.com',
        'Signer@example-project.iam.gserviceaccount.com',
        'signer@example_project.iam.gserviceaccount.com',
        'signer@example-project.iam.gserviceaccount.com.evil',
        ' signer@example-project.iam.gserviceaccount.com', 7, True,
    ],
)
def test_signing_service_account_refuses_every_other_shape(
    account: object,
) -> None:
    """Invalid impersonation targets fail at the public boundary.

    Args:
        account: Invalid service-account address.

    Returns:
        None.
    """
    with pytest.raises(ConfigurationError, match='signing_service_account'):
        _validate({
            'command': 'signed_url',
            'method': 'GET',
            'signing_service_account': account,
        })


@pytest.mark.parametrize(
    ('url', 'command', 'expected_bucket', 'expected_key'),
    [
        ('gs://bucket', 'list', 'bucket', ''),
        ('gs://bucket/', 'list', 'bucket', ''),
        ('gs://bucket/prefix', 'list', 'bucket', 'prefix'),
        ('gs://bucket/a%2Fb', 'head', 'bucket', 'a%2Fb'),
        ('gs://BUCKET/key', 'head', 'bucket', 'key'),
    ],
)
def test_valid_targets_preserve_object_text_and_normalize_bucket(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    command: str,
    expected_bucket: str,
    expected_key: str,
) -> None:
    """List-only empty keys and opaque object text parse before breaker use.

    Args:
        monkeypatch: Test patcher.
        url: Valid GCS target.
        command: Operation whose object rule applies.
        expected_bucket: Normalized bucket.
        expected_key: Preserved key or prefix.

    Returns:
        None.
    """
    calls: list[tuple[object, ...]] = []
    breaker = object()

    def record_breaker(*args: object, **kwargs: object) -> object:
        """Record the destination used by the base constructor."""
        calls.append((*args, kwargs))
        return breaker

    monkeypatch.setattr(base, 'get_breaker', record_breaker)
    info = _validate({'command': command})

    built = GcsRequest(
        url,
        None,
        _response(url),
        info=info,
        redact_params=frozenset(),
    )

    assert (built.bucket, built.key) == (expected_bucket, expected_key)
    assert built.circuit_breaker is breaker
    assert calls == [('gs', expected_bucket, UNKNOWN_PORT, {}, {})]


@pytest.mark.parametrize(
    ('url', 'command'),
    [
        ('gs:///key', 'head'),
        ('gs://user@bucket/key', 'head'),
        ('gs://user:password@bucket/key', 'head'),
        ('gs://bucket:443/key', 'head'),
        ('gs://bucket:not-a-port/key', 'head'),
        ('gs://bucket/key?generation=1', 'head'),
        ('gs://bucket/key#fragment', 'head'),
        ('gs://bucket', 'head'),
        ('gs://bucket/', 'download'),
    ],
)
def test_invalid_target_shape_fails_before_base_breaker_lookup(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    command: str,
) -> None:
    """Remaining target validation precedes the base constructor.

    Args:
        monkeypatch: Test patcher.
        url: Invalid post-scheme-guard target.
        command: Operation whose object rule applies.

    Returns:
        None.
    """
    def unexpected_breaker(*args: object, **kwargs: object) -> NoReturn:
        """Fail if invalid input reaches the base constructor."""
        raise AssertionError('breaker lookup must not run')

    monkeypatch.setattr(base, 'get_breaker', unexpected_breaker)
    info: dict[str, Any] = {'command': command}
    if command == 'download':
        info['local_path'] = '/tmp/download.bin'

    with pytest.raises(ConfigurationError):
        GcsRequest(
            url,
            None,
            _response(url),
            info=_validate(info),
            redact_params=frozenset(),
        )


@pytest.mark.parametrize('auth', [object(), {}, '', False, 0])
def test_auth_must_be_exactly_none_before_base_breaker_lookup(
    monkeypatch: pytest.MonkeyPatch,
    auth: object,
) -> None:
    """Every explicit auth shape is rejected before breaker lookup.

    Args:
        monkeypatch: Test patcher.
        auth: Forbidden caller authentication data.

    Returns:
        None.
    """
    def unexpected_breaker(*args: object, **kwargs: object) -> NoReturn:
        """Fail if invalid auth reaches the base constructor."""
        raise AssertionError('breaker lookup must not run')

    monkeypatch.setattr(base, 'get_breaker', unexpected_breaker)

    with pytest.raises(ConfigurationError, match='auth'):
        GcsRequest(
            'gs://bucket/key',
            auth,
            _response(),
            info=_validate({'command': 'head'}),
            redact_params=frozenset(),
        )


def test_breaker_identity_is_bucket_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blob names never enter the normalized breaker destination."""
    calls: list[tuple[object, ...]] = []

    def record_breaker(*args: object, **kwargs: object) -> object:
        """Capture each normalized breaker lookup."""
        calls.append((*args, kwargs))
        return object()

    monkeypatch.setattr(base, 'get_breaker', record_breaker)
    info = _validate({'command': 'head'})

    for url in (
        'gs://bucket/first',
        'gs://bucket/second',
        'gs://other/first',
    ):
        GcsRequest(
            url,
            None,
            _response(url),
            info=info,
            redact_params=frozenset(),
        )

    assert calls == [
        ('gs', 'bucket', UNKNOWN_PORT, {}, {}),
        ('gs', 'bucket', UNKNOWN_PORT, {}, {}),
        ('gs', 'other', UNKNOWN_PORT, {}, {}),
    ]


async def test_foreign_scheme_is_rejected_after_preprocessor(
) -> None:
    """The existing scheme guard remains after caller preprocessing."""
    processor_calls: list[str] = []

    async def preprocessor(response: Mapping[str, Any]) -> str:
        """Record that preprocessing ran before scheme validation."""
        processor_calls.append(response['protocol'])
        return 'processed'

    with pytest.raises(ConfigurationError, match='dispatches only'):
        await request(
            'https://bucket/key',
            protocol='GCS',
            protocol_info={'command': 'head'},
            pre_processor_config={'function': preprocessor},
        )

    assert processor_calls == ['GCS']


async def test_strict_options_fail_before_preprocessor(
) -> None:
    """Strict protocol options are rejected before an envelope or callback."""
    processor_calls: list[str] = []

    async def preprocessor(response: Mapping[str, Any]) -> str:
        """Record an unexpected preprocessor invocation."""
        processor_calls.append(response['protocol'])
        return 'processed'

    with pytest.raises(ConfigurationError):
        await request(
            'gs://bucket/key',
            protocol='GCS',
            protocol_info={'command': 'head', 'page_token': 'mis-scoped'},
            pre_processor_config={'function': preprocessor},
        )

    assert processor_calls == []


# --- GCS-04: private provider lifecycle and capacity foundation -----------


async def _release_lease(lease: Any) -> None:
    """Release a private test lease whether its seam is sync or async."""
    outcome = lease.release()
    if inspect.isawaitable(outcome):
        await outcome


async def _wait_for_thread_event(event: threading.Event) -> None:
    """Await a deterministic worker signal without blocking the loop."""
    assert await asyncio.to_thread(event.wait, 1)


def test_gcs_offloader_is_one_private_exactly_sized_executor() -> None:
    """The provider pool has the frozen size and identifying prefix."""
    executor = getattr(gcs_client, '_GCS_EXECUTOR')

    assert isinstance(executor, concurrent.futures.ThreadPoolExecutor)
    assert executor._max_workers == 4
    assert executor._thread_name_prefix == 'asyncio-gateway-gcs'


async def test_four_lifecycle_leases_admit_without_a_waiting_queue() -> None:
    """Four immediate acquisitions succeed and the fifth fails locally."""
    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    leases = [acquire() for _ in range(4)]
    try:
        with pytest.raises(GcsCapacityError) as raised:
            acquire()
        assert raised.value.code == 'GCS_CAPACITY'
        assert raised.value.status_code == 503
    finally:
        for lease in leases:
            await _release_lease(lease)


async def test_one_lease_never_has_two_provider_futures_outstanding() -> None:
    """A second provider call waits for the same lease's first call."""
    lease = getattr(gcs_client, '_acquire_gcs_lease')()
    first_started = threading.Event()
    first_release = threading.Event()
    second_started = threading.Event()

    def first() -> str:
        first_started.set()
        assert first_release.wait(timeout=1)
        return 'first'

    def second() -> str:
        second_started.set()
        return 'second'

    first_task = asyncio.create_task(lease.run(first, timeout=1))
    try:
        await _wait_for_thread_event(first_started)
        second_task = asyncio.create_task(lease.run(second, timeout=1))
        await asyncio.sleep(0)
        assert not second_started.is_set()
        first_release.set()
        assert await first_task == 'first'
        assert await second_task == 'second'
    finally:
        first_release.set()
        await _release_lease(lease)


@pytest.mark.parametrize(
    'seam',
    [
        'adc', 'credential-refresh', 'client', 'bucket', 'blob', 'lookup',
        'close',
    ],
)
async def test_every_provider_lifecycle_seam_runs_only_on_the_gcs_pool(
    seam: str,
) -> None:
    """Generic provider work is never executed on the loop thread."""
    lease = getattr(gcs_client, '_acquire_gcs_lease')()
    loop_thread = threading.get_ident()
    try:
        worker_thread, worker_name, observed = await lease.run(
            lambda: (threading.get_ident(), threading.current_thread().name,
                     seam),
            timeout=1,
        )
    finally:
        await _release_lease(lease)

    assert worker_thread != loop_thread
    assert worker_name.startswith('asyncio-gateway-gcs')
    assert observed == seam


async def test_four_blocked_provider_workers_do_not_starve_default_executor(
) -> None:
    """Provider saturation leaves unrelated default-executor work runnable."""
    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    leases = [acquire() for _ in range(4)]
    started = [threading.Event() for _ in range(4)]
    release = threading.Event()

    def block(marker: threading.Event) -> None:
        marker.set()
        assert release.wait(timeout=1)

    tasks = [
        asyncio.create_task(lease.run(block, marker, timeout=1))
        for lease, marker in zip(leases, started)
    ]
    try:
        for marker in started:
            await _wait_for_thread_event(marker)
        unrelated_thread = await asyncio.wait_for(
            asyncio.to_thread(threading.get_ident), timeout=0.25)
        assert unrelated_thread != threading.get_ident()
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for lease in leases:
            await _release_lease(lease)


async def test_cancellation_retains_capacity_drains_and_closes_late_resource(
) -> None:
    """Repeated cancellation cannot release or leak a late provider result."""
    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    leases = [acquire() for _ in range(4)]
    started = threading.Event()
    release = threading.Event()
    closed_on: list[int] = []

    class Resource:
        def close(self) -> None:
            closed_on.append(threading.get_ident())

    def late_resource() -> Resource:
        started.set()
        assert release.wait(timeout=1)
        return Resource()

    task = asyncio.create_task(
        leases[0].run(late_resource, timeout=1,
                      close_result=lambda resource: resource.close()))
    try:
        await _wait_for_thread_event(started)
        task.cancel('original-cancellation')
        task.cancel('repeated-cancellation')
        with pytest.raises(GcsCapacityError):
            acquire()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.args == ('original-cancellation',)
        assert closed_on and closed_on[0] != threading.get_ident()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        for lease in leases:
            await _release_lease(lease)


async def test_result_acceptance_deadline_rejects_and_closes_late_result(
) -> None:
    """A worker result produced after the finite deadline is never success."""
    lease = getattr(gcs_client, '_acquire_gcs_lease')()
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class Resource:
        def close(self) -> None:
            closed.set()

    def late_resource() -> Resource:
        started.set()
        assert release.wait(timeout=1)
        return Resource()

    task = asyncio.create_task(
        lease.run(late_resource, timeout=1e-6,
                  close_result=lambda resource: resource.close()))
    try:
        await _wait_for_thread_event(started)
        release.set()
        with pytest.raises(GatewayTimeoutError):
            await task
        assert closed.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await _release_lease(lease)


def test_operational_sdk_controls_are_retry_none_with_finite_timeout() -> None:
    """The shared adapter freezes provider retry and timeout ownership."""
    controls = getattr(gcs_client, '_operational_sdk_kwargs')(2.5)

    assert controls == {'retry': None, 'timeout': 2.5}


# --- AGW-48 tranche A: guarded upload foundation -------------------------


class _UploadBreaker:
    """Record breaker execution while invoking one supplied attempt."""

    def __init__(self, timeline: list[str]) -> None:
        """Retain the shared operation timeline."""
        self.timeline = timeline
        self.calls = 0

    async def run(self, call: Any, *args: Any, **kwargs: Any) -> Any:
        """Record and invoke one operation attempt."""
        self.calls += 1
        self.timeline.append('breaker')
        return await call(*args, **kwargs)


class _UploadBlob:
    """Synchronous blob double for one bounded upload."""

    def __init__(self, provider: '_UploadProvider') -> None:
        """Retain the provider recorder and valid upload metadata."""
        self.provider = provider
        self.etag = 'upload-etag'
        self.generation = 19
        self.metageneration = 2
        self.crc32c = 'crc32c=='

    def upload_from_string(
        self,
        data: bytes,
        *,
        if_generation_match: int,
        retry: object,
        timeout: int | float,
    ) -> None:
        """Record the exact bytes and operational SDK controls."""
        self.provider.record_thread('upload')
        self.provider.upload_calls.append((data, {
            'if_generation_match': if_generation_match,
            'retry': retry,
            'timeout': timeout,
        }))
        if self.provider.upload_started is not None:
            self.provider.upload_started.set()
        if self.provider.upload_release is not None:
            assert self.provider.upload_release.wait(timeout=1)
        if self.provider.upload_outcomes:
            outcome = self.provider.upload_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome


class _UploadBucket:
    """Bucket double returning one observable blob."""

    def __init__(self, provider: '_UploadProvider') -> None:
        """Retain the provider recorder."""
        self.provider = provider

    def blob(self, key: str) -> _UploadBlob:
        """Record the caller's exact object key."""
        self.provider.record_thread('blob')
        self.provider.blob_keys.append(key)
        return self.provider.blob


class _UploadClient:
    """Storage-client double with observable success cleanup."""

    def __init__(self, provider: '_UploadProvider') -> None:
        """Retain the provider recorder."""
        self.provider = provider
        self.closed = False
        self.close_calls = 0

    def bucket(self, name: str) -> _UploadBucket:
        """Record the caller's normalized bucket name."""
        self.provider.record_thread('bucket')
        self.provider.bucket_names.append(name)
        return self.provider.bucket

    def close(self) -> None:
        """Record deterministic client cleanup."""
        self.provider.record_thread('client-close')
        self.close_calls += 1
        if self.provider.close_started is not None:
            self.provider.close_started.set()
        if self.provider.close_release is not None:
            assert self.provider.close_release.wait(timeout=1)
        if self.provider.close_outcomes:
            outcome = self.provider.close_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
        self.closed = True


class _UploadCredentials:
    """Already-valid ADC credentials requiring no refresh."""

    def __init__(self) -> None:
        """Start valid with no scripted refresh failure."""
        self.valid = True
        self.expired = False
        self.refresh_error: BaseException | None = None

    def refresh(self, request: object) -> None:
        """Raise a scripted refresh failure or reject an unexpected call."""
        if self.refresh_error is not None:
            raise self.refresh_error
        raise AssertionError(
            f'valid credentials must not refresh: {request!r}')


class _UploadProvider:
    """ADC and storage SDK boundary recorder with no network behavior."""

    def __init__(self, timeline: list[str]) -> None:
        """Create one connected tree of deterministic provider doubles."""
        self.timeline = timeline
        self.threads: dict[str, tuple[int, str]] = {}
        self.upload_calls: list[tuple[bytes, dict[str, object]]] = []
        self.upload_outcomes: list[BaseException | None] = []
        self.upload_started: threading.Event | None = None
        self.upload_release: threading.Event | None = None
        self.close_started: threading.Event | None = None
        self.close_release: threading.Event | None = None
        self.close_outcomes: list[BaseException | None] = []
        self.client_started: threading.Event | None = None
        self.client_release: threading.Event | None = None
        self.fresh_client_per_attempt = False
        self.bucket_names: list[str] = []
        self.blob_keys: list[str] = []
        self.adc_error: BaseException | None = None
        self.credentials = _UploadCredentials()
        self.blob = _UploadBlob(self)
        self.bucket = _UploadBucket(self)
        self.client = _UploadClient(self)
        self.returned_clients: list[_UploadClient] = []

    def record_thread(self, seam: str) -> None:
        """Record one provider seam and its executing thread."""
        self.timeline.append(seam)
        self.threads[seam] = (
            threading.get_ident(), threading.current_thread().name)

    def adc(self, *args: object, **kwargs: object) -> tuple[object, str]:
        """Return valid deterministic ADC credentials and project."""
        self.record_thread('adc')
        if self.adc_error is not None:
            raise self.adc_error
        return self.credentials, 'test-project'

    def make_client(self, *args: object, **kwargs: object) -> _UploadClient:
        """Return the single observable storage client."""
        self.record_thread('client')
        if self.client_started is not None:
            self.client_started.set()
        if self.client_release is not None:
            assert self.client_release.wait(timeout=1)
        client = (
            _UploadClient(self)
            if self.fresh_client_per_attempt else self.client
        )
        self.returned_clients.append(client)
        return client

    def script_upload(self, *outcomes: BaseException | None) -> None:
        """Replace the ordered upload outcomes."""
        self.upload_outcomes = list(outcomes)

    def script_close(self, *outcomes: BaseException | None) -> None:
        """Replace the ordered client-cleanup outcomes."""
        self.close_outcomes = list(outcomes)


def _install_upload_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: _UploadProvider,
) -> None:
    """Replace ADC and GCS construction with deterministic doubles."""
    monkeypatch.setattr(google_auth, 'default', provider.adc)
    monkeypatch.setattr(storage, 'Client', provider.make_client)


async def test_upload_read_failure_precedes_adc_breaker_and_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed guarded-read failure owns a lease but touches no provider."""
    timeline: list[str] = []
    reads: list[tuple[object, int, int]] = []
    breaker = _UploadBreaker(timeline)
    provider = _UploadProvider(timeline)
    loop_thread = threading.get_ident()

    async def fail_guarded_read(
        path: object,
        *,
        max_bytes: int,
        chunk_size: int = 65536,
    ) -> NoReturn:
        """Record the held lease and raise one typed local failure."""
        del chunk_size
        timeline.append('read')
        reads.append((
            path,
            max_bytes,
            getattr(gcs_client, '_active_gcs_leases')(),
        ))
        raise ConfigurationError('guarded upload read failed')

    monkeypatch.setattr(
        gcs_client, 'read_guarded_file', fail_guarded_read, raising=False)
    monkeypatch.setattr(base, 'get_breaker', lambda *args, **kwargs: breaker)
    _install_upload_provider(monkeypatch, provider)

    result = await request(
        'gs://upload-bucket/object.bin',
        protocol='GCS',
        protocol_info={
            'command': 'upload',
            'local_path': '/caller/source.bin',
            'max_upload_bytes': 23,
        },
    )

    assert reads == [('/caller/source.bin', 23, 1)]
    assert timeline == ['read']
    assert breaker.calls == 0
    assert provider.threads == {}
    assert getattr(gcs_client, '_active_gcs_leases')() == 0
    assert threading.get_ident() == loop_thread
    assert result['ok'] is False
    assert result['error']['code'] == 'CONFIG'


@pytest.mark.parametrize(
    ('guarded_bytes', 'generation_option', 'expected_generation'),
    [
        pytest.param(b'', None, 0, id='empty-default-create-only'),
        pytest.param(b'bounded-bytes', 7, 7, id='bytes-explicit-generation'),
    ],
)
async def test_upload_replays_guarded_bytes_with_exact_sdk_contract(
    monkeypatch: pytest.MonkeyPatch,
    guarded_bytes: bytes,
    generation_option: int | None,
    expected_generation: int,
) -> None:
    """One guarded body reaches one off-loop upload and closes cleanly."""
    timeline: list[str] = []
    provider = _UploadProvider(timeline)
    breaker = _UploadBreaker(timeline)
    loop_thread = threading.get_ident()
    reads: list[tuple[object, int, int, int]] = []

    async def guarded_read(
        path: object,
        *,
        max_bytes: int,
        chunk_size: int = 65536,
    ) -> bytes:
        """Return the exact pre-read bytes while the lease is retained."""
        del chunk_size
        timeline.append('read')
        reads.append((
            path,
            max_bytes,
            getattr(gcs_client, '_active_gcs_leases')(),
            threading.get_ident(),
        ))
        return guarded_bytes

    monkeypatch.setattr(
        gcs_client, 'read_guarded_file', guarded_read, raising=False)
    monkeypatch.setattr(base, 'get_breaker', lambda *args, **kwargs: breaker)
    _install_upload_provider(monkeypatch, provider)
    info: dict[str, object] = {
        'command': 'upload',
        'local_path': '/caller/source.bin',
        'max_upload_bytes': 23,
        'timeout': 2.5,
    }
    if generation_option is not None:
        info['if_generation_match'] = generation_option

    result = await request(
        'gs://Upload-Bucket/folder/object.bin',
        protocol='GCS',
        protocol_info=info,
    )

    assert reads == [('/caller/source.bin', 23, 1, loop_thread)]
    assert timeline == [
        'read', 'breaker', 'adc', 'client', 'bucket', 'blob', 'upload',
        'client-close',
    ]
    assert provider.upload_calls == [(guarded_bytes, {
        'if_generation_match': expected_generation,
        'retry': None,
        'timeout': 2.5,
    })]
    assert provider.upload_calls[0][0] is guarded_bytes
    assert provider.bucket_names == ['upload-bucket']
    assert provider.blob_keys == ['folder/object.bin']
    assert provider.client.closed is True
    assert set(provider.threads) == {
        'adc', 'client', 'bucket', 'blob', 'upload', 'client-close',
    }
    assert all(
        thread_id != loop_thread
        and thread_name.startswith('asyncio-gateway-gcs')
        for thread_id, thread_name in provider.threads.values()
    )
    assert breaker.calls == 1
    assert getattr(gcs_client, '_active_gcs_leases')() == 0
    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['protocol_details'] == {
        'command': 'upload',
        'bucket': 'upload-bucket',
        'key': 'folder/object.bin',
        'local_path': '/caller/source.bin',
        'bytes_read': len(guarded_bytes),
        'etag': 'upload-etag',
        'generation': 19,
        'metageneration': 2,
        'crc32c': 'crc32c==',
    }


class _FakeServiceError(Exception):
    """Structural Google-like service error with hostile incidental state."""

    def __init__(
        self,
        status: object,
        *,
        code: str = 'conditionNotMet',
        message: str = 'safe service refusal',
        request_id: str = 'request-7',
    ) -> None:
        """Build one structural error without an SDK dependency."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.response = {
            'status_code': status,
            'headers': {'x-goog-request-id': request_id},
            'provider_object': object(),
        }


@pytest.mark.parametrize(
    ('status', 'abortable'),
    [(408, False), (429, False), (500, False), (502, False), (503, False),
     (504, False), (400, True), (401, True), (403, True), (404, True),
     (409, True), (412, True)],
)
def test_service_failure_classification_is_structural(
    status: int,
    abortable: bool,
) -> None:
    """Only the frozen status set is retryable and breaker-counted."""
    failure = getattr(gcs_client, '_service_failure_for')(
        _FakeServiceError(status), command='head', bucket='bucket',
        target='object')

    assert isinstance(failure, AbortableServiceError) is abortable
    assert failure.status_code == status
    assert failure.details == {
        'command': 'head',
        'bucket': 'bucket',
        'target': 'object',
        'gcs_error_code': 'conditionNotMet',
        'gcs_error_message': 'safe service refusal',
        'response_metadata': {
            'http_status_code': status,
            'request_id': 'request-7',
        },
    }


@pytest.mark.parametrize('status', [True, '503', 99, 600, None])
def test_malformed_service_status_normalizes_to_502(status: object) -> None:
    """Provider status is trusted only when it is an integer in 100..599."""
    failure = getattr(gcs_client, '_service_failure_for')(
        _FakeServiceError(status), command='list', bucket='bucket', target='')

    assert failure.status_code == 502
    assert failure.details['response_metadata'] == {
        'http_status_code': 502,
        'request_id': 'request-7',
    }


@pytest.mark.parametrize(
    ('error', 'expected'),
    [
        (socket.gaierror(), DnsError),
        (ssl.SSLError(), TlsError),
        (TimeoutError(), GatewayTimeoutError),
        (ConnectionError(), ConnectError),
        (OSError(), TransportError),
    ],
)
def test_transport_failure_mapping_preserves_existing_boundaries(
    error: BaseException,
    expected: type[TransportError],
) -> None:
    """GCS transport faults reuse the existing public vocabulary."""
    mapped = getattr(gcs_client, '_transport_error_for')(error)

    assert type(mapped) is expected


# --- AGW-48 tranche B1: retry and public failure vocabulary --------------


def _gcs_retry_config(
    retries: int,
    events: list[str],
) -> dict[str, Any]:
    """Build an exact retry budget with observable breaker callbacks."""
    return {
        'maximum_failures': 99,
        'retry_config': {
            'allowed_retries': retries,
            'delay': 0,
            'jitter': False,
            'on_failed_attempt': lambda: events.append('failed'),
            'on_retries_exhausted': lambda: events.append('exhausted'),
            'on_abort': lambda: events.append('abort'),
        },
    }


async def _public_scripted_upload(
    monkeypatch: pytest.MonkeyPatch,
    provider: _UploadProvider,
    *,
    bucket: str,
    body: bytes = b'retry-body',
    generation: int = 11,
    local_path: str = '/caller/retry-source.bin',
    timeout: int | float = 2.5,
    breaker_config: Mapping[str, Any],
) -> tuple[Mapping[str, Any], list[tuple[object, int]]]:
    """Run one public upload with a deterministic guarded read and SDK."""
    reads: list[tuple[object, int]] = []

    async def guarded_read(
        path: object,
        *,
        max_bytes: int,
        chunk_size: int = 65536,
    ) -> bytes:
        """Return one caller-independent pre-read bytes object."""
        del chunk_size
        reads.append((path, max_bytes))
        return body

    monkeypatch.setattr(gcs_client, 'read_guarded_file', guarded_read)
    _install_upload_provider(monkeypatch, provider)
    result = await request(
        f'gs://{bucket}/object.bin',
        protocol='GCS',
        protocol_info={
            'command': 'upload',
            'local_path': local_path,
            'max_upload_bytes': 41,
            'if_generation_match': generation,
            'timeout': timeout,
            'circuit_breaker_config': dict(breaker_config),
        },
    )
    return result, reads


@pytest.mark.parametrize(
    'first_failure',
    [
        pytest.param(_FakeServiceError(503), id='service'),
        pytest.param(socket.gaierror('foreign retry text'), id='transport'),
    ],
)
async def test_upload_retry_reuses_one_guarded_body_and_sdk_contract(
    monkeypatch: pytest.MonkeyPatch,
    first_failure: BaseException,
) -> None:
    """One gateway retry replays identity-stable bytes and controls."""
    events: list[str] = []
    provider = _UploadProvider([])
    provider.script_upload(first_failure, None)
    body = b'identity-stable-upload'

    result, reads = await _public_scripted_upload(
        monkeypatch,
        provider,
        bucket=f'retry-{type(first_failure).__name__.lower()}',
        body=body,
        generation=17,
        breaker_config=_gcs_retry_config(1, events),
    )

    assert result['ok'] is True
    assert reads == [('/caller/retry-source.bin', 41)]
    assert len(provider.upload_calls) == 2
    assert all(call[0] is body for call in provider.upload_calls)
    assert [call[1] for call in provider.upload_calls] == [{
        'if_generation_match': 17,
        'retry': None,
        'timeout': 2.5,
    }] * 2
    assert events == ['failed']


@pytest.mark.parametrize(
    ('status', 'retryable'),
    [
        (408, True), (429, True), (500, True), (502, True),
        (503, True), (504, True), (400, False), (401, False),
        (403, False), (404, False), (409, False), (412, False),
    ],
)
async def test_upload_service_status_has_exact_public_retry_semantics(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    retryable: bool,
) -> None:
    """Service allowlist alone decides count, retry, and public status."""
    events: list[str] = []
    provider = _UploadProvider([])
    attempt_count = 2 if retryable else 1
    provider.script_upload(*[
        _FakeServiceError(status) for _ in range(attempt_count)
    ])

    result, reads = await _public_scripted_upload(
        monkeypatch,
        provider,
        bucket=f'service-{status}',
        breaker_config=_gcs_retry_config(1, events),
    )

    assert reads == [('/caller/retry-source.bin', 41)]
    assert len(provider.upload_calls) == attempt_count
    assert result['ok'] is False
    assert result['status_code'] == status
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {
        'command': 'upload',
        'bucket': f'service-{status}',
        'target': 'object.bin',
        'gcs_error_code': 'conditionNotMet',
        'gcs_error_message': 'safe service refusal',
        'response_metadata': {
            'http_status_code': status,
            'request_id': 'request-7',
        },
    }
    assert 'provider_object' not in repr(result)
    assert events == (
        ['failed', 'failed', 'exhausted'] if retryable else ['abort'])


@pytest.mark.parametrize(
    ('failure', 'code', 'status'),
    [
        (socket.gaierror('foreign-dns-secret'), 'DNS', 502),
        (ssl.SSLError('foreign-tls-secret'), 'TLS', 502),
        (ConnectionError('foreign-connect-secret'), 'CONNECT', 502),
        (TimeoutError('foreign-timeout-secret'), 'TIMEOUT', 504),
        (OSError('foreign-transport-secret'), 'TRANSPORT', 502),
    ],
)
async def test_upload_transport_types_map_safely_and_count_once(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    code: str,
    status: int,
) -> None:
    """Provider transport prose never crosses the stable error boundary."""
    events: list[str] = []
    provider = _UploadProvider([])
    provider.script_upload(failure)

    result, _ = await _public_scripted_upload(
        monkeypatch,
        provider,
        bucket=f'transport-{code.lower()}',
        breaker_config=_gcs_retry_config(0, events),
    )

    assert result['ok'] is False
    assert result['status_code'] == status
    assert result['error']['code'] == code
    assert 'foreign-' not in repr(result)
    assert len(provider.upload_calls) == 1
    assert events == ['failed', 'exhausted']


@pytest.mark.parametrize('credential_stage', ['adc', 'refresh'])
async def test_upload_credential_failure_is_config_and_uncounted(
    monkeypatch: pytest.MonkeyPatch,
    credential_stage: str,
) -> None:
    """Missing or unusable ADC is local configuration, never a retry."""
    secret = f'credential-{credential_stage}-secret'
    events: list[str] = []
    provider = _UploadProvider([])
    if credential_stage == 'adc':
        provider.adc_error = google_auth_exceptions.DefaultCredentialsError(
            secret)
    else:
        provider.credentials.valid = False
        provider.credentials.refresh_error = (
            google_auth_exceptions.RefreshError(secret))

    result, reads = await _public_scripted_upload(
        monkeypatch,
        provider,
        bucket=f'credential-{credential_stage}',
        breaker_config=_gcs_retry_config(2, events),
    )

    assert reads == [('/caller/retry-source.bin', 41)]
    assert result['ok'] is False
    assert result['status_code'] == 400
    assert result['error']['code'] == 'CONFIG'
    assert secret not in repr(result)
    assert provider.upload_calls == []
    assert events == ['abort']


def _logged_gcs_surfaces(caplog: pytest.LogCaptureFixture) -> str:
    """Render captured log records for cross-surface leak assertions."""
    return ''.join(str(record.__dict__) for record in caplog.records)


async def test_upload_failure_never_leaks_any_private_input_surface(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Credential, body, path, precondition, and SDK text stay private."""
    sentinels = {
        'credential': 'credential-private-b1',
        'error': 'error-private-b1',
        'body': 'body-private-b1',
        'path': 'path-private-b1',
        'precondition': '987654321047',
    }
    events: list[str] = []
    provider = _UploadProvider([])
    provider.credentials.__repr__ = lambda: sentinels['credential']
    hostile_text = ' '.join(sentinels.values())
    provider.script_upload(_FakeServiceError(
        403,
        code=f'authorization=Bearer {hostile_text}',
        message=f'authorization=Bearer {hostile_text}',
        request_id=f'authorization=Bearer {hostile_text}',
    ))
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, _ = await _public_scripted_upload(
        monkeypatch,
        provider,
        bucket='private-surface',
        body=sentinels['body'].encode(),
        generation=int(sentinels['precondition']),
        local_path=f"/caller/{sentinels['path']}.bin",
        breaker_config=_gcs_retry_config(1, events),
    )

    surfaces = repr(result) + _logged_gcs_surfaces(caplog) + repr(events)
    assert result['error']['code'] == 'GCS_STATUS'
    assert set(result['protocol_details']) == {
        'command', 'bucket', 'target', 'gcs_error_code',
        'gcs_error_message', 'response_metadata',
    }
    assert all(secret not in surfaces for secret in sentinels.values())


# --- AGW-48 tranche B2: normalized upload and cleanup ownership ----------


@pytest.mark.parametrize(
    ('generation', 'metageneration', 'etag', 'crc32c'),
    [
        pytest.param(None, None, None, None, id='all-optional-none'),
        pytest.param(0, 0, '', '', id='zero-and-empty-strings'),
        pytest.param(2**63, 2**31, 'etag-value', 'crc-value', id='integers'),
    ],
)
async def test_upload_success_normalizes_exact_optional_metadata(
    monkeypatch: pytest.MonkeyPatch,
    generation: int | None,
    metageneration: int | None,
    etag: str | None,
    crc32c: str | None,
) -> None:
    """Valid optional provider scalars publish only the frozen schema."""
    provider = _UploadProvider([])
    provider.blob.generation = generation
    provider.blob.metageneration = metageneration
    provider.blob.etag = etag
    provider.blob.crc32c = crc32c

    result, _ = await _public_scripted_upload(
        monkeypatch,
        provider,
        bucket='normalized-upload',
        breaker_config={},
    )

    assert result['protocol_details'] == {
        'command': 'upload',
        'bucket': 'normalized-upload',
        'key': 'object.bin',
        'local_path': '/caller/retry-source.bin',
        'bytes_read': len(b'retry-body'),
        'etag': etag,
        'generation': generation,
        'metageneration': metageneration,
        'crc32c': crc32c,
    }
    assert provider.client.close_calls == 1


class _HostileUploadMetadata:
    """Foreign SDK value whose representation is a leak sentinel."""

    def __init__(self, sentinel: str) -> None:
        """Retain the value that must never cross a public surface."""
        self.sentinel = sentinel

    def __repr__(self) -> str:
        """Expose a deterministic marker if code retains this object."""
        return self.sentinel


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        pytest.param('generation', True, id='generation-bool'),
        pytest.param('generation', -1, id='generation-negative'),
        pytest.param('generation', '19', id='generation-string'),
        pytest.param('generation', _HostileUploadMetadata(
            'generation-foreign-b2'), id='generation-foreign'),
        pytest.param('metageneration', False, id='metageneration-bool'),
        pytest.param('metageneration', -1, id='metageneration-negative'),
        pytest.param('metageneration', '2', id='metageneration-string'),
        pytest.param('metageneration', _HostileUploadMetadata(
            'metageneration-foreign-b2'), id='metageneration-foreign'),
        pytest.param('etag', 7, id='etag-integer'),
        pytest.param('etag', _HostileUploadMetadata(
            'etag-foreign-b2'), id='etag-foreign'),
        pytest.param('crc32c', True, id='crc32c-bool'),
        pytest.param('crc32c', _HostileUploadMetadata(
            'crc32c-foreign-b2'), id='crc32c-foreign'),
    ],
)
async def test_malformed_upload_success_is_closed_gcs_status_without_leak(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    field: str,
    value: object,
) -> None:
    """Malformed nominal success is 502 and never partially published."""
    provider = _UploadProvider([])
    setattr(provider.blob, field, value)
    events: list[str] = []
    caplog.set_level(logging.ERROR, logger='asyncio_gateway')

    result, _ = await _public_scripted_upload(
        monkeypatch,
        provider,
        bucket='malformed-upload',
        breaker_config=_gcs_retry_config(1, events),
    )

    surfaces = repr(result) + _logged_gcs_surfaces(caplog) + repr(events)
    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {}
    assert provider.client.close_calls == 1
    if isinstance(value, _HostileUploadMetadata):
        assert value.sentinel not in surfaces


@pytest.mark.parametrize(
    ('failure', 'expected_code'),
    [
        pytest.param(_FakeServiceError(403), 'GCS_STATUS', id='abortable'),
        pytest.param(_FakeServiceError(503), 'GCS_STATUS', id='retryable'),
        pytest.param(socket.gaierror('transport-private-b2'), 'DNS',
                     id='exhausted-transport'),
    ],
)
async def test_upload_failure_closes_its_owned_client_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected_code: str,
) -> None:
    """Abortable and exhausted operations close their attempt resource."""
    provider = _UploadProvider([])
    provider.script_upload(failure)

    result, _ = await _public_scripted_upload(
        monkeypatch,
        provider,
        bucket='failed-close-once',
        breaker_config=_gcs_retry_config(0, []),
    )

    assert result['error']['code'] == expected_code
    assert provider.client.close_calls == 1


async def test_upload_retry_closes_each_attempt_client_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every retry owns and closes a distinct storage client."""
    provider = _UploadProvider([])
    provider.fresh_client_per_attempt = True
    provider.script_upload(_FakeServiceError(503), None)

    result, _ = await _public_scripted_upload(
        monkeypatch,
        provider,
        bucket='per-attempt-cleanup',
        breaker_config=_gcs_retry_config(1, []),
    )

    assert result['ok'] is True
    assert len(provider.returned_clients) == 2
    assert provider.returned_clients[0] is not provider.returned_clients[1]
    assert [client.close_calls for client in provider.returned_clients] == [
        1, 1,
    ]


@pytest.mark.parametrize(
    ('body_kind', 'expected_code'),
    [
        pytest.param('service', 'GCS_STATUS', id='service'),
        pytest.param('transport', 'DNS', id='transport'),
        pytest.param('metadata', 'GCS_STATUS', id='malformed-metadata'),
    ],
)
async def test_upload_body_failure_wins_over_later_hostile_close(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    body_kind: str,
    expected_code: str,
) -> None:
    """Cleanup cannot replace or leak an already-known upload failure."""
    close_sentinel = f'hostile-close-{body_kind}-b2'
    provider = _UploadProvider([])
    if body_kind == 'service':
        provider.script_upload(_FakeServiceError(403))
    elif body_kind == 'transport':
        provider.script_upload(socket.gaierror('body-transport-private-b2'))
    else:
        provider.blob.generation = -1
    provider.script_close(RuntimeError(close_sentinel))
    caplog.set_level(logging.ERROR, logger='asyncio_gateway')

    result, _ = await _public_scripted_upload(
        monkeypatch,
        provider,
        bucket='body-precedence',
        breaker_config=_gcs_retry_config(0, []),
    )

    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    assert result['error']['code'] == expected_code
    assert close_sentinel not in surfaces
    assert provider.client.close_calls == 1


@pytest.mark.parametrize(
    ('cleanup_error', 'expected_code'),
    [
        pytest.param(socket.gaierror('close-dns-private-b2'), 'DNS', id='dns'),
        pytest.param(
            google_auth_exceptions.DefaultCredentialsError(
                'close-credential-private-b2'),
            'CONFIG', id='credential'),
        pytest.param(_FakeServiceError(403), 'GCS_STATUS', id='service'),
    ],
)
async def test_upload_cleanup_only_failure_has_stable_typed_result(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error: BaseException,
    expected_code: str,
) -> None:
    """A provider cleanup failure is typed when no body failure exists."""
    provider = _UploadProvider([])
    provider.script_close(cleanup_error)

    result, _ = await _public_scripted_upload(
        monkeypatch,
        provider,
        bucket='cleanup-only',
        breaker_config=_gcs_retry_config(0, []),
    )

    assert result['ok'] is False
    assert result['error']['code'] == expected_code
    assert result['protocol_details'] == {}
    assert provider.client.close_calls == 1


async def test_upload_cleanup_only_programming_defect_is_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown cleanup defect propagates with identity and state intact."""
    provider = _UploadProvider([])
    failure = RuntimeError('exact-cleanup-programming-defect-b2')
    cause = ValueError('exact-cleanup-cause-b2')
    failure.__cause__ = cause
    provider.script_close(failure)

    with pytest.raises(RuntimeError) as caught:
        await _public_scripted_upload(
            monkeypatch,
            provider,
            bucket='cleanup-programming-defect',
            breaker_config=_gcs_retry_config(0, []),
        )

    assert caught.value is failure
    assert caught.value.__cause__ is cause
    assert provider.client.close_calls == 1


@pytest.mark.parametrize('blocked_seam', ['upload', 'close'])
async def test_upload_cancellation_drains_cleanup_and_retains_capacity(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    blocked_seam: str,
) -> None:
    """Repeated cancellation retains its lease through upload or close."""
    provider = _UploadProvider([])
    close_sentinel = f'cancel-close-private-{blocked_seam}-b2'
    provider.script_close(RuntimeError(close_sentinel))
    caplog.set_level(logging.ERROR, logger='asyncio_gateway')
    started = threading.Event()
    release = threading.Event()
    if blocked_seam == 'upload':
        provider.upload_started = started
        provider.upload_release = release
    else:
        provider.close_started = started
        provider.close_release = release
    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    other_leases = [acquire() for _ in range(3)]
    task = asyncio.create_task(_public_scripted_upload(
        monkeypatch,
        provider,
        bucket=f'cancel-{blocked_seam}',
        breaker_config={},
    ))
    admitted_after: Any = None
    try:
        await _wait_for_thread_event(started)
        task.cancel('first-upload-cancellation')
        task.cancel('repeated-upload-cancellation')
        with pytest.raises(GcsCapacityError):
            acquire()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.args == ('first-upload-cancellation',)
        cancellation_surfaces = (
            ''.join(traceback.format_exception(caught.value))
            + _logged_gcs_surfaces(caplog)
        )
        assert close_sentinel not in cancellation_surfaces
        assert provider.client.close_calls == 1
        admitted_after = acquire()
        with pytest.raises(GcsCapacityError):
            acquire()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if admitted_after is not None:
            await _release_lease(admitted_after)
        for lease in other_leases:
            await _release_lease(lease)


async def test_upload_timeout_wins_over_hostile_close_after_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance timeout remains the outcome after cleanup completes."""
    provider = _UploadProvider([])
    provider.upload_started = threading.Event()
    provider.upload_release = threading.Event()
    provider.script_close(RuntimeError('timeout-close-private-b2'))
    task = asyncio.create_task(_public_scripted_upload(
        monkeypatch,
        provider,
        bucket='timeout-cleanup-precedence',
        timeout=1e-6,
        breaker_config=_gcs_retry_config(0, []),
    ))
    try:
        await _wait_for_thread_event(provider.upload_started)
        provider.upload_release.set()
        result, _ = await task
    finally:
        provider.upload_release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert result['error']['code'] == 'TIMEOUT'
    assert provider.client.close_calls == 1


@pytest.mark.parametrize('outcome', ['cancel', 'timeout'])
async def test_late_created_upload_client_closes_under_retained_capacity(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """A rejected late constructor result is closed before lease release."""
    provider = _UploadProvider([])
    provider.client_started = threading.Event()
    provider.client_release = threading.Event()
    timeout = 1
    if outcome == 'timeout':
        async def timeout_after_client_starts(
            awaitable: Any,
            *,
            timeout: int | float,
        ) -> Any:
            """Inject expiry only after the client constructor has begun."""
            del timeout

            async def wait_for_client_start() -> None:
                while not provider.client_started.is_set():
                    await asyncio.sleep(0)

            operation = asyncio.ensure_future(awaitable)
            client_start = asyncio.create_task(wait_for_client_start())
            done, _ = await asyncio.wait(
                {operation, client_start},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                client_start.cancel()
                await asyncio.gather(client_start, return_exceptions=True)
                return operation.result()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise asyncio.TimeoutError

        monkeypatch.setattr(
            gcs_client.asyncio, 'wait_for', timeout_after_client_starts)
    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    other_leases = [acquire() for _ in range(3)]
    task = asyncio.create_task(_public_scripted_upload(
        monkeypatch,
        provider,
        bucket=f'late-client-{outcome}',
        timeout=timeout,
        breaker_config=_gcs_retry_config(0, []),
    ))
    try:
        await _wait_for_thread_event(provider.client_started)
        if outcome == 'cancel':
            task.cancel('late-client-cancellation')
            task.cancel('repeated-late-client-cancellation')
        with pytest.raises(GcsCapacityError):
            acquire()
        provider.client_release.set()
        if outcome == 'cancel':
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args == ('late-client-cancellation',)
        else:
            result, _ = await task
            assert result['error']['code'] == 'TIMEOUT'
        assert provider.client.close_calls == 1
    finally:
        provider.client_release.set()
        await asyncio.gather(task, return_exceptions=True)
        for lease in other_leases:
            await _release_lease(lease)


# --- AGW-48 tranche D1: pinned raw-download foundation -------------------


class _DownloadBlob:
    """Synchronous exact-range blob double for one bounded download."""

    def __init__(self, provider: '_DownloadProvider') -> None:
        """Retain the provider recorder and one valid metadata pin."""
        self.provider = provider
        self.generation: object = 41
        self.size: object = len(provider.stored_bytes)
        self.etag: object = 'download-etag'
        self.crc32c: object = 'download-crc32c=='

    def reload(self, **kwargs: object) -> None:
        """Record exact pin controls and the provider worker identity."""
        self.provider.record_thread('reload')
        self.provider.reload_calls.append(dict(kwargs))

    def download_as_bytes(self, **kwargs: object) -> bytes:
        """Return one exact inclusive slice of the stored/raw object."""
        self.provider.record_thread('range')
        self.provider.range_calls.append(dict(kwargs))
        start = kwargs['start']
        end = kwargs['end']
        assert isinstance(start, int) and not isinstance(start, bool)
        assert isinstance(end, int) and not isinstance(end, bool)
        return self.provider.stored_bytes[start:end + 1]


class _DownloadBucket:
    """Bucket double returning one observable download blob."""

    def __init__(self, provider: '_DownloadProvider') -> None:
        """Retain the provider recorder."""
        self.provider = provider

    def blob(self, key: str) -> _DownloadBlob:
        """Record the exact object key selected by the request."""
        self.provider.record_thread('blob')
        self.provider.blob_keys.append(key)
        return self.provider.blob


class _DownloadClient:
    """Storage-client double with observable download cleanup."""

    def __init__(self, provider: '_DownloadProvider') -> None:
        """Retain the provider recorder."""
        self.provider = provider
        self.close_calls = 0

    def bucket(self, name: str) -> _DownloadBucket:
        """Record the normalized bucket name."""
        self.provider.record_thread('bucket')
        self.provider.bucket_names.append(name)
        return self.provider.bucket

    def close(self) -> None:
        """Record one deterministic off-loop cleanup."""
        self.provider.record_thread('client-close')
        self.close_calls += 1


class _DownloadProvider:
    """Deterministic ADC, metadata, raw-range, and cleanup recorder."""

    def __init__(self, stored_bytes: bytes) -> None:
        """Create one connected provider tree for the stored bytes."""
        self.stored_bytes = stored_bytes
        self.timeline: list[str] = []
        self.threads: dict[str, list[tuple[int, str]]] = {}
        self.reload_calls: list[dict[str, object]] = []
        self.range_calls: list[dict[str, object]] = []
        self.bucket_names: list[str] = []
        self.blob_keys: list[str] = []
        self.credentials = _UploadCredentials()
        self.blob = _DownloadBlob(self)
        self.bucket = _DownloadBucket(self)
        self.client = _DownloadClient(self)

    def record_thread(self, seam: str) -> None:
        """Record every occurrence of one synchronous provider seam."""
        self.timeline.append(seam)
        self.threads.setdefault(seam, []).append((
            threading.get_ident(), threading.current_thread().name))

    def adc(self, *args: object, **kwargs: object) -> tuple[object, str]:
        """Return valid local ADC without contacting a metadata server."""
        self.record_thread('adc')
        return self.credentials, 'download-project'

    def make_client(self, *args: object, **kwargs: object) -> _DownloadClient:
        """Return the observable storage client."""
        self.record_thread('client')
        return self.client


def _install_download_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: _DownloadProvider,
) -> None:
    """Replace ADC and GCS construction with download-only doubles."""
    monkeypatch.setattr(google_auth, 'default', provider.adc)
    monkeypatch.setattr(storage, 'Client', provider.make_client)


async def _public_download(
    monkeypatch: pytest.MonkeyPatch,
    provider: _DownloadProvider,
    local_path: Path,
    *,
    max_response_bytes: int,
    generation: int | None = None,
) -> tuple[dict[str, Any], _UploadBreaker]:
    """Drive one download through the public selector with local doubles."""
    breaker = _UploadBreaker(provider.timeline)
    monkeypatch.setattr(base, 'get_breaker', lambda *args, **kwargs: breaker)
    _install_download_provider(monkeypatch, provider)
    info: dict[str, object] = {
        'command': 'download',
        'local_path': str(local_path),
        'max_response_bytes': max_response_bytes,
        'timeout': 2.5,
    }
    if generation is not None:
        info['if_generation_match'] = generation
    result = await request(
        'gs://Download-Bucket/folder/object.bin',
        protocol='GCS',
        protocol_info=info,
    )
    return result, breaker


@pytest.mark.parametrize(
    ('size', 'caller_generation', 'expected_ranges'),
    [
        pytest.param(0, None, [], id='empty'),
        pytest.param(1, 41, [(0, 0)], id='one-byte'),
        pytest.param(65536, None, [(0, 65535)], id='exact-64-kib'),
        pytest.param(
            65537, None, [(0, 65535), (65536, 65536)],
            id='two-inclusive-ranges',
        ),
    ],
)
async def test_download_pins_then_streams_exact_raw_ranges_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    size: int,
    caller_generation: int | None,
    expected_ranges: list[tuple[int, int]],
) -> None:
    """One immutable pin feeds exact raw ranges to the atomic writer."""
    stored_bytes = bytes(index % 251 for index in range(size))
    provider = _DownloadProvider(stored_bytes)
    if size == 0:
        provider.blob.etag = None
        provider.blob.crc32c = None
    target = tmp_path / 'download.bin'
    target.write_bytes(b'old-target')
    stream_calls: list[dict[str, object]] = []
    loop_thread = threading.get_ident()

    async def recording_stream(
        path: object,
        chunks: Any,
        *,
        overwrite: bool,
        max_bytes: int | None,
        advertised_bytes: int | None = None,
    ) -> int:
        """Record the held lease and delegate to the real atomic writer."""
        provider.timeline.append('stream')
        stream_calls.append({
            'path': path,
            'overwrite': overwrite,
            'max_bytes': max_bytes,
            'advertised_bytes': advertised_bytes,
            'active_leases': getattr(gcs_client, '_active_gcs_leases')(),
        })
        return await stream_to_path(
            path,
            chunks,
            overwrite=overwrite,
            max_bytes=max_bytes,
            advertised_bytes=advertised_bytes,
        )

    monkeypatch.setattr(
        gcs_client, 'stream_to_path', recording_stream, raising=False)

    result, breaker = await _public_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=max(size, 1),
        generation=caller_generation,
    )

    expected_reload: dict[str, object] = {
        'retry': None,
        'timeout': 2.5,
    }
    if caller_generation is not None:
        expected_reload['if_generation_match'] = caller_generation
    assert provider.reload_calls == [expected_reload]
    assert provider.range_calls == [
        {
            'start': start,
            'end': end,
            'if_generation_match': 41,
            'raw_download': True,
            'retry': None,
            'timeout': 2.5,
        }
        for start, end in expected_ranges
    ]
    assert stream_calls == [{
        'path': str(target),
        'overwrite': True,
        'max_bytes': max(size, 1),
        'advertised_bytes': size,
        'active_leases': 1,
    }]
    assert provider.timeline.index('reload') < provider.timeline.index(
        'stream')
    assert target.read_bytes() == stored_bytes
    assert provider.client.close_calls == 1
    assert provider.bucket_names == ['download-bucket']
    assert provider.blob_keys == ['folder/object.bin']
    assert breaker.calls == 1
    assert getattr(gcs_client, '_active_gcs_leases')() == 0
    for seam in {'adc', 'client', 'bucket', 'blob', 'reload', 'range',
                 'client-close'}:
        for thread_id, thread_name in provider.threads.get(seam, []):
            assert thread_id != loop_thread
            assert thread_name.startswith('asyncio-gateway-gcs')
    assert result['status_code'] == 200
    assert result['protocol_details'] == {
        'command': 'download',
        'bucket': 'download-bucket',
        'key': 'folder/object.bin',
        'local_path': str(target),
        'bytes_written': size,
        'etag': None if size == 0 else 'download-etag',
        'generation': 41,
        'crc32c': None if size == 0 else 'download-crc32c==',
    }


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        pytest.param('generation', True, id='generation-bool'),
        pytest.param('generation', -1, id='generation-negative'),
        pytest.param('generation', None, id='generation-missing'),
        pytest.param('size', False, id='size-bool'),
        pytest.param('size', -1, id='size-negative'),
        pytest.param('size', None, id='size-missing'),
    ],
)
async def test_download_rejects_malformed_pin_before_range_or_local_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """A malformed nominal metadata success fails closed before output."""
    provider = _DownloadProvider(b'x')
    setattr(provider.blob, field, value)
    target = tmp_path / 'download.bin'
    target.write_bytes(b'existing-target')
    stream_called = False

    async def forbidden_stream(*args: object, **kwargs: object) -> NoReturn:
        """Fail if malformed metadata reaches the local writer."""
        nonlocal stream_called
        stream_called = True
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(
        gcs_client, 'stream_to_path', forbidden_stream, raising=False)

    result, breaker = await _public_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=1,
    )

    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {}
    assert provider.reload_calls == [{'retry': None, 'timeout': 2.5}]
    assert provider.range_calls == []
    assert stream_called is False
    assert target.read_bytes() == b'existing-target'
    assert provider.client.close_calls == 1
    assert breaker.calls == 1


async def test_download_caller_generation_is_authoritative_before_ranges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reload cannot silently replace an explicit caller generation pin."""
    provider = _DownloadProvider(b'x')
    provider.blob.generation = 42
    target = tmp_path / 'download.bin'
    target.write_bytes(b'existing-target')
    stream_called = False

    async def forbidden_stream(*args: object, **kwargs: object) -> NoReturn:
        """Fail if a mismatched generation reaches local output."""
        nonlocal stream_called
        stream_called = True
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(
        gcs_client, 'stream_to_path', forbidden_stream, raising=False)

    result, _ = await _public_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=1,
        generation=41,
    )

    assert result['error']['code'] == 'GCS_STATUS'
    assert result['status_code'] == 502
    assert provider.reload_calls == [{
        'retry': None,
        'timeout': 2.5,
        'if_generation_match': 41,
    }]
    assert provider.range_calls == []
    assert stream_called is False
    assert target.read_bytes() == b'existing-target'


async def test_download_advertised_cap_precedes_range_and_path_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pinned stored size above the cap is refused before local output."""
    provider = _DownloadProvider(b'over-cap')
    target = tmp_path / 'download.bin'
    target.write_bytes(b'existing-target')
    stream_called = False

    async def forbidden_stream(*args: object, **kwargs: object) -> NoReturn:
        """Fail if an advertised oversize reaches the local writer."""
        nonlocal stream_called
        stream_called = True
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(
        gcs_client, 'stream_to_path', forbidden_stream, raising=False)

    result, _ = await _public_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=7,
    )

    assert result['ok'] is False
    assert result['error']['code'] == 'RESPONSE_TOO_LARGE'
    assert provider.reload_calls == [{'retry': None, 'timeout': 2.5}]
    assert provider.range_calls == []
    assert stream_called is False
    assert target.read_bytes() == b'existing-target'
    assert provider.client.close_calls == 1


# --- GCS-04 tranche B: shutdown, telemetry, and outcome precedence -------


_FRESH_GCS_MODULE_NUMBER = itertools.count()
_CAPACITY_EVENT_FIELDS: Mapping[str, frozenset[str]] = {
    'gcs_capacity_state': frozenset({'active_leases', 'max_leases'}),
    'gcs_capacity_rejected': frozenset({'active_leases', 'reason'}),
    'gcs_drain_started': frozenset(),
    'gcs_drain_finished': frozenset({
        'active_leases', 'duration_seconds',
    }),
    'gcs_capacity_closing': frozenset({'active_leases'}),
}
_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.LogRecord(
    name='', level=0, pathname='', lineno=0, msg='', args=(),
    exc_info=None,
).__dict__) | frozenset({'asctime', 'message'})


class _RecordingExecutor:
    """Run calls in one real worker while recording shutdown arguments."""

    def __init__(self) -> None:
        """Create an isolated executor and an empty shutdown trace."""
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix='gcs-tranche-b-test',
        )
        self.shutdown_calls: list[tuple[bool, bool]] = []

    def submit(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        """Delegate provider work to the isolated real executor."""
        return self._executor.submit(function, *args, **kwargs)

    def shutdown(
        self,
        wait: bool = True,
        *,
        cancel_futures: bool = False,
    ) -> None:
        """Record and delegate the exact production shutdown call."""
        self.shutdown_calls.append((wait, cancel_futures))
        self._executor.shutdown(
            wait=wait,
            cancel_futures=cancel_futures,
        )

    def close_test_executor(self) -> None:
        """Join the isolated worker without changing the recorded trace."""
        self._executor.shutdown(wait=True, cancel_futures=True)


def _fresh_gcs_lifecycle_module() -> tuple[ModuleType, _RecordingExecutor]:
    """Load isolated process state so shutdown cannot poison another test."""
    source_path = Path(gcs_client.__file__ or '')
    module_name = (
        'asyncio_gateway.logic.gcs_client_tranche_b_'
        f'{next(_FRESH_GCS_MODULE_NUMBER)}'
    )
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    original_executor = getattr(module, '_GCS_EXECUTOR')
    original_executor.shutdown(wait=False, cancel_futures=True)
    recording_executor = _RecordingExecutor()
    setattr(module, '_GCS_EXECUTOR', recording_executor)
    return module, recording_executor


async def _call_gcs_shutdown(module: ModuleType) -> None:
    """Call the private shutdown path whether it is sync or async."""
    outcome = getattr(module, '_shutdown_gcs_offloader')()
    if inspect.isawaitable(outcome):
        await outcome


async def _release_isolated_lease(lease: Any) -> None:
    """Release one lease from an isolated lifecycle module."""
    outcome = lease.release()
    if inspect.isawaitable(outcome):
        await outcome


def _capacity_events(
    caplog: pytest.LogCaptureFixture,
) -> list[tuple[str, dict[str, Any]]]:
    """Return exact custom fields for GCS capacity telemetry records."""
    events: list[tuple[str, dict[str, Any]]] = []
    for record in caplog.records:
        event = record.getMessage()
        if event not in _CAPACITY_EVENT_FIELDS:
            continue
        custom = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOG_RECORD_FIELDS
        }
        assert set(custom) == set(_CAPACITY_EVENT_FIELDS[event])
        events.append((event, custom))
    return events


async def test_idle_shutdown_closes_admission_once_and_refuses_after_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Idle shutdown is immediate, idempotent, and locally typed."""
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    module, executor = _fresh_gcs_lifecycle_module()
    try:
        await _call_gcs_shutdown(module)
        await _call_gcs_shutdown(module)

        with pytest.raises(GcsCapacityError) as raised:
            getattr(module, '_acquire_gcs_lease')()

        assert raised.value.code == 'GCS_CAPACITY'
        assert raised.value.status_code == 503
        assert executor.shutdown_calls == [(False, True)]
        assert _capacity_events(caplog) == [
            ('gcs_capacity_closing', {'active_leases': 0}),
            ('gcs_capacity_rejected', {
                'active_leases': 0,
                'reason': 'closing',
            }),
        ]
    finally:
        executor.close_test_executor()


async def test_active_shutdown_waits_for_final_release_and_keeps_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Closing refuses newcomers but preserves accepted cleanup capacity."""
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    module, executor = _fresh_gcs_lifecycle_module()
    lease = getattr(module, '_acquire_gcs_lease')()
    try:
        await _call_gcs_shutdown(module)
        assert executor.shutdown_calls == []

        with pytest.raises(GcsCapacityError):
            getattr(module, '_acquire_gcs_lease')()

        assert await lease.run(lambda: 'cleanup-finished', timeout=1) == (
            'cleanup-finished')
        assert executor.shutdown_calls == []

        await _release_isolated_lease(lease)
        await _release_isolated_lease(lease)
        await _call_gcs_shutdown(module)

        assert executor.shutdown_calls == [(False, True)]
        assert _capacity_events(caplog) == [
            ('gcs_capacity_state', {
                'active_leases': 1,
                'max_leases': 4,
            }),
            ('gcs_capacity_closing', {'active_leases': 1}),
            ('gcs_capacity_rejected', {
                'active_leases': 1,
                'reason': 'closing',
            }),
            ('gcs_capacity_state', {
                'active_leases': 0,
                'max_leases': 4,
            }),
        ]
    finally:
        await _release_isolated_lease(lease)
        executor.close_test_executor()


async def test_saturation_and_release_emit_each_exact_state_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Acquire, saturated rejection, and release telemetry is cardinal."""
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    module, executor = _fresh_gcs_lifecycle_module()
    acquire = getattr(module, '_acquire_gcs_lease')
    leases = [acquire() for _ in range(4)]
    try:
        with pytest.raises(GcsCapacityError):
            acquire()
        for lease in leases:
            await _release_isolated_lease(lease)
            await _release_isolated_lease(lease)

        assert _capacity_events(caplog) == [
            *[
                ('gcs_capacity_state', {
                    'active_leases': active,
                    'max_leases': 4,
                })
                for active in (1, 2, 3, 4)
            ],
            ('gcs_capacity_rejected', {
                'active_leases': 4,
                'reason': 'saturated',
            }),
            *[
                ('gcs_capacity_state', {
                    'active_leases': active,
                    'max_leases': 4,
                })
                for active in (3, 2, 1, 0)
            ],
        ]
    finally:
        for lease in leases:
            await _release_isolated_lease(lease)
        executor.close_test_executor()


@pytest.mark.parametrize('outcome', ['cancel', 'timeout'])
async def test_drain_telemetry_is_cardinal_finite_and_secret_safe(
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    """Only over-deadline/cancel drain emits safe start and finish events."""
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    module, executor = _fresh_gcs_lifecycle_module()
    lease = getattr(module, '_acquire_gcs_lease')()
    started = threading.Event()
    release = threading.Event()
    sentinels = (
        'bucket-secret-47', 'object-secret-47', 'page-token-secret-47',
        'signer-secret-47', 'credential-secret-47', 'payload-secret-47',
        'signed-header-secret-47', 'signed-query-secret-47',
        'https://signed-secret-47.example/?signature=secret',
    )

    class CleanupFailure(RuntimeError):
        """Hostile cleanup error that must not replace the first outcome."""

    class Resource:
        def close(self) -> None:
            raise CleanupFailure(' '.join(sentinels))

    def blocked_resource() -> Resource:
        started.set()
        assert release.wait(timeout=1)
        return Resource()

    for index, sentinel in enumerate(sentinels):
        setattr(lease, f'hostile_{index}', sentinel)
    timeout = 1 if outcome == 'cancel' else 1e-6
    task = asyncio.create_task(
        lease.run(
            blocked_resource,
            timeout=timeout,
            close_result=lambda resource: resource.close(),
        )
    )
    try:
        await _wait_for_thread_event(started)
        if outcome == 'cancel':
            task.cancel('original-cancellation')
            task.cancel('repeated-cancellation')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()

        release.set()
        if outcome == 'cancel':
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args == ('original-cancellation',)
        else:
            with pytest.raises(GatewayTimeoutError):
                await task

        await _release_isolated_lease(lease)
        events = _capacity_events(caplog)
        assert events[0] == ('gcs_capacity_state', {
            'active_leases': 1,
            'max_leases': 4,
        })
        assert events[1] == ('gcs_drain_started', {})
        assert events[2][0] == 'gcs_drain_finished'
        assert events[2][1]['active_leases'] == 1
        duration = events[2][1]['duration_seconds']
        assert isinstance(duration, (int, float))
        assert not isinstance(duration, bool)
        assert math.isfinite(duration) and duration >= 0
        assert events[3] == ('gcs_capacity_state', {
            'active_leases': 0,
            'max_leases': 4,
        })
        assert len(events) == 4

        telemetry_surfaces = caplog.text + ''.join(
            repr(record.__dict__) for record in caplog.records)
        for sentinel in sentinels:
            assert sentinel not in telemetry_surfaces
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await _release_isolated_lease(lease)
        executor.close_test_executor()

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

from failsafe import CircuitOpen, RetriesExhausted

from google import auth as google_auth
from google.api_core import exceptions as google_api_exceptions
from google.auth import credentials as google_auth_credentials
from google.auth import exceptions as google_auth_exceptions
from google.auth import impersonated_credentials
from google.cloud import storage

import pytest

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.helpers.internal import base
from asyncio_gateway.helpers.internal.circuit_breaker_helper import (
    AbortableServiceError,
    CircuitBreakerHelper,
    validated_breaker_config,
)
from asyncio_gateway.logic import gcs_client
from asyncio_gateway.logic.gcs_client import GcsRequest
from asyncio_gateway.utils.constants import (
    HTTP_TIMEOUT,
    MAX_RESPONSE_BYTES,
    UNKNOWN_PORT,
)
from asyncio_gateway.utils.envelope import GatewayResponse, new_envelope
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
        ('https://bucket/key', 'head'),
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


# --- AGW-50 tranche S1: direct V4 signed URLs ----------------------------


class _DirectSigner:
    """Minimal deterministic signer exposed by direct ADC credentials."""

    @property
    def key_id(self) -> str:
        """Return one stable test-only key identifier."""
        return 'direct-signing-key'

    def sign(self, message: bytes) -> bytes:
        """Return a deterministic signature without external work."""
        return b'direct-signature:' + message


class _DirectSigningCredentials(google_auth_credentials.Signing):
    """Valid ADC credentials implementing Google's signing capability."""

    def __init__(self, provider: '_SignedUrlProvider') -> None:
        """Retain the provider recorder and one usable signer identity."""
        self.provider = provider
        self.valid = True
        self.expired = False
        self._signer = _DirectSigner()

    def sign_bytes(self, message: bytes) -> bytes:
        """Delegate deterministic signing to the exposed signer."""
        return self._signer.sign(message)

    @property
    def signer_email(self) -> str:
        """Expose one non-empty identity and record selection off-loop."""
        self.provider.record_thread('signer-identity')
        return 'direct-signer@example-project.iam.gserviceaccount.com'

    @property
    def signer(self) -> _DirectSigner:
        """Expose the deterministic signer required by the capability."""
        self.provider.record_thread('signer')
        return self._signer

    def refresh(self, request: object) -> None:
        """Reject refresh because this direct credential starts valid."""
        raise AssertionError(
            f'valid direct credentials must not refresh: {request!r}')


class _BearerOnlyCredentials:
    """Valid bearer credentials deliberately lacking signing capability."""

    def __init__(self, token: str) -> None:
        """Retain one sentinel bearer token for safe-refusal proof."""
        self.valid = True
        self.expired = False
        self.token = token

    def refresh(self, request: object) -> None:
        """Reject refresh because this bearer credential starts valid."""
        raise AssertionError(
            f'valid bearer credentials must not refresh: {request!r}')


class _SignedUrlBlob:
    """Exact-call V4 URL-generation double with open-client visibility."""

    def __init__(self, provider: '_SignedUrlProvider') -> None:
        """Retain the connected provider recorder."""
        self.provider = provider

    def generate_signed_url(self, **kwargs: object) -> str:
        """Record one exact generation call without signing or network I/O."""
        self.provider.record_thread('generate')
        self.provider.generate_calls.append(dict(kwargs))
        self.provider.client_open_during_generate.append(
            not self.provider.client.closed)
        assert self.provider.response is not None
        self.provider.details_during_generate.append(dict(
            self.provider.response['protocol_details']))
        return self.provider.signed_url


class _SignedUrlBucket:
    """Bucket double returning one exact-object signing target."""

    def __init__(self, provider: '_SignedUrlProvider') -> None:
        """Retain the connected provider recorder."""
        self.provider = provider

    def blob(self, key: str) -> _SignedUrlBlob:
        """Record and return the caller-selected exact object."""
        self.provider.record_thread('blob')
        self.provider.blob_keys.append(key)
        return self.provider.blob


class _SignedUrlClient:
    """Storage client double exposing close-before-publication ordering."""

    def __init__(self, provider: '_SignedUrlProvider') -> None:
        """Retain the provider recorder and start open."""
        self.provider = provider
        self.closed = False
        self.close_calls = 0

    def bucket(self, name: str) -> _SignedUrlBucket:
        """Record one normalized bucket lookup while the client is open."""
        self.provider.record_thread('bucket')
        self.provider.bucket_names.append(name)
        return self.provider.bucket

    def close(self) -> None:
        """Capture unpublished details before closing the owned client."""
        self.provider.record_thread('client-close')
        assert self.provider.response is not None
        self.provider.details_during_close.append(dict(
            self.provider.response['protocol_details']))
        self.close_calls += 1
        self.closed = True


class _SignedUrlProvider:
    """Deterministic direct-signing ADC and storage SDK tree."""

    def __init__(self, credentials: object | None = None) -> None:
        """Create one observable provider tree with no external I/O."""
        self.timeline: list[str] = []
        self.threads: list[tuple[str, int, str]] = []
        self.generate_calls: list[dict[str, object]] = []
        self.client_calls: list[
            tuple[tuple[object, ...], dict[str, object]]
        ] = []
        self.bucket_names: list[str] = []
        self.blob_keys: list[str] = []
        self.client_open_during_generate: list[bool] = []
        self.details_during_generate: list[dict[str, object]] = []
        self.details_during_close: list[dict[str, object]] = []
        self.response: dict[str, Any] | None = None
        self.signed_url = (
            'https://storage.googleapis.com/signed-bucket/folder/object.bin'
            '?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Signature=s1')
        self.credentials = (
            _DirectSigningCredentials(self)
            if credentials is None else credentials
        )
        self.blob = _SignedUrlBlob(self)
        self.bucket = _SignedUrlBucket(self)
        self.client = _SignedUrlClient(self)

    def record_thread(self, seam: str) -> None:
        """Record provider seam ordering and executing thread identity."""
        self.timeline.append(seam)
        self.threads.append((
            seam, threading.get_ident(), threading.current_thread().name))

    def adc(self, *args: object, **kwargs: object) -> tuple[object, str]:
        """Return deterministic direct ADC without metadata-server work."""
        self.record_thread('adc')
        return self.credentials, 'signed-url-project'

    def make_client(self, *args: object, **kwargs: object) -> _SignedUrlClient:
        """Record exact storage-client construction and return it open."""
        self.record_thread('client')
        self.client_calls.append((args, dict(kwargs)))
        return self.client


def _install_signed_url_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: _SignedUrlProvider,
) -> '_UploadBreaker':
    """Install direct ADC/storage doubles and a non-executed breaker."""
    breaker = _UploadBreaker(provider.timeline)
    monkeypatch.setattr(base, 'get_breaker', lambda *args, **kwargs: breaker)
    monkeypatch.setattr(google_auth, 'default', provider.adc)
    monkeypatch.setattr(storage, 'Client', provider.make_client)
    return breaker


@pytest.mark.parametrize(
    (
        'method', 'expiry_option', 'expected_expiry',
        'max_upload_bytes', 'generation',
    ),
    [
        pytest.param('GET', 1, 1, None, None, id='get-min-expiry'),
        pytest.param('GET', None, 900, None, None, id='get-default-expiry'),
        pytest.param('GET', 3600, 3600, None, None, id='get-max-expiry'),
        pytest.param('PUT', 1, 1, 1, None, id='put-min-default-gen'),
        pytest.param('PUT', None, 900, 23, 41, id='put-default-custom-gen'),
        pytest.param('PUT', 3600, 3600, 64, None, id='put-max-expiry'),
    ],
)
async def test_direct_signer_exact_v4_call_closes_before_publish(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    expiry_option: int | None,
    expected_expiry: int,
    max_upload_bytes: int | None,
    generation: int | None,
) -> None:
    """GET and PUT bind exact V4 calls and atomically publish after close."""
    provider = _SignedUrlProvider()
    breaker = _install_signed_url_provider(monkeypatch, provider)
    loop_thread = threading.get_ident()
    info: dict[str, object] = {
        'command': 'signed_url',
        'method': method,
        'timeout': 2.5,
    }
    if expiry_option is not None:
        info['expires_in_seconds'] = expiry_option
    if method == 'PUT':
        assert max_upload_bytes is not None
        info.update({
            'content_type': 'application/octet-stream',
            'max_upload_bytes': max_upload_bytes,
        })
        if generation is not None:
            info['if_generation_match'] = generation

    response = _response(
        'gs://Signed-Bucket/folder/object.bin')
    provider.response = response
    strategy = GcsRequest(
        'gs://Signed-Bucket/folder/object.bin',
        None,
        response,
        _validate(info),
        redact_params=frozenset(),
    )

    result = await strategy.handle_request()

    expected_call: dict[str, object] = {
        'version': 'v4',
        'expiration': timedelta(seconds=expected_expiry),
        'method': method,
        'credentials': provider.credentials,
    }
    expected_details: dict[str, object] = {
        'command': 'signed_url',
        'method': method,
        'bucket': 'signed-bucket',
        'key': 'folder/object.bin',
        'expires_in_seconds': expected_expiry,
        'signed_url': provider.signed_url,
    }
    if method == 'PUT':
        assert max_upload_bytes is not None
        expected_generation = 0 if generation is None else generation
        sdk_headers = {
            'x-goog-content-length-range': f'1,{max_upload_bytes}',
            'x-goog-if-generation-match': str(expected_generation),
        }
        expected_call.update({
            'content_type': 'application/octet-stream',
            'headers': sdk_headers,
        })
        expected_details.update({
            'content_type': 'application/octet-stream',
            'max_upload_bytes': max_upload_bytes,
            'if_generation_match': expected_generation,
            'required_headers': {
                'content-type': 'application/octet-stream',
                **sdk_headers,
            },
        })

    assert isinstance(
        provider.credentials, google_auth_credentials.Signing)
    assert provider.generate_calls == [expected_call]
    assert provider.client_calls == [(
        (),
        {
            'credentials': provider.credentials,
            'project': 'signed-url-project',
        },
    )]
    assert provider.bucket_names == ['signed-bucket']
    assert provider.blob_keys == ['folder/object.bin']
    assert provider.client_open_during_generate == [True]
    assert provider.details_during_generate == [{}]
    assert provider.details_during_close == [{}]
    assert provider.client.close_calls == 1
    assert provider.client.closed is True
    assert provider.timeline.count('generate') == 1
    assert provider.timeline.index('generate') < provider.timeline.index(
        'client-close')
    assert breaker.calls == 0
    signer_identity_threads = [
        (thread_id, thread_name)
        for seam, thread_id, thread_name in provider.threads
        if seam == 'signer-identity'
    ]
    assert signer_identity_threads
    assert all(
        thread_id != loop_thread
        and thread_name.startswith('asyncio-gateway-gcs')
        for thread_id, thread_name in signer_identity_threads
    )
    provider_threads = [
        (thread_id, thread_name)
        for seam, thread_id, thread_name in provider.threads
        if seam in {'generate', 'client-close'}
    ]
    assert len(provider_threads) == 2
    assert all(
        thread_id != loop_thread
        and thread_name.startswith('asyncio-gateway-gcs')
        for thread_id, thread_name in provider_threads
    )
    assert result is response
    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['protocol_details'] == expected_details


async def test_bearer_only_direct_credentials_fail_before_url_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid bearer token without Signing capability fails safely."""
    secret = 'bearer-only-direct-token-agw50-s1'
    provider = _SignedUrlProvider(_BearerOnlyCredentials(secret))
    breaker = _install_signed_url_provider(monkeypatch, provider)

    result = await request(
        'gs://signed-bucket/folder/object.bin',
        protocol='GCS',
        protocol_info={
            'command': 'signed_url',
            'method': 'GET',
            'timeout': 2.5,
        },
    )

    assert result['ok'] is False
    assert result['status_code'] == 400
    assert result['error']['code'] == 'CONFIG'
    assert result['protocol_details'] == {}
    assert provider.generate_calls == []
    assert breaker.calls == 0
    assert secret not in repr(result)


# --- AGW-50 tranche S2: signer identity and IAM impersonation ------------


class _HostileSignerIdentity:
    """Non-string identity that fails if production tries to render it."""

    def __init__(self) -> None:
        """Start with no attempted string or representation conversion."""
        self.str_calls = 0
        self.repr_calls = 0

    def __str__(self) -> str:
        """Reject conversion of an untrusted credential-provider value."""
        self.str_calls += 1
        raise AssertionError('hostile signer identity was stringified')

    def __repr__(self) -> str:
        """Reject diagnostic rendering of an untrusted provider value."""
        self.repr_calls += 1
        raise AssertionError('hostile signer identity was represented')


class _SignerIdentityCredentials(_DirectSigningCredentials):
    """Direct signing credentials exposing one caller-selected identity."""

    def __init__(
        self,
        provider: '_SignedUrlProvider',
        identity: object,
    ) -> None:
        """Retain the raw identity without normalizing or rendering it."""
        super().__init__(provider)
        self.identity = identity

    @property
    def signer_email(self) -> Any:
        """Return the raw test value and record off-loop validation."""
        self.provider.record_thread('signer-identity')
        return self.identity


@pytest.mark.parametrize(
    'identity_case',
    [
        pytest.param('blank', id='blank'),
        pytest.param('whitespace', id='whitespace'),
        pytest.param('hostile-non-string', id='hostile-non-string'),
    ],
)
async def test_direct_signer_rejects_unusable_identity_before_client(
    monkeypatch: pytest.MonkeyPatch,
    identity_case: str,
) -> None:
    """Blank and hostile signer identities fail CONFIG without disclosure."""
    provider = _SignedUrlProvider()
    hostile: _HostileSignerIdentity | None = None
    identity: object = ''
    if identity_case == 'whitespace':
        identity = ' \t\n '
    elif identity_case == 'hostile-non-string':
        hostile = _HostileSignerIdentity()
        identity = hostile
    provider.credentials = _SignerIdentityCredentials(provider, identity)
    breaker = _install_signed_url_provider(monkeypatch, provider)

    result = await request(
        'gs://signed-bucket/folder/object.bin',
        protocol='GCS',
        protocol_info={
            'command': 'signed_url',
            'method': 'GET',
            'timeout': 2.5,
        },
    )

    assert result['ok'] is False
    assert result['status_code'] == 400
    assert result['error']['code'] == 'CONFIG'
    assert result['protocol_details'] == {}
    assert provider.client_calls == []
    assert provider.generate_calls == []
    assert breaker.calls == 0
    assert provider.timeline.count('signer-identity') == 1
    if hostile is not None:
        assert hostile.str_calls == 0
        assert hostile.repr_calls == 0


class _ImpersonationSourceCredentials:
    """Non-signing ADC source with deterministic refresh behavior."""

    def __init__(
        self,
        provider: '_ImpersonationProvider',
        *,
        valid: bool,
        refresh_error: BaseException | None = None,
    ) -> None:
        """Configure source validity and an optional refresh refusal."""
        self.provider = provider
        self.valid = valid
        self.expired = not valid
        self.refresh_error = refresh_error

    def refresh(self, request: object) -> None:
        """Refresh off-loop or raise the configured credential failure."""
        self.provider.record_thread('source-refresh')
        if self.refresh_error is not None:
            raise self.refresh_error
        self.valid = True
        self.expired = False


class _ImpersonatedSigningCredentials(google_auth_credentials.Signing):
    """Deterministic signing target returned by IAM impersonation."""

    def __init__(
        self,
        provider: '_ImpersonationProvider',
        *,
        refresh_error: BaseException | None = None,
    ) -> None:
        """Start invalid so target refresh is an observable requirement."""
        self.provider = provider
        self.valid = False
        self.expired = True
        self.refresh_error = refresh_error
        self._signer = _DirectSigner()

    def refresh(self, request: object) -> None:
        """Refresh the impersonated target or raise one IAM refusal."""
        self.provider.record_thread('target-refresh')
        if self.refresh_error is not None:
            raise self.refresh_error
        self.valid = True
        self.expired = False

    def sign_bytes(self, message: bytes) -> bytes:
        """Record nested IAM signing from inside URL generation."""
        self.provider.record_thread('sign-bytes')
        return b'impersonated-signature:' + message

    @property
    def signer_email(self) -> str:
        """Expose the exact validated target principal while off-loop."""
        self.provider.record_thread('signer-identity')
        return self.provider.signing_target

    @property
    def signer(self) -> _DirectSigner:
        """Expose a deterministic signer required by the Signing ABC."""
        return self._signer


class _ImpersonationSignedUrlBlob(_SignedUrlBlob):
    """URL double exercising nested target signing in the provider worker."""

    def generate_signed_url(self, **kwargs: object) -> str:
        """Use the selected target signer without modeling IAM retries."""
        credentials = kwargs['credentials']
        assert credentials is self.provider.target_credentials
        assert credentials.signer_email == self.provider.signing_target
        credentials.sign_bytes(b'canonical-request')
        return super().generate_signed_url(**kwargs)


class _ImpersonationProvider(_SignedUrlProvider):
    """ADC, IAM impersonation, and storage tree with exact call capture."""

    def __init__(
        self,
        *,
        source_valid: bool = False,
        adc_error: BaseException | None = None,
        source_refresh_error: BaseException | None = None,
        target_refresh_error: BaseException | None = None,
    ) -> None:
        """Configure one deterministic impersonation lifecycle."""
        self.signing_target = VALID_SIGNING_ACCOUNT
        self.adc_error = adc_error
        self.impersonation_calls: list[
            tuple[tuple[object, ...], dict[str, object]]
        ] = []
        source = _ImpersonationSourceCredentials(
            self,
            valid=source_valid,
            refresh_error=source_refresh_error,
        )
        super().__init__(source)
        self.source_credentials = source
        self.target_credentials = _ImpersonatedSigningCredentials(
            self, refresh_error=target_refresh_error)
        self.blob = _ImpersonationSignedUrlBlob(self)

    def adc(self, *args: object, **kwargs: object) -> tuple[object, str]:
        """Return source ADC or raise one deterministic discovery failure."""
        self.record_thread('adc')
        if self.adc_error is not None:
            raise self.adc_error
        return self.source_credentials, 'source-adc-project'

    def make_impersonated_credentials(
        self,
        *args: object,
        **kwargs: object,
    ) -> _ImpersonatedSigningCredentials:
        """Capture the public google-auth constructor contract off-loop."""
        self.record_thread('impersonated-constructor')
        self.impersonation_calls.append((args, dict(kwargs)))
        return self.target_credentials


def _install_impersonation_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: _ImpersonationProvider,
) -> '_UploadBreaker':
    """Install deterministic ADC, IAM, and storage provider boundaries."""
    breaker = _install_signed_url_provider(monkeypatch, provider)
    monkeypatch.setattr(
        impersonated_credentials,
        'Credentials',
        provider.make_impersonated_credentials,
    )
    return breaker


@pytest.mark.parametrize(
    'method',
    [pytest.param('GET', id='get'), pytest.param('PUT', id='put')],
)
async def test_impersonation_uses_scoped_target_and_closes_before_publish(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """Validated GET/PUT impersonation uses one refreshed signing target."""
    provider = _ImpersonationProvider()
    breaker = _install_impersonation_provider(monkeypatch, provider)
    loop_thread = threading.get_ident()
    info: dict[str, object] = {
        'command': 'signed_url',
        'method': method,
        'signing_service_account': provider.signing_target,
        'timeout': 2.5,
    }
    if method == 'PUT':
        info.update({
            'content_type': 'application/octet-stream',
            'max_upload_bytes': 17,
            'if_generation_match': 9,
        })
    response = _response('gs://Signed-Bucket/folder/object.bin')
    provider.response = response
    strategy = GcsRequest(
        'gs://Signed-Bucket/folder/object.bin',
        None,
        response,
        _validate(info),
        redact_params=frozenset(),
    )

    result = await strategy.handle_request()

    assert not isinstance(
        provider.source_credentials, google_auth_credentials.Signing)
    assert provider.impersonation_calls == [(
        (),
        {
            'source_credentials': provider.source_credentials,
            'target_principal': provider.signing_target,
            'target_scopes': (
                'https://www.googleapis.com/auth/cloud-platform',
            ),
        },
    )]
    assert provider.timeline.count('source-refresh') == 1
    assert provider.timeline.count('target-refresh') == 1
    assert provider.timeline.index('source-refresh') \
        < provider.timeline.index('impersonated-constructor') \
        < provider.timeline.index('target-refresh') \
        < provider.timeline.index('client') \
        < provider.timeline.index('generate') \
        < provider.timeline.index('client-close')
    assert provider.client_calls == [(
        (),
        {
            'credentials': provider.target_credentials,
            'project': 'source-adc-project',
        },
    )]
    assert len(provider.generate_calls) == 1
    assert provider.generate_calls[0]['credentials'] \
        is provider.target_credentials
    assert provider.client_open_during_generate == [True]
    assert provider.details_during_generate == [{}]
    assert provider.details_during_close == [{}]
    assert provider.client.close_calls == 1
    assert provider.timeline.index('generate') < provider.timeline.index(
        'client-close')
    assert breaker.calls == 0
    worker_seams = {
        'source-refresh',
        'impersonated-constructor',
        'target-refresh',
        'signer-identity',
        'sign-bytes',
        'generate',
        'client-close',
    }
    observed_worker_seams = {
        seam for seam, _, _ in provider.threads if seam in worker_seams
    }
    assert observed_worker_seams == worker_seams
    assert all(
        thread_id != loop_thread
        and thread_name.startswith('asyncio-gateway-gcs')
        for seam, thread_id, thread_name in provider.threads
        if seam in worker_seams
    )
    assert result is response
    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['protocol_details']['signed_url'] == provider.signed_url
    assert result['protocol_details']['method'] == method


@pytest.mark.parametrize(
    'failure_stage',
    [pytest.param('default', id='default'),
     pytest.param('source-refresh', id='source-refresh')],
)
async def test_impersonation_source_failures_are_config_and_uncounted(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    """ADC discovery and source refresh fail CONFIG before IAM or storage."""
    secret = f'source-secret-agw50-s2-{failure_stage}'
    provider_kwargs: dict[str, object]
    if failure_stage == 'default':
        provider_kwargs = {
            'adc_error': google_auth_exceptions.DefaultCredentialsError(
                secret),
        }
    else:
        provider_kwargs = {
            'source_refresh_error': google_auth_exceptions.RefreshError(
                secret),
        }
    provider = _ImpersonationProvider(**provider_kwargs)
    breaker = _install_impersonation_provider(monkeypatch, provider)

    result = await request(
        'gs://signed-bucket/folder/object.bin',
        protocol='GCS',
        protocol_info={
            'command': 'signed_url',
            'method': 'GET',
            'signing_service_account': provider.signing_target,
            'timeout': 2.5,
        },
    )

    assert result['ok'] is False
    assert result['status_code'] == 400
    assert result['error']['code'] == 'CONFIG'
    assert result['protocol_details'] == {}
    assert provider.timeline.count('adc') == 1
    assert provider.timeline.count('source-refresh') == (
        1 if failure_stage == 'source-refresh' else 0)
    assert provider.impersonation_calls == []
    assert provider.client_calls == []
    assert provider.generate_calls == []
    assert breaker.calls == 0
    assert secret not in repr(result)


async def test_impersonation_target_refresh_error_is_safe_gcs_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unstructured IAM target refresh refusal is GCS_STATUS/502."""
    secret = 'target-refresh-secret-agw50-s2'
    provider = _ImpersonationProvider(
        source_valid=True,
        target_refresh_error=google_auth_exceptions.RefreshError(secret),
    )
    breaker = _install_impersonation_provider(monkeypatch, provider)

    result = await request(
        'gs://signed-bucket/folder/object.bin',
        protocol='GCS',
        protocol_info={
            'command': 'signed_url',
            'method': 'GET',
            'signing_service_account': provider.signing_target,
            'timeout': 2.5,
        },
    )

    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {}
    assert provider.timeline.count('target-refresh') == 1
    assert provider.client_calls == []
    assert provider.generate_calls == []
    assert breaker.calls == 0
    assert secret not in repr(result)
    assert provider.signing_target not in repr(result)


# --- AGW-50 tranche S3A: signed URL failure and cleanup precedence -------


class _SignedUrlFailureBlob(_SignedUrlBlob):
    """Signed-URL boundary double with one scripted generation outcome."""

    def generate_signed_url(self, **kwargs: object) -> str:
        """Capture one gateway invocation, then return or raise its outcome."""
        provider = self.provider
        assert isinstance(provider, _SignedUrlFailureProvider)
        provider.record_thread('generate')
        provider.generate_calls.append(dict(kwargs))
        provider.client_open_during_generate.append(
            not provider.client.closed)
        if isinstance(provider.generation_outcome, BaseException):
            raise provider.generation_outcome
        return provider.generation_outcome


class _SignedUrlFailureClient(_SignedUrlClient):
    """Owned storage client with one deterministic cleanup outcome."""

    def close(self) -> None:
        """Record exactly one close before raising any scripted failure."""
        provider = self.provider
        assert isinstance(provider, _SignedUrlFailureProvider)
        provider.record_thread('client-close')
        self.close_calls += 1
        self.closed = True
        if provider.close_error is not None:
            raise provider.close_error


class _SignedUrlFailureProvider(_SignedUrlProvider):
    """No-network provider tree for signed-URL failure semantics."""

    def __init__(
        self,
        *,
        generation_outcome: str | BaseException | None = None,
        client_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        """Configure independent client, generation, and cleanup outcomes."""
        super().__init__()
        self.generation_outcome = (
            self.signed_url
            if generation_outcome is None else generation_outcome
        )
        self.client_error = client_error
        self.close_error = close_error
        self.blob = _SignedUrlFailureBlob(self)
        self.client = _SignedUrlFailureClient(self)

    def make_client(
        self,
        *args: object,
        **kwargs: object,
    ) -> _SignedUrlFailureClient:
        """Return one owned client or fail before ownership transfers."""
        self.record_thread('client')
        self.client_calls.append((args, dict(kwargs)))
        if self.client_error is not None:
            raise self.client_error
        return self.client


def _google_service_failure(
    status: int,
    secret: str,
) -> google_api_exceptions.GoogleAPICallError:
    """Build one real Google error with hostile safe-to-drop text."""
    error_type: type[google_api_exceptions.GoogleAPICallError] = (
        google_api_exceptions.Forbidden
        if status == 403
        else google_api_exceptions.ServiceUnavailable
    )
    return error_type(f'authorization=Bearer {secret}')


async def _public_signed_url_failure(
    monkeypatch: pytest.MonkeyPatch,
    provider: _SignedUrlFailureProvider,
) -> tuple[dict[str, Any], '_UploadBreaker']:
    """Drive one GET through the public request conversion boundary."""
    breaker = _install_signed_url_provider(monkeypatch, provider)
    result = await request(
        'gs://Signed-Url-Failure-Bucket/folder/object.bin',
        protocol='GCS',
        protocol_info={
            'command': 'signed_url',
            'method': 'GET',
            'timeout': 2.5,
        },
    )
    return result, breaker


def _expected_signed_service_details(status: int) -> dict[str, object]:
    """Return the exact safe public details for one signing refusal."""
    return {
        'command': 'signed_url',
        'bucket': 'signed-url-failure-bucket',
        'target': 'folder/object.bin',
        'gcs_error_code': None,
        'gcs_error_message': None,
        'response_metadata': {
            'http_status_code': status,
            'request_id': None,
        },
    }


@pytest.mark.parametrize(
    ('failure_kind', 'expected_code', 'expected_status'),
    [
        pytest.param('credential', 'CONFIG', 400, id='credential'),
        pytest.param('service', 'GCS_STATUS', 403, id='google-service'),
        pytest.param('transport', 'DNS', 502, id='transport'),
    ],
)
async def test_signed_url_client_failure_never_closes_unowned_client(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
    expected_code: str,
    expected_status: int,
) -> None:
    """Client construction failures are typed without fabricated cleanup."""
    secret = f'client-construction-private-s3a-{failure_kind}'
    failures: dict[str, BaseException] = {
        'credential': google_auth_exceptions.DefaultCredentialsError(secret),
        'service': _google_service_failure(403, secret),
        'transport': socket.gaierror(secret),
    }
    provider = _SignedUrlFailureProvider(
        client_error=failures[failure_kind])
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, breaker = await _public_signed_url_failure(
        monkeypatch, provider)

    assert result['ok'] is False
    assert result['status_code'] == expected_status
    assert result['error']['code'] == expected_code
    assert result['protocol_details'] == (
        _expected_signed_service_details(403)
        if failure_kind == 'service' else {}
    )
    assert provider.timeline.count('client') == 1
    assert provider.generate_calls == []
    assert provider.client.close_calls == 0
    assert breaker.calls == 0
    assert secret not in repr(result) + _logged_gcs_surfaces(caplog)
    assert getattr(gcs_client, '_active_gcs_leases')() == 0


@pytest.mark.parametrize(
    ('failure_kind', 'expected_code', 'expected_status', 'service_status'),
    [
        pytest.param('forbidden', 'GCS_STATUS', 403, 403,
                     id='google-service-403'),
        pytest.param('unavailable', 'GCS_STATUS', 503, 503,
                     id='google-service-503-no-retry'),
        pytest.param('auth-refresh', 'GCS_STATUS', 502, None,
                     id='iam-refresh'),
        pytest.param('auth-transport', 'TRANSPORT', 502, None,
                     id='google-auth-transport'),
        pytest.param('dns', 'DNS', 502, None, id='dns'),
        pytest.param('tls', 'TLS', 502, None, id='tls'),
        pytest.param('connect', 'CONNECT', 502, None, id='connect'),
        pytest.param('timeout', 'TIMEOUT', 504, None, id='timeout'),
        pytest.param('transport', 'TRANSPORT', 502, None, id='transport'),
    ],
)
async def test_signed_url_generation_failure_is_typed_once_and_uncounted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
    expected_code: str,
    expected_status: int,
    service_status: int | None,
) -> None:
    """Signing service, IAM, and transport failures never retry or leak."""
    secret = f'generation-private-s3a-{failure_kind}'
    failures: dict[str, BaseException] = {
        'forbidden': _google_service_failure(403, secret),
        'unavailable': _google_service_failure(503, secret),
        'auth-refresh': google_auth_exceptions.RefreshError(secret),
        'auth-transport': google_auth_exceptions.TransportError(secret),
        'dns': socket.gaierror(secret),
        'tls': ssl.SSLError(secret),
        'connect': ConnectionError(secret),
        'timeout': TimeoutError(secret),
        'transport': OSError(secret),
    }
    provider = _SignedUrlFailureProvider(
        generation_outcome=failures[failure_kind])
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, breaker = await _public_signed_url_failure(
        monkeypatch, provider)

    assert result['ok'] is False
    assert result['status_code'] == expected_status
    assert result['error']['code'] == expected_code
    assert result['protocol_details'] == (
        _expected_signed_service_details(service_status)
        if service_status is not None else {}
    )
    assert len(provider.generate_calls) == 1
    assert provider.timeline.count('generate') == 1
    assert provider.client.close_calls == 1
    assert breaker.calls == 0
    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    assert secret not in surfaces
    assert provider.signed_url not in surfaces
    assert getattr(gcs_client, '_active_gcs_leases')() == 0


async def test_signed_url_service_failure_wins_over_hostile_close(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A known signing refusal remains the body outcome after close fails."""
    body_secret = 'signed-body-private-s3a'
    close_secret = 'signed-close-private-s3a'
    provider = _SignedUrlFailureProvider(
        generation_outcome=_google_service_failure(403, body_secret),
        close_error=RuntimeError(close_secret),
    )
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, breaker = await _public_signed_url_failure(
        monkeypatch, provider)

    assert result['ok'] is False
    assert result['status_code'] == 403
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == _expected_signed_service_details(403)
    assert len(provider.generate_calls) == 1
    assert provider.client.close_calls == 1
    assert breaker.calls == 0
    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    assert body_secret not in surfaces
    assert close_secret not in surfaces
    assert provider.signed_url not in surfaces


async def test_signed_url_programming_body_identity_wins_over_close_defect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first unknown body defect retains identity and exception state."""
    cause = ValueError('signed-body-programming-cause-s3a')
    body_error = RuntimeError('signed-body-programming-defect-s3a')
    body_error.__cause__ = cause
    body_error.__suppress_context__ = True
    close_error = RuntimeError('signed-close-programming-defect-s3a')
    provider = _SignedUrlFailureProvider(
        generation_outcome=body_error,
        close_error=close_error,
    )
    breaker = _install_signed_url_provider(monkeypatch, provider)
    response = _response(
        'gs://signed-url-failure-bucket/folder/object.bin')
    strategy = GcsRequest(
        'gs://signed-url-failure-bucket/folder/object.bin',
        None,
        response,
        _validate({'command': 'signed_url', 'method': 'GET'}),
        redact_params=frozenset(),
    )

    with pytest.raises(RuntimeError) as caught:
        await strategy.handle_request()

    assert caught.value is body_error
    assert caught.value.__cause__ is cause
    assert caught.value.__suppress_context__ is True
    assert response['protocol_details'] == {}
    assert len(provider.generate_calls) == 1
    assert provider.client.close_calls == 1
    assert breaker.calls == 0
    assert getattr(gcs_client, '_active_gcs_leases')() == 0


@pytest.mark.parametrize(
    ('failure_kind', 'expected_code', 'expected_status'),
    [
        pytest.param('credential', 'CONFIG', 400, id='credential'),
        pytest.param('service', 'GCS_STATUS', 503, id='google-service'),
        pytest.param('transport', 'DNS', 502, id='transport'),
    ],
)
async def test_signed_url_close_failure_discards_generated_bearer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
    expected_code: str,
    expected_status: int,
) -> None:
    """Recognized cleanup failure is stable and cannot publish its URL."""
    secret = f'cleanup-private-s3a-{failure_kind}'
    failures: dict[str, BaseException] = {
        'credential': google_auth_exceptions.DefaultCredentialsError(secret),
        'service': _google_service_failure(503, secret),
        'transport': socket.gaierror(secret),
    }
    provider = _SignedUrlFailureProvider(
        close_error=failures[failure_kind])
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, breaker = await _public_signed_url_failure(
        monkeypatch, provider)

    assert result['ok'] is False
    assert result['status_code'] == expected_status
    assert result['error']['code'] == expected_code
    assert result['protocol_details'] == {}
    assert len(provider.generate_calls) == 1
    assert provider.client.close_calls == 1
    assert breaker.calls == 0
    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    assert provider.signed_url not in surfaces
    assert secret not in surfaces
    assert getattr(gcs_client, '_active_gcs_leases')() == 0


async def test_signed_url_unknown_close_defect_preserves_identity_without_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown cleanup defects escape unchanged without publishing a URL."""
    cause = ValueError('signed-cleanup-programming-cause-s3a')
    cleanup_error = RuntimeError('signed-cleanup-programming-defect-s3a')
    cleanup_error.__cause__ = cause
    cleanup_error.__suppress_context__ = True
    provider = _SignedUrlFailureProvider(close_error=cleanup_error)
    breaker = _install_signed_url_provider(monkeypatch, provider)
    response = _response(
        'gs://signed-url-failure-bucket/folder/object.bin')
    strategy = GcsRequest(
        'gs://signed-url-failure-bucket/folder/object.bin',
        None,
        response,
        _validate({'command': 'signed_url', 'method': 'GET'}),
        redact_params=frozenset(),
    )

    with pytest.raises(RuntimeError) as caught:
        await strategy.handle_request()

    assert caught.value is cleanup_error
    assert caught.value.__cause__ is cause
    assert caught.value.__suppress_context__ is True
    assert response['protocol_details'] == {}
    assert len(provider.generate_calls) == 1
    assert provider.client.close_calls == 1
    assert breaker.calls == 0
    failure_surfaces = repr(response) + ''.join(
        traceback.format_exception(caught.value))
    assert provider.signed_url not in failure_surfaces
    assert getattr(gcs_client, '_active_gcs_leases')() == 0


# --- AGW-50 tranche S3B1: pre-signing cancellation and timeout drains ----


class _PreSigningLifecycleClient(_SignedUrlClient):
    """Late-created client whose hostile close is externally controlled."""

    def close(self) -> None:
        """Block one off-loop close, then raise a private cleanup defect."""
        provider = self.provider
        assert isinstance(provider, _PreSigningLifecycleProvider)
        provider.record_thread('client-close')
        self.close_calls += 1
        self.closed = True
        provider.close_started.set()
        assert provider.close_release.wait(timeout=1)
        raise RuntimeError(provider.close_sentinel)


class _PreSigningLifecycleProvider(_ImpersonationProvider):
    """Event-driven credential/client tree for pre-signing race tests."""

    def __init__(self, blocked_seam: str) -> None:
        """Block exactly one selected provider seam before URL generation."""
        self.blocked_seam = blocked_seam
        self.operation_started = threading.Event()
        self.operation_release = threading.Event()
        self.operation_started_async = asyncio.Event()
        self.event_loop = asyncio.get_running_loop()
        self.close_started = threading.Event()
        self.close_release = threading.Event()
        self.close_sentinel = (
            f'{blocked_seam}-hostile-close-private-s3b1')
        super().__init__(source_valid=blocked_seam != 'source-refresh')
        self.source_secret = f'{blocked_seam}-source-private-s3b1'
        self.target_secret = f'{blocked_seam}-target-private-s3b1'
        self.source_credentials.private_token = self.source_secret
        self.target_credentials.private_signature = self.target_secret
        self.client = _PreSigningLifecycleClient(self)

    def record_thread(self, seam: str) -> None:
        """Record every seam and block the one selected by the test row."""
        super().record_thread(seam)
        if seam != self.blocked_seam:
            return
        self.operation_started.set()
        self.event_loop.call_soon_threadsafe(
            self.operation_started_async.set)
        assert self.operation_release.wait(timeout=1)


@pytest.mark.parametrize(
    ('blocked_seam', 'outcome'),
    [
        pytest.param('adc', 'cancel', id='adc-cancel'),
        pytest.param('adc', 'timeout', id='adc-timeout'),
        pytest.param('source-refresh', 'cancel', id='source-refresh-cancel'),
        pytest.param('source-refresh', 'timeout',
                     id='source-refresh-timeout'),
        pytest.param('impersonated-constructor', 'cancel',
                     id='impersonated-constructor-cancel'),
        pytest.param('impersonated-constructor', 'timeout',
                     id='impersonated-constructor-timeout'),
        pytest.param('target-refresh', 'cancel', id='target-refresh-cancel'),
        pytest.param('target-refresh', 'timeout',
                     id='target-refresh-timeout'),
        pytest.param('client', 'cancel', id='late-client-cancel'),
        pytest.param('client', 'timeout', id='late-client-timeout'),
    ],
)
async def test_signed_url_pre_signing_drain_retains_capacity_and_outcome(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    blocked_seam: str,
    outcome: str,
) -> None:
    """Credential/client races drain without signing or secret publication."""
    provider = _PreSigningLifecycleProvider(blocked_seam)
    breaker = _install_impersonation_provider(monkeypatch, provider)
    expiry_injected = asyncio.Event()
    if outcome == 'timeout':
        real_wait_for = asyncio.wait_for
        expired = False

        async def expire_selected_provider_wait_once(
            awaitable: Any,
            *,
            timeout: int | float,
        ) -> Any:
            """Expire result acceptance only after the selected seam starts."""
            nonlocal expired
            if expired:
                return await real_wait_for(awaitable, timeout=timeout)
            operation = asyncio.ensure_future(awaitable)
            started = asyncio.create_task(
                provider.operation_started_async.wait())
            done, _ = await asyncio.wait(
                {operation, started},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                started.cancel()
                await asyncio.gather(started, return_exceptions=True)
                return operation.result()
            expired = True
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            expiry_injected.set()
            raise asyncio.TimeoutError

        monkeypatch.setattr(
            gcs_client.asyncio, 'wait_for',
            expire_selected_provider_wait_once)

    captured_responses: list[GatewayResponse] = []

    async def capture_response(*, response: GatewayResponse) -> str:
        """Retain the live public envelope without changing dispatch state."""
        captured_responses.append(response)
        return 'captured-before-gcs-dispatch'

    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    loop_thread = threading.get_ident()
    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    companion_leases = [acquire() for _ in range(3)]
    target = 'gs://pre-signing-lifecycle-bucket/folder/object.bin'
    task = asyncio.create_task(request(
        target,
        protocol='GCS',
        protocol_info={
            'command': 'signed_url',
            'method': 'GET',
            'signing_service_account': provider.signing_target,
            'timeout': 1,
        },
        pre_processor_config={'function': capture_response},
    ))
    admitted_after: Any = None
    try:
        await _wait_for_thread_event(provider.operation_started)
        selected_threads = [
            (thread_id, thread_name)
            for seam, thread_id, thread_name in provider.threads
            if seam == blocked_seam
        ]
        assert len(selected_threads) == 1
        assert selected_threads[0][0] != loop_thread
        assert selected_threads[0][1].startswith('asyncio-gateway-gcs')

        if outcome == 'cancel':
            task.cancel('first-pre-signing-cancellation')
            await asyncio.sleep(0)
        else:
            await expiry_injected.wait()
        assert getattr(gcs_client, '_active_gcs_leases')() == 4
        with pytest.raises(GcsCapacityError):
            acquire()

        provider.operation_release.set()
        if blocked_seam == 'client':
            await _wait_for_thread_event(provider.close_started)
            assert getattr(gcs_client, '_active_gcs_leases')() == 4
            with pytest.raises(GcsCapacityError):
                acquire()
            if outcome == 'cancel':
                task.cancel('repeated-pre-signing-cancellation')
                await asyncio.sleep(0)
            provider.close_release.set()

        if outcome == 'cancel':
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args == ('first-pre-signing-cancellation',)
            outcome_surfaces = ''.join((
                ''.join(traceback.format_exception(caught.value)),
                repr(caught.value.__cause__),
                repr(caught.value.__context__),
            ))
        else:
            result = await task
            assert result['ok'] is False
            assert result['status_code'] == 504
            assert result['error']['code'] == 'TIMEOUT'
            assert result['protocol_details'] == {}
            outcome_surfaces = repr(result)

        assert len(captured_responses) == 1
        captured = captured_responses[0]
        assert captured['url'] == target
        assert captured['protocol_details'] == {}
        assert provider.generate_calls == []
        assert provider.timeline.count('generate') == 0
        assert breaker.calls == 0
        if blocked_seam == 'client':
            assert len(provider.client_calls) == 1
            assert provider.client.close_calls == 1
            assert provider.client.closed is True
            assert provider.timeline.index('client') < provider.timeline.index(
                'client-close')
            close_threads = [
                (thread_id, thread_name)
                for seam, thread_id, thread_name in provider.threads
                if seam == 'client-close'
            ]
            assert len(close_threads) == 1
            assert close_threads[0][0] != loop_thread
            assert close_threads[0][1].startswith('asyncio-gateway-gcs')
        else:
            assert provider.client_calls == []
            assert provider.client.close_calls == 0

        public_surfaces = ''.join((
            repr(captured),
            outcome_surfaces,
            repr(breaker.__dict__),
            _logged_gcs_surfaces(caplog),
        ))
        for sentinel in (
            provider.source_secret,
            provider.target_secret,
            provider.signing_target,
            provider.close_sentinel,
            provider.signed_url,
        ):
            assert sentinel not in public_surfaces
        assert getattr(gcs_client, '_active_gcs_leases')() == 3

        admitted_after = acquire()
        with pytest.raises(GcsCapacityError):
            acquire()
    finally:
        provider.operation_release.set()
        provider.close_release.set()
        await asyncio.gather(task, return_exceptions=True)
        if admitted_after is not None:
            await _release_lease(admitted_after)
        for lease in companion_leases:
            await _release_lease(lease)


# --- AGW-50 tranche S3B2: generation and close lifecycle drains ---------


class _PostSigningLifecycleBlob(_SignedUrlBlob):
    """Event-driven URL generator for post-signing lifecycle races."""

    def generate_signed_url(self, **kwargs: object) -> str:
        """Generate exactly once, optionally blocking until cancellation."""
        provider = self.provider
        assert isinstance(provider, _PostSigningLifecycleProvider)
        provider.record_thread('generate')
        provider.generate_calls.append(dict(kwargs))
        provider.client_open_during_generate.append(
            not provider.client.closed)
        assert provider.response is not None
        provider.details_during_generate.append(dict(
            provider.response['protocol_details']))
        provider.generate_started.set()
        if provider.blocked_phase == 'generate':
            provider.mark_outcome_started()
            assert provider.generate_release.wait(timeout=1)
        return provider.signed_url


class _PostSigningLifecycleClient(_SignedUrlClient):
    """Owned client whose close remains observable until externally freed."""

    def close(self) -> None:
        """Block off-loop, then finish or raise one hostile late failure."""
        provider = self.provider
        assert isinstance(provider, _PostSigningLifecycleProvider)
        provider.record_thread('client-close')
        assert provider.response is not None
        provider.details_during_close.append(dict(
            provider.response['protocol_details']))
        self.close_calls += 1
        provider.close_started.set()
        if provider.blocked_phase == 'client-close':
            provider.mark_outcome_started()
        assert provider.close_release.wait(timeout=1)
        self.closed = True
        if provider.close_error is not None:
            raise provider.close_error


class _PostSigningLifecycleProvider(_SignedUrlProvider):
    """No-network signing tree with generation and close event controls."""

    def __init__(
        self,
        blocked_phase: str,
        close_failure_kind: str,
    ) -> None:
        """Configure the selected race and any later cleanup failure."""
        self.blocked_phase = blocked_phase
        self.event_loop = asyncio.get_running_loop()
        self.outcome_started = threading.Event()
        self.outcome_started_async = asyncio.Event()
        self.generate_started = threading.Event()
        self.generate_release = threading.Event()
        self.close_started = threading.Event()
        self.close_release = threading.Event()
        super().__init__()
        self.credential_sentinel = (
            f'{blocked_phase}-{close_failure_kind}-credential-private-s3b2')
        self.bearer_sentinel = (
            f'{blocked_phase}-{close_failure_kind}-bearer-private-s3b2')
        self.close_sentinel = (
            f'{blocked_phase}-{close_failure_kind}-close-private-s3b2')
        self.signed_url = (
            'https://storage.googleapis.com/private-s3b2/object.bin'
            f'?X-Goog-Credential={self.credential_sentinel}'
            f'&X-Goog-Signature={self.bearer_sentinel}')
        self.credentials.private_token = self.credential_sentinel
        self.close_error: BaseException | None = None
        if close_failure_kind == 'recognized':
            self.close_error = google_auth_exceptions.DefaultCredentialsError(
                self.close_sentinel)
        elif close_failure_kind == 'unknown':
            self.close_error = RuntimeError(self.close_sentinel)
        self.blob = _PostSigningLifecycleBlob(self)
        self.client = _PostSigningLifecycleClient(self)

    def mark_outcome_started(self) -> None:
        """Signal both worker and event-loop observers of the selected race."""
        self.outcome_started.set()
        self.event_loop.call_soon_threadsafe(
            self.outcome_started_async.set)


def _inject_post_signing_timeout(
    monkeypatch: pytest.MonkeyPatch,
    provider: _PostSigningLifecycleProvider,
) -> asyncio.Event:
    """Expire exactly the provider wait blocked at the selected phase."""
    real_wait_for = asyncio.wait_for
    expiry_injected = asyncio.Event()
    expired = False

    async def expire_selected_wait_once(
        awaitable: Any,
        *,
        timeout: int | float,
    ) -> Any:
        """Reject only the result whose selected worker seam has started."""
        nonlocal expired
        if expired:
            return await real_wait_for(awaitable, timeout=timeout)
        operation = asyncio.ensure_future(awaitable)
        started = asyncio.create_task(
            provider.outcome_started_async.wait())
        done, _ = await asyncio.wait(
            {operation, started},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation in done:
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
            return operation.result()
        expired = True
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        expiry_injected.set()
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        gcs_client.asyncio, 'wait_for', expire_selected_wait_once)
    return expiry_injected


@pytest.mark.parametrize(
    (
        'blocked_phase', 'outcome', 'close_failure_kind',
        'repeat_cancellation',
    ),
    [
        pytest.param(
            'generate', 'cancel', 'success', False,
            id='generate-cancel-late-url'),
        pytest.param(
            'generate', 'timeout', 'success', False,
            id='generate-timeout-late-url'),
        pytest.param(
            'client-close', 'cancel', 'recognized', False,
            id='close-cancel-recognized-failure'),
        pytest.param(
            'client-close', 'cancel', 'unknown', True,
            id='close-repeated-cancel-unknown-failure'),
        pytest.param(
            'client-close', 'timeout', 'recognized', False,
            id='close-timeout-recognized-failure'),
        pytest.param(
            'client-close', 'timeout', 'unknown', False,
            id='close-timeout-unknown-failure'),
    ],
)
async def test_signed_url_generation_and_close_drain_never_publish_bearer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    blocked_phase: str,
    outcome: str,
    close_failure_kind: str,
    repeat_cancellation: bool,
) -> None:
    """First cancel/deadline wins while private URL work drains and closes."""
    provider = _PostSigningLifecycleProvider(
        blocked_phase, close_failure_kind)
    breaker = _install_signed_url_provider(monkeypatch, provider)
    expiry_injected = (
        _inject_post_signing_timeout(monkeypatch, provider)
        if outcome == 'timeout' else None
    )
    captured_responses: list[GatewayResponse] = []

    async def capture_response(*, response: GatewayResponse) -> str:
        """Retain the live envelope to detect any late URL publication."""
        captured_responses.append(response)
        provider.response = response
        return 'captured-before-post-signing-race'

    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    loop_thread = threading.get_ident()
    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    companion_leases = [acquire() for _ in range(3)]
    target = 'gs://post-signing-lifecycle-bucket/folder/object.bin'
    task = asyncio.create_task(request(
        target,
        protocol='GCS',
        protocol_info={
            'command': 'signed_url',
            'method': 'GET',
            'timeout': 1,
        },
        pre_processor_config={'function': capture_response},
    ))
    admitted_after: Any = None
    try:
        await _wait_for_thread_event(provider.outcome_started)
        if outcome == 'cancel':
            task.cancel('first-post-signing-cancellation')
            await asyncio.sleep(0)
            if repeat_cancellation:
                task.cancel('repeated-post-signing-cancellation')
                await asyncio.sleep(0)
        else:
            assert expiry_injected is not None
            await expiry_injected.wait()

        assert getattr(gcs_client, '_active_gcs_leases')() == 4
        with pytest.raises(GcsCapacityError):
            acquire()

        provider.generate_release.set()
        await _wait_for_thread_event(provider.close_started)
        assert captured_responses[0]['protocol_details'] == {}
        assert getattr(gcs_client, '_active_gcs_leases')() == 4
        with pytest.raises(GcsCapacityError):
            acquire()

        provider.close_release.set()
        if outcome == 'cancel':
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args == (
                'first-post-signing-cancellation',)
            outcome_surfaces = ''.join((
                ''.join(traceback.format_exception(caught.value)),
                repr(caught.value.__cause__),
                repr(caught.value.__context__),
            ))
        else:
            result = await task
            assert result['ok'] is False
            assert result['status_code'] == 504
            assert result['error']['code'] == 'TIMEOUT'
            assert result['protocol_details'] == {}
            outcome_surfaces = repr(result)

        await asyncio.sleep(0)
        assert len(captured_responses) == 1
        captured = captured_responses[0]
        assert captured['url'] == target
        assert captured['protocol_details'] == {}
        assert len(provider.generate_calls) == 1
        assert provider.timeline.count('generate') == 1
        assert provider.client_open_during_generate == [True]
        assert provider.details_during_generate == [{}]
        assert provider.details_during_close == [{}]
        assert provider.client.close_calls == 1
        assert provider.client.closed is True
        assert provider.timeline.index('generate') < provider.timeline.index(
            'client-close')
        assert breaker.calls == 0
        close_threads = [
            (thread_id, thread_name)
            for seam, thread_id, thread_name in provider.threads
            if seam == 'client-close'
        ]
        assert len(close_threads) == 1
        assert close_threads[0][0] != loop_thread
        assert close_threads[0][1].startswith('asyncio-gateway-gcs')
        public_surfaces = ''.join((
            repr(captured),
            outcome_surfaces,
            repr(breaker.__dict__),
            _logged_gcs_surfaces(caplog),
        ))
        for sentinel in (
            provider.signed_url,
            provider.bearer_sentinel,
            provider.credential_sentinel,
            provider.close_sentinel,
        ):
            assert sentinel not in public_surfaces
        assert task.done()
        assert getattr(gcs_client, '_active_gcs_leases')() == 3

        admitted_after = acquire()
        with pytest.raises(GcsCapacityError):
            acquire()
    finally:
        provider.generate_release.set()
        provider.close_release.set()
        await asyncio.gather(task, return_exceptions=True)
        if admitted_after is not None:
            await _release_lease(admitted_after)
        for lease in companion_leases:
            await _release_lease(lease)


# --- AGW-50 tranche S3B3: public capacity and bearer containment ---------


_GCS_CAPACITY_EVENT_NAMES = frozenset({
    'gcs_capacity_state',
    'gcs_capacity_rejected',
    'gcs_capacity_closing',
    'gcs_drain_started',
    'gcs_drain_finished',
})


def _capacity_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    """Return only private GCS capacity and drain telemetry records."""
    return [
        record for record in caplog.records
        if record.name == gcs_client.__name__
        and record.getMessage() in _GCS_CAPACITY_EVENT_NAMES
    ]


def _surface_leaves(
    value: object,
    path: tuple[object, ...] = (),
) -> list[tuple[tuple[object, ...], object]]:
    """Return every structural leaf and its path from one public surface."""
    if isinstance(value, Mapping):
        leaves: list[tuple[tuple[object, ...], object]] = []
        for key, child in value.items():
            leaves.extend(_surface_leaves(child, (*path, key)))
        return leaves
    if isinstance(value, (list, tuple)):
        leaves = []
        for index, child in enumerate(value):
            leaves.extend(_surface_leaves(child, (*path, index)))
        return leaves
    return [(path, value)]


async def test_public_signed_url_saturation_is_safe_and_recovers_exactly(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fifth public request fails locally and leaves all permits reusable."""
    provider = _SignedUrlProvider()
    provider.signed_url = (
        'https://storage.googleapis.com/capacity-bucket/object.bin'
        '?X-Goog-Credential=capacity-private-credential'
        '&X-Goog-Signature=capacity-private-signature')
    breaker = _install_signed_url_provider(monkeypatch, provider)
    breaker_lookups: list[tuple[object, ...]] = []

    def record_breaker(*args: object, **kwargs: object) -> object:
        """Capture bucket identity without executing the returned breaker."""
        breaker_lookups.append((*args, kwargs))
        return breaker

    monkeypatch.setattr(base, 'get_breaker', record_breaker)
    captured: list[GatewayResponse] = []

    async def capture_response(*, response: GatewayResponse) -> str:
        """Expose the live envelope to the deterministic provider double."""
        captured.append(response)
        provider.response = response
        return 'captured-before-capacity-admission'

    target = 'gs://capacity-bucket/capacity-private-target'
    signing_target = (
        'capacity-signer@example-project.iam.gserviceaccount.com')
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    leases = [acquire() for _ in range(4)]
    try:
        result = await request(
            target,
            protocol='GCS',
            protocol_info={
                'command': 'signed_url',
                'method': 'GET',
                'signing_service_account': signing_target,
                'timeout': 2.5,
            },
            pre_processor_config={'function': capture_response},
        )

        assert result['ok'] is False
        assert result['status_code'] == 503
        assert result['error']['code'] == 'GCS_CAPACITY'
        assert result['protocol_details'] == {}
        assert result['url'] == target
        assert getattr(gcs_client, '_active_gcs_leases')() == 4
        assert provider.timeline == []
        assert provider.client_calls == []
        assert provider.generate_calls == []
        assert provider.client.close_calls == 0
        assert breaker.calls == 0
        assert len(breaker_lookups) == 1

        rejections = [
            record for record in _capacity_records(caplog)
            if record.getMessage() == 'gcs_capacity_rejected'
        ]
        assert [
            (record.active_leases, record.reason)
            for record in rejections
        ] == [(4, 'saturated')]
        safe_error = repr(result['error']) + repr(
            result['protocol_details'])
        telemetry = ''.join(
            repr(record.__dict__) for record in _capacity_records(caplog))
        for secret in (
            'capacity-private-target',
            signing_target,
            provider.signed_url,
            'capacity-private-credential',
            'capacity-private-signature',
        ):
            assert secret not in safe_error
            assert secret not in telemetry
    finally:
        for lease in leases:
            await _release_lease(lease)

    assert getattr(gcs_client, '_active_gcs_leases')() == 0
    recovered = await request(
        target,
        protocol='GCS',
        protocol_info={
            'command': 'signed_url',
            'method': 'GET',
            'timeout': 2.5,
        },
        pre_processor_config={'function': capture_response},
    )
    assert recovered['ok'] is True
    assert recovered['protocol_details']['signed_url'] == provider.signed_url
    assert provider.timeline.count('generate') == 1
    assert provider.client.close_calls == 1
    assert breaker.calls == 0
    assert getattr(gcs_client, '_active_gcs_leases')() == 0

    reprobe = [acquire() for _ in range(4)]
    try:
        with pytest.raises(GcsCapacityError):
            acquire()
    finally:
        for lease in reprobe:
            await _release_lease(lease)
    assert getattr(gcs_client, '_active_gcs_leases')() == 0


async def test_public_signed_url_closing_refusal_is_safe_and_restorable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reversible admission-close probe refuses before provider work."""
    provider = _SignedUrlProvider()
    provider.signed_url = (
        'https://storage.googleapis.com/closing-bucket/object.bin'
        '?X-Goog-Credential=closing-private-credential'
        '&X-Goog-Signature=closing-private-signature')
    breaker = _install_signed_url_provider(monkeypatch, provider)
    signing_target = (
        'closing-signer@example-project.iam.gserviceaccount.com')
    target = 'gs://closing-bucket/closing-private-target'
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    state_lock = getattr(gcs_client, '_GCS_STATE_LOCK')
    with state_lock:
        original_closing = getattr(gcs_client, '_GCS_ADMISSION_CLOSING')
        original_shutdown = getattr(gcs_client, '_GCS_EXECUTOR_SHUTDOWN')
        assert original_closing is False
        assert original_shutdown is False
        setattr(gcs_client, '_GCS_ADMISSION_CLOSING', True)
    try:
        result = await request(
            target,
            protocol='GCS',
            protocol_info={
                'command': 'signed_url',
                'method': 'GET',
                'signing_service_account': signing_target,
                'timeout': 2.5,
            },
        )
    finally:
        with state_lock:
            setattr(
                gcs_client, '_GCS_ADMISSION_CLOSING', original_closing)

    assert result['ok'] is False
    assert result['status_code'] == 503
    assert result['error']['code'] == 'GCS_CAPACITY'
    assert result['protocol_details'] == {}
    assert result['url'] == target
    assert provider.timeline == []
    assert provider.client_calls == []
    assert provider.generate_calls == []
    assert provider.client.close_calls == 0
    assert breaker.calls == 0
    assert getattr(gcs_client, '_GCS_EXECUTOR_SHUTDOWN') is original_shutdown

    rejections = [
        record for record in _capacity_records(caplog)
        if record.getMessage() == 'gcs_capacity_rejected'
    ]
    assert [
        (record.active_leases, record.reason)
        for record in rejections
    ] == [(0, 'closing')]
    telemetry = ''.join(
        repr(record.__dict__) for record in _capacity_records(caplog))
    for secret in (
        'closing-private-target',
        signing_target,
        provider.signed_url,
        'closing-private-credential',
        'closing-private-signature',
    ):
        assert secret not in repr(result['error'])
        assert secret not in telemetry

    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    reprobe = [acquire() for _ in range(4)]
    try:
        with pytest.raises(GcsCapacityError):
            acquire()
    finally:
        for lease in reprobe:
            await _release_lease(lease)
    assert getattr(gcs_client, '_active_gcs_leases')() == 0


@pytest.mark.parametrize(
    ('method', 'expiry', 'put_options', 'expected_required_headers'),
    [
        pytest.param('GET', 3600, {}, None, id='get'),
        pytest.param(
            'PUT',
            321,
            {
                'content_type': 'application/octet-stream',
                'max_upload_bytes': 17,
                'if_generation_match': 7,
            },
            {
                'content-type': 'application/octet-stream',
                'x-goog-content-length-range': '1,17',
                'x-goog-if-generation-match': '7',
            },
            id='put',
        ),
    ],
)
async def test_successful_public_signed_url_has_one_bearer_surface_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    method: str,
    expiry: int,
    put_options: dict[str, object],
    expected_required_headers: dict[str, str] | None,
) -> None:
    """GET and PUT publish one bearer leaf and no diagnostic copy."""
    provider = _SignedUrlProvider()
    credential_fragment = (
        f'surface-{method.lower()}%40example-project.iam.gserviceaccount.com'
        '%2F20260822%2Fauto%2Fstorage%2Fgoog4_request')
    signature_fragment = f'0123456789abcdef-{method.lower()}-signature'
    provider.signed_url = (
        'https://storage.googleapis.com/bearer-surface-bucket/'
        'folder/object.bin?X-Goog-Algorithm=GOOG4-RSA-SHA256'
        f'&X-Goog-Credential={credential_fragment}'
        '&X-Goog-Date=20260822T120000Z'
        f'&X-Goog-Expires={expiry}'
        '&X-Goog-SignedHeaders=host'
        f'&X-Goog-Signature={signature_fragment}')
    breaker = _install_signed_url_provider(monkeypatch, provider)
    breaker_lookups: list[tuple[object, ...]] = []

    def record_breaker(*args: object, **kwargs: object) -> object:
        """Capture the bucket-only lookup for secret-surface inspection."""
        breaker_lookups.append((*args, kwargs))
        return breaker

    monkeypatch.setattr(base, 'get_breaker', record_breaker)

    async def capture_response(*, response: GatewayResponse) -> str:
        """Expose pre-publication state to the deterministic SDK double."""
        provider.response = response
        return 'captured-before-bearer-publication'

    target = 'gs://bearer-surface-bucket/folder/object.bin'
    info: dict[str, object] = {
        'command': 'signed_url',
        'method': method,
        'expires_in_seconds': expiry,
        'timeout': 2.5,
        **put_options,
    }
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    result = await request(
        target,
        protocol='GCS',
        protocol_info=info,
        pre_processor_config={'function': capture_response},
    )

    expected_details: dict[str, object] = {
        'command': 'signed_url',
        'method': method,
        'bucket': 'bearer-surface-bucket',
        'key': 'folder/object.bin',
        'expires_in_seconds': expiry,
        'signed_url': provider.signed_url,
    }
    if method == 'PUT':
        expected_details.update(put_options)
        expected_details['required_headers'] = expected_required_headers

    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['url'] == target
    assert result['error'] is None
    assert result['protocol_details'] == expected_details
    assert [
        path for path, value in _surface_leaves(result)
        if value == provider.signed_url
    ] == [('protocol_details', 'signed_url')]
    assert repr(result).count(provider.signed_url) == 1
    assert len(provider.generate_calls) == 1
    assert provider.generate_calls[0]['method'] == method
    assert provider.generate_calls[0]['expiration'] == timedelta(
        seconds=expiry)
    assert provider.client_open_during_generate == [True]
    assert provider.details_during_generate == [{}]
    assert provider.details_during_close == [{}]
    assert provider.client.close_calls == 1
    assert provider.client.closed is True
    assert provider.timeline.count('generate') == 1
    assert provider.timeline.index('generate') < provider.timeline.index(
        'client-close')
    assert breaker.calls == 0
    assert breaker_lookups == [(
        'gs', 'bearer-surface-bucket', UNKNOWN_PORT, {}, {})]
    assert not [
        record for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert not [
        record for record in _capacity_records(caplog)
        if record.getMessage() in {'gcs_drain_started', 'gcs_drain_finished'}
    ]

    non_bearer_leaves = [
        (path, value) for path, value in _surface_leaves(result)
        if path != ('protocol_details', 'signed_url')
    ]
    other_surfaces = ''.join((
        repr(non_bearer_leaves),
        _logged_gcs_surfaces(caplog),
        repr(breaker_lookups),
        repr(breaker.__dict__),
        repr(result['error']),
    ))
    for secret in (
        provider.signed_url,
        credential_fragment,
        signature_fragment,
        'X-Goog-Credential=',
        'X-Goog-Signature=',
    ):
        assert secret not in other_surfaces
    assert getattr(gcs_client, '_active_gcs_leases')() == 0


# --- AGW-49 foundation: normalized head and one-page list ----------------


class _HeadListBlob:
    """Metadata-bearing blob double shared by head and list fixtures."""

    def __init__(self, provider: '_HeadListProvider') -> None:
        """Retain the recorder and expose one full metadata snapshot."""
        self.provider = provider
        self.name: object = 'prefix/first.txt'
        self.size: object = 17
        self.content_type: object = 'text/plain'
        self.etag: object = 'head-list-etag'
        self.generation: object = 31
        self.metageneration: object = 4
        self.updated: object = datetime(
            2026, 8, 22, 10, 30, 45, tzinfo=timezone.utc)
        self.crc32c: object = 'head-list-crc32c=='
        self.metadata: object = {'owner': 'gateway', 'tier': 'sit'}

    def reload(self, **kwargs: object) -> None:
        """Record one exact metadata fetch and its SDK controls."""
        self.provider.record_thread('reload')
        self.provider.reload_calls.append(dict(kwargs))


class _HeadListBucket:
    """Bucket double returning the exact object selected for head."""

    def __init__(self, provider: '_HeadListProvider') -> None:
        """Retain the shared provider recorder."""
        self.provider = provider
        self.name = 'head-list-bucket'

    def blob(self, key: str) -> _HeadListBlob:
        """Record and return the requested metadata object."""
        self.provider.record_thread('blob')
        self.provider.blob_keys.append(key)
        return self.provider.head_blob

    def list_blobs(self, **kwargs: object) -> '_HeadListIterator':
        """Support the official bucket convenience method equivalently."""
        self.provider.record_thread('list-construction')
        self.provider.list_calls.append((self, dict(kwargs)))
        return self.provider.iterator


class _HeadListPage:
    """One official-SDK-shaped page preserving service item order."""

    def __init__(
        self,
        provider: '_HeadListProvider',
        items: list[_HeadListBlob],
    ) -> None:
        """Retain the exact ordered objects for one page iteration."""
        self.provider = provider
        self.items = tuple(items)

    def __iter__(self) -> Any:
        """Record page iteration and expose only the scripted objects."""
        self.provider.record_thread('page-iteration')
        self.provider.page_iteration_count += 1
        return iter(self.items)


class _OnePageCursor:
    """Pages cursor that rejects any implicit second-page access."""

    def __init__(self, iterator: '_HeadListIterator') -> None:
        """Retain the owning iterator and start before its only page."""
        self.iterator = iterator
        self.page_fetched = False

    def __iter__(self) -> '_OnePageCursor':
        """Return this one-shot pages cursor."""
        return self

    def __next__(self) -> _HeadListPage:
        """Return exactly one page and reject recursive page traversal."""
        if self.page_fetched:
            self.iterator.provider.second_page_accesses += 1
            raise AssertionError('list must not fetch a second service page')
        self.page_fetched = True
        self.iterator.provider.record_thread('page-fetch')
        self.iterator.provider.page_fetch_count += 1
        self.iterator.next_page_token = self.iterator.server_token
        return self.iterator.page


class _HeadListIterator:
    """Official iterator-shaped result from ``Client.list_blobs``."""

    def __init__(
        self,
        provider: '_HeadListProvider',
        items: list[_HeadListBlob],
        server_token: str | None,
    ) -> None:
        """Create one page and expose its token after the page is fetched."""
        self.provider = provider
        self.server_token = server_token
        self.next_page_token: str | None = None
        self.page = _HeadListPage(provider, items)
        self._pages = _OnePageCursor(self)

    @property
    def pages(self) -> _OnePageCursor:
        """Expose the single-page cursor without fetching a page."""
        self.provider.record_thread('pages')
        self.provider.pages_access_count += 1
        return self._pages


class _HeadListClient:
    """Storage client double for one head or list lifecycle."""

    def __init__(self, provider: '_HeadListProvider') -> None:
        """Retain the provider recorder and cleanup count."""
        self.provider = provider
        self.close_calls = 0

    def bucket(self, name: str) -> _HeadListBucket:
        """Record one normalized bucket lookup."""
        self.provider.record_thread('bucket')
        self.provider.bucket_names.append(name)
        return self.provider.bucket

    def list_blobs(
        self,
        bucket: object,
        **kwargs: object,
    ) -> _HeadListIterator:
        """Construct one bounded official-SDK-shaped list iterator."""
        self.provider.record_thread('list-construction')
        self.provider.list_calls.append((bucket, dict(kwargs)))
        return self.provider.iterator

    def close(self) -> None:
        """Record one owned-client cleanup on the provider worker."""
        self.provider.record_thread('client-close')
        self.close_calls += 1


class _HeadListProvider:
    """Deterministic head/list SDK tree with no external I/O."""

    def __init__(
        self,
        *,
        list_items: list[_HeadListBlob] | None = None,
        server_token: str | None = None,
    ) -> None:
        """Create one connected provider tree and page script."""
        self.timeline: list[str] = []
        self.threads: list[tuple[str, int, str]] = []
        self.lease_counts: list[tuple[str, int]] = []
        self.reload_calls: list[dict[str, object]] = []
        self.list_calls: list[tuple[object, dict[str, object]]] = []
        self.bucket_names: list[str] = []
        self.blob_keys: list[str] = []
        self.pages_access_count = 0
        self.page_fetch_count = 0
        self.page_iteration_count = 0
        self.second_page_accesses = 0
        self.credentials = _UploadCredentials()
        self.head_blob = _HeadListBlob(self)
        self.bucket = _HeadListBucket(self)
        self.iterator = _HeadListIterator(
            self,
            list_items or [],
            server_token,
        )
        self.client = _HeadListClient(self)

    def record_thread(self, seam: str) -> None:
        """Record provider thread identity and retained lease count."""
        self.timeline.append(seam)
        self.threads.append((
            seam, threading.get_ident(), threading.current_thread().name))
        self.lease_counts.append((
            seam, getattr(gcs_client, '_active_gcs_leases')()))

    def adc(self, *args: object, **kwargs: object) -> tuple[object, str]:
        """Return valid ADC without network or metadata-server access."""
        self.record_thread('adc')
        return self.credentials, 'head-list-project'

    def make_client(self, *args: object, **kwargs: object) -> _HeadListClient:
        """Return one observable storage client."""
        self.record_thread('client')
        return self.client


def _install_head_list_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: _HeadListProvider,
) -> '_UploadBreaker':
    """Install one deterministic provider and direct breaker."""
    breaker = _UploadBreaker(provider.timeline)
    monkeypatch.setattr(base, 'get_breaker', lambda *args, **kwargs: breaker)
    monkeypatch.setattr(google_auth, 'default', provider.adc)
    monkeypatch.setattr(storage, 'Client', provider.make_client)
    return breaker


@pytest.mark.parametrize(
    ('generation', 'expected_reload'),
    [
        pytest.param(None, {'retry': None, 'timeout': 2.5}, id='latest'),
        pytest.param(
            31,
            {
                'if_generation_match': 31,
                'retry': None,
                'timeout': 2.5,
            },
            id='authoritative-generation',
        ),
    ],
)
async def test_head_fetches_once_off_loop_and_returns_exact_metadata_schema(
    monkeypatch: pytest.MonkeyPatch,
    generation: int | None,
    expected_reload: dict[str, object],
) -> None:
    """Head owns one lease, one reload, exact normalization, and cleanup."""
    provider = _HeadListProvider()
    breaker = _install_head_list_provider(monkeypatch, provider)
    loop_thread = threading.get_ident()
    info: dict[str, object] = {'command': 'head', 'timeout': 2.5}
    if generation is not None:
        info['if_generation_match'] = generation

    result = await request(
        'gs://Head-List-Bucket/folder/object.txt',
        protocol='GCS',
        protocol_info=info,
    )

    assert provider.reload_calls == [expected_reload]
    assert provider.timeline == [
        'breaker', 'adc', 'client', 'bucket', 'blob', 'reload',
        'client-close',
    ]
    assert provider.bucket_names == ['head-list-bucket']
    assert provider.blob_keys == ['folder/object.txt']
    assert provider.client.close_calls == 1
    assert breaker.calls == 1
    assert provider.lease_counts == [
        (seam, 1) for seam, *_ in provider.threads]
    assert all(
        thread_id != loop_thread
        and thread_name.startswith('asyncio-gateway-gcs')
        for _, thread_id, thread_name in provider.threads
    )
    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['protocol_details'] == {
        'command': 'head',
        'bucket': 'head-list-bucket',
        'key': 'folder/object.txt',
        'content_length': 17,
        'content_type': 'text/plain',
        'etag': 'head-list-etag',
        'generation': 31,
        'metageneration': 4,
        'last_modified': '2026-08-22T10:30:45+00:00',
        'crc32c': 'head-list-crc32c==',
        'metadata': {'owner': 'gateway', 'tier': 'sit'},
    }
    assert (
        result['protocol_details']['metadata']
        is not provider.head_blob.metadata
    )


async def test_head_normalizes_absent_optional_metadata_without_schema_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent optional object metadata becomes None or a fresh empty map."""
    provider = _HeadListProvider()
    provider.head_blob.size = None
    provider.head_blob.content_type = None
    provider.head_blob.etag = None
    provider.head_blob.generation = None
    provider.head_blob.metageneration = None
    provider.head_blob.updated = None
    provider.head_blob.crc32c = None
    provider.head_blob.metadata = None
    _install_head_list_provider(monkeypatch, provider)

    result = await request(
        'gs://head-list-bucket/optional.txt',
        protocol='GCS',
        protocol_info={'command': 'head', 'timeout': 2.5},
    )

    assert result['status_code'] == 200
    assert result['protocol_details'] == {
        'command': 'head',
        'bucket': 'head-list-bucket',
        'key': 'optional.txt',
        'content_length': None,
        'content_type': None,
        'etag': None,
        'generation': None,
        'metageneration': None,
        'last_modified': None,
        'crc32c': None,
        'metadata': {},
    }


@pytest.mark.parametrize(
    ('caller_token', 'server_token'),
    [
        pytest.param(
            '  ', 'server-next-page', id='caller-whitespace-preserved'),
        pytest.param(
            '\u00fc' * 2048, '  ',
            id='caller-4096-utf8-bytes-and-server-whitespace',
        ),
    ],
)
async def test_list_fetches_exactly_one_ordered_page_with_opaque_token(
    monkeypatch: pytest.MonkeyPatch,
    caller_token: str,
    server_token: str,
) -> None:
    """List passes the caller token unchanged and never follows the next."""
    provider = _HeadListProvider(server_token=server_token)
    first = _HeadListBlob(provider)
    second = _HeadListBlob(provider)
    second.name = 'prefix/second.bin'
    second.size = 0
    second.content_type = None
    second.etag = None
    second.generation = None
    second.updated = None
    second.crc32c = None
    provider.iterator = _HeadListIterator(
        provider, [first, second], server_token)
    breaker = _install_head_list_provider(monkeypatch, provider)
    loop_thread = threading.get_ident()

    result = await request(
        'gs://Head-List-Bucket/prefix/',
        protocol='GCS',
        protocol_info={
            'command': 'list',
            'max_items': 2,
            'page_token': caller_token,
            'timeout': 2.5,
        },
    )

    assert provider.list_calls == [(provider.bucket, {
        'prefix': 'prefix/',
        'max_results': 2,
        'page_token': caller_token,
        'retry': None,
        'timeout': 2.5,
    })]
    assert provider.timeline == [
        'breaker', 'adc', 'client', 'bucket', 'list-construction',
        'pages', 'page-fetch', 'page-iteration', 'client-close',
    ]
    assert provider.pages_access_count == 1
    assert provider.page_fetch_count == 1
    assert provider.page_iteration_count == 1
    assert provider.second_page_accesses == 0
    assert provider.client.close_calls == 1
    assert breaker.calls == 1
    assert provider.lease_counts == [
        (seam, 1) for seam, *_ in provider.threads]
    assert all(
        thread_id != loop_thread
        and thread_name.startswith('asyncio-gateway-gcs')
        for _, thread_id, thread_name in provider.threads
    )
    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['protocol_details'] == {
        'command': 'list',
        'bucket': 'head-list-bucket',
        'prefix': 'prefix/',
        'items': [
            {
                'key': 'prefix/first.txt',
                'size': 17,
                'content_type': 'text/plain',
                'etag': 'head-list-etag',
                'generation': 31,
                'last_modified': '2026-08-22T10:30:45+00:00',
                'crc32c': 'head-list-crc32c==',
            },
            {
                'key': 'prefix/second.bin',
                'size': 0,
                'content_type': None,
                'etag': None,
                'generation': None,
                'last_modified': None,
                'crc32c': None,
            },
        ],
        'item_count': 2,
        'is_truncated': True,
        'next_page_token': server_token,
    }
    assert 'page_token' not in result['protocol_details']
    assert all(
        value != caller_token
        for value in result['protocol_details'].values()
    )


async def test_list_empty_prefix_returns_one_coherent_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bucket-root listing is bounded, empty, and has no token echo field."""
    provider = _HeadListProvider()
    breaker = _install_head_list_provider(monkeypatch, provider)

    result = await request(
        'gs://Head-List-Bucket',
        protocol='GCS',
        protocol_info={'command': 'list', 'timeout': 2.5},
    )

    assert provider.list_calls == [(provider.bucket, {
        'prefix': '',
        'max_results': 1000,
        'retry': None,
        'timeout': 2.5,
    })]
    assert provider.page_fetch_count == 1
    assert provider.page_iteration_count == 1
    assert provider.second_page_accesses == 0
    assert provider.client.close_calls == 1
    assert breaker.calls == 1
    assert result['status_code'] == 200
    assert result['protocol_details'] == {
        'command': 'list',
        'bucket': 'head-list-bucket',
        'prefix': '',
        'items': [],
        'item_count': 0,
        'is_truncated': False,
        'next_page_token': None,
    }


# --- AGW-49 normalization hardening: hostile nominal successes ----------


class _HostileHeadListValue:
    """Foreign SDK value whose representation is a containment sentinel."""

    def __init__(self, sentinel: str) -> None:
        """Retain the marker that must not cross a public surface."""
        self.sentinel = sentinel

    def __repr__(self) -> str:
        """Expose the marker if production retains or stringifies the value."""
        return self.sentinel


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        pytest.param('size', True, id='content-length-bool'),
        pytest.param('size', -1, id='content-length-negative'),
        pytest.param('size', '17', id='content-length-string'),
        pytest.param(
            'size',
            _HostileHeadListValue('head-content-length-foreign-agw49'),
            id='content-length-foreign',
        ),
        pytest.param('generation', False, id='generation-bool'),
        pytest.param('generation', -1, id='generation-negative'),
        pytest.param('generation', '31', id='generation-string'),
        pytest.param('metageneration', True, id='metageneration-bool'),
        pytest.param('metageneration', -1, id='metageneration-negative'),
        pytest.param(
            'metageneration', '4', id='metageneration-string'),
        pytest.param('content_type', '', id='content-type-empty'),
        pytest.param('content_type', 7, id='content-type-non-string'),
        pytest.param(
            'content_type',
            _HostileHeadListValue('head-content-type-foreign-agw49'),
            id='content-type-foreign',
        ),
        pytest.param('etag', '', id='etag-empty'),
        pytest.param('etag', 7, id='etag-non-string'),
        pytest.param('crc32c', '', id='crc32c-empty'),
        pytest.param('crc32c', True, id='crc32c-non-string'),
        pytest.param(
            'updated', datetime(2026, 8, 22, 10, 30, 45),
            id='last-modified-naive',
        ),
        pytest.param(
            'updated', '2026-08-22T10:30:45+00:00',
            id='last-modified-string',
        ),
        pytest.param(
            'updated',
            _HostileHeadListValue('head-timestamp-foreign-agw49'),
            id='last-modified-foreign',
        ),
        pytest.param('metadata', [], id='metadata-non-mapping'),
        pytest.param(
            'metadata', {7: 'owner'}, id='metadata-non-string-key'),
        pytest.param(
            'metadata', {'owner': 7}, id='metadata-non-string-value'),
        pytest.param(
            'metadata',
            _HostileHeadListValue('head-metadata-foreign-agw49'),
            id='metadata-foreign',
        ),
    ],
)
async def test_head_malformed_nominal_success_fails_closed_without_leak(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    field: str,
    value: object,
) -> None:
    """Every malformed head scalar/map is one safe 502 with no partial data."""
    provider = _HeadListProvider()
    setattr(provider.head_blob, field, value)
    _install_head_list_provider(monkeypatch, provider)
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result = await request(
        'gs://head-list-bucket/malformed-head.txt',
        protocol='GCS',
        protocol_info={'command': 'head', 'timeout': 2.5},
    )

    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {}
    assert provider.client.close_calls == 1
    if isinstance(value, _HostileHeadListValue):
        assert value.sentinel not in surfaces


async def test_head_accepts_zero_integer_boundaries_in_exact_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero is valid for every non-negative head integer family."""
    provider = _HeadListProvider()
    provider.head_blob.size = 0
    provider.head_blob.generation = 0
    provider.head_blob.metageneration = 0
    _install_head_list_provider(monkeypatch, provider)

    result = await request(
        'gs://head-list-bucket/zero-head.txt',
        protocol='GCS',
        protocol_info={'command': 'head', 'timeout': 2.5},
    )

    assert result['status_code'] == 200
    assert result['protocol_details']['content_length'] == 0
    assert result['protocol_details']['generation'] == 0
    assert result['protocol_details']['metageneration'] == 0


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        pytest.param('name', '', id='key-empty'),
        pytest.param('name', 7, id='key-non-string'),
        pytest.param(
            'name',
            _HostileHeadListValue('list-key-foreign-agw49'),
            id='key-foreign',
        ),
        pytest.param('size', True, id='size-bool'),
        pytest.param('size', -1, id='size-negative'),
        pytest.param('size', '17', id='size-string'),
        pytest.param(
            'size',
            _HostileHeadListValue('list-size-foreign-agw49'),
            id='size-foreign',
        ),
        pytest.param('content_type', '', id='content-type-empty'),
        pytest.param('content_type', 7, id='content-type-non-string'),
        pytest.param(
            'content_type',
            _HostileHeadListValue('list-content-type-foreign-agw49'),
            id='content-type-foreign',
        ),
        pytest.param('etag', '', id='etag-empty'),
        pytest.param('etag', 7, id='etag-non-string'),
        pytest.param('generation', False, id='generation-bool'),
        pytest.param('generation', -1, id='generation-negative'),
        pytest.param('generation', '31', id='generation-string'),
        pytest.param(
            'generation',
            _HostileHeadListValue('list-generation-foreign-agw49'),
            id='generation-foreign',
        ),
        pytest.param(
            'updated', datetime(2026, 8, 22, 10, 30, 45),
            id='last-modified-naive',
        ),
        pytest.param(
            'updated', '2026-08-22T10:30:45+00:00',
            id='last-modified-string',
        ),
        pytest.param(
            'updated',
            _HostileHeadListValue('list-timestamp-foreign-agw49'),
            id='last-modified-foreign',
        ),
        pytest.param('crc32c', '', id='crc32c-empty'),
        pytest.param('crc32c', 7, id='crc32c-non-string'),
    ],
)
async def test_list_malformed_item_fails_whole_page_without_leak(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    field: str,
    value: object,
) -> None:
    """One malformed item rejects the ordered page without partial output."""
    provider = _HeadListProvider()
    valid = _HeadListBlob(provider)
    valid.name = 'prefix/already-normalized.txt'
    malformed = _HeadListBlob(provider)
    malformed.name = 'prefix/malformed.txt'
    setattr(malformed, field, value)
    provider.iterator = _HeadListIterator(
        provider, [valid, malformed], 'server-next-page')
    _install_head_list_provider(monkeypatch, provider)
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')
    caller_token = 'caller-page-private-agw49'

    result = await request(
        'gs://head-list-bucket/prefix/',
        protocol='GCS',
        protocol_info={
            'command': 'list',
            'max_items': 2,
            'page_token': caller_token,
            'timeout': 2.5,
        },
    )

    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {}
    assert provider.client.close_calls == 1
    assert caller_token not in surfaces
    if isinstance(value, _HostileHeadListValue):
        assert value.sentinel not in surfaces


@pytest.mark.parametrize(
    'case_name',
    [
        pytest.param('empty-server-token', id='empty-server-token'),
        pytest.param('non-string-server-token', id='non-string-server-token'),
        pytest.param('foreign-server-token', id='foreign-server-token'),
        pytest.param('foreign-iterator', id='foreign-iterator'),
        pytest.param('foreign-page', id='foreign-page'),
        pytest.param('oversized-page', id='oversized-page'),
    ],
)
async def test_list_malformed_page_or_iterator_is_safe_gcs_status(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case_name: str,
) -> None:
    """Invalid page structure/token and over-limit rows all fail closed."""
    provider = _HeadListProvider()
    sentinel = f'list-{case_name}-private-agw49'
    max_items = 1
    # Reason for type: ignore[assignment] -- inject malformed provider shapes.
    if case_name == 'empty-server-token':
        provider.iterator.server_token = ''
    elif case_name == 'non-string-server-token':
        provider.iterator.server_token = 7  # type: ignore[assignment]
    elif case_name == 'foreign-server-token':
        provider.iterator.server_token = (  # type: ignore[assignment]
            _HostileHeadListValue(sentinel))
    elif case_name == 'foreign-iterator':
        provider.iterator = (  # type: ignore[assignment]
            _HostileHeadListValue(sentinel))
    elif case_name == 'foreign-page':
        provider.iterator.page = (  # type: ignore[assignment]
            _HostileHeadListValue(sentinel))
    else:
        first = _HeadListBlob(provider)
        second = _HeadListBlob(provider)
        second.name = 'prefix/extra.txt'
        provider.iterator = _HeadListIterator(
            provider, [first, second], 'server-next-page')
    _install_head_list_provider(monkeypatch, provider)
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')
    caller_token = 'caller-structural-private-agw49'

    result = await request(
        'gs://head-list-bucket/prefix/',
        protocol='GCS',
        protocol_info={
            'command': 'list',
            'max_items': max_items,
            'page_token': caller_token,
            'timeout': 2.5,
        },
    )

    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {}
    assert provider.client.close_calls == 1
    assert caller_token not in surfaces
    assert sentinel not in surfaces


# --- AGW-49 error, retry, cleanup, and containment hardening ------------


class _HeadListErrorBlob(_HeadListBlob):
    """Head blob whose metadata fetch consumes a fresh scripted outcome."""

    def reload(self, **kwargs: object) -> None:
        """Record the exact fetch and raise only the current attempt value."""
        self.provider.record_thread('reload')
        self.provider.reload_calls.append(dict(kwargs))
        provider = self.provider
        if isinstance(provider, _HeadListErrorProvider):
            provider.raise_next('head')


class _HeadListErrorPage(_HeadListPage):
    """One list page with an independently scripted iteration boundary."""

    def __iter__(self) -> Any:
        """Raise at page iteration or preserve the exact service order."""
        self.provider.record_thread('page-iteration')
        self.provider.page_iteration_count += 1
        provider = self.provider
        if isinstance(provider, _HeadListErrorProvider):
            provider.raise_next('iteration')
        return iter(self.items)


class _HeadListErrorCursor:
    """Single page cursor with a separately scriptable fetch boundary."""

    def __init__(self, iterator: '_HeadListErrorIterator') -> None:
        """Retain one fresh iterator for exactly one attempt."""
        self.iterator = iterator

    def __iter__(self) -> '_HeadListErrorCursor':
        """Return this deterministic cursor."""
        return self

    def __next__(self) -> _HeadListErrorPage:
        """Fetch exactly one page or raise its current scripted outcome."""
        provider = self.iterator.provider
        provider.record_thread('page-fetch')
        provider.page_fetch_count += 1
        provider.raise_next('page-fetch')
        self.iterator.next_page_token = provider.server_token
        return self.iterator.page


class _HeadListErrorIterator:
    """Fresh official-SDK-shaped iterator for one list attempt."""

    def __init__(self, provider: '_HeadListErrorProvider') -> None:
        """Create a fresh page/cursor so retries never share iterator state."""
        self.provider = provider
        self.next_page_token: str | None = None
        self.page = _HeadListErrorPage(provider, provider.list_items)

    @property
    def pages(self) -> _HeadListErrorCursor:
        """Expose one fresh, bounded page cursor."""
        self.provider.record_thread('pages')
        self.provider.pages_access_count += 1
        return _HeadListErrorCursor(self)


class _HeadListErrorClient:
    """Attempt-owned head/list client with scripted construction and close."""

    def __init__(self, provider: '_HeadListErrorProvider') -> None:
        """Retain the provider and a per-resource cleanup count."""
        self.provider = provider
        self.close_calls = 0

    def bucket(self, name: str) -> _HeadListBucket:
        """Return the deterministic bucket without external I/O."""
        self.provider.record_thread('bucket')
        self.provider.bucket_names.append(name)
        return self.provider.bucket

    def list_blobs(
        self,
        bucket: object,
        **kwargs: object,
    ) -> _HeadListErrorIterator:
        """Construct only one bounded iterator under exact SDK controls."""
        self.provider.record_thread('list-construction')
        self.provider.list_calls.append((bucket, dict(kwargs)))
        self.provider.raise_next('list-construction')
        return _HeadListErrorIterator(self.provider)

    def close(self) -> None:
        """Close once and consume only this attempt's cleanup outcome."""
        self.provider.record_thread('client-close')
        self.close_calls += 1
        self.provider.raise_next('close')


class _HeadListErrorProvider(_HeadListProvider):
    """Fresh-attempt provider tree for deterministic public failure tests."""

    def __init__(self) -> None:
        """Create valid defaults and independent scripts for every seam."""
        super().__init__()
        self.adc_error: BaseException | None = None
        self.outcomes: dict[str, list[BaseException | None]] = {
            'head': [],
            'list-construction': [],
            'page-fetch': [],
            'iteration': [],
            'close': [],
        }
        self.server_token: str | None = None
        self.head_blob = _HeadListErrorBlob(self)
        self.bucket = _HeadListBucket(self)
        self.list_items = [_HeadListBlob(self)]
        self.returned_clients: list[_HeadListErrorClient] = []

    def adc(self, *args: object, **kwargs: object) -> tuple[object, str]:
        """Return valid ADC or one fresh local credential failure."""
        self.record_thread('adc')
        if self.adc_error is not None:
            raise self.adc_error
        return self.credentials, 'head-list-error-project'

    def make_client(
        self,
        *args: object,
        **kwargs: object,
    ) -> _HeadListErrorClient:
        """Return a new owned client for every gateway attempt."""
        self.record_thread('client')
        client = _HeadListErrorClient(self)
        self.returned_clients.append(client)
        return client

    def script(
        self,
        seam: str,
        *outcomes: BaseException | None,
    ) -> None:
        """Replace one seam's ordered attempt outcomes."""
        self.outcomes[seam] = list(outcomes)

    def raise_next(self, seam: str) -> None:
        """Raise and consume one independently constructed scripted value."""
        if self.outcomes[seam]:
            outcome = self.outcomes[seam].pop(0)
            if isinstance(outcome, BaseException):
                raise outcome


async def _public_retrying_head_list(
    monkeypatch: pytest.MonkeyPatch,
    provider: _HeadListErrorProvider,
    *,
    command: str,
    retries: int,
    events: list[str],
    caller_token: str | None = None,
) -> tuple[dict[str, Any], CircuitBreakerHelper]:
    """Drive one command through the real deterministic breaker contract."""
    breaker_config = _gcs_retry_config(retries, events)
    breaker = CircuitBreakerHelper(
        **validated_breaker_config(breaker_config))
    monkeypatch.setattr(
        base, 'get_breaker', lambda *args, **kwargs: breaker)
    monkeypatch.setattr(google_auth, 'default', provider.adc)
    monkeypatch.setattr(storage, 'Client', provider.make_client)
    info: dict[str, object] = {
        'command': command,
        'timeout': 2.5,
        'circuit_breaker_config': breaker_config,
    }
    if command == 'list':
        info['max_items'] = 1
        if caller_token is not None:
            info['page_token'] = caller_token
    target = 'prefix/' if command == 'list' else 'object.txt'
    result = await request(
        f'gs://Head-List-Error-Bucket/{target}',
        protocol='GCS',
        protocol_info=info,
    )
    return result, breaker


@pytest.mark.parametrize('command', ['head', 'list'])
@pytest.mark.parametrize('credential_stage', ['adc', 'refresh'])
async def test_head_list_credential_failure_is_config_and_uncounted(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    credential_stage: str,
) -> None:
    """ADC discovery and refresh fail locally before provider counting."""
    secret = f'{command}-{credential_stage}-credential-private-agw49'
    events: list[str] = []
    provider = _HeadListErrorProvider()
    if credential_stage == 'adc':
        provider.adc_error = google_auth_exceptions.DefaultCredentialsError(
            secret)
    else:
        provider.credentials.valid = False
        provider.credentials.refresh_error = (
            google_auth_exceptions.RefreshError(secret))

    result, breaker = await _public_retrying_head_list(
        monkeypatch, provider, command=command, retries=2, events=events)

    assert result['status_code'] == 400
    assert result['error']['code'] == 'CONFIG'
    assert result['protocol_details'] == {}
    assert secret not in repr(result)
    assert provider.returned_clients == []
    assert events == ['abort']
    assert breaker.failures == 0


@pytest.mark.parametrize(
    ('command', 'stage'),
    [
        pytest.param('head', 'head', id='head-fetch'),
        pytest.param('list', 'list-construction', id='list-construction'),
        pytest.param('list', 'page-fetch', id='list-page-fetch'),
        pytest.param('list', 'iteration', id='list-page-iteration'),
    ],
)
@pytest.mark.parametrize(
    ('failure_type', 'expected_code', 'expected_status'),
    [
        pytest.param(socket.gaierror, 'DNS', 502, id='dns'),
        pytest.param(ssl.SSLError, 'TLS', 502, id='tls'),
        pytest.param(ConnectionError, 'CONNECT', 502, id='connect'),
        pytest.param(TimeoutError, 'TIMEOUT', 504, id='timeout'),
        pytest.param(OSError, 'TRANSPORT', 502, id='transport'),
    ],
)
async def test_head_list_transport_stage_is_typed_and_breaker_owned(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    stage: str,
    failure_type: type[BaseException],
    expected_code: str,
    expected_status: int,
) -> None:
    """Every provider transport stage has one stable public result."""
    events: list[str] = []
    provider = _HeadListErrorProvider()
    provider.script(
        stage,
        failure_type(f'{command}-{stage}-transport-private-agw49'),
    )

    result, breaker = await _public_retrying_head_list(
        monkeypatch, provider, command=command, retries=0, events=events)

    assert result['status_code'] == expected_status
    assert result['error']['code'] == expected_code
    assert result['protocol_details'] == {}
    assert 'private-agw49' not in repr(result)
    assert sum(client.close_calls for client in provider.returned_clients) == 1
    assert events == ['failed', 'exhausted']
    assert breaker.failures == 1


@pytest.mark.parametrize('command', ['head', 'list'])
@pytest.mark.parametrize(
    ('status', 'retryable'),
    [
        (408, True), (429, True), (500, True), (502, True),
        (503, True), (504, True), (400, False), (401, False),
        (403, False), (404, False), (409, False), (412, False),
    ],
)
async def test_head_list_service_status_has_exact_retry_semantics(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    status: int,
    retryable: bool,
) -> None:
    """The frozen service allowlist alone controls retry and counting."""
    events: list[str] = []
    provider = _HeadListErrorProvider()
    attempts = 2 if retryable else 1
    stage = 'head' if command == 'head' else 'page-fetch'
    provider.script(
        stage,
        *[_FakeServiceError(status) for _ in range(attempts)],
    )

    result, breaker = await _public_retrying_head_list(
        monkeypatch, provider, command=command, retries=1, events=events)

    target = 'object.txt' if command == 'head' else 'prefix/'
    assert result['status_code'] == status
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {
        'command': command,
        'bucket': 'head-list-error-bucket',
        'target': target,
        'gcs_error_code': 'conditionNotMet',
        'gcs_error_message': 'safe service refusal',
        'response_metadata': {
            'http_status_code': status,
            'request_id': 'request-7',
        },
    }
    close_calls = sum(
        client.close_calls for client in provider.returned_clients)
    assert close_calls == attempts
    assert events == (
        ['failed', 'failed', 'exhausted'] if retryable else ['abort'])
    assert breaker.failures == (attempts if retryable else 0)


@pytest.mark.parametrize('command', ['head', 'list'])
@pytest.mark.parametrize(
    ('body_kind', 'expected_code'),
    [
        pytest.param('service', 'GCS_STATUS', id='service'),
        pytest.param('transport', 'DNS', id='transport'),
        pytest.param('malformed', 'GCS_STATUS', id='malformed-success'),
    ],
)
async def test_head_list_body_failure_wins_over_hostile_close(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    command: str,
    body_kind: str,
    expected_code: str,
) -> None:
    """Known command failure is retained over a later cleanup defect."""
    provider = _HeadListErrorProvider()
    stage = 'head' if command == 'head' else 'page-fetch'
    if body_kind == 'service':
        provider.script(stage, _FakeServiceError(403))
    elif body_kind == 'transport':
        provider.script(stage, socket.gaierror('body-private-agw49'))
    elif command == 'head':
        provider.head_blob.size = -1
    else:
        provider.list_items[0].name = ''
    close_secret = f'{command}-{body_kind}-close-private-agw49'
    provider.script('close', RuntimeError(close_secret))
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, _ = await _public_retrying_head_list(
        monkeypatch, provider, command=command, retries=0, events=[])

    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    assert result['error']['code'] == expected_code
    assert result['protocol_details'] == (
        {
            'command': command,
            'bucket': 'head-list-error-bucket',
            'target': 'object.txt' if command == 'head' else 'prefix/',
            'gcs_error_code': 'conditionNotMet',
            'gcs_error_message': 'safe service refusal',
            'response_metadata': {
                'http_status_code': 403,
                'request_id': 'request-7',
            },
        }
        if body_kind == 'service'
        else {}
    )
    assert close_secret not in surfaces
    assert sum(client.close_calls for client in provider.returned_clients) == 1


@pytest.mark.parametrize('command', ['head', 'list'])
@pytest.mark.parametrize(
    ('cleanup_factory', 'expected_code', 'expected_status'),
    [
        pytest.param(
            lambda: socket.gaierror('cleanup-dns-private-agw49'),
            'DNS', 502, id='transport'),
        pytest.param(
            lambda: google_auth_exceptions.DefaultCredentialsError(
                'cleanup-credential-private-agw49'),
            'CONFIG', 400, id='credential'),
        pytest.param(
            lambda: _FakeServiceError(503),
            'GCS_STATUS', 503, id='service'),
    ],
)
async def test_head_list_cleanup_only_failure_has_stable_public_type(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    cleanup_factory: Any,
    expected_code: str,
    expected_status: int,
) -> None:
    """Credential, transport, and service cleanup map deterministically."""
    events: list[str] = []
    provider = _HeadListErrorProvider()
    provider.script('close', cleanup_factory())

    result, _ = await _public_retrying_head_list(
        monkeypatch, provider, command=command, retries=0, events=events)

    assert result['status_code'] == expected_status
    assert result['error']['code'] == expected_code
    assert result['protocol_details'] == {}
    assert 'private-agw49' not in repr(result)
    assert events == (
        ['failed', 'exhausted'] if expected_code == 'DNS' else ['abort'])


@pytest.mark.parametrize('command', ['head', 'list'])
async def test_head_list_cleanup_programming_defect_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """Unknown cleanup defects propagate unchanged at one conversion point."""
    provider = _HeadListErrorProvider()
    failure = RuntimeError(f'{command}-cleanup-programming-defect-agw49')
    cause = ValueError(f'{command}-cleanup-programming-cause-agw49')
    failure.__cause__ = cause
    provider.script('close', failure)

    with pytest.raises(RuntimeError) as caught:
        await _public_retrying_head_list(
            monkeypatch, provider, command=command, retries=0, events=[])

    assert caught.value is failure
    assert caught.value.__cause__ is cause
    assert sum(client.close_calls for client in provider.returned_clients) == 1


async def test_list_failure_contains_tokens_credentials_and_sdk_values(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every failed-list surface omits caller/server/provider bearer data."""
    sentinels = {
        'caller': 'caller-page-token-private-agw49',
        'server': 'server-page-token-private-agw49',
        'credential': 'credential-value-private-agw49',
        'sdk': 'sdk-response-private-agw49',
    }
    events: list[str] = []
    provider = _HeadListErrorProvider()
    provider.server_token = sentinels['server']
    provider.credentials.__repr__ = lambda: sentinels['credential']
    hostile = ' authorization=Bearer '.join(sentinels.values())
    failure = _FakeServiceError(
        403, code=hostile, message=hostile, request_id=hostile)
    provider.script('iteration', failure)
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, breaker = await _public_retrying_head_list(
        monkeypatch,
        provider,
        command='list',
        retries=1,
        events=events,
        caller_token=sentinels['caller'],
    )

    surfaces = ''.join((
        repr(result),
        _logged_gcs_surfaces(caplog),
        repr(events),
        ''.join(traceback.format_exception(failure)),
        repr(breaker.__dict__),
    ))
    assert result['error']['code'] == 'GCS_STATUS'
    assert set(result['protocol_details']) == {
        'command', 'bucket', 'target', 'gcs_error_code',
        'gcs_error_message', 'response_metadata',
    }
    assert all(secret not in surfaces for secret in sentinels.values())


# --- AGW-49 lifecycle hardening: retained head/list drains ---------------


class _HeadListLifecycleBlob(_HeadListBlob):
    """Head blob whose reload can remain live beyond waiter acceptance."""

    def reload(self, **kwargs: object) -> None:
        """Block reload and expose late metadata only privately."""
        self.provider.record_thread('reload')
        self.provider.reload_calls.append(dict(kwargs))
        provider = self.provider
        assert isinstance(provider, _HeadListLifecycleProvider)
        provider.block_selected('head-reload')


class _HeadListLifecyclePage(_HeadListPage):
    """One list page whose iteration is an independently blocked seam."""

    def __iter__(self) -> Any:
        """Block iteration before returning the late service objects."""
        self.provider.record_thread('page-iteration')
        self.provider.page_iteration_count += 1
        provider = self.provider
        assert isinstance(provider, _HeadListLifecycleProvider)
        provider.block_selected('list-iteration')
        return iter(self.items)


class _HeadListLifecycleCursor:
    """Single-page cursor with an event-controlled fetch boundary."""

    def __init__(self, iterator: '_HeadListLifecycleIterator') -> None:
        """Retain the one iterator whose first page is fetched."""
        self.iterator = iterator

    def __iter__(self) -> '_HeadListLifecycleCursor':
        """Return this one-shot cursor."""
        return self

    def __next__(self) -> _HeadListLifecyclePage:
        """Block the page fetch and then return exactly one late page."""
        provider = self.iterator.provider
        provider.record_thread('page-fetch')
        provider.page_fetch_count += 1
        provider.block_selected('list-page-fetch')
        self.iterator.next_page_token = provider.server_token
        return self.iterator.page


class _HeadListLifecycleIterator:
    """One bounded iterator exposing separately controlled list seams."""

    def __init__(self, provider: '_HeadListLifecycleProvider') -> None:
        """Create one page containing only the late sentinel item."""
        self.provider = provider
        self.next_page_token: str | None = None
        self.page = _HeadListLifecyclePage(provider, provider.list_items)

    @property
    def pages(self) -> _HeadListLifecycleCursor:
        """Expose one cursor without fetching a second service page."""
        self.provider.record_thread('pages')
        self.provider.pages_access_count += 1
        return _HeadListLifecycleCursor(self)


class _HeadListLifecycleClient:
    """Attempt-owned client with blocked list construction and cleanup."""

    def __init__(self, provider: '_HeadListLifecycleProvider') -> None:
        """Retain the provider and one exact cleanup counter."""
        self.provider = provider
        self.close_calls = 0

    def bucket(self, name: str) -> _HeadListBucket:
        """Return the deterministic bucket on the provider worker."""
        self.provider.record_thread('bucket')
        self.provider.bucket_names.append(name)
        return self.provider.bucket

    def list_blobs(
        self,
        bucket: object,
        **kwargs: object,
    ) -> _HeadListLifecycleIterator:
        """Block construction before returning one private late iterator."""
        self.provider.record_thread('list-construction')
        self.provider.list_calls.append((bucket, dict(kwargs)))
        self.provider.block_selected('list-construction')
        return _HeadListLifecycleIterator(self.provider)

    def close(self) -> None:
        """Block exact off-loop cleanup and then raise its hostile sentinel."""
        self.provider.record_thread('client-close')
        self.close_calls += 1
        self.provider.close_started.set()
        assert self.provider.close_release.wait(timeout=1)
        raise RuntimeError(self.provider.close_sentinel)


class _HeadListLifecycleProvider(_HeadListProvider):
    """Event-driven head/list provider for retained-lifecycle assertions."""

    def __init__(self, blocked_seam: str) -> None:
        """Create one blocked command seam and one blocked cleanup seam."""
        super().__init__()
        self.blocked_seam = blocked_seam
        self.operation_started = threading.Event()
        self.operation_release = threading.Event()
        self.operation_started_async = asyncio.Event()
        self.event_loop = asyncio.get_running_loop()
        self.close_started = threading.Event()
        self.close_release = threading.Event()
        self.late_sentinel = f'{blocked_seam}-late-result-private-agw49'
        self.server_token = f'{blocked_seam}-server-token-private-agw49'
        self.close_sentinel = f'{blocked_seam}-close-private-agw49'
        self.head_blob = _HeadListLifecycleBlob(self)
        self.head_blob.etag = self.late_sentinel
        late_item = _HeadListBlob(self)
        late_item.etag = self.late_sentinel
        self.list_items = [late_item]
        self.bucket = _HeadListBucket(self)
        self.returned_clients: list[_HeadListLifecycleClient] = []

    def block_selected(self, seam: str) -> None:
        """Signal and block only the lifecycle seam selected by the case."""
        if seam != self.blocked_seam:
            return
        self.operation_started.set()
        self.event_loop.call_soon_threadsafe(self.operation_started_async.set)
        assert self.operation_release.wait(timeout=1)

    def make_client(
        self,
        *args: object,
        **kwargs: object,
    ) -> _HeadListLifecycleClient:
        """Return one new client for the single command attempt."""
        self.record_thread('client')
        client = _HeadListLifecycleClient(self)
        self.returned_clients.append(client)
        return client


async def _public_head_list_lifecycle(
    *,
    command: str,
    caller_token: str,
) -> dict[str, Any]:
    """Drive one public head/list call through the installed provider."""
    info: dict[str, object] = {'command': command, 'timeout': 1}
    target = 'object.txt'
    if command == 'list':
        target = 'prefix/'
        info.update({'max_items': 1, 'page_token': caller_token})
    return await request(
        f'gs://Head-List-Lifecycle-Bucket/{target}',
        protocol='GCS',
        protocol_info=info,
    )


@pytest.mark.parametrize(
    ('command', 'blocked_seam'),
    [
        pytest.param('head', 'head-reload', id='head-reload'),
        pytest.param(
            'list', 'list-construction', id='list-construction'),
        pytest.param('list', 'list-page-fetch', id='list-page-fetch'),
        pytest.param('list', 'list-iteration', id='list-iteration'),
    ],
)
@pytest.mark.parametrize('outcome', ['cancel', 'timeout'])
async def test_head_list_drain_retains_capacity_and_first_outcome(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    command: str,
    blocked_seam: str,
    outcome: str,
) -> None:
    """Late work and hostile cleanup cannot replace cancellation or timeout."""
    provider = _HeadListLifecycleProvider(blocked_seam)
    caller_token = f'{blocked_seam}-caller-token-private-agw49'
    expiry_injected = asyncio.Event()
    if outcome == 'timeout':
        real_wait_for = asyncio.wait_for
        expired = False

        async def expire_selected_provider_wait_once(
            awaitable: Any,
            *,
            timeout: int | float,
        ) -> Any:
            """Expire only after the selected sync provider seam has begun."""
            nonlocal expired
            if expired:
                return await real_wait_for(awaitable, timeout=timeout)
            operation = asyncio.ensure_future(awaitable)
            started = asyncio.create_task(
                provider.operation_started_async.wait())
            done, _ = await asyncio.wait(
                {operation, started},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                started.cancel()
                await asyncio.gather(started, return_exceptions=True)
                return operation.result()
            expired = True
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            expiry_injected.set()
            raise asyncio.TimeoutError

        monkeypatch.setattr(
            gcs_client.asyncio, 'wait_for', expire_selected_provider_wait_once)

    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    loop_thread = threading.get_ident()
    breaker = _install_head_list_provider(monkeypatch, provider)
    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    companion_leases = [acquire() for _ in range(3)]
    task = asyncio.create_task(_public_head_list_lifecycle(
        command=command,
        caller_token=caller_token,
    ))
    admitted_after: Any = None
    try:
        await _wait_for_thread_event(provider.operation_started)
        if outcome == 'cancel':
            task.cancel('first-head-list-cancellation')
            task.cancel('repeated-head-list-cancellation')
        else:
            await expiry_injected.wait()
        with pytest.raises(GcsCapacityError):
            acquire()

        provider.operation_release.set()
        await _wait_for_thread_event(provider.close_started)
        with pytest.raises(GcsCapacityError):
            acquire()
        task.cancel('cancellation-during-head-list-close')
        provider.close_release.set()

        if outcome == 'cancel':
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args == ('first-head-list-cancellation',)
            surfaces = ''.join((
                ''.join(traceback.format_exception(caught.value)),
                repr(caught.value.__cause__),
                repr(caught.value.__context__),
            ))
        else:
            result = await task
            assert result['ok'] is False
            assert result['error']['code'] == 'TIMEOUT'
            assert result['protocol_details'] == {}
            surfaces = repr(result)
        surfaces += (
            repr(breaker.__dict__) + _logged_gcs_surfaces(caplog))

        assert provider.late_sentinel not in surfaces
        assert provider.server_token not in surfaces
        assert caller_token not in surfaces
        assert provider.close_sentinel not in surfaces
        assert sum(
            client.close_calls for client in provider.returned_clients) == 1
        assert provider.timeline[-1] == 'client-close'
        recorded_seam = {
            'head-reload': 'reload',
            'list-construction': 'list-construction',
            'list-page-fetch': 'page-fetch',
            'list-iteration': 'page-iteration',
        }[blocked_seam]
        assert provider.timeline.index(
            recorded_seam) < provider.timeline.index('client-close')
        close_threads = [
            (thread_id, thread_name)
            for seam, thread_id, thread_name in provider.threads
            if seam == 'client-close'
        ]
        assert len(close_threads) == 1
        assert close_threads[0][0] != loop_thread
        assert close_threads[0][1].startswith('asyncio-gateway-gcs')
        assert getattr(gcs_client, '_active_gcs_leases')() == 3

        admitted_after = acquire()
        with pytest.raises(GcsCapacityError):
            acquire()
    finally:
        provider.operation_release.set()
        provider.close_release.set()
        await asyncio.gather(task, return_exceptions=True)
        if admitted_after is not None:
            await _release_lease(admitted_after)
        for lease in companion_leases:
            await _release_lease(lease)


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
        if self.provider.reload_started is not None:
            self.provider.reload_started.set()
        if self.provider.reload_release is not None:
            assert self.provider.reload_release.wait(timeout=1)
        if self.provider.reload_outcomes:
            outcome = self.provider.reload_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome

    def download_as_bytes(self, **kwargs: object) -> object:
        """Return one exact inclusive slice of the stored/raw object."""
        self.provider.record_thread('range')
        self.provider.range_calls.append(dict(kwargs))
        if self.provider.range_handler is not None:
            return self.provider.range_handler(dict(kwargs))
        if self.provider.range_outcomes:
            outcome = self.provider.range_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
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
        if self.provider.close_started is not None:
            self.provider.close_started.set()
        if self.provider.close_release is not None:
            assert self.provider.close_release.wait(timeout=1)
        if self.provider.close_outcomes:
            outcome = self.provider.close_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome


class _DownloadProvider:
    """Deterministic ADC, metadata, raw-range, and cleanup recorder."""

    def __init__(self, stored_bytes: bytes) -> None:
        """Create one connected provider tree for the stored bytes."""
        self.stored_bytes = stored_bytes
        self.timeline: list[str] = []
        self.threads: dict[str, list[tuple[int, str]]] = {}
        self.reload_calls: list[dict[str, object]] = []
        self.range_calls: list[dict[str, object]] = []
        self.reload_outcomes: list[object] = []
        self.range_outcomes: list[object] = []
        self.close_outcomes: list[object] = []
        self.range_handler: Any = None
        self.bucket_names: list[str] = []
        self.blob_keys: list[str] = []
        self.credentials = _UploadCredentials()
        self.adc_error: BaseException | None = None
        self.reload_started: threading.Event | None = None
        self.reload_release: threading.Event | None = None
        self.close_started: threading.Event | None = None
        self.close_release: threading.Event | None = None
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
        if self.adc_error is not None:
            raise self.adc_error
        return self.credentials, 'download-project'

    def make_client(self, *args: object, **kwargs: object) -> _DownloadClient:
        """Return the observable storage client."""
        self.record_thread('client')
        return self.client

    def script_reload(self, *outcomes: object) -> None:
        """Queue deterministic metadata-pin outcomes in call order."""
        self.reload_outcomes.extend(outcomes)

    def script_ranges(self, *outcomes: object) -> None:
        """Queue deterministic raw-range outcomes in call order."""
        self.range_outcomes.extend(outcomes)

    def script_close(self, *outcomes: object) -> None:
        """Queue deterministic owned-client cleanup outcomes."""
        self.close_outcomes.extend(outcomes)


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


async def _public_retrying_download(
    monkeypatch: pytest.MonkeyPatch,
    provider: _DownloadProvider,
    local_path: Path,
    *,
    max_response_bytes: int,
    retries: int,
    events: list[str],
) -> tuple[dict[str, Any], CircuitBreakerHelper]:
    """Drive download through one deterministic real retry/breaker loop."""
    breaker_config = _gcs_retry_config(retries, events)
    breaker = CircuitBreakerHelper(
        **validated_breaker_config(breaker_config))
    monkeypatch.setattr(
        base, 'get_breaker', lambda *args, **kwargs: breaker)
    _install_download_provider(monkeypatch, provider)
    result = await request(
        'gs://Download-Retry-Bucket/folder/object.bin',
        protocol='GCS',
        protocol_info={
            'command': 'download',
            'local_path': str(local_path),
            'max_response_bytes': max_response_bytes,
            'timeout': 2.5,
            'circuit_breaker_config': breaker_config,
        },
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


# --- AGW-48 tranche D2a: immutable retry and raw-shape adversaries -------


async def test_download_retries_transient_reload_only_until_pin_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A transient metadata refusal retries before the immutable pin exists."""
    events: list[str] = []
    provider = _DownloadProvider(b'pinned-body')
    provider.script_reload(_FakeServiceError(503), None)
    target = tmp_path / 'download.bin'

    result, breaker = await _public_retrying_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=32,
        retries=1,
        events=events,
    )

    assert result['ok'] is True
    assert target.read_bytes() == b'pinned-body'
    assert provider.reload_calls == [
        {'retry': None, 'timeout': 2.5},
        {'retry': None, 'timeout': 2.5},
    ]
    assert len(provider.range_calls) == 1
    assert provider.client.close_calls == 2
    assert events == ['failed']
    assert breaker.failures == 0


async def test_download_range_retry_restarts_zero_without_reloading_pin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A later transient range failure replays byte zero on the same pin."""
    events: list[str] = []
    original = bytes(index % 251 for index in range(65537))
    provider = _DownloadProvider(original)
    provider.script_ranges(
        original[:65536],
        _FakeServiceError(503),
        original[:65536],
        original[65536:],
    )
    target = tmp_path / 'download.bin'
    target.write_bytes(b'existing-target')
    target_before_attempts: list[bytes] = []

    async def observe_atomic_target(
        path: object,
        chunks: Any,
        **kwargs: object,
    ) -> int:
        """Prove a failed attempt never publishes before the retry."""
        target_before_attempts.append(target.read_bytes())
        return await stream_to_path(path, chunks, **kwargs)

    monkeypatch.setattr(gcs_client, 'stream_to_path', observe_atomic_target)

    result, breaker = await _public_retrying_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=len(original),
        retries=1,
        events=events,
    )

    assert result['ok'] is True
    assert target.read_bytes() == original
    assert provider.reload_calls == [{'retry': None, 'timeout': 2.5}]
    assert [call['start'] for call in provider.range_calls] == [
        0, 65536, 0, 65536,
    ]
    assert [call['if_generation_match'] for call in provider.range_calls] == [
        41, 41, 41, 41,
    ]
    assert all(call['raw_download'] is True for call in provider.range_calls)
    assert [
        (call['retry'], call['timeout'])
        for call in provider.range_calls
    ] == [(None, 2.5)] * 4
    assert target_before_attempts == [b'existing-target'] * 2
    assert provider.client.close_calls == 2
    assert events == ['failed']
    assert breaker.failures == 0


async def test_download_latest_replacement_cannot_mix_pinned_ranges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing the latest object between chunks still emits pinned bytes."""
    original = b'a' * 65536 + b'old-tail'
    replacement = b'b' * 65536 + b'new-tail-which-is-longer'
    provider = _DownloadProvider(original)
    latest = {'generation': 41, 'bytes': original}

    def generation_aware_range(arguments: dict[str, object]) -> bytes:
        start = arguments['start']
        end = arguments['end']
        generation = arguments['if_generation_match']
        assert isinstance(start, int) and isinstance(end, int)
        if start == 0:
            latest.update(generation=42, bytes=replacement)
        source = original if generation == 41 else latest['bytes']
        assert isinstance(source, bytes)
        return source[start:end + 1]

    provider.range_handler = generation_aware_range
    target = tmp_path / 'download.bin'

    result, _ = await _public_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=len(original),
    )

    assert result['ok'] is True
    assert latest == {'generation': 42, 'bytes': replacement}
    assert target.read_bytes() == original
    assert provider.reload_calls == [{'retry': None, 'timeout': 2.5}]
    assert [call['if_generation_match'] for call in provider.range_calls] == [
        41, 41,
    ]
    assert [
        (call['start'], call['end'])
        for call in provider.range_calls
    ] == [(0, 65535), (65536, len(original) - 1)]


@pytest.mark.parametrize('status', [404, 412])
async def test_download_pinned_generation_refusal_aborts_without_counting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: int,
) -> None:
    """A missing or changed pinned generation cannot retry or mutate output."""
    events: list[str] = []
    provider = _DownloadProvider(b'x')
    provider.script_ranges(_FakeServiceError(status))
    target = tmp_path / 'download.bin'
    target.write_bytes(b'existing-target')

    result, breaker = await _public_retrying_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=1,
        retries=2,
        events=events,
    )

    assert result['ok'] is False
    assert result['status_code'] == status
    assert result['error']['code'] == 'GCS_STATUS'
    assert target.read_bytes() == b'existing-target'
    assert len(provider.range_calls) == 1
    assert provider.reload_calls == [{'retry': None, 'timeout': 2.5}]
    assert provider.client.close_calls == 1
    assert events == ['abort']
    assert breaker.failures == 0


async def test_download_content_encoding_emits_stored_gzip_bytes_under_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Content-Encoding metadata never expands the stored/raw byte stream."""
    stored_gzip = b'\x1f\x8b\x08\x00stored-compressed-representation'
    provider = _DownloadProvider(stored_gzip)
    provider.blob.content_encoding = 'gzip'
    target = tmp_path / 'download.bin'

    result, _ = await _public_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=len(stored_gzip),
    )

    assert result['ok'] is True
    assert target.read_bytes() == stored_gzip
    assert provider.range_calls == [{
        'start': 0,
        'end': len(stored_gzip) - 1,
        'if_generation_match': 41,
        'raw_download': True,
        'retry': None,
        'timeout': 2.5,
    }]
    assert result['protocol_details']['bytes_written'] == len(stored_gzip)
    assert result['protocol_details']['crc32c'] == 'download-crc32c=='
    assert all(
        'verif' not in key.lower()
        for key in result['protocol_details']
    )


class _HostileDownloadRange:
    """Non-bytes provider result whose representation is private."""

    def __repr__(self) -> str:
        """Return a sentinel that must not enter any public surface."""
        return 'hostile-download-range-secret-d2a'


@pytest.mark.parametrize(
    'outcome',
    [
        pytest.param(b'xy', id='short'),
        pytest.param(b'transparently-decompressed', id='overlong'),
        pytest.param(_HostileDownloadRange(), id='non-bytes'),
    ],
)
async def test_download_malformed_range_fails_closed_without_partial_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    outcome: object,
) -> None:
    """Malformed nominal range success is safe, abortable, and atomic."""
    events: list[str] = []
    provider = _DownloadProvider(b'raw')
    provider.script_ranges(outcome)
    target = tmp_path / 'download.bin'
    target.write_bytes(b'existing-target')
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, breaker = await _public_retrying_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=3,
        retries=2,
        events=events,
    )

    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {}
    assert target.read_bytes() == b'existing-target'
    assert len(provider.range_calls) == 1
    assert provider.client.close_calls == 1
    assert events == ['abort']
    assert breaker.failures == 0
    assert 'hostile-download-range-secret-d2a' not in surfaces
    assert 'verified' not in surfaces.lower()


# --- AGW-48 tranche D2b1: download errors and cleanup ownership ----------


@pytest.mark.parametrize('credential_stage', ['adc', 'refresh'])
async def test_download_credential_failure_is_config_before_counting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    credential_stage: str,
) -> None:
    """ADC discovery and refresh failures are safe local configuration."""
    secret = f'download-{credential_stage}-credential-private-d2b1'
    events: list[str] = []
    provider = _DownloadProvider(b'provider-body')
    if credential_stage == 'adc':
        provider.adc_error = google_auth_exceptions.DefaultCredentialsError(
            secret)
    else:
        provider.credentials.valid = False
        provider.credentials.refresh_error = (
            google_auth_exceptions.RefreshError(secret))
    target = tmp_path / 'credential-target.bin'
    target.write_bytes(b'existing-target')

    result, breaker = await _public_retrying_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=32,
        retries=2,
        events=events,
    )

    assert result['ok'] is False
    assert result['status_code'] == 400
    assert result['error']['code'] == 'CONFIG'
    assert secret not in repr(result)
    assert target.read_bytes() == b'existing-target'
    assert provider.reload_calls == []
    assert provider.range_calls == []
    assert provider.client.close_calls == 0
    assert events == ['abort']
    assert breaker.failures == 0


@pytest.mark.parametrize('stage', ['pin', 'range'])
@pytest.mark.parametrize(
    ('failure_type', 'expected_code', 'expected_status'),
    [
        pytest.param(socket.gaierror, 'DNS', 502, id='dns'),
        pytest.param(ssl.SSLError, 'TLS', 502, id='tls'),
        pytest.param(ConnectionError, 'CONNECT', 502, id='connect'),
        pytest.param(TimeoutError, 'TIMEOUT', 504, id='timeout'),
        pytest.param(OSError, 'TRANSPORT', 502, id='transport'),
    ],
)
async def test_download_transport_failure_is_typed_and_breaker_owned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    failure_type: type[BaseException],
    expected_code: str,
    expected_status: int,
) -> None:
    """Each transport class at pin and range has one stable public result."""
    events: list[str] = []
    provider = _DownloadProvider(b'x')
    failure = failure_type(f'{expected_code.lower()}-private-d2b1')
    if stage == 'pin':
        provider.script_reload(failure)
    else:
        provider.script_ranges(failure)
    target = tmp_path / 'transport-target.bin'
    target.write_bytes(b'existing-target')

    result, breaker = await _public_retrying_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=1,
        retries=0,
        events=events,
    )

    assert result['ok'] is False
    assert result['status_code'] == expected_status
    assert result['error']['code'] == expected_code
    assert 'private-d2b1' not in repr(result)
    assert target.read_bytes() == b'existing-target'
    assert len(provider.reload_calls) == 1
    assert len(provider.range_calls) == (0 if stage == 'pin' else 1)
    assert provider.client.close_calls == 1
    assert events == ['failed', 'exhausted']
    assert breaker.failures == 1


@pytest.mark.parametrize('stage', ['pin', 'range'])
@pytest.mark.parametrize(
    ('status', 'retryable'),
    [
        (408, True), (429, True), (500, True), (502, True),
        (503, True), (504, True), (400, False), (401, False),
        (403, False), (404, False), (409, False), (412, False),
    ],
)
async def test_download_service_status_has_exact_retry_and_cleanup_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    status: int,
    retryable: bool,
) -> None:
    """Pin and range service statuses share the frozen gateway policy."""
    events: list[str] = []
    provider = _DownloadProvider(b'x')
    failures = [_FakeServiceError(status) for _ in range(
        2 if retryable else 1)]
    if stage == 'pin':
        provider.script_reload(*failures)
    else:
        provider.script_ranges(*failures)
    target = tmp_path / 'service-target.bin'
    target.write_bytes(b'existing-target')

    result, breaker = await _public_retrying_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=1,
        retries=1,
        events=events,
    )

    attempts = 2 if retryable else 1
    assert result['ok'] is False
    assert result['status_code'] == status
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {
        'command': 'download',
        'bucket': 'download-retry-bucket',
        'target': 'folder/object.bin',
        'gcs_error_code': 'conditionNotMet',
        'gcs_error_message': 'safe service refusal',
        'response_metadata': {
            'http_status_code': status,
            'request_id': 'request-7',
        },
    }
    assert target.read_bytes() == b'existing-target'
    assert len(provider.reload_calls) == (attempts if stage == 'pin' else 1)
    assert len(provider.range_calls) == (0 if stage == 'pin' else attempts)
    assert provider.client.close_calls == attempts
    assert events == (
        ['failed', 'failed', 'exhausted'] if retryable else ['abort'])
    assert breaker.failures == (attempts if retryable else 0)


async def test_download_hostile_pin_metadata_is_safe_gcs_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hostile nominal metadata value cannot escape normalization."""
    provider = _DownloadProvider(b'x')
    provider.blob.generation = _HostileUploadMetadata(
        'hostile-download-pin-private-d2b1')
    target = tmp_path / 'metadata-target.bin'
    target.write_bytes(b'existing-target')
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, _ = await _public_download(
        monkeypatch, provider, target, max_response_bytes=1)

    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    assert result['status_code'] == 502
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {}
    assert 'hostile-download-pin-private-d2b1' not in surfaces
    assert target.read_bytes() == b'existing-target'
    assert provider.range_calls == []
    assert provider.client.close_calls == 1


@pytest.mark.parametrize(
    ('body_kind', 'expected_code'),
    [
        pytest.param('service', 'GCS_STATUS', id='service'),
        pytest.param('transport', 'DNS', id='transport'),
        pytest.param('metadata', 'GCS_STATUS', id='metadata'),
    ],
)
async def test_download_body_failure_wins_over_hostile_client_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    body_kind: str,
    expected_code: str,
) -> None:
    """A later cleanup defect cannot replace a known download failure."""
    close_secret = f'download-close-{body_kind}-private-d2b1'
    provider = _DownloadProvider(b'x')
    if body_kind == 'service':
        provider.script_ranges(_FakeServiceError(403))
    elif body_kind == 'transport':
        provider.script_ranges(socket.gaierror('range-private-d2b1'))
    else:
        provider.blob.size = -1
    provider.script_close(RuntimeError(close_secret))
    target = tmp_path / 'body-precedence.bin'
    target.write_bytes(b'existing-target')
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, _ = await _public_retrying_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=1,
        retries=0,
        events=[],
    )

    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    assert result['error']['code'] == expected_code
    assert close_secret not in surfaces
    assert target.read_bytes() == b'existing-target'
    assert provider.client.close_calls == 1


@pytest.mark.parametrize(
    ('command', 'body_kind', 'expected_code', 'expected_status',
     'expected_events'),
    [
        pytest.param(
            'upload', 'service', 'GCS_STATUS', 403, ['abort'],
            id='upload-service',
        ),
        pytest.param(
            'upload', 'transport', 'DNS', 502,
            ['failed', 'exhausted'], id='upload-transport',
        ),
        pytest.param(
            'upload', 'malformed', 'GCS_STATUS', 502, ['abort'],
            id='upload-malformed-success',
        ),
        pytest.param(
            'download', 'service', 'GCS_STATUS', 403, ['abort'],
            id='download-service',
        ),
        pytest.param(
            'download', 'transport', 'DNS', 502,
            ['failed', 'exhausted'], id='download-transport',
        ),
        pytest.param(
            'download', 'malformed', 'GCS_STATUS', 502, ['abort'],
            id='download-malformed-success',
        ),
    ],
)
async def test_body_failure_precedes_cancellation_during_blocked_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    command: str,
    body_kind: str,
    expected_code: str,
    expected_status: int,
    expected_events: list[str],
) -> None:
    """A later close cancellation cannot replace an established body result."""
    body_secret = f'body-private-{command}-{body_kind}-review-1'
    cleanup_secret = f'cleanup-private-{command}-{body_kind}-review-1'
    payload_secret = f'payload-private-{command}-{body_kind}-review-1'
    events: list[str] = []
    breaker_config = _gcs_retry_config(0, events)
    breaker = CircuitBreakerHelper(
        **validated_breaker_config(breaker_config))
    monkeypatch.setattr(base, 'get_breaker', lambda *args, **kwargs: breaker)
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    close_started = threading.Event()
    close_release = threading.Event()
    body_failure: BaseException | None = None
    reads: list[tuple[object, int]] = []
    source = tmp_path / f'{command}-{body_kind}-source.bin'
    original_path_bytes = b'caller-path-must-remain-unchanged'
    source.write_bytes(original_path_bytes)

    if command == 'upload':
        provider: _UploadProvider | _DownloadProvider = _UploadProvider([])
        if body_kind == 'service':
            body_failure = _FakeServiceError(
                403,
                code=f'secret={body_secret}',
                message=f'secret={body_secret}',
                request_id=f'secret={body_secret}',
            )
            provider.script_upload(body_failure)
        elif body_kind == 'transport':
            body_failure = socket.gaierror(body_secret)
            provider.script_upload(body_failure)
        else:
            provider.blob.generation = _HostileUploadMetadata(body_secret)
        provider.close_started = close_started
        provider.close_release = close_release

        async def guarded_read(
            path: object,
            *,
            max_bytes: int,
            chunk_size: int = 65536,
        ) -> bytes:
            """Return fixed bytes while recording the caller's safe path."""
            del chunk_size
            reads.append((path, max_bytes))
            return payload_secret.encode()

        monkeypatch.setattr(gcs_client, 'read_guarded_file', guarded_read)
        _install_upload_provider(monkeypatch, provider)
        operation = request(
            'gs://precedence-upload/object.bin',
            protocol='GCS',
            protocol_info={
                'command': 'upload',
                'local_path': str(source),
                'max_upload_bytes': 128,
                'timeout': 2.5,
                'circuit_breaker_config': breaker_config,
            },
        )
    else:
        provider = _DownloadProvider(payload_secret.encode())
        if body_kind == 'service':
            body_failure = _FakeServiceError(
                403,
                code=f'secret={body_secret}',
                message=f'secret={body_secret}',
                request_id=f'secret={body_secret}',
            )
            provider.script_ranges(body_failure)
        elif body_kind == 'transport':
            body_failure = socket.gaierror(body_secret)
            provider.script_ranges(body_failure)
        else:
            provider.blob.generation = _HostileUploadMetadata(body_secret)
        provider.close_started = close_started
        provider.close_release = close_release
        _install_download_provider(monkeypatch, provider)
        operation = request(
            'gs://precedence-download/folder/object.bin',
            protocol='GCS',
            protocol_info={
                'command': 'download',
                'local_path': str(source),
                'max_response_bytes': 128,
                'timeout': 2.5,
                'circuit_breaker_config': breaker_config,
            },
        )

    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    companion_leases = [acquire() for _ in range(3)]
    task = asyncio.create_task(operation)
    admitted_after: Any = None
    try:
        await _wait_for_thread_event(close_started)
        assert getattr(gcs_client, '_active_gcs_leases')() == 4
        task.cancel(cleanup_secret)
        await asyncio.sleep(0)
        assert task.done() is False
        with pytest.raises(GcsCapacityError):
            acquire()

        close_release.set()
        result = await task
        assert task.exception() is None
        assert getattr(gcs_client, '_active_gcs_leases')() == 3
        admitted_after = acquire()
        with pytest.raises(GcsCapacityError):
            acquire()

        expected_type = (
            'DnsError' if expected_code == 'DNS' else 'GcsStatusError')
        expected_message = (
            'GCS provider transport failed'
            if expected_code == 'DNS'
            else 'GCS provider service request failed'
        )
        assert result['ok'] is False
        assert result['status_code'] == expected_status
        assert result['error'] == {
            'type': expected_type,
            'code': expected_code,
            'message': expected_message,
            'cause': (
                'gaierror: GCS provider transport failure'
                if expected_code == 'DNS'
                else None
            ),
        }
        if body_kind == 'service':
            assert result['protocol_details'] == {
                'command': command,
                'bucket': f'precedence-{command}',
                'target': (
                    'object.bin'
                    if command == 'upload'
                    else 'folder/object.bin'
                ),
                'gcs_error_code': None,
                'gcs_error_message': None,
                'response_metadata': {
                    'http_status_code': 403,
                    'request_id': None,
                },
            }
        else:
            assert result['protocol_details'] == {}
        assert events == expected_events
        assert breaker.failures == (1 if body_kind == 'transport' else 0)
        assert provider.client.close_calls == 1
        assert source.read_bytes() == original_path_bytes
        if command == 'upload':
            assert reads == [(str(source), 128)]
            assert len(provider.upload_calls) == 1
        else:
            assert len(provider.reload_calls) == 1
            assert len(provider.range_calls) == (
                0 if body_kind == 'malformed' else 1)
        exception_surface = (
            ''.join(traceback.format_exception(body_failure))
            if body_failure is not None
            else ''
        )
        surfaces = (
            repr(result)
            + _logged_gcs_surfaces(caplog)
            + repr(events)
            + exception_surface
        )
        assert all(secret not in surfaces for secret in (
            body_secret, cleanup_secret, payload_secret,
        ))
    finally:
        close_release.set()
        await asyncio.gather(task, return_exceptions=True)
        if admitted_after is not None:
            await _release_lease(admitted_after)
        for lease in companion_leases:
            await _release_lease(lease)


@pytest.mark.parametrize(
    ('cleanup_error', 'expected_code', 'expected_status'),
    [
        pytest.param(socket.gaierror('cleanup-dns-private-d2b1'),
                     'DNS', 502, id='transport'),
        pytest.param(google_auth_exceptions.DefaultCredentialsError(
            'cleanup-credential-private-d2b1'), 'CONFIG', 400,
            id='credential'),
        pytest.param(_FakeServiceError(503), 'GCS_STATUS', 503,
                     id='service'),
    ],
)
async def test_download_cleanup_only_failure_has_stable_public_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_error: BaseException,
    expected_code: str,
    expected_status: int,
) -> None:
    """Owned-client cleanup alone maps to the stable public vocabulary."""
    events: list[str] = []
    provider = _DownloadProvider(b'x')
    provider.script_close(cleanup_error)
    target = tmp_path / 'cleanup-only.bin'
    target.write_bytes(b'existing-target')

    result, _ = await _public_retrying_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=1,
        retries=0,
        events=events,
    )

    assert result['ok'] is False
    assert result['status_code'] == expected_status
    assert result['error']['code'] == expected_code
    assert result['protocol_details'] == {}
    assert provider.client.close_calls == 1
    assert events == (
        ['failed', 'exhausted']
        if expected_code == 'DNS'
        else ['abort']
    )


async def test_download_failure_redacts_provider_credential_and_local_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failure surfaces retain no provider, credential, or local-path prose."""
    sentinels = {
        'credential': 'download-credential-private-d2b1',
        'provider': 'download-provider-private-d2b1',
        'target': 'download-local-target-private-d2b1',
        'bearer': 'download-bearer-private-d2b1',
    }
    events: list[str] = []
    provider = _DownloadProvider(b'x')
    provider.credentials.__repr__ = lambda: sentinels['credential']
    hostile = ' authorization=Bearer '.join(sentinels.values())
    provider.script_ranges(_FakeServiceError(
        403,
        code=hostile,
        message=hostile,
        request_id=hostile,
    ))
    target = tmp_path / sentinels['target']
    target.write_bytes(b'existing-target')
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, _ = await _public_retrying_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=1,
        retries=1,
        events=events,
    )

    surfaces = repr(result) + _logged_gcs_surfaces(caplog) + repr(events)
    assert result['error']['code'] == 'GCS_STATUS'
    assert target.read_bytes() == b'existing-target'
    assert all(secret not in surfaces for secret in sentinels.values())


# --- AGW-48 tranche D2b2: download cancellation and timeout draining -----


async def test_download_pin_cancellation_drains_and_preserves_original(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cancelled metadata pin drains, closes, and preserves its target."""
    provider = _DownloadProvider(b'x')
    provider.reload_started = threading.Event()
    provider.reload_release = threading.Event()
    range_sentinel = 'download-late-reload-private-d2b2'
    close_sentinel = 'download-reload-close-private-d2b2'
    provider.script_reload(RuntimeError(range_sentinel))
    provider.script_close(RuntimeError(close_sentinel))
    target = tmp_path / 'cancelled-pin.bin'
    target.write_bytes(b'existing-target')
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    loop_thread = threading.get_ident()
    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    other_leases = [acquire() for _ in range(3)]
    task = asyncio.create_task(_public_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=1,
    ))
    admitted_after: Any = None
    try:
        assert provider.reload_started is not None
        await _wait_for_thread_event(provider.reload_started)
        task.cancel('first-download-pin-cancellation')
        task.cancel('repeated-download-pin-cancellation')
        with pytest.raises(GcsCapacityError):
            acquire()

        assert provider.reload_release is not None
        provider.reload_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task

        surfaces = (
            ''.join(traceback.format_exception(caught.value))
            + _logged_gcs_surfaces(caplog)
        )
        assert caught.value.args == ('first-download-pin-cancellation',)
        assert range_sentinel not in surfaces
        assert close_sentinel not in surfaces
        assert target.read_bytes() == b'existing-target'
        assert list(tmp_path.iterdir()) == [target]
        assert provider.range_calls == []
        assert provider.client.close_calls == 1
        assert provider.timeline[-1] == 'client-close'
        close_thread, close_name = provider.threads['client-close'][0]
        assert close_thread != loop_thread
        assert close_name.startswith('asyncio-gateway-gcs')

        admitted_after = acquire()
        with pytest.raises(GcsCapacityError):
            acquire()
    finally:
        if provider.reload_release is not None:
            provider.reload_release.set()
        await asyncio.gather(task, return_exceptions=True)
        if admitted_after is not None:
            await _release_lease(admitted_after)
        for lease in other_leases:
            await _release_lease(lease)


@pytest.mark.parametrize('outcome', ['cancel', 'timeout'])
async def test_download_range_drain_retains_capacity_through_hostile_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    """Late range work and hostile close cannot replace the first outcome."""
    provider = _DownloadProvider(b'x')
    range_started = threading.Event()
    range_release = threading.Event()
    provider.close_started = threading.Event()
    provider.close_release = threading.Event()
    range_sentinel = f'download-late-range-{outcome}-private-d2b2'
    close_sentinel = f'download-close-{outcome}-private-d2b2'

    def blocked_range(arguments: Mapping[str, object]) -> bytes:
        """Return late bytes or failure after deterministic release."""
        del arguments
        range_started.set()
        assert range_release.wait(timeout=1)
        if outcome == 'cancel':
            raise RuntimeError(range_sentinel)
        return b'x'

    provider.range_handler = blocked_range
    provider.script_close(RuntimeError(close_sentinel))
    expiry_injected = asyncio.Event()
    if outcome == 'timeout':
        real_wait_for = asyncio.wait_for
        expired = False

        async def expire_started_range_once(
            awaitable: Any,
            *,
            timeout: int | float,
        ) -> Any:
            """Expire only the wait whose provider range has started."""
            nonlocal expired
            if range_started.is_set() and not expired:
                expired = True
                operation = asyncio.ensure_future(awaitable)
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                expiry_injected.set()
                raise asyncio.TimeoutError
            return await real_wait_for(awaitable, timeout=timeout)

        monkeypatch.setattr(
            gcs_client.asyncio, 'wait_for', expire_started_range_once)

    target = tmp_path / f'{outcome}-range.bin'
    target.write_bytes(b'existing-target')
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    loop_thread = threading.get_ident()
    acquire = getattr(gcs_client, '_acquire_gcs_lease')
    other_leases = [acquire() for _ in range(3)]
    task = asyncio.create_task(_public_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=1,
    ))
    admitted_after: Any = None
    try:
        await _wait_for_thread_event(range_started)
        if outcome == 'cancel':
            task.cancel('first-download-range-cancellation')
            task.cancel('repeated-download-range-cancellation')
        else:
            await expiry_injected.wait()
        with pytest.raises(GcsCapacityError):
            acquire()

        range_release.set()
        assert provider.close_started is not None
        await _wait_for_thread_event(provider.close_started)
        with pytest.raises(GcsCapacityError):
            acquire()
        if outcome == 'cancel':
            task.cancel('cancellation-during-download-close')

        assert provider.close_release is not None
        provider.close_release.set()
        if outcome == 'cancel':
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args == (
                'first-download-range-cancellation',)
            surfaces = ''.join(traceback.format_exception(caught.value))
        else:
            result, _ = await task
            assert result['ok'] is False
            assert result['error']['code'] == 'TIMEOUT'
            surfaces = repr(result)
        surfaces += _logged_gcs_surfaces(caplog)

        assert range_sentinel not in surfaces
        assert close_sentinel not in surfaces
        assert target.read_bytes() == b'existing-target'
        assert list(tmp_path.iterdir()) == [target]
        assert len(provider.range_calls) == 1
        assert provider.client.close_calls == 1
        assert provider.timeline[-1] == 'client-close'
        assert provider.timeline.index('range') < provider.timeline.index(
            'client-close')
        for seam in ('range', 'client-close'):
            worker_thread, worker_name = provider.threads[seam][0]
            assert worker_thread != loop_thread
            assert worker_name.startswith('asyncio-gateway-gcs')

        admitted_after = acquire()
        with pytest.raises(GcsCapacityError):
            acquire()
    finally:
        range_release.set()
        if provider.close_release is not None:
            provider.close_release.set()
        await asyncio.gather(task, return_exceptions=True)
        if admitted_after is not None:
            await _release_lease(admitted_after)
        for lease in other_leases:
            await _release_lease(lease)


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


# --- AGW-49 focused coverage defect loop --------------------------------


async def test_drain_retains_first_of_repeated_cancellations() -> None:
    """Repeated waiter cancellation preserves the first cancellation."""
    future: asyncio.Future[str] = (
        asyncio.get_running_loop().create_future())
    task = asyncio.create_task(
        getattr(gcs_client, '_drain_provider_future')(future, None))
    await asyncio.sleep(0)

    task.cancel('first-drain-cancellation')
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel('repeated-drain-cancellation')
    await asyncio.sleep(0)
    assert not task.done()
    future.set_result('late-result')

    outcome, cancellation = await task

    assert outcome == 'late-result'
    assert cancellation is not None
    assert cancellation.args == ('first-drain-cancellation',)


async def test_drain_consumes_exceptional_underlying_future() -> None:
    """An exceptional worker future is drained into the private sentinel."""
    future: asyncio.Future[str] = (
        asyncio.get_running_loop().create_future())
    task = asyncio.create_task(
        getattr(gcs_client, '_drain_provider_future')(future, None))
    await asyncio.sleep(0)
    failure = RuntimeError('drained-provider-failure')

    future.set_exception(failure)
    outcome, cancellation = await task

    assert outcome is getattr(gcs_client, '_NO_RESULT')
    assert cancellation is None


async def test_released_lease_rejects_further_provider_work() -> None:
    """A released lease refuses execution without invoking the callable."""
    lease = getattr(gcs_client, '_acquire_gcs_lease')()
    calls: list[str] = []
    await _release_lease(lease)

    with pytest.raises(GcsCapacityError):
        await lease.run(
            lambda: calls.append('unexpected'),
            timeout=1,
        )

    assert calls == []
    assert getattr(gcs_client, '_active_gcs_leases')() == 0


class _ScriptedLoopClock:
    """Proxy the real loop while returning exact production clock samples."""

    def __init__(self, loop: Any, samples: list[float]) -> None:
        """Retain the real executor seam and ordered monotonic samples."""
        self.loop = loop
        self.samples = list(samples)

    def time(self) -> float:
        """Return the next deterministic result-acceptance clock sample."""
        assert self.samples
        return self.samples.pop(0)

    def run_in_executor(
        self,
        executor: object,
        function: Any,
        *args: object,
    ) -> Any:
        """Delegate provider submission to the real running loop."""
        return self.loop.run_in_executor(executor, function, *args)


class _AsyncioClockProxy:
    """Override only ``get_running_loop`` for deterministic clock control."""

    def __init__(self, clock: _ScriptedLoopClock) -> None:
        """Retain one scripted loop-clock facade."""
        self.clock = clock

    def get_running_loop(self) -> _ScriptedLoopClock:
        """Return the deterministic facade used by the provider lease."""
        return self.clock

    def __getattr__(self, name: str) -> Any:
        """Delegate every non-clock asyncio primitive unchanged."""
        return getattr(asyncio, name)


async def test_post_wait_deadline_and_nonfinite_drain_are_fail_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A late accepted result times out and infinite telemetry becomes zero."""
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    module, executor = _fresh_gcs_lifecycle_module()
    real_loop = asyncio.get_running_loop()
    clock = _ScriptedLoopClock(real_loop, [10.0, 10.0, 11.0, 20.0,
                                           math.inf])
    setattr(module, 'asyncio', _AsyncioClockProxy(clock))
    lease = getattr(module, '_acquire_gcs_lease')()
    try:
        with pytest.raises(GatewayTimeoutError):
            await lease.run(lambda: 'completed-at-deadline', timeout=1)

        drain_events = [
            fields
            for event, fields in _capacity_events(caplog)
            if event == 'gcs_drain_finished'
        ]
        assert drain_events == [{
            'active_leases': 1,
            'duration_seconds': 0.0,
        }]
        assert clock.samples == []
    finally:
        await _release_isolated_lease(lease)
        executor.close_test_executor()


async def test_list_missing_server_token_attribute_fails_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page iterator without its token attribute is malformed success."""
    provider = _HeadListProvider()
    page = provider.iterator.page

    class IteratorWithoutToken:
        """Expose one valid page but omit ``next_page_token`` entirely."""

        @property
        def pages(self) -> Any:
            """Return exactly one deterministic page."""
            return iter((page,))

    # Reason for type: ignore[assignment] -- inject a malformed iterator shape.
    provider.iterator = IteratorWithoutToken()  # type: ignore[assignment]
    _install_head_list_provider(monkeypatch, provider)

    result = await request(
        'gs://head-list-bucket/prefix/',
        protocol='GCS',
        protocol_info={'command': 'list', 'max_items': 1},
    )

    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {}
    assert provider.client.close_calls == 1


def test_foreign_provider_fields_are_never_stringified() -> None:
    """Foreign code, message, and request-ID objects normalize to None."""

    class HostileText:
        """Fail loudly if normalization tries to stringify provider state."""

        def __str__(self) -> NoReturn:
            """Reject accidental provider-object stringification."""
            raise AssertionError('foreign provider value was stringified')

    hostile = HostileText()
    error = _FakeServiceError(403)
    # Reason for type: ignore[assignment] -- inject foreign provider fields.
    error.code = hostile  # type: ignore[assignment]
    error.message = hostile  # type: ignore[assignment]
    error.response['headers']['x-goog-request-id'] = hostile

    failure = getattr(gcs_client, '_service_failure_for')(
        error,
        command='head',
        bucket='bucket',
        target='object',
    )

    assert failure.details['gcs_error_code'] is None
    assert failure.details['gcs_error_message'] is None
    assert failure.details['response_metadata']['request_id'] is None


def test_service_response_object_headers_are_normalized() -> None:
    """Non-mapping response objects supply status and request-ID headers."""

    class Response:
        """Model the attribute-based SDK response shape."""

        status_code = 429
        headers = {'x-goog-request-id': 'attribute-request-id'}

    error = _FakeServiceError(502)
    error.response = Response()

    failure = getattr(gcs_client, '_service_failure_for')(
        error,
        command='list',
        bucket='bucket',
        target='prefix/',
    )

    assert failure.status_code == 429
    assert failure.details['response_metadata'] == {
        'http_status_code': 429,
        'request_id': 'attribute-request-id',
    }


class _OrdinaryCommandBreaker:
    """Raise one exact breaker wrapper without invoking provider work."""

    def __init__(self, outcome: str, sentinel: str) -> None:
        """Retain the requested refusal and one hostile private value."""
        self.outcome = outcome
        self.sentinel = sentinel
        self.calls = 0

    async def run(self, call: Any, *args: Any, **kwargs: Any) -> Any:
        """Refuse before invoking the supplied ordinary-command attempt."""
        del call, args, kwargs
        self.calls += 1
        if self.outcome == 'open':
            raise CircuitOpen()
        try:
            raise RuntimeError(self.sentinel)
        except RuntimeError as cause:
            raise RetriesExhausted() from cause


def _ordinary_command_case(command: str) -> tuple[str, dict[str, object]]:
    """Return one valid target and options mapping for an ordinary command."""
    target = 'object.bin'
    info: dict[str, object] = {'command': command}
    if command == 'upload':
        info.update({'local_path': '/caller/upload.bin',
                     'max_upload_bytes': 1})
    elif command == 'download':
        info.update({'local_path': '/caller/download.bin',
                     'max_response_bytes': 1})
    elif command == 'list':
        target = 'prefix/'
        info['max_items'] = 1
    return target, info


@pytest.mark.parametrize('command', ['upload', 'download', 'head', 'list'])
@pytest.mark.parametrize(
    ('outcome', 'expected_code', 'expected_status'),
    [
        pytest.param('open', 'CIRCUIT_OPEN', 503, id='circuit-open'),
        pytest.param('unknown', 'TRANSPORT', 502,
                     id='unknown-retries-exhausted-cause'),
    ],
)
async def test_ordinary_command_breaker_refusals_are_stable_and_safe(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    outcome: str,
    expected_code: str,
    expected_status: int,
) -> None:
    """All ordinary commands map open/unknown breaker wrappers exactly."""
    sentinel = f'{command}-{outcome}-breaker-private'
    breaker = _OrdinaryCommandBreaker(outcome, sentinel)
    provider_calls: list[str] = []
    read_calls: list[object] = []

    def forbidden_adc(*args: object, **kwargs: object) -> NoReturn:
        """Reject any provider call after a breaker refusal."""
        del args, kwargs
        provider_calls.append('adc')
        raise AssertionError('provider must not run after breaker refusal')

    async def guarded_read(
        path: object,
        *,
        max_bytes: int,
        chunk_size: int = 65536,
    ) -> bytes:
        """Supply the upload's allowed pre-breaker guarded body."""
        del max_bytes, chunk_size
        read_calls.append(path)
        return b'x'

    monkeypatch.setattr(
        base, 'get_breaker', lambda *args, **kwargs: breaker)
    monkeypatch.setattr(google_auth, 'default', forbidden_adc)
    monkeypatch.setattr(gcs_client, 'read_guarded_file', guarded_read)
    target, info = _ordinary_command_case(command)

    result = await request(
        f'gs://ordinary-command-bucket/{target}',
        protocol='GCS',
        protocol_info=info,
    )

    assert set(result) == set(GatewayResponse.__annotations__)
    assert result['ok'] is False
    assert result['status_code'] == expected_status
    assert result['error']['code'] == expected_code
    assert result['protocol_details'] == {}
    assert sentinel not in repr(result)
    assert breaker.calls == 1
    assert provider_calls == []
    assert read_calls == (
        ['/caller/upload.bin'] if command == 'upload' else [])
    assert getattr(gcs_client, '_active_gcs_leases')() == 0


@pytest.mark.parametrize('command', ['head', 'list'])
async def test_head_list_cleanup_repeated_cancellation_preserves_first(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """Cleanup-only repeated cancellation wins before detail publication."""
    provider = _HeadListLifecycleProvider('no-body-block')
    _install_head_list_provider(monkeypatch, provider)
    target = 'object.txt' if command == 'head' else 'prefix/'
    info: dict[str, object] = {'command': command, 'timeout': 1}
    if command == 'list':
        info['max_items'] = 1
    url = f'gs://head-list-cleanup-bucket/{target}'
    response = _response(url)
    built = GcsRequest(
        url,
        None,
        response,
        info=_validate(info),
        redact_params=frozenset(),
    )
    task = asyncio.create_task(built.handle_request())
    try:
        await _wait_for_thread_event(provider.close_started)
        assert response['protocol_details'] == {}
        task.cancel('first-cleanup-cancellation')
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel('repeated-cleanup-cancellation')
        await asyncio.sleep(0)
        assert not task.done()
        provider.close_release.set()

        with pytest.raises(asyncio.CancelledError) as caught:
            await task

        assert caught.value.args == ('first-cleanup-cancellation',)
        assert response['protocol_details'] == {}
        assert provider.close_sentinel not in ''.join(
            traceback.format_exception(caught.value))
        assert getattr(gcs_client, '_active_gcs_leases')() == 0
    finally:
        provider.close_release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_download_writer_underreport_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A writer count below the immutable pin is malformed provider success."""
    provider = _DownloadProvider(b'raw')
    target = tmp_path / 'underreported.bin'

    async def underreporting_writer(
        path: object,
        chunks: Any,
        **kwargs: object,
    ) -> int:
        """Consume the exact raw stream but under-report its final count."""
        del path, kwargs
        observed = b''.join([chunk async for chunk in chunks])
        assert observed == b'raw'
        return len(observed) - 1

    monkeypatch.setattr(
        gcs_client, 'stream_to_path', underreporting_writer)

    result, breaker = await _public_download(
        monkeypatch,
        provider,
        target,
        max_response_bytes=3,
    )

    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {}
    assert breaker.calls == 1
    assert provider.client.close_calls == 1
    assert not target.exists()


async def test_download_unknown_cleanup_defect_preserves_exact_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A close-time programming defect escapes with identity and cause."""
    provider = _DownloadProvider(b'raw')
    cause = ValueError('download-cleanup-cause')
    cleanup_error = RuntimeError('download-cleanup-defect')
    cleanup_error.__cause__ = cause
    cleanup_error.__suppress_context__ = True
    provider.script_close(cleanup_error)
    target = tmp_path / 'committed-before-close-defect.bin'
    breaker = _UploadBreaker(provider.timeline)
    monkeypatch.setattr(
        base, 'get_breaker', lambda *args, **kwargs: breaker)
    _install_download_provider(monkeypatch, provider)
    url = 'gs://download-bucket/object.bin'
    response = _response(url)
    built = GcsRequest(
        url,
        None,
        response,
        info=_validate({
            'command': 'download',
            'local_path': str(target),
            'max_response_bytes': 3,
        }),
        redact_params=frozenset(),
    )

    with pytest.raises(RuntimeError) as caught:
        await built.handle_request()

    assert caught.value is cleanup_error
    assert caught.value.args == ('download-cleanup-defect',)
    assert caught.value.__cause__ is cause
    assert caught.value.__suppress_context__ is True
    assert target.read_bytes() == b'raw'
    assert response['protocol_details'] == {}
    assert provider.client.close_calls == 1
    assert getattr(gcs_client, '_active_gcs_leases')() == 0


# --- AGW-50 contribution-audit defect loop: exact bearer boundary -------


@pytest.mark.parametrize(
    ('url', 'private_target_text'),
    [
        pytest.param(
            'gs://bucket/private-prefix-sentinel-agw50/',
            'private-prefix-sentinel-agw50',
            id='trailing-prefix',
        ),
        pytest.param(
            'gs://bucket/private-star-sentinel-agw50/*.txt',
            'private-star-sentinel-agw50',
            id='star-wildcard',
        ),
        pytest.param(
            'gs://bucket/private-question-sentinel-agw50?.txt',
            'private-question-sentinel-agw50',
            id='question-wildcard',
        ),
        pytest.param(
            'gs://bucket/private-bracket-sentinel-agw50/[ab].txt',
            'private-bracket-sentinel-agw50',
            id='bracket-class',
        ),
    ],
)
def test_signed_url_rejects_prefix_and_glob_targets_before_breaker(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    private_target_text: str,
) -> None:
    """Signed grants reject prefix/glob shapes before any side effect."""
    def unexpected_side_effect(
        *args: object,
        **kwargs: object,
    ) -> NoReturn:
        """Fail if hostile target data reaches a later boundary."""
        del args, kwargs
        raise AssertionError(
            'breaker, ADC, and provider work must not run')

    monkeypatch.setattr(base, 'get_breaker', unexpected_side_effect)
    monkeypatch.setattr(google_auth, 'default', unexpected_side_effect)
    monkeypatch.setattr(storage, 'Client', unexpected_side_effect)

    with pytest.raises(ConfigurationError) as caught:
        GcsRequest(
            url,
            None,
            _response(url),
            info=_validate({
                'command': 'signed_url',
                'method': 'GET',
            }),
            redact_params=frozenset(),
        )

    assert private_target_text not in str(caught.value)


def test_signed_url_accepts_exact_nested_object_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path separators remain valid inside one exact object name."""
    calls: list[tuple[object, ...]] = []
    breaker = object()

    def record_breaker(*args: object, **kwargs: object) -> object:
        """Capture the sole allowed constructor-side interaction."""
        calls.append((*args, kwargs))
        return breaker

    monkeypatch.setattr(base, 'get_breaker', record_breaker)
    url = 'gs://Exact-Object-Bucket/folder/nested/object.bin'

    built = GcsRequest(
        url,
        None,
        _response(url),
        info=_validate({'command': 'signed_url', 'method': 'GET'}),
        redact_params=frozenset(),
    )

    assert built.key == 'folder/nested/object.bin'
    assert built.circuit_breaker is breaker
    assert calls == [(
        'gs', 'exact-object-bucket', UNKNOWN_PORT, {}, {})]


@pytest.mark.parametrize(
    ('generation_outcome', 'private_marker'),
    [
        pytest.param(None, None, id='none'),
        pytest.param(
            {'opaque': 'malformed-url-private-agw50'},
            'malformed-url-private-agw50',
            id='mapping',
        ),
        pytest.param(
            ['malformed-url-private-agw50'],
            'malformed-url-private-agw50',
            id='list',
        ),
        pytest.param(
            b'malformed-url-private-agw50',
            'malformed-url-private-agw50',
            id='bytes',
        ),
        pytest.param(9283746501, '9283746501', id='integer'),
        pytest.param('', None, id='empty-string'),
    ],
)
async def test_signed_url_malformed_nominal_success_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    generation_outcome: object,
    private_marker: str | None,
) -> None:
    """Only a non-empty string may cross the signing success boundary."""
    provider = _SignedUrlFailureProvider()
    provider.generation_outcome = generation_outcome
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result, breaker = await _public_signed_url_failure(
        monkeypatch, provider)

    assert provider.timeline.count('generate') == 1
    assert len(provider.generate_calls) == 1
    assert provider.client.close_calls == 1
    assert provider.client.closed is True
    assert breaker.calls == 0
    assert result['ok'] is False
    assert result['status_code'] == 502
    assert result['error']['code'] == 'GCS_STATUS'
    assert result['protocol_details'] == {}
    surfaces = repr(result) + _logged_gcs_surfaces(caplog)
    if private_marker is not None:
        assert private_marker not in surfaces


def _exception_direct_strings(error: BaseException) -> list[str]:
    """Collect direct string args across a bounded exception graph."""
    strings: list[str] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        strings.extend(
            arg for arg in current.args if isinstance(arg, str))
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return strings


def _traceback_direct_string_leaks(
    error: BaseException,
    secret: str,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Inspect live traceback frames without rendering foreign objects."""
    frame_names: list[str] = []
    leaks: list[tuple[str, str]] = []
    current = error.__traceback__
    while current is not None:
        frame_name = current.tb_frame.f_code.co_name
        frame_names.append(frame_name)
        for local_name, value in dict(
            current.tb_frame.f_locals
        ).items():
            if isinstance(value, str) and secret in value:
                leaks.append((frame_name, local_name))
        current = current.tb_next
    return frame_names, leaks


async def test_signed_url_unknown_close_defect_scrubs_live_bearer_state(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unchanged programming defect identity retains no bearer state."""
    provider = _SignedUrlFailureProvider()
    provider.signed_url = (
        'https://storage.googleapis.com/private-close-bucket/object.bin'
        '?X-Goog-Algorithm=GOOG4-RSA-SHA256'
        '&X-Goog-Credential=private-close-credential-agw50'
        '&X-Goog-Signature=private-close-signature-agw50')
    provider.generation_outcome = provider.signed_url
    cause = ValueError(provider.signed_url)
    context = LookupError(provider.signed_url)
    cleanup_error = RuntimeError(provider.signed_url)
    cleanup_error.__cause__ = cause
    cleanup_error.__context__ = context
    cleanup_error.__suppress_context__ = True
    provider.close_error = cleanup_error
    breaker = _install_signed_url_provider(monkeypatch, provider)
    response = _response(
        'gs://private-close-bucket/object.bin')
    strategy = GcsRequest(
        'gs://private-close-bucket/object.bin',
        None,
        response,
        _validate({'command': 'signed_url', 'method': 'GET'}),
        redact_params=frozenset(),
    )
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    with pytest.raises(RuntimeError) as caught:
        await strategy.handle_request()

    frame_names, traceback_leaks = _traceback_direct_string_leaks(
        caught.value, provider.signed_url)
    exception_leaks = [
        value for value in _exception_direct_strings(caught.value)
        if provider.signed_url in value
    ]
    assert caught.value is cleanup_error
    assert '_signed_url_attempt' in frame_names
    assert response['protocol_details'] == {}
    assert provider.timeline.count('generate') == 1
    assert len(provider.generate_calls) == 1
    assert provider.client.close_calls == 1
    assert breaker.calls == 0
    assert provider.signed_url not in (
        repr(response) + _logged_gcs_surfaces(caplog))
    assert {
        'exception_state': exception_leaks,
        'traceback_locals': traceback_leaks,
    } == {
        'exception_state': [],
        'traceback_locals': [],
    }


# --- AGW-50 security defect: signing-principal containment ---------------


@pytest.mark.parametrize(
    'signer_path',
    [
        pytest.param('direct', id='direct-signer'),
        pytest.param('impersonated', id='configured-impersonation'),
    ],
)
async def test_public_signing_forbidden_never_exposes_signer_identity(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    signer_path: str,
) -> None:
    """A plain IAM principal in signing prose stays private everywhere."""
    info: dict[str, object] = {
        'command': 'signed_url',
        'method': 'GET',
        'timeout': 2.5,
    }
    if signer_path == 'direct':
        provider: _SignedUrlProvider = _SignedUrlProvider()
        principal = (
            'direct-signer@example-project.iam.gserviceaccount.com')
        selected_credentials = provider.credentials
        expected_project = 'signed-url-project'
        breaker = _install_signed_url_provider(monkeypatch, provider)
    else:
        provider = _ImpersonationProvider(source_valid=True)
        principal = provider.signing_target
        selected_credentials = provider.target_credentials
        expected_project = 'source-adc-project'
        info['signing_service_account'] = principal
        breaker = _install_impersonation_provider(monkeypatch, provider)

    provider_message = (
        'Permission iam.serviceAccounts.signBlob denied for '
        f'{principal}')
    provider_error = google_api_exceptions.Forbidden(provider_message)
    assert '=' not in provider_message
    assert '://' not in provider_message

    def refuse_signing(**kwargs: object) -> str:
        """Raise one real Google IAM refusal from the provider boundary."""
        provider.record_thread('generate')
        provider.generate_calls.append(dict(kwargs))
        provider.client_open_during_generate.append(
            not provider.client.closed)
        assert provider.response is not None
        provider.details_during_generate.append(dict(
            provider.response['protocol_details']))
        raise provider_error

    monkeypatch.setattr(
        provider.blob, 'generate_signed_url', refuse_signing)

    async def capture_response(*, response: GatewayResponse) -> str:
        """Expose only the live envelope to deterministic test doubles."""
        provider.response = response
        return 'captured-before-signing-refusal'

    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')
    result = await request(
        'gs://signer-containment-bucket/folder/object.bin',
        protocol='GCS',
        protocol_info=info,
        pre_processor_config={'function': capture_response},
    )

    assert result['ok'] is False
    assert result['status_code'] == 403
    assert result['error'] is not None
    assert result['error']['code'] == 'GCS_STATUS'
    details = result['protocol_details']
    assert {
        key: value for key, value in details.items()
        if key != 'gcs_error_message'
    } == {
        'command': 'signed_url',
        'bucket': 'signer-containment-bucket',
        'target': 'folder/object.bin',
        'gcs_error_code': None,
        'response_metadata': {
            'http_status_code': 403,
            'request_id': None,
        },
    }
    assert isinstance(details['gcs_error_message'], (str, type(None)))

    assert provider.generate_calls == [{
        'version': 'v4',
        'expiration': timedelta(seconds=900),
        'method': 'GET',
        'credentials': selected_credentials,
    }]
    assert provider.client_calls == [(
        (),
        {
            'credentials': selected_credentials,
            'project': expected_project,
        },
    )]
    assert provider.client_open_during_generate == [True]
    assert provider.details_during_generate == [{}]
    assert provider.details_during_close == [{}]
    assert provider.timeline.count('generate') == 1
    assert provider.timeline.index('generate') < provider.timeline.index(
        'client-close')
    assert provider.client.close_calls == 1
    assert provider.client.closed is True
    assert breaker.calls == 0
    assert not {
        'method', 'expires_in_seconds', 'signed_url', 'content_type',
        'max_upload_bytes', 'if_generation_match', 'required_headers',
    }.intersection(details)
    assert provider.signed_url not in repr(result)

    capacity_records = _capacity_records(caplog)
    assert [
        (
            record.getMessage(),
            record.active_leases,
            record.max_leases,
        )
        for record in capacity_records
    ] == [
        ('gcs_capacity_state', 1, 4),
        ('gcs_capacity_state', 0, 4),
    ]
    assert getattr(gcs_client, '_active_gcs_leases')() == 0

    leaks = {
        'envelope_leaves': [
            path for path, value in _surface_leaves(result)
            if isinstance(value, str) and principal in value
        ],
        'result_repr': principal in repr(result),
        'caplog': principal in _logged_gcs_surfaces(caplog),
        'breaker': principal in repr(breaker.__dict__),
        'capacity_telemetry': principal in ''.join(
            repr(record.__dict__) for record in capacity_records),
    }
    assert leaks == {
        'envelope_leaves': [],
        'result_repr': False,
        'caplog': False,
        'breaker': False,
        'capacity_telemetry': False,
    }

"""Boundary-contract tests for the bounded Google Cloud Storage selector.

These tests stop at public configuration and constructor validation. They do
not construct credentials, contact Google Cloud, touch local paths, or execute
an object operation.
"""

import math
from collections.abc import Mapping
from typing import Any, NoReturn

import pytest

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.helpers.internal import base
from asyncio_gateway.logic.gcs_client import GcsRequest
from asyncio_gateway.utils.constants import (
    HTTP_TIMEOUT,
    MAX_RESPONSE_BYTES,
    UNKNOWN_PORT,
)
from asyncio_gateway.utils.envelope import new_envelope
from asyncio_gateway.utils.exceptions import ConfigurationError


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

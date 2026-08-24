"""Tests for the exception hierarchy and error model (spec R10, Step 5).

Covers R10-AC1 (one public base, every class pickling), AC4 (``__cause__``
unwrapped recursively), AC6 (a stable machine-readable code per error) and
the status mapping of Part B's table.

The defect the whole model exists for is FI-3: ``str(RetriesExhausted())``
is ``''``, and the resilience library raises exactly that on every failed
call. An empty error message reads as success, which is worse than the
fabricated ``999`` it replaces -- so "the message is never empty" is a
tested property here, not a convention.
"""

import pickle

from aiohttp import ClientConnectorError
from aiohttp.client_reqrep import ConnectionKey

from failsafe import RetriesExhausted

import orjson

import pytest

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.utils import exceptions
from asyncio_gateway.utils.exceptions import (
    AsyncGatewayError,
    CircuitOpenError,
    ConfigurationError,
    ConnectError,
    DnsError,
    FtpStatusError,
    GatewayTimeoutError,
    HostKeyError,
    HttpStatusError,
    LocalWriteError,
    PathContainmentError,
    ProtocolError,
    ResponseTooDeepError,
    ResponseTooLargeError,
    SerializationError,
    SftpStatusError,
    SoapFaultError,
    StackExhaustedError,
    TlsError,
    TransportError,
    UnsafeXmlError,
    UnsupportedVerbError,
    unwrap_cause,
)
from asyncio_gateway.utils.redaction import REDACTED
from asyncio_gateway.utils.status_map import (
    DEFAULT_STATUS,
    STATUS_BY_CODE,
    WARNING_CODES,
    status_for,
)

# (class, its parent, its wire-stable code, its default status) -- Part B's
# hierarchy diagram and status table, asserted rather than described.
HIERARCHY = [
    (ConfigurationError, AsyncGatewayError, 'CONFIG', 400),
    (UnsupportedVerbError, ConfigurationError, 'CONFIG', 400),
    (SerializationError, AsyncGatewayError, 'SERIALIZATION', 502),
    (UnsafeXmlError, SerializationError, 'XML_UNSAFE', 502),
    (PathContainmentError, AsyncGatewayError, 'PATH', 400),
    # Shares `PATH` with the row above and is a sibling of it, not a
    # subclass. Both mean "the local destination will not take this
    # file", so a caller branches on the code once; only one of them is
    # a security finding, so the classes stay distinct (NEW-R10-1).
    (LocalWriteError, AsyncGatewayError, 'PATH', 400),
    (TransportError, AsyncGatewayError, 'TRANSPORT', 502),
    (ConnectError, TransportError, 'CONNECT', 502),
    (DnsError, TransportError, 'DNS', 502),
    (TlsError, TransportError, 'TLS', 502),
    (HostKeyError, TransportError, 'HOST_KEY', 495),
    (GatewayTimeoutError, TransportError, 'TIMEOUT', 504),
    (ResponseTooLargeError, TransportError, 'RESPONSE_TOO_LARGE', 502),
    (ResponseTooDeepError, TransportError, 'RESPONSE_TOO_DEEP', 502),
    (CircuitOpenError, AsyncGatewayError, 'CIRCUIT_OPEN', 503),
    (StackExhaustedError, AsyncGatewayError, 'STACK_EXHAUSTED', 502),
    (ProtocolError, AsyncGatewayError, 'PROTOCOL', 502),
    (HttpStatusError, ProtocolError, 'HTTP_STATUS', 502),
    (FtpStatusError, ProtocolError, 'FTP_STATUS', 500),
    (SftpStatusError, ProtocolError, 'SFTP_STATUS', 500),
    (SoapFaultError, ProtocolError, 'SOAP_FAULT', 502),
]


def _connector_error(reason: str) -> ClientConnectorError:
    """Build the connector failure aiohttp itself raises.

    Args:
        reason: The operating-system error text to report.

    Returns:
        A real ``ClientConnectorError`` for ``example.invalid:443``, so the
        test cannot pass against a shape aiohttp does not produce.
    """
    key = ConnectionKey(
        host='example.invalid',
        port=443,
        is_ssl=True,
        ssl=None,
        proxy=None,
        proxy_auth=None,
        proxy_headers_hash=None,
        server_hostname=None,
    )
    return ClientConnectorError(
        connection_key=key, os_error=OSError(111, reason))


def _retries_exhausted_from(cause: BaseException) -> RetriesExhausted:
    """Raise and catch the wrapper the resilience library really raises.

    Args:
        cause: The failure the wrapped call raised.

    Returns:
        A ``RetriesExhausted`` whose ``__cause__`` is ``cause`` and whose
        own ``str()`` is empty -- exactly what ``Failsafe.run`` produces.
    """
    try:
        raise RetriesExhausted() from cause
    except RetriesExhausted as raised:
        return raised


@pytest.mark.parametrize(
    'error_class, parent, code, status',
    [pytest.param(*row, id=row[0].__name__) for row in HIERARCHY],
)
def test_the_hierarchy_parents_codes_and_statuses(
    error_class: type,
    parent: type,
    code: str,
    status: int,
) -> None:
    """Each class sits under its parent with its documented code (R10-AC1).

    One base a consumer catches, one code they can branch on that does not
    move when the message is reworded.
    """
    assert issubclass(error_class, parent)
    assert issubclass(error_class, AsyncGatewayError)
    assert error_class.code == code
    assert error_class('boom').status_code == status


@pytest.mark.parametrize(
    'error_class',
    [pytest.param(row[0], id=row[0].__name__) for row in HIERARCHY],
)
def test_every_class_populates_exception_args(error_class: type) -> None:
    """``super().__init__(message)`` runs, so ``args`` and ``str`` work.

    The class this replaces did neither, which is why it could not be
    pickled across a process pool.
    """
    raised = error_class('the message')

    assert raised.args == ('the message',)
    assert str(raised) == 'the message'


@pytest.mark.parametrize(
    'original',
    [
        pytest.param(GatewayTimeoutError('timed out'), id='default-status'),
        pytest.param(HttpStatusError('gone', 410), id='remote-status'),
    ],
)
def test_an_error_survives_pickling(original: AsyncGatewayError) -> None:
    """Instances cross a process boundary intact (R10-AC1).

    Including a remote-supplied status, which replaying ``args`` alone
    would silently reset to the class default.
    """
    restored = pickle.loads(pickle.dumps(original))

    assert type(restored) is type(original)
    assert str(restored) == str(original)
    assert restored.status_code == original.status_code
    assert restored.code == original.code


def test_a_protocol_error_carries_the_real_remote_status() -> None:
    """The remote side's own status reaches the envelope, not a stand-in."""
    assert HttpStatusError('not found', 404).status_code == 404
    assert FtpStatusError('no such file', 550).status_code == 550
    assert SftpStatusError('permission denied', 403).status_code == 403


def test_every_declared_code_is_registered_in_the_status_table() -> None:
    """No class can declare a code the one status table does not know.

    The registry is what keeps ``status_code`` "always populated" true: an
    unregistered code would silently fall back to the default and nobody
    would notice.
    """
    declared = {row[0].code for row in HIERARCHY}
    declared.add(AsyncGatewayError.code)

    assert declared <= set(STATUS_BY_CODE)
    assert WARNING_CODES <= set(STATUS_BY_CODE)


def test_status_for_prefers_the_remote_status_over_the_default() -> None:
    """An override is the status the remote side actually returned."""
    assert status_for('HTTP_STATUS') == 502
    assert status_for('HTTP_STATUS', override=404) == 404
    assert status_for('NOT_A_CODE') == DEFAULT_STATUS


def test_fi3_retries_exhausted_from_a_connector_error_is_never_empty(
) -> None:
    """FI-3: the wrapper is empty, so the chain supplies the message.

    ``str(RetriesExhausted())`` is ``''``. A naive report of that reads as
    success -- no error text at all -- which is why this is the criterion
    the error model is built around.
    """
    wrapper = _retries_exhausted_from(_connector_error('Connection refused'))

    assert str(wrapper) == ''

    message, cause = unwrap_cause(wrapper)

    assert message
    assert 'Cannot connect to host example.invalid:443' in message
    assert 'RetriesExhausted' in message
    assert cause is not None
    assert 'ClientConnectorError' in cause


def test_unwrap_cause_keeps_its_own_message_when_it_has_one() -> None:
    """A wrapper with a message keeps it, and still reports the cause."""
    try:
        try:
            raise ValueError('inner')
        except ValueError as inner:
            raise ConnectError('outer') from inner
    except ConnectError as outer:
        message, cause = unwrap_cause(outer)

    assert message == 'outer'
    assert cause == 'ValueError: inner'


def test_unwrap_cause_reports_the_deepest_non_empty_message() -> None:
    """The useful text is at the bottom of the chain, not the top."""
    try:
        try:
            try:
                raise ValueError('the real reason')
            except ValueError as inner:
                raise RetriesExhausted() from inner
        except RetriesExhausted as middle:
            raise ConnectError('') from middle
    except ConnectError as outer:
        message, cause = unwrap_cause(outer)

    assert message == 'ConnectError: the real reason'
    assert cause == 'ValueError: the real reason'


def test_unwrap_cause_walks_context_when_there_is_no_cause() -> None:
    """An implicitly chained exception is still worth reporting."""
    try:
        try:
            raise ValueError('implicit')
        except ValueError:
            raise ConnectError('')
    except ConnectError as outer:
        message, cause = unwrap_cause(outer)

    assert message == 'ConnectError: implicit'
    assert cause == 'ValueError: implicit'


def test_unwrap_cause_falls_back_to_the_class_name() -> None:
    """An entirely empty chain still yields a non-empty message (E3)."""
    wrapper = _retries_exhausted_from(RetriesExhausted())

    message, cause = unwrap_cause(wrapper)

    assert message == 'RetriesExhausted'
    assert cause is None


@pytest.mark.parametrize('cycle_length', [1, 2])
def test_unwrap_cause_terminates_on_a_cyclic_chain(
    cycle_length: int,
) -> None:
    """A chain that points back at itself must not loop forever."""
    first = ConnectError('first')
    if cycle_length == 1:
        first.__cause__ = first
    else:
        second = ConnectError('second')
        first.__cause__ = second
        second.__cause__ = first

    message, _ = unwrap_cause(first)

    assert message == 'first'


def test_unwrap_cause_is_depth_bounded() -> None:
    """A very deep chain stops at ``max_depth`` rather than walking it all."""
    deepest = ValueError('too deep to reach')
    link: BaseException = deepest
    for _ in range(20):
        outer = ConnectError('')
        outer.__cause__ = link
        link = outer

    message, cause = unwrap_cause(link, max_depth=3)

    assert 'too deep to reach' not in message
    assert cause is None


def test_unwrap_cause_redacts_foreign_text_without_being_asked() -> None:
    """The seam holds for a consumer that never heard of redaction (E9).

    ``unwrap_cause`` is the one place text this library did not author
    becomes a caller-visible string, and the exceptions it walks into --
    ``aiohttp``'s ``NonHttpUrlClientError`` and friends -- stringify to
    the full URL, query string included. Called with no redaction
    argument at all, which is how a future third consumer will call it,
    it still must not hand back a live credential: the built-in name set
    is the base contract, not an opt-in.
    """
    wrapper = _retries_exhausted_from(
        ValueError('https://host/p?api_key=APIKEYSECRET'))

    message, cause = unwrap_cause(wrapper)

    assert 'APIKEYSECRET' not in message
    assert cause is not None
    assert 'APIKEYSECRET' not in cause
    # Masked, not dropped: a redactor that deleted the parameter would
    # satisfy the two assertions above and lose the diagnostic.
    assert REDACTED in message
    assert REDACTED in cause


def test_unwrap_cause_masks_a_caller_declared_name_when_given_one() -> None:
    """The caller's normalised set widens the seam's built-in names."""
    wrapper = _retries_exhausted_from(
        ValueError('https://host/p?session_id=SESSIONSECRET'))

    plain, _ = unwrap_cause(wrapper)
    extended, _ = unwrap_cause(wrapper, redact_params=frozenset({
        'session_id'}))

    assert 'SESSIONSECRET' in plain
    assert 'SESSIONSECRET' not in extended
    assert REDACTED in extended


def test_configuration_error_is_raisable_with_a_message_alone() -> None:
    """The ratified single-argument constructor is unchanged.

    ``ConfigurationError`` predates this hierarchy and is reparented into
    it rather than duplicated or dropped: same name, same one-argument
    call, and ``str()`` is still the message the existing callers assert
    substrings of.
    """
    raised = ConfigurationError('serialization must return str, got bytes')

    assert isinstance(raised, AsyncGatewayError)
    assert raised.code == 'CONFIG'
    assert raised.status_code == 400
    assert str(raised) == 'serialization must return str, got bytes'


def test_the_removed_exception_is_gone_not_deprecated() -> None:
    """``CustomGlobalException`` is removed, with no alias left behind.

    It was raised in one place and caught nowhere. An alias would preserve
    the trap it represents, so old code must fail loudly instead.
    """
    assert not hasattr(exceptions, 'CustomGlobalException')


async def test_a_configuration_error_escapes_request_synchronously(
) -> None:
    """A caller-configuration error raises out of ``request()``.

    Not every failure becomes an envelope, and this asymmetry is
    deliberate (spec :3815-3818): a configuration error raised *before
    dispatch* -- by ``request()``'s own guards or by the protocol object's
    constructor -- is a "raises" row in the failure table, because
    returning an envelope for a call that can never succeed invites a
    retry loop against it. Only a ``ConfigurationError`` raised *inside*
    ``handle_request`` becomes a 400 envelope.
    """
    with pytest.raises(ConfigurationError) as raised:
        await request(
            'http://127.0.0.1/unused',
            data={},
            protocol='HTTP',
            protocol_info={
                'request_type': 'POST',
                'serialization': orjson.dumps,
            },
        )

    assert 'must return str' in str(raised.value)


async def test_an_unknown_protocol_escapes_request_synchronously() -> None:
    """The same rule for the entry point's own guard (spec :3815)."""
    with pytest.raises(ConfigurationError) as raised:
        await request('http://127.0.0.1/unused', protocol='CARRIER_PIGEON')

    assert 'CARRIER_PIGEON' in str(raised.value)


@pytest.mark.parametrize(
    'name, code, warning',
    [
        pytest.param('JsonRpcError', 'JSONRPC_ERROR', True),
        pytest.param('JsonRpcProtocolError', 'JSONRPC_PROTOCOL', False),
        pytest.param('GraphqlError', 'GRAPHQL_ERROR', True),
        pytest.param('GraphqlProtocolError', 'GRAPHQL_PROTOCOL', False),
        pytest.param('S3StatusError', 'S3_STATUS', True),
        pytest.param('GrpcStatusError', 'GRPC_STATUS', True),
    ],
)
def test_new_protocol_errors_have_stable_registered_contracts(
    name: str,
    code: str,
    warning: bool,
) -> None:
    """Every new remote/protocol failure is typed and wire-stable."""
    error_class = getattr(exceptions, name)
    raised = error_class('peer refused')

    assert issubclass(error_class, ProtocolError)
    assert raised.code == code
    assert raised.status_code == 502
    assert STATUS_BY_CODE[code] == 502
    assert (code in WARNING_CODES) is warning


def test_gcs_status_error_has_a_stable_remote_contract() -> None:
    """GCS service refusals are typed warning-class remote responses."""
    error_class = getattr(exceptions, 'GcsStatusError')
    raised = error_class('peer refused')

    assert error_class.__bases__ == (ProtocolError,)
    assert raised.code == 'GCS_STATUS'
    assert raised.status_code == 502
    assert STATUS_BY_CODE['GCS_STATUS'] == 502
    assert 'GCS_STATUS' in WARNING_CODES


def test_gcs_capacity_error_has_a_safe_local_contract() -> None:
    """GCS admission refusal is fixed, local, and carries no details."""
    error_class = getattr(exceptions, 'GcsCapacityError')
    first = error_class()
    second = error_class()

    assert error_class.__bases__ == (AsyncGatewayError,)
    assert first.code == 'GCS_CAPACITY'
    assert first.status_code == 503
    assert STATUS_BY_CODE['GCS_CAPACITY'] == 503
    assert 'GCS_CAPACITY' not in WARNING_CODES
    assert str(first)
    assert str(first) == str(second)
    assert first.args == (str(first),)
    assert vars(first) == {'status_code': 503}
    restored = pickle.loads(pickle.dumps(first))
    assert type(restored) is error_class
    assert str(restored) == str(first)
    assert restored.status_code == 503

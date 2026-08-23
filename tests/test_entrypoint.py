"""Tests for dispatch and input validation at the entry point (R11, Step 6).

Covers R11-AC1 (the protocol string normalised once, for both the guard and
the registry lookup), AC2 (every non-protocol rejected as configuration),
AC3 (``protocol_info`` guarded rather than read raw), AC4 (``'HTTPS'``
enforces TLS), AC6 (a typed registry with no ``None`` in it) and AC7
(validation at the boundary, once). It also covers R10-AC3: a programming
error inside a protocol handler escapes ``request()`` instead of being
reported as a failed network call.

Nothing here reaches the network. The nine registered protocols are
exercised through ``request()`` with their ``handle_request`` replaced by a
recorder, because what is under test is everything ``request()`` does
*around* the dispatch -- which protocol object it built, and with which
URL. A real transport would only add a dependency on a server this file
does not need.

The findings closed here are H4 (the guard normalised and the lookup did
not, so ``protocol='http'`` died with an uncaught ``KeyError``), H5 (the
constructor guarded ``info`` and then read the raw parameter, so
``protocol_info=None`` died with an ``AttributeError``), H6 (``'HTTPS'``
mapped to the same class as ``'HTTP'`` with nothing enforcing TLS) and the
registry half of H3 (``'SOAP': None``, which reached callers as ``TypeError:
'NoneType' object is not callable``). FI-14 -- the scheme check running
against the URL actually dispatched, after the pre-processor -- has its own
named test below.
"""

import inspect
import logging
from collections.abc import Mapping
from typing import Any, Callable, Final, Optional

from aiohttp import BasicAuth

import pytest

from asyncio_gateway.asyncio_gateway import (
    DISPATCH_CONTROLLING_KEYS,
    HTTP_FAMILY_SCHEMES,
    PROTOCOL_SCHEME_ALLOWLISTS,
    URL_DISPATCHED_PROTOCOLS,
    dispatch_url_for,
    request,
    resolve_protocol,
)
from asyncio_gateway.helpers.internal.base import (
    BaseRequestClass,
    validated_port,
    validated_protocol_info,
)
from asyncio_gateway.logic import protocol_mapping
from asyncio_gateway.logic.ftp_client import FTPRequest
from asyncio_gateway.logic.gcs_client import GcsRequest
from asyncio_gateway.logic.graphql_client import GraphqlRequest
from asyncio_gateway.logic.grpc_client import GrpcRequest
from asyncio_gateway.logic.http_client import HttpRequest
from asyncio_gateway.logic.jsonrpc_client import JsonRpcRequest
from asyncio_gateway.logic.s3_client import S3Request
from asyncio_gateway.logic.sftp_client import SFTPRequest
from asyncio_gateway.logic.soap_client import SoapRequest
from asyncio_gateway.utils.constants import HTTP_TIMEOUT
from asyncio_gateway.utils.envelope import GatewayResponse, finalise_ok
from asyncio_gateway.utils.exceptions import (
    ConfigurationError,
    ProcessorError,
)

from tests.fixtures.protocol_transports import CONTRACT_CALL, contract_call

AUTH: Final[BasicAuth] = BasicAuth('user', 'password')


class _HostileCredentialObject:
    """A credential-shaped scalar whose representation is sensitive."""

    def __repr__(self) -> str:
        """Return the secret-like representation a boundary must not read."""
        return 'gcs-ac13-credential-object'


EXPECTED_PROTOCOL_MAPPING: Final[dict[
    str, type[BaseRequestClass]
]] = {
    'HTTP': HttpRequest,
    'HTTPS': HttpRequest,
    'FTP': FTPRequest,
    'SFTP': SFTPRequest,
    'SOAP': SoapRequest,
    'JSONRPC': JsonRpcRequest,
    'GRAPHQL': GraphqlRequest,
    'S3': S3Request,
    'GCS': GcsRequest,
    'GRPC': GrpcRequest,
}


async def _valid_processor(response: GatewayResponse, **params: Any) -> str:
    """Behave exactly as a documented processor callback should.

    Shared by the NEW-2 rows whose hostile value is somewhere *other*
    than the function, so each of those tests varies one thing only.

    Args:
        response: The envelope, as ``request()`` passes it.
        params: The caller's own ``params``, unused.

    Returns:
        A sentinel.
    """
    return 'processed'

# Every way of writing a protocol name that R11-AC1 requires to work. They
# are applied to each registered name rather than to a fixed list, so a
# protocol registered later is covered the day it is registered.
SPELLINGS: Final[dict[str, Callable[[str], str]]] = {
    'as-registered': lambda name: name,
    'lowercase': str.lower,
    'mixed-case': str.title,
    'surrounding-whitespace': lambda name: f'  {name}  ',
}


def capture_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> list[BaseRequestClass]:
    """Record every protocol object dispatched, instead of calling out.

    Replaces ``handle_request`` on every registered protocol class with a
    recorder that finalises the envelope it was handed as a success. The
    protocol object itself is recorded, so a test can assert which class
    ``request()`` chose and which URL it handed it.

    Args:
        monkeypatch: The pytest patcher, which undoes this on teardown.

    Returns:
        The live list the recorder appends to, in dispatch order.
    """
    dispatched: list[BaseRequestClass] = []

    async def handle_request(self: BaseRequestClass) -> GatewayResponse:
        """Record this dispatch and close its envelope as a success.

        Args:
            self: The protocol object ``request()`` constructed.

        Returns:
            That object's own envelope, finalised.
        """
        dispatched.append(self)
        return finalise_ok(
            self.response, status_code=200, started=self.start_time)

    for protocol_class in set(protocol_mapping.values()):
        monkeypatch.setattr(protocol_class, 'handle_request', handle_request)
    return dispatched


def gateway_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    """Return only the records this library emitted.

    Args:
        caplog: The capture fixture, already set to DEBUG on the tree.

    Returns:
        Every captured record from the ``asyncio_gateway`` logger tree.
    """
    return [
        record for record in caplog.records
        if record.name.startswith('asyncio_gateway')
    ]


# --- R11-AC1: normalised once, for the guard and for the lookup (H4) -------


@pytest.mark.parametrize('name', tuple(CONTRACT_CALL))
@pytest.mark.parametrize(
    'spelling', list(SPELLINGS.values()), ids=list(SPELLINGS))
async def test_r11_ac1_protocol_spelling_dispatches_the_same_class(
    monkeypatch: pytest.MonkeyPatch,
    spelling: Callable[[str], str],
    name: str,
) -> None:
    """Case and whitespace do not change which protocol is dispatched (H4)."""
    dispatched = capture_dispatch(monkeypatch)

    call = contract_call(name)
    call['protocol'] = spelling(name)
    result = await request(**call)

    assert result['ok'] is True
    assert [type(obj) for obj in dispatched] == [protocol_mapping[name]]
    assert result['protocol'] == name


@pytest.mark.parametrize('name', tuple(CONTRACT_CALL))
@pytest.mark.parametrize(
    'spelling', list(SPELLINGS.values()), ids=list(SPELLINGS))
def test_r11_ac1_resolve_protocol_normalises_before_it_looks_up(
    spelling: Callable[[str], str],
    name: str,
) -> None:
    """The guard and the lookup are one operation returning one value."""
    assert resolve_protocol(spelling(name)) == (name, protocol_mapping[name])


def test_gcs_selector_resolves_case_insensitively() -> None:
    """GCS has one normalized public selector backed by a strategy class."""
    assert 'GCS' in protocol_mapping
    assert resolve_protocol('  gCs  ') == (
        'GCS', protocol_mapping['GCS'])


def test_gcs_selector_has_only_the_gs_scheme() -> None:
    """GCS dispatch accepts only explicit ``gs`` targets."""
    assert PROTOCOL_SCHEME_ALLOWLISTS['GCS'] == frozenset({'gs'})


@pytest.mark.parametrize(
    ('parameter', 'required_statement'),
    [
        pytest.param(
            'protocol',
            'one of the names registered in '
            '``asyncio_gateway.logic.protocol_mapping`` -- HTTP, HTTPS, FTP, '
            'SFTP, SOAP, JSONRPC, GRAPHQL, S3, GCS, or GRPC.',
            id='protocol-registers-gcs',
        ),
        pytest.param(
            'data',
            'GCS does not use this argument.',
            id='data-unused-by-gcs',
        ),
        pytest.param(
            'auth',
            'GCS requires ``None``; this selects Application Default '
            'Credentials, including Workload Identity.',
            id='auth-requires-adc-or-workload-identity',
        ),
    ],
)
def test_request_docstring_documents_gcs_in_parameter_section(
    parameter: str,
    required_statement: str,
) -> None:
    """Each GCS public-call claim belongs to its own parameter section."""
    docstring = inspect.getdoc(request) or ''
    marker = f':param {parameter}:'
    _, separator, remainder = docstring.partition(marker)
    section = remainder.partition('\n:param ')[0] if separator else ''

    assert required_statement in ' '.join(section.split())


# --- GCS-SEC-001 / AC1.3: GCS data is not an authentication side channel --


@pytest.mark.parametrize(
    ('data', 'secrets'),
    [
        pytest.param(
            {'ordinary': 'gcs-ac13-opaque-data'},
            ('gcs-ac13-opaque-data',),
            id='ordinary-non-empty-mapping',
        ),
        pytest.param(
            {
                'type': 'service_account',
                'private_key': 'gcs-ac13-service-account-key',
                'client_email': 'gcs-ac13@example.invalid',
                'token_uri': 'https://gcs-ac13.invalid/token',
            },
            (
                'gcs-ac13-service-account-key',
                'gcs-ac13@example.invalid',
                'gcs-ac13.invalid/token',
            ),
            id='service-account-mapping',
        ),
        pytest.param(
            _HostileCredentialObject(),
            ('gcs-ac13-credential-object',),
            id='credential-object-with-hostile-repr',
        ),
        pytest.param(
            'Bearer gcs-ac13-access-token',
            ('gcs-ac13-access-token',),
            id='bearer-token-text',
        ),
        pytest.param(
            b'gcs-ac13-key-material',
            ('gcs-ac13-key-material',),
            id='key-material-bytes',
        ),
        pytest.param(
            {'project': 'gcs-ac13-project-override'},
            ('gcs-ac13-project-override',),
            id='project-override',
        ),
        pytest.param(
            {'api_endpoint': 'https://gcs-ac13-endpoint.invalid'},
            ('gcs-ac13-endpoint.invalid',),
            id='custom-endpoint',
        ),
        pytest.param(
            {'hostname': 'gcs-ac13-hostname.invalid'},
            ('gcs-ac13-hostname.invalid',),
            id='custom-hostname',
        ),
    ],
)
async def test_gcs_sec001_ac13_rejects_data_before_gateway_work(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    data: object,
    secrets: tuple[str, ...],
) -> None:
    """GCS rejects every populated data shape before any owned side effect.

    The sentinels are deliberately the boundaries that an accepted GCS call
    would otherwise reach: envelope creation, a caller processor, strategy
    construction and its breaker, and the ADC, SDK, and filesystem seams.
    Their emptiness after the public call is the placement proof, not a mock
    of the outcome.  The credential object has a hostile ``repr`` so a
    diagnostic that attempts to format caller data fails this test too.
    """
    crossed: list[str] = []

    def prohibit(boundary: str) -> Callable[..., None]:
        """Return a boundary sentinel that records and rejects a crossing."""
        def forbidden(*args: Any, **kwargs: Any) -> None:
            """Fail if a rejected request reaches an owned side effect."""
            crossed.append(boundary)
            raise AssertionError(
                'rejected GCS data crossed a pre-dispatch boundary')

        return forbidden

    monkeypatch.setattr(
        'asyncio_gateway.asyncio_gateway.new_envelope', prohibit('envelope'))
    monkeypatch.setattr(
        'asyncio_gateway.asyncio_gateway.run_processor',
        prohibit('processor'))
    monkeypatch.setattr(GcsRequest, '__init__', prohibit('gcs-constructor'))
    monkeypatch.setattr(
        'asyncio_gateway.helpers.internal.base.get_breaker',
        prohibit('breaker'))
    monkeypatch.setattr(
        'asyncio_gateway.logic.gcs_client.google_auth.default',
        prohibit('adc'))
    monkeypatch.setattr(
        'asyncio_gateway.logic.gcs_client.storage.Client',
        prohibit('storage-client'))
    monkeypatch.setattr(
        'asyncio_gateway.logic.gcs_client.read_guarded_file',
        prohibit('filesystem-read'))
    monkeypatch.setattr(
        'asyncio_gateway.logic.gcs_client.stream_to_path',
        prohibit('filesystem-write'))
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    call = contract_call('GCS')
    call['data'] = data
    call['pre_processor_config'] = {'function': _valid_processor}
    with pytest.raises(ConfigurationError) as raised:
        await request(**call)

    surfaces = (str(raised.value), caplog.text)
    assert all(
        secret not in surface for secret in secrets for surface in surfaces)
    assert gateway_records(caplog) == []
    assert crossed == []


@pytest.mark.parametrize(
    'data', [None, {}],
    ids=['none-normalizes-to-empty-payload', 'empty-mapping'])
async def test_ac13_gcs_keeps_the_empty_shared_payload_controls(
    monkeypatch: pytest.MonkeyPatch,
    data: object,
) -> None:
    """The ADC-only boundary retains the compatible empty data controls."""
    dispatched = capture_dispatch(monkeypatch)
    call = contract_call('GCS')
    call['data'] = data

    result = await request(**call)

    assert result['payload'] == {}
    assert [type(item) for item in dispatched] == [GcsRequest]


# --- R11-AC2: everything that is not a protocol is a configuration error ---


@pytest.mark.parametrize(
    'protocol', [None, '', 123, 'HTTPX'],
    ids=['none', 'empty', 'not-a-string', 'unknown'])
async def test_r11_ac2_unusable_protocol_raises_configuration_error(
    protocol: object,
) -> None:
    """None, empty, non-string and unknown are all rejected the same way."""
    with pytest.raises(ConfigurationError) as raised:
        await request('http://host/p', protocol=protocol)

    message = str(raised.value)
    for supported in protocol_mapping:
        assert supported in message


def test_h3_soap_maps_to_a_real_class_and_never_to_none() -> None:
    """``'SOAP'`` dispatches to a real strategy class (H3, R11-AC5).

    The closing half of H3, and the reason this assertion is written as a
    *type* check rather than as ``'SOAP' in protocol_mapping``. The defect
    was never the key's absence: the key was present and mapped to
    ``None``, so ``request(protocol='SOAP')`` reached
    ``protocol_class(...)`` and died with ``TypeError: 'NoneType' object
    is not callable`` -- a crash out of a function whose contract is to
    return an envelope. A membership test passes on that exact defect.

    Between the two, the key was deliberately *absent* rather than mapped
    to a placeholder, so a SOAP call was rejected as an unknown protocol
    with a message naming what the library did support. S22 wrote
    ``logic/soap_client.py``, so the entry that was once a landmine is
    now a class, and every entry in the registry is one.
    """
    assert protocol_mapping['SOAP'] is SoapRequest
    assert all(
        isinstance(strategy, type)
        and issubclass(strategy, BaseRequestClass)
        for strategy in protocol_mapping.values())


def test_pe80_registry_is_the_exact_ten_protocol_contract() -> None:
    """The production selector registry contains every first-class client."""
    assert protocol_mapping == EXPECTED_PROTOCOL_MAPPING
    assert set(CONTRACT_CALL) == set(protocol_mapping)
    for protocol, row in CONTRACT_CALL.items():
        assert {'url', 'data', 'auth', 'info'} <= set(row), protocol


async def test_r11_ac2_a_rejected_protocol_is_reported_once_and_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pre-dispatch raise is the whole report; nothing logs it as well."""
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    with pytest.raises(ConfigurationError):
        await request('http://host/p', protocol='HTTPX')

    assert gateway_records(caplog) == []


# --- R11-AC3: protocol_info is guarded, then read through the guard (H5) ---


@pytest.mark.parametrize('name', ['FTP', 'SFTP'])
@pytest.mark.parametrize('info', [None, {}], ids=['none', 'empty'])
async def test_r11_ac3_absent_protocol_info_works_where_nothing_is_required(
    monkeypatch: pytest.MonkeyPatch,
    info: object,
    name: str,
) -> None:
    """The documented "no protocol_info" call reaches dispatch (H5)."""
    dispatched = capture_dispatch(monkeypatch)

    result = await request(
        'host', protocol=name, auth=AUTH, protocol_info=info)

    assert result['ok'] is True
    assert dispatched[0].info == {}


@pytest.mark.parametrize('name', ['HTTP', 'HTTPS'])
@pytest.mark.parametrize(
    'info', [None, {}, {'timeout': 5}],
    ids=['none', 'empty', 'missing-the-required-key'])
async def test_r11_ac3_a_missing_required_key_names_it(
    name: str,
    info: object,
) -> None:
    """HTTP requires a verb, and names the key when it does not get one."""
    with pytest.raises(ConfigurationError) as raised:
        await request(
            CONTRACT_CALL[name]['url'], protocol=name, protocol_info=info)

    assert 'request_type' in str(raised.value)


@pytest.mark.parametrize('name', tuple(CONTRACT_CALL))
async def test_each_selector_owns_its_public_required_key_contract(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """Every selector's declared requirements are enforced at the boundary."""
    protocol_class = EXPECTED_PROTOCOL_MAPPING[name]
    required = protocol_class.REQUIRED_INFO_KEYS
    if not required:
        dispatched = capture_dispatch(monkeypatch)
        call = contract_call(name)
        call['protocol_info'] = None
        result = await request(**call)
        assert result['ok'] is True
        assert len(dispatched) == 1
        return

    for missing in required:
        call = contract_call(name)
        del call['protocol_info'][missing]
        with pytest.raises(ConfigurationError) as raised:
            await request(**call)
        assert missing in str(raised.value)


@pytest.mark.parametrize(
    'info', ['request_type', ['request_type'], 42],
    ids=['string', 'list', 'int'])
async def test_r11_ac3_non_mapping_protocol_info_is_a_configuration_error(
    info: object,
) -> None:
    """A non-dict is rejected as configuration, never as an AttributeError."""
    with pytest.raises(ConfigurationError):
        await request('host', protocol='FTP', auth=AUTH, protocol_info=info)


# --- H5: `auth` is optional in the signature, required by FTP and SFTP ----


@pytest.mark.parametrize('name', ['FTP', 'SFTP'])
async def test_h5_the_documented_default_call_is_configuration_not_a_crash(
    caplog: pytest.LogCaptureFixture,
    name: str,
) -> None:
    """``auth=None`` is the caller's mistake, reported as one (H5).

    ``request()`` defaults ``auth`` to None and documents it optional, so
    this *is* the documented default call -- and for the two protocols
    that interpolate credentials into their connect it used to die on
    ``self.auth.login`` with an ``AttributeError``. That escaped the
    envelope and, being no ``AsyncGatewayError``, told the caller through
    this library's own contract that it had hit a bug in here. A missing
    credential is the caller's configuration.

    Raised rather than enveloped, and so not logged: the constructor runs
    outside the entry point's one conversion ``try``, which is the rule
    that decides (AGW-35), and the single-report principle gives a
    raising path no log line.
    """
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    with pytest.raises(ConfigurationError) as raised:
        await request('host', protocol=name, protocol_info=None)

    assert name in str(raised.value)
    assert 'auth' in str(raised.value)
    assert gateway_records(caplog) == []


@pytest.mark.parametrize('name', ['FTP', 'SFTP'])
@pytest.mark.parametrize(
    'auth',
    [
        pytest.param(None, id='absent'),
        pytest.param(object(), id='no-credential-attributes'),
        pytest.param('user:password', id='a-string-not-an-auth-object'),
    ],
)
async def test_h5_auth_without_string_credentials_is_refused(
    auth: object,
    name: str,
) -> None:
    """Every shape that cannot yield a login and a password is refused."""
    with pytest.raises(ConfigurationError) as raised:
        await request('host', protocol=name, auth=auth, protocol_info=None)

    assert 'login' in str(raised.value)
    assert 'password' in str(raised.value)


@pytest.mark.parametrize(
    'protocol_class', [FTPRequest, SFTPRequest], ids=['ftp', 'sftp'])
def test_h5_an_empty_password_is_a_password(protocol_class: type) -> None:
    """The boundary is pinned from the accepting side too.

    An empty password is one a server may well accept, so refusing it
    here would be this library inventing a policy the transport does not
    have. Asserted beside the rejections so a guard tightened into
    ``if not password`` fails rather than quietly narrowing what callers
    may send.
    """
    built = protocol_class(
        'host', BasicAuth('user', ''), {}, info=None,
        redact_params=frozenset())

    assert (built.user, built.password) == ('user', '')


@pytest.mark.parametrize('name', ['HTTP', 'HTTPS'])
async def test_h5_the_http_family_still_accepts_no_auth(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """The guard is FTP's and SFTP's, not a new requirement everywhere.

    HTTP forwards ``auth`` to aiohttp untouched and None means "send no
    credentials", which is the overwhelmingly common call. A guard that
    leaked into this path would break every anonymous HTTP request.
    """
    dispatched = capture_dispatch(monkeypatch)

    call = contract_call(name)
    call['auth'] = None
    result = await request(**call)

    assert result['ok'] is True
    assert dispatched[0].auth is None


def test_h5_the_constructor_reads_only_the_guarded_info() -> None:
    """Every post-guard read goes through ``self.info``, so None is fine."""
    ftp = FTPRequest(
        'host', AUTH, {}, info=None, redact_params=frozenset())

    assert ftp.info == {}
    assert ftp.timeout == HTTP_TIMEOUT
    assert ftp.certificate is None
    assert ftp.circuit_breaker_config == {}


def test_r11_ac3_validated_protocol_info_copies_rather_than_aliases() -> None:
    """The validated mapping is the library's, not the caller's to mutate."""
    supplied = {'request_type': 'GET'}

    validated = validated_protocol_info(
        supplied, required=frozenset({'request_type'}))
    validated['request_type'] = 'DELETE'

    assert supplied == {'request_type': 'GET'}


# --- NEW-2: the port that reaches the breaker registry's dict key ----------


@pytest.mark.parametrize(
    'port',
    [
        pytest.param(None, id='none-means-the-caller-named-no-port'),
        pytest.param(0, id='zero-is-a-legal-port'),
        pytest.param(21, id='the-ftp-default'),
        pytest.param(8080, id='an-ordinary-high-port'),
        pytest.param(65535, id='the-top-of-the-16-bit-range'),
    ],
)
def test_validated_port_passes_every_usable_port(port: Any) -> None:
    """A validator that rejects a legal port is its own defect.

    ``0`` is on this list rather than the reject list on purpose, and
    it is the one row worth arguing about: ``0`` is a legal port number,
    which is precisely why
    :data:`~asyncio_gateway.utils.constants.UNKNOWN_PORT` is ``-1`` and
    not ``0``. Rejecting it here would contradict that choice one module
    away.

    Args:
        port: A port the library must accept unchanged.

    Returns:
        None.
    """
    assert validated_port(port) == port


@pytest.mark.parametrize(
    'port',
    [
        # The NEW-2 shapes: unhashable, so they reached
        # `_BREAKERS.get(key)` and raised a bare `TypeError` from inside
        # the registry rather than a `ConfigurationError` at the
        # boundary. All three builtins, since it is the container-ness
        # that does it.
        pytest.param(['a', 'list'], id='an-unhashable-list'),
        pytest.param({'a': 'dict'}, id='an-unhashable-dict'),
        pytest.param({'a', 'set'}, id='an-unhashable-set'),
        # Hashable and still wrong, which is why this validator checks
        # the type and the range rather than stopping at hashability.
        # `'21'` would have opened a *second* registry key for a
        # destination that already had one -- `('ftp', 'h', '21')` and
        # `('ftp', 'h', 21)` are distinct -- so each key accumulates
        # half the failures and neither opens the circuit (H8, quietly
        # restored for one caller).
        pytest.param('21', id='a-numeric-string'),
        pytest.param('not a port', id='a-non-numeric-string'),
        pytest.param(21.0, id='a-float-however-round'),
        # `True` is an `int` of value 1, and port 1 is not what anyone
        # meant by `port=True` -- the same bargain the timeout, the
        # response cap and the redirect bound all strike.
        pytest.param(True, id='a-bool-which-is-an-int-of-value-one'),
        pytest.param(False, id='the-other-bool'),
        # `-1` is `UNKNOWN_PORT` itself: accepting it would let a caller
        # collide with the registry's own "no port known" sentinel.
        pytest.param(-1, id='the-unknown-port-sentinel'),
        pytest.param(-8080, id='any-other-negative'),
        pytest.param(65536, id='one-past-the-16-bit-ceiling'),
        pytest.param(10 ** 12, id='far-past-it'),
        pytest.param(object(), id='an-arbitrary-object'),
    ],
)
def test_validated_port_refuses_everything_a_socket_cannot_use(
    port: Any,
) -> None:
    """Every rejection is a ``ConfigurationError``, never a builtin.

    The contract this library makes is that a non-``AsyncGatewayError``
    reaching the caller means a bug in *here*. A caller's own bad
    ``protocol_info['port']`` is not that, so it is reported as
    configuration -- and reported at the boundary, before any protocol
    object exists to crash inside.

    Args:
        port: A port no socket can be opened on.

    Returns:
        None.
    """
    with pytest.raises(ConfigurationError) as raised:
        validated_port(port)

    # The message names the key and the range, so a caller can fix it
    # without reading this source.
    assert 'protocol_info["port"]' in str(raised.value)
    assert '0..65535' in str(raised.value)


@pytest.mark.parametrize('protocol', ['HTTP', 'HTTPS', 'FTP', 'SFTP', 'SOAP'])
async def test_an_unusable_port_is_refused_before_any_protocol_runs(
    protocol: str,
) -> None:
    """The registry key is built in a constructor, so the guard is earlier.

    Every protocol object's ``__init__`` calls
    ``get_breaker(*destination_of(..., info.get('port')))`` before it
    does anything else, and that tuple is a **dict key**. So an
    unhashable port did not fail in the protocol the caller chose -- it
    failed identically in all five, from inside the shared registry, as
    a bare ``TypeError`` past the entry point's one conversion point
    where nothing catches it.

    Checking at the boundary is what makes the fix protocol-agnostic
    rather than five fixes, and this row asserts that by running every
    protocol through the same hostile value. It also pins the
    *placement*: a ``ConfigurationError`` raised synchronously, per
    AGW-35, rather than an ``ok=False`` envelope a retry loop would
    re-attempt forever for a mistake no retry can fix.

    Args:
        protocol: The protocol under test.

    Returns:
        None.
    """
    with pytest.raises(ConfigurationError) as raised:
        await request(
            'http://host.invalid/p',
            protocol=protocol,
            protocol_info={
                'request_type': 'GET',
                'operation': 'Op',
                'mode': 'download',
                'server_path': '/f',
                'local_path': '/tmp/unused',
                'port': ['unhashable'],
            },
            auth=AUTH,
        )

    assert 'protocol_info["port"]' in str(raised.value)
    assert raised.value.code == 'CONFIG'


# --- R11-AC4 + FI-14: the scheme of the URL actually dispatched (H6) -------


@pytest.mark.parametrize(
    ('protocol', 'url', 'expected'),
    [
        ('HTTP', 'http://host/p', 'http://host/p'),
        ('HTTP', 'https://host/p', 'https://host/p'),
        ('HTTPS', 'https://host/p', 'https://host/p'),
        ('HTTPS', 'host/p', 'https://host/p'),
    ],
    ids=['http-plaintext', 'http-tls', 'https-tls', 'https-schemeless'])
async def test_r11_ac4_an_accepted_scheme_is_the_one_dispatched(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    url: str,
    expected: str,
) -> None:
    """``'HTTP'`` takes either scheme; ``'HTTPS'`` upgrades a bare host."""
    dispatched = capture_dispatch(monkeypatch)

    result = await request(
        url, protocol=protocol, protocol_info={'request_type': 'GET'})

    assert result['ok'] is True
    assert dispatched[0].url == expected


@pytest.mark.parametrize(
    ('protocol', 'url'),
    [
        ('HTTPS', 'http://host/p'),
        ('HTTP', 'ftp://host/p'),
        ('HTTPS', 'ftp://host/p'),
        ('HTTP', 'file:///etc/passwd'),
        ('HTTP', 'javascript:alert(1)'),
        ('HTTP', 'host:8080/p'),
    ],
    ids=[
        'https-refuses-plaintext',
        'http-refuses-ftp',
        'https-refuses-ftp',
        'http-refuses-file',
        'http-refuses-javascript',
        'http-refuses-a-host-that-parses-as-a-scheme',
    ])
async def test_r11_ac4_a_scheme_the_protocol_will_not_serve_is_refused(
    protocol: str,
    url: str,
) -> None:
    """A caller who asked for TLS never gets plaintext, silently or at all."""
    with pytest.raises(ConfigurationError) as raised:
        await request(
            url, protocol=protocol, protocol_info={'request_type': 'GET'})

    assert protocol in str(raised.value)


async def test_r11_ac4_an_unparseable_url_is_a_configuration_error() -> None:
    """A URL that will not parse is the caller's error, not a ValueError."""
    with pytest.raises(ConfigurationError):
        await request(
            'http://[::1/p', protocol='HTTP',
            protocol_info={'request_type': 'GET'})


@pytest.mark.parametrize('name', ['FTP', 'SFTP'])
async def test_r11_ac4_scheme_rules_do_not_reach_the_file_protocols(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """An FTP or SFTP url is a host name and is dispatched untouched."""
    dispatched = capture_dispatch(monkeypatch)

    result = await request('ftp://host/p', protocol=name, auth=AUTH)

    assert result['ok'] is True
    assert dispatched[0].url == 'ftp://host/p'


async def test_fi14_a_pre_processor_cannot_downgrade_an_https_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL checked and the URL dispatched are the same value.

    The pre-processor runs first and is handed the mutable envelope, so a
    scheme check made against the caller's original argument before it ran
    would be checking something the dispatch no longer used.
    """
    dispatched = capture_dispatch(monkeypatch)

    async def downgrade(response: GatewayResponse) -> str:
        """Rewrite the envelope's URL to plaintext and report doing so.

        Args:
            response: The envelope, before dispatch.

        Returns:
            A marker proving the pre-processor ran.
        """
        response['url'] = 'http://evil/p'
        return 'ran'

    with pytest.raises(ProcessorError, match='url'):
        await request(
            'host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={'function': downgrade},
        )

    assert dispatched == []


def test_fi14_the_checked_url_is_the_value_handed_to_the_protocol() -> None:
    """``dispatch_url_for`` returns what it checked, so they cannot differ."""
    assert dispatch_url_for('HTTPS', 'host/p') == 'https://host/p'
    assert dispatch_url_for('FTP', 'anything at all') == 'anything at all'


@pytest.mark.parametrize(
    'protocol', sorted(URL_DISPATCHED_PROTOCOLS))
@pytest.mark.parametrize(
    'url',
    [
        pytest.param('//host/p', id='a-bare-authority'),
        pytest.param('//host:8080/p', id='with-a-port'),
        pytest.param('//user:pw@host/p', id='with-userinfo'),
        pytest.param('//host', id='authority-only'),
    ],
)
def test_a_protocol_relative_url_is_refused_at_validation(
    protocol: str,
    url: str,
) -> None:
    """A URL with an authority and no scheme is config, not transport.

    ``//host/p`` is the one schemeless shape the "leave it alone" branch
    could not survive. A bare ``host/p`` has no authority, so
    ``aiohttp`` reads the whole thing as a path and fails in a way the
    transport layer already maps; ``//host/p`` has a real authority and
    no scheme, so ``aiohttp`` got far enough to assert
    ``port is not None`` internally and a bare ``AssertionError`` left
    ``request()`` un-enveloped -- the same one-conversion-point break as
    the earlier escapes (F3).

    Refused rather than upgraded, because upgrading would guess:
    ``'https://' + '//host/p'`` is a different URL, and RFC 3986 says
    the scheme is exactly the part this reference is waiting for. A
    ``ConfigurationError`` names what is missing, which is what the
    neighbouring ``validated_*`` family does with every other unusable
    configuration.

    Parametrised over every protocol that dispatches a *URL* rather than
    a bare host name -- which is a wider set than the two with a scheme
    allowlist. ``'SOAP'`` constrains no scheme and still reaches the
    same ``aiohttp`` call, so a check scoped to ``HTTP_FAMILY_SCHEMES``
    would have left it escaping.

    Args:
        protocol: The URL-dispatched protocol under test.
        url: One protocol-relative spelling.

    Returns:
        None.
    """
    with pytest.raises(ConfigurationError) as raised:
        dispatch_url_for(protocol, url)

    assert 'protocol-relative' in str(raised.value)
    assert raised.value.code == 'CONFIG'


def test_a_protocol_relative_url_is_named_without_its_credential() -> None:
    """The refusal message is a caller-visible surface like any other.

    ``dispatch_url_for``'s own "url is not parseable" message published
    a password once (M1/AGW-34); a new message naming a rejected URL is
    the same surface and gets the same treatment.
    """
    with pytest.raises(ConfigurationError) as raised:
        dispatch_url_for('HTTP', '//user:REFUSEDPW@host/p')

    assert 'REFUSEDPW' not in str(raised.value)


def test_a_schemeless_url_with_no_authority_is_still_not_refused() -> None:
    """The bound: only an *authority* without a scheme is a refusal.

    ``host/p`` under ``'HTTP'`` is left alone and under ``'HTTPS'`` is
    upgraded, and both predate this check. A guard that refused every
    schemeless URL would break the documented upgrade and every caller
    passing a bare host.
    """
    assert dispatch_url_for('HTTP', 'host/p') == 'host/p'
    assert dispatch_url_for('HTTPS', 'host/p') == 'https://host/p'
    # `///` and `''` parse to an *empty* netloc, which is why they were
    # already safe -- and why their presence in the invariant matrix did
    # not cover `//host/p`.
    assert dispatch_url_for('HTTP', '///') == '///'
    assert dispatch_url_for('HTTP', '') == ''


def test_every_url_dispatched_protocol_is_a_registered_one() -> None:
    """The second table cannot drift from the registry it names.

    :data:`URL_DISPATCHED_PROTOCOLS` is a hand-maintained superset of
    ``HTTP_FAMILY_SCHEMES``' keys, and a hand-maintained list of
    protocol names is exactly the thing that goes stale when a protocol
    is renamed or added. This fails at the rename rather than at the
    next bare ``AssertionError``.
    """
    assert URL_DISPATCHED_PROTOCOLS <= set(protocol_mapping)
    assert set(HTTP_FAMILY_SCHEMES) <= URL_DISPATCHED_PROTOCOLS


async def test_the_envelope_reports_the_url_that_was_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``result['url']`` is the dispatched value, not the caller's argument.

    Under ``'HTTPS'`` a schemeless URL is upgraded, so the two differ. The
    envelope is seeded before the upgrade can happen -- it has to be, the
    pre-processor is handed it first -- and reporting the pre-upgrade value
    left a caller reading ``host/p`` for a call that went to
    ``https://host/p`` and disagreed with the failure log, which is written
    from the dispatched URL.
    """
    dispatched = capture_dispatch(monkeypatch)

    result = await request(
        'host/p', protocol='HTTPS', protocol_info={'request_type': 'GET'})

    assert dispatched[0].url == 'https://host/p'
    assert result['url'] == dispatched[0].url


# --- R11-AC7: validated at the boundary, and what deliberately is not ------


async def test_r11_ac7_a_missing_required_key_raises_before_the_pre_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary rejects the call before the caller's code runs at all.

    ``protocol_info``'s required keys used to be checked only when the
    protocol object was constructed, which is *after* the pre-processor --
    so a call that could never be dispatched still ran the caller's
    side effects first.
    """
    capture_dispatch(monkeypatch)
    side_effects: list[str] = []

    async def record(response: GatewayResponse) -> str:
        """Record that the pre-processor ran.

        Args:
            response: The envelope, before dispatch.

        Returns:
            A marker the caller would see if this were reached.
        """
        side_effects.append('ran')
        return 'ran'

    with pytest.raises(ConfigurationError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={},
            pre_processor_config={'function': record},
        )

    assert 'request_type' in str(raised.value)
    assert side_effects == []


async def test_r11_ac7_the_scheme_check_still_runs_after_the_pre_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one check that is deliberately not at the boundary (FI-14).

    It has to run last: the URL it checks must be the URL that is
    dispatched, and the pre-processor gets to touch the envelope in
    between. So a scheme rejection legitimately happens *after* the
    caller's side effects, unlike every other validation -- and moving it
    forward to join them would be the regression.
    """
    capture_dispatch(monkeypatch)
    side_effects: list[str] = []

    async def record(response: GatewayResponse) -> str:
        """Record that the pre-processor ran.

        Args:
            response: The envelope, before dispatch.

        Returns:
            A marker proving the pre-processor ran.
        """
        side_effects.append('ran')
        return 'ran'

    with pytest.raises(ConfigurationError):
        await request(
            'http://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={'function': record},
        )

    assert side_effects == ['ran']


# --- R11-AC6: the registry is typed, so a None entry cannot hide in it -----


def test_r11_ac6_every_registry_entry_is_a_protocol_class() -> None:
    """No entry is None, and every one of them is constructible."""
    for name, protocol_class in protocol_mapping.items():
        assert isinstance(protocol_class, type), name
        assert issubclass(protocol_class, BaseRequestClass), name


# --- R28: the strategy contract's own body, for a subclass that defers -----


class DeferringProtocol(BaseRequestClass):
    """A protocol class that overrides ``handle_request`` and then defers.

    The shape a half-written protocol takes: the abstract method is
    declared, so the class instantiates and the registry's own
    ``issubclass`` check passes, but the body hands the work back to the
    base rather than dispatching anything. Written out as a real class
    rather than assembled with ``type()`` because that is how the mistake
    it stands in for is actually written.
    """

    async def handle_request(self) -> GatewayResponse:
        """Defer to the base class instead of dispatching.

        Returns:
            Never; the base class's body raises.

        Raises:
            NotImplementedError: Always, from the base class.
        """
        return await super().handle_request()


async def test_r28_a_subclass_that_defers_to_the_base_gets_a_named_error(
) -> None:
    """R28: the contract's body is a named refusal, not a silent ``None``.

    ``@abc.abstractmethod`` stops a subclass that declares *nothing* from
    instantiating at all -- but it does nothing about the subclass that
    declares the method and never finishes it, which is the case this
    line exists for and the only one that reaches production. Delete the
    ``raise`` and the body is a bare docstring: ``handle_request``
    returns ``None``, ``request()`` hands that ``None`` back as the
    envelope, and the caller reads ``envelope['ok']`` and gets a
    ``TypeError`` from somewhere with no protocol in the traceback. The
    ``NotImplementedError`` names the contract that was not met, at the
    class that did not meet it.

    Driven as a direct construction rather than through ``request()``:
    the public path can only reach a *registered* protocol, and a
    protocol that reached the registry unfinished is a defect this test
    would then be unable to describe.
    """
    deferring = DeferringProtocol(
        'host', AUTH, {}, info=None, redact_params=frozenset())

    with pytest.raises(NotImplementedError):
        await deferring.handle_request()


# --- R10-AC3: a library bug escapes; it is not reported as a failed call ---


@pytest.mark.parametrize('name', tuple(CONTRACT_CALL))
@pytest.mark.parametrize(
    'bug', [KeyError, TypeError, UnboundLocalError],
    ids=['key-error', 'type-error', 'unbound-local-error'])
async def test_r10_ac3_a_programming_error_escapes_request(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    bug: type[Exception],
) -> None:
    """A bug in a handler reaches the caller as itself, not as an envelope."""
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    async def handle_request(self: BaseRequestClass) -> GatewayResponse:
        """Fail the way a library bug fails.

        Args:
            self: The protocol object under test.

        Returns:
            Never; this always raises.

        Raises:
            Exception: The injected programming error.
        """
        raise bug('injected')

    monkeypatch.setattr(
        protocol_mapping[name], 'handle_request', handle_request)

    with pytest.raises(bug):
        await request(**contract_call(name))

    # Not caught means not converted *and* not logged: the caller's own
    # traceback is the report.
    assert gateway_records(caplog) == []


# --- NEW-H2: a RecursionError is a failure, not a library bug --------------


@pytest.mark.parametrize('name', tuple(CONTRACT_CALL))
async def test_a_recursion_error_becomes_an_envelope_like_any_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
) -> None:
    """The one exception to the row above, and it is not an inconsistency.

    A ``KeyError`` escapes because it is a mistake in a line of this
    library's code and reporting it as a failed network call is what hid
    an audit's worth of defects. A ``RecursionError`` is a different
    animal: the interpreter refusing to go further, driven by **what the
    remote side sent**. A 221 KB multipart body of 2000 nesting levels
    produced exactly that and it escaped ``request()`` as a bare builtin
    where the contract says every failure arrives as an ``ok=False``
    envelope (NEW-H2) -- so remote input got to choose which of the two
    kinds of report a caller received.

    Injected here rather than driven from a real body, deliberately.
    The multipart walk that provoked it is iterative and depth-capped
    now, so no input reaches this arm any more -- and the arm exists for
    the *next* unbounded descent, not that one. Injecting is the only way
    to test a backstop whose whole purpose is to catch something not yet
    written; the real hostile body is asserted end to end in
    ``tests/helpers/test_request_helper.py``.

    Every protocol, because the conversion sits in the entry point and a
    guard placed in one protocol client would be three-quarters absent.
    """
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    async def handle_request(self: BaseRequestClass) -> GatewayResponse:
        """Fail the way an unbounded descent fails.

        Args:
            self: The protocol object under test.

        Returns:
            Never; this always raises.

        Raises:
            RecursionError: Always.
        """
        raise RecursionError('maximum recursion depth exceeded')

    monkeypatch.setattr(
        protocol_mapping[name], 'handle_request', handle_request)

    result = await request(**contract_call(name))

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'STACK_EXHAUSTED'
    assert result['status_code'] == 502
    # Converted means logged, exactly as every other envelope-producing
    # failure is -- the mirror of the empty-log assertion above.
    assert len(gateway_records(caplog)) == 1


# --- AGW-35: which side of the one conversion `try` a config error is on ---


@pytest.mark.parametrize(
    ('name', 'info'),
    [
        pytest.param(
            'HTTP',
            {
                'request_type': 'get',
                'http_file_upload_config': {
                    'local_filepath': '/tmp/x', 'file_key': 'f'},
            },
            id='http-upload-config-on-a-get'),
        pytest.param(
            'HTTP', {'request_type': 'frobnicate'}, id='http-unknown-verb'),
        pytest.param(
            'HTTPS', {'request_type': 'get', 'timeout': 'ten'},
            id='https-non-numeric-timeout'),
    ],
)
async def test_agw35_a_constructor_rejection_raises_and_does_not_log(
    caplog: pytest.LogCaptureFixture,
    name: str,
    info: dict[str, Any],
) -> None:
    """Outside the ``try`` means: escapes, and is reported exactly once.

    The rule AGW-35 settled is mechanical -- an ``AsyncGatewayError``
    raised *outside* the entry point's one conversion ``try`` escapes,
    one raised inside becomes an envelope. Everything checked in a
    protocol constructor is outside it, because ``request()`` builds the
    protocol object before the ``try``.

    The empty log is half the assertion, not decoration: the
    single-report principle says a path never both raises and logs, and
    a check moved inside the ``try`` would start doing both.
    """
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    with pytest.raises(ConfigurationError):
        await request(
            CONTRACT_CALL[name]['url'], protocol=name, protocol_info=info)

    assert gateway_records(caplog) == []


@pytest.mark.parametrize(
    ('name', 'info'),
    [
        pytest.param('FTP', {'server_path': '/f'}, id='ftp-absent-command'),
        pytest.param(
            'FTP', {'command': 'frobnicate', 'server_path': '/f'},
            id='ftp-unknown-command'),
        pytest.param('FTP', {'command': 'download'}, id='ftp-absent-path'),
        pytest.param('SFTP', {'remote_path': '/f'}, id='sftp-absent-mode'),
        pytest.param(
            'SFTP', {'mode': 'frobnicate', 'remote_path': '/f'},
            id='sftp-unknown-mode'),
        pytest.param('SFTP', {'mode': 'get'}, id='sftp-absent-path'),
    ],
)
async def test_agw35_a_deferred_rejection_envelopes_at_config_400(
    caplog: pytest.LogCaptureFixture,
    name: str,
    info: dict[str, Any],
) -> None:
    """Inside the ``try`` means: a ``CONFIG``/400 envelope, logged once.

    FTP's ``command`` and SFTP's ``mode`` are the deferred set, and they
    are deferred for a reason that is not stylistic: R11-AC3 requires
    both objects to be constructible with ``protocol_info=None``, so
    neither key can be checked in ``__init__``.

    The status is the whole point of the row. Each of these used to
    report whatever the *connect* failed with first -- an unreachable
    host made an unknown FTP command ``CONNECT``/502, an unverified host
    key made an unknown SFTP mode ``HOST_KEY``/495 -- so a caller was
    handed a transport verdict, and an invitation to retry, for a typo
    that could never have run. No socket is mocked here deliberately:
    these must resolve as configuration *before* anything is opened, and
    against a host that does not answer, a transport verdict is exactly
    what a regression would produce.
    """
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    result = await request(
        'host', protocol=name, auth=AUTH, protocol_info=info)

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'CONFIG'
    assert result['status_code'] == 400
    assert len(gateway_records(caplog)) == 1


# --- NEW-2: the two processor configs, validated like every other input ---
#
# `pre_processor_config['function']` was indexed and awaited with zero
# checking, so two documented public parameters were a direct route to a
# bare builtin: 18 of 20 hostile shapes a security review drove through the
# public API escaped `request()` un-enveloped. The invariant matrix in
# `tests/test_entrypoint_invariant.py` asserts only that *nothing bare*
# escapes; these tests assert the part that matters to a caller reading the
# error -- which of the two errors they get, and why the split is where it
# is.


async def test_new2_a_non_mapping_processor_config_raises_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config that is not a mapping was ``TypeError: list indices ...``.

    The shape a caller most easily produces by passing the function
    itself where the config was wanted.
    """
    capture_dispatch(monkeypatch)

    with pytest.raises(ConfigurationError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config=['function'],
        )

    assert 'pre_processor_config' in str(raised.value)
    assert 'list' in str(raised.value)


async def test_new2_a_missing_function_key_raises_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KeyError('function')`` was the single most likely escape.

    A well-shaped mapping with the key mis-spelled is an ordinary typo,
    and it reached the caller as the rawest possible builtin.
    """
    capture_dispatch(monkeypatch)

    with pytest.raises(ConfigurationError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={'fn': _valid_processor},
        )

    assert 'function' in str(raised.value)


async def test_new2_a_non_callable_function_raises_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-callable is a config error, not a callback failure.

    The distinction this asserts is the one the mutation proof found
    unguarded: ``run_processor`` would convert this too, but as a
    ``ProcessorError``, telling a caller their *function* failed when in
    fact they never supplied one. The code is what a caller branches on,
    so the split has to be asserted and not merely intended.
    """
    capture_dispatch(monkeypatch)

    with pytest.raises(ConfigurationError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={'function': 'not callable'},
        )

    assert raised.value.code == 'CONFIG'
    assert raised.value.status_code == 400
    assert 'callable' in str(raised.value)


async def test_new2_a_non_mapping_params_raises_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TypeError: argument after ** must be a mapping, not str``."""
    capture_dispatch(monkeypatch)

    with pytest.raises(ConfigurationError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={
                'function': _valid_processor, 'params': 'nope'},
        )

    assert 'params' in str(raised.value)


async def test_new2_a_non_str_params_key_raises_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TypeError: keywords must be strings``, from the ``**`` expansion.

    Asserted as ``CONFIG`` for the same reason the non-callable row is:
    ``run_processor`` catches the ``TypeError`` regardless, so without
    this test the guard could be deleted and only the *classification*
    would change -- silently, and in the direction that misleads.
    """
    capture_dispatch(monkeypatch)

    with pytest.raises(ConfigurationError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={
                'function': _valid_processor, 'params': {1: 'v'}},
        )

    assert raised.value.code == 'CONFIG'
    assert 'str' in str(raised.value)


async def test_new2_params_may_not_shadow_the_response_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``got multiple values for keyword argument 'response'``.

    Refused rather than resolved, because either resolution is wrong: the
    caller's value would hide the envelope the hook exists to pass, and
    the envelope would silently discard a value the caller explicitly
    supplied. ``CONFIG`` again, and again asserted rather than assumed --
    dropping this guard leaves the call failing as a ``ProcessorError``,
    which reads as "your function is broken" for a config the caller can
    see is not.
    """
    capture_dispatch(monkeypatch)

    with pytest.raises(ConfigurationError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={
                'function': _valid_processor,
                'params': {'response': 'mine'}},
        )

    assert raised.value.code == 'CONFIG'
    assert 'response' in str(raised.value)


async def test_new2_the_post_config_is_validated_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed *post*-processor config refuses the call up front.

    The deliberate half of the placement. Validating it lazily, where it
    is used, would mean a caller with a typo in their post-processor
    config discovers it only after the request has gone out -- and a
    request cannot be un-sent. Nothing is dispatched here, which is the
    assertion.
    """
    dispatched = capture_dispatch(monkeypatch)

    with pytest.raises(ConfigurationError):
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            post_processor_config={'function': None},
        )

    assert dispatched == []


async def test_new2_a_raising_callback_is_a_processor_error_not_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller's own callback blowing up is a different fault entirely.

    The configuration was accepted and this library called exactly what
    it was asked to call, so reporting ``CONFIG`` would send the caller
    to look at their config table when the bug is in their function. The
    original is chained, so nothing about it is lost.
    """
    capture_dispatch(monkeypatch)

    async def explode(response: GatewayResponse) -> str:
        """Fail the way a buggy caller callback does.

        Args:
            response: The envelope, unused.

        Returns:
            Never; this always raises.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("inside the caller's own code")

    with pytest.raises(ProcessorError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={'function': explode},
        )

    assert raised.value.code == 'PROCESSOR'
    assert raised.value.status_code == 500
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert 'inside the caller' in str(raised.value.__cause__)


async def test_new2_a_callback_raising_a_typed_error_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callback that raises this library's own error keeps its code.

    Re-wrapping it as a ``ProcessorError`` would bury the code the caller
    deliberately chose, which is the opposite of helpful: a caller who
    raises ``ConfigurationError`` from their own validation hook has
    already said what they want reported.
    """
    capture_dispatch(monkeypatch)

    async def refuse(response: GatewayResponse) -> str:
        """Refuse the call with this library's own typed error.

        Args:
            response: The envelope, unused.

        Returns:
            Never; this always raises.

        Raises:
            ConfigurationError: Always.
        """
        raise ConfigurationError('the caller rejected this call themselves')

    with pytest.raises(ConfigurationError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={'function': refuse},
        )

    assert raised.value.code == 'CONFIG'
    assert 'themselves' in str(raised.value)


async def test_new2_a_pre_processor_emptying_the_envelope_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ninth escape: a *valid* callback that removes an envelope key.

    Neither half is hostile alone, which is why one-dimensional coverage
    could not reach it. The config is well-formed and the callback
    succeeds; the crash came later, when ``http_client`` read
    ``self.response['payload']`` from inside the one conversion ``try``
    where an ``except AsyncGatewayError`` cannot see a ``KeyError``.
    """
    capture_dispatch(monkeypatch)

    async def strip(response: GatewayResponse) -> str:
        """Remove the key both HTTP and SOAP read before dispatch.

        Args:
            response: The envelope, mutated in place.

        Returns:
            A marker proving the callback itself succeeded.
        """
        response.pop('payload')
        return 'stripped'

    with pytest.raises(ProcessorError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={'function': strip},
        )

    assert 'payload' in str(raised.value)


async def test_new2_a_post_processor_emptying_the_envelope_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same guard on the far side, for a different reason.

    Nothing in this library reads the envelope after the post-processor,
    so this cannot crash -- but ``result['ok']`` is the documented and
    only success predicate, and returning an object that no longer has it
    is a broken contract however self-inflicted.
    """
    capture_dispatch(monkeypatch)

    async def strip(response: GatewayResponse) -> str:
        """Remove the success predicate from the finished envelope.

        Args:
            response: The envelope, mutated in place.

        Returns:
            A marker proving the callback itself succeeded.
        """
        response.pop('ok')
        return 'stripped'

    with pytest.raises(ProcessorError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            post_processor_config={'function': strip},
        )

    assert 'ok' in str(raised.value)


async def test_new2_a_processor_may_still_change_what_the_envelope_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard permits non-routing values to be rewritten.

    Mutating request metadata is the point of the hook. AGW-43 freezes URL
    and protocol because they describe dispatch, while payload remains a
    supported pre-dispatch edit.
    """
    capture_dispatch(monkeypatch)

    async def annotate(response: GatewayResponse) -> str:
        """Overwrite envelope values without removing any key.

        Args:
            response: The envelope, mutated in place.

        Returns:
            A marker proving the callback ran.
        """
        response['payload'] = {'replaced': True}
        return 'annotated'

    result = await request(
        'https://host/p',
        protocol='HTTPS',
        protocol_info={'request_type': 'GET'},
        pre_processor_config={'function': annotate},
    )

    assert result['pre_processor_response'] == 'annotated'
    assert result['ok'] is True
    assert result['payload'] == {'replaced': True}


async def test_new2_the_documented_processor_call_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control row: the README's own shape, with ``params``, passes.

    Every rejection above is worthless if the validator also refuses the
    documented call -- a library that rejects everything leaks no bare
    exceptions either.
    """
    capture_dispatch(monkeypatch)
    seen: list[Any] = []

    async def record(response: GatewayResponse, tag: str = '') -> str:
        """Accept the envelope and a caller-supplied parameter.

        Args:
            response: The envelope, as ``request()`` passes it.
            tag: One value from the caller's own ``params``.

        Returns:
            The tag, so the envelope carries proof of the round trip.
        """
        seen.append((response['protocol'], tag))
        return f'ran:{tag}'

    result = await request(
        'https://host/p',
        protocol='HTTPS',
        protocol_info={'request_type': 'GET'},
        pre_processor_config={'function': record, 'params': {'tag': 'pre'}},
        post_processor_config={'function': record, 'params': {'tag': 'post'}},
    )

    assert result['pre_processor_response'] == 'ran:pre'
    assert result['post_processor_response'] == 'ran:post'
    assert seen == [('HTTPS', 'pre'), ('HTTPS', 'post')]


# --- NEW-3: what a processor may and may not rewrite -----------------------


@pytest.mark.parametrize(
    'value',
    [
        # The crash half. `destination_of` calls `.lower()` on this
        # value, so anything without one was a bare `AttributeError`
        # from inside a protocol object -- past the boundary and inside
        # the one conversion `try`, where `except AsyncGatewayError`
        # cannot see it.
        pytest.param(123, id='an-int-has-no-lower'),
        pytest.param(None, id='none-has-no-lower'),
        pytest.param(['SFTP'], id='a-list-has-no-lower'),
        # The *worse* half, and the reason a type check alone would not
        # have been the fix. Every one of these is a perfectly good
        # `str` that `destination_of` lowers without complaint -- and
        # then keys the shared, process-lifetime breaker registry with.
        # An FTP call whose processor writes 'HTTPS' accumulates its
        # failures under ('https', host, 443), a breaker belonging to
        # other callers' HTTPS traffic to that host. That is
        # cross-caller state corruption, and it is silent.
        pytest.param('HTTPS', id='a-valid-string-retargeting-the-breaker'),
        pytest.param('SFTP', id='another-registered-protocol'),
        pytest.param('ftp', id='the-same-protocol-in-another-case'),
        pytest.param('anything', id='an-unregistered-name'),
    ],
)
async def test_a_pre_processor_may_not_rewrite_the_dispatch_protocol(
    monkeypatch: pytest.MonkeyPatch,
    value: Any,
) -> None:
    """``protocol`` is read back to route the call, so it is frozen.

    ``checked_envelope`` refused *removal* only, on the reasoning that
    this library has no business policing what a caller writes into an
    envelope it gave them. That holds for every key the library never
    reads again -- and ``protocol`` is not one of them:
    ``BaseRequestClass.__init__`` reads it to build the breaker
    registry's ``(family, host, port)`` key.

    Both failure shapes are on this list on purpose. A non-``str``
    crashes, which is loud; a valid ``str`` does not, which is why it
    is the more dangerous row. A fix that only type-checked the
    rewritten value would have passed the first three rows and left the
    last four corrupting another caller's breaker.

    Args:
        monkeypatch: The patcher, for the doubled dispatch.
        value: What the processor writes over ``protocol``.

    Returns:
        None.
    """
    capture_dispatch(monkeypatch)

    async def retarget(response: GatewayResponse) -> str:
        """Rewrite the dispatch-controlling protocol field.

        Args:
            response: The envelope, mutated in place.

        Returns:
            A marker that must never be reached.
        """
        response['protocol'] = value
        return 'retargeted'

    with pytest.raises(ProcessorError) as raised:
        await request(
            'http://host/p',
            protocol='HTTP',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={'function': retarget},
        )

    # Named, so the caller is told which field they may not touch
    # rather than being left to guess from a crash site.
    assert "rewrote ['protocol']" in str(raised.value)
    assert 'pre_processor_config' in str(raised.value)


async def test_a_pre_processor_may_not_rewrite_the_dispatch_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL edit fails instead of pretending it retargeted the request.

    Before AGW-43, the callback's edit was accepted and then overwritten
    from the original function argument. That silent discard made the live
    envelope look like a request-construction API while dispatching to a
    different destination. The failure must precede protocol dispatch and
    must not echo either credential-bearing URL.

    Args:
        monkeypatch: The patcher, for the doubled dispatch.

    Returns:
        None.
    """
    dispatched = capture_dispatch(monkeypatch)

    async def rewrite(response: GatewayResponse) -> str:
        """Try to replace the request's network destination.

        Args:
            response: The envelope, mutated in place.

        Returns:
            A marker proving the callback ran to completion.
        """
        response['url'] = 'https://mallory:replacement-secret@other/p'
        return 'retargeted'

    with pytest.raises(ProcessorError) as raised:
        await request(
            'https://alice:original-secret@host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={'function': rewrite},
        )

    message = str(raised.value)
    assert "rewrote ['url']" in message
    assert 'original-secret' not in message
    assert 'replacement-secret' not in message
    assert dispatched == []


async def test_a_pre_processor_may_rewrite_non_routing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Payload, annotation fields, and caller metadata remain writable."""
    dispatched = capture_dispatch(monkeypatch)

    async def annotate(response: GatewayResponse) -> str:
        """Enrich request data without changing where it is sent."""
        response['payload'] = {'replaced': True}
        response['headers'] = {'x-annotation': 'mine'}
        response['protocol_details'] = {'caller': 'state'}
        response['caller_metadata'] = {'trace': 'caller-owned'}
        return 'annotated'

    result = await request(
        'https://host/p',
        protocol='HTTPS',
        protocol_info={'request_type': 'GET'},
        pre_processor_config={'function': annotate},
    )

    assert result['pre_processor_response'] == 'annotated'
    assert result['payload'] == {'replaced': True}
    assert result['caller_metadata'] == {'trace': 'caller-owned'}
    assert dispatched[0].url == 'https://host/p'


async def test_a_post_processor_may_rewrite_anything_it_likes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After dispatch there is nothing left to corrupt.

    The rule NEW-3 settles is "immutable while it still decides
    something", not "immutable forever". By the time a post-processor
    runs, the protocol object has been built, the breaker keyed and the
    call made -- so ``protocol`` is reportorial, and freezing it would
    only stop a caller annotating the result they are about to receive.

    Asserting the asymmetry rather than leaving it implied, because it
    is the kind of scope decision a later change silently widens.

    Args:
        monkeypatch: The patcher, for the doubled dispatch.

    Returns:
        None.
    """
    capture_dispatch(monkeypatch)

    async def relabel(response: GatewayResponse) -> str:
        """Rewrite the protocol after every decision has been made.

        Args:
            response: The envelope, mutated in place.

        Returns:
            A marker proving the callback ran.
        """
        response['protocol'] = 'RELABELLED'
        response['url'] = 'reported://after-dispatch'
        return 'relabelled'

    result = await request(
        'https://host/p',
        protocol='HTTPS',
        protocol_info={'request_type': 'GET'},
        post_processor_config={'function': relabel},
    )

    assert result['post_processor_response'] == 'relabelled'
    assert result['protocol'] == 'RELABELLED'
    assert result['url'] == 'reported://after-dispatch'


def test_every_dispatch_controlling_key_is_a_real_envelope_key() -> None:
    """The frozen set cannot name a key the envelope does not have.

    ``checked_envelope`` indexes ``response[key]`` for every member, so
    a typo in :data:`DISPATCH_CONTROLLING_KEYS` would be a ``KeyError``
    on the *success* path of every call with a pre-processor -- a
    library bug reaching the caller as a bare builtin, which is the one
    thing this module's contract is built to prevent.
    """
    assert DISPATCH_CONTROLLING_KEYS <= set(
        GatewayResponse.__annotations__)
    # And it is non-empty, or the guard is silently inert.
    assert DISPATCH_CONTROLLING_KEYS
    assert DISPATCH_CONTROLLING_KEYS == frozenset({'protocol', 'url'})


@pytest.mark.parametrize(
    'unknown',
    [
        pytest.param('protcol_info', id='protocol-info-typo'),
        pytest.param('preprocesor_config', id='pre-processor-typo'),
        pytest.param('timeuot', id='timeout-in-the-wrong-location'),
    ],
)
async def test_unknown_top_level_keywords_fail_before_processors_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    unknown: str,
) -> None:
    """A misspelled top-level option cannot become an ignored no-op."""
    dispatched = capture_dispatch(monkeypatch)
    processor_ran = False

    async def processor(response: GatewayResponse) -> None:
        """Record whether boundary validation happened too late."""
        nonlocal processor_ran
        processor_ran = True

    with pytest.raises(ConfigurationError) as raised:
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET'},
            pre_processor_config={'function': processor},
            **{unknown: object()},
        )

    message = str(raised.value)
    assert unknown in message
    assert 'accepted top-level arguments' in message
    assert 'protocol_info' in message
    assert 'processor' in message
    assert processor_ran is False
    assert dispatched == []


async def test_a_security_sensitive_unknown_legacy_option_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misspelled TLS control is rejected, never warned and ignored."""
    dispatched = capture_dispatch(monkeypatch)

    with pytest.raises(ConfigurationError, match='verify_sll'):
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET', 'verify_sll': False},
        )

    assert dispatched == []


async def test_a_non_string_legacy_option_name_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protocol option names are keywords and therefore strings."""
    dispatched = capture_dispatch(monkeypatch)

    with pytest.raises(ConfigurationError, match='keys must be strings'):
        await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET', 7: 'not-a-keyword'},
        )

    assert dispatched == []


async def test_an_ordinary_unknown_legacy_option_warns_during_1_x(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility window is visible and keeps the call working."""
    dispatched = capture_dispatch(monkeypatch)

    with pytest.warns(DeprecationWarning, match='future_transport_hint'):
        result = await request(
            'https://host/p',
            protocol='HTTPS',
            protocol_info={
                'request_type': 'GET',
                'future_transport_hint': 'legacy-value',
            },
        )

    assert result['ok'] is True
    assert len(dispatched) == 1


def test_legacy_protocols_declare_complete_accepted_option_sets() -> None:
    """The warning boundary has an exhaustive legacy option census."""
    common = {
        'certificate', 'circuit_breaker_config', 'port',
        'redact_query_params', 'timeout',
    }
    expected = {
        HttpRequest: common | {
            'allow_redirects', 'allowed_schemes', 'cookies',
            'cross_origin_headers', 'headers',
            'http_file_download_config', 'http_file_upload_config',
            'max_redirects', 'max_response_bytes', 'request_type',
            'serialization', 'session', 'trace_config', 'verify_ssl',
        },
        FTPRequest: common | {
            'client_path', 'command', 'max_response_bytes', 'overwrite',
            'server_path', 'tls_mode', 'verify_ssl',
        },
        SFTPRequest: common | {
            'additional_arguments', 'client_keys', 'host_key',
            'insecure_skip_host_key_check', 'known_hosts', 'local_path',
            'max_response_bytes', 'mode', 'overwrite', 'remote_path',
        },
        SoapRequest: common | {
            'allow_redirects', 'allowed_schemes', 'cookies',
            'cross_origin_headers', 'headers',
            'http_file_download_config', 'http_file_upload_config',
            'max_redirects', 'max_response_bytes', 'request_type',
            'serialization', 'session', 'soap_action', 'soap_headers',
            'soap_version', 'trace_config', 'verify_ssl',
        },
    }

    for protocol_class, accepted in expected.items():
        assert protocol_class.ACCEPTED_INFO_KEYS == frozenset(accepted)


def test_new_protocols_publish_their_closed_option_census_once() -> None:
    """Strict selectors expose the same inventory as allowed and accepted."""
    for protocol_class in (
        JsonRpcRequest, GraphqlRequest, S3Request, GrpcRequest,
    ):
        assert protocol_class.ALLOWED_INFO_KEYS is not None
        assert (
            protocol_class.ACCEPTED_INFO_KEYS
            == protocol_class.ALLOWED_INFO_KEYS
        )


class _SemanticBoundaryRequest(BaseRequestClass):
    """Minimal strategy used to exercise the new public-boundary contract."""

    ALLOWED_INFO_KEYS = frozenset({'known'})

    async def handle_request(self) -> GatewayResponse:
        """Return the envelope without opening a transport."""
        return finalise_ok(
            self.response, status_code=200, started=self.start_time)


class _InfoHookRequest(BaseRequestClass):
    """Strategy double proving class-specific boundary validation runs once."""

    ALLOWED_INFO_KEYS = frozenset({'known'})
    validation_calls = 0

    @classmethod
    def validate_protocol_info(
        cls,
        info: Optional[Mapping[str, Any]],
        *,
        protocol: Optional[str] = None,
    ) -> dict[str, Any]:
        """Count, delegate, and normalize one protocol-specific value."""
        cls.validation_calls += 1
        validated = super().validate_protocol_info(
            info, protocol=protocol)
        validated['known'] = 'normalized'
        return validated

    async def handle_request(self) -> GatewayResponse:
        """Expose the exact validated mapping the constructor received."""
        self.response['protocol_details'] = {'info': self.info}
        return finalise_ok(
            self.response, status_code=200, started=self.start_time)


async def test_protocol_specific_info_hook_runs_once_at_public_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strategy owns one extension hook whose returned copy is dispatched."""
    _InfoHookRequest.validation_calls = 0
    monkeypatch.setitem(protocol_mapping, 'JSONRPC', _InfoHookRequest)

    result = await request(
        'https://host/rpc',
        protocol='JSONRPC',
        protocol_info={'known': 'caller spelling'},
    )

    assert _InfoHookRequest.validation_calls == 1
    assert result['protocol_details'] == {
        'info': {'known': 'normalized'},
    }


def test_protocol_info_can_opt_into_an_exact_key_allowlist() -> None:
    """New selectors reject a typo instead of silently ignoring it."""
    with pytest.raises(ConfigurationError, match='unknown key'):
        validated_protocol_info(
            {'known': 1, 'typo': 2},
            allowed=frozenset({'known'}),
        )


def test_protocol_info_without_an_allowlist_remains_permissive() -> None:
    """Legacy selectors retain their existing unknown-key behavior."""
    assert validated_protocol_info({'legacy_extension': 1}) == {
        'legacy_extension': 1}


def test_protocol_info_allowlist_rejects_non_string_keys_as_configuration(
) -> None:
    """A non-string key is rejected without being rendered or sorted."""
    with pytest.raises(
        ConfigurationError,
        match='protocol_info keys must be strings',
    ):
        validated_protocol_info(
            {'typo': 1, 2: 'also unknown'},
            allowed=frozenset({'known'}),
        )


def test_protocol_info_allowlist_never_renders_a_hostile_key() -> None:
    """Caller-controlled repr cannot escape or disclose a key's contents."""
    class HostileKey:
        """Hashable mapping key whose representation must never be invoked."""

        def __repr__(self) -> str:
            """Fail if validation tries to render the key."""
            raise RuntimeError('secret repr was invoked')

    with pytest.raises(
        ConfigurationError,
        match='protocol_info keys must be strings',
    ):
        validated_protocol_info(
            {HostileKey(): 'secret'},
            allowed=frozenset({'known'}),
        )


def test_protocol_info_checks_key_types_before_required_key_comparison(
) -> None:
    """A hash collision cannot invoke hostile equality before refusal."""
    class CollidingKey:
        """Non-string key colliding with a selector's required key."""

        def __hash__(self) -> int:
            """Collide deliberately with the required string."""
            return hash('method')

        def __eq__(self, other: object) -> bool:
            """Fail if validation compares this caller-controlled key."""
            raise RuntimeError('hostile equality was invoked')

    with pytest.raises(
        ConfigurationError,
        match='protocol_info keys must be strings',
    ):
        validated_protocol_info(
            {CollidingKey(): 'secret'},
            required=frozenset({'method'}),
            allowed=frozenset({'method'}),
        )


async def test_new_selector_allowlist_is_enforced_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The class-declared inventory is applied at the public boundary."""
    monkeypatch.setitem(
        protocol_mapping, 'JSONRPC', _SemanticBoundaryRequest)

    with pytest.raises(ConfigurationError, match='unknown key'):
        await request(
            'https://host/rpc',
            protocol='JSONRPC',
            protocol_info={'typo': True},
        )


@pytest.mark.parametrize(
    ('protocol', 'expected'),
    [
        pytest.param(
            protocol,
            None if protocol in {'JSONRPC', 'GRAPHQL'} else {},
            id=protocol,
        )
        for protocol in CONTRACT_CALL
    ],
)
async def test_none_payload_compatibility_is_explicit_for_every_selector(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    expected: object,
) -> None:
    """Only semantic JSON selectors preserve None; all others get ``{}``."""
    monkeypatch.setitem(
        protocol_mapping, protocol, _SemanticBoundaryRequest)
    call = contract_call(protocol)
    call['data'] = None
    call['protocol_info'] = {}

    result = await request(**call)

    assert result['payload'] == expected


@pytest.mark.parametrize(
    'protocol, accepted, rejected',
    [
        pytest.param(
            'JSONRPC', 'https://host/rpc', 'ftp://host/rpc', id='jsonrpc'),
        pytest.param(
            'GRAPHQL', 'http://host/graphql', 'file:///tmp/q', id='graphql'),
        pytest.param('S3', 's3://bucket/key', 'https://bucket/key', id='s3'),
        pytest.param(
            'GRPC', 'grpcs://host:443', 'https://host:443', id='grpc'),
    ],
)
def test_new_url_selectors_have_closed_scheme_allowlists(
    protocol: str,
    accepted: str,
    rejected: str,
) -> None:
    """Every URL-backed selector fails closed on a foreign scheme."""
    assert dispatch_url_for(protocol, accepted) == accepted
    with pytest.raises(ConfigurationError, match='dispatches only'):
        dispatch_url_for(protocol, rejected)


@pytest.mark.parametrize('protocol', ['JSONRPC', 'GRAPHQL', 'S3', 'GRPC'])
def test_new_url_selectors_reject_schemeless_targets(protocol: str) -> None:
    """New URL contracts never guess a transport scheme for the caller."""
    with pytest.raises(ConfigurationError, match='requires an explicit'):
        dispatch_url_for(protocol, 'host/path')

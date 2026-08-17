"""Tests for dispatch and input validation at the entry point (R11, Step 6).

Covers R11-AC1 (the protocol string normalised once, for both the guard and
the registry lookup), AC2 (every non-protocol rejected as configuration),
AC3 (``protocol_info`` guarded rather than read raw), AC4 (``'HTTPS'``
enforces TLS), AC6 (a typed registry with no ``None`` in it) and AC7
(validation at the boundary, once). It also covers R10-AC3: a programming
error inside a protocol handler escapes ``request()`` instead of being
reported as a failed network call.

Nothing here reaches the network. The four registered protocols are
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

import logging
from typing import Any, Callable, Final

from aiohttp import BasicAuth

import pytest

from async_gateway.async_gateway import (
    dispatch_url_for,
    request,
    resolve_protocol,
)
from async_gateway.helpers.internal.base import (
    BaseRequestClass,
    validated_protocol_info,
)
from async_gateway.logic import protocol_mapping
from async_gateway.logic.ftp_client import FTPRequest
from async_gateway.logic.http_client import HttpRequest
from async_gateway.logic.sftp_client import SFTPRequest
from async_gateway.logic.soap_client import SoapRequest
from async_gateway.utils.constants import HTTP_TIMEOUT
from async_gateway.utils.envelope import GatewayResponse, finalise_ok
from async_gateway.utils.exceptions import ConfigurationError

AUTH: Final[BasicAuth] = BasicAuth('user', 'password')

# One call per registered protocol that is valid for that protocol and for
# no other reason: FTP and SFTP address a bare host and require no
# `protocol_info` at all, the HTTP family addresses a schemed URL and
# requires a verb.
VALID_CALL: Final[dict[str, dict[str, Any]]] = {
    'HTTP': {'url': 'http://host/p', 'protocol_info': {'request_type': 'GET'}},
    'HTTPS': {
        'url': 'https://host/p',
        'protocol_info': {'request_type': 'GET'},
    },
    'FTP': {'url': 'host', 'protocol_info': None},
    'SFTP': {'url': 'host', 'protocol_info': None},
}

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
        Every captured record from the ``async_gateway`` logger tree.
    """
    return [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
    ]


# --- R11-AC1: normalised once, for the guard and for the lookup (H4) -------


@pytest.mark.parametrize('name', sorted(VALID_CALL))
@pytest.mark.parametrize(
    'spelling', list(SPELLINGS.values()), ids=list(SPELLINGS))
async def test_r11_ac1_protocol_spelling_dispatches_the_same_class(
    monkeypatch: pytest.MonkeyPatch,
    spelling: Callable[[str], str],
    name: str,
) -> None:
    """Case and whitespace do not change which protocol is dispatched (H4)."""
    dispatched = capture_dispatch(monkeypatch)

    result = await request(
        protocol=spelling(name), auth=AUTH, **VALID_CALL[name])

    assert result['ok'] is True
    assert [type(obj) for obj in dispatched] == [protocol_mapping[name]]
    assert result['protocol'] == name


@pytest.mark.parametrize('name', sorted(VALID_CALL))
@pytest.mark.parametrize(
    'spelling', list(SPELLINGS.values()), ids=list(SPELLINGS))
def test_r11_ac1_resolve_protocol_normalises_before_it_looks_up(
    spelling: Callable[[str], str],
    name: str,
) -> None:
    """The guard and the lookup are one operation returning one value."""
    assert resolve_protocol(spelling(name)) == (name, protocol_mapping[name])


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


async def test_r11_ac2_a_rejected_protocol_is_reported_once_and_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pre-dispatch raise is the whole report; nothing logs it as well."""
    caplog.set_level(logging.DEBUG, logger='async_gateway')

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
            VALID_CALL[name]['url'], protocol=name, protocol_info=info)

    assert 'request_type' in str(raised.value)


@pytest.mark.parametrize(
    'info', ['request_type', ['request_type'], 42],
    ids=['string', 'list', 'int'])
async def test_r11_ac3_non_mapping_protocol_info_is_a_configuration_error(
    info: object,
) -> None:
    """A non-dict is rejected as configuration, never as an AttributeError."""
    with pytest.raises(ConfigurationError):
        await request('host', protocol='FTP', auth=AUTH, protocol_info=info)


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

    result = await request(
        'host/p',
        protocol='HTTPS',
        protocol_info={'request_type': 'GET'},
        pre_processor_config={'function': downgrade},
    )

    assert result['pre_processor_response'] == 'ran'
    assert dispatched[0].url == 'https://host/p'


def test_fi14_the_checked_url_is_the_value_handed_to_the_protocol() -> None:
    """``dispatch_url_for`` returns what it checked, so they cannot differ."""
    assert dispatch_url_for('HTTPS', 'host/p') == 'https://host/p'
    assert dispatch_url_for('FTP', 'anything at all') == 'anything at all'


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


# --- R10-AC3: a library bug escapes; it is not reported as a failed call ---


@pytest.mark.parametrize(
    'protocol_class', [HttpRequest, FTPRequest, SFTPRequest],
    ids=['http', 'ftp', 'sftp'])
@pytest.mark.parametrize(
    'bug', [KeyError, TypeError, UnboundLocalError],
    ids=['key-error', 'type-error', 'unbound-local-error'])
async def test_r10_ac3_a_programming_error_escapes_request(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    protocol_class: type[BaseRequestClass],
    bug: type[Exception],
) -> None:
    """A bug in a handler reaches the caller as itself, not as an envelope."""
    caplog.set_level(logging.DEBUG, logger='async_gateway')

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

    monkeypatch.setattr(protocol_class, 'handle_request', handle_request)
    name = next(
        key for key, value in protocol_mapping.items()
        if value is protocol_class
    )

    with pytest.raises(bug):
        await request(protocol=name, auth=AUTH, **VALID_CALL[name])

    # Not caught means not converted *and* not logged: the caller's own
    # traceback is the report.
    assert gateway_records(caplog) == []

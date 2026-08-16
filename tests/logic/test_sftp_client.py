"""SFTP transport security: host-key verification and key auth (R16).

Every test here guards one half of C4 -- ``logic/sftp.py:32`` hardcoded
``known_hosts=None``, so asyncssh accepted **any** server key on **every**
session, and no ``client_keys`` was passed, so the host process's ambient
``~/.ssh/id_*`` identities were offered to whatever answered. The failure
mode both halves share is that they are *invisible*: a call against an
impersonating server succeeds, transfers, and returns ``ok=True``. Nothing
in the envelope, the logs or the caller's own tests distinguishes it from
a call to the real endpoint, which is why each guard below is asserted
directly rather than inferred from a passing transfer.

Two kinds of assertion appear, deliberately. The **options** assertions
read ``SSHTransportDouble.connections`` -- the keyword arguments the
client actually handed asyncssh -- because those, and only those, are
what the real library's security depends on. The **outcome** assertions
drive ``request()`` end to end and read ``error['code']``, because a
guard that fails safely but reports nothing a caller can branch on is
half a guard.

What is *not* asserted here: the success envelope. R17 owns the SFTP
client's envelope mapping, which today reaches neither finaliser, so a
test in this module that asserted ``ok is True`` would be asserting R17's
work and would have to be rewritten by it. The ``opened`` counter on the
double is read instead, which stays true across that rewrite.
"""

import logging
import re
from pathlib import Path
from typing import Any, Iterator

from aiohttp import BasicAuth

from async_gateway.async_gateway import request
from async_gateway.utils.envelope import GatewayResponse
from async_gateway.utils.exceptions import ConfigurationError

import pytest

from tests.fixtures.sftp import (
    ACCEPTED_KEY_ALGORITHMS,
    OTHER_HOST_KEY,
    SERVER_HOST_KEY,
    SSHTransportDouble,
    plant_ambient_key,
)

HOST = 'sftp.example.invalid'
REMOTE_PATH = '/remote/f'
AUTH = BasicAuth('user', 'password')

SFTP_LOGGER = 'async_gateway.logic.sftp_client'

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / 'async_gateway'


def package_sources() -> Iterator[tuple[str, str]]:
    """Yield every module in the package with its source text.

    Returns:
        Pairs of package-relative POSIX path and file contents, so a test
        can make the same assertion the acceptance criterion's ``grep``
        makes.
    """
    for path in sorted(PACKAGE_ROOT.rglob('*.py')):
        yield (
            path.relative_to(PACKAGE_ROOT).as_posix(),
            path.read_text(encoding='utf-8'),
        )


def files_containing(pattern: str) -> list[str]:
    """Return the package modules whose source matches ``pattern``.

    Args:
        pattern: A regular expression applied to each module's source.

    Returns:
        The package-relative paths that match, sorted.
    """
    expression = re.compile(pattern)
    return [
        name for name, source in package_sources()
        if expression.search(source)
    ]


async def sftp_call(**info: Any) -> GatewayResponse:
    """Drive one SFTP call through the public entry point.

    Args:
        info: Extra ``protocol_info`` keys, merged over the minimum a
            download needs. Passing them through ``request()`` rather
            than constructing ``SFTPRequest`` directly is what makes each
            assertion cover the seam a consumer actually uses.

    Returns:
        The envelope ``request()`` returned.
    """
    return await request(
        HOST,
        data={},
        auth=AUTH,
        protocol='SFTP',
        protocol_info={
            'mode': 'get',
            'remote_path': REMOTE_PATH,
            **info,
        },
    )


def sftp_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the warnings this module's logger emitted.

    Args:
        caplog: The pytest capture fixture, already set to a level that
            admits warnings.

    Returns:
        Every captured ``WARNING`` record from
        ``async_gateway.logic.sftp_client``, so the entry point's own
        failure log cannot be mistaken for the bypass warning.
    """
    return [
        record for record in caplog.records
        if record.name == SFTP_LOGGER and record.levelno == logging.WARNING
    ]


def known_hosts_file(tmp_path: Path, *keys: str) -> Path:
    """Write a ``known_hosts`` file containing ``keys``.

    Args:
        tmp_path: The test's temporary directory.
        keys: The host keys the file should trust.

    Returns:
        The path written.
    """
    path = tmp_path / 'known_hosts'
    path.write_text('\n'.join(keys) + '\n', encoding='utf-8')
    return path


# --- R16-AC1 / AC2: the hardcoded bypass is gone, the default is absent ----


def test_r16_ac1_the_hardcoded_host_key_bypass_is_gone_from_the_package(
) -> None:
    """The literal that disabled verification appears nowhere (R16-AC1).

    The acceptance criterion is a ``grep``, and it is a ``grep`` for a
    reason: the behaviour tests below all drive the *current* call, so
    every one of them would still pass if someone reintroduced
    ``known_hosts=None`` on a second, unexercised code path. This is the
    assertion that notices that. Remove it and the one-line regression
    that started this whole finding can come back unobserved.
    """
    assert files_containing(r'known_hosts=None') == []


async def test_r16_ac2_the_default_call_omits_known_hosts_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller who configures nothing gets asyncssh's own default.

    Absence is the requirement, not a particular value: asyncssh resolves
    ``~/.ssh/known_hosts`` when the argument is not passed, and any value
    this library passed instead -- even the empty tuple that is asyncssh's
    default today -- would be this library owning a default it should
    not, and would be one literal away from being ``None`` again.

    Remove this and "verification is on by default" rests on nothing: the
    grep above is satisfied by ``known_hosts = None`` with spaces, by a
    variable, or by a value that merely happens to be falsy.
    """
    double = SSHTransportDouble()
    double.install(monkeypatch)

    await sftp_call()

    assert 'known_hosts' not in double.options


async def test_r16_ac2_the_default_call_fails_closed_on_an_unknown_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``~/.ssh/known_hosts`` on the host still refuses (R16 edge case).

    A container has no such file, which is the deployment this library is
    most often imported into. asyncssh trusts nothing when it cannot read
    one, so the session must fail -- and must fail saying which option
    fixes it, because a bare ``Host key is not trusted`` sends the reader
    to asyncssh's documentation to discover that this library even has a
    knob for it.

    Without this test, "fails closed" is an assumption about a library
    this package does not own, on the exact path that has no file to make
    the failure visible in development.
    """
    double = SSHTransportDouble(system_known_hosts=None)
    double.install(monkeypatch)

    envelope = await sftp_call()

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'HOST_KEY'
    assert envelope['status_code'] == 495
    assert 'known_hosts' in envelope['error']['message']
    assert double.opened == 0


async def test_r16_ac2_a_host_in_the_system_known_hosts_file_connects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verification on by default still lets a trusted host through.

    The other half of the criterion, and the half a "fail closed" fix
    breaks: a default that refused *every* server would satisfy every
    assertion about refusal above while making the protocol unusable.
    """
    double = SSHTransportDouble(
        system_known_hosts=known_hosts_file(tmp_path, SERVER_HOST_KEY))
    double.install(monkeypatch)

    await sftp_call()

    assert double.opened == 1


# --- R16-AC3: explicit pinning ---------------------------------------------


async def test_r16_ac3_a_pinned_host_key_is_passed_through_to_asyncssh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``protocol_info['host_key']`` reaches asyncssh as a trusted key.

    asyncssh has no client-side ``host_key`` option at all -- pinning is
    expressed as the ``(host keys, CA keys, revoked keys)`` triple on
    ``known_hosts`` -- so the caller-facing key has to be *translated*,
    and a translation is exactly the kind of code that can silently
    produce something asyncssh ignores. A caller who pinned a key and was
    quietly given the default resolution instead would have no way to
    tell from the outside.
    """
    double = SSHTransportDouble()
    double.install(monkeypatch)

    await sftp_call(host_key=SERVER_HOST_KEY)

    assert double.options['known_hosts'] == ([SERVER_HOST_KEY], [], [])
    assert double.opened == 1


async def test_r16_ac3_a_mismatched_host_key_fails_with_host_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin is enforced, and the failure is machine-readable (R16-AC3).

    This is the whole point of the requirement: an on-path attacker
    presenting a different key must not be able to complete the session.
    ``HOST_KEY`` rather than a generic transport code because a consumer
    has to be able to branch on "the server is not who it claims" without
    parsing message text -- that is what the wire-stable code is for.

    If this guard is removed, a mismatch reaches the caller as a passing
    transfer, which is precisely the invisible failure C4 describes.
    """
    double = SSHTransportDouble()
    double.install(monkeypatch)

    envelope = await sftp_call(host_key=OTHER_HOST_KEY)

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'HOST_KEY'
    assert envelope['status_code'] == 495
    assert double.opened == 0


async def test_r16_ac3_a_known_hosts_path_is_forwarded_and_honoured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``protocol_info['known_hosts']`` is passed through untranslated.

    The documented alternative to pinning one key: hand asyncssh whatever
    it accepts. Forwarding it *unchanged* is the contract -- a value this
    library reinterpreted would silently narrow what asyncssh supports to
    whatever this library happened to think of.
    """
    trusted = known_hosts_file(tmp_path, SERVER_HOST_KEY)
    double = SSHTransportDouble()
    double.install(monkeypatch)

    await sftp_call(known_hosts=str(trusted))

    assert double.options['known_hosts'] == str(trusted)
    assert double.opened == 1


async def test_r16_a_rejected_host_key_algorithm_names_the_algorithm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unusable host key algorithm is a HOST_KEY failure (R16 edge case).

    A server offering only an algorithm the client will not accept never
    reaches key validation -- the handshake ends during negotiation, and
    asyncssh reports it through a different exception class. Classified
    as anything else it reads to the caller as a network problem and gets
    retried forever; classified here, the message carries the algorithm
    name, which is the one piece of information that makes it fixable.
    """
    double = SSHTransportDouble(key_algorithm='ssh-dss')
    double.install(monkeypatch)

    envelope = await sftp_call(host_key=SERVER_HOST_KEY)

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'HOST_KEY'
    assert 'ssh-dss' in envelope['error']['message']
    assert sorted(ACCEPTED_KEY_ALGORITHMS)[0] in envelope['error']['message']


# --- R16-AC4: the bypass has to be asked for by name -----------------------


async def test_r16_ac4_the_bypass_disables_verification_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The named bypass works, and says what it costs (R16-AC4a).

    An escape hatch that no one can find gets replaced by a worse one, so
    the bypass exists -- but a bypass that is silent becomes permanent,
    because nothing in a running system ever mentions it again. The
    warning is the only artefact that reaches an operator, so it is
    asserted to actually name the risk rather than merely to exist.
    """
    caplog.set_level(logging.DEBUG, logger='async_gateway')
    double = SSHTransportDouble()
    double.install(monkeypatch)

    await sftp_call(insecure_skip_host_key_check=True)

    assert double.options['known_hosts'] is None
    assert double.opened == 1
    warning, = sftp_warnings(caplog)
    assert 'host key verification is disabled' in warning.getMessage()


async def test_r16_ac4_the_bypass_warns_on_every_single_use(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two bypassed sessions produce two warnings (R16-AC4a).

    "Warn once" is the natural implementation and the wrong one: a
    long-lived process logs the warning during startup traffic, the line
    scrolls away, and every subsequent unverified session is invisible.
    The count is what pins that, and a one-shot guard would pass a test
    that only asserted the warning exists.
    """
    caplog.set_level(logging.DEBUG, logger='async_gateway')
    double = SSHTransportDouble()
    double.install(monkeypatch)

    await sftp_call(insecure_skip_host_key_check=True)
    await sftp_call(insecure_skip_host_key_check=True)

    assert len(sftp_warnings(caplog)) == 2


@pytest.mark.parametrize(
    'info',
    [
        pytest.param({'verify_ssl': False}, id='verify_ssl-false'),
        pytest.param({'verify_ssl': 0}, id='verify_ssl-zero'),
        pytest.param({'verify_ssl': None}, id='verify_ssl-none'),
        pytest.param(
            {'insecure_skip_host_key_check': False},
            id='the-flag-explicitly-off'),
        pytest.param(
            {'insecure_skip_host_key_check': 'false'},
            id='the-flag-as-the-string-false'),
        pytest.param(
            {'insecure_skip_host_key_check': 'no'},
            id='the-flag-as-the-string-no'),
        pytest.param(
            {'insecure_skip_host_key_check': 'off'},
            id='the-flag-as-the-string-off'),
        pytest.param(
            {'insecure_skip_host_key_check': [0]},
            id='the-flag-as-a-truthy-non-boolean'),
        pytest.param({}, id='nothing-at-all'),
    ],
)
async def test_r16_ac4_nothing_but_the_named_keyword_enables_the_bypass(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    info: dict[str, Any],
) -> None:
    """Only the one keyword turns verification off (R16-AC4b).

    ``verify_ssl`` is the HTTP family's TLS switch and says nothing about
    SSH host keys, but it is the key a caller reaches for -- it is the
    only security-shaped name this library's ``protocol_info`` already
    had. Honouring it here would recreate C4 for every caller who copied
    an HTTP example, and would do so *silently*, since the bypass warning
    would be the only sign and a caller who did not ask for a bypass has
    no reason to read for one.

    The string rows are the same failure from the other direction. A
    value read out of a config file or an environment variable arrives as
    text, and ``'false'`` is a non-empty string: coerced with ``bool()``
    it *enables* the bypass, so a caller who wrote the flag down as off
    would get no host-key verification and a warning they had no reason
    to read. Only the boolean ``True`` is the flag.

    Each row asserts the absence of the option rather than the presence
    of a safe one, because "some value was passed" is how the next
    regression will look.
    """
    double = SSHTransportDouble(host_key=SERVER_HOST_KEY)
    double.install(monkeypatch)
    caplog.set_level(logging.DEBUG, logger='async_gateway')

    await sftp_call(**info)

    assert 'known_hosts' not in double.options
    assert sftp_warnings(caplog) == []


async def test_r16_ac4_known_hosts_none_is_refused_not_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asyncssh spelling of the bypass is not a second door.

    ``known_hosts=None`` is exactly what C4 was, and forwarding a
    caller's ``None`` unchanged would reinstate it under a name that
    warns about nothing -- the forwarding path for ``known_hosts`` is
    otherwise deliberately uninterpreted, so this is the one value it has
    to look at. Rejecting it points the caller at the keyword that does
    warn.
    """
    double = SSHTransportDouble()
    double.install(monkeypatch)

    with pytest.raises(ConfigurationError) as raised:
        await sftp_call(known_hosts=None)

    assert 'insecure_skip_host_key_check' in str(raised.value)
    assert double.connections == []


@pytest.mark.parametrize(
    'info',
    [
        pytest.param(
            {'known_hosts': '/etc/ssh/known_hosts',
             'host_key': SERVER_HOST_KEY},
            id='known_hosts-and-host_key'),
        pytest.param(
            {'host_key': SERVER_HOST_KEY,
             'insecure_skip_host_key_check': True},
            id='host_key-and-the-bypass'),
        pytest.param(
            {'known_hosts': '/etc/ssh/known_hosts',
             'insecure_skip_host_key_check': True},
            id='known_hosts-and-the-bypass'),
    ],
)
async def test_r16_naming_two_host_key_policies_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    info: dict[str, Any],
) -> None:
    """Two policies at once is a caller error, not a precedence puzzle.

    Any precedence this library picked would be invisible at the call
    site, and two of the three pairs resolve *towards* the bypass if the
    later branch wins -- a caller who pinned a key would get no
    verification and never know. Rejecting the pair before dispatch is
    the only reading that cannot fail open.
    """
    double = SSHTransportDouble()
    double.install(monkeypatch)

    with pytest.raises(ConfigurationError):
        await sftp_call(**info)

    assert double.connections == []


# --- R16-AC5: no ambient identity, and reachable key auth ------------------


async def test_r16_ac5_the_default_call_offers_no_ambient_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``client_keys=None`` by default, so ``~/.ssh/id_*`` never goes.

    Omitting ``client_keys`` makes asyncssh load the host process's own
    private keys and offer them to whatever answered -- combined with the
    absent host-key check that was C4, that is this library handing a
    deployment's SSH identities to an impersonating server. ``None`` is
    what refuses, and only ``None``: an empty list is falsy but is not
    ``None``, so asyncssh's ``prepare`` takes the same
    ``load_default_keypairs()`` branch an omitted argument takes. Because
    that distinction is invisible in the recorded option, the assertion
    is made on what asyncssh *resolves* against a real planted key, not
    on the literal value passed.
    """
    ambient = plant_ambient_key(monkeypatch, tmp_path)
    double = SSHTransportDouble(host_key=SERVER_HOST_KEY)
    double.install(monkeypatch)

    await sftp_call()

    assert double.options['client_keys'] is None
    assert ambient not in double.identities.keys
    assert double.identities.keys == ()


async def test_r16_ac5_the_default_call_does_not_reach_the_ssh_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The other ambient-identity path is closed too.

    ``~/.ssh/id_*`` is not the only place asyncssh finds identities to
    offer. Whenever its ``client_keys`` resolution yields anything at all
    -- including the empty list ``load_default_keypairs()`` returns on a
    host that has no key files -- it also points ``agent_path`` at
    ``SSH_AUTH_SOCK`` and offers every identity a running ssh-agent
    holds. A deployment that forwards an agent would hand those to an
    impersonating server with no key on disk to find, so the file check
    above does not cover this on its own.
    """
    plant_ambient_key(monkeypatch, tmp_path)
    monkeypatch.setenv('SSH_AUTH_SOCK', str(tmp_path / 'agent.sock'))
    double = SSHTransportDouble(host_key=SERVER_HOST_KEY)
    double.install(monkeypatch)

    await sftp_call()

    assert double.identities.agent_path is None


async def test_r16_ac5_a_supplied_client_key_list_is_forwarded_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key-based authentication is reachable, and untouched in transit.

    The counterweight to the guard above: refusing ambient identities
    must not also make deliberate key auth impossible. "Unchanged" is
    asserted element for element because a list this library rebuilt --
    filtered, path-expanded, wrapped -- is a list asyncssh may no longer
    accept, and the caller would see it as an authentication failure with
    no hint of where their key went.
    """
    keys = ['/keys/id_ed25519', '/keys/id_rsa']
    double = SSHTransportDouble()
    double.install(monkeypatch)

    await sftp_call(client_keys=keys)

    assert double.options['client_keys'] == keys


async def test_r16_a_password_and_client_keys_are_both_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both credentials reach asyncssh together (R16 edge case).

    The documented order is asyncssh's, not this library's: public-key
    authentication is attempted first and the password is the fallback.
    What this library owes the caller is that supplying one does not drop
    the other -- a client that passed ``client_keys`` *instead of* the
    password would strand every caller whose key is rejected by a server
    that would have accepted the password.
    """
    double = SSHTransportDouble()
    double.install(monkeypatch)

    await sftp_call(client_keys=['/keys/id_ed25519'])

    assert double.options['client_keys'] == ['/keys/id_ed25519']
    assert double.options['password'] == AUTH.password
    assert double.options['username'] == AUTH.login

"""SFTP: host keys and key auth (R16), and honest reporting (R17).

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

The R16 tests deliberately still read the double's ``opened`` counter
rather than the envelope: "this host-key policy was accepted" is a claim
about the handshake, and reading it off the transport keeps it true
however the envelope below the handshake is later shaped.

The R17 half asserts the envelope, because that is precisely what R17 is
about. Three defects lived between the completed operation and the
caller. ``remote_files`` was bound only inside the directory branch and
read unconditionally, so **every single-file target raised
``UnboundLocalError`` after the transfer or the deletion had already
happened** (H2) -- and a blanket ``except Exception`` turned that into
``ok=False``, ``error=None``, ``status_code=999``, which a caller
following the documented retry policy re-ran. On ``mode='remove'`` that
means deleting the file twice and being told, both times, that nothing
happened. Metadata came from string-parsing an undocumented ``str()`` of
``SFTPAttrs`` and crashed before the transfer was even attempted (M4),
and ``recurse=True`` was written into the caller's own
``additional_arguments`` dict, so it was inherited by every later call
sharing that ``protocol_info`` (M28).
"""

import logging
import re
import socket
from pathlib import Path
from typing import Any, Iterator, Optional, Text

from aiohttp import BasicAuth

import asyncssh

import pytest

from async_gateway.async_gateway import request
from async_gateway.logic.sftp_client import SFTPRequest
from async_gateway.utils.envelope import GatewayResponse, new_envelope
from async_gateway.utils.exceptions import ConfigurationError

from tests.fixtures.sftp import (
    ACCEPTED_KEY_ALGORITHMS,
    DIRECTORY_ATTRS,
    FILE_ATTRS,
    HARMLESS_CONTENT,
    LOCAL_CONTENT,
    OTHER_HOST_KEY,
    REMOTE_CONTENT,
    REMOTE_TREE_ROOT,
    SERVER_HOST_KEY,
    SSHTransportDouble,
    SYMLINK_ATTRS,
    StubSFTPClient,
    TYPELESS_ATTRS,
    TransferringSFTPClient,
    UNPARSEABLE_ATTRS,
    hostile_tree,
    plant_ambient_key,
)

HOST = 'sftp.example.invalid'
REMOTE_PATH = '/remote/f'
LOCAL_PATH = '/local/f'
AUTH = BasicAuth('user', 'password')

# A retry policy with retries actually armed. The destructive-success
# criterion is that a completed `remove` is not re-run, and a breaker
# configured to retry nothing would satisfy it without proving anything.
RETRYING_BREAKER: dict[Text, Any] = {
    'maximum_failures': 3,
    'retry_config': {'name': 'delay', 'allowed_retries': 2, 'delay': 0},
}

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


# --- R17-AC1: the operation that succeeded is reported as a success --------


def trusting_double(
    monkeypatch: pytest.MonkeyPatch,
    sftp: Optional[StubSFTPClient] = None,
) -> SSHTransportDouble:
    """Install a double whose host key this call already trusts.

    R17 is about what happens *after* the handshake, so every test below
    pins the server's own key: a session that never opened would satisfy
    "no stale ``recurse`` was passed" by having passed nothing at all.

    Args:
        monkeypatch: The pytest patcher.
        sftp: The SFTP client the session yields, or None for one
            describing a single regular file.

    Returns:
        The installed double, for its recorded connections and client.
    """
    double = SSHTransportDouble(sftp=sftp or StubSFTPClient())
    double.install(monkeypatch)
    return double


def sftp_request(**info: Any) -> tuple[SFTPRequest, GatewayResponse]:
    """Build an ``SFTPRequest`` over an envelope the test can identify.

    Args:
        info: ``protocol_info`` overrides; ``mode`` and ``remote_path``
            have defaults.

    Returns:
        The client and the exact envelope object it was handed.
    """
    envelope = new_envelope(url=HOST, protocol='SFTP', payload={})
    client = SFTPRequest(
        HOST, AUTH, envelope,
        info={'mode': 'get', 'remote_path': REMOTE_PATH, **info},
        redact_params=frozenset())
    return client, envelope


@pytest.mark.parametrize(
    'mode, local_path',
    [
        pytest.param('get', LOCAL_PATH, id='get'),
        pytest.param('put', LOCAL_PATH, id='put'),
        pytest.param('remove', None, id='remove'),
    ],
)
@pytest.mark.parametrize(
    'attrs, target',
    [
        pytest.param(FILE_ATTRS, 'file', id='file'),
        pytest.param(DIRECTORY_ATTRS, 'directory', id='directory'),
    ],
)
async def test_r17_ac1_every_mode_against_either_target_reports_the_truth(
    monkeypatch: pytest.MonkeyPatch,
    mode: Text,
    local_path: Optional[Text],
    attrs: asyncssh.SFTPAttrs,
    target: Text,
) -> None:
    """H2: what the remote side did is what the envelope says, on all six.

    ``remote_files`` was assigned only inside ``if 'directory' in
    lstat['type']`` and read unconditionally two statements later, so the
    three single-file rows raised ``UnboundLocalError`` *after* the
    transfer or the deletion had already happened on the server. The
    directory rows are here as the control: they were the only path that
    ever worked, and a fix that initialises the name but breaks the
    branch would be invisible without them.

    Both dimensions are parametrised rather than folded into one list
    because the defect is the product of the two -- mode decides what was
    already done to the remote side when the crash lands, and target
    decides whether it lands at all.

    Five rows report ``ok=True``. The sixth cannot, and asserting that it
    did was this table's own defect: ``asyncssh.SFTPClient.remove``
    *"removes a remote file or symbolic link"* and takes a path and
    nothing else -- the directory operation is the separate ``rmtree`` --
    so a directory ``remove`` earns ``SSH_FX_FAILURE`` from the server.
    R17-AC1's "``ok=True`` for all six" was unachievable against the real
    library, and the row is asserted as the honest typed failure it is
    (spec amendment, review iteration 2). Routing it to ``rmtree``
    instead would escalate a caller's ``remove`` into a recursive tree
    deletion they never named, which is the class of implicit behaviour
    this release exists to remove.
    """
    double = trusting_double(monkeypatch, StubSFTPClient(attrs=attrs))

    envelope = await sftp_call(
        host_key=SERVER_HOST_KEY,
        mode=mode,
        local_path=local_path)

    assert mode in double.sftp.names(), 'the operation never went out'
    if mode == 'remove' and target == 'directory':
        assert envelope['ok'] is False
        assert envelope['error']['code'] == 'SFTP_STATUS'
        assert envelope['status_code'] == 500
    else:
        assert envelope['ok'] is True, target
        assert envelope['error'] is None
        assert envelope['status_code'] == 200


async def test_r17_ac2_a_completed_deletion_is_never_re_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding in one test: delete once, report success, stop.

    Deleting is not idempotent from the caller's point of view, and the
    old client reported a *successful* deletion as ``ok=False`` with
    ``status_code: 999`` -- the shape the documented retry policy exists
    to re-run. The breaker here has retries armed precisely so that the
    call count means something: with ``allowed_retries=0`` the row would
    pass against a client that had no idea whether it had succeeded.

    The count is asserted on the double's own log, so a client that
    reported success without ever issuing the ``remove`` fails it too.
    """
    double = trusting_double(monkeypatch)

    envelope = await sftp_call(
        host_key=SERVER_HOST_KEY,
        mode='remove',
        circuit_breaker_config=RETRYING_BREAKER)

    assert envelope['ok'] is True
    assert envelope['error'] is None
    assert envelope['status_code'] == 200
    assert double.sftp.names().count('remove') == 1


# --- R17-AC3: the dead attribute that looked like the fix ------------------


def test_r17_ac3_the_dead_remote_files_attribute_is_gone() -> None:
    """L6: the thing that looks like H2's missing initialisation.

    ``self.remote_files`` was assigned the *remote path* -- a string
    where the envelope wanted a list of names -- and then never read.
    Anyone fixing H2 by initialising "the obvious attribute" would have
    published a path string as ``files`` for every single-file call and
    passed every test that only checked ``ok``. It is asserted on the
    constructed object rather than by reading the source, because an
    attribute reintroduced under any spelling of the assignment is the
    regression, not the literal line.
    """
    client, _ = sftp_request()

    assert not hasattr(client, 'remote_files')


# --- R17-AC4: metadata from the typed fields, not from a repr --------------


@pytest.mark.parametrize(
    'attrs, crash',
    [
        pytest.param(UNPARSEABLE_ATTRS, 'IndexError', id='no-colon-fragment'),
        pytest.param(TYPELESS_ATTRS, 'KeyError', id='no-type-key'),
    ],
)
async def test_r17_ac4_attrs_that_used_to_crash_the_parse_now_transfer(
    monkeypatch: pytest.MonkeyPatch,
    attrs: asyncssh.SFTPAttrs,
    crash: Text,
) -> None:
    """M4: the metadata read no longer decides whether the call happens.

    Both rows are ordinary answers from a real server, and both used to
    raise -- ``IndexError`` on a fragment of ``str(attrs)`` with no
    ``':'`` in it, ``KeyError`` on a parse that succeeded and yielded no
    ``type`` key -- **before the transfer was attempted**, landing as a
    fabricated ``999`` for a call that never reached the remote side at
    all. The second row is not an exotic server: ``SFTPAttrs.__str__``
    omits the file type whenever asyncssh reports it as unknown.

    The transfer itself is asserted, not just ``ok``: the point of the
    finding is that a metadata read was standing between the caller and
    the operation they asked for.
    """
    double = trusting_double(monkeypatch, StubSFTPClient(attrs=attrs))

    envelope = await sftp_call(host_key=SERVER_HOST_KEY)

    assert envelope['ok'] is True, crash
    assert envelope['error'] is None
    assert 'get' in double.sftp.names()


async def test_r17_ac4_file_stats_carry_the_typed_attrs_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The metadata a caller gets is the metadata asyncssh reported.

    Asserted field by field against the ``SFTPAttrs`` the server
    answered with, because "it did not crash" is not the criterion --
    the criterion is that the values come from the typed fields. The
    string parse it replaces silently produced ``' 12'`` with a leading
    space for the size and dropped every field whose value contained a
    comma.
    """
    trusting_double(monkeypatch, StubSFTPClient(attrs=FILE_ATTRS))

    envelope = await sftp_call(host_key=SERVER_HOST_KEY)

    assert envelope['protocol_details']['file_stats'] == {
        'type': 'file',
        'size': FILE_ATTRS.size,
        'permissions': FILE_ATTRS.permissions,
        'uid': None,
        'gid': None,
        'owner': None,
        'group': None,
        'atime': None,
        'mtime': None,
    }


# --- R17-AC5 / M28: the caller's dict is never mutated ---------------------


async def test_r17_ac5_a_shared_protocol_info_does_not_leak_recurse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M28: the cross-request state leak, in its documented shape.

    ``self.additional_arguments.update({'recurse': True})`` wrote into
    the caller's own dict, which reaches the client by reference through
    three layers. One directory ``get`` therefore left
    ``protocol_info['additional_arguments']`` permanently
    ``{'recurse': True}``, and the next call sharing that config -- the
    ``asyncio.gather`` with one shared config this library documents --
    inherited it. A single-file ``remove`` with ``recurse=True`` is a
    keyword the caller never wrote, sent to a server, on the one
    operation that cannot be undone.

    One ``additional_arguments`` object is deliberately reused rather
    than copied between the calls: copying it is what the caller is *not*
    required to do, and a test that copied would prove nothing.

    The second call carries no ``additional_arguments`` of its own,
    because ``asyncssh.SFTPClient.remove`` accepts none: it takes a path
    and nothing else, so a ``preserve`` handed to it is a ``TypeError``
    from the library and driving one would only assert that a stub
    swallowed it. Asserting the deletion went out with an **empty**
    option mapping is the stricter claim anyway -- it fails on any
    keyword leaking in, not merely on ``recurse``.
    """
    shared_arguments: dict[Text, Any] = {'preserve': True}
    protocol_info: dict[Text, Any] = {
        'host_key': SERVER_HOST_KEY,
        'remote_path': REMOTE_PATH,
    }
    double = trusting_double(
        monkeypatch, StubSFTPClient(attrs=DIRECTORY_ATTRS))

    await request(
        HOST, data={}, auth=AUTH, protocol='SFTP',
        protocol_info={
            **protocol_info,
            'mode': 'get',
            'additional_arguments': shared_arguments,
        })
    double.sftp.attrs = FILE_ATTRS
    await request(
        HOST, data={}, auth=AUTH, protocol='SFTP',
        protocol_info={**protocol_info, 'mode': 'remove'})

    options = {name: kwargs for name, _, kwargs in double.sftp.calls}
    assert options['get'] == {'preserve': True, 'recurse': True}
    assert options['remove'] == {}
    assert shared_arguments == {'preserve': True}


async def test_r17_ac5_the_callers_additional_arguments_reach_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copying the dict must not mean discarding what was in it.

    The cheapest way to pass the leak test above is to stop forwarding
    ``additional_arguments`` at all, which would silently drop every
    option a caller configured. This is the counterweight.
    """
    double = trusting_double(monkeypatch)

    await sftp_call(
        host_key=SERVER_HOST_KEY,
        additional_arguments={'block_size': 4096})

    assert dict(
        (name, kwargs) for name, _, kwargs in double.sftp.calls
    )['get'] == {'block_size': 4096}


# --- R17-AC6 / H10-sftp: the session is bounded ----------------------------


async def test_r17_ac6_the_connect_and_the_login_are_both_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H10-sftp: SFTP was the protocol that never used ``self.timeout``.

    ``base.py`` computes it for every protocol and SFTP ignored it, so a
    server that completed the TCP handshake and then went silent held the
    coroutine open forever. The breaker cannot help: a hang raises
    nothing for it to count, so no failure is ever recorded and the
    circuit never opens. Both keywords are asserted because they bound
    different halves -- the TCP connect, and the SSH authentication that
    follows it.
    """
    double = trusting_double(monkeypatch)

    await sftp_call(host_key=SERVER_HOST_KEY, timeout=7)

    assert double.options['connect_timeout'] == 7
    assert double.options['login_timeout'] == 7


# --- R17-AC8: the envelope contract ----------------------------------------


async def test_r17_ac8_the_client_returns_the_envelope_it_was_handed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E1's other half: one object is filled and returned, not replaced.

    ``return True`` is what the old client did. The entry point assigns
    what ``handle_request`` returns straight back over its own envelope,
    so the caller of an SFTP call received the bare literal ``True`` --
    no keys, no ``ok``, nothing the documented ``result['ok']`` check
    could even be applied to. Identity rather than equality, because a
    client that built an equal dict of its own would discard everything
    the entry point had already written into the envelope.
    """
    trusting_double(monkeypatch)
    client, envelope = sftp_request(host_key=SERVER_HOST_KEY)

    returned = await client.handle_request()

    assert returned is envelope
    assert returned['ok'] is True


async def test_r17_ac8_the_protocol_extras_live_under_protocol_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8: SFTP's own keys stop leaking into the top-level key set.

    ``mode``, ``files``, ``file_stats`` and ``tat`` were written at the
    top level, so the envelope's shape depended on which protocol had
    answered -- the exact thing R8 exists to remove. ``latency`` is
    asserted too: ``tat`` was not renamed in place, it was a *different*
    number, computed from ``time.time()`` and therefore able to go
    backwards across a clock step.
    """
    trusting_double(monkeypatch, StubSFTPClient(attrs=DIRECTORY_ATTRS))

    envelope = await sftp_call(
        host_key=SERVER_HOST_KEY, local_path=LOCAL_PATH)

    assert envelope['protocol_details'] == {
        'mode': 'get',
        'remote_path': REMOTE_PATH,
        'local_path': LOCAL_PATH,
        'file_stats': envelope['protocol_details']['file_stats'],
        'files': ['f'],
    }
    assert 'tat' not in envelope
    assert envelope['latency'] >= 0
    for leaked in ('mode', 'files', 'file_stats'):
        assert leaked not in envelope


def test_r17_ac9_no_docstring_in_the_module_calls_it_an_ftp_class() -> None:
    """L5: the SFTP module described itself as FTP.

    Copy-paste provenance left in the one place a reader looks to find
    out what the class is. It matters more than a typo usually would:
    this package has both protocols, they take different
    ``protocol_info`` keys, and the docstring sent the reader to the
    wrong one.
    """
    docstrings = [
        SFTPRequest.__doc__,
        SFTPRequest.__init__.__doc__,
        SFTPRequest.handle_request.__doc__,
    ]

    # A word boundary, so that the fix cannot be read as "say `sftp
    # request class` and the substring is still in there".
    calls_itself_ftp = re.compile(r'\bftp request class\b')
    for docstring in docstrings:
        assert docstring is not None
        assert not calls_itself_ftp.search(docstring.lower())


# --- R17 edge cases --------------------------------------------------------


async def test_r17_a_missing_mode_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None.lower()`` is not a diagnosis (R17 edge case).

    ``mode`` has no default, so a caller who omitted it used to open a
    connection, run ``lstat`` against the server, and only then raise
    ``AttributeError`` from ``self.mode_.lower()`` -- swallowed into the
    same ``999`` as everything else, which says nothing about whose
    mistake it was or whether retrying could help.

    Reported as an envelope rather than raised synchronously, unlike the
    host-key policy errors above: ``protocol_info`` is optional for this
    protocol at the entry point (R11-AC3), so the check cannot live in
    the constructor. It still runs before the connect, which is what the
    empty connection log asserts.
    """
    double = trusting_double(monkeypatch)

    envelope = await request(
        HOST, data={}, auth=AUTH, protocol='SFTP',
        protocol_info={'remote_path': REMOTE_PATH})

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'CONFIG'
    assert envelope['status_code'] == 400
    assert 'mode' in envelope['error']['message']
    assert double.connections == []


@pytest.mark.parametrize(
    'attrs, entries, files',
    [
        pytest.param(FILE_ATTRS, ('f',), None, id='file'),
        pytest.param(SYMLINK_ATTRS, ('f',), None, id='symlink'),
        pytest.param(DIRECTORY_ATTRS, (), [], id='empty-directory'),
        pytest.param(DIRECTORY_ATTRS, ('a', 'b'), ['a', 'b'], id='directory'),
    ],
)
async def test_r17_files_distinguishes_an_empty_directory_from_a_file(
    monkeypatch: pytest.MonkeyPatch,
    attrs: asyncssh.SFTPAttrs,
    entries: tuple[Text, ...],
    files: Optional[list[Text]],
) -> None:
    """``files: []`` and ``files: None`` are different answers.

    An empty directory really has no entries; a file has no entry list at
    all. Collapsing them -- which ``files = remote_files or []`` would --
    tells a caller that a file they just downloaded is an empty
    directory. The symlink row is the third state the old ``'directory'
    in lstat['type']`` substring test never had a name for.
    """
    double = trusting_double(
        monkeypatch, StubSFTPClient(attrs=attrs, entries=entries))

    envelope = await sftp_call(host_key=SERVER_HOST_KEY)

    assert envelope['protocol_details']['files'] == files
    assert ('listdir' in double.sftp.names()) is (files is not None)


# --- failure translation ---------------------------------------------------


@pytest.mark.parametrize(
    'error, code, status',
    [
        pytest.param(
            asyncssh.SFTPNoSuchFile('no such file'),
            'SFTP_STATUS', 404, id='no-such-file'),
        pytest.param(
            asyncssh.SFTPPermissionDenied('denied'),
            'SFTP_STATUS', 403, id='permission-denied'),
        pytest.param(
            asyncssh.SFTPFailure('it went wrong'),
            'SFTP_STATUS', 500, id='other-sftp-error'),
        pytest.param(
            ConnectionRefusedError('refused'), 'CONNECT', 502, id='refused'),
        pytest.param(
            socket.gaierror('name not known'), 'DNS', 502, id='dns'),
        pytest.param(
            TimeoutError('too slow'), 'TIMEOUT', 504, id='timeout'),
        pytest.param(
            asyncssh.PermissionDenied('authentication failed'),
            'TRANSPORT', 502, id='ssh-error-with-no-family'),
    ],
)
@pytest.mark.parametrize('during', ['lstat', 'operation'])
async def test_a_failure_maps_to_its_typed_error_wherever_it_is_raised(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    code: Text,
    status: int,
    during: Text,
) -> None:
    """Every failure carries a code and a status a consumer can read.

    The blanket ``except Exception`` reported all of these identically:
    ``ok=False``, ``error=None``, ``status_code=999`` and the exception
    *object* in ``text``, which is not even serialisable. A caller could
    not tell a missing file from a dead host, so no retry decision was
    possible -- and 404 versus 502 is exactly that decision.

    The last row is an SSH-level failure -- a rejected password -- that
    belongs to no transport family and is not an ``SFTPError`` either. It
    is here because the classifier's fallthrough is the branch a library
    bug also reaches, and the two must part company: an ``asyncssh.Error``
    is a failed call and becomes an envelope, while anything else is this
    library's bug and propagates (the test below).

    Both raise sites are parametrised because they take different routes
    through the classifier: the operation runs inside the resilience
    layer, which wraps whatever it raised in a ``FailsafeError`` whose
    own ``str()`` is empty, so both the class and the message have to be
    recovered from the cause. ``lstat`` is not wrapped. A classifier that
    only unwrapped would report the unwrapped case as a bare type name.
    """
    trusting_double(monkeypatch, StubSFTPClient(**{f'{during}_error': error}))

    envelope = await sftp_call(host_key=SERVER_HOST_KEY, mode='remove')

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == code
    assert envelope['status_code'] == status
    assert envelope['error']['message']


async def test_an_open_circuit_is_reported_as_an_open_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The breaker's own refusal is not a transport failure.

    ``CircuitOpen`` is a ``FailsafeError`` with nothing under it that the
    generic unwrap can classify, so without its own clause it would land
    as a 502 the caller would retry -- into a circuit that is open
    precisely to stop them. 503 says "not now" instead.
    """
    trusting_double(
        monkeypatch,
        StubSFTPClient(operation_error=ConnectionResetError('reset')))

    envelope = await sftp_call(
        host_key=SERVER_HOST_KEY,
        circuit_breaker_config={
            'maximum_failures': 1,
            'retry_config': {'name': 'delay', 'allowed_retries': 2,
                             'delay': 0},
        })

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'CIRCUIT_OPEN'
    assert envelope['status_code'] == 503


async def test_a_library_bug_propagates_instead_of_becoming_an_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule the blanket ``except Exception`` broke for this package.

    A ``KeyError`` raised inside the session is this library's bug, not a
    failed request. Reporting it as a ``999`` envelope is what hid it --
    and hid most of this audit -- for the life of the package.
    Reinstate any blanket handler and this test stops raising.
    """
    trusting_double(
        monkeypatch, StubSFTPClient(operation_error=KeyError('library bug')))

    with pytest.raises(KeyError):
        await sftp_call(host_key=SERVER_HOST_KEY)


# --- AGW-33: the operand order, per mode -----------------------------------


async def test_agw33_an_upload_mode_passes_local_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asyncssh signatures disagree on order, so one cannot serve.

    ``get(remotepaths, localpath)`` against
    ``put(localpaths, remotepath)``. The client passed
    ``(remote_path, local_path)`` positionally to **every** mode, which
    is correct for ``get``/``mget`` and inverted for ``put``/``mput``.
    """
    double = trusting_double(monkeypatch)

    await sftp_call(
        host_key=SERVER_HOST_KEY, mode='put', local_path=LOCAL_PATH)

    passed = {name: args for name, args, _ in double.sftp.calls}
    assert passed['put'] == (LOCAL_PATH, REMOTE_PATH)


def test_agw33_mput_is_tabled_local_first_before_it_is_dispatchable(
) -> None:
    """``mput``'s direction is pinned even though S19 will not dispatch it.

    This was an end-to-end row of the ``put`` test above until S19's
    allowlist landed. The two stories are both right and do not
    actually disagree: AGW-33 says *if* ``mput`` runs, its local operand
    goes first; R21 says ``mput`` may not be asked for yet, precisely
    because -- as :data:`SFTP_MODES` records -- admitting a verb is an
    operand-order commitment. Driving it through ``sftp_call`` after
    S19 asserts the allowlist is broken, not that the order is right.

    So the assertion moves down to the table that owns the property.
    That keeps AGW-33's guarantee live and load-bearing for the day a
    story admits ``mput``: whoever adds it to :data:`SFTP_MODES` gets a
    correct operand order already tested, which is the whole reason the
    table carries a row for a mode nothing dispatches.
    """
    client, _ = sftp_request(mode='put', local_path=LOCAL_PATH)

    assert client._operands('mput') == (LOCAL_PATH, REMOTE_PATH)
    assert client._operands('put') == (LOCAL_PATH, REMOTE_PATH)
    assert client._operands('get') == (REMOTE_PATH, LOCAL_PATH)


async def test_agw33_a_download_keeps_remote_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get`` was already right, and swapping the shared order breaks it.

    The regression the fix could introduce: replacing one universal
    order with the *other* universal order fixes ``put`` and silently
    inverts ``get``. Pinning both directions is what makes the per-mode
    table demonstrably a table.
    """
    double = trusting_double(monkeypatch)

    await sftp_call(
        host_key=SERVER_HOST_KEY, mode='get', local_path=LOCAL_PATH)

    passed = {name: args for name, args, _ in double.sftp.calls}
    assert passed['get'] == (REMOTE_PATH, LOCAL_PATH)


async def test_agw33_a_put_sends_the_local_file_the_caller_named(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The mirrored-tree case, asserted on content and not on arguments.

    S12's double is signature-faithful, which catches a refused keyword
    or a wrong arity -- but both operands here are path-like
    positionals, so a transposition binds cleanly and it cannot see
    this. Nor can a double that merely records the argument (the
    standing S11 learning). Only a modelled filesystem can.

    The setup is the deployment that makes AGW-33 High: a local file
    exists at the remote path, so the transposed ``put`` finds
    something, uploads it, and reports ``ok=True`` -- the wrong file, at
    the wrong destination, under a success envelope.
    """
    local = tmp_path / 'local' / 'report.pdf'
    local.parent.mkdir()
    local.write_bytes(LOCAL_CONTENT)
    mirrored = tmp_path / 'mirror' / 'report.pdf'
    mirrored.parent.mkdir()
    mirrored.write_bytes(REMOTE_CONTENT)
    double = trusting_double(monkeypatch, TransferringSFTPClient())

    envelope = await sftp_call(
        host_key=SERVER_HOST_KEY,
        mode='put',
        remote_path=str(mirrored),
        local_path=str(local))

    assert envelope['ok'] is True
    assert double.sftp.uploaded == LOCAL_CONTENT
    assert double.sftp.uploaded != REMOTE_CONTENT
    assert double.sftp.upload_destination == str(mirrored)


# --- R22: containment on the recursive download ----------------------------


async def test_r22_a_hostile_entry_name_cannot_write_outside_local_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """M17's vector on SFTP, driven through asyncssh's real ``_copy``.

    Testing ``resolve_within`` directly is exactly what lets this hide:
    **none of the filename arithmetic happens in this library**.
    ``_copy`` filters a ``scandir`` entry name that is ``.`` or ``..``
    *exactly*, then ``posixpath.join``s it onto the destination -- so a
    single name that **contains** a separator, ``../victimdir/OWNED``,
    is not filtered and composes straight through.

    Only the remote side is faked; the recursion, the filter and the
    join are asyncssh's own. Measured against the unfixed path, the
    hostile entry landed outside the target at mode 0644.

    The claim is about the **filesystem**: the victim directory stays
    empty, and the transfer is known to have really run because the
    operation reached the server exactly once rather than being refused
    before it started.
    """
    target = tmp_path / 'downloads'
    victim = tmp_path / 'victimdir'
    victim.mkdir()
    double = trusting_double(
        monkeypatch,
        StubSFTPClient(
            attrs=DIRECTORY_ATTRS,
            remote_tree=hostile_tree(escape=f'../{victim.name}/OWNED')))

    envelope = await sftp_call(
        host_key=SERVER_HOST_KEY,
        mode='get',
        remote_path=REMOTE_TREE_ROOT,
        local_path=str(target))

    assert envelope['ok'] is False
    assert envelope['error']['code'] == 'PATH'
    assert envelope['status_code'] == 400
    assert list(victim.iterdir()) == [], (
        'a server-supplied entry name escaped the download directory')
    assert double.sftp.names().count('get') == 1


async def test_r22_an_ordinary_tree_still_downloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The control: a well-behaved recursive download still works.

    Without it, refusing every download would satisfy the row above.
    Bytes *and* mode are asserted -- the mode because M18's other half
    is that a download landed at whatever ``umask`` allowed, which in
    the measured escape was 0644.
    """
    target = tmp_path / 'downloads'
    trusting_double(
        monkeypatch,
        StubSFTPClient(attrs=DIRECTORY_ATTRS, remote_tree=hostile_tree()))

    envelope = await sftp_call(
        host_key=SERVER_HOST_KEY,
        mode='get',
        remote_path=REMOTE_TREE_ROOT,
        local_path=str(target))

    landed = target / 'harmless.txt'
    assert envelope['ok'] is True
    assert landed.read_bytes() == HARMLESS_CONTENT
    assert landed.stat().st_mode & 0o777 == 0o600


async def test_r22_a_server_supplied_symlink_is_not_recreated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A link the *server* describes is refused rather than created.

    ``_copy`` answers a remote symlink by creating a local one with the
    **server's** target string. Containing the link's own path would
    keep it inside the base and would not stop it *pointing* out -- and
    a link inside the download directory aimed at ``/etc/passwd`` is an
    escape the next write would have to catch. Refusing is the narrower
    guarantee and the one R22 asks for.
    """
    target = tmp_path / 'downloads'
    trusting_double(
        monkeypatch,
        StubSFTPClient(
            attrs=DIRECTORY_ATTRS,
            remote_tree=hostile_tree(symlink='/etc/passwd')))

    envelope = await sftp_call(
        host_key=SERVER_HOST_KEY,
        mode='get',
        remote_path=REMOTE_TREE_ROOT,
        local_path=str(target))

    assert envelope['ok'] is False
    assert envelope['error']['code'] == 'PATH'
    assert not (target / 'link').is_symlink()


async def test_r22_the_containment_seam_is_required_not_optional(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If the asyncssh seam disappears, the download is refused.

    Containment is installed at ``SFTPClient._begin_copy``, which is
    private -- a real cost, and this is the guard on it. A future
    asyncssh that renames or removes the method must produce a refused
    call, never a download quietly running through the uncontained
    ``local_fs`` again. That failure would otherwise be completely
    silent, which is the property this whole mechanism exists to remove.
    """
    double = trusting_double(monkeypatch)
    monkeypatch.delattr(type(double.sftp), '_begin_copy')

    envelope = await sftp_call(
        host_key=SERVER_HOST_KEY, mode='get', local_path=str(tmp_path / 'f'))

    assert envelope['ok'] is False
    assert envelope['error']['code'] == 'PATH'
    assert '_begin_copy' in envelope['error']['message']


async def test_r22_a_downloads_caller_options_still_reach_asyncssh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Routing through the private seam does not drop the caller's options.

    ``contained_download`` binds ``additional_arguments`` against the
    **public** ``get`` signature and forwards the result positionally.
    Two things must hold: an option the caller set arrives, and
    ``recurse`` -- which the client adds for a directory target --
    arrives with it.
    """
    double = trusting_double(
        monkeypatch, StubSFTPClient(attrs=DIRECTORY_ATTRS))

    await sftp_call(
        host_key=SERVER_HOST_KEY,
        mode='get',
        local_path=str(tmp_path / 'f'),
        additional_arguments={'preserve': True})

    options = {name: kwargs for name, _, kwargs in double.sftp.calls}
    assert options['get'] == {'preserve': True, 'recurse': True}


async def test_r22_an_option_asyncssh_would_refuse_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reaching a private method does not widen what the public one takes.

    The risk in calling ``_begin_copy`` is that it takes its options
    **positionally**, so it would accept anything placed in the right
    slot. ``contained_download`` binds the caller's options against the
    real public ``get`` signature first, so a keyword asyncssh does not
    have is refused with asyncssh's own ``TypeError`` -- which has a
    library bug's shape and propagates rather than becoming an envelope.
    """
    trusting_double(monkeypatch)

    with pytest.raises(TypeError):
        await sftp_call(
            host_key=SERVER_HOST_KEY,
            mode='get',
            local_path=str(tmp_path / 'f'),
            additional_arguments={'no_such_option': True})


# --- the verb allowlist (R21-AC1,2 · R17-AC7) -------------------------------


@pytest.mark.parametrize(
    'mode',
    [
        pytest.param('rmtree', id='destructive-not-admitted'),
        pytest.param('exit', id='session-attribute'),
        pytest.param('mget', id='real-but-not-admitted'),
        pytest.param('gett', id='typo'),
    ],
)
async def test_r17_ac7_an_unadmitted_mode_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
    mode: Text,
) -> None:
    """An unknown mode is CONFIG, and the allowed set is named.

    ``rmtree`` is the row that matters most. It is a real, recursive,
    destructive method of ``asyncssh.SFTPClient``, and the unbounded
    ``getattr`` this replaces resolved it happily -- so a caller one
    character away from ``remove`` deleted a tree. The spec is explicit
    that this crosses no privilege boundary (a caller who can ask for
    ``remove`` can ask for ``rmtree``), which is exactly why the fix is
    an allowlist rather than a permission check: the defect is an
    unbounded capability surface, and bounding it is the whole remedy.
    """
    trusting_double(monkeypatch)

    envelope = await sftp_call(mode=mode, host_key=SERVER_HOST_KEY)

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'CONFIG'
    assert envelope['status_code'] == 400
    assert repr(mode) in envelope['error']['message']
    for allowed in ('get', 'put', 'remove'):
        assert allowed in envelope['error']['message']


async def test_an_unadmitted_mode_runs_nothing_on_the_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: ``rmtree`` is refused before the channel is addressed.

    The envelope assertion above would hold just as well for a check
    placed after the attribute was resolved. This one would not: the stub
    records every operation asked of it, so an empty log is what
    distinguishes "refused the name" from "resolved the name and then
    declined to call it".

    ``lstat`` and ``listdir`` still run -- they are the session prologue,
    not the caller's operation -- so the assertion names the operation
    rather than requiring silence.
    """
    double = trusting_double(monkeypatch)

    envelope = await sftp_call(mode='rmtree', host_key=SERVER_HOST_KEY)

    assert envelope['ok'] is False
    assert 'rmtree' not in double.sftp.names()


@pytest.mark.parametrize(
    'mode',
    [
        pytest.param(' get ', id='surrounding-whitespace'),
        pytest.param('GET', id='upper'),
        pytest.param('Get', id='mixed'),
    ],
)
async def test_an_admitted_mode_is_matched_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
    mode: Text,
) -> None:
    """One reading of a verb, matching the protocol name's (R21).

    The normalisation has to reach the ``RECURSING_MODES`` test too, not
    only the allowlist: ``'GET'`` admitted by a case-insensitive
    allowlist but compared case-sensitively against the recursing set
    would run a directory ``get`` without ``recurse``, which is the
    silent half-failure the two lookups agreeing prevents.
    """
    double = trusting_double(monkeypatch)

    envelope = await sftp_call(
        mode=mode, local_path=LOCAL_PATH, host_key=SERVER_HOST_KEY)

    assert envelope['ok'] is True
    assert 'get' in double.sftp.names()


async def test_a_normalised_mode_still_selects_the_recursing_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``'GET'`` on a directory recurses exactly as ``'get'`` does.

    The companion to the case test above, and the reason the mode is
    normalised once and read twice rather than lower-cased at the
    allowlist alone. Delete the ``.strip().lower()`` feeding the
    ``RECURSING_MODES`` test and this goes red while every other SFTP
    test stays green.
    """
    double = trusting_double(
        monkeypatch, StubSFTPClient(attrs=DIRECTORY_ATTRS))

    envelope = await sftp_call(
        mode='GET', local_path=LOCAL_PATH, host_key=SERVER_HOST_KEY)

    assert envelope['ok'] is True
    options = {name: kwargs for name, _, kwargs in double.sftp.calls}
    assert options['get'].get('recurse') is True

"""An SSH transport double that verifies the host key, from the start.

FI-9 of the v1.0.0 release: this double is written with host-key
verification **on**, before the first SFTP test is written against it. A
double that connected to anything would have to be rewritten the moment
R16 landed -- and until it was, every SFTP test would have been quietly
certifying the behaviour C4 is about: a client that trusts whatever
answers, passing its whole suite.

What it doubles is ``asyncssh.connect``, the last seam before the wire,
exactly as ``tests/fixtures/protocol_transports.py`` doubles it for the
envelope contract rows. Everything between that seam and the caller --
``logic/sftp_client.py``'s option building and the entry point's one
conversion point -- is the real code under test, and no socket is opened.

What it proves, and what it does not. The double **models** asyncssh's
host-key rules rather than implementing them: it reads the connection
options it was handed and decides, so a test can drive a mismatched key
all the way to ``error['code'] == 'HOST_KEY'`` without a server, a key
pair or a network. A model can agree with a client that configures the
real asyncssh wrongly, so the assertions that matter for the shipped
library are made on the *options themselves* -- recorded verbatim in
:attr:`SSHTransportDouble.connections` -- and the model only carries
those options to an outcome.

Recording verbatim is necessary and, for one option, not sufficient.
asyncssh does not treat ``client_keys`` as the literal set of keys to
offer: an empty list is falsy but is not ``None``, so its ``prepare``
falls through to ``load_default_keypairs()`` and offers the host
process's own ``~/.ssh/id_*`` identities -- the very thing an empty list
reads as refusing. A test asserting ``client_keys == []`` against a
verbatim record therefore certifies a leak as a fix.
``offered_identities`` closes that gap by resolving the recorded options
through the **real** ``SSHClientConnectionOptions``, so what a test
asserts about identities is what the shipped library will do, not a
restatement of its rules that can drift from it.

The operation surface (:class:`StubSFTPClient`) is the minimum the
host-key tests need to reach a session. R17 owns extending it for the
transfer and mutation modes its own tests drive; grow it there rather
than here, for operations no test exercises yet.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import asyncssh

import pytest

from tests.fixtures.protocol_transports import entered

# The key the doubled server presents, and one it does not, so a test can
# pin the wrong key without inventing a second server.
SERVER_HOST_KEY = 'ssh-ed25519 AAAASERVERKEY'
OTHER_HOST_KEY = 'ssh-ed25519 AAAAOTHERKEY'

# The algorithm that key is in, and the algorithms a client accepts by
# default. A server whose only algorithm is outside the set is how the
# "offers only an algorithm the client rejects" edge case is reached.
SERVER_KEY_ALGORITHM = 'ssh-ed25519'
ACCEPTED_KEY_ALGORITHMS = frozenset({'ssh-ed25519', 'rsa-sha2-256'})


@dataclass(frozen=True)
class OfferedIdentities:
    """The identities a set of connection options actually offers.

    Attributes:
        keys: The public half of every keypair asyncssh would present, in
            its own order. Empty means no key is offered at all.
        agent_path: The ssh-agent socket asyncssh would additionally
            collect identities from, or None when the agent is not
            consulted at all. The empty string is asyncssh's third state
            -- the agent path is enabled but no socket is configured --
            and is deliberately not collapsed into None, because a
            deployment that forwards an agent turns it into a live leak.
    """

    keys: tuple[bytes, ...]
    agent_path: Optional[str]


def offered_identities(options: Mapping[str, Any]) -> OfferedIdentities:
    """Resolve options the way ``asyncssh`` itself resolves them.

    ``client_keys`` is the one option this double cannot judge by
    reading. asyncssh maps ``[]`` and an omitted argument onto the *same*
    ``load_default_keypairs()`` branch and enables ``agent_path`` for
    both, so a recorded ``client_keys == []`` says nothing about whether
    the host's identities were offered. The real
    ``SSHClientConnectionOptions`` is built here so the answer comes from
    the library that will run in production.

    Args:
        options: The keyword arguments the client passed to ``connect``.
            Only ``client_keys`` is forwarded, and only when the client
            passed it, because whether it is present at all is itself
            part of what is under test.

    Returns:
        What asyncssh would offer for these options.
    """
    forwarded: dict[str, Any] = {}
    if 'client_keys' in options:
        forwarded['client_keys'] = options['client_keys']

    prepared = asyncssh.SSHClientConnectionOptions(**forwarded)
    keypairs = prepared.client_keys or ()
    return OfferedIdentities(
        keys=tuple(keypair.public_data for keypair in keypairs),
        agent_path=prepared.agent_path,
    )


def plant_ambient_key(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
) -> bytes:
    """Give the host process an ambient ``~/.ssh/id_ed25519`` to leak.

    Without one there is nothing to leak: ``load_default_keypairs()``
    finds no file, and a client that wrongly asks asyncssh for the host's
    own identities is indistinguishable from one that refuses them. The
    check would then pass in a container and fail on the laptop of
    whoever has an SSH key, which is worse than no check. Planting one
    makes the leak reachable, and the self-check below makes a planting
    that silently did nothing a failure rather than a green test.

    Args:
        monkeypatch: The patcher, which restores ``HOME`` on teardown.
        home: The directory standing in for the host process's home.

    Returns:
        The public half of the planted key, so a test can name the exact
        identity that must not be offered.

    Raises:
        AssertionError: If asyncssh does not pick the planted key up as
            an ambient default, which would make every assertion built on
            it vacuous.
    """
    ssh_dir = home / '.ssh'
    ssh_dir.mkdir(parents=True, exist_ok=True)
    key = asyncssh.generate_private_key('ssh-ed25519')
    (ssh_dir / 'id_ed25519').write_bytes(key.export_private_key())
    (ssh_dir / 'id_ed25519.pub').write_bytes(key.export_public_key())
    monkeypatch.setenv('HOME', str(home))

    assert key.public_data in offered_identities({}).keys, (
        'the planted key is not an ambient default, so a test asserting '
        'it is never offered would pass without proving anything')
    return key.public_data


def read_trusted_keys(path: Optional[Path]) -> list[str]:
    """Return the host keys a ``known_hosts`` file makes trusted.

    Args:
        path: The file to read, or None for a host that has none --
            which is the container case R16's edge case names, and is
            modelled as "nothing is trusted" rather than as an error,
            because that is what asyncssh itself does with a missing
            ``~/.ssh/known_hosts``.

    Returns:
        One entry per non-empty line, or an empty list when there is no
        readable file. Empty means nothing is trusted, so verification
        fails closed.
    """
    if path is None or not path.is_file():
        return []
    return [
        line.strip() for line in path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]


class StubSFTPClient:
    """The slice of ``asyncssh``'s SFTP client an SFTP session uses.

    Records every operation it was asked to perform so a test can assert
    what actually happened on the remote side -- including that a
    destructive operation ran exactly once.
    """

    def __init__(self) -> None:
        """Start with an empty operation log."""
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def lstat(self, path: str) -> asyncssh.SFTPAttrs:
        """Return the attributes ``asyncssh`` reports for a remote path.

        The real ``asyncssh.SFTPAttrs`` is returned rather than a
        stand-in, so a client reading it is held to the type the library
        actually hands back.

        Args:
            path: The remote path being described.

        Returns:
            Attributes describing a regular file.
        """
        self.calls.append(('lstat', (path,)))
        return asyncssh.SFTPAttrs(size=12, permissions=0o100644)

    async def listdir(self, path: str) -> list[str]:
        """Return the entries ``asyncssh`` reports for a remote directory.

        Args:
            path: The remote directory being listed.

        Returns:
            One file name.
        """
        self.calls.append(('listdir', (path,)))
        return ['f']

    async def get(self, *args: Any, **kwargs: Any) -> None:
        """Accept a download and report success by not raising.

        Args:
            args: The remote and local paths.
            kwargs: Transfer options such as ``recurse``, unused.

        Returns:
            None, as ``asyncssh`` does.
        """
        self.calls.append(('get', args))

    async def put(self, *args: Any, **kwargs: Any) -> None:
        """Accept an upload and report success by not raising.

        Args:
            args: The local and remote paths.
            kwargs: Transfer options such as ``recurse``, unused.

        Returns:
            None, as ``asyncssh`` does.
        """
        self.calls.append(('put', args))

    async def remove(self, *args: Any, **kwargs: Any) -> None:
        """Accept a deletion and report success by not raising.

        Args:
            args: The remote path being removed.
            kwargs: Options, unused.

        Returns:
            None, as ``asyncssh`` does.
        """
        self.calls.append(('remove', args))


class StubSSHConnection:
    """The slice of an ``asyncssh`` connection an SFTP session uses."""

    def __init__(self, sftp: StubSFTPClient) -> None:
        """Hold the SFTP client this connection opens channels onto.

        Args:
            sftp: The client every ``start_sftp_client`` call yields, so
                one test can read back everything the session did.
        """
        self._sftp = sftp

    def start_sftp_client(self) -> Any:
        """Open an SFTP channel on this connection.

        Returns:
            An async context manager yielding the SFTP client.
        """
        return entered(self._sftp)


class FailingTransport:
    """An async context manager whose entry raises a prepared failure.

    The shape ``asyncssh`` produces for a handshake that goes wrong: the
    call that builds the context manager succeeds, and the failure
    surfaces from ``__aenter__``.
    """

    def __init__(self, error: BaseException) -> None:
        """Hold the failure this transport will raise on entry.

        Args:
            error: The exception ``__aenter__`` raises.
        """
        self._error = error

    async def __aenter__(self) -> Any:
        """Fail the connection the way a rejected handshake does.

        Returns:
            Never; this always raises.

        Raises:
            BaseException: The prepared failure, always.
        """
        raise self._error

    async def __aexit__(self, *exc_info: Any) -> bool:
        """Leave the context, suppressing nothing.

        Args:
            exc_info: The exception triple, unused.

        Returns:
            False, so any exception propagates.
        """
        return False


@dataclass
class SSHTransportDouble:
    """A stand-in for ``asyncssh.connect`` that checks the host key.

    Attributes:
        host_key: The key the doubled server presents.
        key_algorithm: The algorithm that key is in.
        accepted_key_algorithms: What the client will accept. A server
            outside this set ends the handshake before the key is even
            looked at, which is asyncssh's own ordering.
        system_known_hosts: The file standing in for the host's own
            ``~/.ssh/known_hosts``, consulted only when the client omits
            the ``known_hosts`` option. None is the container case: the
            host has no such file, so nothing is trusted.
        connections: The keyword arguments of every ``connect`` call, in
            order and verbatim. These are the assertions that hold the
            real library to account; the verification model below only
            carries them to an outcome. For identities, assert on
            :attr:`identities` instead -- the literal value is not what
            asyncssh acts on.
        opened: How many connections got past verification. A test that
            needs "this policy was accepted" asserts on this rather than
            on the envelope, so it stays true through R17's rewrite of
            everything downstream of the handshake.
        sftp: The SFTP client every session yields.

    Usage:
        double = SSHTransportDouble()
        double.install(monkeypatch)
    """

    host_key: str = SERVER_HOST_KEY
    key_algorithm: str = SERVER_KEY_ALGORITHM
    accepted_key_algorithms: frozenset[str] = ACCEPTED_KEY_ALGORITHMS
    system_known_hosts: Optional[Path] = None
    connections: list[dict[str, Any]] = field(default_factory=list)
    opened: int = 0
    sftp: StubSFTPClient = field(default_factory=StubSFTPClient)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace ``asyncssh.connect`` with this double for one test.

        Args:
            monkeypatch: The pytest patcher, which undoes this on
                teardown.

        Returns:
            None.
        """
        monkeypatch.setattr(asyncssh, 'connect', self.connect)

    @property
    def options(self) -> dict[str, Any]:
        """Return the options the most recent connection was opened with.

        Returns:
            The keyword arguments of the last ``connect`` call.

        Raises:
            AssertionError: If no connection was attempted. A test
                asserting on options that were never passed would pass
                against a client that never connected at all.
        """
        assert self.connections, 'no connection was attempted'
        return self.connections[-1]

    @property
    def identities(self) -> OfferedIdentities:
        """Return what the most recent connection actually offered.

        Returns:
            The keys and ssh-agent socket asyncssh resolves from the last
            call's options, as opposed to the literal ``client_keys``
            value :attr:`options` records.
        """
        return offered_identities(self.options)

    def connect(self, *args: Any, **kwargs: Any) -> Any:
        """Open, or refuse to open, an SSH connection.

        Args:
            args: Positional connection options. The client under test
                passes none; they are accepted and recorded as absent so
                that a client which started passing them is not silently
                ignored.
            kwargs: Host, credentials and host-key policy, recorded
                verbatim and then judged.

        Returns:
            An async context manager yielding an SSH connection, or one
            that raises the handshake failure these options earn.
        """
        self.connections.append(dict(kwargs))
        failure = self.verification_failure(kwargs)
        if failure is not None:
            return FailingTransport(failure)
        self.opened += 1
        return entered(StubSSHConnection(self.sftp))

    def verification_failure(
        self,
        options: Mapping[str, Any],
    ) -> Optional[asyncssh.Error]:
        """Return the handshake failure these options earn, if any.

        Models the three things asyncssh does with ``known_hosts``:
        ``None`` disables verification outright, an explicit value (a
        path, or the ``(host keys, CA keys, revoked keys)`` pinning
        tuple) is the trusted set, and an **absent** argument resolves
        the host's own ``~/.ssh/known_hosts``. The last of those is the
        one that matters most here: a double that treated an absent
        option as "no checking" would make the library's default look
        safe while proving nothing.

        Args:
            options: The keyword arguments the client passed.

        Returns:
            The ``asyncssh`` exception the handshake would raise, or None
            when the server is trusted.
        """
        if 'known_hosts' in options and options['known_hosts'] is None:
            return None

        if self.key_algorithm not in self.accepted_key_algorithms:
            offered = ','.join(sorted(self.accepted_key_algorithms))
            return asyncssh.KeyExchangeFailed(
                f'No matching host key algorithm found, sent {offered} '
                f'and received {self.key_algorithm}')

        if self.host_key not in self.trusted_keys(options):
            host = options.get('host', '')
            return asyncssh.HostKeyNotVerifiable(
                f'Host key is not trusted for host {host}')

        return None

    def trusted_keys(self, options: Mapping[str, Any]) -> Sequence[str]:
        """Return the host keys these connection options make trusted.

        Args:
            options: The keyword arguments the client passed.

        Returns:
            The trusted keys: the pinning tuple's first list, the
            contents of the named ``known_hosts`` file, or -- when the
            option is absent -- the host's own file, which is empty when
            there is none.
        """
        if 'known_hosts' not in options:
            return read_trusted_keys(self.system_known_hosts)

        known_hosts = options['known_hosts']
        if isinstance(known_hosts, tuple):
            return list(known_hosts[0])
        return read_trusted_keys(Path(known_hosts))

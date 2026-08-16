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

The operation surface (:class:`StubSFTPClient`) covers the modes R17
drives -- ``get``, ``put`` and ``remove`` -- and records each one with the
keyword options it was handed. Recording the *options* is what R17's M28
needs: the claim there is that a keyword is **absent**, and a log of
positional arguments alone cannot see a ``recurse=True`` that leaked in
from an earlier call sharing the same ``protocol_info``.

That surface is signature-faithful for the same reason ``client_keys`` is
resolved through the real options object: a double that accepts more than
the library does certifies calls the library refuses. Every operation
takes ``*args, **kwargs`` and :func:`bind_to_real_signature` binds them
against the **real** ``asyncssh.SFTPClient`` method before the call is
recorded, so the check is on the seam rather than restated per method --
which is what keeps it true for the wider mode set R21's allowlist
admits. ``remove`` is the case that made it necessary: it takes a path
and nothing else, so a ``recurse=True`` sent to it is a ``TypeError``
from the library, and a double that swallowed the keyword reported a
broken directory deletion as a completed one.

The double also models the one server rule that decides an R17 row:
``remove`` removes *a file or a symbolic link*, and asyncssh's directory
operation is the separate ``rmtree``, so a ``remove`` aimed at a
directory earns an ``SSH_FX_FAILURE`` from the server rather than a
success.

``lstat`` is configurable rather than fixed for the same reason
``client_keys`` is resolved through the real options object above: the
file type drives the whole directory branch, and asyncssh carries it in
the typed ``SFTPAttrs.type`` field, not in the ``str()`` fragment the
client used to parse. A test therefore hands the double a **real**
``SFTPAttrs`` -- including the two shapes that used to crash the parse --
rather than a mapping shaped like what the client hoped to find.
"""

import inspect
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

# One recorded SFTP operation: the method name, its positional arguments
# and the keyword options it was given. The options are recorded because
# M28's claim is about a keyword that must *not* be there.
SFTPCall = tuple[str, tuple[Any, ...], dict[str, Any]]

# What ``lstat`` reports for each kind of target. Real ``SFTPAttrs``, and
# the ``type`` field carries the answer: asyncssh fills it from the mode
# bits when it decodes an SFTPv3 reply and reads it off the wire from v4
# on, so it is the field a client is meant to branch on.
FILE_ATTRS = asyncssh.SFTPAttrs(
    type=asyncssh.FILEXFER_TYPE_REGULAR, size=12, permissions=0o100644)
DIRECTORY_ATTRS = asyncssh.SFTPAttrs(
    type=asyncssh.FILEXFER_TYPE_DIRECTORY, size=4096, permissions=0o40755)
SYMLINK_ATTRS = asyncssh.SFTPAttrs(
    type=asyncssh.FILEXFER_TYPE_SYMLINK, size=7, permissions=0o120777)

# The two attribute shapes that used to crash the client before it had
# transferred anything. ``SFTPAttrs()`` stringifies to the empty string,
# so the old ``str(...).split(',')`` produced one fragment with no ``':'``
# in it and the fragment's ``split(':')[1]`` raised ``IndexError``. The
# second stringifies to ``'size: 12, permissions: 100644'`` -- a parse
# that *succeeds* and yields no ``type`` key, so the lookup of that key
# raised ``KeyError``. Both are ordinary answers from a real server: the
# first is a server that sent no attributes, the second is any server at
# all, since ``SFTPAttrs.__str__`` omits an unknown file type.
UNPARSEABLE_ATTRS = asyncssh.SFTPAttrs()
TYPELESS_ATTRS = asyncssh.SFTPAttrs(size=12, permissions=0o100644)


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


def bind_to_real_signature(
    name: str,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> None:
    """Refuse a call the real ``asyncssh.SFTPClient`` would refuse.

    A stub whose methods take ``*args, **kwargs`` accepts every keyword
    ever offered to it, including the ones the library rejects, so a test
    driven through it can report a broken operation as a working one --
    which is what a ``recurse=True`` sent to ``remove`` did. Binding
    against the real method's signature moves that judgement back to the
    library: the double decides nothing, it only replays the arguments to
    the object that owns the rules.

    Args:
        name: The operation being invoked, which must be a real method of
            ``asyncssh.SFTPClient``.
        args: The positional arguments the client passed.
        kwargs: The keyword arguments the client passed.

    Returns:
        None, when the real method would have accepted this call.

    Raises:
        AttributeError: If ``name`` is not a method of the real client at
            all, which no ``getattr`` in the client under test could have
            resolved either.
        TypeError: If the real method would refuse these arguments -- an
            unexpected keyword, a missing or surplus positional. Raised
            from the same call the client made, so it lands exactly where
            the library would have raised it.
    """
    method = getattr(asyncssh.SFTPClient, name)
    # `None` stands in for `self`: the signature is read off the class,
    # so the bound receiver is still a parameter of it.
    inspect.signature(method).bind(None, *args, **kwargs)


class StubSFTPClient:
    """The slice of ``asyncssh``'s SFTP client an SFTP session uses.

    Records every operation it was asked to perform, with the keyword
    options it was handed, so a test can assert what actually happened on
    the remote side -- that a destructive operation ran exactly once, and
    that no option leaked into it from an earlier call. Every operation is
    bound against the real method's signature first, so an argument the
    library would refuse is refused here too.
    """

    def __init__(
        self,
        *,
        attrs: asyncssh.SFTPAttrs = FILE_ATTRS,
        entries: Sequence[str] = ('f',),
        lstat_error: Optional[BaseException] = None,
        operation_error: Optional[BaseException] = None,
    ) -> None:
        """Build an SFTP client double.

        Args:
            attrs: What :meth:`lstat` reports for the target. The file
                type in it is what selects the directory branch, so this
                is the one knob a test turns to cover both sides of it.
            entries: What :meth:`listdir` reports. The default is one
                file; the empty tuple is the empty-directory edge case,
                which must stay distinguishable from "not a directory".
            lstat_error: Raised by :meth:`lstat`, so a test can drive a
                failure that happens *before* the transfer. Raised
                unwrapped, unlike the operation below, which the
                resilience layer wraps -- and the two travel different
                branches of the error classifier.
            operation_error: Raised by whichever operation is invoked, so
                a test can drive a failure during the transfer or the
                mutation itself.
        """
        self.calls: list[SFTPCall] = []
        self.attrs = attrs
        self.entries = entries
        self.lstat_error = lstat_error
        self.operation_error = operation_error

    def names(self) -> list[str]:
        """Return the operations invoked, in order.

        Returns:
            One name per recorded call, so a test that cares only about
            *which* operations ran -- and how many times -- does not have
            to restate their arguments to say so.
        """
        return [name for name, _, _ in self.calls]

    async def _invoked(
        self,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """Record one operation and fail if configured to.

        The call is bound against the real signature *before* it is
        recorded, because a call the library refuses never reaches the
        server: recording it would leave a test able to assert that an
        impossible operation was issued.

        Args:
            name: The operation the client called.
            args: The positional arguments it passed.
            kwargs: The transfer options it passed.

        Returns:
            None.

        Raises:
            TypeError: If the real ``asyncssh.SFTPClient`` method would
                refuse these arguments. See :func:`bind_to_real_signature`.
            BaseException: ``operation_error``, when one was configured.
        """
        bind_to_real_signature(name, args, kwargs)
        self.calls.append((name, args, kwargs))
        if self.operation_error is not None:
            raise self.operation_error

    async def lstat(self, path: str) -> asyncssh.SFTPAttrs:
        """Return the attributes ``asyncssh`` reports for a remote path.

        A real ``asyncssh.SFTPAttrs`` is returned rather than a stand-in,
        so a client reading it is held to the type the library actually
        hands back -- which is the whole of M4: the field is typed and
        the client used to go looking for it in a ``str()``.

        Args:
            path: The remote path being described.

        Returns:
            The configured attributes.

        Raises:
            BaseException: ``lstat_error``, when one was configured.
        """
        self.calls.append(('lstat', (path,), {}))
        if self.lstat_error is not None:
            raise self.lstat_error
        return self.attrs

    async def listdir(self, path: str) -> list[str]:
        """Return the entries ``asyncssh`` reports for a remote directory.

        Args:
            path: The remote directory being listed.

        Returns:
            The configured entries.
        """
        self.calls.append(('listdir', (path,), {}))
        return list(self.entries)

    async def get(self, *args: Any, **kwargs: Any) -> None:
        """Accept a download and report success by not raising.

        Args:
            args: The remote and local paths.
            kwargs: Transfer options such as ``recurse``.

        Returns:
            None, as ``asyncssh`` does.
        """
        await self._invoked('get', args, kwargs)

    async def put(self, *args: Any, **kwargs: Any) -> None:
        """Accept an upload and report success by not raising.

        Args:
            args: The local and remote paths.
            kwargs: Transfer options such as ``recurse``.

        Returns:
            None, as ``asyncssh`` does.
        """
        await self._invoked('put', args, kwargs)

    async def remove(self, *args: Any, **kwargs: Any) -> None:
        """Delete a remote file, or refuse a directory the way a server does.

        ``asyncssh.SFTPClient.remove`` removes *a file or a symbolic
        link* -- the directory operation is the separate ``rmtree`` -- so
        a server answers a ``remove`` aimed at a directory with
        ``SSH_FX_FAILURE``. Modelling that here is what makes R17-AC1's
        sixth row report an honest typed failure rather than a success
        the real remote side would never have granted.

        Args:
            args: The remote path being removed.
            kwargs: Options, of which the real method accepts none.

        Returns:
            None, as ``asyncssh`` does, for a file or a symlink.

        Raises:
            asyncssh.SFTPFailure: When the target is a directory.
            BaseException: ``operation_error``, when one was configured.
        """
        await self._invoked('remove', args, kwargs)
        if self.attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
            raise asyncssh.SFTPFailure(
                'the remove target is a directory, not a file')


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

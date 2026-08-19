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
import logging
import os
import posixpath
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

# `_begin_copy`'s positional options after `copy_type`/`expand_glob`,
# in its own order, and what each is when the caller named nothing.
# Read off the real `get` signature rather than restated, so a release
# that reorders or renames one is a loud failure here rather than a
# silent misattribution in a recorded call.
COPY_OPTION_NAMES: tuple[str, ...] = (
    'preserve',
    'recurse',
    'follow_symlinks',
    'sparse',
    'block_size',
    'max_requests',
    'progress_handler',
    'error_handler',
)
COPY_OPTION_DEFAULTS: Mapping[str, Any] = {
    name: inspect.signature(
        asyncssh.SFTPClient.get).parameters[name].default
    for name in COPY_OPTION_NAMES
}

# One remote tree a hostile server can present, keyed by byte path:
# `{path: (attrs, [(entry name, attrs), ...], contents)}`. Entry names
# are what `scandir` yields, and nothing constrains them -- which is
# the whole point: a name containing `../` is exactly what R22's
# Description says a server may emit.
RemoteTree = Mapping[bytes, tuple[asyncssh.SFTPAttrs,
                                  Sequence[tuple[bytes, asyncssh.SFTPAttrs]],
                                  bytes]]


def _normalised(options: Sequence[Any]) -> list[Any]:
    """Resolve the ``<= 0`` sentinels the real ``_begin_copy`` resolves.

    ``block_size`` and ``max_requests`` default to ``-1``, meaning "pick
    one from the transports' limits", and it is the real
    ``_begin_copy`` -- not ``_copy`` -- that does the picking. This
    double replaces ``_begin_copy``, so it inherits that job: passing
    ``-1`` straight through makes the copier ask for a zero-length
    range and write an **empty file**, which would quietly turn every
    download assertion into a vacuous one.

    Args:
        options: ``_begin_copy``'s positional options, in its own order.

    Returns:
        The same options with the two sentinels resolved.
    """
    resolved = list(options)
    block = COPY_OPTION_NAMES.index('block_size')
    requests = COPY_OPTION_NAMES.index('max_requests')
    if resolved[block] <= 0:
        resolved[block] = asyncssh.sftp.MAX_SFTP_READ_LEN
    if resolved[requests] <= 0:
        resolved[requests] = 16
    return resolved


def remote_file(contents: bytes) -> asyncssh.SFTPAttrs:
    """Return the attributes a server reports for a file of some size.

    Args:
        contents: The bytes the file holds.

    Returns:
        Real ``SFTPAttrs``, so ``_copy`` branches on the same typed
        field it branches on in production.
    """
    return asyncssh.SFTPAttrs(
        type=asyncssh.FILEXFER_TYPE_REGULAR,
        size=len(contents),
        permissions=0o100644)


#: The remote directory the tree below is rooted at.
REMOTE_TREE_ROOT: str = '/pub/data'

#: Payloads distinguishable on sight, because every defect these
#: witness is "the right operation moved the wrong bytes". AGW-33's
#: killer assertion compares the first two, so they must never be equal.
LOCAL_CONTENT: bytes = b'the file the caller meant'
REMOTE_CONTENT: bytes = b'the old remote copy'
HARMLESS_CONTENT: bytes = b'an ordinary file from an ordinary server'
ESCAPED_CONTENT: bytes = b'written outside the directory you named'


def hostile_tree(
    *,
    escape: Optional[str] = None,
    symlink: Optional[str] = None,
) -> RemoteTree:
    """Build the tree a server presents to a recursive download.

    Always carries one harmless file, so a test can tell "the transfer
    ran and the escape was refused" from "nothing happened at all".

    Args:
        escape: An entry name to add verbatim, typically containing
            ``../``. asyncssh's ``_copy`` filters a name that is ``.``
            or ``..`` *exactly* and then ``posixpath.join``s it, so a
            name that merely **contains** a separator composes straight
            through -- which is the defect. None omits the row.
        symlink: A link target the server claims for an entry named
            ``link``, or None for no link at all.

    Returns:
        The tree, keyed by byte path.
    """
    root = os.fsencode(REMOTE_TREE_ROOT)
    harmless = posixpath.join(root, b'harmless.txt')
    entries: list[tuple[bytes, asyncssh.SFTPAttrs]] = [
        (b'harmless.txt', remote_file(HARMLESS_CONTENT)),
    ]
    tree: dict[bytes, Any] = {
        root: (DIRECTORY_ATTRS, entries, b''),
        harmless: (remote_file(HARMLESS_CONTENT), [], HARMLESS_CONTENT),
    }
    if escape is not None:
        name = os.fsencode(escape)
        entries.append((name, remote_file(ESCAPED_CONTENT)))
        tree[posixpath.join(root, name)] = (
            remote_file(ESCAPED_CONTENT), [], ESCAPED_CONTENT)
    if symlink is not None:
        entries.append((b'link', SYMLINK_ATTRS))
        tree[posixpath.join(root, b'link')] = (
            SYMLINK_ATTRS, [], os.fsencode(symlink))
    return tree


class RemoteTreeFS:
    """The remote half of a transfer: whatever a server chooses to say.

    Implements the small structural filesystem protocol asyncssh's
    ``_copy`` calls on its *source*, backed by a dict instead of a
    socket. Only the wire is faked -- every decision about what the
    entry names mean, and where they compose to, stays asyncssh's.

    Attributes:
        tree: The directory structure this server presents.
    """

    limits = asyncssh.sftp.SFTPLimits(0, 32768, 32768, 0)

    def __init__(self, tree: RemoteTree) -> None:
        """Hold the tree this server answers from.

        Args:
            tree: The structure to present.
        """
        self.tree = tree

    def encode(self, path: Any) -> bytes:
        """Encode a path the way asyncssh's remote side encodes one.

        Args:
            path: The path to encode.

        Returns:
            The encoded path.
        """
        return path if isinstance(path, bytes) else os.fsencode(str(path))

    @staticmethod
    def basename(path: bytes) -> bytes:
        """Return the final component of a remote path.

        Args:
            path: The path to take the basename of.

        Returns:
            Its final component.
        """
        return posixpath.basename(path)

    def compose_path(
        self,
        path: bytes,
        parent: Optional[bytes] = None,
    ) -> bytes:
        """Join a name onto a remote parent directory.

        Args:
            path: The name to join.
            parent: The directory to join it onto, or None.

        Returns:
            The composed path.
        """
        path = self.encode(path)
        return posixpath.join(parent, path) if parent else path

    async def stat(
        self,
        path: bytes,
        *,
        follow_symlinks: bool = True,
    ) -> asyncssh.SFTPAttrs:
        """Return what this server says about a path.

        Args:
            path: The path to describe.
            follow_symlinks: Unused; this server presents no links.

        Returns:
            The configured attributes.
        """
        return self.tree[path][0]

    async def isdir(self, path: bytes) -> bool:
        """Report whether this server calls a path a directory.

        Args:
            path: The path to test.

        Returns:
            True when it is a directory.
        """
        return (self.tree[path][0].type
                == asyncssh.FILEXFER_TYPE_DIRECTORY)

    async def scandir(self, path: bytes) -> Any:
        """Yield the entries this server claims a directory holds.

        Args:
            path: The directory to scan.

        Yields:
            One ``SFTPName`` per configured entry, name verbatim --
            including one containing ``../``, which is what a hostile
            server sends and what ``_copy`` must not compose through.
        """
        for name, attrs in self.tree[path][1]:
            yield asyncssh.sftp.SFTPName(name, attrs=attrs)

    async def readlink(self, path: bytes) -> bytes:
        """Return the link target this server claims for a path.

        Args:
            path: The remote link.

        Returns:
            The target string, verbatim -- the server chooses it, and
            nothing constrains where it points.
        """
        return self.tree[path][2]

    async def open(
        self,
        path: bytes,
        mode: str,
        block_size: int = -1,
    ) -> Any:
        """Open a remote file for reading.

        Args:
            path: The remote path.
            mode: The mode asyncssh asked for, unused.
            block_size: Unused.

        Returns:
            A reader over the configured contents.
        """
        return _RemoteFile(self.tree[path][2])


class _RemoteFile:
    """One remote file being read, as asyncssh's copier expects it."""

    def __init__(self, data: bytes) -> None:
        """Hold the bytes this file yields.

        Args:
            data: The file's contents.
        """
        self.data = data

    def request_ranges(self, offset: int, length: int) -> Any:
        """Return the ranges holding data, as one whole range.

        Args:
            offset: Where to start.
            length: How much to cover.

        Returns:
            An async iterator over the single range.
        """
        async def ranges() -> Any:
            """Yield the one range this file has.

            Yields:
                The ``(offset, length)`` pair.
            """
            yield offset, length

        return ranges()

    async def read(self, size: int, offset: int) -> Optional[bytes]:
        """Read from the file.

        Args:
            size: How many bytes to read.
            offset: Where to read from.

        Returns:
            The bytes, or None at end of file.
        """
        return self.data[offset:offset + size] or None

    async def close(self) -> None:
        """Close the file.

        Returns:
            None.
        """


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

    Attributes:
        version: The SFTP protocol version ``_copy`` reads off its
            client to choose an error class.
        supports_remote_copy: Read by ``_copy`` on the remote-to-remote
            path, which no download takes.
        logger: Where ``_copy`` writes its progress lines.
    """

    version = 3
    supports_remote_copy = False
    logger = logging.getLogger('tests.fixtures.sftp')

    #: asyncssh's own recursion, bound to this double. ``_copy`` calls
    #: ``self._copy`` for each subdirectory, so it has to be reachable
    #: as an attribute here and not merely invoked once from outside --
    #: otherwise only the top level of a tree is ever real, which is the
    #: one level a traversal test does not care about.
    _copy = asyncssh.SFTPClient._copy

    def __init__(
        self,
        *,
        absent_paths: Sequence[str] = (),
        attrs: asyncssh.SFTPAttrs = FILE_ATTRS,
        entries: Sequence[str] = ('f',),
        lstat_error: Optional[BaseException] = None,
        operation_error: Optional[BaseException] = None,
        remote_tree: Optional[RemoteTree] = None,
    ) -> None:
        """Build an SFTP client double.

        Args:
            absent_paths: Paths this server does **not** have, for which
                :meth:`lstat` raises ``SFTPNoSuchFile`` the way a real
                server does. Empty by default, which is the historical
                behaviour -- and that default is what hid AGW-40: a
                double answering ``lstat`` with attributes for *every*
                path reports an upload's not-yet-created destination as
                already present, so the client's pre-flight ``lstat``
                always succeeded here and failed against every real
                server. A knob rather than a new default, because the
                existing rows are about what happens once the target is
                known to exist.
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
            remote_tree: What a server presents to a *recursive
                download*. When given, :meth:`_begin_copy` runs
                asyncssh's real ``_copy`` over it instead of only
                recording the call, so bytes actually land on disk and a
                containment claim can be asserted against where they
                landed. None -- the default -- keeps every existing row
                recording-only.
        """
        self.calls: list[SFTPCall] = []
        self.absent_paths = set(absent_paths)
        self.attrs = attrs
        self.entries = entries
        self.lstat_error = lstat_error
        self.operation_error = operation_error
        self.remote_tree = remote_tree

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
            SFTPNoSuchFile: When ``path`` is in ``absent_paths``, which
                is what a real server answers for a path it does not
                have.
        """
        self.calls.append(('lstat', (path,), {}))
        if self.lstat_error is not None:
            raise self.lstat_error
        if path in self.absent_paths:
            raise asyncssh.SFTPNoSuchFile('No such file')
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

    async def _begin_copy(
        self,
        srcfs: Any,
        dstfs: Any,
        srcpaths: Any,
        dstpath: Any,
        copy_type: str,
        expand_glob: bool,
        *options: Any,
    ) -> None:
        """Record a download and run its real local-side effects.

        The private seam local-download containment is installed at
        (R22/AGW-18). It is doubled rather than the public ``get``
        because the client now reaches this method for a download -- but
        *recording* alone would prove nothing about containment, which
        is a claim about where bytes land, not about which arguments
        were passed.

        So when the test supplies a :attr:`remote_tree`, this delegates
        to **asyncssh's own** ``_copy`` with the ``dstfs`` the client
        chose. That is the real recursion, the real ``posixpath.join``
        filename arithmetic, and the real filter that drops only an
        exactly-``.``/``..`` entry name -- so a server-supplied name
        that *contains* a separator composes through exactly as it does
        in production, and whether it escapes is decided by the object
        under test rather than by this double.

        Args:
            srcfs: The source filesystem; the client passes itself.
            dstfs: The destination filesystem -- the object whose
                containment is under test.
            srcpaths: The remote source operand.
            dstpath: The local destination operand.
            copy_type: ``'get'`` or ``'mget'``.
            expand_glob: Whether the source is a glob pattern.
            options: ``_begin_copy``'s remaining positional options, in
                its own order.

        Returns:
            None.

        Raises:
            BaseException: ``operation_error``, when one was configured.
        """
        await self._invoked(copy_type, (srcpaths, dstpath), {
            name: value
            for name, value in zip(COPY_OPTION_NAMES, options)
            if value != COPY_OPTION_DEFAULTS[name]
        })
        if self.remote_tree is None:
            return
        source = os.fsencode(srcpaths)
        await self._copy(
            RemoteTreeFS(self.remote_tree),
            dstfs,
            source,
            os.fsencode(dstpath),
            self.remote_tree[source][0],
            *_normalised(options),
            False,
        )

    async def put(self, *args: Any, **kwargs: Any) -> None:
        """Accept an upload and report success by not raising.

        The destination stops being absent, because on a real server an
        upload *creates* it. Without this the double contradicts itself
        the moment a test uses ``absent_paths`` for an upload: the
        transfer succeeds and the very next ``lstat`` of the path it just
        wrote still says the file is not there.

        Args:
            args: The local and remote paths.
            kwargs: Transfer options such as ``recurse``.

        Returns:
            None, as ``asyncssh`` does.
        """
        await self._invoked('put', args, kwargs)
        if len(args) > 1:
            self.absent_paths -= {args[1]}

    async def mput(self, *args: Any, **kwargs: Any) -> None:
        """Accept a glob upload and report success by not raising.

        Present because AGW-33's operand-order table has to cover the
        mode R21's allowlist (S19) is about to admit, not only the two
        dispatched today: ``mput`` carries the identical transposition,
        and a table proven over ``put`` alone would leave it live on the
        day it becomes reachable.

        Args:
            args: The local and remote paths.
            kwargs: Transfer options such as ``recurse``.

        Returns:
            None, as ``asyncssh`` does.
        """
        await self._invoked('mput', args, kwargs)

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


class TransferringSFTPClient(StubSFTPClient):
    """A client that actually *moves bytes*, for the operand-order rows.

    AGW-33 transposes two path-like positionals, and neither this
    module's signature-faithful binding nor a recording double can see
    that: both operands bind cleanly either way round. Only a test with
    a modelled filesystem can, by asserting which path was **read** and
    which was **written**.

    So ``put`` here reads its source operand off the real disk, the way
    ``asyncssh.SFTPClient.put`` does, and keeps what it read.

    Attributes:
        uploaded: The bytes the server received, or None.
        upload_destination: The remote path they were written to.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Build a transferring client double.

        Args:
            kwargs: Forwarded to :class:`StubSFTPClient`.
        """
        super().__init__(**kwargs)
        self.uploaded: Optional[bytes] = None
        self.upload_destination: Optional[str] = None

    async def put(self, *args: Any, **kwargs: Any) -> None:
        """Read the local source and record what reached the server.

        Args:
            args: ``(localpaths, remotepath)`` in asyncssh's order for
                this verb -- source is **local**.
            kwargs: Transfer options.

        Returns:
            None, as ``asyncssh`` does.

        Raises:
            FileNotFoundError: If the source operand names nothing
                locally, which is what a real ``put`` does and is the
                honest-failure half of AGW-33.
        """
        await self._invoked('put', args, kwargs)
        self.uploaded = Path(args[0]).read_bytes()
        self.upload_destination = str(args[1])


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

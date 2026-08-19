"""Path containment and safe local file handling (R22).

Three findings share one root: nothing in this library ever decided
*where* a local write was allowed to land. ``download_filepath``,
``local_filepath``, ``client_path`` and ``local_path`` reached ``open()``
or a transfer client verbatim, and on a recursive directory download the
**remote server** supplies the entry names -- so a hostile server could
emit ``../`` and write outside the directory the caller named (M17). The
writes themselves used no ``O_EXCL``, no ``O_NOFOLLOW`` and no mode
restriction, so a symlink pre-created at a predictable path was followed
(M18). And the documented cleanup step raised ``FileNotFoundError`` when
the file was already gone, while a failed download left its partial
bytes behind (M19).

Three primitives replace that, and the order of operations in each is
the security property rather than an implementation detail.

**:func:`resolve_within` canonicalises before it checks.** A containment
test applied to a path that still contains ``..`` or a symlinked
component is testing a string, not a location: ``base/sub/../../evil``
starts with ``base`` and is not inside it. So the candidate's *parent*
is resolved -- symlinks followed, all the way -- and the containment
question is asked of the answer.

**The final component is returned verbatim, unresolved.** Resolving it
too would be strictly worse: a symlink at the target path would be
replaced by whatever it pointed at, the containment check would be
applied to the *target*, and a link pointing back inside ``base`` would
then be opened and followed. Leaving the last component alone is what
leaves something for ``O_NOFOLLOW`` to refuse.

**The refusal lives in the ``os.open`` flags, not in a check before
them.** A pre-write ``lstat`` that likes what it sees and then opens the
path is two syscalls with a window between them, and the window is the
attack: the symlink is planted after the check and before the open.
``O_NOFOLLOW`` and ``O_EXCL`` are evaluated by the kernel as part of the
same operation that creates the file, so there is no window at all.
:func:`safe_writer` therefore *classifies* a refusal after the fact
(EEXIST on a symlink reads as containment, on a plain file as
configuration) but never *decides* one before it.

Platform note (R22's last criterion). ``O_NOFOLLOW`` is POSIX and is
absent on some platforms. Where :data:`O_NOFOLLOW` is 0 the guarded
opener degrades to an explicit ``lstat`` symlink check immediately
before the ``os.open``, which is the two-syscall shape above and carries
its window -- an explicit, documented weakening rather than a silent
one. :func:`guarded_opener` takes the flag as an argument precisely so
that the degraded branch is reachable, and covered, on a platform that
has the flag.

Three further findings (N2, N3, N4) shared a single root, and are
answered by a single change rather than by three patches.

**The root: a path was validated as a string and opened later by that
string.** ``resolve_within`` answers a *name*, and every guarantee it
established -- this parent is inside the base, no component of it is a
symbolic link -- describes the filesystem at the instant it looked.
Handing that name to ``os.open`` asks the kernel to walk every component
again, from scratch, against whatever the filesystem says *now*. The
leaf was protected by ``O_NOFOLLOW``; nothing protected the directory
components above it, and a hostile local process that swaps one between
the two walks redirects the write. Measured on this library before the
fix: with a thread flipping one directory component between an inner
directory and an outside one, 3000 resolutions put 2 files outside the
base, and the same run with a realistic scheduling gap between the
resolve and the open put 7 of 2000 outside. Crucially the containment
*answer* was never wrong (0 of 5000 resolutions returned an escaping
path) -- so no amount of re-checking the string could have caught it,
and only ceasing to re-walk it can.

**So the write walks file descriptors, not names.** :func:`open_within`
opens the containment base once, then opens each component relative to
the descriptor it already holds -- ``O_NOFOLLOW | O_DIRECTORY``, with
``dir_fd=`` -- and finally creates the leaf with ``dir_fd`` too. A
descriptor names an inode, not a path: once a component is open, no
rename or symlink swap can substitute a different directory for it,
because the next step never consults the name again. This is what makes
the walk *atomic with respect to the attacker* even though it is many
syscalls; the check-then-use pair that N2 exploits no longer exists,
because there is no second lookup to poison. ``os.path.realpath`` with
``strict=True`` was rejected as the fix: it re-derives a *string*, which
is precisely the artefact that goes stale.

**The leaf is judged through the descriptor that was actually opened.**
``fstat`` on the returned fd -- never a ``stat`` on the path -- answers
both remaining findings on the object the bytes will really reach:

* **N3, non-regular targets.** Without ``S_ISREG`` a target that is a
  FIFO hung ``request()`` permanently: the blocking ``open`` sits in a
  threadpool worker where the caller's deadline cannot reach it, so a
  3-second timeout never returned. ``/dev/null`` reported ``ok=True``
  with the body silently discarded. Both are refused, and the refusal
  cannot itself hang, because the open carries ``O_NONBLOCK``. That
  flag is necessary but *not sufficient* on its own: a FIFO with a
  reader already attached opens instantly and only ``fstat`` reveals
  what it is. The flag is cleared from the descriptor immediately
  afterwards, so the ordinary regular-file write keeps blocking
  semantics and a short write cannot silently drop bytes.
* **N4, hardlinks.** ``is_symlink()`` is False for a hardlink, so a
  hardlinked target was written through and overwrote the file it
  shared an inode with. ``st_nlink > 1`` on the opened descriptor is
  refused. This is deliberately checked *after* the open rather than
  before, for the same reason as everything else here: a link created
  between a check and the open would be missed, while the descriptor
  cannot lie about the inode it refers to.

**What ``overwrite=True`` means, decided deliberately.** It means "you
may replace the file at this path", and it does **not** mean "you may
write through it to somewhere else". A hardlinked target is refused
whether or not overwriting was requested, because the caller who asked
to replace their own download did not thereby ask to overwrite an
unrelated file that happens to share the inode -- and the second file's
owner never asked for anything at all. This matches the existing
treatment of symbolic links, which ``overwrite=True`` has never
unlocked; the two are the same escape wearing different metadata.
"""

import asyncio
import errno
import fcntl
import os
import stat
from contextlib import asynccontextmanager, suppress
from pathlib import Path, PurePath
from typing import AsyncIterator, Callable, Final, Union

import aiofiles
import aiofiles.os
from aiofiles.threadpool.binary import AsyncBufferedIOBase

from asyncio_gateway.utils.exceptions import (
    AsyncGatewayError,
    ConfigurationError,
    LocalWriteError,
    PathContainmentError,
)

#: Anything path-like this module accepts from a caller or from a remote
#: listing. ``PurePath`` rather than ``Path`` because a name that came
#: off the wire is a string to be judged, not a location to be touched.
PathLike = Union[str, PurePath]

#: The same, plus ``bytes``. ``asyncssh``'s filesystem protocol speaks
#: bytes throughout -- ``scandir`` yields byte filenames and ``_copy``
#: joins them with ``posixpath.join`` -- so the containment seam facing
#: it has to accept them.
BytesOrPathLike = Union[bytes, str, PurePath]

#: The mode every file this library creates is opened with: readable and
#: writable by its owner and by nobody else. M18's "no mode restriction"
#: meant a download landed at whatever ``umask`` allowed, which on the
#: shared hosts the README's ``/tmp`` examples describe is world
#: readable.
FILE_MODE: Final[int] = 0o600

#: ``O_NOFOLLOW`` where the platform has it, else 0 -- which is both the
#: "add nothing to the flags" identity and the falsy value
#: :func:`guarded_opener` branches on to select the degraded check.
O_NOFOLLOW: Final[int] = getattr(os, 'O_NOFOLLOW', 0)

#: The errnos a kernel answers an ``O_NOFOLLOW`` open of a symbolic link
#: with. POSIX specifies ``ELOOP``; several BSD-derived kernels answer
#: ``EMLINK`` for this case instead, and macOS is one of them.
SYMLINK_ERRNOS: Final[frozenset[int]] = frozenset(
    code for code in (
        getattr(errno, 'ELOOP', None),
        getattr(errno, 'EMLINK', None),
    ) if code is not None
)

#: The errnos an ``O_NONBLOCK`` open answers with when the target is not
#: something a regular write can land on. ``ENXIO`` is the POSIX answer
#: for a FIFO opened for writing with no reader attached, and is also
#: what Linux answers for a bound unix socket; macOS answers
#: ``EOPNOTSUPP`` for the socket instead. Both mean the same thing here,
#: and a set spanning them is what keeps the refusal identical on both
#: platforms rather than letting one of them escape as a bare ``OSError``.
NOT_A_REGULAR_FILE_ERRNOS: Final[frozenset[int]] = frozenset(
    code for code in (
        getattr(errno, 'ENXIO', None),
        getattr(errno, 'EOPNOTSUPP', None),
    ) if code is not None
)

#: ``O_DIRECTORY`` where the platform has it, else 0. Used on every
#: intermediate step of the descriptor walk so that a component which
#: has become a *file* between two steps is refused by the kernel rather
#: than opened and then discovered.
O_DIRECTORY: Final[int] = getattr(os, 'O_DIRECTORY', 0)

#: ``O_NONBLOCK`` where the platform has it, else 0. This is what stops
#: the N3 refusal from being unable to happen: opening a FIFO for
#: writing blocks until a reader attaches, and the blocked call sits in
#: a threadpool worker where the caller's timeout cannot reach it, so
#: the check that would have refused the FIFO never gets to run. Cleared
#: from the descriptor by :func:`_restore_blocking` once the open has
#: answered.
O_NONBLOCK: Final[int] = getattr(os, 'O_NONBLOCK', 0)

#: ``O_CLOEXEC`` where the platform has it, else 0. The walk holds
#: directory descriptors briefly; leaking them into a child process
#: would hand that child a handle to a directory inside the containment
#: base, which is the opposite of this module's purpose.
O_CLOEXEC: Final[int] = getattr(os, 'O_CLOEXEC', 0)

#: The flags each intermediate directory of the walk is opened with.
#: ``O_RDONLY`` because nothing is written *to* a directory here -- it is
#: opened only to be the anchor the next component is resolved against.
DIRECTORY_FLAGS: Final[int] = (
    os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)


def _walk_to_parent(base_fd: int, parts: tuple[str, ...]) -> int:
    """Descend ``parts`` from ``base_fd``, returning a descriptor.

    The heart of the N2 fix, and the reason it is a *loop over
    descriptors* rather than a single ``os.open`` of a joined path. Each
    step resolves exactly one component against a directory this process
    already holds open, so the only thing an attacker can substitute is
    a component that has not been reached yet -- and reaching it goes
    through the same ``O_NOFOLLOW`` refusal. Once a step succeeds, the
    directory it opened is pinned by its inode and cannot be swapped for
    another.

    Ownership: this closes every descriptor it opens except the one it
    returns, including on failure. The caller owns the returned one.

    Args:
        base_fd: An open descriptor for the directory to descend from.
            Borrowed, not consumed -- the caller still closes it.
        parts: The path components to descend, in order. Already known
            to contain no ``.``, ``..`` or separator, because
            :func:`resolve_within` canonicalised them away.

    Returns:
        An open descriptor for the final directory in ``parts``, or a
        duplicate of ``base_fd`` when ``parts`` is empty.

    Raises:
        OSError: As ``os.open`` does. ``ELOOP``/``EMLINK`` when a
            component is a symbolic link, ``ENOTDIR`` when it is not a
            directory, ``ENOENT`` when it is not there.
    """
    current = os.dup(base_fd)
    try:
        for part in parts:
            following = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = following
    except BaseException:
        os.close(current)
        raise
    return current


def _restore_blocking(descriptor: int, nonblock: int = O_NONBLOCK) -> None:
    """Clear ``O_NONBLOCK`` from a descriptor the walk just opened.

    The flag is on the open only so that a FIFO cannot wedge the call
    before :func:`_reject_irregular` gets to refuse it. Leaving it set
    afterwards would change what a *regular* file's writes mean: a
    non-blocking write may transfer fewer bytes than it was given, and
    the callers here stream a body chunk by chunk, so a silently short
    write is a silently truncated download. Regular files ignore the
    flag on most kernels, which is exactly why it must not be relied on
    -- ``most`` is not ``all``.

    Args:
        descriptor: The open file descriptor to restore.
        nonblock: The ``O_NONBLOCK`` bit, or 0 on a platform without it,
            where there is nothing to clear. A parameter for the same
            reason ``nofollow`` is one on :func:`guarded_opener`: a
            platform branch that no test on this platform can execute
            carries no coverage, and an unexecuted branch is an
            unverified claim.

    Returns:
        None.
    """
    if not nonblock:
        return
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    fcntl.fcntl(descriptor, fcntl.F_SETFL, flags & ~nonblock)


def _reject_irregular(descriptor: int, path: PathLike) -> None:
    """Refuse a freshly-opened descriptor that is not a plain file.

    Answers N3 and N4 together, because they are one question asked of
    one ``fstat``: *is the thing I have actually opened a private,
    ordinary file?* Asked of the descriptor rather than of the path,
    which is what makes it unraceable -- the path could name something
    else by now, the descriptor cannot.

    Args:
        descriptor: The open descriptor to judge.
        path: The path it was opened from, for the message only.

    Returns:
        None, when the descriptor is a regular file with one link.

    Raises:
        PathContainmentError: When it is not a regular file (a FIFO, a
            device, a socket), or when its link count shows the inode is
            reachable under another name.
    """
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise PathContainmentError(
            f'refusing to write to {str(path)!r}, which is a '
            f'{_describe(info.st_mode)} rather than a regular file')
    if info.st_nlink > 1:
        raise PathContainmentError(
            f'refusing to write to {str(path)!r}, which is a hard link '
            f'to a file also reachable under {info.st_nlink - 1} other '
            f'name(s); writing it would modify them too')


def _describe(mode: int) -> str:
    """Name the kind of filesystem object a stat mode describes.

    A refusal that says "not a regular file" leaves the reader to go and
    find out what it *was*; naming it is the difference between a
    diagnosable error and a puzzle. Only the kinds a write can actually
    land on are enumerated.

    Args:
        mode: The ``st_mode`` from a stat result.

    Returns:
        A short human-readable name for the object kind.
    """
    for predicate, name in (
        (stat.S_ISFIFO, 'named pipe'),
        (stat.S_ISCHR, 'character device'),
        (stat.S_ISBLK, 'block device'),
        (stat.S_ISSOCK, 'socket'),
        (stat.S_ISDIR, 'directory'),
        (stat.S_ISLNK, 'symbolic link'),
    ):
        if predicate(mode):
            return name
    return 'special file'


def _describe_at(parent_fd: int, leaf: str) -> str:
    """Name what sits at ``leaf``, asked relative to an open directory.

    The counterpart to :func:`_describe` for the one refusal that has no
    descriptor to ``fstat``: when the open itself fails there is nothing
    to inspect but the name. That makes this an ``lstat`` on a path, and
    so in principle raceable -- but only in the direction of a *wrong
    noun in an error message*, because the open has already been refused
    and no write can follow. Naming the kind is worth that, and getting
    it wrong is what a hardcoded "named pipe" did on Linux, where a
    bound unix socket earns the same ``ENXIO`` a reader-less FIFO does.

    Args:
        parent_fd: The open descriptor for the containing directory.
        leaf: The final component to describe.

    Returns:
        A short human-readable name for the object kind, falling back to
        the generic phrase when the entry cannot be stat'ed at all.
    """
    try:
        return _describe(os.lstat(leaf, dir_fd=parent_fd).st_mode)
    except OSError:
        return 'named pipe or unconnected device'


def _names_a_directory(relative: PurePath) -> bool:
    """Report whether a candidate names a directory rather than a file.

    R22's "an entry name that is empty or ``.``/``..`` exactly" edge
    case. Such a candidate has no final component to hold back from the
    canonicalisation: there is no file to open, so there is nothing for
    ``O_NOFOLLOW`` to protect, and holding one back would ask the
    containment question of the wrong directory. ``pathlib`` drops a
    ``.`` while parsing, so ``PurePath('.')`` and ``PurePath('')`` both
    have **no parts at all** -- which is why this reads the parts rather
    than the ``name`` (``(base / '.').name`` is the base's own name, and
    its parent is the directory *above* the base).

    Args:
        relative: The candidate, already parsed and known relative.

    Returns:
        True when the candidate is empty, ``.``, or ends in ``..``.
    """
    parts = relative.parts
    return not parts or parts[-1] == '..'


def _contained(base: Path, resolved: Path) -> bool:
    """Report whether a canonical path is at or inside a canonical base.

    Args:
        base: The canonicalised directory writes are confined to.
        resolved: The canonicalised path to judge.

    Returns:
        True when ``resolved`` is ``base`` itself or lies under it.
    """
    return resolved == base or resolved.is_relative_to(base)


def resolve_within(base: PathLike, candidate: PathLike) -> Path:
    """Return where ``candidate`` may be written under ``base``, or refuse.

    Blocking: this canonicalises, which reads the filesystem. Call it
    from :func:`resolve_within_async` on any async path -- the AST scan
    in ``tests/test_no_blocking_io.py`` cannot see this call shape, so
    the guarantee is held by ``test_paths``'s thread-recording test
    instead of by the scan.

    ``~`` is **not** special-cased and is not expanded:
    ``resolve_within(base, '~/evil')`` answers ``base/~/evil``, a
    directory literally named ``~``. That is POSIX filename semantics --
    tilde expansion is a shell feature, not a filesystem one -- and a
    remote server that emits ``~`` in an entry name has named an ordinary
    file, not the caller's home directory.

    A relative ``base`` is resolved against the process working
    directory, as ``Path.resolve`` does; the containment answer is
    therefore about where the process is running, which is documented
    rather than defended against.

    Args:
        base: The directory the write must land inside. Canonicalised
            here, so a symlinked base is judged by where it points.
        candidate: The path relative to ``base`` -- typically an entry
            name a remote server supplied. Absolute candidates are
            refused rather than honoured, because a transfer confined to
            ``base`` has no business naming a root.

    Returns:
        The path to write to: the canonicalised parent directory joined
        with the final component **exactly as given**, so a symbolic
        link at that final component survives to be refused by
        ``O_NOFOLLOW`` rather than being silently resolved here. That
        holds for an **empty** candidate too -- the base as its own
        candidate, which is what a single-file transfer produces -- and
        it did not before: that case fell into the ``..`` arm, whose
        whole-path ``resolve()`` canonicalised the leaf away.

    Raises:
        PathContainmentError: If ``candidate`` is absolute, contains a
            null byte, or resolves -- through ``..``, through a
            symlinked intermediate component, or both -- to somewhere
            outside ``base``.
    """
    if '\x00' in str(candidate) or '\x00' in str(base):
        raise PathContainmentError(
            f'path {str(candidate)!r} contains a null byte, which no '
            f'filesystem accepts and every path check mis-reads')

    relative = PurePath(candidate)
    if relative.is_absolute():
        raise PathContainmentError(
            f'path {str(candidate)!r} is absolute, so it names a '
            f'location rather than an entry inside {str(base)!r}')

    canonical_base = Path(base).resolve()
    combined = Path(base) / relative

    if not relative.parts:
        # The candidate is the base *itself* -- an empty tail, or `.`.
        # It reaches here from `under()` on a single-file transfer,
        # where the caller's `client_path`/`local_path` is both the
        # confinement base and the one file to write, so
        # `relative_to` leaves nothing over.
        #
        # It must NOT take the `..` arm below. That arm answers
        # `combined.resolve()`, which canonicalises the **final**
        # component too -- and the final component here is the file
        # about to be opened. A symbolic link at it was therefore
        # resolved away before the open, so `O_NOFOLLOW` was handed the
        # link's *target* and had nothing left to refuse: measured
        # against the real `aioftp` recursion, a `client_path` that was
        # a symlink to someone else's file wrote straight through it at
        # mode 0644 and answered ok=True, where the identical HTTP
        # download answered PATH/400 (R22/M18).
        #
        # Containment is trivially satisfied -- the base is at the base
        # -- so what is owed is the *other* half of this function's
        # contract: canonicalise the parent, hand the final component
        # back verbatim, exactly as the leaf arm does.
        whole = Path(base).absolute()
        return whole.parent.resolve() / whole.name

    if _names_a_directory(relative):
        whole = combined.resolve()
        if not _contained(canonical_base, whole):
            raise PathContainmentError(
                f'path {str(candidate)!r} resolves to {str(whole)!r}, '
                f'which is outside {str(canonical_base)!r}')
        return whole

    parent = combined.parent.resolve()
    if not _contained(canonical_base, parent):
        raise PathContainmentError(
            f'path {str(candidate)!r} resolves into {str(parent)!r}, '
            f'which is outside {str(canonical_base)!r}')
    return parent / combined.name


async def resolve_within_async(base: PathLike, candidate: PathLike) -> Path:
    """Canonicalise and check containment without blocking the loop.

    :func:`resolve_within` stats every component of the path it
    canonicalises. On a cold page cache that is real latency, and on the
    event loop it is latency every other in-flight request pays -- the
    R20 property, in a call shape the package's AST scan is structurally
    unable to see (it matches ``Path(x).stat()``, not ``.resolve()``, and
    not a call made through a helper).

    Args:
        base: The directory the write must land inside.
        candidate: The path relative to ``base``.

    Returns:
        Whatever :func:`resolve_within` returns.

    Raises:
        PathContainmentError: As :func:`resolve_within` does.
    """
    return await asyncio.to_thread(resolve_within, base, candidate)


def under(base: PathLike, path: BytesOrPathLike) -> Path:
    """Contain a path a **transfer library** composed, against ``base``.

    The seam between :func:`resolve_within` and a third-party client.
    ``aioftp`` and ``asyncssh`` both do the recursive filename
    arithmetic internally -- they take the entry names a server sent,
    join them onto the local destination, and hand the *result* to their
    filesystem layer -- so by the time this library can see a path
    again, the ``..`` is already inside a fully-composed absolute one
    like ``/downloads/../victim/OWNED``. This splits that back into the
    trusted prefix and the untrusted tail, and puts the tail through the
    same check every other write goes through.

    The split is **textual**, and it has to be: normalising first would
    delete the very ``..`` the check exists to catch, and resolving
    first would resolve the escape into a location that no longer looks
    like one. ``PurePath.relative_to`` does no filesystem access and no
    normalisation, so ``/dl/../victim/OWNED`` against ``/dl`` yields
    ``../victim/OWNED`` -- intact, and refused a line later.

    Args:
        base: The local directory the transfer is confined to.
            Absolute, and the same value handed to the client as its
            local operand, so the prefix genuinely matches.
        path: What the client is about to touch. ``bytes`` is accepted
            because ``asyncssh``'s filesystem protocol speaks bytes.

    Returns:
        The path to act on, canonical parent and verbatim final
        component, exactly as :func:`resolve_within` returns.

    Raises:
        PathContainmentError: If ``path`` does not start with ``base``
            at all, or if its tail escapes ``base``.
    """
    root = PurePath(os.fsdecode(base))
    composed = PurePath(os.fsdecode(path))
    try:
        tail = composed.relative_to(root)
    except ValueError as err:
        raise PathContainmentError(
            f'transfer path {str(composed)!r} is not under the local '
            f'directory {str(root)!r} it was confined to') from err
    return resolve_within(root, tail)


def caller_path(path: PathLike) -> Path:
    """Canonicalise a caller-named path against its own parent directory.

    Blocking; :func:`resolve_caller_path` is the async entry point and
    is what every coroutine calls. Split out so the whole of it -- the
    ``absolute()`` as well as the ``resolve()`` -- runs in the one
    thread hop rather than leaving the first half on the loop.

    ``Path.absolute`` rather than ``os.path.abspath``: ``abspath``
    applies ``normpath``, which deletes a ``..`` **textually**, before
    anything has looked at the filesystem. On a path whose preceding
    component is a symbolic link that is the wrong answer, and it is the
    wrong answer in the permissive direction. ``absolute`` only prefixes
    the working directory and leaves every component intact for
    :func:`resolve_within` to canonicalise properly.

    Args:
        path: The local path the caller asked to write to.

    Returns:
        The canonical parent joined with the final component verbatim.

    Raises:
        PathContainmentError: If the path contains a null byte, or its
            final component is ``.``/``..``/empty, which names a
            directory rather than a file to write. A path that *is* an
            existing directory but does not spell one -- ``/tmp`` -- is
            not caught here and is refused by the open itself, as
            ``IsADirectoryError``: the honest report of what went wrong,
            and R22's "a caller-supplied path that is itself a
            directory" edge case.
    """
    # Read off the *string*, before `Path` sees it: `pathlib` strips a
    # trailing separator while parsing, so `Path('/tmp/x/').name` is
    # `'x'` and a check made after construction cannot tell `/tmp/x/`
    # -- which names a directory -- from `/tmp/x`, which names a file.
    spelled = os.fspath(path)
    if not spelled or spelled.endswith(os.sep) or _names_a_directory(
            PurePath(Path(spelled).name)):
        raise PathContainmentError(
            f'{str(path)!r} names a directory, not a file to write to')
    absolute = Path(spelled).absolute()
    return resolve_within(absolute.parent, absolute.name)


async def resolve_caller_path(path: PathLike) -> Path:
    """Canonicalise a path the **caller** named, against its own parent.

    The caller's own ``download_filepath`` is not the same kind of value
    as a filename a remote server supplied, and treating it as one would
    be wrong in both directions. A caller naming ``/tmp/report.pdf`` has
    named a location and is entitled to; refusing it because it is
    absolute would break every documented use. But the write still has
    to be guarded -- the README's fixed ``/tmp/test.pdf`` example is
    exactly the symlink pre-creation target M18 names -- so the path is
    canonicalised against **its own parent directory** and then handed
    to :func:`safe_writer` like any other.

    What this buys over passing the string straight to ``open``: the
    parent is resolved, so the value reaching the opener is a real
    location rather than one containing ``..`` or a symlinked component,
    and the final component is still held back verbatim for
    ``O_NOFOLLOW`` to judge. What it does not buy is confinement -- the
    base *is* the caller's own directory, so containment is trivially
    satisfied. Confinement is meaningful only where the untrusted party
    supplies the name, which is the FTP/SFTP recursive-download case.

    Args:
        path: The local path the caller asked to write to. Relative
            paths resolve against the process working directory, as
            documented on :func:`resolve_within`.

    Returns:
        The canonical parent joined with the final component verbatim.

    Raises:
        PathContainmentError: As :func:`caller_path` does.
    """
    return await asyncio.to_thread(caller_path, path)


def open_within(path: str, flags: int, *, nofollow: int = O_NOFOLLOW) -> int:
    """Open ``path`` by descending descriptors, never by re-walking it.

    The single construction that answers N2, N3 and N4, because all
    three are the same mistake seen from three angles: a decision made
    about a *name* and then acted on through that name a moment later.
    Here the decision and the action are the same descriptor.

    Three phases, and the order is the security property:

    1. **Anchor.** Open the path's root -- ``/``, or the working
       directory for a relative path -- as a directory descriptor.
    2. **Descend.** Open each intermediate component relative to the
       descriptor already held, refusing a symbolic link
       (``O_NOFOLLOW``) and anything that is not a directory
       (``O_DIRECTORY``) at every step. This is what N2's flipper cannot
       beat: a component swapped after its step is irrelevant because
       that step is never repeated, and a component swapped before its
       step is refused when it is reached.
    3. **Create, judge, only then truncate.** Open the final component
       with ``dir_fd``, ``fstat`` the descriptor that came back, refuse
       it if it is not a plain unshared file (N3, N4) -- and *only if it
       survives that* apply the truncation ``overwrite=True`` asked for.

    **``O_TRUNC`` is deliberately not passed to the open**, and the
    reason is a defect this function had in its first draft: with the
    flag on the open, the kernel truncates as part of opening, so a
    hardlinked victim was emptied *before* the ``fstat`` that refuses
    it ever ran. The refusal was correct and the file was already
    destroyed -- a worse outcome than the write-through it was meant to
    prevent. Truncation is therefore an explicit ``ftruncate`` on the
    validated descriptor, which is also strictly more race-free: it acts
    on the inode this call opened, not on whatever the name means now.

    Args:
        path: The path to open. Absolute in every call this library
            makes, because both entry points canonicalise a parent
            first; a relative path is anchored at the working directory.
        flags: The open flags the caller computed, already carrying
            ``O_EXCL`` or ``O_TRUNC`` per the overwrite policy. The
            ``O_TRUNC`` bit is honoured after validation rather than by
            the open, per the note above.
        nofollow: The ``O_NOFOLLOW`` bit, or 0 on a platform without it.
            At 0 the descent cannot refuse a symlinked component and
            the leaf falls back to the two-syscall
            :func:`_refuse_symlink_at`
            check -- the documented degradation, unchanged in kind by
            this function and now stated in one place instead of two.

    Returns:
        An open file descriptor for the final component, blocking-mode
        restored, owned by the caller.

    Raises:
        PathContainmentError: If a component or the leaf is a symbolic
            link, or the opened file is not a regular file, or its inode
            is reachable under another name.
        OSError: As ``os.open`` does, for every other reason.
    """
    pure = PurePath(path)
    anchor = pure.anchor or '.'
    parts = pure.parts[1:] if pure.anchor else pure.parts
    if not parts:
        raise PathContainmentError(
            f'{path!r} names a directory, not a file to write to')
    leaf = parts[-1]

    base_fd = os.open(anchor, DIRECTORY_FLAGS & ~nofollow | O_CLOEXEC)
    try:
        parent_fd = _walk_to_parent(base_fd, parts[:-1])
    finally:
        os.close(base_fd)

    try:
        if not nofollow:
            # The degraded platform: no flag can refuse the link, so the
            # check is a separate syscall and carries its window. Made
            # against ``dir_fd`` so it at least asks about the same
            # directory the open will use.
            _refuse_symlink_at(parent_fd, leaf, path)
        try:
            descriptor = os.open(
                leaf,
                (flags & ~os.O_TRUNC) | nofollow | O_NONBLOCK | O_CLOEXEC,
                FILE_MODE,
                dir_fd=parent_fd,
            )
        except OSError as err:
            # ``O_NONBLOCK`` converts N3's hang into an errno, and which
            # errno depends on the platform: Linux answers ``ENXIO`` for
            # both a reader-less FIFO and a bound unix socket, while
            # macOS answers ``ENXIO`` for the FIFO and ``EOPNOTSUPP``
            # for the socket. Both are refusals of the same kind --
            # "this is not a regular file" -- so both are typed, and the
            # message asks the filesystem what is actually there rather
            # than assuming the FIFO. Typed here rather than left to
            # `classify_refusal`, because this is the one site that
            # knows the errno came from *this* library's own open flag.
            if err.errno not in NOT_A_REGULAR_FILE_ERRNOS:
                raise
            raise PathContainmentError(
                f'refusing to write to {str(path)!r}, which is a '
                f'{_describe_at(parent_fd, leaf)} rather than a regular '
                f'file') from err
    finally:
        os.close(parent_fd)

    try:
        _reject_irregular(descriptor, path)
        _restore_blocking(descriptor)
        if flags & os.O_TRUNC:
            os.ftruncate(descriptor, 0)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _refuse_symlink_at(parent_fd: int, leaf: str, path: str) -> None:
    """Refuse a symlinked leaf, asked relative to an open directory.

    Args:
        parent_fd: The open descriptor for the containing directory.
        leaf: The final component to test.
        path: The whole path, for the message only.

    Returns:
        None, when the leaf is not a symbolic link or is absent.

    Raises:
        PathContainmentError: If the leaf is a symbolic link.
    """
    try:
        info = os.lstat(leaf, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise PathContainmentError(
            f'refusing to write through the symbolic link at {path!r}')


def guarded_opener(
    *,
    overwrite: bool,
    nofollow: int = O_NOFOLLOW,
) -> Callable[[str, int], int]:
    """Build the ``opener`` a guarded write passes to ``open``.

    Returned rather than applied, because ``open`` (and therefore
    ``aiofiles.open``) takes an ``opener`` and calls it with the flags it
    computed from the mode. Adding to *those* flags is what puts the
    refusal inside the same syscall that creates the file.

    ``O_TRUNC`` is what ``'wb'`` asks for and is replaced by ``O_EXCL``
    unless the caller opted into overwriting: R22 decides the default is
    **refuse to overwrite**, so the ordinary case is a create that fails
    if anything is already there -- a symlink very much included.

    The open itself is :func:`open_within`'s descriptor walk rather than
    a bare ``os.open`` of the path (N2/N3/N4). The flag arithmetic is
    unchanged and stays here, where the overwrite policy lives; what
    changed underneath is that the flags are applied to a leaf resolved
    against an open directory instead of to a string the kernel re-walks.

    Args:
        overwrite: True to truncate an existing file, False to refuse
            one. The default everywhere is False.
        nofollow: The ``O_NOFOLLOW`` bit, or 0 on a platform without it.
            A parameter rather than a constant read inside, so the
            degraded branch is reachable from a test on a platform that
            has the flag -- R22's last criterion asks for the fallback to
            carry the same coverage, and a fallback nothing can execute
            carries none.

    Returns:
        A callable of ``(path, flags)`` returning an open file
        descriptor, suitable as ``open(..., opener=)``.
    """
    def opener(path: str, flags: int) -> int:
        """Open ``path`` with the containment flags added.

        Args:
            path: The path ``open`` was called with.
            flags: The flags ``open`` derived from its mode.

        Returns:
            The open file descriptor.

        Raises:
            OSError: As ``os.open`` does -- ``EEXIST`` for a target that
                is already there, ``ELOOP``/``EMLINK`` for a symbolic
                link. :func:`safe_writer` classifies these.
            PathContainmentError: If a directory component or the leaf
                is a symbolic link, if the target is not a regular file,
                or if it is a hard link to a second name.
        """
        if not overwrite:
            flags = (flags & ~os.O_TRUNC) | os.O_EXCL
        return open_within(path, flags, nofollow=nofollow)

    return opener


def _what_is_there(path: str) -> str:
    """Return what kind of thing already occupies ``path``.

    Blocking; called through a thread. Classification only, and only
    *after* an open has already been refused -- so unlike a pre-write
    check, nothing here can be raced into permitting a write.

    Args:
        path: The path the open refused.

    Returns:
        ``'symlink'``, ``'directory'``, or ``'file'`` for anything else
        -- including a path that has since vanished, which is the
        ordinary-file message and not worth a third case.
    """
    if os.path.islink(path):
        return 'symlink'
    if os.path.isdir(path):
        return 'directory'
    return 'file'


def classify_refusal(path: PathLike, err: OSError) -> AsyncGatewayError:
    """Return the typed error a refused local write should raise.

    Classification only. The open (or the write) has already failed and
    nothing usable has been left behind, so reading the filesystem here
    cannot be raced into permitting anything -- which is what makes an
    ``islink`` legitimate *after* the refusal when it would be a
    vulnerability before it.

    Blocking, and deliberately so: the two write seams that need it are
    already inside a thread when the refusal arrives -- the protocol
    wrappers' opens run in an executor -- and only the async
    :func:`safe_writer` has to hand it to one. A single classifier is
    the point. When the FTP and SFTP wrappers each let the raw
    ``OSError`` escape instead, the same refused overwrite surfaced as
    ``FileExistsError`` on the protocol paths and as ``ConfigurationError``
    on the HTTP one, so R22-AC3's default was untypeable by a caller
    that used both.

    **Total, since NEW-R10-1**: every ``OSError`` gets a class, and the
    residual ones become :class:`LocalWriteError`. It used to answer
    None for "belongs to neither", which delegated the decision back to
    three call sites that each made it differently -- FTP re-raised into
    a clause that classified an ``OSError`` as ``CONNECT``, SFTP's
    reported ``PATH``, and HTTP had no ``OSError`` row at all so a
    missing parent directory reached ``request()`` as a raw
    ``FileNotFoundError``. Returning an answer for every input is what
    makes the four protocols agree by construction rather than by three
    tables happening to line up.

    Args:
        path: The path the write refused.
        err: What the filesystem raised.

    Returns:
        A ``PathContainmentError`` when the target was a symbolic link, a
        ``ConfigurationError`` when it was an ordinary existing file and
        the caller did not ask to overwrite or a directory, and a
        ``LocalWriteError`` for every other local failure -- a missing
        parent directory, an unwritable one, a full disk.
    """
    if err.errno in SYMLINK_ERRNOS:
        return PathContainmentError(
            f'refusing to write through the symbolic link at '
            f'{str(path)!r}')
    if err.errno != errno.EEXIST:
        # The local filesystem said no for a reason that is neither a
        # containment finding nor a policy refusal. Named as itself so a
        # caller can tell "this machine could not keep the file" from
        # "the network failed", and so no retry is spent re-downloading
        # a body this disk will refuse again.
        return LocalWriteError(
            f'cannot write the local file {str(path)!r}: {err.strerror} '
            f'(errno {err.errno})')
    kind = _what_is_there(os.fspath(path))
    if kind == 'symlink':
        return PathContainmentError(
            f'refusing to write through the symbolic link at '
            f'{str(path)!r}')
    if kind == 'directory':
        # Reported as itself rather than as "exists, pass overwrite":
        # `overwrite=True` would not help, it would reach the open
        # again and earn `IsADirectoryError`. Telling a caller to
        # retry with a flag that cannot work is worse than saying
        # nothing. R22's "a caller-supplied path that is itself a
        # directory" edge case.
        return ConfigurationError(
            f'{str(path)!r} is a directory, not a file to write to')
    return ConfigurationError(
        f'{str(path)!r} already exists and overwrite is False; pass '
        f'overwrite=True to replace it')


@asynccontextmanager
async def safe_writer(
    path: PathLike,
    *,
    overwrite: bool = False,
) -> AsyncIterator[AsyncBufferedIOBase]:
    """Open ``path`` for writing under the containment guarantees, or refuse.

    A *writer* rather than a ``safe_write(path, data)``: every caller in
    this library streams a body it has deliberately never held whole in
    memory (R14's cap exists to keep it that way), so a signature taking
    the bytes would undo the reason the bytes are chunked.

    On any failure inside the block the partially-written file is
    removed before the exception is re-raised (M19). That includes the
    ``overwrite=True`` case: what is left after a failed truncating write
    is a truncated file, not the caller's original, so leaving it would
    orphan a corrupt one rather than preserve a good one.

    Args:
        path: Where to write. Route it through :func:`resolve_within`
            first -- this function guards *how* the file is opened, not
            *where*, and the two are separate halves of R22.
        overwrite: True to replace an existing file, False (the default)
            to refuse one.

    Yields:
        An ``aiofiles`` binary handle, already open.

    Raises:
        PathContainmentError: If the target is a symbolic link.
        ConfigurationError: If the target exists and ``overwrite`` is
            False, or is a directory.
        LocalWriteError: For every other reason the local file could not
            be opened, written, or flushed -- a missing parent
            directory, a permission failure, a full disk. It used to
            raise the bare ``OSError`` instead, which on the HTTP path
            matched no transport family and escaped ``request()`` raw
            (NEW-R10-1).
    """
    try:
        handle: AsyncBufferedIOBase = await aiofiles.open(
            path, 'wb', opener=guarded_opener(overwrite=overwrite))
    except OSError as err:
        # In a thread: the classifier stats the path, and R20 bans that
        # on the loop. The protocol wrappers are already in one.
        raise await asyncio.to_thread(classify_refusal, path, err) from err

    try:
        yield handle
    except BaseException as err:
        # `suppress`, and it is not hiding a failure: `err` is the
        # reason this block failed and is the one the caller must see.
        # A `close()` that fails while unwinding is the *same* fault
        # arriving a second time -- the buffered flush of the write that
        # already raised -- and letting it propagate from here would
        # replace the real diagnosis with its own. The file is removed
        # either way, so nothing is left behind by the suppression.
        with suppress(OSError):
            await handle.close()
        await safe_unlink(path)
        if isinstance(err, OSError):
            # The *write* half, and it is not the open half repeated. A
            # missing parent fails at the open above; ENOSPC, EDQUOT and
            # EFBIG fail here, on a write to a file that opened
            # perfectly. Classified through the same function, so the
            # two halves cannot disagree about what a full disk is.
            raise await asyncio.to_thread(
                classify_refusal, path, err) from err
        raise

    try:
        await handle.close()
    except OSError as err:
        # The third seam, and the one a write-side check alone misses.
        # `aiofiles` buffers, so a body small enough to fit the buffer
        # never fails at `write()` at all -- the first and only syscall
        # is the flush inside `close()`, on the success path, after the
        # block above has exited cleanly. Measured: a 200 KB download
        # under an 8 KiB `RLIMIT_FSIZE` reached `request()` as a raw
        # `OSError` from exactly here, with every other arm green.
        await safe_unlink(path)
        raise await asyncio.to_thread(classify_refusal, path, err) from err


async def safe_unlink(path: PathLike) -> None:
    """Remove ``path``, succeeding when it is already gone.

    M19: ``delete_local_file_path`` is documented as the post-processor
    *cleanup* step, and a cleanup that raises on the second call is a
    cleanup no caller can run from a ``finally``. Absence is the
    outcome the caller asked for, however it was reached.

    Args:
        path: The file to remove.

    Returns:
        None, whether or not there was anything to remove.
    """
    try:
        await aiofiles.os.remove(path)
    except FileNotFoundError:
        return

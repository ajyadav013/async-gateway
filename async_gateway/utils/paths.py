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
"""

import asyncio
import errno
import os
from contextlib import asynccontextmanager
from pathlib import Path, PurePath
from typing import AsyncIterator, Callable, Final, Optional, Text, Union

import aiofiles
import aiofiles.os
from aiofiles.threadpool.binary import AsyncBufferedIOBase

from async_gateway.utils.exceptions import (
    AsyncGatewayError,
    ConfigurationError,
    PathContainmentError,
)

#: Anything path-like this module accepts from a caller or from a remote
#: listing. ``PurePath`` rather than ``Path`` because a name that came
#: off the wire is a string to be judged, not a location to be touched.
PathLike = Union[Text, PurePath]

#: The same, plus ``bytes``. ``asyncssh``'s filesystem protocol speaks
#: bytes throughout -- ``scandir`` yields byte filenames and ``_copy``
#: joins them with ``posixpath.join`` -- so the containment seam facing
#: it has to accept them.
BytesOrPathLike = Union[bytes, Text, PurePath]

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
        ``O_NOFOLLOW`` rather than being silently resolved here.

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


def refuse_symlink(path: Text) -> None:
    """Refuse a path that is a symbolic link, by checking before opening.

    The degraded form of ``O_NOFOLLOW``, for a platform that does not
    have it. It is two syscalls where the flag is one, so a link planted
    between this check and the ``os.open`` that follows is followed --
    the window ``O_NOFOLLOW`` exists to remove. That is why this runs
    only where the flag is genuinely unavailable, and why the weakening
    is stated here rather than left for a reader to infer.

    Args:
        path: The path about to be opened.

    Returns:
        None, when the path is not a symbolic link.

    Raises:
        PathContainmentError: If it is one.
    """
    if os.path.islink(path):
        raise PathContainmentError(
            f'refusing to write through the symbolic link at {path!r}')


def guarded_opener(
    *,
    overwrite: bool,
    nofollow: int = O_NOFOLLOW,
) -> Callable[[Text, int], int]:
    """Build the ``opener`` a guarded write passes to ``open``.

    Returned rather than applied, because ``open`` (and therefore
    ``aiofiles.open``) takes an ``opener`` and calls it with the flags it
    computed from the mode. Adding to *those* flags is what puts the
    refusal inside the same syscall that creates the file.

    ``O_TRUNC`` is what ``'wb'`` asks for and is replaced by ``O_EXCL``
    unless the caller opted into overwriting: R22 decides the default is
    **refuse to overwrite**, so the ordinary case is a create that fails
    if anything is already there -- a symlink very much included.

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
    def opener(path: Text, flags: int) -> int:
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
            PathContainmentError: From the degraded symlink check, on a
                platform with no ``O_NOFOLLOW``.
        """
        if nofollow:
            flags |= nofollow
        else:
            refuse_symlink(path)
        if not overwrite:
            flags = (flags & ~os.O_TRUNC) | os.O_EXCL
        return os.open(path, flags, FILE_MODE)

    return opener


def _what_is_there(path: Text) -> Text:
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


def classify_refusal(path: PathLike, err: OSError) -> Optional[
        AsyncGatewayError]:
    """Return the typed error a refused guarded open should raise.

    Classification only. The open has already failed and nothing has
    been written, so reading the filesystem here cannot be raced into
    permitting anything -- which is what makes an ``islink`` legitimate
    *after* the refusal when it would be a vulnerability before it.

    Blocking, and deliberately so: the two write seams that need it are
    already inside a thread when the refusal arrives -- the protocol
    wrappers' opens run in an executor -- and only the async
    :func:`safe_writer` has to hand it to one. A single classifier is
    the point. When the FTP and SFTP wrappers each let the raw
    ``OSError`` escape instead, the same refused overwrite surfaced as
    ``FileExistsError`` on the protocol paths and as ``ConfigurationError``
    on the HTTP one, so R22-AC3's default was untypeable by a caller
    that used both.

    Args:
        path: The path the open refused.
        err: What ``os.open`` raised.

    Returns:
        A ``PathContainmentError`` when the target was a symbolic link, a
        ``ConfigurationError`` when it was an ordinary existing file and
        the caller did not ask to overwrite, or None when the failure
        belongs to neither -- a missing parent directory, a full disk --
        and the original ``OSError`` should propagate untranslated.
    """
    if err.errno in SYMLINK_ERRNOS:
        return PathContainmentError(
            f'refusing to write through the symbolic link at '
            f'{str(path)!r}')
    if err.errno != errno.EEXIST:
        return None
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
            False.
        OSError: For every other reason the file could not be opened --
            a missing parent directory, a permission failure -- reported
            as itself rather than translated into a security finding it
            is not.
    """
    try:
        handle: AsyncBufferedIOBase = await aiofiles.open(
            path, 'wb', opener=guarded_opener(overwrite=overwrite))
    except OSError as err:
        # In a thread: the classifier stats the path, and R20 bans that
        # on the loop. The protocol wrappers are already in one.
        refusal = await asyncio.to_thread(classify_refusal, path, err)
        if refusal is None:
            raise
        raise refusal from err

    try:
        yield handle
    except BaseException:
        await handle.close()
        await safe_unlink(path)
        raise
    await handle.close()


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

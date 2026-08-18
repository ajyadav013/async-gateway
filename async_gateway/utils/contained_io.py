"""Local-filesystem containment for the two transfer clients (R22).

R22's user story is that *a hostile or buggy remote server is unable to
write outside the directory I named*, and its Description names the
**recursive directory download** as the vector. That vector does not
pass through this library's own code at all. ``aioftp`` and ``asyncssh``
each take the remote entry names a server sends, do the whole recursion
and all the filename arithmetic internally, and write through their own
filesystem layer -- so guarding the four HTTP write sites leaves the
protocol path the requirement was written for completely undefended.

Both escapes were reproduced against the real libraries with only the
wire faked, and both wrote outside the target directory at mode 0644:

* **FTP.** ``aioftp.Client.download`` computes
  ``destination_path / name.relative_to(source)`` for each listed entry
  and recurses. A server that lists ``/pub/data/../victimdir/OWNED``
  under ``/pub/data`` gets ``downloads/../victimdir/OWNED``.
* **SFTP.** ``asyncssh.SFTPClient._copy`` filters ``scandir`` names that
  are ``.`` or ``..`` *exactly*, then ``posixpath.join``s them onto the
  destination. A single entry name that **contains** a separator --
  ``../victimdir/OWNED`` -- is not filtered and composes straight
  through.

Neither library offers a "confine to this directory" option, so
containment is installed at the seam each one does offer: ``aioftp``
takes a ``path_io_factory``, and ``asyncssh``'s ``_begin_copy`` takes a
destination filesystem object satisfying a small structural protocol.
Each wrapper delegates to the library's own implementation and adds one
thing -- every path is routed through
:func:`~async_gateway.utils.paths.under` before it is touched -- so the
transfer semantics are the library's and only the containment is ours.

Every write additionally goes through the same guarded open the HTTP
paths use: ``O_NOFOLLOW``, ``O_EXCL`` unless the caller opted into
overwriting, and mode 0600. Containment answers M17; the guarded open
answers M18 on the same path.
"""

import asyncio
import inspect
import io
import os
from contextlib import suppress
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Final,
    Mapping,
    Optional,
    Tuple,
)

import aioftp

import asyncssh
from asyncssh.sftp import LocalFile, local_fs

from async_gateway.utils.exceptions import (
    AsyncGatewayError,
    ConfigurationError,
    LocalWriteError,
    PathContainmentError,
)
from async_gateway.utils.http_file_config import response_too_large
from async_gateway.utils.paths import (
    BytesOrPathLike,
    PathLike,
    classify_refusal,
    guarded_opener,
    under,
)

#: The modes that create or truncate a file, as opposed to reading one.
#: Only these get the guarded opener: an ``upload`` reads its local
#: source, and applying ``O_EXCL`` to that would refuse every file that
#: exists, which is all of them.
WRITING_MODES: Final[frozenset[str]] = frozenset({'w', 'x', 'a'})


class TransferBudget:
    """The bytes one transfer may write locally, counted down as it goes.

    R14 states the response-size ceiling *globally* -- "every response
    read is capped at ``max_response_bytes``" -- and it was implemented
    on the HTTP family alone. Measured with a 1 KiB cap and a 256 KiB
    payload: HTTP refused with ``RESPONSE_TOO_LARGE`` and wrote nothing,
    while FTP and SFTP both returned ``ok=True`` having written all
    262144 bytes (NEW-R10-2). The bound a caller sets to keep a hostile
    or misconfigured endpoint from filling their disk simply did not
    exist on two of the four protocols.

    Neither transport library offers a transfer-size limit, so the
    budget is enforced where this package already owns the write: the
    containment wrappers every download's bytes pass through. Counting
    there rather than at each call site is what makes the bound hold
    across a **recursive** transfer -- the vector R22 is about -- where
    the server chooses both the file count and the file sizes, and a
    per-file check would let a thousand small files past a cap meant to
    bound the lot.

    One budget object is shared by every file of one transfer, so it is
    a ceiling on the transfer and not a per-file allowance.

    Attributes:
        limit: The ceiling in bytes, as the caller set it.
        written: How much has been written so far.
    """

    def __init__(self, limit: int) -> None:
        """Start a budget of ``limit`` bytes.

        Args:
            limit: The ceiling, already validated positive by
                ``logic.http_client.validated_max_response_bytes``.
        """
        self.limit = limit
        self.written = 0

    def spend(self, count: int) -> None:
        """Account for ``count`` bytes, or refuse the transfer.

        Called **before** the write, not after: a cap enforced after the
        fact has already put the bytes on the disk it exists to
        protect.

        Args:
            count: The size of the block about to be written.

        Returns:
            None.

        Raises:
            ResponseTooLargeError: If the block would cross the
                ceiling. The same error the HTTP path raises, so a
                caller reads one code for one condition on all four
                protocols.
        """
        self.written += count
        if self.written > self.limit:
            raise response_too_large(self.written, self.limit)


async def _unlink(path: Path) -> None:
    """Remove ``path``, succeeding when it is already gone.

    The transfer wrappers' equivalent of
    :func:`~async_gateway.utils.paths.safe_unlink`, in a thread because
    R20 bans a blocking filesystem call on the event loop and these
    wrappers -- unlike ``safe_writer`` -- are not already inside one.

    Args:
        path: The partial file to remove.

    Returns:
        None, whether or not there was anything to remove.
    """
    await asyncio.to_thread(path.unlink, missing_ok=True)


def _writes(mode: str) -> bool:
    """Report whether an open mode creates or modifies a file.

    Args:
        mode: The mode string, as passed to ``open``.

    Returns:
        True when the open is a write.
    """
    return bool(mode) and mode[0] in WRITING_MODES


def _shown(path: BytesOrPathLike) -> str:
    """Render a path for a diagnostic message.

    Pure string work -- ``os.fsdecode`` touches no filesystem -- but the
    ban in ``tests/test_no_blocking_io.py`` is by *module prefix* rather
    than by known-blocking name, deliberately, so that an ``os.``
    nobody thought of is refused rather than missed. Adding a name to
    that test's ``PURE_PATH_HELPERS`` to admit this one would widen a
    guard belonging to another story; keeping the call in a plain
    ``def`` costs a function and widens nothing.

    Args:
        path: The path to render, bytes or text.

    Returns:
        Its text form.
    """
    return os.fsdecode(path)


def _as_given(given: Path, checked: Path, produced: Path) -> Path:
    """Re-express a checked path in the form the caller handed down.

    The half of containment that is *not* a security property, and
    whose absence was a High defect (AGW-38). Every check in this
    module canonicalises -- that is how the guard works, because a
    ``..`` or a symlinked component only becomes visible once the path
    is resolved. But a canonical path is the wrong thing to hand *back*
    to a caller that is going to do arithmetic against the
    uncanonicalised operand it still holds.

    ``aioftp.Client.upload`` is exactly that caller: its directory arm
    computes ``path.relative_to(source)`` for every entry
    :meth:`ContainedPathIO.list` yields, against the ``source`` it was
    given. Wherever any component of that source is a symbolic link the
    two disagree -- ``/private/tmp/x`` is not under ``/tmp/x`` as a
    *string* -- and ``relative_to`` raises ``ValueError``, which is in
    no FTP transport family and so escaped ``request()`` un-enveloped,
    part-way through a directory upload. Reproduced on the README's own
    documented ``/tmp`` path.

    So the check stays canonical and only the *answer* is translated:
    ``produced`` is known to sit at or under ``checked`` (it was
    composed from it), and is re-rooted onto ``given`` so the caller's
    own arithmetic works. Containment is untouched -- nothing here
    decides whether a path is allowed, and every path handed back is
    checked again by whichever method acts on it next.

    Args:
        given: The path as the caller passed it in, uncanonicalised.
        checked: What :meth:`ContainedPathIO.contained` made of it.
        produced: A path at or under ``checked``.

    Returns:
        ``produced`` re-rooted onto ``given``.
    """
    return Path(given) / produced.relative_to(checked)


class _AsGivenLister(aioftp.AsyncListerMixin[Path]):
    """``aioftp``'s directory lister, yielding caller-form paths.

    The listing half of :func:`_as_given`. ``aioftp``'s own lister is
    built over the *checked* directory and therefore yields entries
    under it; this re-roots each one onto the path the caller named, so
    ``aioftp.Client.upload``'s ``relative_to(source)`` succeeds.

    A wrapper rather than a reimplementation: the iteration, the
    executor hop, the timeout and ``aioftp``'s ``PathIOError`` wrapping
    all stay the library's own. ``AsyncListerMixin`` is subclassed for
    the ``await lister`` form, which ``aioftp`` supports alongside
    ``async for`` and which a bare async iterator would not answer.

    Attributes:
        inner: ``aioftp``'s lister over the checked directory.
        given: The directory as the caller named it.
        checked: What containment made of it.
    """

    def __init__(self, inner: Any, given: Path, checked: Path) -> None:
        """Wrap one lister.

        Args:
            inner: ``aioftp``'s lister over ``checked``.
            given: The directory as the caller named it.
            checked: The contained form of ``given``.
        """
        super().__init__()
        self.inner = inner
        self.given = given
        self.checked = checked

    def __aiter__(self) -> '_AsGivenLister':
        """Start iterating the wrapped lister.

        Returns:
            This object, which is its own iterator.
        """
        self.iterator = self.inner.__aiter__()
        return self

    async def __anext__(self) -> Path:
        """Return the next entry, in the caller's own form.

        Returns:
            The next entry re-rooted onto :attr:`given`.
        """
        return _as_given(
            self.given, self.checked, await self.iterator.__anext__())


def classify_mkdir_refusal(path: Path, err: OSError) -> AsyncGatewayError:
    """Return the typed error a refused ``mkdir`` should raise.

    :func:`~async_gateway.utils.paths.classify_refusal` with the one arm
    that cannot apply to a directory replaced. That function is written
    for the *open* of a file, so its ``EEXIST`` answer is
    ``ConfigurationError``: "already exists and overwrite is False; pass
    ``overwrite=True`` to replace it". For a ``mkdir`` that advice is
    simply false -- ``overwrite`` governs whether a *file* this library
    writes may replace one already there, and no value of it makes
    ``mkdir`` succeed over an existing regular file. Telling a caller to
    retry with a flag that cannot work is the same mistake the directory
    arm of ``classify_refusal`` already refuses to make.

    So a ``mkdir`` refused because something is already in the way is a
    :class:`~async_gateway.utils.exceptions.LocalWriteError` -- code
    ``PATH``, which is what **every other** ``mkdir`` refusal on both
    protocols already reports: a missing ancestor
    (``LocalWriteError``), and an escaping path
    (``PathContainmentError``). Measured before this change, against
    real loopback servers, with a directory download aimed at a local
    path occupied by a regular file: FTP answered ``CONFIG``/400 and
    SFTP answered ``PATH``/400 -- one question, two codes, which is the
    class of divergence this release has spent eleven rounds closing.

    A symbolic link in the way keeps its ``PathContainmentError``: that
    is a containment finding and not an operational one, and the
    distinction between the two is the reason those are separate
    classes.

    Args:
        path: The directory the create refused, for the message.
        err: What the filesystem raised.

    Returns:
        The typed error, always reporting ``PATH``.
    """
    typed = classify_refusal(path, err)
    if isinstance(typed, ConfigurationError):
        return LocalWriteError(
            f'cannot create the local directory {str(path)!r}: something '
            f'is already there and it is not a directory')
    return typed


def _open_guarded(path: Path, mode: str, overwrite: bool) -> io.BytesIO:
    """Open ``path`` synchronously with the containment flags applied.

    Blocking by design: both callers already run their opens in a
    thread (``aioftp``'s ``AsyncPathIO`` through an executor,
    ``asyncssh``'s ``LocalFS`` -- see :class:`ContainedLocalFS` for how
    that one is moved off the loop).

    Args:
        path: The already-contained path to open.
        mode: The open mode the library asked for.
        overwrite: Whether an existing file may be replaced.

    Returns:
        The open file object.

    Raises:
        PathContainmentError: If the target is a symbolic link.
        ConfigurationError: If the target exists and ``overwrite`` is
            False.
        LocalWriteError: For every other reason the open failed -- a
            missing parent, a permission failure, a full disk.
    """
    # `type: ignore[return-value]` -- the declared return is `io.BytesIO`
    # because that is what `aioftp.pathio.AbstractPathIO._open` declares
    # and this value is handed straight back to it; the builtin actually
    # answers a `BufferedReader`/`BufferedWriter`. Annotating the true
    # type here would make the override incompatible with the base class,
    # so the mismatch is aioftp's and is pinned at the two sites that
    # cross it rather than papered over with `Any`.
    if not _writes(mode):
        return open(path, mode)  # type: ignore[return-value]
    try:
        return open(  # type: ignore[return-value]
            path, mode, opener=guarded_opener(overwrite=overwrite))
    except OSError as err:
        # The same classification :func:`safe_writer` gives the HTTP
        # path. Without it a refused overwrite reached an FTP or SFTP
        # caller as a raw ``FileExistsError`` and a refused symlink as
        # ``OSError``, so R22-AC3's typed contract held on one protocol
        # and not the other two. Already in a thread, so the classifier's
        # stat is off the loop.
        #
        # `classify_refusal` is total since NEW-R10-1, so there is no
        # longer a residual-`OSError` arm to re-raise from here -- a
        # missing parent directory now becomes a `LocalWriteError` on
        # this seam exactly as it does on the HTTP one, rather than
        # escaping to be re-classified as `CONNECT` by the FTP dispatch.
        raise classify_refusal(path, err) from err


class ContainedPathIO(aioftp.pathio.AsyncPathIO):
    """``aioftp``'s own path layer, confined to one local directory.

    Installed as the client's ``path_io_factory``, which is the only
    seam ``aioftp`` offers between its recursion and the disk. Every
    path the client hands down -- the ones it composed out of the
    server's entry names included -- is routed through
    :func:`~async_gateway.utils.paths.under` first, so a name that
    escapes the base is refused before any filesystem call is made with
    it.

    ``AsyncPathIO`` and not ``PathIO`` is the base deliberately. The
    parent's operations run in an executor; ``PathIO``'s run inline on
    the event loop, which is what R20 bans. The docstring on
    ``AsyncPathIO`` calls itself "really slow" relative to the blocking
    one, and that is the trade this library has already made everywhere
    else.

    A factory rather than an instance: ``aioftp.BaseClient.__init__``
    calls ``path_io_factory(timeout=...)``, so the base directory and
    the overwrite policy are bound with :func:`contained_path_io_factory`
    before the class ever reaches the client.

    Attributes:
        base: The local directory every path must resolve inside.
        overwrite: Whether an existing local file may be replaced.
    """

    def __init__(
        self,
        *args: Any,
        base: PathLike,
        overwrite: bool = False,
        budget: Optional[TransferBudget] = None,
        **kwargs: Any,
    ) -> None:
        """Bind this path layer to one directory.

        Args:
            args: Positional arguments for ``AsyncPathIO``.
            base: The local directory writes are confined to.
            overwrite: Whether an existing file may be replaced.
            budget: The bytes this whole transfer may write, or None
                for an unbounded one. One object per transfer, shared
                across every file of a recursive download, so the
                ceiling bounds the transfer rather than each file.
            kwargs: Keyword arguments for ``AsyncPathIO``; ``aioftp``
                passes ``timeout``.
        """
        super().__init__(*args, **kwargs)
        self.base = Path(base)
        self.overwrite = overwrite
        self.budget = budget
        #: Where each open handle was opened, so a write that fails can
        #: remove what it left. ``aioftp`` hands the *file object* back
        #: to ``write``/``close`` and never the path, and a partial file
        #: is exactly what M19 says this library must not orphan --
        #: without this a refused transfer left a truncated download on
        #: disk where the HTTP path leaves nothing.
        self._targets: dict[int, Path] = {}

    def contained(self, path: BytesOrPathLike) -> Path:
        """Return ``path`` proven to be inside :attr:`base`.

        Args:
            path: Whatever ``aioftp`` is about to act on.

        Returns:
            The contained path.

        Raises:
            PathContainmentError: If it escapes the base.
        """
        return under(self.base, path)

    async def exists(self, path: Path) -> bool:
        """Report whether a contained path exists.

        Args:
            path: The path to test.

        Returns:
            True when it exists.
        """
        return await super().exists(self.contained(path))

    async def is_dir(self, path: Path) -> bool:
        """Report whether a contained path is a directory.

        Args:
            path: The path to test.

        Returns:
            True when it is a directory.
        """
        return await super().is_dir(self.contained(path))

    async def is_file(self, path: Path) -> bool:
        """Report whether a contained path is a regular file.

        Args:
            path: The path to test.

        Returns:
            True when it is a file.
        """
        return await super().is_file(self.contained(path))

    async def mkdir(
        self,
        path: Path,
        *,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        """Create one directory, inside the base, never a whole tree.

        The directory half of the escape, and it lands *before* the
        file half: a hostile entry name that is a directory is
        ``mkdir``'d first and written second, so refusing here stops
        the tree being created at all rather than only its leaves.

        ``parents`` is **dropped rather than forwarded**, which is
        NEW-R11-1. ``aioftp.Client.download`` calls
        ``mkdir(parent, parents=True, exist_ok=True)`` before it writes
        anything, so forwarding the flag ran an unbounded ``mkdir -p``
        on a caller-named path -- and FTP alone answered a missing
        destination directory with ``ok=True``/200, having silently
        created the tree, where HTTP and SFTP both refuse with
        ``PATH``/400. Measured against real loopback servers with
        ``client_path=<tmp>/a/b/c/out.bin``: three levels created on
        FTP, nothing created and ``PATH`` on the other two; a recursive
        download created eight caller- and server-named entries.

        Containment itself was never breached -- the tree landed inside
        the caller's own literal path -- so this is a *contract*
        divergence, not an escape: creating directories nobody asked
        for is the surprising behaviour, and the two siblings already
        refuse. Dropping the flag makes all three agree, and agree the
        way the majority already did.

        What survives is the one level ``asyncssh`` also creates: the
        base itself for a directory download, since ``exist_ok`` makes
        it a no-op when it is already there and its own parent must
        exist either way. Nothing legitimate is lost -- ``aioftp``'s
        recursion descends one level at a time and creates every parent
        before its child, so a ``parents`` it never needs is a
        capability only a gap in the caller's path can consume.

        One path outside the base is tolerated, by **exact match**: the
        base's own parent. For a single-file download the destination
        *is* the base, so ``aioftp`` asks for its parent -- the
        directory the caller named the inside of. That is neither
        created nor refused here; it is left to the open, which fails
        with ``PATH`` if it is missing, exactly as the HTTP path does.
        The tolerance is safe because it is an exact match and not a
        prefix rule: ``base/../victimdir`` is a different path and is
        refused.

        Args:
            path: The directory to create.
            parents: Accepted and deliberately ignored -- see above.
            exist_ok: Do not fail when it is already there.

        Returns:
            None.

        Raises:
            PathContainmentError: If the directory escapes the base, or
                a symbolic link is in the way.
            LocalWriteError: If the local filesystem refused it -- a
                missing ancestor above all, or something already
                occupying the path. Classified here for the same reason
                :meth:`write` and :meth:`close` classify: ``aioftp``
                wraps the ``OSError`` in a ``PathIOError``, which
                reached the FTP dispatch as an ``AIOFTPException`` and
                was reported ``TRANSPORT``/502 -- a network verdict for
                a local directory, on the very call this method now
                refuses.

                Through :func:`classify_mkdir_refusal` and not the
                plain classifier, so that a path already occupied by a
                regular file reports ``PATH`` here as it does on SFTP.
                It used to report ``CONFIG``, because the shared
                classifier answers ``EEXIST`` with "pass
                ``overwrite=True`` to replace it" -- advice that is
                true of a file open and false of a ``mkdir``, where no
                value of ``overwrite`` can make the call succeed.
        """
        del parents
        # The tolerance is tested *before* containment, and must stay
        # there: the base's own parent is by definition outside the
        # base, so containing it first would refuse the very path this
        # arm exists to wave through.
        if Path(_shown(path)).absolute() == self.base.parent:
            return
        target = self.contained(path)
        await self._classifying(
            super().mkdir(target, parents=False, exist_ok=exist_ok),
            classify=classify_mkdir_refusal,
            path=target)

    async def rmdir(self, path: Path) -> None:
        """Remove a directory, inside the base or not at all.

        Args:
            path: The directory to remove.

        Returns:
            None.
        """
        await super().rmdir(self.contained(path))

    async def unlink(self, path: Path) -> None:
        """Remove a file, inside the base or not at all.

        Args:
            path: The file to remove.

        Returns:
            None.
        """
        await super().unlink(self.contained(path))

    def list(self, path: Path) -> Any:
        """List a directory, inside the base, in the caller's own form.

        The one method whose **return value** a caller does arithmetic
        on, and therefore the one that cannot hand back the canonical
        path the check produced (AGW-38). ``aioftp.Client.upload``'s
        directory arm computes ``entry.relative_to(source)`` against
        the uncanonicalised ``source`` it still holds, so a canonical
        entry raised ``ValueError`` -- not an FTP transport family,
        so it escaped ``request()`` un-enveloped part-way through the
        upload -- whenever any component of ``client_path`` was a
        symbolic link. That is the README's own ``/tmp`` example on
        macOS, and any symlinked mount or home directory.

        Every *other* method here returns None, a bool, an
        ``os.stat_result`` or a file handle, none of which carries a
        path for a caller to compose against; :meth:`rename` returns
        the destination it was given, which ``aioftp.Client`` discards.
        So the translation belongs here and only here -- see
        :func:`_as_given` for why the check itself stays canonical.

        Args:
            path: The directory to list.

        Returns:
            ``aioftp``'s async lister, each entry re-rooted onto
            ``path`` exactly as the caller wrote it.
        """
        target = self.contained(path)
        return _AsGivenLister(super().list(target), Path(path), target)

    async def stat(self, path: Path) -> os.stat_result:
        """Stat a contained path.

        Args:
            path: The path to stat.

        Returns:
            Its ``os.stat_result``.
        """
        return await super().stat(self.contained(path))

    async def rename(self, source: Path, destination: Path) -> Path:
        """Rename within the base, refusing either end outside it.

        Args:
            source: The path to rename.
            destination: What to rename it to.

        Returns:
            The destination.
        """
        return await super().rename(
            self.contained(source), self.contained(destination))

    # `type: ignore[override]` -- aioftp's own `AsyncPathIO._open` carries
    # the identical ignore against its `AbstractPathIO._open(self, path,
    # mode)` base: the concrete signature widens `path` to `Path` and adds
    # `**kwargs`. This override matches the class it actually extends, so
    # the incompatibility is inherited from the dependency's own hierarchy
    # and cannot be annotated away from here.
    async def _open(  # type: ignore[override]
        self,
        path: Path,
        mode: str = 'rb',
        **kwargs: Any,
    ) -> io.BytesIO:
        """Open a contained path, under the write guarantees for a write.

        The one override that does more than contain. A download's open
        is the write M18 is about, so it goes through
        :func:`~async_gateway.utils.paths.guarded_opener` -- refusing a
        symlink at the target and, by default, refusing to overwrite --
        at mode 0600 rather than at whatever ``umask`` allows. An
        upload's open is a *read* of the caller's own file and is left
        alone.

        Args:
            path: The path to open.
            mode: The mode ``aioftp`` asked for.
            kwargs: The rest of ``aioftp``'s open arguments, which it
                does not pass for a transfer and which the guarded open
                does not accept.

        Returns:
            The open file object.

        Raises:
            PathContainmentError: If the path escapes the base, or is a
                symbolic link.
            ConfigurationError: If the target exists and overwriting was
                not asked for.
        """
        target = self.contained(path)
        if not _writes(mode):
            # The parent's, decorators and all: an upload's read is
            # ordinary ``aioftp`` behaviour and its failures should
            # arrive as ``aioftp`` failures.
            #
            # `type: ignore[arg-type]` -- aioftp types the base `_open`'s
            # `mode` as a union of ~40 string *Literals*, but the value
            # arriving here is whatever `aioftp.Client` passed down, typed
            # `str` at this override's own signature (which is aioftp's
            # own shape -- see the `[override]` ignore on the def). mypy
            # cannot narrow a `str` to a Literal union, and enumerating
            # the union here would restate a dependency's private detail
            # that goes stale on its next release.
            return await super()._open(
                target, mode, **kwargs)  # type: ignore[arg-type]
        opened = asyncio.get_running_loop().run_in_executor(
            self.executor, _open_guarded, target, mode, self.overwrite)
        handle = (
            await opened if self.timeout is None
            else await asyncio.wait_for(opened, self.timeout))
        self._targets[id(handle)] = target
        return handle

    async def write(self, file: io.BytesIO, data: Any) -> int:
        """Write a block, classifying a local filesystem refusal.

        The open half of NEW-R10-1 is :meth:`_open` above; this is the
        other half, and only this one can see a full disk. ENOSPC,
        EDQUOT and EFBIG are raised by the ``write`` syscall on a file
        that opened perfectly, so no amount of care at the open can
        catch them.

        Args:
            file: The open local file.
            data: The block to write.

        Returns:
            The number of bytes written.

        Raises:
            LocalWriteError: If the local filesystem refused the write.
            ResponseTooLargeError: If the block would cross the
                transfer's byte ceiling (R14, NEW-R10-2). Charged
                *before* the write, so the bytes over the cap never
                reach the disk.
        """
        try:
            if self.budget is not None:
                self.budget.spend(len(data))
            return int(await self._classifying(super().write(file, data)))
        except BaseException:
            # M19, on the transfer protocols: what a failed write leaves
            # is a truncated file, not the caller's data, so it is
            # removed rather than orphaned. `safe_writer` gives the HTTP
            # path the same guarantee from its `except` arm; doing it
            # here is what keeps a refused download leaving *no* file on
            # all four protocols instead of a 0-byte one on two of them.
            await self._discard(file)
            raise

    async def close(self, file: io.BytesIO) -> None:
        """Close a local file, classifying a refusal from its flush.

        The third seam, and the one the write override alone misses: a
        buffered handle defers small writes, so the first syscall to
        fail can be the flush inside ``close``.

        Args:
            file: The open local file.

        Returns:
            None.

        Raises:
            LocalWriteError: If the local filesystem refused the flush.
        """
        target = self._targets.pop(id(file), None)
        try:
            await self._classifying(super().close(file))
        except BaseException:
            if target is not None:
                await _unlink(target)
            raise

    async def _discard(self, file: io.BytesIO) -> None:
        """Close and remove the partial file behind ``file``.

        Args:
            file: The open handle whose write failed.

        Returns:
            None. A close that also fails is suppressed: the write's own
            failure is the one the caller must see, and the file is
            removed either way.
        """
        target = self._targets.pop(id(file), None)
        with suppress(Exception):
            await super().close(file)
        if target is not None:
            await _unlink(target)

    async def _classifying(
        self,
        awaited: Any,
        *,
        classify: Callable[
            [Path, OSError], AsyncGatewayError] = classify_refusal,
        path: Optional[Path] = None,
    ) -> Any:
        """Await ``awaited``, typing a local filesystem refusal.

        ``aioftp`` wraps every path-layer failure in a ``PathIOError``
        carrying the original in ``reason``, so the ``OSError`` is one
        unwrap away rather than absent. Unwrapping it here is what
        makes a full disk report ``PATH`` on FTP exactly as it does on
        HTTP -- without it the wrapper reached the dispatch as an
        ``AIOFTPException`` and was classified ``TRANSPORT``, inviting
        a retry of a body this disk will refuse again.

        Args:
            awaited: The parent operation's awaitable.
            classify: Which classifier types the unwrapped ``OSError``.
                The write seams take the default;
                :meth:`mkdir` passes
                :func:`classify_mkdir_refusal`, whose ``EEXIST`` answer
                differs because ``overwrite=True`` can replace a file
                and can never create a directory over one.
            path: The path the refusal is about, for the message. The
                write seams have only ``self.base`` to name -- the
                handle ``aioftp`` hands back does not carry its path --
                so that stays the default.

        Returns:
            Whatever the parent returned.

        Raises:
            LocalWriteError: If the wrapped failure was an ``OSError``.
        """
        try:
            return await awaited
        except aioftp.PathIOError as err:
            reason = err.reason
            cause = reason[1] if reason is not None else None
            if not isinstance(cause, OSError):
                raise
            raise classify(
                self.base if path is None else path, cause) from err


def contained_path_io_factory(
    base: PathLike,
    *,
    overwrite: bool = False,
    budget: Optional[TransferBudget] = None,
) -> Any:
    """Return the ``path_io_factory`` an FTP client is built with.

    ``aioftp.BaseClient.__init__`` calls ``path_io_factory(timeout=...)``
    and keeps the result, so the base directory has to be bound into the
    callable rather than passed at call time.

    Args:
        base: The local directory the transfer is confined to.
        overwrite: Whether an existing local file may be replaced.
        budget: The bytes this transfer may write, or None for
            unbounded. Bound here rather than passed per call for the
            same reason ``base`` is, and it is what makes one ceiling
            span every file of a recursive download.

    Returns:
        A callable ``aioftp`` can use where it expects a path-IO class.
    """
    def factory(*args: Any, **kwargs: Any) -> ContainedPathIO:
        """Build the contained path layer for one client.

        Args:
            args: Positional arguments ``aioftp`` supplies.
            kwargs: Keyword arguments ``aioftp`` supplies.

        Returns:
            The bound path layer.
        """
        return ContainedPathIO(
            *args, base=base, overwrite=overwrite, budget=budget,
            **kwargs)

    return factory


class ClassifyingLocalFile(LocalFile):
    """``asyncssh``'s local file handle, with typed write failures.

    The SFTP counterpart of :meth:`ContainedPathIO.write` and
    :meth:`ContainedPathIO.close`, and it exists for exactly the reason
    those do (NEW-R10-1). :class:`ContainedLocalFS` guards the *open*;
    a full disk is not an open failure. ENOSPC, EDQUOT and EFBIG are
    raised by the ``write`` -- or by the flush inside ``close`` for a
    body small enough to sit in the buffer -- on a file that opened
    perfectly, and ``asyncssh`` passes them straight up to the dispatch,
    where an ``OSError`` matched ``(OSError, ConnectError)`` and was
    reported as ``CONNECT``: a network verdict for a local disk, with
    the retry and the breaker count that verdict carries.

    A subclass rather than a wrapper, so ``isinstance`` checks and any
    method this does not override keep asyncssh's own behaviour.

    Attributes:
        path: The local path, for the diagnostic. ``LocalFile`` keeps
            only the handle, and a message naming no file is not
            actionable.
    """

    def __init__(
        self,
        file: Any,
        path: Path,
        budget: Optional[TransferBudget] = None,
    ) -> None:
        """Wrap an open local file with its own path.

        Args:
            file: The open file object, as ``LocalFile`` takes.
            path: The path it was opened at.
            budget: The transfer's shared byte budget, or None.
        """
        super().__init__(file)
        self.path = path
        self.budget = budget

    async def write(self, data: bytes, offset: int) -> int:
        """Write ``data``, classifying a local filesystem refusal.

        Args:
            data: The bytes to write.
            offset: Where in the file to write them.

        Returns:
            The number of bytes written.

        Raises:
            LocalWriteError: If the local filesystem refused the write.
            ResponseTooLargeError: If the block would cross the
                transfer's byte ceiling (R14, NEW-R10-2). Charged
                before the write, so the excess never reaches the disk.
        """
        try:
            if self.budget is not None:
                self.budget.spend(len(data))
            return await super().write(data, offset)
        except OSError as err:
            await self._discard()
            raise classify_refusal(self.path, err) from err
        except BaseException:
            # M19 on this protocol too: a refused transfer leaves no
            # truncated file, matching what `safe_writer` guarantees the
            # HTTP path and what the FTP wrapper now guarantees its own.
            await self._discard()
            raise

    async def close(self) -> None:
        """Close the file, classifying a refusal from its flush.

        Returns:
            None.

        Raises:
            LocalWriteError: If the local filesystem refused the flush.
        """
        try:
            await super().close()
        except OSError as err:
            await _unlink(self.path)
            raise classify_refusal(self.path, err) from err

    async def _discard(self) -> None:
        """Close and remove the partial file this handle wrote.

        Returns:
            None. A close that also fails is suppressed: the write's own
            failure is the one the caller must see, and the file goes
            either way.
        """
        with suppress(Exception):
            await super().close()
        await _unlink(self.path)


class ContainedLocalFS:
    """``asyncssh``'s local filesystem, confined to one directory.

    Handed to ``SFTPClient._begin_copy`` in place of the module-level
    ``local_fs`` for a download. Every method delegates to that real
    object after routing its path through
    :func:`~async_gateway.utils.paths.under`, so the transfer behaviour
    is asyncssh's and only the containment is this library's.

    The surface is the structural protocol ``_begin_copy`` and ``_copy``
    actually use (asyncssh names it ``_SFTPFSProtocol``): ``encode``,
    ``basename``, ``compose_path``, ``limits``, ``stat``, ``setstat``,
    ``exists``, ``isdir``, ``scandir``, ``mkdir``, ``readlink``,
    ``symlink`` and ``open``. Delegating rather than subclassing keeps
    that surface visible: a future asyncssh that reaches for a method
    not listed here fails loudly with an ``AttributeError`` at the seam,
    where a subclass would silently inherit an *uncontained* one.

    Attributes:
        base: The local directory every path must resolve inside.
        overwrite: Whether an existing local file may be replaced.
    """

    #: asyncssh reads this off the filesystem object to size its reads
    #: and writes. Delegated to the real one verbatim.
    limits = local_fs.limits

    def __init__(
        self,
        base: PathLike,
        *,
        overwrite: bool = False,
        budget: Optional[TransferBudget] = None,
    ) -> None:
        """Bind a local filesystem view to one directory.

        Args:
            base: The local directory writes are confined to.
            overwrite: Whether an existing file may be replaced.
            budget: The bytes this whole transfer may write, or None
                for an unbounded one -- shared across every file of a
                recursive download.
        """
        self.base = Path(base)
        self.overwrite = overwrite
        self.budget = budget

    def contained(self, path: BytesOrPathLike) -> bytes:
        """Return ``path`` proven inside :attr:`base`, byte-encoded.

        Args:
            path: Whatever asyncssh is about to act on. Bytes, in
                practice: its filesystem protocol speaks them
                throughout.

        Returns:
            The contained path, byte-encoded.

        Raises:
            PathContainmentError: If it escapes the base.
        """
        return os.fsencode(under(self.base, path))

    @staticmethod
    def basename(path: bytes) -> bytes:
        """Return the final component of a path.

        Pure string work, so it is delegated unchanged.

        Args:
            path: The path to take the basename of.

        Returns:
            Its final component.
        """
        return local_fs.basename(path)

    def encode(self, path: BytesOrPathLike) -> bytes:
        """Encode a path the way the local filesystem encodes one.

        Args:
            path: The path to encode.

        Returns:
            The encoded path. Not contained: asyncssh encodes the
            destination it was *given* before any entry name has been
            composed onto it, and containing that would refuse the base
            against itself.
        """
        return local_fs.encode(path)

    def compose_path(
        self,
        path: bytes,
        parent: Optional[bytes] = None,
    ) -> bytes:
        """Join a name onto a parent directory.

        Args:
            path: The name to join.
            parent: The directory to join it onto, or None.

        Returns:
            The composed path, uncontained -- every method that *acts*
            on it contains it first, which is what keeps a single
            refusal point rather than one per composition.
        """
        return local_fs.compose_path(path, parent)

    async def stat(
        self,
        path: bytes,
        *,
        follow_symlinks: bool = True,
    ) -> asyncssh.SFTPAttrs:
        """Return the attributes of a contained local path.

        Args:
            path: The path to stat.
            follow_symlinks: Whether to follow a final symlink.

        Returns:
            The attributes asyncssh reports.
        """
        return await local_fs.stat(
            self.contained(path), follow_symlinks=follow_symlinks)

    async def setstat(
        self,
        path: bytes,
        attrs: asyncssh.SFTPAttrs,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        """Apply the *safe* attributes to a contained local path.

        Reached only for ``preserve=True``, and it is a write: a
        ``chmod`` through an escaping path is the same escape as a
        ``write`` through one. Containment alone is not enough, though,
        because the danger here is not *where* the call lands but
        *what* it sets.

        **The ownership and permission bits are dropped.** Every local
        file this library creates is opened at
        :data:`~async_gateway.utils.paths.FILE_MODE` -- 0600, owner-only
        -- and that is a security property of this release rather than
        an accident of ``umask`` (M18). ``asyncssh``'s ``_setstat``
        ends with an ``os.chmod`` to whatever ``attrs.permissions``
        carries, so a ``preserve=True`` download silently reverted it:
        measured against a real loopback SFTP server with the remote
        file at 0777, the **local** file was left at 0o777, on both a
        single-file and a recursive ``get``. That hands a remote server
        the power to decide the mode of a file on this machine, which
        is precisely the authority the guarded open exists to deny it.

        Intersecting with 0600 was considered and rejected: it is the
        same answer as dropping the field for every mode a caller would
        actually preserve, and it invites the reading that *some*
        server-supplied bit is honoured. Refusing the whole call was
        rejected too -- ``preserve=True`` is a legitimate option whose
        timestamps this library has no reason to withhold, and turning
        a working download into an error would cost a caller a feature
        to fix a bit they never asked about.

        **The timestamps are preserved, and are the point.** ``atime``
        and ``mtime`` describe the remote file and grant nobody
        anything: the worst a hostile server can do with them is date a
        file it already chose the contents of. ``size`` is kept for the
        same reason it exists here at all -- ``_setstat`` truncates to
        it, which is asyncssh completing its own copy.

        The other three protocols have no equivalent to reconcile:
        neither ``aioftp`` nor this library's HTTP write path applies
        any server-supplied attribute to a local file, so 0600 already
        held there unconditionally. This is what makes all four agree.

        Args:
            path: The path to modify.
            attrs: The attributes asyncssh read off the remote file.
            follow_symlinks: Whether to follow a final symlink.

        Returns:
            None.
        """
        await local_fs.setstat(
            self.contained(path),
            _without_ownership(attrs),
            follow_symlinks=follow_symlinks)

    async def exists(self, path: bytes) -> bool:
        """Report whether a contained local path exists.

        Args:
            path: The path to test.

        Returns:
            True when it exists.
        """
        return await local_fs.exists(self.contained(path))

    async def isdir(self, path: bytes) -> bool:
        """Report whether a contained local path is a directory.

        Args:
            path: The path to test.

        Returns:
            True when it is a directory.
        """
        return await local_fs.isdir(self.contained(path))

    async def scandir(self, path: bytes) -> AsyncIterator[Any]:
        """Iterate the entries of a contained local directory.

        Args:
            path: The directory to scan.

        Yields:
            One ``SFTPName`` per entry, as asyncssh does.
        """
        async for entry in local_fs.scandir(self.contained(path)):
            yield entry

    async def mkdir(self, path: bytes) -> None:
        """Create a directory, inside the base or not at all.

        The refusal is **typed**, for the same reason
        :meth:`ContainedPathIO.mkdir` types its own: ``asyncssh``'s
        ``LocalFS.mkdir`` calls ``os.mkdir`` bare, so a local
        filesystem refusal left here as a raw ``OSError`` reached the
        SFTP dispatch's residual-``OSError`` row and was reported by
        the *dispatch's* judgement rather than by this seam's. That the
        two happened to agree on ``PATH`` was luck, not construction --
        the FTP sibling's identical refusal answered ``CONFIG``, and
        neither wrapper decided anything.

        Args:
            path: The directory to create.

        Returns:
            None.

        Raises:
            PathContainmentError: If the directory escapes the base, or
                a symbolic link is in the way.
            LocalWriteError: If the local filesystem refused it -- a
                missing ancestor, an unwritable parent, or something
                already occupying the path.
        """
        target = self.contained(path)
        try:
            await local_fs.mkdir(target)
        except OSError as err:
            raise classify_mkdir_refusal(
                Path(_shown(target)), err) from err

    async def readlink(self, path: bytes) -> bytes:
        """Read the target of a contained local symbolic link.

        Args:
            path: The link to read.

        Returns:
            Its target.
        """
        return await local_fs.readlink(self.contained(path))

    async def symlink(self, oldpath: bytes, newpath: bytes) -> None:
        """Refuse to create a local symbolic link.

        Reached when the remote side reports an entry as a symlink, in
        which case asyncssh recreates it locally with the **server's**
        target string. Containing ``newpath`` would stop the link
        landing outside the base, and would not stop it *pointing*
        outside -- and a symlink inside the download directory aimed at
        ``/etc/passwd`` is a next-write escape that this module's own
        ``O_NOFOLLOW`` would then have to catch. Refusing is the
        narrower guarantee and the one R22 asks for.

        Args:
            oldpath: The target the server supplied.
            newpath: Where the link would be created.

        Returns:
            None; this always raises.

        Raises:
            PathContainmentError: Always.
        """
        raise PathContainmentError(
            f'refusing to create the server-supplied symbolic link '
            f'{_shown(newpath)!r} -> {_shown(oldpath)!r} '
            f'inside {str(self.base)!r}')

    async def open(
        self,
        path: bytes,
        mode: str,
        block_size: int = -1,
    ) -> LocalFile:
        """Open a contained local path, guarded when it is a write.

        The open runs in a thread. asyncssh's own ``LocalFS.open`` calls
        the builtin inline, on the event loop -- which is R20's ban, and
        it is invisible to the package AST scan because it happens in a
        dependency. Moving it off the loop here is a property this
        wrapper adds rather than one it preserves.

        Args:
            path: The path to open.
            mode: The mode asyncssh asked for.
            block_size: Accepted and ignored, as asyncssh's own does.

        Returns:
            The ``LocalFile`` wrapper asyncssh expects.

        Raises:
            PathContainmentError: If the path escapes the base, or is a
                symbolic link.
            ConfigurationError: If the target exists and overwriting was
                not asked for.
        """
        target = Path(_shown(self.contained(path)))
        handle = await asyncio.to_thread(
            _open_guarded, target, mode, self.overwrite)
        if _writes(mode):
            await asyncio.to_thread(_make_sparse, handle)
        return ClassifyingLocalFile(handle, target, self.budget)


#: The ``SFTPAttrs`` fields a **remote server** must not get to set on a
#: local file. ``permissions`` is the finding: ``_setstat`` chmods to it,
#: undoing the 0600 every guarded open establishes. The four identity
#: fields ride with it because ``_setstat`` chowns to them by the same
#: logic and the same argument applies -- a download is not an
#: invitation to reassign a local file's owner. Dropping a field means
#: setting it to None, which is how ``SFTPAttrs`` spells "unset" and is
#: what makes ``_setstat`` skip the call entirely.
UNPRESERVED_ATTRS: Final[Tuple[str, ...]] = (
    'permissions',
    'uid',
    'gid',
    'owner',
    'group',
)


def _without_ownership(attrs: asyncssh.SFTPAttrs) -> asyncssh.SFTPAttrs:
    """Return ``attrs`` with every field in :data:`UNPRESERVED_ATTRS` unset.

    A copy, never a mutation: the object belongs to ``asyncssh``'s
    ``_copy``, which reads it again after the ``setstat`` to log what it
    preserved, and editing it in place would make that log lie.

    Built by reading ``SFTPAttrs.__slots__`` rather than by naming the
    fields to keep, so a field a future ``asyncssh`` adds is carried
    through instead of silently dropped -- the failure this direction
    can produce is a preserved timestamp going missing, while the other
    direction produces exactly the mode-widening being fixed.

    Args:
        attrs: What ``asyncssh`` read off the remote file.

    Returns:
        The same attributes with ownership and permissions unset.
    """
    fields = {
        name: getattr(attrs, name) for name in type(attrs).__slots__
    }
    fields.update({name: None for name in UNPRESERVED_ATTRS})
    return asyncssh.SFTPAttrs(**fields)


def _make_sparse(handle: io.BytesIO) -> None:
    """Apply asyncssh's sparse-file hint to a freshly opened file.

    ``LocalFS.open`` calls ``make_sparse_file`` for a write mode. It is
    a no-op everywhere but Windows, and it is reproduced rather than
    skipped because this wrapper's contract is "asyncssh's behaviour
    plus containment" -- dropping a step because it currently does
    nothing on this platform is how the two drift apart.

    Args:
        handle: The open file object.

    Returns:
        None.
    """
    asyncssh.sftp.make_sparse_file(handle)


#: The download modes, and whether each expands a glob in its remote
#: operand. Read off asyncssh's own call sites: ``get`` passes
#: ``expand_glob=False`` to ``_begin_copy`` and ``mget`` passes True.
GLOB_EXPANDING: Final[Mapping[str, bool]] = MappingProxyType({
    'get': False,
    'mget': True,
})

#: The parameters ``_begin_copy`` takes after its four path operands and
#: its ``copy_type``/``expand_glob`` pair, in the order it takes them.
#: They are exactly the keyword-only parameters of ``get`` -- which is
#: what lets a caller's options be validated against the *public*
#: signature and then forwarded to the private one positionally.
COPY_OPTIONS: Final[Tuple[str, ...]] = (
    'preserve',
    'recurse',
    'follow_symlinks',
    'sparse',
    'block_size',
    'max_requests',
    'progress_handler',
    'error_handler',
)


async def contained_download(
    sftp: Any,
    mode: str,
    remote_paths: Any,
    local_path: PathLike,
    *,
    base: PathLike,
    overwrite: bool = False,
    budget: Optional[TransferBudget] = None,
    **options: Any,
) -> None:
    """Run an SFTP download whose local destination cannot be escaped.

    ``asyncssh.SFTPClient.get`` reads the module-level ``local_fs`` as a
    global, so there is no per-call or per-client way to substitute a
    contained one -- and patching the module attribute would be
    process-wide, which is unusable in a library whose documented usage
    is an ``asyncio.gather`` of concurrent calls. ``_begin_copy``, one
    frame below, takes the destination filesystem as an **argument**,
    so that is where the substitution happens.

    Reaching for a private method is a real cost and is taken
    deliberately: the alternative is leaving R22's named threat model
    undefended on the protocol it was written for. It is bounded by the
    ``asyncssh>=2.24.0,<3`` pin, by validating the caller's options
    against the **public** ``get`` signature first -- so an option
    asyncssh would refuse is still refused, with the same ``TypeError``,
    from the same place -- and by :func:`_require_begin_copy`, which
    fails loudly rather than silently downloading uncontained if a
    future release moves it.

    Args:
        sftp: The open ``asyncssh`` SFTP client.
        mode: ``'get'`` or ``'mget'``.
        remote_paths: The remote source operand, as the caller gave it.
        local_path: The local destination operand.
        base: The local directory the transfer is confined to.
        overwrite: Whether an existing local file may be replaced.
        budget: The bytes this transfer may write locally, or None for
            an unbounded one (R14, NEW-R10-2).
        options: The caller's ``additional_arguments``.

    Returns:
        None, as ``asyncssh`` does.

    Raises:
        TypeError: If ``options`` names something ``get`` does not
            accept -- raised by binding the real public signature, so
            the message and the class are asyncssh's own.
        PathContainmentError: If any path the transfer composes escapes
            ``base``, or is a symbolic link.
        ConfigurationError: If a destination exists and ``overwrite``
            is not True.
    """
    bound = inspect.signature(asyncssh.SFTPClient.get).bind(
        sftp, remote_paths, local_path, **options)
    bound.apply_defaults()
    settled = bound.arguments

    await _require_begin_copy(sftp)(
        sftp,
        ContainedLocalFS(base, overwrite=overwrite, budget=budget),
        remote_paths,
        local_path,
        mode,
        GLOB_EXPANDING[mode],
        *(settled[name] for name in COPY_OPTIONS),
    )


def _require_begin_copy(sftp: Any) -> Any:
    """Return ``sftp._begin_copy``, or refuse to transfer at all.

    The seam containment depends on. If a future ``asyncssh`` renames
    or removes it, the honest outcome is a refused call -- not a
    download that quietly runs through the uncontained ``local_fs``
    again, which is the failure this whole module exists to prevent.

    Args:
        sftp: The SFTP client.

    Returns:
        The bound ``_begin_copy`` coroutine function.

    Raises:
        PathContainmentError: If the client has no ``_begin_copy``.
    """
    begin_copy = getattr(sftp, '_begin_copy', None)
    if begin_copy is None:
        raise PathContainmentError(
            'this asyncssh release does not expose the seam local '
            'download containment is installed at '
            '(SFTPClient._begin_copy); refusing the transfer rather '
            'than running it unconfined')
    return begin_copy


def local_base(path: PathLike) -> Path:
    """Return the directory a transfer's local side is confined to.

    The caller's own local operand, made absolute -- **not** its
    parent. That path is the whole local side of the transfer as the
    caller described it: the tree's root for a directory download, the
    file itself for a single-file one. Either way it is the boundary
    they named, and a name the *server* supplied has no business
    resolving outside it.

    Taking the parent instead would be a level too generous, and
    measurably so: with the parent as the base, a hostile entry name
    of ``../victimdir/OWNED`` landed as ``downloads/victimdir/OWNED``
    -- outside the tree the caller named, inside the check.

    Args:
        path: The local operand from ``protocol_info``.

    Returns:
        The absolute path, uncanonicalised. Canonicalisation happens
        inside :func:`~async_gateway.utils.paths.resolve_within`, which
        is where the base and the candidate are compared as locations.
    """
    return Path(path).absolute()

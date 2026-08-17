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
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    AsyncIterator,
    Final,
    Mapping,
    Optional,
    Text,
    Tuple,
)

import aioftp

import asyncssh
from asyncssh.sftp import LocalFile, local_fs

from async_gateway.utils.exceptions import PathContainmentError
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
WRITING_MODES: Final[frozenset[Text]] = frozenset({'w', 'x', 'a'})


def _writes(mode: Text) -> bool:
    """Report whether an open mode creates or modifies a file.

    Args:
        mode: The mode string, as passed to ``open``.

    Returns:
        True when the open is a write.
    """
    return bool(mode) and mode[0] in WRITING_MODES


def _shown(path: BytesOrPathLike) -> Text:
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


def _open_guarded(path: Path, mode: Text, overwrite: bool) -> io.BytesIO:
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
        OSError: For every other reason the open failed -- a missing
            parent, a permission failure -- reported as itself.
    """
    # type: ignore[return-value] -- the declared return is `io.BytesIO`
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
        refusal = classify_refusal(path, err)
        if refusal is None:
            raise
        raise refusal from err


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
        **kwargs: Any,
    ) -> None:
        """Bind this path layer to one directory.

        Args:
            args: Positional arguments for ``AsyncPathIO``.
            base: The local directory writes are confined to.
            overwrite: Whether an existing file may be replaced.
            kwargs: Keyword arguments for ``AsyncPathIO``; ``aioftp``
                passes ``timeout``.
        """
        super().__init__(*args, **kwargs)
        self.base = Path(base)
        self.overwrite = overwrite

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
        """Create a directory, inside the base or not at all.

        The directory half of the escape, and it lands *before* the
        file half: a hostile entry name that is a directory is
        ``mkdir``'d first and written second, so refusing here stops
        the tree being created at all rather than only its leaves.

        One path outside the base is permitted, by **exact match**: the
        base's own parent. ``aioftp.Client.download`` creates the
        destination's parent before writing a single file, and for a
        file download the destination *is* the base -- so refusing its
        parent would refuse every single-file download. The exception is
        safe because it is not a prefix rule: a server-derived name can
        satisfy it only by naming exactly the one directory the caller
        already named the inside of, where ``exist_ok`` makes it a
        no-op. ``base/../victimdir`` is a different path and is refused.

        Args:
            path: The directory to create.
            parents: Create missing parents.
            exist_ok: Do not fail when it is already there.

        Returns:
            None.
        """
        if Path(_shown(path)).absolute() == self.base.parent:
            await aioftp.pathio.AsyncPathIO.mkdir(
                self, self.base.parent, parents=parents, exist_ok=True)
            return
        await super().mkdir(
            self.contained(path), parents=parents, exist_ok=exist_ok)

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
        """List a directory, inside the base or not at all.

        Args:
            path: The directory to list.

        Returns:
            ``aioftp``'s async lister over the contained path.
        """
        return super().list(self.contained(path))

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

    # type: ignore[override] -- aioftp's own `AsyncPathIO._open` carries
    # the identical ignore against its `AbstractPathIO._open(self, path,
    # mode)` base: the concrete signature widens `path` to `Path` and adds
    # `**kwargs`. This override matches the class it actually extends, so
    # the incompatibility is inherited from the dependency's own hierarchy
    # and cannot be annotated away from here.
    async def _open(  # type: ignore[override]
        self,
        path: Path,
        mode: Text = 'rb',
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
            return await super()._open(target, mode, **kwargs)
        opened = asyncio.get_running_loop().run_in_executor(
            self.executor, _open_guarded, target, mode, self.overwrite)
        if self.timeout is None:
            return await opened
        return await asyncio.wait_for(opened, self.timeout)


def contained_path_io_factory(
    base: PathLike,
    *,
    overwrite: bool = False,
) -> Any:
    """Return the ``path_io_factory`` an FTP client is built with.

    ``aioftp.BaseClient.__init__`` calls ``path_io_factory(timeout=...)``
    and keeps the result, so the base directory has to be bound into the
    callable rather than passed at call time.

    Args:
        base: The local directory the transfer is confined to.
        overwrite: Whether an existing local file may be replaced.

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
            *args, base=base, overwrite=overwrite, **kwargs)

    return factory


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

    def __init__(self, base: PathLike, *, overwrite: bool = False) -> None:
        """Bind a local filesystem view to one directory.

        Args:
            base: The local directory writes are confined to.
            overwrite: Whether an existing file may be replaced.
        """
        self.base = Path(base)
        self.overwrite = overwrite

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
        """Apply attributes to a contained local path.

        Reached only for ``preserve=True``, and it is a write: a
        ``chmod`` through an escaping path is the same escape as a
        ``write`` through one.

        Args:
            path: The path to modify.
            attrs: The attributes to apply.
            follow_symlinks: Whether to follow a final symlink.

        Returns:
            None.
        """
        await local_fs.setstat(
            self.contained(path), attrs, follow_symlinks=follow_symlinks)

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

        Args:
            path: The directory to create.

        Returns:
            None.
        """
        await local_fs.mkdir(self.contained(path))

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
        mode: Text,
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
        return LocalFile(handle)


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
GLOB_EXPANDING: Final[Mapping[Text, bool]] = MappingProxyType({
    'get': False,
    'mget': True,
})

#: The parameters ``_begin_copy`` takes after its four path operands and
#: its ``copy_type``/``expand_glob`` pair, in the order it takes them.
#: They are exactly the keyword-only parameters of ``get`` -- which is
#: what lets a caller's options be validated against the *public*
#: signature and then forwarded to the private one positionally.
COPY_OPTIONS: Final[Tuple[Text, ...]] = (
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
    mode: Text,
    remote_paths: Any,
    local_path: PathLike,
    *,
    base: PathLike,
    overwrite: bool = False,
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
        ContainedLocalFS(base, overwrite=overwrite),
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

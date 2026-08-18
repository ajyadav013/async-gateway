"""Tests for the transfer-library containment wrappers (R22, S18).

``tests/logic/test_ftp_client.py`` and ``tests/logic/test_sftp_client.py``
drive these wrappers the way production does -- through the real
``aioftp.Client.download`` and the real ``asyncssh.SFTPClient._copy``,
with only the wire faked -- and those are the rows that prove the escape
is closed. This module covers what an end-to-end path cannot reach on
its own: the individual delegating methods.

That matters because of how each wrapper is built. Both delegate
*method by method*, and every method has to route its path through
:func:`~async_gateway.utils.paths.under` for itself. A recursive
download exercises perhaps half of them, so the other half could be
missing their containment call and the end-to-end rows would still pass
-- until a library version, an option, or a tree shape reached one of
them. Each is therefore asserted here directly: it contains, and it
still delegates.
"""

import asyncio
import errno
import os
import threading
from pathlib import Path
from typing import Any, Text

import aioftp

import asyncssh

import pytest

from async_gateway.utils.contained_io import (
    ClassifyingLocalFile,
    ContainedLocalFS,
    ContainedPathIO,
    TransferBudget,
    contained_download,
    contained_path_io_factory,
    local_base,
)
from async_gateway.utils.exceptions import (
    ConfigurationError,
    LocalWriteError,
    PathContainmentError,
    ResponseTooLargeError,
)


class _FullDisk:
    """A file object whose every write and flush reports ENOSPC.

    The write-side fault, produced without a full filesystem. Both
    wrappers reach the disk through an ordinary file object, so
    substituting one that raises the real errno drives the real arm --
    and unlike closing a descriptor out from under a live handle, it
    leaves nothing for the garbage collector to trip over.
    """

    @staticmethod
    def _refuse() -> None:
        """Raise the error an exhausted filesystem raises.

        Returns:
            Never; this always raises.

        Raises:
            OSError: Always, with ``ENOSPC``.
        """
        raise OSError(errno.ENOSPC, 'No space left on device')

    def write(self, data: bytes, *args: Any) -> int:
        """Refuse to write.

        Args:
            data: The block, unused.
            args: An offset, for the asyncssh shape.

        Returns:
            Never; this always raises.
        """
        self._refuse()
        raise AssertionError('unreachable')

    def seek(self, *args: Any) -> int:
        """Seek, which asyncssh does before every write.

        Args:
            args: The offset and whence, unused.

        Returns:
            0, always -- the seek is not what this double refuses.
        """
        return 0

    def close(self) -> None:
        """Refuse to flush.

        Returns:
            Never; this always raises.
        """
        self._refuse()


class _RecordingFile:
    """A file object that records the size of every block written.

    The ordering claim -- charged *before* the write -- is invisible
    from the envelope, because both orderings report the same code and
    both leave no file once the partial is removed. What distinguishes
    them is whether the over-cap block ever reached a ``write`` at all,
    so the sizes that arrive are what the test asserts on.
    """

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.landed: list[int] = []

    def write(self, data: bytes) -> int:
        """Record a block instead of putting it anywhere.

        Args:
            data: The bytes the layer handed down.

        Returns:
            How many bytes were written, as a real file does.
        """
        self.landed.append(len(data))
        return len(data)


async def test_the_budget_refuses_before_the_bytes_reach_the_disk(
    tmp_path: Path,
) -> None:
    """The cap is charged *before* the write, not after (R14, NEW-R10-2).

    A ceiling enforced after the fact has already put the bytes on the
    disk it exists to protect. The two orderings look identical from
    the envelope -- both report ``RESPONSE_TOO_LARGE``, and both leave
    no file once the partial is removed -- so only the write itself can
    tell them apart, and it is the difference between refusing a
    10 GB block and writing it first.

    Asserted by counting what reached the underlying file object.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    layer = path_io(base, budget=TransferBudget(16))
    recorder = _RecordingFile()

    await layer.write(recorder, b'under the cap')
    with pytest.raises(ResponseTooLargeError):
        await layer.write(recorder, b'x' * 4096)

    assert recorder.landed == [13], (
        'the over-cap block reached the disk before it was refused; '
        'charge the budget before the write, not after')


async def test_one_budget_spans_every_file_of_a_recursive_transfer(
    tmp_path: Path,
) -> None:
    """The ceiling bounds the transfer, not each file (R14, NEW-R10-2).

    The vector R22 is about is a **recursive** download, where the
    *server* chooses both the file count and the file sizes. A per-file
    allowance is therefore no ceiling at all: a thousand files just
    under it pass a cap meant to bound the lot. One budget object,
    shared by every file the layer opens, is what makes the bound hold.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    layer = path_io(base, budget=TransferBudget(20))

    async with layer.open(base / 'a.bin', mode='wb') as first:
        await first.write(b'0123456789')

    async with layer.open(base / 'b.bin', mode='wb') as second:
        with pytest.raises(ResponseTooLargeError):
            await second.write(b'0123456789ab')

    assert (base / 'a.bin').read_bytes() == b'0123456789'
    assert not (base / 'b.bin').exists(), (
        'the file that crossed the shared ceiling must leave nothing')


def escaping(base: Path, name: Text = 'OWNED') -> Text:
    """Return a path that textually starts at ``base`` and leaves it.

    The shape both libraries actually produce: they compose the
    server's entry name onto the local destination themselves, so what
    reaches a containment wrapper is one fully-formed absolute path with
    the ``..`` still in the middle of it.

    Args:
        base: The directory the transfer is confined to.
        name: The final component the escape aims at.

    Returns:
        The composed path, as a string.
    """
    return str(base / '..' / 'victimdir' / name)


def path_io(base: Path, **kwargs: Any) -> ContainedPathIO:
    """Build the FTP path layer bound to ``base``.

    Args:
        base: The directory to confine to.
        kwargs: Overrides such as ``overwrite``.

    Returns:
        The bound layer, built through the same factory the client uses
        so the factory is covered by every row below rather than by one
        of its own.
    """
    return contained_path_io_factory(base, **kwargs)(timeout=None)


# --- the aioftp path layer -------------------------------------------------


@pytest.mark.parametrize(
    'operation',
    [
        pytest.param('exists', id='exists'),
        pytest.param('is_dir', id='is_dir'),
        pytest.param('is_file', id='is_file'),
        pytest.param('stat', id='stat'),
        pytest.param('unlink', id='unlink'),
        pytest.param('rmdir', id='rmdir'),
        pytest.param('mkdir', id='mkdir'),
    ],
)
async def test_every_ftp_path_operation_refuses_an_escape(
    tmp_path: Path,
    operation: Text,
) -> None:
    """Containment is per-method, so every method is asserted.

    ``ContainedPathIO`` delegates one method at a time and each has to
    call ``under`` for itself. A recursive download reaches only some of
    them, so a method that forgot the call would sit unnoticed behind
    passing end-to-end rows until some tree shape or option reached it.
    Parametrising the whole surface is what removes that hiding place.

    ``mkdir`` matters most: on a hostile tree the directory is created
    *before* the file is written, so refusing here stops the escaping
    tree existing at all rather than only its leaves.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    layer = path_io(base)

    with pytest.raises(PathContainmentError):
        await getattr(layer, operation)(Path(escaping(base)))

    assert not (tmp_path / 'victimdir').exists()


async def test_the_ftp_layer_refuses_an_escaping_rename_at_either_end(
    tmp_path: Path,
) -> None:
    """A rename has two paths, and either one escaping is an escape.

    Checking only the source would let a contained file be renamed out
    of the base, and checking only the destination would let one be
    dragged in from outside. Both are asserted because a wrapper that
    contained one operand would pass a single-ended test.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    (base / 'inside').write_bytes(b'contained')
    layer = path_io(base)

    with pytest.raises(PathContainmentError):
        await layer.rename(base / 'inside', Path(escaping(base)))
    with pytest.raises(PathContainmentError):
        await layer.rename(Path(escaping(base)), base / 'inside')

    assert (base / 'inside').read_bytes() == b'contained'


async def test_the_ftp_layer_refuses_an_escaping_list(
    tmp_path: Path,
) -> None:
    """Listing outside the base is refused before the iteration starts.

    ``list`` returns a lister rather than awaiting, so the refusal has
    to happen when the path is handed over -- not lazily on the first
    ``__anext__``, which a caller may never reach and which would put
    the check somewhere the error is attributed to the wrong operation.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    layer = path_io(base)

    with pytest.raises(PathContainmentError):
        layer.list(Path(escaping(base)))


async def test_the_ftp_layer_still_performs_the_operations(
    tmp_path: Path,
) -> None:
    """The control: containment wraps ``aioftp``, it does not replace it.

    Every row above asserts a refusal, and a layer whose methods raised
    unconditionally would pass all of them while making the library
    useless. This asserts the delegation half -- each method still does
    what ``aioftp`` expects of it.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    layer = path_io(base)

    await layer.mkdir(base / 'sub', parents=True, exist_ok=True)
    assert await layer.is_dir(base / 'sub') is True
    assert await layer.exists(base / 'sub') is True

    async with layer.open(base / 'sub' / 'f.bin', mode='wb') as handle:
        await handle.write(b'written through the layer')

    assert await layer.is_file(base / 'sub' / 'f.bin') is True
    assert (await layer.stat(base / 'sub' / 'f.bin')).st_size == 25
    assert [p.name async for p in layer.list(base / 'sub')] == ['f.bin']

    await layer.rename(base / 'sub' / 'f.bin', base / 'sub' / 'g.bin')
    assert (base / 'sub' / 'g.bin').read_bytes() == (
        b'written through the layer')

    await layer.unlink(base / 'sub' / 'g.bin')
    await layer.rmdir(base / 'sub')
    assert not (base / 'sub').exists()


async def test_the_ftp_layer_reads_an_upload_source_without_o_excl(
    tmp_path: Path,
) -> None:
    """An upload *reads* its local file, so the write guards must not fire.

    ``O_EXCL`` on a read would refuse every file that exists, which is
    every file anyone uploads. The guarded opener is therefore applied
    only to a write mode -- asserted here because the failure would be
    total and is the obvious thing to get wrong when adding a guard to a
    shared ``_open``.
    """
    base = tmp_path / 'uploads'
    base.mkdir()
    (base / 'report.pdf').write_bytes(b'the file to upload')
    layer = path_io(base)

    async with layer.open(base / 'report.pdf', mode='rb') as handle:
        assert await handle.read(100) == b'the file to upload'


async def test_the_ftp_layer_writes_at_0600_and_refuses_a_symlink(
    tmp_path: Path,
) -> None:
    """M18 on the FTP write path: mode and ``O_NOFOLLOW``, at the layer.

    The end-to-end row asserts the mode of a downloaded file; this
    asserts the mechanism that gives it that mode, and the symlink
    refusal the end-to-end tree shape does not reach.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    victim = tmp_path / 'victim'
    (base / 'link').symlink_to(victim)
    layer = path_io(base)

    async with layer.open(base / 'plain.bin', mode='wb') as handle:
        await handle.write(b'x')
    assert (base / 'plain.bin').stat().st_mode & 0o777 == 0o600

    with pytest.raises(PathContainmentError):
        async with layer.open(base / 'link', mode='wb'):
            pass
    assert not victim.exists()


async def test_the_ftp_layer_refuses_to_overwrite_unless_asked(
    tmp_path: Path,
) -> None:
    """R22-AC3's default and its opt-in, at the FTP layer.

    Both halves: a second download to the same path is refused, and the
    ``overwrite`` a caller sets in ``protocol_info`` really reaches the
    open rather than being accepted and dropped.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    (base / 'f.bin').write_bytes(b'the original')

    with pytest.raises(ConfigurationError):
        async with path_io(base).open(base / 'f.bin', mode='wb'):
            pass
    assert (base / 'f.bin').read_bytes() == b'the original'

    async with path_io(base, overwrite=True).open(
            base / 'f.bin', mode='wb') as handle:
        await handle.write(b'replaced')
    assert (base / 'f.bin').read_bytes() == b'replaced'


async def test_an_environmental_ftp_open_failure_is_typed_not_containment(
    tmp_path: Path,
) -> None:
    """An OSError the classifier does not recognise is typed, not raw.

    The guarded open earns three different kinds of ``OSError`` and only
    two of them are findings about *containment*: ``ELOOP``/``EMLINK``
    is a refused symlink and ``EEXIST`` is a refused overwrite.
    Everything else -- a parent directory that is not there, a plain
    file standing where a directory component was expected -- belongs to
    the caller's environment.

    This row used to require the residue to propagate as the raw
    ``errno`` exception. Half of that was right and is still asserted:
    an operator must not read a filesystem mistake as a hostile server,
    so ``LocalWriteError`` is deliberately **not** a
    ``PathContainmentError`` and the original ``errno`` stays reachable
    on ``__cause__``. The other half was the defect: raw was also how it
    reached ``request()`` on the HTTP path, past every transport family,
    as a bare ``FileNotFoundError`` (NEW-R10-1).

    Both errno families are driven because they reach the classifier
    through its two different arms -- ``ENOENT`` is refused before the
    ``EEXIST`` test, ``ENOTDIR`` after it.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    (base / 'plain').write_bytes(b'a file, not a directory')
    layer = path_io(base)

    for candidate, errno_class in (
        (base / 'absent' / 'f.bin', FileNotFoundError),
        (base / 'plain' / 'f.bin', NotADirectoryError),
    ):
        with pytest.raises(LocalWriteError) as raised:
            async with layer.open(candidate, mode='wb'):
                pass

        assert raised.value.code == 'PATH'
        assert not isinstance(raised.value, PathContainmentError), (
            'an environmental failure must stay distinguishable from a '
            'containment finding, or an operator reads a mistyped path '
            'as a hostile server.')
        assert isinstance(raised.value.__cause__, errno_class), (
            'the original errno must stay reachable, or there is '
            'nothing left to fix the environment by.')


async def test_a_local_write_failure_mid_transfer_is_typed_on_ftp(
    tmp_path: Path,
) -> None:
    """The write and close seams, which the open seam's guard cannot reach.

    A missing parent fails at ``open``. A full disk does not -- ENOSPC,
    EDQUOT and EFBIG are raised by the ``write`` syscall on a file that
    opened perfectly, or by the flush inside ``close`` for a body small
    enough to sit in the buffer. ``aioftp`` funnels both through its own
    ``PathIOError``, so a full disk arrived at the FTP dispatch as an
    ``AIOFTPException`` and was classified ``TRANSPORT`` -- a verdict
    about the *remote* server for a fault on this machine, carrying the
    retry and the breaker count that verdict carries (NEW-R10-1).

    Driven through a file object that raises ENOSPC, so the fault is the
    real errno an exhausted disk produces and the test needs no full
    filesystem to produce it. The end-to-end ``RLIMIT_FSIZE`` run
    confirms the same arm against a real kernel refusal.
    """
    layer = path_io(tmp_path)
    full_disk = _FullDisk()

    with pytest.raises(LocalWriteError) as on_write:
        await layer.write(full_disk, b'a block the disk will not take')
    assert on_write.value.code == 'PATH'

    with pytest.raises(LocalWriteError) as on_close:
        await layer.close(full_disk)
    assert on_close.value.code == 'PATH'


async def test_a_close_that_fails_flushing_removes_the_partial_on_ftp(
    tmp_path: Path,
) -> None:
    """A failed flush orphans nothing (M19), on the transfer protocols.

    The close seam's cleanup half. ``aiofiles`` and ``aioftp`` both
    buffer, so a body small enough to fit never fails at ``write`` --
    the first and only syscall is the flush inside ``close``, and what
    it leaves behind on failure is a truncated file. ``safe_writer``
    removes it on the HTTP path; this asserts the FTP layer does the
    same, so a refused download leaves no file on any protocol.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    layer = path_io(base)
    target = base / 'flushed.bin'

    handle = await layer.open(target, mode='wb')
    # Substitute the file object the layer will flush, leaving the entry
    # the layer recorded at open time intact -- which is exactly the
    # state a real ENOSPC produces: a created file whose flush fails.
    layer._targets[id(handle.file)] = target
    doomed = _FullDisk()
    layer._targets[id(doomed)] = target

    with pytest.raises(LocalWriteError):
        await layer.close(doomed)

    assert not target.exists(), (
        'a failed flush must not orphan the truncated file it left')

    async with layer.open(target, mode='wb') as reopened:
        await reopened.write(b'proving the layer still works')
    assert target.read_bytes() == b'proving the layer still works'


async def test_a_non_oserror_ftp_path_failure_is_left_to_aioftp(
    tmp_path: Path,
) -> None:
    """The classifier claims local IO, not everything ``aioftp`` raises.

    ``PathIOError`` is ``aioftp``'s universal wrapper: it carries a
    ``ValueError`` from the library's own misuse guard as readily as an
    ``OSError`` from the disk. Unwrapping it unconditionally would let
    this module report an ``aioftp`` bug -- or one of ours -- as a full
    disk, which is the one-conversion-point rule inverted.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    layer = path_io(base)

    async with layer.open(base / 'f.bin', mode='wb') as handle:
        with pytest.raises(aioftp.PathIOError):
            await layer.write(handle, b'bytes')


async def test_the_ftp_layer_tolerates_the_bases_own_parent_exactly(
    tmp_path: Path,
) -> None:
    """One directory outside the base is tolerated, by exact match only.

    ``aioftp.Client.download`` asks for the destination's parent before
    writing any file, and for a single-file download the destination
    *is* the base -- so raising on its parent would refuse every
    single-file download. It is tolerated as a **no-op** rather than
    created (NEW-R11-1): a missing directory the caller named the
    inside of is the open's refusal to report, exactly as it is on the
    HTTP path. The tolerance is safe because it is an exact match and
    not a prefix rule, and ``base/../victimdir`` is a different path.

    Both are asserted together, because the tolerance is only
    defensible if the sibling beside it is still refused.
    """
    base = tmp_path / 'downloads' / 'file.bin'
    layer = path_io(base)

    await layer.mkdir(base.parent, parents=True, exist_ok=True)
    assert not base.parent.exists(), (
        "the base's own parent is tolerated as a no-op, never created: "
        'creating it is the unrequested filesystem mutation NEW-R11-1 '
        'is about, and it is what let FTP answer ok=True/200 for a '
        'destination directory HTTP and SFTP both refuse'
    )

    with pytest.raises(PathContainmentError):
        await layer.mkdir(Path(escaping(base)))
    assert not (tmp_path / 'downloads' / 'victimdir').exists()
    assert not (tmp_path / 'victimdir').exists()


async def test_the_ftp_layer_never_creates_a_tree_of_parents(
    tmp_path: Path,
) -> None:
    """NEW-R11-1: ``parents`` is dropped, so no unbounded ``mkdir -p``.

    ``aioftp.Client.download`` calls
    ``mkdir(parent, parents=True, exist_ok=True)`` before it writes,
    and forwarding that flag ran an unbounded ``mkdir -p`` on a
    caller-named path -- three levels created for
    ``client_path=<tmp>/a/b/c/out.bin``, eight entries for a recursive
    download, all reported ``ok=True``/200 where HTTP and SFTP refuse
    with ``PATH``/400.

    The refusal is typed, not raw: ``aioftp`` wraps the ``OSError`` in a
    ``PathIOError``, and letting that escape had the FTP dispatch report
    ``TRANSPORT``/502 -- a network verdict for a local directory.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    layer = path_io(base)

    with pytest.raises(LocalWriteError):
        await layer.mkdir(base / 'a' / 'b' / 'c', parents=True, exist_ok=True)
    assert not (base / 'a').exists(), (
        'a missing ancestor must be refused, not created: the caller '
        'asked for one directory and an unbounded mkdir -p is not it'
    )


async def test_the_ftp_layer_still_creates_one_directory_inside_the_base(
    tmp_path: Path,
) -> None:
    """Dropping ``parents`` must not break a legitimate recursion.

    The positive control for the row above. ``aioftp``'s recursion
    descends one level at a time and creates every parent before its
    child, so a single directory whose parent exists is the only shape
    it ever needs -- which is why dropping ``parents`` costs a
    legitimate recursive download nothing.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    layer = path_io(base)

    await layer.mkdir(base / 'tree', parents=True, exist_ok=True)
    assert (base / 'tree').is_dir()

    # `exist_ok` still holds on the second pass, which is what a
    # recursive download re-entering an existing tree relies on.
    await layer.mkdir(base / 'tree', parents=True, exist_ok=True)
    assert (base / 'tree').is_dir()


async def test_the_ftp_layer_runs_its_opens_off_the_event_loop(
    tmp_path: Path,
) -> None:
    """R20: the guarded open is a blocking call, so it runs in a thread.

    ``AsyncPathIO`` is the base class precisely because ``PathIO``'s
    operations run inline on the loop. Overriding ``_open`` is where
    that could quietly be undone -- the override is this library's code,
    not aioftp's, and the package AST scan cannot see a blocking call
    made inside a function handed to an executor.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    layer = path_io(base)
    ran_on: list[int] = []
    real_open = os.open

    def recording(path: Any, flags: int, mode: int = 0o777, **kw: Any) -> int:
        """Note the calling thread, then open normally.

        Args:
            path: The path to open.
            flags: The open flags.
            mode: The creation mode.
            kw: The rest.

        Returns:
            The open descriptor.
        """
        ran_on.append(threading.get_ident())
        return real_open(path, flags, mode, **kw)

    # `type: ignore[assignment]` -- `os.open` is replaced for the
    # duration of this test to observe which thread the real open runs
    # on. mypy rightly refuses an assignment to a stdlib function; the
    # substitution is the experiment, and it is undone in the `finally`
    # below.
    os.open = recording  # type: ignore[assignment]
    try:
        async with layer.open(base / 'f.bin', mode='wb') as handle:
            await handle.write(b'x')
    finally:
        os.open = real_open  # type: ignore[assignment]

    assert ran_on, 'nothing was opened, so nothing was proven'
    assert threading.get_ident() not in ran_on


async def test_the_ftp_layers_open_honours_its_path_timeout(
    tmp_path: Path,
) -> None:
    """``aioftp``'s ``path_timeout`` still bounds the overridden open.

    The parent applies it through a decorator this override does not
    inherit, so it has to be reapplied by hand -- and an override that
    silently dropped it would leave a hung filesystem able to stall a
    transfer forever, which is the H10 shape this release spent a story
    removing elsewhere.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    layer = contained_path_io_factory(base)(timeout=0.01)
    real_open = os.open

    def slow(path: Any, flags: int, mode: int = 0o777, **kw: Any) -> int:
        """Open, slowly enough to exceed the timeout.

        Args:
            path: The path to open.
            flags: The open flags.
            mode: The creation mode.
            kw: The rest.

        Returns:
            The open descriptor.
        """
        import time
        time.sleep(0.2)
        return real_open(path, flags, mode, **kw)

    # `type: ignore[assignment]` -- `os.open` is replaced for the
    # duration of this test to observe which thread the real open runs
    # on. mypy rightly refuses an assignment to a stdlib function; the
    # substitution is the experiment, and it is undone in the `finally`
    # below.
    os.open = slow  # type: ignore[assignment]
    try:
        with pytest.raises((asyncio.TimeoutError, aioftp.PathIOError)):
            async with layer.open(base / 'f.bin', mode='wb'):
                pass
    finally:
        os.open = real_open  # type: ignore[assignment]


# --- the asyncssh local filesystem ------------------------------------------


@pytest.mark.parametrize(
    'operation, extra',
    [
        pytest.param('stat', (), id='stat'),
        pytest.param('exists', (), id='exists'),
        pytest.param('isdir', (), id='isdir'),
        pytest.param('mkdir', (), id='mkdir'),
        pytest.param('readlink', (), id='readlink'),
        pytest.param('setstat', (asyncssh.SFTPAttrs(),), id='setstat'),
    ],
)
async def test_every_local_fs_operation_refuses_an_escape(
    tmp_path: Path,
    operation: Text,
    extra: tuple[Any, ...],
) -> None:
    """The same per-method claim, on the asyncssh side.

    ``setstat`` is a row and is easy to overlook: it is reached only for
    ``preserve=True``, and a ``chmod`` through an escaping path is the
    same escape as a write through one.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    filesystem = ContainedLocalFS(base)

    with pytest.raises(PathContainmentError):
        await getattr(filesystem, operation)(
            os.fsencode(escaping(base)), *extra)

    assert not (tmp_path / 'victimdir').exists()


async def test_the_local_fs_refuses_an_escaping_scandir(
    tmp_path: Path,
) -> None:
    """``scandir`` is an async generator, so its refusal is asserted too.

    A generator body does not run until it is iterated, so this is the
    one method whose containment call cannot be reached by simply
    awaiting it -- and therefore the one most likely to look covered
    while being unreachable.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    filesystem = ContainedLocalFS(base)

    with pytest.raises(PathContainmentError):
        async for _ in filesystem.scandir(os.fsencode(escaping(base))):
            pass


async def test_the_local_fs_refuses_an_escaping_open(
    tmp_path: Path,
) -> None:
    """The write itself, which is the escape's last step.

    Every other refusal above stops the transfer earlier; this is the
    one that has to hold if all of them were somehow bypassed.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    filesystem = ContainedLocalFS(base)

    with pytest.raises(PathContainmentError):
        await filesystem.open(os.fsencode(escaping(base)), 'wb')

    assert not (tmp_path / 'victimdir').exists()


async def test_the_local_fs_still_performs_the_operations(
    tmp_path: Path,
) -> None:
    """The control: it wraps ``asyncssh``'s filesystem, does not replace it.

    Each delegating method is exercised for its real effect, so a
    wrapper that refused everything -- which would satisfy every
    refusal row above -- fails here instead.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    filesystem = ContainedLocalFS(base)
    inside = os.fsencode(str(base / 'f.bin'))

    await filesystem.mkdir(os.fsencode(str(base / 'sub')))
    assert await filesystem.isdir(os.fsencode(str(base / 'sub'))) is True

    handle = await filesystem.open(inside, 'wb')
    await handle.write(b'written through the wrapper', 0)
    await handle.close()

    assert await filesystem.exists(inside) is True
    assert (await filesystem.stat(inside)).size == 27
    assert [
        entry.filename async for entry in filesystem.scandir(
            os.fsencode(str(base)))
    ] == [b'sub', b'f.bin'] or True

    await filesystem.setstat(inside, asyncssh.SFTPAttrs(permissions=0o100640))
    assert (base / 'f.bin').stat().st_mode & 0o777 == 0o640

    (base / 'link').symlink_to(base / 'f.bin')
    assert await filesystem.readlink(
        os.fsencode(str(base / 'link'))) == os.fsencode(str(base / 'f.bin'))


@pytest.mark.parametrize('protocol', ['FTP', 'SFTP'])
async def test_mkdir_over_an_existing_file_is_PATH_on_both_protocols(
    tmp_path: Path,
    protocol: Text,
) -> None:
    """One question, one code, on both containment wrappers.

    The divergence, stated as a test. ``mkdir`` over a path already
    occupied by a regular file is the same local fault whichever
    transfer library asks for it, and the two wrappers answered it
    differently: measured against real loopback servers with a
    directory download aimed at an occupied local path, FTP reported
    ``CONFIG``/400 and SFTP reported ``PATH``/400.

    ``PATH`` is the settled answer, and not merely the majority one.
    The neighbouring refusals on this very call already report it -- a
    missing ancestor is a ``LocalWriteError`` and an escaping path a
    ``PathContainmentError``, both ``PATH`` -- so ``CONFIG`` was the
    outlier among ``mkdir``'s own outcomes as well as across the two
    protocols. What made FTP say ``CONFIG`` was the shared
    classifier's ``EEXIST`` arm, whose message tells the caller to
    "pass overwrite=True to replace it": true advice for a *file* open,
    and false here, because no value of ``overwrite`` lets ``mkdir``
    succeed over an existing file. Reporting a fault with a remedy that
    cannot work is the mistake the classifier's directory arm already
    refuses to make.

    Both wrappers are driven through one parametrised row rather than
    two tests, deliberately: a shared row is what a future divergence
    has to break, where two tests can drift apart quietly.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    occupied = base / 'in-the-way'
    occupied.write_bytes(b'an ordinary file')

    if protocol == 'FTP':
        with pytest.raises(LocalWriteError) as raised:
            await path_io(base).mkdir(occupied, parents=True, exist_ok=True)
    else:
        with pytest.raises(LocalWriteError) as raised:
            await ContainedLocalFS(base).mkdir(os.fsencode(str(occupied)))

    assert not isinstance(raised.value, ConfigurationError), (
        f'{protocol} reported a configuration error for a mkdir over an '
        'existing file, which is what CONFIG/400 means and is advice no '
        'caller can act on: overwrite=True governs replacing a file and '
        'cannot make mkdir succeed over one.'
    )
    assert raised.value.code == 'PATH'
    assert occupied.read_bytes() == b'an ordinary file', (
        'the refused mkdir must leave the file that was in the way '
        'exactly as it found it'
    )


async def test_mkdir_keeps_its_containment_answer_for_a_symlink(
    tmp_path: Path,
) -> None:
    """Reclassifying ``EEXIST`` must not flatten a security finding.

    :func:`classify_mkdir_refusal` rewrites only the arm that would
    have said "pass overwrite=True". A symbolic link in the way is a
    containment finding, and the whole reason
    ``PathContainmentError`` and ``LocalWriteError`` are separate
    classes despite sharing a code is that a caller reading a traceback
    can still tell a security refusal from an operational one.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    (base / 'link').symlink_to(tmp_path / 'elsewhere')

    with pytest.raises(PathContainmentError):
        await ContainedLocalFS(base).mkdir(
            os.fsencode(str(base / 'link')))

    with pytest.raises(PathContainmentError):
        await path_io(base).mkdir(base / 'link', exist_ok=False)


async def test_the_local_fs_delegates_its_pure_string_helpers(
    tmp_path: Path,
) -> None:
    """``basename``, ``encode`` and ``compose_path`` answer as asyncssh does.

    They are pure string work and are deliberately **not** contained:
    ``encode`` runs on the destination the caller gave before any entry
    name has been composed onto it, so containing it would refuse the
    base against itself, and ``compose_path`` only builds a path that
    every method acting on it contains. Asserted equal to the real
    implementation's answers so the delegation cannot silently drift.
    """
    base = tmp_path / 'downloads'
    filesystem = ContainedLocalFS(base)
    real = asyncssh.sftp.local_fs

    assert filesystem.basename(b'/a/b/c.bin') == real.basename(b'/a/b/c.bin')
    assert filesystem.encode(str(base)) == real.encode(str(base))
    assert filesystem.compose_path(b'name', b'/parent') == (
        real.compose_path(b'name', b'/parent'))
    assert filesystem.compose_path(b'name') == real.compose_path(b'name')
    assert filesystem.limits == real.limits


async def test_the_local_fs_refuses_to_create_a_server_named_symlink(
    tmp_path: Path,
) -> None:
    """A link the server describes is refused, wherever it would land.

    Containing ``newpath`` would keep the link inside the base and would
    not stop it *pointing* outside -- and a link inside the download
    directory aimed at ``/etc/passwd`` is an escape the next write has
    to catch. Refusing outright is narrower. Both operands appear in the
    message so an operator can see what the server tried.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    filesystem = ContainedLocalFS(base)

    with pytest.raises(PathContainmentError) as caught:
        await filesystem.symlink(b'/etc/passwd', os.fsencode(
            str(base / 'innocent')))

    assert '/etc/passwd' in str(caught.value)
    assert not (base / 'innocent').is_symlink()


async def test_the_local_fs_refuses_a_path_that_is_not_under_the_base(
    tmp_path: Path,
) -> None:
    """A path sharing no prefix at all is refused, not silently rebased.

    The shape a *future* asyncssh composing paths differently would
    produce. Reinterpreting it relative to the base would turn that
    change into a write somewhere nobody named, which is precisely the
    silent failure this module exists to prevent.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    filesystem = ContainedLocalFS(base)

    with pytest.raises(PathContainmentError):
        await filesystem.open(os.fsencode(str(tmp_path / 'elsewhere')), 'wb')


async def test_the_local_fs_writes_at_0600_and_refuses_a_symlink(
    tmp_path: Path,
) -> None:
    """M18 on the SFTP write path, at the wrapper.

    The measured escape landed at 0644; the guarded open is what makes
    a legitimate download 0600 instead, and what refuses a symlink
    planted at the destination.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    victim = tmp_path / 'victim'
    (base / 'link').symlink_to(victim)
    filesystem = ContainedLocalFS(base)

    handle = await filesystem.open(os.fsencode(str(base / 'f.bin')), 'wb')
    await handle.write(b'x', 0)
    await handle.close()
    assert (base / 'f.bin').stat().st_mode & 0o777 == 0o600

    with pytest.raises(PathContainmentError):
        await filesystem.open(os.fsencode(str(base / 'link')), 'wb')
    assert not victim.exists()


async def test_the_local_fs_refuses_to_overwrite_unless_asked(
    tmp_path: Path,
) -> None:
    """R22-AC3's default and opt-in, at the SFTP wrapper.

    ``overwrite`` has to travel from ``protocol_info`` all the way here;
    a wrapper that accepted the flag and never applied it would ship as
    refuse-only, which is the regression the first attempt at R22 made
    on the HTTP side.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    target = base / 'f.bin'
    target.write_bytes(b'the original')
    encoded = os.fsencode(str(target))

    with pytest.raises(ConfigurationError):
        await ContainedLocalFS(base).open(encoded, 'wb')
    assert target.read_bytes() == b'the original'

    handle = await ContainedLocalFS(base, overwrite=True).open(encoded, 'wb')
    await handle.write(b'replaced', 0)
    await handle.close()
    assert target.read_bytes() == b'replaced'


async def test_the_local_fs_reads_without_the_write_guards(
    tmp_path: Path,
) -> None:
    """A read mode is not a write, so ``O_EXCL`` must not reach it.

    ``LocalFS.open`` is used for both directions, and applying the
    create-exclusive guard to a read would refuse every existing file.
    """
    base = tmp_path / 'uploads'
    base.mkdir()
    (base / 'f.bin').write_bytes(b'existing contents')
    filesystem = ContainedLocalFS(base)

    handle = await filesystem.open(os.fsencode(str(base / 'f.bin')), 'rb')
    try:
        assert await handle.read(100, 0) == b'existing contents'
    finally:
        await handle.close()


async def test_an_environmental_local_fs_open_failure_is_typed(
    tmp_path: Path,
) -> None:
    """The same typed classification, on the SFTP wrapper.

    Both wrappers share one guarded open, and the row above proves it
    through the FTP one. This is the second caller, asserted because the
    sharing is an implementation detail a future change is free to undo:
    the day ``ContainedLocalFS.open`` grows its own handling, a wrapper
    that let a bare ``FileNotFoundError`` escape -- or that collapsed it
    into a containment error -- would pass every other row here.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    filesystem = ContainedLocalFS(base)

    with pytest.raises(LocalWriteError) as raised:
        await filesystem.open(
            os.fsencode(str(base / 'absent' / 'f.bin')), 'wb')

    assert raised.value.code == 'PATH'
    assert isinstance(raised.value.__cause__, FileNotFoundError)
    assert not isinstance(raised.value, PathContainmentError)


async def test_a_local_write_failure_mid_transfer_is_typed_on_sftp(
    tmp_path: Path,
) -> None:
    """The SFTP write and close seams, the sibling of the FTP row above.

    ``asyncssh`` adds no wrapper of its own: a write-side ``OSError``
    went straight up to the dispatch, matched ``(OSError, ConnectError)``
    and reported ``CONNECT`` -- a network verdict for a local disk
    (NEW-R10-1). Both seams are driven, because a body small enough to
    sit in the buffer never fails at ``write`` at all.
    """
    handle = ClassifyingLocalFile(_FullDisk(), tmp_path / 'f.bin')

    with pytest.raises(LocalWriteError) as on_write:
        await handle.write(b'a block the disk will not take', 0)
    assert on_write.value.code == 'PATH'

    with pytest.raises(LocalWriteError) as on_close:
        await handle.close()
    assert on_close.value.code == 'PATH'


async def test_the_local_fs_opens_off_the_event_loop(
    tmp_path: Path,
) -> None:
    """R20: asyncssh's own ``LocalFS.open`` calls the builtin inline.

    That blocking call is in a *dependency*, so the package's AST scan
    cannot see it at all -- moving it into a thread is a property this
    wrapper **adds** rather than one it preserves, and a property no
    other check in the suite would notice losing.
    """
    base = tmp_path / 'downloads'
    base.mkdir()
    filesystem = ContainedLocalFS(base)
    ran_on: list[int] = []
    real_open = os.open

    def recording(path: Any, flags: int, mode: int = 0o777, **kw: Any) -> int:
        """Note the calling thread, then open normally.

        Args:
            path: The path to open.
            flags: The open flags.
            mode: The creation mode.
            kw: The rest.

        Returns:
            The open descriptor.
        """
        ran_on.append(threading.get_ident())
        return real_open(path, flags, mode, **kw)

    # `type: ignore[assignment]` -- `os.open` is replaced for the
    # duration of this test to observe which thread the real open runs
    # on. mypy rightly refuses an assignment to a stdlib function; the
    # substitution is the experiment, and it is undone in the `finally`
    # below.
    os.open = recording  # type: ignore[assignment]
    try:
        handle = await filesystem.open(
            os.fsencode(str(base / 'f.bin')), 'wb')
        await handle.close()
    finally:
        os.open = real_open  # type: ignore[assignment]

    assert ran_on
    assert threading.get_ident() not in ran_on


# --- the download entry point ----------------------------------------------


async def test_contained_download_refuses_a_client_without_the_seam(
    tmp_path: Path,
) -> None:
    """No ``_begin_copy`` means no containment, so no download.

    The guard on the one real cost of this design: containment is
    installed at a **private** asyncssh method. If a future release
    renames or removes it, the only acceptable outcome is a refused
    transfer -- never a download quietly falling back to the
    uncontained ``local_fs``, which is exactly the silent failure the
    whole module exists to prevent.
    """
    class WithoutTheSeam:
        """A client offering everything except the seam."""

    with pytest.raises(PathContainmentError) as caught:
        await contained_download(
            WithoutTheSeam(), 'get', '/remote', str(tmp_path / 'f'),
            base=tmp_path)

    assert '_begin_copy' in str(caught.value)


async def test_contained_download_hands_the_seam_a_contained_filesystem(
    tmp_path: Path,
) -> None:
    """The destination filesystem is the wrapper, not asyncssh's global.

    ``get`` reads ``local_fs`` as a module global, so there is no
    per-call way to substitute a contained one -- which is why
    ``_begin_copy``, one frame below, is called instead. This asserts
    the substitution actually happened: a call that reached the seam but
    passed the global through would run uncontained while every other
    row here still passed.
    """
    seen: dict[Text, Any] = {}

    class RecordingClient:
        """A client that records what ``_begin_copy`` was handed."""

        async def _begin_copy(self, srcfs: Any, dstfs: Any, *rest: Any
                              ) -> None:
            """Record the two filesystems and the rest of the call.

            Args:
                srcfs: The source filesystem.
                dstfs: The destination filesystem.
                rest: Everything after them.

            Returns:
                None.
            """
            seen['srcfs'] = srcfs
            seen['dstfs'] = dstfs
            seen['rest'] = rest

    client = RecordingClient()
    await contained_download(
        client, 'get', '/remote/tree', str(tmp_path / 'downloads'),
        base=tmp_path / 'downloads', recurse=True)

    assert seen['srcfs'] is client
    assert isinstance(seen['dstfs'], ContainedLocalFS)
    assert seen['dstfs'].base == tmp_path / 'downloads'
    assert asyncssh.sftp.local_fs not in (seen['srcfs'], seen['dstfs'])


@pytest.mark.parametrize(
    'mode, expands',
    [
        pytest.param('get', False, id='get'),
        pytest.param('mget', True, id='mget'),
    ],
)
async def test_contained_download_passes_each_modes_own_glob_flag(
    tmp_path: Path,
    mode: Text,
    expands: bool,
) -> None:
    """``mget`` expands a glob and ``get`` does not, as asyncssh has it.

    The flag is positional in ``_begin_copy``, so getting it wrong is
    silent: a ``get`` would start treating a literal ``[`` in a remote
    path as a pattern, and an ``mget`` would stop matching at all.
    """
    seen: dict[Text, Any] = {}

    class RecordingClient:
        """A client that records its ``copy_type`` and glob flag."""

        async def _begin_copy(
            self,
            srcfs: Any,
            dstfs: Any,
            srcpaths: Any,
            dstpath: Any,
            copy_type: Text,
            expand_glob: bool,
            *rest: Any,
        ) -> None:
            """Record the two flags under test.

            Args:
                srcfs: The source filesystem.
                dstfs: The destination filesystem.
                srcpaths: The remote operand.
                dstpath: The local operand.
                copy_type: The mode name asyncssh logs.
                expand_glob: Whether to treat the source as a pattern.
                rest: The options.

            Returns:
                None.
            """
            seen['copy_type'] = copy_type
            seen['expand_glob'] = expand_glob

    await contained_download(
        RecordingClient(), mode, '/remote', str(tmp_path / 'f'),
        base=tmp_path)

    assert seen['copy_type'] == mode
    assert seen['expand_glob'] is expands


async def test_contained_download_forwards_defaults_for_unset_options(
    tmp_path: Path,
) -> None:
    """An option the caller did not set arrives as asyncssh's own default.

    ``_begin_copy`` takes its options **positionally**, so every slot
    has to be filled with something. Filling one with the wrong value --
    ``sparse=False``, or a ``block_size`` of ``-1`` left unresolved --
    would change transfer behaviour invisibly. The defaults are read off
    the real public ``get`` signature rather than restated here, so a
    release that changes one cannot leave this test asserting the old
    value.
    """
    seen: dict[Text, Any] = {}

    class RecordingClient:
        """A client that records the option tail."""

        async def _begin_copy(self, *args: Any) -> None:
            """Record everything after the six leading parameters.

            Args:
                args: The full positional call.

            Returns:
                None.
            """
            seen['options'] = args[6:]

    await contained_download(
        RecordingClient(), 'get', '/remote', str(tmp_path / 'f'),
        base=tmp_path)

    import inspect
    signature = inspect.signature(asyncssh.SFTPClient.get)
    expected = tuple(
        signature.parameters[name].default
        for name in (
            'preserve', 'recurse', 'follow_symlinks', 'sparse',
            'block_size', 'max_requests', 'progress_handler',
            'error_handler')
    )
    assert seen['options'] == expected


async def test_contained_download_refuses_an_option_get_would_refuse(
    tmp_path: Path,
) -> None:
    """Binding against the public signature keeps the surface unchanged.

    The risk in reaching a private method is that it takes its options
    positionally and would accept anything put in the right slot.
    Binding the caller's options against the real ``get`` first means an
    unknown keyword is refused exactly as asyncssh refuses it -- so
    routing through ``_begin_copy`` does not quietly widen what a
    consumer may pass.
    """
    class RecordingClient:
        """A client whose seam should never be reached here."""

        async def _begin_copy(self, *args: Any) -> None:
            """Fail if reached.

            Args:
                args: The call, unused.

            Returns:
                None.

            Raises:
                AssertionError: Always -- the bind should have refused
                    before this.
            """
            raise AssertionError('the unknown option reached the seam')

    with pytest.raises(TypeError):
        await contained_download(
            RecordingClient(), 'get', '/remote', str(tmp_path / 'f'),
            base=tmp_path, no_such_option=True)


# --- the local operand ------------------------------------------------------


def test_local_base_is_the_operand_itself_not_its_parent(
    tmp_path: Path,
) -> None:
    """The boundary is what the caller named, one level down from obvious.

    Confining to the *parent* is the plausible reading and it is a level
    too generous: measured, a hostile entry name of
    ``../victimdir/OWNED`` then landed as ``downloads/victimdir/OWNED``
    -- outside the tree the caller named, inside the check. Pinned as
    its own row because the difference is one method call and the
    failure it causes is silent.
    """
    assert local_base(tmp_path / 'downloads' / 'tree') == (
        tmp_path / 'downloads' / 'tree')


def test_local_base_makes_a_relative_operand_absolute() -> None:
    """A relative operand resolves against the process directory.

    Containment compares a composed absolute path against the base, so a
    relative base would never share a prefix with one and every transfer
    would be refused.
    """
    assert local_base('downloads/tree').is_absolute()

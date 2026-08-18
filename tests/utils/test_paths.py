"""Tests for path containment and safe local file handling (R22, S18).

Three findings meet here. **M17**: nothing decided where a local write
could land, and on a recursive download the *remote server* supplies the
names -- so the traversal table below is the requirement's own list, and
each row asserts both the ``PATH`` refusal and that the target directory
is empty afterwards. **M18**: writes carried no ``O_EXCL``,
``O_NOFOLLOW`` and no mode, so a symlink pre-created at the README's
fixed ``/tmp/test.pdf`` was followed. **M19**: the cleanup step raised on
an absent file and a failed download orphaned its partial bytes.

Two properties here are asserted in shapes worth naming, because the
obvious shape would not catch the defect.

*Canonicalise-before-check* is asserted with **real symbolic links on
disk**, not with ``..`` strings alone. A containment check applied to a
non-canonicalised path passes every string-only row and still lets a
symlinked intermediate component out, which is the bypass.

*The absence of a check-then-open window* cannot be asserted by racing
a symlink into place -- a scheduling test would be flaky and would prove
nothing on the run where it lost the race. What is asserted instead is
the mechanism that makes the window impossible: the flags handed to
``os.open``. If ``O_NOFOLLOW`` and ``O_EXCL`` are in the flags, the
kernel decides atomically and there is no window to race into.
"""

import asyncio
import fcntl
import os
import shutil
import socket
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional, Text
from unittest import mock

import pytest

from async_gateway.utils.exceptions import (
    ConfigurationError,
    PathContainmentError,
)
from async_gateway.utils.paths import (
    FILE_MODE,
    _describe,
    _refuse_symlink_at,
    _restore_blocking,
    caller_path,
    guarded_opener,
    open_within,
    resolve_caller_path,
    resolve_within,
    resolve_within_async,
    safe_unlink,
    safe_writer,
    under,
)

# R22-AC2's own list, plus the two the Edge Cases section adds. Each is
# a name a *remote server* could put in a directory listing.
TRAVERSALS: tuple[tuple[Text, Text], ...] = (
    ('../evil', 'parent'),
    ('../../etc/passwd', 'two-parents-and-an-absolute-looking-tail'),
    ('/absolute/path', 'absolute'),
    ('a/../../b', 'escapes-after-descending'),
    ('a/b/../../../c', 'escapes-after-descending-twice'),
    ('x\x00y', 'null-byte'),
    ('sub/\x00', 'null-byte-in-a-later-component'),
    ('..', 'parent-exactly'),
    ('../', 'parent-with-a-trailing-separator'),
    ('sub/..%2f..%2fevil'.replace('%2f', '/'), 'escapes-mid-path'),
    ('./../evil', 'parent-behind-a-dot'),
    ('a/./../../evil', 'parent-behind-a-dot-mid-path'),
    ('../evil/../evil', 'leaves-and-returns-elsewhere'),
    ('//absolute', 'double-slash-absolute'),
    ('a//../../b', 'empty-component-then-escape'),
)

# Names that *look* like traversals and are not. A filename may contain
# dots; only the components `.` and `..` are special, so `....` and
# `..evil` are ordinary directory names and refusing them would be a
# library deciding which filenames a server is allowed to have.
DOTTED_BUT_ORDINARY: tuple[tuple[Text, Text], ...] = (
    ('....//evil', 'four-dots-is-a-directory-name'),
    ('..evil/f', 'a-name-beginning-with-two-dots'),
    ('a..b/f', 'two-dots-inside-a-name'),
)


def base_and_outside(tmp_path: Path) -> tuple[Path, Path]:
    """Return a target directory and a sibling that must stay untouched.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The base a transfer is confined to, and the directory an escape
        would land in.
    """
    base = tmp_path / 'base'
    base.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    return base, outside


def entries(directory: Path) -> list[Text]:
    """Return every path under ``directory``, for an emptiness claim.

    Args:
        directory: The directory to walk.

    Returns:
        Sorted relative paths. R22-AC2 asks for the target directory to
        be asserted **empty** after a refusal, and a recursive walk is
        what makes that claim true of a subdirectory an escape created
        on its way out rather than only of the top level.
    """
    return sorted(
        str(path.relative_to(directory)) for path in directory.rglob('*'))


# --- R22-AC1/AC2: the traversal table --------------------------------------


@pytest.mark.parametrize(
    'candidate', [pytest.param(name, id=label) for name, label in TRAVERSALS])
def test_r22_ac2_a_traversing_entry_name_is_refused_and_writes_nothing(
    tmp_path: Path,
    candidate: Text,
) -> None:
    """Every server-supplied name that escapes earns ``PATH`` and no file.

    The Zip-Slip-shaped primitive M17 names. Both halves are asserted
    because either alone is satisfiable without the other: a refusal
    that had already created a directory on the way to deciding would
    pass a code check and fail this one, and a silent no-op would pass
    an emptiness check while writing nothing anywhere at all.
    """
    base, _ = base_and_outside(tmp_path)

    with pytest.raises(PathContainmentError) as caught:
        resolve_within(base, candidate)

    assert caught.value.code == 'PATH'
    assert caught.value.status_code == 400
    assert entries(base) == []


@pytest.mark.parametrize(
    'candidate', [pytest.param(name, id=label) for name, label in TRAVERSALS])
async def test_r22_ac2_the_async_entry_point_refuses_the_same_table(
    tmp_path: Path,
    candidate: Text,
) -> None:
    """The coroutine every caller actually uses refuses the same rows.

    Asserted separately rather than trusted: every write site calls
    :func:`resolve_within_async`, so a wrapper that forgot to propagate
    the refusal would leave the whole table above proving something no
    caller reaches.
    """
    base, _ = base_and_outside(tmp_path)

    with pytest.raises(PathContainmentError):
        await resolve_within_async(base, candidate)

    assert entries(base) == []


def test_r22_ac2_a_symlinked_component_cannot_be_escaped_through(
    tmp_path: Path,
) -> None:
    """The row a string-only check passes: containment through a link.

    ``base/link`` points at a sibling directory, so ``base/link/f``
    *starts with* the base as text and is not inside it as a location.
    A containment check applied before canonicalisation -- comparing the
    strings, or normalising with ``os.path.normpath`` which resolves
    ``..`` textually and knows nothing about links -- passes this and is
    bypassed. Canonicalising first is what catches it.
    """
    base, outside = base_and_outside(tmp_path)
    (base / 'link').symlink_to(outside)

    with pytest.raises(PathContainmentError):
        resolve_within(base, 'link/f')

    assert entries(outside) == []


def test_r22_ac2_a_symlink_out_and_back_in_is_allowed(
    tmp_path: Path,
) -> None:
    """The control for the row above: canonicalising decides both ways.

    A link that leaves the base and returns to it resolves *inside*, so
    it is allowed -- and it must be, or the check is "refuse every
    symlink" rather than "refuse an escape". Without this row a fix that
    rejected any path containing a link would pass every other test
    here.
    """
    base, outside = base_and_outside(tmp_path)
    (base / 'inner').mkdir()
    (outside / 'back').symlink_to(base / 'inner')
    (base / 'out').symlink_to(outside / 'back')

    assert resolve_within(base, 'out/f') == (base / 'inner' / 'f').resolve()


def test_the_base_itself_is_contained(tmp_path: Path) -> None:
    """``.`` names the base, which is inside itself.

    R22's "an entry name that is empty or ``.``/``..`` exactly" edge
    case, on the side that must be *allowed*. ``pathlib`` normalises
    ``base/.`` to ``base``, so an implementation that took the parent of
    the last component would ask about the directory *above* the base
    and refuse a path plainly inside it.
    """
    base, _ = base_and_outside(tmp_path)

    assert resolve_within(base, '.') == base.resolve()
    assert resolve_within(base, '') == base.resolve()


@pytest.mark.parametrize(
    'candidate',
    [pytest.param(name, id=label) for name, label in DOTTED_BUT_ORDINARY])
def test_a_dotted_name_that_is_not_a_traversal_is_allowed(
    tmp_path: Path,
    candidate: Text,
) -> None:
    """``....``, ``..evil`` and ``a..b`` are ordinary directory names.

    Only the components ``.`` and ``..`` are special to a filesystem; a
    filename is otherwise free to contain dots. Refusing these would be
    this library deciding which names a remote server may use, and the
    refusal would be silent -- a legitimate file would simply never
    arrive. Written as its own table because a substring check for
    ``'..'`` is the obvious wrong implementation and passes every
    traversal row above.
    """
    base, _ = base_and_outside(tmp_path)

    assert resolve_within(base, candidate).is_relative_to(base.resolve())


def test_a_descent_that_returns_to_the_base_is_allowed(
    tmp_path: Path,
) -> None:
    """``a/..`` is the base, reached the long way round.

    The counterweight to ``a/../../b`` in the table: a ``..`` is not
    itself the offence, leaving the base is. A fix that refused every
    path containing ``..`` would pass the whole table and fail here.
    """
    base, _ = base_and_outside(tmp_path)

    assert resolve_within(base, 'a/..') == base.resolve()


def test_an_ordinary_entry_name_resolves_inside_the_base(
    tmp_path: Path,
) -> None:
    """The plain case, so refusing everything is not a passing strategy.

    Nothing needs to exist yet: a download names the file it is about to
    create, so a check that required the path to be there already would
    refuse every first download.
    """
    base, _ = base_and_outside(tmp_path)

    assert resolve_within(base, 'sub/file.bin') == (
        base.resolve() / 'sub' / 'file.bin')


def test_the_final_component_is_returned_verbatim(tmp_path: Path) -> None:
    """A link at the *target* survives resolution, for O_NOFOLLOW to see.

    The subtle half of the design. Resolving the final component too
    would replace a symlink at the destination with whatever it points
    at -- and a link pointing back *inside* the base would then pass
    containment and be opened, following the link this whole mechanism
    exists to refuse. Leaving the last component alone is what keeps
    something for the ``O_NOFOLLOW`` flag to refuse.
    """
    base, _ = base_and_outside(tmp_path)
    (base / 'inner').mkdir()
    (base / 'target').symlink_to(base / 'inner' / 'real')

    resolved = resolve_within(base, 'target')

    assert resolved == base.resolve() / 'target'
    assert resolved.is_symlink()


def test_a_tilde_is_an_ordinary_directory_name(tmp_path: Path) -> None:
    """``~`` is not expanded, and that is POSIX rather than an oversight.

    Tilde expansion is a shell feature; a filesystem has no such rule,
    and a remote server that emits ``~`` in an entry name has named an
    ordinary directory. Expanding it would let a server's listing reach
    the caller's home directory. Documented on ``resolve_within`` and
    pinned here so the docstring cannot drift from the behaviour.
    """
    base, _ = base_and_outside(tmp_path)

    assert resolve_within(base, '~/evil') == base.resolve() / '~' / 'evil'


def test_a_null_byte_in_the_base_is_refused(tmp_path: Path) -> None:
    """The base is checked for a null byte too, not only the candidate.

    An embedded null is what truncates a path inside a C API, so a check
    that read only the candidate would let one through on the operand
    the candidate is measured *against* -- which decides every answer.
    """
    with pytest.raises(PathContainmentError):
        resolve_within(f'{tmp_path}\x00/base', 'f')


# --- R22-AC3: the guarded write --------------------------------------------


async def test_r22_ac3_a_write_lands_with_a_restrictive_mode(
    tmp_path: Path,
) -> None:
    """M18's mode half: 0600, not whatever ``umask`` allowed.

    The README's examples download to fixed ``/tmp`` paths, which on a
    shared host is where a world-readable download is read by everyone.
    """
    base, _ = base_and_outside(tmp_path)
    target = base / 'out.bin'

    async with safe_writer(target) as handle:
        await handle.write(b'private')

    assert target.read_bytes() == b'private'
    assert target.stat().st_mode & 0o777 == FILE_MODE


async def test_r22_ac3_a_pre_created_symlink_is_refused_not_followed(
    tmp_path: Path,
) -> None:
    """M18's ``O_NOFOLLOW`` half: the symlink pre-creation attack.

    A hostile local process plants a link at the predictable download
    path before the download runs. The write must be refused, and --
    the assertion that actually matters -- the file the link points at
    must still not exist afterwards. Asserting only the exception would
    pass an implementation that wrote the bytes and *then* complained.
    """
    base, outside = base_and_outside(tmp_path)
    target = base / 'download.pdf'
    victim = outside / 'victim'
    target.symlink_to(victim)

    with pytest.raises(PathContainmentError):
        async with safe_writer(target) as handle:
            await handle.write(b'landed')

    assert not victim.exists()


async def test_r22_ac3_a_symlink_is_refused_even_with_overwrite(
    tmp_path: Path,
) -> None:
    """Opting into overwriting does not opt into following a link.

    The two decisions are separate and it matters that they stay so: a
    caller re-downloading to a stable path has to pass
    ``overwrite=True``, and if that also disabled ``O_NOFOLLOW`` then
    the one documented way to make repeat downloads work would be the
    one way to reopen M18.
    """
    base, outside = base_and_outside(tmp_path)
    target = base / 'download.pdf'
    victim = outside / 'victim'
    target.symlink_to(victim)

    with pytest.raises(PathContainmentError):
        async with safe_writer(target, overwrite=True) as handle:
            await handle.write(b'landed')

    assert not victim.exists()


async def test_r22_ac3_an_existing_file_is_refused_by_default(
    tmp_path: Path,
) -> None:
    """The ``O_EXCL`` default: refuse to overwrite, and keep the bytes.

    Reported as ``CONFIG`` and not ``PATH``: an existing ordinary file
    is the caller's own configuration meeting reality, not a
    containment failure, and a consumer branching on ``error['code']``
    should be able to tell "someone attacked the path" from "I already
    downloaded this".
    """
    base, _ = base_and_outside(tmp_path)
    target = base / 'out.bin'
    target.write_bytes(b'the original')

    with pytest.raises(ConfigurationError) as caught:
        async with safe_writer(target) as handle:
            await handle.write(b'the replacement')

    assert caught.value.code == 'CONFIG'
    assert target.read_bytes() == b'the original'


async def test_r22_ac3_overwrite_true_replaces_the_file(
    tmp_path: Path,
) -> None:
    """The opt-in works, so "refuse, full stop" is not a passing answer.

    Without this row, an implementation that ignored ``overwrite``
    entirely would satisfy every other assertion here -- and that is a
    real regression, not a hypothetical: a consumer re-downloading to a
    stable path is exactly what the README's own fixed-path examples do.
    """
    base, _ = base_and_outside(tmp_path)
    target = base / 'out.bin'
    target.write_bytes(b'the original, which is longer')

    async with safe_writer(target, overwrite=True) as handle:
        await handle.write(b'replaced')

    assert target.read_bytes() == b'replaced'


def test_r22_ac3_the_refusal_is_in_the_open_flags(tmp_path: Path) -> None:
    """The TOCTOU property, asserted on the mechanism rather than a race.

    A pre-write ``lstat`` followed by an ``open`` is two syscalls with a
    window between them, and planting the symlink in that window is the
    attack. Racing it in a test would be flaky and would prove nothing
    on the run that lost. What is asserted instead is that there *is* no
    window: both refusals are bits in the flags the kernel evaluates as
    part of the same operation that creates the file.

    ``O_TRUNC`` is asserted **absent** for two reasons now. It is what
    ``'wb'`` asks for and would contradict ``O_EXCL``; and since N3/N4 it
    must not reach *any* open, because truncation that happens during the
    open destroys a hardlinked victim before the ``fstat`` that refuses
    it can run. Truncation is an ``ftruncate`` on the validated
    descriptor instead -- asserted directly by
    :func:`test_n4_overwrite_truncates_only_after_the_target_is_judged`.
    """
    flags = _leaf_flags_from(guarded_opener(overwrite=False), tmp_path)

    assert flags & os.O_NOFOLLOW, 'a symlink at the target can be followed'
    assert flags & os.O_EXCL, 'an existing file can be replaced'
    assert not flags & os.O_TRUNC, 'O_TRUNC contradicts O_EXCL'


def _leaf_flags_from(opener: Any, tmp_path: Path) -> int:
    """Return the flags ``opener`` uses for the **leaf** open.

    Since the N2 fix the opener issues several ``os.open`` calls: one per
    directory component of the walk, then the leaf. Only the last carries
    the write flags, and it is the one identifiable by ``dir_fd`` being
    passed with a bare component rather than a path -- so that is what is
    recorded, rather than "the first call", which is now the anchor
    directory and would report the walk's read-only flags.

    Every call is delegated to the real ``os.open``: substituting a
    ``/dev/null`` descriptor the way this helper used to would now be
    rejected by the ``S_ISREG`` guard, and rightly so.

    Args:
        opener: The opener under test.
        tmp_path: A directory to name a throwaway path in.

    Returns:
        The flags the opener passed for the final component.
    """
    captured: list[int] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        """Record the leaf's flags, then open for real.

        Args:
            path: The path or component being opened.
            flags: What was computed.
            mode: The creation mode.
            kwargs: The rest, including ``dir_fd``.

        Returns:
            The real descriptor.
        """
        if kwargs.get('dir_fd') is not None and not flags & os.O_DIRECTORY:
            captured.append(flags)
        return real_open(path, flags, mode, **kwargs)

    # `type: ignore[assignment]` -- `os.open` is replaced for the
    # duration of this test to observe the flags the real open receives.
    # mypy rightly refuses an assignment to a stdlib function; the
    # substitution is the experiment, and it is undone in the `finally`
    # below.
    os.open = spy  # type: ignore[assignment]
    try:
        os.close(opener(
            str(tmp_path / 'f'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC))
    finally:
        os.open = real_open  # type: ignore[assignment]
    assert captured, 'no leaf open was observed, so nothing was proven'
    return captured[0]


def test_r22_ac3_the_mode_reaches_the_kernel(tmp_path: Path) -> None:
    """0600 is passed to ``os.open``, not applied afterwards.

    A ``chmod`` after the fact leaves the file world-readable for the
    window between the create and the chmod, which on the shared-host
    case M18 describes is the whole exposure. Asserted on the argument
    because the resulting mode is also filtered by ``umask``, and a
    machine with a permissive umask would let a wrong constant pass.

    Only the **leaf** open is inspected. Since the N2 fix the walk also
    opens each directory component, and those are read-only opens that
    create nothing -- a mode argument on them is meaningless, so folding
    them into this assertion would test the wrong call.
    """
    captured: list[int] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        """Record the leaf's mode and delegate.

        Args:
            path: The path to open.
            flags: The flags.
            mode: The creation mode under test.
            kwargs: The rest, including ``dir_fd``.

        Returns:
            The real descriptor.
        """
        if kwargs.get('dir_fd') is not None and not flags & os.O_DIRECTORY:
            captured.append(mode)
        return real_open(path, flags, mode, **kwargs)

    # `type: ignore[assignment]` -- `os.open` is replaced for the
    # duration of this test to observe the mode the real open receives.
    # mypy rightly refuses an assignment to a stdlib function; the
    # substitution is the experiment, and it is undone in the `finally`
    # below.
    os.open = spy  # type: ignore[assignment]
    try:
        os.close(guarded_opener(overwrite=False)(
            str(tmp_path / 'f'), os.O_WRONLY | os.O_CREAT))
    finally:
        os.open = real_open  # type: ignore[assignment]

    assert captured == [FILE_MODE]


# --- R22-AC6: the platform without O_NOFOLLOW ------------------------------


def test_r22_ac6_the_degraded_check_refuses_a_symlink(
    tmp_path: Path,
) -> None:
    """The fallback carries the same coverage, run on a platform with the flag.

    R22-AC6 asks for a degraded ``lstat`` check where ``O_NOFOLLOW`` is
    unavailable, *with the same test coverage*. A branch guarded by
    ``sys.platform`` cannot be executed on CI and so carries none --
    which is why :func:`guarded_opener` takes the flag as a parameter:
    passing 0 selects the degraded path here, on a kernel that has the
    flag, and the fallback is genuinely exercised rather than merely
    written.
    """
    link = tmp_path / 'link'
    link.symlink_to(tmp_path / 'victim')
    opener = guarded_opener(overwrite=False, nofollow=0)

    with pytest.raises(PathContainmentError):
        opener(str(link), os.O_WRONLY | os.O_CREAT)

    assert not (tmp_path / 'victim').exists()


def test_r22_ac6_the_degraded_check_allows_an_ordinary_path(
    tmp_path: Path,
) -> None:
    """And the fallback still writes when there is no link to refuse.

    The control: a degraded branch that refused everything would pass
    the row above and break every download on the platform it exists
    for.
    """
    target = tmp_path / 'ordinary.bin'
    opener = guarded_opener(overwrite=False, nofollow=0)

    os.close(opener(str(target), os.O_WRONLY | os.O_CREAT))

    assert target.exists()


def test_the_degraded_symlink_check_passes_an_absent_path(
    tmp_path: Path,
) -> None:
    """:func:`_refuse_symlink_at` says nothing about an absent path.

    A download names a file it is about to create, so the ordinary case
    for this check is a path with nothing at it at all.

    Now asked relative to an open directory descriptor: the standalone
    ``refuse_symlink`` this used to call was superseded by the
    descriptor-relative form when the walk landed, because a check made
    against a bare path can be answered about a *different* directory
    than the one the open will use.
    """
    parent_fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        _refuse_symlink_at(parent_fd, 'nothing-here', str(tmp_path))
    finally:
        os.close(parent_fd)


# --- R22-AC5 / M19: partial files and the idempotent unlink ----------------


async def test_r22_ac5_a_failure_mid_write_leaves_no_partial_file(
    tmp_path: Path,
) -> None:
    """M19: a failed transfer stops orphaning what it had written.

    Driven by raising inside the ``async with`` -- which is exactly the
    shape of the real failure, since the cap that abandons an oversized
    body raises from inside the write loop.
    """
    base, _ = base_and_outside(tmp_path)
    target = base / 'partial.bin'

    with pytest.raises(RuntimeError):
        async with safe_writer(target) as handle:
            await handle.write(b'the first chunk')
            raise RuntimeError('the stream died mid-body')

    assert not target.exists()
    assert entries(base) == []


async def test_a_failed_overwrite_does_not_leave_a_truncated_file(
    tmp_path: Path,
) -> None:
    """The ``overwrite=True`` case is cleaned up too, and must be.

    What is left after a failed truncating write is not the caller's
    original file -- ``O_TRUNC`` already destroyed that -- it is a
    truncated one. Keeping it would preserve nothing and orphan a
    corrupt file under the name of a good one.
    """
    base, _ = base_and_outside(tmp_path)
    target = base / 'existing.bin'
    target.write_bytes(b'the original contents, which are long')

    with pytest.raises(RuntimeError):
        async with safe_writer(target, overwrite=True) as handle:
            await handle.write(b'short')
            raise RuntimeError('the stream died mid-body')

    assert not target.exists()


async def test_r22_ac4_safe_unlink_is_idempotent(tmp_path: Path) -> None:
    """Called twice, no exception -- R22-AC4, in the criterion's words.

    And the file really goes: a no-op ``safe_unlink`` would satisfy the
    "no exception" half while quietly leaving every download on disk.
    """
    target = tmp_path / 'downloaded.bin'
    target.write_bytes(b'remove me')

    await safe_unlink(target)
    await safe_unlink(target)

    assert not target.exists()


async def test_safe_unlink_reports_a_failure_that_is_not_absence(
    tmp_path: Path,
) -> None:
    """Idempotent about absence, not silent about everything.

    Swallowing every ``OSError`` would make a cleanup that cannot
    delete -- a read-only directory, a permission failure -- look
    successful, and the file would stay on disk with nothing said. Only
    ``FileNotFoundError`` is tolerated.
    """
    directory = tmp_path / 'a-directory'
    directory.mkdir()

    with pytest.raises(OSError):
        await safe_unlink(directory)


# --- R20: the canonicalisation never runs on the event loop ----------------


async def test_canonicalisation_does_not_run_on_the_event_loop(
    tmp_path: Path,
) -> None:
    """R20, for a call shape the package's AST scan cannot see.

    ``resolve_within`` stats every component of the path it
    canonicalises, and on a cold page cache that is latency every other
    in-flight request would pay. ``tests/test_no_blocking_io.py``
    matches ``Path(x).stat()`` and ``os.*`` **written in place**; it
    cannot see ``.resolve()``, and it cannot follow a call into a
    helper -- both limitations it documents about itself. So the
    property is held here instead, by recording which thread the
    blocking work actually ran on.

    Asserted as "not the loop's thread" rather than "some particular
    thread": ``asyncio.to_thread`` uses a pool and which worker answers
    is not this test's business.
    """
    base, _ = base_and_outside(tmp_path)
    ran_on: list[int] = []
    real_resolve = Path.resolve

    def recording(self: Path, strict: bool = False) -> Path:
        """Note the calling thread, then resolve normally.

        Args:
            self: The path being resolved.
            strict: Passed through.

        Returns:
            The resolved path.
        """
        ran_on.append(threading.get_ident())
        return real_resolve(self, strict=strict)

    # `type: ignore[method-assign]` -- `Path.resolve` is replaced for the
    # duration of this test to observe which thread the real call runs
    # on. mypy rightly refuses a method assignment on a stdlib class;
    # the substitution is the experiment, and it is undone in the
    # `finally` below.
    Path.resolve = recording  # type: ignore[method-assign]
    try:
        await resolve_within_async(base, 'sub/f')
    finally:
        Path.resolve = real_resolve  # type: ignore[method-assign]

    assert ran_on, 'nothing was canonicalised, so nothing was proven'
    loop_thread = threading.get_ident()
    assert loop_thread not in ran_on, (
        'Path.resolve ran on the event loop thread: a cold-cache '
        'canonicalisation stalls every other in-flight request (R20)')


async def test_the_caller_path_canonicalisation_is_off_the_loop_too(
    tmp_path: Path,
) -> None:
    """The same property for the caller-supplied entry point.

    :func:`resolve_caller_path` is what the four HTTP write sites call,
    so the row above would leave the busiest path unproven. Both the
    ``absolute()`` and the ``resolve()`` have to be inside the thread
    hop, which is why the sync half is a named function rather than
    inline.
    """
    ran_on: list[int] = []
    real_resolve = Path.resolve

    def recording(self: Path, strict: bool = False) -> Path:
        """Note the calling thread, then resolve normally.

        Args:
            self: The path being resolved.
            strict: Passed through.

        Returns:
            The resolved path.
        """
        ran_on.append(threading.get_ident())
        return real_resolve(self, strict=strict)

    # `type: ignore[method-assign]` -- `Path.resolve` is replaced for the
    # duration of this test to observe which thread the real call runs
    # on. mypy rightly refuses a method assignment on a stdlib class;
    # the substitution is the experiment, and it is undone in the
    # `finally` below.
    Path.resolve = recording  # type: ignore[method-assign]
    try:
        await resolve_caller_path(tmp_path / 'downloaded.bin')
    finally:
        Path.resolve = real_resolve  # type: ignore[method-assign]

    assert ran_on
    assert threading.get_ident() not in ran_on


async def test_the_refusal_classifier_does_not_run_on_the_event_loop(
    tmp_path: Path,
) -> None:
    """The third blocking site R20 covers: classifying a refused open.

    :func:`classify_refusal` stats the path to tell a symlink from an
    ordinary file from a directory, and it is reached on *every* refused
    write -- the ``EEXIST`` a default-refusing download earns is the
    ordinary case, not the rare one. The AST scan cannot see it for the
    same reason it cannot see ``.resolve()``: the ``os.path`` calls are
    inside a helper, one call away from the async function.

    The classifier is deliberately a plain ``def`` so the two protocol
    wrappers -- already inside an executor when their open fails -- can
    call it directly. That makes it :func:`safe_writer`'s job to hop,
    and this asserts the hop rather than trusting it.
    """
    target = tmp_path / 'occupied.bin'
    target.write_bytes(b'already here')
    ran_on: list[int] = []
    real_islink = os.path.islink

    def recording(path: Any) -> bool:
        """Note the calling thread, then answer normally.

        Args:
            path: The path being tested.

        Returns:
            Whether it is a symbolic link.
        """
        ran_on.append(threading.get_ident())
        return real_islink(path)

    # `type: ignore[assignment]` -- `os.path.islink` is replaced for the
    # duration of this test to observe which thread the real call runs
    # on. mypy rightly refuses an assignment to a stdlib function; the
    # substitution is the experiment, and it is undone in the `finally`
    # below.
    os.path.islink = recording  # type: ignore[assignment]
    try:
        with pytest.raises(ConfigurationError):
            async with safe_writer(target):
                pass
    finally:
        os.path.islink = real_islink  # type: ignore[assignment]

    assert ran_on, 'nothing was classified, so nothing was proven'
    assert threading.get_ident() not in ran_on, (
        'the refusal classifier stat ran on the event loop thread (R20)')


# --- the caller-supplied path -----------------------------------------------


async def test_an_absolute_caller_path_is_allowed(tmp_path: Path) -> None:
    """A caller naming ``/tmp/report.pdf`` has named a location.

    The distinction the design turns on: an absolute path from the
    *caller* is a legitimate destination, while an absolute path from a
    remote server's listing is an escape. Refusing both would break
    every documented use of ``download_filepath``.
    """
    target = tmp_path / 'report.pdf'

    assert await resolve_caller_path(target) == target.resolve()


async def test_a_caller_path_still_holds_its_parent_canonical(
    tmp_path: Path,
) -> None:
    """The parent is canonicalised even though containment is trivial.

    What a caller path gets from this is not confinement -- the base is
    its own directory -- but the guarded open: the value reaching it is
    a real location rather than one containing ``..``, and the final
    component is still held back for ``O_NOFOLLOW``.
    """
    inner = tmp_path / 'inner'
    inner.mkdir()

    resolved = await resolve_caller_path(inner / '..' / 'report.pdf')

    assert resolved == tmp_path.resolve() / 'report.pdf'


async def test_a_caller_path_naming_a_directory_is_refused(
    tmp_path: Path,
) -> None:
    """A path spelled as a directory has no file to write.

    R22's "a caller-supplied path that is itself a directory" edge case,
    on the half that is decidable from the string alone.
    """
    with pytest.raises(PathContainmentError):
        await resolve_caller_path(f'{tmp_path}/')


async def test_writing_to_an_existing_directory_reports_what_went_wrong(
    tmp_path: Path,
) -> None:
    """The other half of that edge case, decided by the open.

    A path that *is* a directory but is not spelled as one cannot be
    caught from the string, so it reaches the open -- where ``O_EXCL``
    refuses it as ``EEXIST``, the same errno an existing file earns.
    The two must not be reported the same way: "pass overwrite=True to
    replace it" is advice that cannot work on a directory, because the
    retry reaches the open again and fails. So the message names what
    is actually there.
    """
    directory = tmp_path / 'a-directory'
    directory.mkdir()

    with pytest.raises(ConfigurationError) as caught:
        async with safe_writer(await resolve_caller_path(directory)):
            pass

    assert 'is a directory' in str(caught.value)
    assert 'overwrite=True' not in str(caught.value)


async def test_a_missing_parent_directory_is_reported_as_itself(
    tmp_path: Path,
) -> None:
    """Not every refused open is a security finding.

    ``safe_writer`` translates exactly two failures -- a symlink and an
    existing file -- and lets the rest through unchanged. Translating
    more would report a mistyped directory as a path-containment
    attack, which is both wrong and alarming.
    """
    with pytest.raises(FileNotFoundError):
        async with safe_writer(tmp_path / 'no-such-dir' / 'f.bin'):
            pass


def test_caller_path_is_callable_synchronously(tmp_path: Path) -> None:
    """The sync half is a real function, not an implementation detail.

    It exists so the whole canonicalisation lands in one thread hop.
    Exercised directly so that a refactor collapsing it back into the
    coroutine -- which would put ``absolute()`` on the loop -- has
    something to break.
    """
    assert caller_path(tmp_path / 'f.bin') == (tmp_path / 'f.bin').resolve()


# --- the transfer-library seam ---------------------------------------------


def test_under_splits_a_composed_path_and_refuses_the_escape(
    tmp_path: Path,
) -> None:
    """The seam facing ``aioftp`` and ``asyncssh``, in isolation.

    Both libraries compose the server's entry names onto the local
    destination themselves, so what reaches this library again is a
    fully-formed ``/downloads/../victim/OWNED``. The split back into
    base and tail has to be textual: normalising first would delete the
    ``..`` this exists to catch.
    """
    base, _ = base_and_outside(tmp_path)

    with pytest.raises(PathContainmentError):
        under(base, str(base / '..' / 'outside' / 'OWNED'))


def test_under_accepts_the_bytes_asyncssh_speaks(tmp_path: Path) -> None:
    """The asyncssh filesystem protocol is bytes end to end.

    ``scandir`` yields byte filenames and ``_copy`` joins them with
    ``posixpath.join``, so a seam that took only ``str`` would fail on
    the protocol it was written for.
    """
    base, _ = base_and_outside(tmp_path)

    assert under(base, os.fsencode(str(base / 'f.bin'))) == (
        base.resolve() / 'f.bin')

    with pytest.raises(PathContainmentError):
        under(base, os.fsencode(str(base / '..' / 'outside' / 'OWNED')))


def test_under_refuses_a_path_outside_the_base_entirely(
    tmp_path: Path,
) -> None:
    """A path sharing no prefix with the base is refused, not rebased.

    Silently reinterpreting it relative to the base would turn a
    library bug -- or a future release composing paths differently --
    into a write somewhere nobody named.
    """
    base, outside = base_and_outside(tmp_path)

    with pytest.raises(PathContainmentError):
        under(base, str(outside / 'f.bin'))


def test_under_allows_the_base_itself(tmp_path: Path) -> None:
    """The destination directory is inside itself.

    Both libraries ``mkdir`` and ``isdir`` the destination root before
    they compose anything onto it, so refusing it would break every
    directory download at the first call.
    """
    base, _ = base_and_outside(tmp_path)

    assert under(base, str(base)) == base.resolve()


async def test_a_relative_candidate_type_survives_the_round_trip(
    tmp_path: Path,
) -> None:
    """``Path`` and ``str`` candidates answer identically.

    Callers hand this both -- ``protocol_info`` carries strings, the
    transfer seams carry ``Path`` and ``bytes`` -- and a check whose
    answer depended on which would be a silent gap on one of them.
    """
    base, _ = base_and_outside(tmp_path)

    assert resolve_within(base, 'sub/f') == resolve_within(
        base, Path('sub/f'))
    assert await resolve_within_async(base, Path('sub/f')) == (
        base.resolve() / 'sub' / 'f')


def test_the_refusal_names_the_path_it_refused(tmp_path: Path) -> None:
    """A refusal a caller cannot act on is a refusal they will disable.

    The message carries the offending name and the base it escaped, so
    a consumer reading an ``ok=False`` envelope can tell a hostile
    server from their own mistyped configuration.
    """
    base, _ = base_and_outside(tmp_path)

    with pytest.raises(PathContainmentError) as caught:
        resolve_within(base, '../evil')

    message = str(caught.value)
    assert '../evil' in message
    assert str(base.resolve()) in message


async def test_concurrent_writers_to_one_path_do_not_both_succeed(
    tmp_path: Path,
) -> None:
    """R22's concurrency edge case: ``O_EXCL`` makes the second fail.

    Two downloads racing for one destination is the documented case,
    and the answer is not last-writer-wins: exactly one creates the
    file and the other is refused. Asserted through a real
    ``asyncio.gather`` rather than by reasoning about the flag, because
    the claim is about what two coroutines actually observe.
    """
    base, _ = base_and_outside(tmp_path)
    target = base / 'contested.bin'

    async def download(payload: bytes) -> Optional[Text]:
        """Write ``payload`` to the contested path.

        Args:
            payload: What this writer would write.

        Returns:
            None on success, or the error's code on refusal.
        """
        try:
            async with safe_writer(target) as handle:
                await asyncio.sleep(0)
                await handle.write(payload)
        except ConfigurationError as err:
            return err.code
        return None

    outcomes = await asyncio.gather(download(b'first'), download(b'second'))

    assert sorted(str(outcome) for outcome in outcomes) == ['CONFIG', 'None']
    assert target.read_bytes() in (b'first', b'second')


# --- N2/N3/N4: the descriptor walk -----------------------------------------
#
# One construction answers all three, so the tests are grouped rather than
# split by finding: they exercise the same `open_within` from three angles.
# Each was written against the pre-fix code first and observed to FAIL.


def test_n2_a_directory_component_swapped_after_the_check_cannot_redirect(
    tmp_path: Path,
) -> None:
    """N2, asserted deterministically rather than by winning a race.

    The defect: ``resolve_within`` canonicalises the parent and answers a
    *string*; handing that string to ``os.open`` makes the kernel walk
    every component again, so a directory component swapped in between is
    followed. Measured on the pre-fix code at 6 escaped payloads per 3000
    writes under a flipping component.

    A scheduling race would be flaky and would prove nothing on the run
    that lost, so the swap is performed **at a guaranteed moment**: the
    hook fires when the leaf component is opened, and replaces the
    already-traversed directory ``hop`` with a symbolic link pointing
    outside the base.

    That instant is chosen because it is the one both implementations
    share, which is what makes this a discriminator rather than a
    tautology. The old code opened the leaf by its **full path**, so the
    kernel re-walked ``hop`` after the swap and the payload landed
    outside. The new code opens the leaf by **name against ``hop``'s
    descriptor**, so the swap changes a name nobody consults again and
    the payload lands where it was checked to land.
    """
    base, outside = base_and_outside(tmp_path)
    hop = base / 'hop'
    hop.mkdir()
    target = base.resolve() / 'hop' / 'payload.bin'

    real_open = os.open
    swapped: list[bool] = []

    def swap_at_the_leaf(
        path: Any, flags: int, mode: int = 0o777, **kwargs: Any,
    ) -> int:
        """Replace ``hop`` with a link outside, as the leaf is opened.

        Args:
            path: The component or path being opened.
            flags: The open flags.
            mode: The creation mode.
            kwargs: The rest, including ``dir_fd``.

        Returns:
            The real descriptor.
        """
        if str(path).endswith('payload.bin') and not swapped:
            swapped.append(True)
            hop.rename(base / 'hop-moved-away')
            (base / 'hop').symlink_to(outside)
        return real_open(path, flags, mode, **kwargs)

    # `type: ignore[assignment]` -- `os.open` is replaced to force the
    # swap at the one instant that matters. Undone in the `finally`.
    os.open = swap_at_the_leaf  # type: ignore[assignment]
    try:
        descriptor = guarded_opener(overwrite=False)(
            str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    finally:
        os.open = real_open  # type: ignore[assignment]
    try:
        os.write(descriptor, b'PAYLOAD')
    finally:
        os.close(descriptor)

    assert swapped, 'the swap never fired, so the window was never tested'
    assert entries(outside) == [], (
        'the write followed a directory component swapped after it was '
        'checked -- the N2 TOCTOU is open')
    assert (base / 'hop-moved-away' / 'payload.bin').read_bytes() == b'PAYLOAD'


def test_n2_a_symlinked_directory_component_is_refused_at_the_walk(
    tmp_path: Path,
) -> None:
    """The walk refuses a symlinked component even reached directly.

    ``resolve_within`` would normally have canonicalised this away, so
    this asserts the *opener's own* guarantee rather than the resolver's
    -- the two are separate halves and the opener must not depend on
    having been called correctly.
    """
    base, outside = base_and_outside(tmp_path)
    (base / 'link').symlink_to(outside)

    with pytest.raises(OSError):
        guarded_opener(overwrite=False)(
            str(base / 'link' / 'f.bin'), os.O_WRONLY | os.O_CREAT)

    assert entries(outside) == []


def test_n3_a_fifo_target_is_refused_instead_of_hanging_forever(
    tmp_path: Path,
) -> None:
    """N3: the defect was an unbounded hang, not merely a wrong answer.

    Opening a FIFO for writing blocks until a reader attaches. That open
    runs in a threadpool worker, where the caller's ``timeout`` cannot
    reach it -- so ``request(timeout=3)`` measured no return after 6s.
    ``O_NONBLOCK`` turns the block into ``ENXIO``, which is typed as a
    containment refusal.

    The test itself would hang rather than fail if this regressed, which
    is the honest shape: a bounded assertion cannot be written for
    "returns at all" without a watchdog, and pytest's own timeout would
    report it.
    """
    target = tmp_path / 'pipe'
    os.mkfifo(target)

    with pytest.raises(PathContainmentError) as caught:
        guarded_opener(overwrite=True)(
            str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

    assert 'named pipe' in str(caught.value)


def test_n3_a_fifo_with_a_reader_is_still_refused(tmp_path: Path) -> None:
    """``O_NONBLOCK`` alone is not the fix, and this is why.

    A FIFO that already has a reader attached opens *immediately* and
    without error, so the ``ENXIO`` path never runs and an implementation
    relying on the flag alone would accept it -- streaming the download
    into a pipe some other process is reading. Only the ``fstat`` on the
    returned descriptor catches this one, which is the reason both
    mechanisms are present rather than either alone.
    """
    target = tmp_path / 'pipe'
    os.mkfifo(target)
    reader = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(PathContainmentError) as caught:
            guarded_opener(overwrite=True)(
                str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    finally:
        os.close(reader)

    assert 'named pipe' in str(caught.value)


def test_a_socket_is_refused_and_named_a_socket(tmp_path: Path) -> None:
    """The refused-open message names what is there, on every platform.

    A container run caught this and the host suite could not: Linux
    answers ``ENXIO`` for a bound unix socket, exactly as it does for a
    reader-less FIFO, so a branch that hardcoded "named pipe" told a
    Linux operator their socket was a pipe. macOS answers
    ``EOPNOTSUPP`` for the same open, took the untyped path, and left
    the assertion satisfied by its ``OSError`` alternative -- which is
    why only the container saw it.

    Both errnos are now refusals of the same kind, and the noun is read
    off the filesystem rather than assumed. Asserted here on the
    refusal *type* as well as the wording, because the macOS half of the
    defect was a bare ``OSError`` escaping where a containment refusal
    was owed.
    """
    # Bound from inside a short-named directory: `AF_UNIX` paths are
    # capped near 104 bytes on macOS and pytest's `tmp_path` alone
    # already exceeds it, so binding by absolute path fails before the
    # code under test is reached. The *parent* is then canonicalised for
    # the open, because `/tmp` is itself a symlink on macOS and the
    # descriptor walk refuses a symlinked component by design -- which
    # would fail this test for the wrong reason.
    endpoint = Path(tempfile.mkdtemp(dir='/tmp')) / 's'
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(endpoint))
        target = endpoint.parent.resolve() / endpoint.name
        with pytest.raises(PathContainmentError) as caught:
            guarded_opener(overwrite=True)(
                str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    finally:
        server.close()
        shutil.rmtree(endpoint.parent, ignore_errors=True)

    assert 'socket' in str(caught.value)


def test_the_refusal_still_names_something_when_the_entry_vanishes(
    tmp_path: Path,
) -> None:
    """The describing ``lstat`` is best-effort, and says so when it fails.

    Naming the kind means asking the filesystem a second time, after the
    open has already been refused -- so the entry can be gone by then.
    That is harmless (nothing can be written; the open failed) but it
    must not turn a clean containment refusal into an unrelated
    ``FileNotFoundError`` from the error path itself. Forced here by
    removing the FIFO between the two syscalls, which is the race the
    fallback exists for.
    """
    target = tmp_path / 'pipe'
    os.mkfifo(target)

    real_lstat = os.lstat

    def vanishing_lstat(*args: Any, **kwargs: Any) -> os.stat_result:
        """Delete the FIFO, then answer as the real ``lstat`` would."""
        target.unlink(missing_ok=True)
        return real_lstat(*args, **kwargs)

    with mock.patch.object(os, 'lstat', vanishing_lstat):
        with pytest.raises(PathContainmentError) as caught:
            guarded_opener(overwrite=True)(
                str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

    assert 'unconnected device' in str(caught.value)


def test_n3_a_character_device_target_is_refused(tmp_path: Path) -> None:
    """``/dev/null`` reported ``ok=True`` and discarded the whole body.

    The worst shape of this defect: no error, no file, and a caller told
    the download succeeded. Refused by ``S_ISREG`` on the descriptor.
    """
    with pytest.raises(PathContainmentError) as caught:
        guarded_opener(overwrite=True)(
            os.devnull, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

    assert 'character device' in str(caught.value)


async def test_n3_a_devnull_download_no_longer_reports_success(
    tmp_path: Path,
) -> None:
    """The same finding at the seam a caller actually uses.

    Asserted through ``safe_writer`` rather than the opener, because the
    envelope a consumer sees is built from what this context manager
    raises -- and the defect was precisely that it raised nothing.
    """
    with pytest.raises(PathContainmentError):
        async with safe_writer(Path(os.devnull), overwrite=True) as handle:
            await handle.write(b'this body would be silently discarded')


def test_n4_a_hardlinked_target_is_refused(tmp_path: Path) -> None:
    """N4: ``is_symlink()`` is False for a hard link, so it was written.

    The reviewer overwrote a victim file holding ``'secret-original'``
    and got ``ok=True``. A hard link is the same inode under a second
    name, so writing the download writes the victim.
    """
    victim = tmp_path / 'victim'
    victim.write_bytes(b'secret-original')
    target = tmp_path / 'download.bin'
    os.link(victim, target)

    with pytest.raises(PathContainmentError) as caught:
        guarded_opener(overwrite=True)(
            str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

    assert 'hard link' in str(caught.value)
    assert victim.read_bytes() == b'secret-original'


def test_n4_overwrite_truncates_only_after_the_target_is_judged(
    tmp_path: Path,
) -> None:
    """The subtle half of N4, and a defect this fix had in its first draft.

    Passing ``O_TRUNC`` to the open makes the kernel truncate *as part of
    opening*, before any ``fstat`` can refuse the file. The refusal then
    fires correctly -- and the victim has already been emptied, which is
    a worse outcome than the write-through the check exists to prevent.

    Asserted on the victim's **contents**, not on the exception: the
    exception was already correct in the broken draft. That is what makes
    this row worth its own test rather than an extra assertion above.
    """
    victim = tmp_path / 'victim'
    victim.write_bytes(b'secret-original')
    target = tmp_path / 'download.bin'
    os.link(victim, target)

    with pytest.raises(PathContainmentError):
        guarded_opener(overwrite=True)(
            str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

    assert victim.read_bytes() == b'secret-original', (
        'the victim was truncated by the open before the guard refused it')


def test_n4_overwrite_still_truncates_an_ordinary_file(
    tmp_path: Path,
) -> None:
    """The control: moving truncation off the open must not lose it.

    Without this row, an implementation that simply dropped ``O_TRUNC``
    would pass every assertion above while leaving the tail of a longer
    previous download appended to every shorter new one -- silent data
    corruption that no test here would otherwise notice.
    """
    target = tmp_path / 'existing.bin'
    target.write_bytes(b'the original contents, which are much longer')

    descriptor = guarded_opener(overwrite=True)(
        str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(descriptor, b'short')
    finally:
        os.close(descriptor)

    assert target.read_bytes() == b'short'


def test_a_hardlink_is_refused_by_default_too(tmp_path: Path) -> None:
    """The default path refuses the hard link as well, by ``O_EXCL``.

    ``overwrite=False`` never reaches the ``fstat``: ``O_EXCL`` fails the
    open with ``EEXIST`` first, and :func:`safe_writer` classifies that
    as configuration. The raw opener therefore raises ``FileExistsError``
    here rather than a typed error -- the translation happens one layer
    up, which is exactly where the existing suite already pins it.

    Worth its own row because the security outcome is what matters and
    it is identical on both paths: the victim is untouched either way.
    """
    victim = tmp_path / 'victim'
    victim.write_bytes(b'secret-original')
    target = tmp_path / 'download.bin'
    os.link(victim, target)

    with pytest.raises(FileExistsError):
        guarded_opener(overwrite=False)(
            str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

    assert victim.read_bytes() == b'secret-original'


def test_a_directory_target_is_refused_by_the_walk(tmp_path: Path) -> None:
    """A path that is itself a directory cannot be opened for writing.

    R22 already covered this through ``O_EXCL``'s ``EEXIST``; the walk
    reaches it first now, so the behaviour is re-pinned at the new site.
    """
    directory = tmp_path / 'a-directory'
    directory.mkdir()

    with pytest.raises((OSError, ConfigurationError, PathContainmentError)):
        guarded_opener(overwrite=True)(
            str(directory), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)


def test_the_walk_refuses_a_path_with_no_components(tmp_path: Path) -> None:
    """The root itself names no file to write.

    Unreachable through ``resolve_within``, which refuses a directory
    candidate earlier, but :func:`open_within` is a separate primitive
    and must answer for its own inputs rather than trusting its caller.
    """
    with pytest.raises(PathContainmentError) as caught:
        open_within(os.sep, os.O_WRONLY | os.O_CREAT)

    assert 'names a directory' in str(caught.value)


def test_the_walk_anchors_a_relative_path_at_the_working_directory(
    tmp_path: Path,
) -> None:
    """A relative path is anchored at ``.``, matching ``resolve_within``.

    Not a shape this library produces -- both entry points canonicalise
    to absolute first -- but the branch exists and an untested branch is
    an unverified claim.
    """
    previous = os.getcwd()
    os.chdir(tmp_path)
    try:
        descriptor = open_within('relative.bin', os.O_WRONLY | os.O_CREAT)
        os.close(descriptor)
    finally:
        os.chdir(previous)

    assert (tmp_path / 'relative.bin').exists()


def test_the_degraded_walk_refuses_a_symlinked_leaf(tmp_path: Path) -> None:
    """The no-``O_NOFOLLOW`` fallback, now asked against the parent's fd.

    R22-AC6 requires the degraded branch to carry real coverage, so the
    flag is passed as 0 explicitly. The check is a separate syscall and
    keeps its documented window; what changed is that it asks about the
    leaf relative to the directory descriptor the open will use, rather
    than about a path that may name a different directory by then.
    """
    victim = tmp_path / 'victim'
    link = tmp_path / 'link'
    link.symlink_to(victim)

    with pytest.raises(PathContainmentError):
        open_within(str(link), os.O_WRONLY | os.O_CREAT, nofollow=0)

    assert not victim.exists()


def test_the_degraded_walk_allows_an_ordinary_absent_path(
    tmp_path: Path,
) -> None:
    """And the fallback still creates a file when there is no link.

    The control for the row above: a degraded branch that refused
    everything would satisfy it and break every download on the platform
    the branch exists for. Also covers the ``FileNotFoundError`` arm of
    the check, which is the ordinary case -- a download names a file that
    is not there yet.
    """
    target = tmp_path / 'ordinary.bin'

    descriptor = open_within(
        str(target), os.O_WRONLY | os.O_CREAT, nofollow=0)
    os.close(descriptor)

    assert target.exists()


def test_the_degraded_walk_allows_an_existing_ordinary_file(
    tmp_path: Path,
) -> None:
    """The degraded check tolerates a file that is present and not a link.

    Distinct from the row above: that one exercises the ``lstat`` raising
    ``FileNotFoundError``, this one exercises it *succeeding* on a
    non-symlink. Two different arms of the same branch.
    """
    target = tmp_path / 'already-here.bin'
    target.write_bytes(b'previous')

    descriptor = open_within(
        str(target), os.O_WRONLY, nofollow=0)
    os.close(descriptor)

    assert target.read_bytes() == b'previous'


def test_a_socket_target_is_named_in_the_refusal(tmp_path: Path) -> None:
    """The refusal names what it found, for every kind a write can hit.

    A message that says only "not a regular file" leaves the operator to
    go and look; naming the kind is the difference between a diagnosable
    error and a puzzle. Sockets are the one remaining kind reachable
    without root, so the enumeration is exercised rather than asserted
    from the source.
    """
    # Bound from inside a short-named directory: `AF_UNIX` paths are
    # capped near 104 bytes on macOS and pytest's `tmp_path` alone
    # already exceeds it, so binding by absolute path fails before the
    # code under test is reached.
    endpoint = Path(tempfile.mkdtemp(dir='/tmp')) / 's'
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(endpoint))
        with pytest.raises((PathContainmentError, OSError)) as caught:
            guarded_opener(overwrite=True)(
                str(endpoint.resolve()),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    finally:
        server.close()
        shutil.rmtree(endpoint.parent, ignore_errors=True)

    assert isinstance(caught.value, OSError) or 'socket' in str(caught.value)


def test_describe_names_every_kind_it_enumerates() -> None:
    """:func:`_describe` answers for each mode, and falls back safely.

    A table test rather than one filesystem object per kind: block
    devices cannot be created without root, so exercising the branch
    through a real file is impossible on CI. The fallback arm matters
    most -- an unknown mode must still produce a usable message rather
    than an index error.
    """
    assert _describe(stat.S_IFIFO) == 'named pipe'
    assert _describe(stat.S_IFCHR) == 'character device'
    assert _describe(stat.S_IFBLK) == 'block device'
    assert _describe(stat.S_IFSOCK) == 'socket'
    assert _describe(stat.S_IFDIR) == 'directory'
    assert _describe(stat.S_IFLNK) == 'symbolic link'
    assert _describe(stat.S_IFREG) == 'special file'


def test_the_walk_closes_every_descriptor_it_opens(tmp_path: Path) -> None:
    """No descriptor leak, on the success path or on a refusal.

    The walk holds one directory descriptor per component, and a leak
    would be invisible until a long-running consumer exhausted its file
    limit -- a failure that surfaces far from its cause. Measured by
    counting open descriptors around a batch of both outcomes.
    """
    deep = tmp_path / 'a' / 'b' / 'c'
    deep.mkdir(parents=True)
    victim = tmp_path / 'victim'
    victim.write_bytes(b'x')
    linked = tmp_path / 'linked'
    os.link(victim, linked)

    def open_descriptor_count() -> int:
        """Count this process's open descriptors.

        Returns:
            How many of the first 512 descriptors are open.
        """
        total = 0
        for candidate in range(512):
            try:
                os.fstat(candidate)
            except OSError:
                continue
            total += 1
        return total

    before = open_descriptor_count()
    for index in range(20):
        descriptor = guarded_opener(overwrite=False)(
            str(deep / f'f{index}.bin'), os.O_WRONLY | os.O_CREAT)
        os.close(descriptor)
        with pytest.raises(PathContainmentError):
            guarded_opener(overwrite=True)(
                str(linked), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

    assert open_descriptor_count() == before


def test_the_walk_propagates_an_unrelated_open_failure(
    tmp_path: Path,
) -> None:
    """Only ``ENXIO`` is translated; every other errno arrives as itself.

    Translating more would report a full disk or a permission failure as
    a containment finding, which is both wrong and alarming -- the same
    principle ``classify_refusal`` already applies at the other seam.
    """
    with pytest.raises(FileNotFoundError):
        guarded_opener(overwrite=False)(
            str(tmp_path / 'no-such-dir' / 'f.bin'),
            os.O_WRONLY | os.O_CREAT)


def test_the_write_descriptor_is_left_in_blocking_mode(
    tmp_path: Path,
) -> None:
    """``O_NONBLOCK`` is a means to the N3 check, not a lasting change.

    A descriptor left non-blocking can accept a short write -- fewer
    bytes than it was handed, with no error -- and every caller here
    streams a body chunk by chunk through ``aiofiles``. That would be a
    silently truncated download, so the flag is cleared once the ``fstat``
    it enabled has run.
    """
    target = tmp_path / 'out.bin'

    descriptor = guarded_opener(overwrite=False)(
        str(target), os.O_WRONLY | os.O_CREAT)
    try:
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    finally:
        os.close(descriptor)

    assert not flags & os.O_NONBLOCK, (
        'the write descriptor is still non-blocking, so a short write '
        'can silently truncate a download')


def test_restoring_blocking_is_a_no_op_where_the_flag_does_not_exist(
    tmp_path: Path,
) -> None:
    """The platform branch, exercised on a platform that has the flag.

    Where ``O_NONBLOCK`` is absent the constant is 0, there is nothing to
    clear, and the function must return without touching the descriptor.
    Passing 0 explicitly reaches that branch here -- the same technique
    ``guarded_opener``'s ``nofollow`` parameter uses, and for the same
    reason: a branch guarded by ``sys.platform`` can never run on CI and
    so carries no coverage at all.
    """
    target = tmp_path / 'out.bin'
    descriptor = os.open(
        str(target), os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK, FILE_MODE)
    try:
        _restore_blocking(descriptor, nonblock=0)
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    finally:
        os.close(descriptor)

    assert flags & os.O_NONBLOCK, (
        'the no-op branch cleared the flag anyway, so it is not a no-op')

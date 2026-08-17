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
import os
import threading
from pathlib import Path
from typing import Any, Optional, Text

import pytest

from async_gateway.utils.exceptions import (
    ConfigurationError,
    PathContainmentError,
)
from async_gateway.utils.paths import (
    FILE_MODE,
    caller_path,
    guarded_opener,
    refuse_symlink,
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

    ``O_TRUNC`` is asserted **absent** for the same reason: it is what
    ``'wb'`` asks for, and leaving it beside ``O_EXCL`` would be a
    contradiction the kernel resolves in nobody's favour.
    """
    seen: list[int] = []

    def record(path: Text, flags: int) -> int:
        """Capture the flags instead of opening anything.

        Args:
            path: The path, unused.
            flags: What the opener was given.

        Returns:
            A file descriptor for ``/dev/null``, so the caller has
            something real to close.
        """
        seen.append(flags)
        return os.open(os.devnull, os.O_WRONLY)

    opener = guarded_opener(overwrite=False)
    fd = opener(str(tmp_path / 'f'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    os.close(fd)
    flags = seen[0] if seen else _flags_from(opener, tmp_path)

    assert flags & os.O_NOFOLLOW, 'a symlink at the target can be followed'
    assert flags & os.O_EXCL, 'an existing file can be replaced'
    assert not flags & os.O_TRUNC, 'O_TRUNC contradicts O_EXCL'


def _flags_from(opener: Any, tmp_path: Path) -> int:
    """Return the flags ``opener`` computes, by intercepting ``os.open``.

    Args:
        opener: The opener under test.
        tmp_path: A directory to name a throwaway path in.

    Returns:
        The flags the opener passed on.
    """
    captured: list[int] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        """Record the flags and open ``/dev/null`` instead.

        Args:
            path: The path, unused.
            flags: What was computed.
            mode: The mode, unused.
            kwargs: The rest, unused.

        Returns:
            An open descriptor.
        """
        captured.append(flags)
        return real_open(os.devnull, os.O_WRONLY)

    # `type: ignore[assignment]` -- `os.open` is replaced for the
    # duration of this test to observe which thread the real open runs
    # on. mypy rightly refuses an assignment to a stdlib function; the
    # substitution is the experiment, and it is undone in the `finally`
    # below.
    os.open = spy  # type: ignore[assignment]
    try:
        os.close(opener(
            str(tmp_path / 'f'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC))
    finally:
        os.open = real_open  # type: ignore[assignment]
    return captured[0]


def test_r22_ac3_the_mode_reaches_the_kernel(tmp_path: Path) -> None:
    """0600 is passed to ``os.open``, not applied afterwards.

    A ``chmod`` after the fact leaves the file world-readable for the
    window between the create and the chmod, which on the shared-host
    case M18 describes is the whole exposure. Asserted on the argument
    because the resulting mode is also filtered by ``umask``, and a
    machine with a permissive umask would let a wrong constant pass.
    """
    captured: list[int] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        """Record the mode and delegate.

        Args:
            path: The path to open.
            flags: The flags.
            mode: The creation mode under test.
            kwargs: The rest.

        Returns:
            The real descriptor.
        """
        captured.append(mode)
        return real_open(path, flags, mode, **kwargs)

    # `type: ignore[assignment]` -- `os.open` is replaced for the
    # duration of this test to observe which thread the real open runs
    # on. mypy rightly refuses an assignment to a stdlib function; the
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
    """:func:`refuse_symlink` says nothing about a path that is not there.

    A download names a file it is about to create, so the ordinary case
    for this check is a path with nothing at it at all.
    """
    refuse_symlink(str(tmp_path / 'nothing-here'))


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

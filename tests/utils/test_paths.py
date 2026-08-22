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
import errno
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

import aiofiles

import pytest

from asyncio_gateway.utils import paths as path_utils
from asyncio_gateway.utils.exceptions import (
    ConfigurationError,
    LocalWriteError,
    PathContainmentError,
    ResponseTooLargeError,
    SerializationError,
)
from asyncio_gateway.utils.paths import (
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

from tests.cancellation import (
    assert_cancelled_error_survives_task_boundary,
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


# --- PE-50: held-descriptor S3 upload reads -------------------------------


async def test_pe50_guarded_upload_read_returns_bytes_from_one_regular_file(
    tmp_path: Path,
) -> None:
    """The upload reader returns the bytes read through its held descriptor."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'one bounded upload')

    payload = await path_utils.read_guarded_file(
        source, max_bytes=1024, chunk_size=4)

    assert payload == b'one bounded upload'


async def test_pe50_guarded_upload_read_refuses_an_observed_oversize(
    tmp_path: Path,
) -> None:
    """The positive upload ceiling is enforced while bytes are observed."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'five!')

    with pytest.raises(ConfigurationError) as caught:
        await path_utils.read_guarded_file(
            source, max_bytes=4, chunk_size=2)

    assert 'max_bytes=4' in str(caught.value)


async def test_pe50_guarded_reader_pins_the_parent_before_leaf_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Swapping the directory at leaf-open cannot substitute the source."""
    live = tmp_path / 'live'
    live.mkdir()
    source = live / 'upload.bin'
    source.write_bytes(b'held inode')
    replacement = tmp_path / 'replacement'
    replacement.mkdir()
    (replacement / source.name).write_bytes(b'substituted inode')
    retired = tmp_path / 'retired'
    original_open = path_utils.os.open
    swapped = False

    def swapping_open(
        name: Any,
        flags: int,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        """Swap the parent immediately before the upload leaf is opened."""
        nonlocal swapped
        spelled = os.fspath(name)
        if not swapped and (
            spelled == source.name or spelled == os.fspath(source)
        ):
            live.rename(retired)
            replacement.rename(live)
            swapped = True
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(path_utils.os, 'open', swapping_open)

    payload = await path_utils.read_guarded_file(
        source, max_bytes=1024, chunk_size=32)

    assert swapped is True
    assert payload == b'held inode'
    assert source.read_bytes() == b'substituted inode'


async def test_pe50_cancelled_read_waits_for_worker_before_closing_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation cannot close a descriptor under an in-flight os.read."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'body')
    started = threading.Event()
    release = threading.Event()
    read_descriptor: list[int] = []
    closed_descriptors: list[int] = []
    original_close = path_utils.os.close

    def blocking_read(descriptor: int, chunk_size: int) -> bytes:
        """Hold the worker until the test has delivered cancellation."""
        del chunk_size
        read_descriptor.append(descriptor)
        started.set()
        release.wait(timeout=5)
        return b''

    def recording_close(descriptor: int) -> None:
        """Record exactly when the upload descriptor is closed."""
        if started.is_set():
            closed_descriptors.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(path_utils.os, 'read', blocking_read)
    monkeypatch.setattr(path_utils.os, 'close', recording_close)
    reading = asyncio.create_task(path_utils.read_guarded_file(
        source, max_bytes=1024, chunk_size=32))
    assert await asyncio.to_thread(started.wait, 2)

    try:
        reading.cancel()
        await asyncio.sleep(0)

        assert read_descriptor[0] not in closed_descriptors
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await reading
    assert read_descriptor[0] in closed_descriptors


async def test_pe50_atomic_replace_uses_one_pinned_parent_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A parent swap at commit cannot redirect either replace operand."""
    live = tmp_path / 'live'
    live.mkdir()
    target = live / 'target.bin'
    target.write_bytes(b'old')
    replacement = tmp_path / 'replacement'
    replacement.mkdir()
    (replacement / target.name).write_bytes(b'decoy')
    retired = tmp_path / 'retired'
    original_replace = path_utils.os.replace
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def swapping_replace(*args: Any, **kwargs: Any) -> None:
        """Swap the name after the primitive has pinned its parent."""
        calls.append((args, kwargs))
        live.rename(retired)
        replacement.rename(live)
        original_replace(*args, **kwargs)

    monkeypatch.setattr(path_utils.os, 'replace', swapping_replace)

    async def chunks() -> Any:
        """Yield the replacement through the production writer."""
        yield b'new'

    written = await path_utils.stream_to_path(
        target, chunks(), overwrite=True, max_bytes=3)

    assert written == 3
    assert retired.joinpath(target.name).read_bytes() == b'new'
    assert live.joinpath(target.name).read_bytes() == b'decoy'
    assert calls[0][1]['src_dir_fd'] == calls[0][1]['dst_dir_fd']


@pytest.mark.parametrize('setting', ['max_bytes', 'chunk_size'])
@pytest.mark.parametrize('value', [True, 0, -1, 1.5, '1'])
async def test_pe50_guarded_reader_rejects_invalid_positive_limits(
    tmp_path: Path,
    setting: str,
    value: Any,
) -> None:
    """Both byte controls are positive non-boolean integers."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'body')
    kwargs: dict[str, Any] = {'max_bytes': 8, 'chunk_size': 4}
    kwargs[setting] = value

    with pytest.raises(ConfigurationError):
        await path_utils.read_guarded_file(source, **kwargs)


async def test_pe50_stream_refuses_declared_and_observed_oversize(
    tmp_path: Path,
) -> None:
    """The shared writer checks both advertised and running byte counts."""
    advertised_target = tmp_path / 'advertised.bin'
    iterated = False

    async def chunks() -> Any:
        """Record whether a declared-size refusal touched the body."""
        nonlocal iterated
        iterated = True
        yield b'body'

    with pytest.raises(ResponseTooLargeError):
        await path_utils.stream_to_path(
            advertised_target,
            chunks(),
            overwrite=False,
            max_bytes=3,
            advertised_bytes=4,
        )
    assert iterated is False
    assert not advertised_target.exists()

    observed_target = tmp_path / 'observed.bin'
    with pytest.raises(ResponseTooLargeError):
        await path_utils.stream_to_path(
            observed_target,
            chunks(),
            overwrite=False,
            max_bytes=3,
        )
    assert not observed_target.exists()


def test_pe50_reader_uses_required_leaf_open_flags_and_parent_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The upload leaf is read-only, no-follow, close-on-exec, and relative."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'body')
    original_open = path_utils.os.open
    leaf_call: list[tuple[int, dict[str, Any]]] = []

    def recording_open(
        name: Any,
        flags: int,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        """Capture only the final upload-leaf open."""
        if os.fspath(name) == source.name:
            leaf_call.append((flags, dict(kwargs)))
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(path_utils.os, 'open', recording_open)
    descriptor = path_utils._open_read_descriptor(source)
    os.close(descriptor)

    flags, keywords = leaf_call[0]
    assert not flags & (os.O_WRONLY | os.O_RDWR)
    assert flags & path_utils.O_NOFOLLOW
    assert flags & path_utils.O_CLOEXEC
    assert isinstance(keywords['dir_fd'], int)


def test_pe50_reader_fallback_accepts_the_same_regular_inode(
    tmp_path: Path,
) -> None:
    """The no-O_NOFOLLOW fallback admits an unchanged regular source."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'fallback')

    descriptor = path_utils._open_read_descriptor(source, nofollow=0)
    try:
        assert os.read(descriptor, 32) == b'fallback'
    finally:
        os.close(descriptor)


def test_pe50_reader_fallback_refuses_symlink_and_nonregular_leaf(
    tmp_path: Path,
) -> None:
    """Fallback lstat refuses both links and directories before leaf open."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'body')
    linked = tmp_path / 'linked.bin'
    linked.symlink_to(source)

    with pytest.raises(PathContainmentError):
        path_utils._open_read_descriptor(linked, nofollow=0)
    with pytest.raises(ConfigurationError):
        path_utils._open_read_descriptor(tmp_path, nofollow=0)


@pytest.mark.parametrize('changed_field', ['st_dev', 'st_ino'])
def test_pe50_reader_fallback_refuses_changed_identity_and_closes_leaf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    changed_field: str,
) -> None:
    """Either device or inode mismatch closes and refuses the opened leaf."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'body')
    real_lstat = path_utils.os.lstat
    real_open = path_utils.os.open
    leaf_descriptor: list[int] = []

    def changed_lstat(
        name: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Return regular metadata with one identity field changed."""
        info = real_lstat(name, *args, **kwargs)
        values = {
            'st_mode': info.st_mode,
            'st_dev': info.st_dev,
            'st_ino': info.st_ino,
        }
        values[changed_field] += 1
        return mock.Mock(**values)

    def recording_open(
        name: Any,
        flags: int,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        """Remember the fallback's final descriptor."""
        descriptor = real_open(name, flags, *args, **kwargs)
        if os.fspath(name) == source.name:
            leaf_descriptor.append(descriptor)
        return descriptor

    monkeypatch.setattr(path_utils.os, 'lstat', changed_lstat)
    monkeypatch.setattr(path_utils.os, 'open', recording_open)

    with pytest.raises(PathContainmentError):
        path_utils._open_read_descriptor(source, nofollow=0)

    with pytest.raises(OSError):
        os.fstat(leaf_descriptor[0])


def test_pe50_reader_refuses_symlink_directory_and_missing_leaf(
    tmp_path: Path,
) -> None:
    """Keep no-follow/fstat refusals typed and unrelated errno unchanged."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'body')
    linked = tmp_path / 'linked.bin'
    linked.symlink_to(source)

    with pytest.raises(PathContainmentError):
        path_utils._open_read_descriptor(linked)
    with pytest.raises(ConfigurationError):
        path_utils._open_read_descriptor(tmp_path)
    with pytest.raises(FileNotFoundError):
        path_utils._open_read_descriptor(tmp_path / 'missing.bin')


async def test_pe50_cancellation_during_open_closes_the_eventual_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An open finishing after cancellation cannot leak its descriptor."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'body')
    real_open = path_utils._open_read_descriptor
    started = threading.Event()
    release = threading.Event()
    opened: list[int] = []

    def delayed_open(path: Any) -> int:
        """Wait until cancellation, then return a real descriptor."""
        started.set()
        release.wait(timeout=5)
        descriptor = real_open(path)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(path_utils, '_open_read_descriptor', delayed_open)
    reading = asyncio.create_task(path_utils.read_guarded_file(
        source, max_bytes=1024))
    assert await asyncio.to_thread(started.wait, 2)
    reading.cancel()
    await asyncio.sleep(0)
    reading.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await reading
    with pytest.raises(OSError):
        os.fstat(opened[0])


async def test_pe50_cancellation_wins_over_a_late_read_error_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Retrieve a late worker error without leaking or reporting it."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'body')
    started = threading.Event()
    release = threading.Event()
    descriptor: list[int] = []

    def failing_read(opened: int, chunk_size: int) -> bytes:
        """Fail only after the caller has cancelled the read."""
        del chunk_size
        descriptor.append(opened)
        started.set()
        release.wait(timeout=5)
        raise OSError(errno.EIO, 'late read failure')

    monkeypatch.setattr(path_utils.os, 'read', failing_read)
    reading = asyncio.create_task(path_utils.read_guarded_file(
        source, max_bytes=1024))
    assert await asyncio.to_thread(started.wait, 2)
    reading.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await reading
    with pytest.raises(OSError):
        os.fstat(descriptor[0])


@pytest.mark.parametrize('suppress_cancellation', [False, True])
async def test_pe50_descriptor_close_defers_then_obeys_cancel_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suppress_cancellation: bool,
) -> None:
    """Finish cleanup, then either re-raise or consume cancellation."""
    source = tmp_path / 'descriptor.bin'
    descriptor = os.open(source, os.O_WRONLY | os.O_CREAT, FILE_MODE)
    real_close = path_utils.os.close
    started = threading.Event()
    release = threading.Event()

    def delayed_close(candidate: int) -> None:
        """Block only the descriptor under test."""
        if candidate == descriptor:
            started.set()
            release.wait(timeout=5)
        real_close(candidate)

    monkeypatch.setattr(path_utils.os, 'close', delayed_close)
    closing = asyncio.create_task(path_utils._close_descriptor(
        descriptor, suppress_cancellation=suppress_cancellation))
    assert await asyncio.to_thread(started.wait, 2)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    release.set()

    if suppress_cancellation:
        await closing
    else:
        with pytest.raises(asyncio.CancelledError):
            await closing
    with pytest.raises(OSError):
        os.fstat(descriptor)


async def test_pe50_atomic_replace_cancellation_before_dispatch_propagates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Preserve both files when cancelled at the pre-dispatch checkpoint."""
    target = tmp_path / 'target.bin'
    target.write_bytes(b'old')
    entered = asyncio.Event()
    release = asyncio.Event()
    replace_calls: list[None] = []

    async def held_checkpoint(delay: float) -> None:
        """Hold the explicit pre-dispatch checkpoint."""
        assert delay == 0
        entered.set()
        await release.wait()

    def forbidden_replace(*args: Any, **kwargs: Any) -> None:
        """Record a dispatch that must not happen."""
        del args, kwargs
        replace_calls.append(None)

    monkeypatch.setattr(path_utils.asyncio, 'sleep', held_checkpoint)
    monkeypatch.setattr(path_utils.os, 'replace', forbidden_replace)

    async def chunks() -> Any:
        """Yield the complete pre-commit replacement."""
        yield b'new'

    replacing = asyncio.create_task(path_utils.stream_to_path(
        target, chunks(), overwrite=True, max_bytes=3))
    await entered.wait()
    replacing.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await replacing
    assert replace_calls == []
    assert target.read_bytes() == b'old'
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('overwrite', [False, True])
@pytest.mark.parametrize('outcome', ['cancel', 'error'])
async def test_pe50_file_close_releases_descriptor_ownership_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overwrite: bool,
    outcome: str,
) -> None:
    """A completed close is never repeated after cancellation or failure."""
    target = tmp_path / 'target.bin'
    if overwrite:
        target.write_bytes(b'old')
    started = asyncio.Event()
    release = asyncio.Event()
    real_close = path_utils._close_descriptor
    file_descriptor: list[int] = []
    close_calls: list[int] = []

    async def controlled_close(
        descriptor: int,
        *,
        suppress_cancellation: bool = False,
    ) -> None:
        """Close the first writer fd, then expose its post-release result."""
        if not file_descriptor:
            file_descriptor.append(descriptor)
            close_calls.append(descriptor)
            started.set()
            cancellation: Optional[asyncio.CancelledError] = None
            try:
                await release.wait()
            except asyncio.CancelledError as exc:
                cancellation = exc
                while not release.is_set():
                    try:
                        await asyncio.shield(release.wait())
                    except asyncio.CancelledError:
                        continue
            await real_close(descriptor, suppress_cancellation=True)
            if cancellation is not None:
                raise cancellation
            raise OSError(errno.EIO, 'close failed after descriptor release')

        close_calls.append(descriptor)
        if descriptor == file_descriptor[0]:
            return
        await real_close(
            descriptor,
            suppress_cancellation=suppress_cancellation,
        )

    monkeypatch.setattr(path_utils, '_close_descriptor', controlled_close)

    async def chunks() -> Any:
        """Yield one complete file before its coordinated close."""
        yield b'new'

    streaming = asyncio.create_task(path_utils.stream_to_path(
        target, chunks(), overwrite=overwrite, max_bytes=3))
    await asyncio.wait_for(started.wait(), timeout=2)
    if outcome == 'cancel':
        streaming.cancel()
        await asyncio.sleep(0)
    release.set()

    if outcome == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await streaming
    else:
        with pytest.raises(LocalWriteError):
            await streaming

    assert close_calls.count(file_descriptor[0]) == 1
    if overwrite:
        assert target.read_bytes() == b'old'
        assert list(tmp_path.iterdir()) == [target]
    else:
        assert not target.exists()


async def test_pe50_stream_explicitly_finalizes_chunks_before_cleanup(
    tmp_path: Path,
) -> None:
    """A consumer-side refusal closes its iterable before target cleanup."""
    target = tmp_path / 'target.bin'
    target.write_bytes(b'old')
    finalized = asyncio.Event()

    async def chunks() -> Any:
        """Record explicit finalization after yielding an oversized chunk."""
        try:
            yield b'too large'
        finally:
            finalized.set()

    with pytest.raises(ResponseTooLargeError):
        await path_utils.stream_to_path(
            target,
            chunks(),
            overwrite=True,
            max_bytes=1,
        )

    assert finalized.is_set()
    assert target.read_bytes() == b'old'
    assert list(tmp_path.iterdir()) == [target]


async def test_pe50_stream_accepts_an_iterator_without_async_close(
    tmp_path: Path,
) -> None:
    """Async iteration does not require the optional ``aclose`` protocol."""
    class PlainIterator:
        """Yield one chunk without exposing ``aclose``."""

        def __init__(self) -> None:
            """Initialize the one-shot iterator."""
            self.sent = False

        def __aiter__(self) -> 'PlainIterator':
            """Return this iterator."""
            return self

        async def __anext__(self) -> bytes:
            """Yield one byte, then stop."""
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return b'x'

    target = tmp_path / 'plain.bin'
    written = await path_utils.stream_to_path(
        target, PlainIterator(), overwrite=False, max_bytes=1)

    assert written == 1
    assert target.read_bytes() == b'x'


async def test_pe50_stream_types_a_nonawaitable_iterator_close(
    tmp_path: Path,
) -> None:
    """A malformed optional close operation cannot leak a raw TypeError."""
    class InvalidCloseIterator:
        """Stop immediately and return a non-awaitable from ``aclose``."""

        def __aiter__(self) -> 'InvalidCloseIterator':
            """Return this iterator."""
            return self

        async def __anext__(self) -> bytes:
            """Stop without yielding bytes."""
            raise StopAsyncIteration

        def aclose(self) -> None:
            """Violate the optional async-close protocol."""
            return None

    target = tmp_path / 'invalid-close.bin'
    with pytest.raises(SerializationError):
        await path_utils.stream_to_path(
            target,
            InvalidCloseIterator(),
            overwrite=False,
            max_bytes=1,
        )

    assert not target.exists()


@pytest.mark.parametrize('close_fails', [False, True])
async def test_pe50_stream_close_completes_before_cancellation_propagates(
    tmp_path: Path,
    close_fails: bool,
) -> None:
    """Repeated cancellation waits for close and wins over a late failure."""
    started = asyncio.Event()
    release = asyncio.Event()

    class ControlledIterator:
        """Stop immediately, then expose a coordinated async close."""

        def __aiter__(self) -> 'ControlledIterator':
            """Return this iterator."""
            return self

        async def __anext__(self) -> bytes:
            """Stop without yielding bytes."""
            raise StopAsyncIteration

        async def aclose(self) -> None:
            """Wait to finish, then optionally report a late close error."""
            started.set()
            await release.wait()
            if close_fails:
                raise RuntimeError('late close failure')

    target = tmp_path / 'cancel-close.bin'
    streaming = asyncio.create_task(path_utils.stream_to_path(
        target,
        ControlledIterator(),
        overwrite=False,
        max_bytes=1,
    ))
    await asyncio.wait_for(started.wait(), timeout=2)
    streaming.cancel()
    await asyncio.sleep(0)
    streaming.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await streaming
    assert not target.exists()


async def test_pe50_iterator_close_self_cancellation_propagates_exactly(
    tmp_path: Path,
) -> None:
    """A completed self-cancelled finalizer cannot spin the shield loop."""
    marker = asyncio.CancelledError('iterator-close-self-cancelled')

    class SelfCancellingIterator:
        """Stop immediately and cancel from inside ``aclose``."""

        def __aiter__(self) -> 'SelfCancellingIterator':
            """Return this iterator."""
            return self

        async def __anext__(self) -> bytes:
            """Stop without yielding bytes."""
            raise StopAsyncIteration

        async def aclose(self) -> None:
            """Raise the exact child-side cancellation marker."""
            raise marker

    target = tmp_path / 'self-cancelled-close.bin'
    with pytest.raises(asyncio.CancelledError) as caught:
        await path_utils.stream_to_path(
            target,
            SelfCancellingIterator(),
            overwrite=False,
            max_bytes=1,
        )

    assert_cancelled_error_survives_task_boundary(
        caught.value,
        ('iterator-close-self-cancelled',),
        original=marker,
    )
    assert not target.exists()


@pytest.mark.parametrize('consumer_fails', [False, True])
async def test_pe50_stream_close_error_obeys_existing_failure_precedence(
    tmp_path: Path,
    consumer_fails: bool,
) -> None:
    """A close error propagates normally but cannot hide a body refusal."""
    class FailingCloseIterator:
        """Yield once and fail whenever explicitly closed."""

        def __init__(self) -> None:
            """Initialize the one-shot iterator."""
            self.sent = False

        def __aiter__(self) -> 'FailingCloseIterator':
            """Return this iterator."""
            return self

        async def __anext__(self) -> bytes:
            """Yield two bytes, then stop."""
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return b'xx'

        async def aclose(self) -> None:
            """Raise after the iterator has released its resources."""
            raise RuntimeError('close failure after release')

    target = tmp_path / 'close-error.bin'
    expected = ResponseTooLargeError if consumer_fails else RuntimeError
    with pytest.raises(expected):
        await path_utils.stream_to_path(
            target,
            FailingCloseIterator(),
            overwrite=False,
            max_bytes=1 if consumer_fails else 2,
        )

    assert not target.exists()


async def test_pe50_late_open_error_still_reports_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cancelled open retrieves and hides its later worker exception."""
    source = tmp_path / 'upload.bin'
    source.write_bytes(b'body')
    started = threading.Event()
    release = threading.Event()

    def delayed_failure(path: Any) -> int:
        """Fail only after cancellation reaches the awaiting task."""
        del path
        started.set()
        release.wait(timeout=5)
        raise OSError(errno.EIO, 'late open failure')

    monkeypatch.setattr(path_utils, '_open_read_descriptor', delayed_failure)
    reading = asyncio.create_task(path_utils.read_guarded_file(
        source, max_bytes=1024))
    assert await asyncio.to_thread(started.wait, 2)
    reading.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await reading


async def test_pe50_thread_worker_error_propagates_without_cancellation(
) -> None:
    """The cancellation guard does not relabel an ordinary worker failure."""
    def failure() -> None:
        """Raise the marker error in the worker."""
        raise OSError(errno.EIO, 'worker failed')

    with pytest.raises(OSError, match='worker failed'):
        await path_utils._run_thread_call(failure)


async def test_pe50_thread_worker_self_cancellation_propagates_exactly(
) -> None:
    """A child-side ``CancelledError`` cannot become a shield busy loop."""
    marker = asyncio.CancelledError('thread-worker-self-cancelled')

    def self_cancel() -> None:
        """Raise the exact marker from inside the worker child."""
        raise marker

    with pytest.raises(asyncio.CancelledError) as caught:
        await path_utils._run_thread_call(self_cancel)

    assert_cancelled_error_survives_task_boundary(
        caught.value,
        ('thread-worker-self-cancelled',),
        original=marker,
    )


async def test_pe50_completed_child_does_not_hide_caller_cancellation(
) -> None:
    """A success/caller-cancel race still reports the caller's marker."""
    marker = 'caller-cancelled-after-child-success'

    async def race() -> None:
        """Complete the child, then cancel this waiter in one callback."""
        loop = asyncio.get_running_loop()
        operation: asyncio.Future[None] = loop.create_future()
        waiter = asyncio.current_task()
        assert waiter is not None

        def complete_then_cancel() -> None:
            """Make the child done before delivering caller cancellation."""
            operation.set_result(None)
            waiter.cancel(marker)

        loop.call_soon(complete_then_cancel)
        await path_utils._await_shielded_operation(operation)

    waiting = asyncio.create_task(race())
    with pytest.raises(asyncio.CancelledError) as caught:
        await waiting

    assert_cancelled_error_survives_task_boundary(
        caught.value,
        (marker,),
    )


def test_pe50_private_path_splitters_refuse_a_directory_root() -> None:
    """Defensive private seams reject a value with no file component."""
    with pytest.raises(PathContainmentError):
        path_utils._leaf_name('/')
    with pytest.raises(PathContainmentError):
        path_utils._open_parent_descriptor('/')


def test_pe50_temporary_creation_retries_collision_and_uses_mode_0600(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A guessed temp name is never opened without exclusive creation."""
    parent = os.open(tmp_path, os.O_RDONLY)
    collision = tmp_path / '.asyncio-gateway-collision.tmp'
    collision.write_bytes(b'occupied')
    tokens = iter(['collision', 'unique'])
    monkeypatch.setattr(
        path_utils.secrets, 'token_hex', lambda size: next(tokens))
    try:
        descriptor, name = path_utils._temporary_file_for(parent)
        os.close(descriptor)
        created = tmp_path / name
        assert created.name == '.asyncio-gateway-unique.tmp'
        assert stat.S_IMODE(created.stat().st_mode) == FILE_MODE
    finally:
        os.close(parent)


def test_pe50_temporary_creation_cleans_a_mode_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed 0600 restriction closes and removes only the new temp."""
    parent = os.open(tmp_path, os.O_RDONLY)

    def fail_mode(descriptor: int, mode: int) -> None:
        """Refuse the post-open mode restriction."""
        del descriptor, mode
        raise OSError(errno.EPERM, 'mode refused')

    monkeypatch.setattr(path_utils.os, 'fchmod', fail_mode)
    try:
        with pytest.raises(OSError, match='mode refused'):
            path_utils._temporary_file_for(parent)
        assert list(tmp_path.iterdir()) == []
    finally:
        os.close(parent)


def test_pe50_temporary_creation_refuses_exhausted_unique_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Repeated collisions fail closed without overwriting the occupant."""
    parent = os.open(tmp_path, os.O_RDONLY)
    occupied = tmp_path / '.asyncio-gateway-same.tmp'
    occupied.write_bytes(b'occupied')
    monkeypatch.setattr(
        path_utils.secrets, 'token_hex', lambda size: 'same')
    try:
        with pytest.raises(ConfigurationError):
            path_utils._temporary_file_for(parent)
        assert occupied.read_bytes() == b'occupied'
    finally:
        os.close(parent)


def test_pe50_exclusive_creation_cleans_a_mode_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exclusive-create cleanup removes the leaf when fchmod fails."""
    parent = os.open(tmp_path, os.O_RDONLY)

    def fail_mode(descriptor: int, mode: int) -> None:
        """Refuse the post-open mode restriction."""
        del descriptor, mode
        raise OSError(errno.EPERM, 'mode refused')

    monkeypatch.setattr(path_utils.os, 'fchmod', fail_mode)
    try:
        with pytest.raises(OSError, match='mode refused'):
            path_utils._exclusive_file_for(parent, 'target.bin')
        path_utils._unlink_at(parent, 'already-absent.bin')
        assert list(tmp_path.iterdir()) == []
    finally:
        os.close(parent)


def test_pe50_write_all_handles_short_write_and_refuses_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short write continues; a zero-byte write cannot silently truncate."""
    answers = iter([2, 3])
    seen: list[bytes] = []

    def short_write(descriptor: int, value: Any) -> int:
        """Write the supplied number of bytes from each remaining view."""
        del descriptor
        seen.append(bytes(value))
        return next(answers)

    monkeypatch.setattr(path_utils.os, 'write', short_write)
    path_utils._write_all(99, b'abcde')
    assert seen == [b'abcde', b'cde']

    monkeypatch.setattr(path_utils.os, 'write', lambda descriptor, value: 0)
    with pytest.raises(OSError, match='no progress'):
        path_utils._write_all(99, b'x')


@pytest.mark.parametrize('overwrite', [False, True])
@pytest.mark.parametrize('late_failure', [False, True])
async def test_pe50_cancellation_during_create_cleans_eventual_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overwrite: bool,
    late_failure: bool,
) -> None:
    """A create worker finishing after cancellation leaves no owned leaf."""
    target = tmp_path / 'target.bin'
    if overwrite:
        target.write_bytes(b'old')
        real_create = path_utils._temporary_file_for
        attribute = '_temporary_file_for'
    else:
        real_create = path_utils._exclusive_file_for
        attribute = '_exclusive_file_for'
    started = threading.Event()
    release = threading.Event()

    def delayed_create(*args: Any) -> Any:
        """Return or fail only after the caller cancels."""
        started.set()
        release.wait(timeout=5)
        if late_failure:
            raise OSError(errno.EIO, 'late create failure')
        return real_create(*args)

    monkeypatch.setattr(path_utils, attribute, delayed_create)

    async def chunks() -> Any:
        """Yield bytes only if create unexpectedly reaches iteration."""
        yield b'new'

    streaming = asyncio.create_task(path_utils.stream_to_path(
        target, chunks(), overwrite=overwrite, max_bytes=1024))
    assert await asyncio.to_thread(started.wait, 2)
    streaming.cancel()
    await asyncio.sleep(0)
    streaming.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await streaming
    if overwrite:
        assert target.read_bytes() == b'old'
        assert list(tmp_path.iterdir()) == [target]
    else:
        assert list(tmp_path.iterdir()) == []


def test_pe50_atomic_replace_is_not_an_extra_public_path_seam() -> None:
    """Keep atomic replacement private to the shared streamed writer."""
    assert not hasattr(path_utils, 'atomic_replace')


async def test_pe50_exclusive_empty_stream_creates_a_private_empty_file(
    tmp_path: Path,
) -> None:
    """Zero bytes is a valid bounded response and exercises success cleanup."""
    target = tmp_path / 'empty.bin'

    async def chunks() -> Any:
        """Yield an empty bytes-like chunk."""
        yield b''

    written = await path_utils.stream_to_path(
        target, chunks(), overwrite=False, max_bytes=1)

    assert written == 0
    assert target.read_bytes() == b''
    assert stat.S_IMODE(target.stat().st_mode) == FILE_MODE


async def test_pe50_stream_rejects_non_bytes_chunk_directly(
    tmp_path: Path,
) -> None:
    """The path seam itself owns runtime bytes-like validation."""
    async def chunks() -> Any:
        """Yield one malformed provider value."""
        yield 'not bytes'

    target = tmp_path / 'bad.bin'
    with pytest.raises(SerializationError):
        await path_utils.stream_to_path(
            target, chunks(), overwrite=False, max_bytes=1024)
    assert not target.exists()


async def test_pe50_atomic_replace_cancellation_during_dispatch_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Once submitted, cancellation waits for and reports the real commit."""
    target = tmp_path / 'target.bin'
    target.write_bytes(b'old')
    started = threading.Event()
    release = threading.Event()
    real_replace = path_utils._replace_at

    def replace_after_release(*args: Any) -> None:
        """Pause before the actual linearized filesystem operation."""
        started.set()
        release.wait(timeout=5)
        real_replace(*args)

    monkeypatch.setattr(path_utils, '_replace_at', replace_after_release)

    async def chunks() -> Any:
        """Yield the replacement through the production writer."""
        yield b'new'

    replacing = asyncio.create_task(path_utils.stream_to_path(
        target, chunks(), overwrite=True, max_bytes=3))
    assert await asyncio.to_thread(started.wait, 2)
    replacing.cancel()
    replacing.cancel()
    release.set()

    assert await replacing == 3
    assert target.read_bytes() == b'new'
    assert list(tmp_path.iterdir()) == [target]


async def test_pe50_atomic_replace_cancellation_after_commit_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Report success when cancellation arrives after os.replace ran."""
    target = tmp_path / 'target.bin'
    target.write_bytes(b'old')
    committed = threading.Event()
    release = threading.Event()
    real_replace = path_utils._replace_at

    def replace_then_wait(*args: Any) -> None:
        """Commit first, but hold the worker future incomplete."""
        real_replace(*args)
        committed.set()
        release.wait(timeout=5)

    monkeypatch.setattr(path_utils, '_replace_at', replace_then_wait)

    async def chunks() -> Any:
        """Yield the replacement through the production writer."""
        yield b'new'

    replacing = asyncio.create_task(path_utils.stream_to_path(
        target, chunks(), overwrite=True, max_bytes=3))
    assert await asyncio.to_thread(committed.wait, 2)
    replacing.cancel()
    release.set()

    assert await replacing == 3
    assert target.read_bytes() == b'new'
    assert list(tmp_path.iterdir()) == [target]


async def test_pe50_overwrite_temp_is_same_directory_private_and_atomic(
    tmp_path: Path,
) -> None:
    """The held-parent temp is 0600 beside the target until replacement."""
    target = tmp_path / 'target.bin'
    target.write_bytes(b'old')
    observed_temp: list[Path] = []

    async def chunks() -> Any:
        """Inspect the temporary while the stream owns it."""
        candidates = [
            entry for entry in tmp_path.iterdir()
            if entry != target
        ]
        observed_temp.extend(candidates)
        assert len(candidates) == 1
        assert stat.S_IMODE(candidates[0].stat().st_mode) == FILE_MODE
        yield bytearray(b'new')
        yield memoryview(b' body')

    written = await path_utils.stream_to_path(
        target, chunks(), overwrite=True, max_bytes=None)

    assert written == 8
    assert observed_temp[0].parent == target.parent
    assert target.read_bytes() == b'new body'
    assert stat.S_IMODE(target.stat().st_mode) == FILE_MODE
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('overwrite', [False, True])
async def test_pe50_cancelled_stream_cleans_only_its_created_file(
    tmp_path: Path,
    overwrite: bool,
) -> None:
    """Cancellation before overwrite dispatch removes partial/new bytes."""
    target = tmp_path / 'target.bin'
    if overwrite:
        target.write_bytes(b'old')
    started = asyncio.Event()
    release = asyncio.Event()

    async def chunks() -> Any:
        """Yield partial bytes, then wait to be cancelled."""
        yield b'partial'
        started.set()
        await release.wait()

    streaming = asyncio.create_task(path_utils.stream_to_path(
        target, chunks(), overwrite=overwrite, max_bytes=1024))
    await started.wait()
    streaming.cancel()

    with pytest.raises(asyncio.CancelledError):
        await streaming
    if overwrite:
        assert target.read_bytes() == b'old'
        assert list(tmp_path.iterdir()) == [target]
    else:
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []


async def test_pe50_replace_failure_preserves_old_target_and_removes_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A dispatched replace failure cannot damage the pre-existing target."""
    target = tmp_path / 'target.bin'
    target.write_bytes(b'old')

    def failing_replace(*args: Any) -> None:
        """Refuse the atomic commit."""
        del args
        raise OSError(errno.EIO, 'replace failed')

    monkeypatch.setattr(path_utils, '_replace_at', failing_replace)

    async def chunks() -> Any:
        """Yield a complete candidate replacement."""
        yield b'new'

    with pytest.raises(LocalWriteError):
        await path_utils.stream_to_path(
            target, chunks(), overwrite=True, max_bytes=1024)

    assert target.read_bytes() == b'old'
    assert list(tmp_path.iterdir()) == [target]


async def test_pe50_exclusive_stream_refuses_existing_without_iteration(
    tmp_path: Path,
) -> None:
    """Non-overwrite remains exclusive and never consumes a refused body."""
    target = tmp_path / 'target.bin'
    target.write_bytes(b'old')
    iterated = False

    async def chunks() -> Any:
        """Record any accidental body consumption."""
        nonlocal iterated
        iterated = True
        yield b'new'

    with pytest.raises(ConfigurationError):
        await path_utils.stream_to_path(
            target, chunks(), overwrite=False, max_bytes=1024)

    assert iterated is False
    assert target.read_bytes() == b'old'


async def test_pe50_stream_validates_its_own_overwrite_and_limit_options(
    tmp_path: Path,
) -> None:
    """The stable path seam rejects malformed policy even without S3."""
    async def chunks() -> Any:
        """Supply no bytes."""
        if False:
            yield b''

    with pytest.raises(ConfigurationError):
        await path_utils.stream_to_path(
            tmp_path / 'bad-overwrite',
            chunks(),
            # `type: ignore[arg-type]` -- exercise invalid-input rejection.
            overwrite='yes',  # type: ignore[arg-type]
            max_bytes=1,
        )
    with pytest.raises(ConfigurationError):
        await path_utils.stream_to_path(
            tmp_path / 'bad-limit',
            chunks(),
            overwrite=False,
            max_bytes=0,
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


async def test_a_missing_parent_directory_is_a_typed_local_write_failure(
    tmp_path: Path,
) -> None:
    """Not every refused open is a security finding -- but all are typed.

    This row used to assert the opposite half of the same idea: that a
    missing parent escaped ``safe_writer`` as a bare
    ``FileNotFoundError``, on the reasoning that reporting it as a
    path-containment attack would be wrong and alarming. The reasoning
    still holds and the conclusion did not: it *is* not a containment
    finding, and it is also not something a library whose whole contract
    is a typed envelope may hand back raw. On HTTP it matched no
    transport family and reached ``request()`` as the interpreter's own
    exception (NEW-R10-1).

    So the distinction is kept in the *class* and dropped from the
    escape: ``LocalWriteError``, a sibling of ``PathContainmentError``
    rather than a subclass, sharing its ``PATH`` code because a caller
    acts on both the same way, while a security finding stays
    distinguishable by type.
    """
    with pytest.raises(LocalWriteError) as raised:
        async with safe_writer(tmp_path / 'no-such-dir' / 'f.bin'):
            pass

    assert raised.value.code == 'PATH'
    assert isinstance(raised.value.__cause__, FileNotFoundError)
    assert not isinstance(raised.value, PathContainmentError), (
        'a missing directory is an operational failure, not a '
        'containment finding; sharing the code must not blur the class.')


async def test_a_write_that_fails_mid_body_is_typed_and_leaves_no_file(
    tmp_path: Path,
) -> None:
    """The *write* seam, which the open seam's guard cannot reach.

    A missing parent fails at ``open``. A full disk does not -- ENOSPC,
    EDQUOT and EFBIG are raised by ``write`` on a file that opened
    perfectly, so a guard placed only at the open lets exactly the
    fault an operator most fears escape raw. Measured through
    ``request()``: a 200 KB download under an 8 KiB ``RLIMIT_FSIZE``
    reached the caller as a bare ``OSError``.

    The partial file must also be gone: what a failed write leaves is a
    truncated body, and M19's rule is that this library never orphans
    one.
    """
    target = tmp_path / 'partial.bin'

    with pytest.raises(LocalWriteError) as raised:
        async with safe_writer(target) as handle:
            await handle.write(b'a chunk that lands')
            raise OSError(errno.ENOSPC, 'No space left on device')

    assert raised.value.code == 'PATH'
    assert 'No space left on device' in str(raised.value)
    assert not target.exists(), (
        'a failed write must not orphan the partial file it wrote')


async def test_a_close_that_fails_flushing_is_typed_and_leaves_no_file(
    tmp_path: Path,
) -> None:
    """The seam a write-side check alone cannot reach.

    ``aiofiles`` buffers, so a body small enough to fit the buffer never
    reaches the ``write`` syscall at all: its first and only syscall is
    the flush inside ``close()``, which runs on the **success** path
    after the block has already exited cleanly. Measured end to end -- a
    200 KB download under an 8 KiB ``RLIMIT_FSIZE`` reached
    ``request()`` as a raw ``OSError`` from exactly here while every
    other arm was green (NEW-R10-1).

    The half-written file must be removed for the same reason a failed
    write's is (M19): what a failed flush leaves is a truncated body,
    and this library never orphans one.
    """
    target = tmp_path / 'flushed.bin'

    async def refuse() -> None:
        """Fail the flush the way an exhausted filesystem does.

        Returns:
            Never; this always raises.

        Raises:
            OSError: Always, with ``ENOSPC``. Patched onto the handle
                rather than onto the builtin writer, whose ``close`` is
                an immutable attribute of a C type.
        """
        raise OSError(errno.ENOSPC, 'No space left on device')

    opened = aiofiles.open

    async def refusing_open(*args: Any, **kwargs: Any) -> Any:
        """Open normally, then make only the flush fail.

        Args:
            args: Forwarded to ``aiofiles.open``.
            kwargs: Forwarded to ``aiofiles.open``.

        Returns:
            The real handle, with ``close`` replaced.
        """
        handle = await opened(*args, **kwargs)
        handle.close = refuse
        return handle

    with mock.patch.object(aiofiles, 'open', refusing_open), \
            pytest.raises(LocalWriteError) as raised:
        async with safe_writer(target) as writing:
            await writing.write(b'small enough to stay in the buffer')

    assert raised.value.code == 'PATH'
    assert 'No space left on device' in str(raised.value)
    assert not target.exists(), (
        'a failed flush must not orphan the truncated file it left')


async def test_a_failure_inside_the_block_keeps_its_own_diagnosis(
    tmp_path: Path,
) -> None:
    """A non-``OSError`` failure is not reclassified on its way out.

    ``safe_writer`` types local *filesystem* refusals. A body that fails
    for its own reason -- a cancelled transfer, a decode error, a bug --
    is the caller's exception and must arrive as itself, or the one
    conversion point would be reporting a library bug as a disk problem.
    """
    target = tmp_path / 'aborted.bin'

    with pytest.raises(ZeroDivisionError):
        async with safe_writer(target) as handle:
            await handle.write(b'partial')
            raise ZeroDivisionError('the block failed for its own reason')

    assert not target.exists()


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

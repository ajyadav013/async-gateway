"""Cross-version assertions for cancellation crossing task boundaries.

CPython 3.10 reconstructs :class:`asyncio.CancelledError` when a cancelled
task's result is retrieved.  The reconstructed exception has empty arguments
and retains the original cancellation in its context chain.  CPython 3.11+
returns the original exception object.  Tests use this helper so they keep
proving the gateway's cancellation provenance without asserting a guarantee
the 3.10 task implementation cannot provide.
"""

import asyncio
import sys
from typing import Optional


def assert_cancelled_error_survives_task_boundary(
    caught: asyncio.CancelledError,
    expected_args: tuple[object, ...],
    *,
    original: Optional[asyncio.CancelledError] = None,
) -> None:
    """Assert cancellation provenance using each interpreter's task contract.

    Args:
        caught: Cancellation observed by the task's awaiting caller.
        expected_args: Arguments attached where cancellation first occurred.
        original: Exact cancellation captured before the task boundary, when
            the test has access to it.

    Returns:
        None after the provenance assertion passes.
    """
    if sys.version_info >= (3, 11):
        if original is not None:
            assert caught is original
        assert caught.args == expected_args
        return

    current: Optional[BaseException] = caught
    seen: set[int] = set()
    found_args = False
    found_original = original is None
    while isinstance(current, asyncio.CancelledError):
        marker = id(current)
        if marker in seen:
            break
        seen.add(marker)
        found_args = found_args or current.args == expected_args
        found_original = found_original or current is original
        current = current.__context__

    assert found_args
    assert found_original

"""Wall-clock timestamps in UTC, and durations from a monotonic clock.

Two clocks, deliberately. ``request_time`` is a wall-clock instant a human
reads and correlates with other systems' logs, so it is UTC ISO-8601 with an
explicit offset -- unambiguous everywhere the library is imported, with no
hardcoded regional timezone. ``latency`` is a duration, so it is measured
with ``time.monotonic``, which an NTP step cannot drag backwards.

Because the library resolves no IANA timezone name, it needs no third-party
timezone library and no ``zoneinfo``, and therefore no ``tzdata`` package on
Windows or on a slim Linux image with no system timezone database.

A monotonic clock is process-local and has no relationship to wall time:
``latency`` is a duration, never a timestamp difference a caller can
correlate with ``request_time``.
"""

import time
from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Return the current instant as a UTC ISO-8601 string.

    Returns:
        For example ``'2026-08-16T09:41:07.123456+00:00'`` -- always with an
        explicit ``+00:00`` offset, so the value is never ambiguous about
        which zone it is in.
    """
    return datetime.now(timezone.utc).isoformat()


def monotonic_now() -> float:
    """Return a monotonic reference point for measuring a duration.

    Returns:
        Seconds from an undefined origin. Only differences are meaningful.
    """
    return time.monotonic()


def elapsed_since(started: float) -> float:
    """Return the seconds elapsed since ``started``, never negative.

    Args:
        started: A reference point from :func:`monotonic_now`.

    Returns:
        The elapsed duration in seconds. A monotonic clock cannot run
        backwards, so the clamp is defence in depth: it makes ``latency >= 0``
        unconditional even for a caller who substituted the clock.
    """
    return max(0.0, time.monotonic() - started)

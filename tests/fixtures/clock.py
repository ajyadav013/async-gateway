"""The controlled clock and recording sleep every timing test drives.

The circuit breaker's behaviour is defined in seconds -- an open circuit
half-opens after ``reset_timeout_seconds``, retries are spaced by a
backoff -- and a test that waited those seconds out would be slow where
it is not flaky. Both doubles here exist so timing can be *asserted*
instead of *endured*: :class:`FakeClock` advances only when a test says
so, and :class:`RecordingSleep` records what it was asked to wait for and
returns immediately.

They are substituted through the facade's own ``clock``/``sleep``
parameters, never by monkeypatching ``time.monotonic`` or
``asyncio.sleep``. That is the point of the seam existing
(``CircuitBreakerConfig`` §seam, R24): a module-global patch would prove
nothing about the interface a consumer actually has, and it would leak
into every other coroutine on the loop.
"""

from collections.abc import Iterator

import pytest

from async_gateway.helpers.internal.breaker_registry import reset


class FakeClock:
    """A monotonic clock that only moves when a test moves it.

    Satisfies the facade's ``clock`` parameter: called with no arguments,
    returns seconds as a float.

    Usage:
        clock = FakeClock()
        breaker = CircuitBreakerHelper(clock=clock, ...)
        clock.advance(61)      # the reset timeout has now elapsed

    Attributes:
        now: The current reading, in seconds.
    """

    def __init__(self, now: float = 0.0) -> None:
        """Start the clock at ``now``.

        Args:
            now: The initial reading. The default of 0 is fine because
                the facade only ever subtracts two readings.
        """
        self.now = now

    def __call__(self) -> float:
        """Return the current reading.

        Returns:
            The clock's value in seconds. Reading does not advance it --
            a clock that ticked when observed could not express "no time
            passed between these two events".
        """
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: How far forward to move. Never negative: the
                contract the facade relies on is monotonicity.

        Returns:
            None.

        Raises:
            ValueError: If ``seconds`` is negative, which would let a
                test assert behaviour the real clock cannot produce.
        """
        if seconds < 0:
            raise ValueError('a monotonic clock cannot go backwards')
        self.now += seconds


class RecordingSleep:
    """An ``asyncio.sleep`` substitute that records and returns at once.

    Satisfies the facade's ``sleep`` parameter. What a retry loop waited
    *for* is the assertable fact -- that three retries were spaced, that
    two sequences were not synchronised -- and the waiting itself is pure
    cost.

    Usage:
        sleep = RecordingSleep()
        breaker = CircuitBreakerHelper(sleep=sleep, ...)
        assert sleep.waits == [0.1, 0.2, 0.4]

    Attributes:
        waits: Every duration requested, in the order requested.
    """

    def __init__(self) -> None:
        """Start with an empty record."""
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Record a requested wait and return immediately.

        Args:
            seconds: The duration the caller asked to wait for.

        Returns:
            None.
        """
        self.waits.append(seconds)


@pytest.fixture(autouse=True)
def clean_breaker_registry() -> Iterator[None]:
    """Empty the breaker registry around every test in the suite.

    Autouse, and for the whole suite rather than this module, because
    the registry is deliberately process-lifetime state: without this,
    one test's accumulated failures decide whether an unrelated later
    test's first request is even dispatched, and which tests fail
    depends on the order ``pytest-randomly`` happens to pick. Three
    already did.

    Cleared on the way *out* as well as in, so a test that opened a
    circuit leaves nothing behind for a differently-ordered run, and a
    :class:`FakeClock` never outlives the test that injected it.

    Returns:
        None -- yields once, around the test.
    """
    reset()
    yield
    reset()


@pytest.fixture()
def fake_clock() -> FakeClock:
    """Return a clock starting at zero.

    Returns:
        A fresh :class:`FakeClock` per test, so one test's advances
        cannot reach another's.
    """
    return FakeClock()


@pytest.fixture()
def recording_sleep() -> RecordingSleep:
    """Return a sleep recorder with an empty log.

    Returns:
        A fresh :class:`RecordingSleep` per test.
    """
    return RecordingSleep()

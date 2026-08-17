"""The breaker facade, the per-destination registry and the clock seam.

Three things are pinned here, and they are one story because they are one
defect. The breaker was constructed per request and could never open
(H8); the obvious fix -- one module-global breaker -- would have made a
single flaky host open the circuit for every destination in the process,
which is a worse outage than the one it fixes (M16, FI-4); and neither
half is testable without a clock a test can move, because ``pyfailsafe``
hardcodes its own (Ruling D, R7 behaviour 7).

R7's **seven fitness behaviours** live here, and behaviour 7 is asserted
against *this library's facade* rather than against ``pyfailsafe``. That
is deliberate: ``pyfailsafe`` provably has no clock or sleep parameter
(``circuit_breaker.py:139,142``, ``failsafe.py:103``), so a behaviour-7
test written against the dependency would be a test of a known-false
proposition.

Not one test here sleeps or reads a real clock. Timing is driven through
the facade's own ``clock``/``sleep`` parameters -- never a monkeypatched
``time.monotonic`` or ``asyncio.sleep``, which would prove nothing about
the interface a consumer actually has and would leak into every other
coroutine on the loop.
"""

import asyncio
import time
from typing import Any, Optional

from failsafe import CircuitOpen, RetriesExhausted

import pytest

from async_gateway.helpers.internal.base import destination_of
from async_gateway.helpers.internal.breaker_registry import (
    get_breaker, registry_size, reset)
from async_gateway.helpers.internal.circuit_breaker_helper import (
    BACKOFF_NAMES, BreakerState, CircuitBreakerHelper,
    DEFAULT_ABORTABLE_EXCEPTIONS, RETRIABLE_FAILURES, get_retry_policy,
    validated_breaker_config)
from async_gateway.utils.constants import (BREAKER_REGISTRY_MAX,
                                           CIRCUIT_BREAKER_DELAY,
                                           CIRCUIT_BREAKER_JITTER,
                                           CIRCUIT_BREAKER_MAX_DELAY,
                                           UNKNOWN_PORT)
from async_gateway.utils.exceptions import (ConfigurationError,
                                            ResponseTooLargeError)

from tests.fixtures.clock import FakeClock, RecordingSleep


class Boom(OSError):
    """A retriable transport failure, in the shape the loop counts."""


def breaker(
    *,
    clock: Optional[FakeClock] = None,
    sleep: Optional[RecordingSleep] = None,
    maximum_failures: int = 2,
    timeout: float = 60.0,
    retry_config: Optional[dict[str, Any]] = None,
) -> CircuitBreakerHelper:
    """Build one breaker directly, on injected timing.

    Bypasses the registry so a state-machine assertion is not also an
    assertion about caching. The registry has its own section below.

    Args:
        clock: The clock to read, or None for a fresh one at zero.
        sleep: The wait recorder, or None for a fresh one.
        maximum_failures: Failures that open the circuit.
        timeout: Seconds the circuit stays open.
        retry_config: The caller's retry configuration, or None for no
            retries.

    Returns:
        A breaker whose every duration is under the test's control.
    """
    config: dict[str, Any] = {
        'maximum_failures': maximum_failures,
        'timeout': timeout,
        'clock': clock or FakeClock(),
        'sleep': sleep or RecordingSleep(),
    }
    if retry_config is not None:
        config['retry_config'] = retry_config
    return CircuitBreakerHelper(**validated_breaker_config(config))


async def succeed() -> str:
    """Return a sentinel, having done nothing.

    Returns:
        The string ``'ok'``.
    """
    return 'ok'


def failing(error: BaseException) -> Any:
    """Return a coroutine function that always raises ``error``.

    Args:
        error: What every call raises.

    Returns:
        A zero-argument coroutine function.
    """
    async def call() -> None:
        """Raise the configured error.

        Raises:
            BaseException: Always -- ``error``, as supplied.
        """
        raise error
    return call


def counting(error: BaseException, succeed_after: int) -> Any:
    """Return a call that fails ``succeed_after`` times, then succeeds.

    Args:
        error: What each failing call raises.
        succeed_after: How many calls fail before one succeeds.

    Returns:
        A coroutine function carrying a ``calls`` counter.
    """
    async def call() -> str:
        """Fail until the quota is spent, then return a sentinel.

        Returns:
            The string ``'ok'``.

        Raises:
            BaseException: ``error``, until ``succeed_after`` calls have
                been made.
        """
        # `type: ignore[attr-defined]` -- the call counter is kept as an
        # attribute on the function object so the closure has no mutable
        # cell to reset between retries; a function is not typed as
        # carrying arbitrary attributes, and the alternative (a class, or
        # a `nonlocal` counter) is more machinery than the fixture needs.
        call.calls += 1  # type: ignore[attr-defined]
        if call.calls <= succeed_after:  # type: ignore[attr-defined]
            raise error
        return 'ok'

    call.calls = 0  # type: ignore[attr-defined]
    return call


# --- R7 behaviour 1: retry on a transient failure --------------------------


async def test_behaviour_1_a_transient_failure_is_retried() -> None:
    """One failure then one success returns, without the caller seeing it.

    R7's first fitness behaviour, asserted against the facade because the
    facade is what runs the loop now.
    """
    call = counting(Boom('transient'), succeed_after=1)
    subject = breaker(
        retry_config={'name': 'r', 'allowed_retries': 2, 'delay': 0.1})

    assert await subject.run(call) == 'ok'
    assert call.calls == 2


async def test_behaviour_1_retries_stop_at_the_caller_s_budget() -> None:
    """``allowed_retries`` is a budget, not a suggestion.

    Three attempts for ``allowed_retries=2``: the first is not a retry.
    """
    call = counting(Boom('always'), succeed_after=99)
    subject = breaker(
        maximum_failures=99,
        retry_config={'name': 'r', 'allowed_retries': 2, 'delay': 0.1})

    with pytest.raises(RetriesExhausted):
        await subject.run(call)

    assert call.calls == 3


# --- R7 behaviour 2: abort on an abortable exception -----------------------


@pytest.mark.parametrize(
    'error',
    [
        pytest.param(ConfigurationError('disallowed verb'), id='config'),
        pytest.param(ResponseTooLargeError('body over cap'), id='too-large'),
    ],
)
@pytest.mark.parametrize(
    'retry_config',
    [
        pytest.param(None, id='no-retry-config'),
        pytest.param(
            {'name': 'r', 'allowed_retries': 5, 'delay': 0.1},
            id='with-retry-config'),
    ],
)
async def test_behaviour_2_a_refusal_aborts_and_is_never_counted(
    error: BaseException,
    retry_config: Optional[dict[str, Any]],
) -> None:
    """This library's own refusals must not open a destination's circuit.

    The **no-retry-config** row is the one that matters and the one a
    naive implementation fails. With a breaker config but no
    ``retry_config`` there is no ``RetryPolicy`` at all, so an abort
    check written behind ``if self.retry_policy is not None`` never runs:
    a body over the cap, a disallowed verb or a hostile redirect hop is
    counted as a *destination* failure, and one caller with a bad verb
    takes that destination offline for every other caller in the process.

    The refusal also arrives as itself. Wrapped in a ``RetriesExhausted``
    -- whose own ``str()`` is ``''`` -- it would reach the caller as a
    blank message where a named reason belongs.
    """
    call = failing(error)
    subject = breaker(maximum_failures=1, retry_config=retry_config)

    with pytest.raises(type(error)) as raised:
        await subject.run(call)

    assert raised.value is error
    assert str(raised.value)
    assert subject.state is BreakerState.CLOSED
    assert subject.failures == 0


async def test_behaviour_2_a_caller_named_abortable_also_aborts() -> None:
    """The caller's own ``abortable_exceptions`` is honoured too."""
    call = failing(Boom('caller says stop'))
    subject = breaker(retry_config={
        'name': 'r',
        'allowed_retries': 5,
        'delay': 0.1,
        'abortable_exceptions': [Boom],
    })

    with pytest.raises(Boom):
        await subject.run(call)

    assert subject.failures == 0


async def test_behaviour_1_a_caller_named_retriable_list_excludes_others(
) -> None:
    """A caller who names ``retriable_exceptions`` excludes everything else.

    The delegated classifier's *matching* branch, which nothing else in
    the suite reached. Step 22's costing found
    ``retry_policy.py:136`` -- the ``any(isinstance(...))`` that answers
    "is this one of the caller's retriable classes?" -- never executed:
    every other test leaves ``retriable_exceptions`` unset, so
    classification short-circuits at ``:133`` on the ``is None`` guard
    and returns True without consulting a list. Replacing line 136 with
    ``return False`` therefore left the whole suite green, which means
    half of the residual this ADR is costing was being paid for and not
    exercised.

    Two calls, one breaker config, so the assertion is about the list and
    not about the budget: a ``Boom`` is named retriable and is retried; a
    ``ConnectionResetError`` -- retriable to the *facade*, since it is an
    ``OSError``, but absent from the caller's list -- is not.
    """
    named = counting(Boom('named retriable'), succeed_after=1)
    subject = breaker(
        maximum_failures=99,
        retry_config={'name': 'r', 'allowed_retries': 3, 'delay': 0.1,
                      'retriable_exceptions': [Boom]})

    assert await subject.run(named) == 'ok'
    assert named.calls == 2

    unnamed = counting(ConnectionResetError('not on the list'),
                       succeed_after=1)
    with pytest.raises(RetriesExhausted):
        await subject.run(unnamed)

    assert unnamed.calls == 1


async def test_a_library_bug_is_not_a_transport_failure() -> None:
    """A ``KeyError`` is this package's bug and propagates untouched.

    It is outside :data:`RETRIABLE_FAILURES`, so the loop never sees it:
    not retried, not counted, not wrapped. Reporting a library bug as a
    failed request is what hid most of this audit.
    """
    subject = breaker()

    with pytest.raises(KeyError):
        await subject.run(failing(KeyError('library bug')))

    assert subject.failures == 0
    assert subject.state is BreakerState.CLOSED


def test_asyncio_timeout_error_is_named_in_the_retriable_set() -> None:
    """The 3.10 class split, pinned as a set-membership fact.

    On Python 3.10 -- inside this project's ``requires-python >= 3.10``
    -- ``asyncio.TimeoutError is TimeoutError`` is False and it is not an
    ``OSError`` subclass. On 3.11+ both are True. Relying on either
    identity therefore leaves the interpreter floor with aiohttp's
    total-deadline timeout escaping the loop un-retried *and uncounted*,
    so a destination that times out on every call could never open its
    circuit -- silently, on exactly one supported interpreter.

    Asserted as membership rather than as behaviour because membership is
    what holds on all five interpreters; a behavioural assertion would
    pass on 3.11+ through ``OSError`` even with the entry deleted.
    """
    assert asyncio.TimeoutError in RETRIABLE_FAILURES


async def test_a_timeout_is_retried_and_counted() -> None:
    """The behavioural half of the above, on whatever interpreter runs."""
    call = counting(asyncio.TimeoutError(), succeed_after=1)
    subject = breaker(
        retry_config={'name': 'r', 'allowed_retries': 2, 'delay': 0.1})

    assert await subject.run(call) == 'ok'
    assert call.calls == 2


# --- R7 behaviour 3: the circuit opens after maximum_failures -------------


async def test_behaviour_3_the_circuit_opens_and_then_refuses() -> None:
    """Two failures open it; the third call never reaches the transport."""
    call = failing(Boom('dead host'))
    subject = breaker(maximum_failures=2)

    for _ in range(2):
        with pytest.raises(RetriesExhausted):
            await subject.run(call)

    assert subject.state is BreakerState.OPEN

    reached = counting(Boom('never'), succeed_after=0)
    with pytest.raises(CircuitOpen):
        await subject.run(reached)

    assert reached.calls == 0


async def test_maximum_failures_zero_is_reachable(
) -> None:
    """L9: an explicit 0 opens on the first failure, rather than meaning 5.

    ``kwargs.get('maximum_failures', None) or CIRCUIT_BREAKER_RETRY``
    made 0 unreachable -- the caller's explicit 0 is falsy and was
    silently replaced by the default of 5.
    """
    subject = breaker(maximum_failures=0)

    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('one')))

    assert subject.state is BreakerState.OPEN


async def test_a_success_forgets_the_accumulated_failures() -> None:
    """The count is of *consecutive* failures, so one success clears it."""
    subject = breaker(maximum_failures=2)

    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('one')))
    assert subject.failures == 1

    assert await subject.run(succeed) == 'ok'

    assert subject.failures == 0
    assert subject.state is BreakerState.CLOSED


# --- R7 behaviour 4 + R24: half-open, on the injected clock ---------------


async def test_behaviour_4_half_open_is_reached_by_advancing_the_clock(
    fake_clock: FakeClock,
    recording_sleep: RecordingSleep,
) -> None:
    """The open circuit half-opens after ``timeout``, and not before.

    Driven entirely by moving :class:`FakeClock`. A test that waited 60
    real seconds for this would be unrunnable, and one that waited a
    shortened real interval would be flaky -- which is why the seam
    exists.
    """
    subject = breaker(
        clock=fake_clock, sleep=recording_sleep,
        maximum_failures=1, timeout=60.0)

    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('dead')))
    assert subject.state is BreakerState.OPEN

    fake_clock.advance(59.9)
    with pytest.raises(CircuitOpen):
        await subject.run(succeed)

    fake_clock.advance(0.1)
    assert await subject.run(succeed) == 'ok'
    assert subject.state is BreakerState.CLOSED


async def test_behaviour_4_a_failed_trial_re_opens_the_circuit(
    fake_clock: FakeClock,
) -> None:
    """The trial asked one question; a failure answers it no.

    Re-opening restarts the countdown from the *trial's* time, so a
    still-dead host is not probed on every subsequent call.
    """
    subject = breaker(clock=fake_clock, maximum_failures=1, timeout=30.0)

    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('dead')))
    fake_clock.advance(30.0)

    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('still dead')))

    assert subject.state is BreakerState.OPEN
    with pytest.raises(CircuitOpen):
        await subject.run(succeed)


async def test_behaviour_4_half_open_admits_exactly_one_trial(
    fake_clock: FakeClock,
) -> None:
    """Fifty concurrent callers into a half-open circuit send one request.

    The defect this pins is a check-then-act race, and it is invisible to
    a sequential test. Reading the state and *then* awaiting the call
    lets all fifty coroutines observe HALF_OPEN before any of them has
    reached a recorder, so all fifty reach the transport -- a dead host
    receiving a fifty-request thundering herd at the exact moment it is
    least able to answer, which is the whole purpose of half-open lost.

    The gate is what makes the window wide enough to be certain: every
    admitted call parks inside the transport until the test releases it,
    so "one got in" cannot be an artefact of the others being slow.
    """
    subject = breaker(clock=fake_clock, maximum_failures=1, timeout=10.0)

    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('dead')))
    fake_clock.advance(10.0)

    gate = asyncio.Event()
    admitted = 0

    async def gated() -> str:
        """Record admission, then park until the test releases.

        Returns:
            The string ``'ok'``.
        """
        nonlocal admitted
        admitted += 1
        await gate.wait()
        return 'ok'

    tasks = [
        asyncio.ensure_future(subject.run(gated)) for _ in range(50)
    ]
    await asyncio.sleep(0)
    assert admitted == 1

    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert results.count('ok') == 1
    assert sum(isinstance(r, CircuitOpen) for r in results) == 49


async def test_the_trial_claim_is_released_on_every_route_out(
    fake_clock: FakeClock,
) -> None:
    """A claim held past its attempt would wedge the circuit forever.

    The release lives in ``run``'s ``finally`` rather than in
    ``record_success``: every route back to half-open goes through
    ``record_failure``, and an abort reaches neither recorder, so a
    release written into a recorder leaves the claim held and the
    destination unreachable for the life of the process. Each row drives
    one of the three routes out and then asserts the *next* trial is
    still admitted.
    """
    subject = breaker(clock=fake_clock, maximum_failures=1, timeout=5.0)

    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('dead')))

    # Route 1: the trial aborts -- neither recorder runs.
    fake_clock.advance(5.0)
    with pytest.raises(ConfigurationError):
        await subject.run(failing(ConfigurationError('refused')))

    # Route 2: the trial fails -- record_failure runs, re-opening.
    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('still dead')))
    assert subject.state is BreakerState.OPEN

    # Route 3: the trial succeeds -- the circuit closes and stays usable.
    fake_clock.advance(5.0)
    assert await subject.run(succeed) == 'ok'
    assert await subject.run(succeed) == 'ok'
    assert subject.state is BreakerState.CLOSED


# --- R7 behaviour 5 + R24: backoff, jitter and the thundering herd -------


async def test_behaviour_5_three_retries_are_spaced_and_exponential(
    recording_sleep: RecordingSleep,
) -> None:
    """M13: retries are spaced, and each wait is longer than the last.

    Asserted off the injected sleep's recorded durations, which is the
    only way to see the spacing without paying for it. Jitter is off in
    this row so the exponential shape itself is assertable; the row below
    puts it back.
    """
    subject = breaker(
        sleep=recording_sleep,
        maximum_failures=99,
        retry_config={
            'name': 'r',
            'allowed_retries': 3,
            'delay': 0.5,
            'max_delay': 30.0,
            'jitter': False,
        })

    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('dead')))

    assert recording_sleep.waits == [0.5, 1.0, 2.0]


async def test_behaviour_5_the_defaults_are_not_a_thundering_herd(
) -> None:
    """M13: the shipped defaults space retries and randomise them.

    The defaults used to be ``delay=0``, ``max_delay=0`` and
    ``jitter=False``: immediate, un-spaced, perfectly synchronised
    retries against a dependency already failing, with no working breaker
    to stop the amplification.
    """
    assert CIRCUIT_BREAKER_DELAY > 0
    assert CIRCUIT_BREAKER_MAX_DELAY > 0
    assert CIRCUIT_BREAKER_JITTER is True

    policy = get_retry_policy('r', allowed_retries=3)

    assert policy.backoff.jitter is True
    assert policy.backoff.for_attempt(1) > 0 or policy.backoff.jitter


async def test_behaviour_5_two_sequences_are_not_synchronised() -> None:
    """Jitter is what stops two callers marching in step onto a dead host.

    Two independent breakers on identical configuration retry the same
    number of times; with jitter on, their recorded wait sequences differ.
    Compared as whole sequences rather than pairwise, so the assertion
    cannot fail on a single coincidental collision -- and over three
    waits drawn from a continuous range, two identical sequences are as
    close to impossible as a test gets.
    """
    async def sequence() -> list[float]:
        """Drive one breaker to exhaustion and return its waits.

        Returns:
            The durations that breaker asked to wait for.
        """
        recorder = RecordingSleep()
        subject = breaker(
            sleep=recorder,
            maximum_failures=99,
            retry_config={
                'name': 'r',
                'allowed_retries': 3,
                'delay': 1.0,
                'max_delay': 30.0,
                'jitter': True,
            })
        with pytest.raises(RetriesExhausted):
            await subject.run(failing(Boom('dead')))
        return recorder.waits

    first, second = await asyncio.gather(sequence(), sequence())

    assert len(first) == len(second) == 3
    assert first != second


async def test_behaviour_5_max_delay_clamps_the_growth(
    recording_sleep: RecordingSleep,
) -> None:
    """Exponential growth stops at the caller's ceiling."""
    subject = breaker(
        sleep=recording_sleep,
        maximum_failures=99,
        retry_config={
            'name': 'r',
            'allowed_retries': 4,
            'delay': 1.0,
            'max_delay': 3.0,
            'jitter': False,
        })

    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('dead')))

    assert recording_sleep.waits == [1.0, 2.0, 3.0, 3.0]


# --- M14: the backoff shape is named, not guessed from `name` ------------


@pytest.mark.parametrize(
    'backoff, expected',
    [
        pytest.param('exponential', [2.0, 4.0], id='exponential'),
        pytest.param('constant', [2.0, 2.0], id='constant'),
    ],
)
async def test_m14_the_backoff_shape_is_selected_by_its_own_key(
    recording_sleep: RecordingSleep,
    backoff: str,
    expected: list[float],
) -> None:
    """The magic ``name == 'backoff'`` string is gone.

    The README documents ``name`` as "Any name", while the code compared
    it to the literal ``'backoff'`` -- so a caller following the docs got
    a constant zero-second delay and had their ``max_delay`` and
    ``jitter`` silently ignored. Both rows here use a README-style
    ``name`` and get the shape they *named*.
    """
    subject = breaker(
        sleep=recording_sleep,
        maximum_failures=99,
        retry_config={
            'name': 'my-service-policy',
            'allowed_retries': 2,
            'delay': 2.0,
            'max_delay': 30.0,
            'jitter': False,
            'backoff': backoff,
        })

    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('dead')))

    assert recording_sleep.waits == expected


def test_m14_an_unknown_backoff_name_is_refused() -> None:
    """A shape this library cannot produce is named, not defaulted."""
    with pytest.raises(ConfigurationError) as raised:
        validated_breaker_config({
            'retry_config': {
                'name': 'r', 'allowed_retries': 1, 'backoff': 'fibonacci'},
        })

    assert 'backoff' in str(raised.value)
    assert sorted(BACKOFF_NAMES)[0] in str(raised.value)


# --- R7 behaviour 6: the cause survives the wrapper ----------------------


async def test_behaviour_6_the_exhausted_wrapper_carries_its_cause() -> None:
    """``str(RetriesExhausted())`` is ``''``, so the cause is the message.

    The envelope layer recovers it through ``unwrap_cause``; this asserts
    the link that makes that possible is actually there.
    """
    original = Boom('connection reset by peer')
    subject = breaker(maximum_failures=99)

    with pytest.raises(RetriesExhausted) as raised:
        await subject.run(failing(original))

    assert raised.value.__cause__ is original
    assert str(raised.value) == ''
    assert str(raised.value.__cause__) == 'connection reset by peer'


async def test_an_open_circuit_chains_the_failure_that_opened_it() -> None:
    """``CircuitOpen`` says which failure it is refusing on behalf of.

    The chain is *within* one ``run``: the circuit opens on the first
    attempt, the retry budget then sends the loop back round, and the
    refusal it meets names the failure that caused it. ``str(CircuitOpen())``
    is ``''``, so without the chain the caller gets a blank message.

    A later, separate ``run`` is deliberately not chained -- that
    refusal's cause belongs to a call the current caller never made.
    """
    original = Boom('dead host')
    subject = breaker(
        maximum_failures=1,
        retry_config={'name': 'r', 'allowed_retries': 3, 'delay': 0.1})

    with pytest.raises(CircuitOpen) as raised:
        await subject.run(failing(original))

    assert raised.value.__cause__ is original

    with pytest.raises(CircuitOpen) as later:
        await subject.run(succeed)

    assert later.value.__cause__ is None


# --- M15: a typo is named, not silently discarded -------------------------


@pytest.mark.parametrize(
    'config, typo',
    [
        pytest.param({'max_failures': 3}, 'max_failures', id='max_failures'),
        pytest.param({'reset_timeout': 5}, 'reset_timeout',
                     id='reset_timeout'),
        pytest.param({'maximum_failure': 3}, 'maximum_failure',
                     id='maximum_failure'),
    ],
)
def test_m15_a_config_typo_raises_and_names_the_key(
    config: dict[str, Any],
    typo: str,
) -> None:
    """An untyped ``**kwargs`` bag read with ``.get()`` swallowed these.

    Each row is a real near-miss of a real key, and each used to leave
    the caller running on a default they never chose -- ``max_failures``
    silently meaning "the breaker opens after 5", not after 3.
    """
    with pytest.raises(ConfigurationError) as raised:
        validated_breaker_config(config)

    assert typo in str(raised.value)
    assert 'maximum_failures' in str(raised.value)


def test_m15_a_retry_config_typo_is_named_too() -> None:
    """The nested structure is validated with the same rigour."""
    with pytest.raises(ConfigurationError) as raised:
        validated_breaker_config({
            'retry_config': {'allowed_retries': 1, 'retires': 3},
        })

    assert 'retires' in str(raised.value)


def test_m15_a_typo_is_refused_on_every_call_not_just_the_first(
) -> None:
    """The contract must not depend on whether the registry has the key.

    Validating only on a cache miss made ``{'max_failures': 3}`` -- R24's
    own named example -- raise on the first call to a destination and
    pass in silence on every call after it, because the second call
    returned early on the hit and never looked at the config at all.

    The ordering matters and is the whole test. A destination is warmed
    with a *valid* config first, so every call after it is a cache hit;
    the typo is then offered to that hit. An implementation that returns
    early on a hit accepts it silently, and the caller runs on defaults
    they never chose against a contract that now depends on cache state.
    """
    destination = ('https', 'typo.example', 443)
    warm = get_breaker(*destination, {'maximum_failures': 3})

    for _ in range(3):
        with pytest.raises(ConfigurationError) as raised:
            get_breaker(*destination, {'max_failures': 3})
        assert 'max_failures' in str(raised.value)

    # Rejecting the typo did not cost the caching it protects: the
    # warmed breaker, and its accumulated state, is still the one
    # returned for a valid config.
    assert get_breaker(*destination, {'maximum_failures': 3}) is warm


def test_a_cache_hit_keeps_the_first_caller_s_settings() -> None:
    """Rebuilding on a hit would discard the very history H8 needs.

    The second caller's different ``maximum_failures`` is validated --
    an invalid one would still raise -- and then discarded, because the
    breaker it would configure is the one already counting failures for
    this destination.
    """
    first = get_breaker('https', 'settled.example', 443,
                        {'maximum_failures': 3})
    second = get_breaker('https', 'settled.example', 443,
                         {'maximum_failures': 9})

    assert second is first
    assert first.maximum_failures == 3


# --- L9 / L10: numbers that mean what they say ---------------------------


@pytest.mark.parametrize(
    'key',
    [
        pytest.param('maximum_failures', id='maximum_failures'),
        pytest.param('timeout', id='timeout'),
    ],
)
@pytest.mark.parametrize(
    'value, accepted',
    [
        pytest.param(0, True, id='zero'),
        pytest.param(True, False, id='bool'),
        pytest.param(1.5, True, id='float'),
        pytest.param('5', False, id='string'),
    ],
)
def test_l9_l10_numeric_settings_accept_numbers_and_reject_bool(
    key: str,
    value: Any,
    accepted: bool,
) -> None:
    """``isinstance(x, int)`` accepted ``True`` and rejected ``1.5``.

    ``bool`` is an ``int`` subclass, so the old check read ``True`` as
    the number 1 -- a caller who wrote ``timeout=True`` got a one-second
    circuit rather than an error. It also refused ``1.5``, which is a
    perfectly good number of seconds. And ``0`` had to become reachable
    (L9): the truthiness fallback it replaced made an explicit 0 mean 5.

    ``maximum_failures`` is whole-number-only, so its float row asserts
    the rejection its sibling accepts -- the difference is deliberate and
    is asserted rather than assumed.
    """
    whole_only = key == 'maximum_failures' and value == 1.5
    if accepted and not whole_only:
        settings = validated_breaker_config({key: value})
        expected = 'maximum_failures' if key == 'maximum_failures' \
            else 'reset_timeout_seconds'
        assert settings[expected] == value
        return

    with pytest.raises(ConfigurationError) as raised:
        validated_breaker_config({key: value})

    assert key in str(raised.value)


@pytest.mark.parametrize(
    'value, accepted',
    [
        pytest.param(0, True, id='zero'),
        pytest.param(True, False, id='bool'),
        pytest.param(1.5, True, id='float'),
        pytest.param('5', False, id='string'),
    ],
)
def test_l10_retry_delays_accept_numbers_and_reject_bool(
    value: Any,
    accepted: bool,
) -> None:
    """The same contract on the retry side, where floats are ordinary."""
    config = {
        'retry_config': {'name': 'r', 'allowed_retries': 1, 'delay': value},
    }
    if accepted:
        assert validated_breaker_config(config)['retry_policy'] is not None
        return

    with pytest.raises(ConfigurationError) as raised:
        validated_breaker_config(config)

    assert 'delay' in str(raised.value)


def test_a_negative_setting_is_refused() -> None:
    """A negative timeout is not a shorter one; it is a mistake."""
    with pytest.raises(ConfigurationError) as raised:
        validated_breaker_config({'timeout': -1})

    assert 'timeout' in str(raised.value)


@pytest.mark.parametrize(
    'retry_config, expected',
    [
        pytest.param({'name': 'r'}, 'allowed_retries',
                     id='allowed_retries-missing'),
        pytest.param({'allowed_retries': -1}, 'allowed_retries',
                     id='allowed_retries-negative'),
        pytest.param({'allowed_retries': 1, 'name': 7}, 'name',
                     id='name-not-a-string'),
        pytest.param({'allowed_retries': 1, 'jitter': 'yes'}, 'jitter',
                     id='jitter-not-a-bool'),
        pytest.param({'allowed_retries': 1, 'on_abort': 'nope'}, 'on_abort',
                     id='callback-not-callable'),
        pytest.param({'allowed_retries': 1, 'retriable_exceptions': Boom},
                     'retriable_exceptions', id='exceptions-not-a-sequence'),
        pytest.param({'allowed_retries': 1,
                      'abortable_exceptions': [Boom('instance')]},
                     'abortable_exceptions', id='exception-instance'),
    ],
)
def test_every_retry_setting_is_validated_and_named(
    retry_config: dict[str, Any],
    expected: str,
) -> None:
    """Each rejection names the key, so the caller can act on it."""
    with pytest.raises(ConfigurationError) as raised:
        validated_breaker_config({'retry_config': retry_config})

    assert expected in str(raised.value)


@pytest.mark.parametrize(
    'config',
    [
        pytest.param('not-a-mapping', id='breaker-config'),
        pytest.param({'retry_config': 'not-a-mapping'}, id='retry-config'),
    ],
)
def test_a_config_that_is_not_a_mapping_is_refused(config: Any) -> None:
    """Reported as configuration, never as an ``AttributeError``."""
    with pytest.raises(ConfigurationError):
        validated_breaker_config(config)


# --- M12: the caller's dict is theirs ------------------------------------


async def test_m12_two_requests_sharing_one_config_leave_it_unchanged(
) -> None:
    """A reused ``protocol_info`` came back carrying a live RetryPolicy.

    ``base.py`` wrote ``retry_policy`` into the caller's own
    ``circuit_breaker_config``, so the second call to a shared config saw
    a key its owner never put there -- and one that
    :func:`validated_breaker_config` now rejects as unknown, which is
    what makes this a correctness bug rather than an untidiness.
    """
    shared: dict[str, Any] = {
        'maximum_failures': 2,
        'retry_config': {'name': 'r', 'allowed_retries': 1, 'delay': 0.1},
    }
    before = {
        'maximum_failures': 2,
        'retry_config': dict(shared['retry_config']),
    }

    get_breaker('https', 'shared-a.example', 443, shared)
    get_breaker('https', 'shared-b.example', 443, shared)

    assert shared == before


# --- the three callbacks, invoked by the facade --------------------------


async def test_the_three_callbacks_are_invoked_by_the_facade() -> None:
    """Ruling D: the facade invokes the callbacks, not the dependency.

    They are *configured* on the ``RetryPolicy`` but *invoked* by
    ``Failsafe.run`` -- the only thing that would otherwise do so, and
    the loop this facade replaced.
    """
    seen: list[str] = []
    retry_config = {
        'name': 'r',
        'allowed_retries': 1,
        'delay': 0.1,
        'on_failed_attempt': lambda: seen.append('failed'),
        'on_retries_exhausted': lambda: seen.append('exhausted'),
        'on_abort': lambda: seen.append('abort'),
    }

    subject = breaker(maximum_failures=99, retry_config=retry_config)
    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('dead')))

    assert seen == ['failed', 'failed', 'exhausted']

    seen.clear()
    aborting = breaker(maximum_failures=99, retry_config=retry_config)
    with pytest.raises(ConfigurationError):
        await aborting.run(failing(ConfigurationError('refused')))

    assert seen == ['abort']


async def test_a_callback_that_raises_reaches_the_caller() -> None:
    """``pyfailsafe`` logs and swallows these; this package does not.

    A blanket handler around a caller's callback buries their bug in a
    log line underneath a failing request -- the exact pattern this
    package bans outright.
    """
    def explode() -> None:
        """Raise the caller's own bug.

        Raises:
            ValueError: Always.
        """
        raise ValueError('callback bug')

    subject = breaker(
        maximum_failures=99,
        retry_config={
            'name': 'r', 'allowed_retries': 0, 'delay': 0.1,
            'on_failed_attempt': explode,
        })

    with pytest.raises(ValueError, match='callback bug'):
        await subject.run(failing(Boom('dead')))


# --- M16 / FI-4: one breaker per destination, and not one for all --------


def test_m16_two_destinations_do_not_share_a_breaker() -> None:
    """The whole of FI-4, at its smallest.

    A module-global breaker would fix H8 and return the *same* object
    here -- and one flaky host would then open the circuit for every
    destination in the process, which is a worse outage than the dead
    breaker it replaced.
    """
    first = get_breaker('https', 'a.example', 443)
    second = get_breaker('https', 'b.example', 443)

    assert first is not second


@pytest.mark.parametrize(
    'other',
    [
        pytest.param(('https', 'a.example', 8443), id='different-port'),
        pytest.param(('http', 'a.example', 443), id='different-family'),
        pytest.param(('https', 'other.example', 443), id='different-host'),
    ],
)
def test_m16_every_part_of_the_key_separates_destinations(
    other: tuple[str, str, int],
) -> None:
    """All three parts matter: one host on two ports is two destinations."""
    assert get_breaker('https', 'a.example', 443) is not get_breaker(*other)


async def test_m16_an_open_circuit_for_a_leaves_b_reachable() -> None:
    """R24's own criterion, at the breaker level.

    Host A is failed until its circuit opens; host B's next call is still
    dispatched. Under a module-global breaker, B's call would be refused
    for a fault B never had.
    """
    host_a = get_breaker('https', 'flaky.example', 443,
                         {'maximum_failures': 1})
    host_b = get_breaker('https', 'healthy.example', 443,
                         {'maximum_failures': 1})

    with pytest.raises(RetriesExhausted):
        await host_a.run(failing(Boom('dead')))

    assert host_a.state is BreakerState.OPEN
    with pytest.raises(CircuitOpen):
        await host_a.run(succeed)

    assert host_b.state is BreakerState.CLOSED
    assert await host_b.run(succeed) == 'ok'


async def test_h8_failures_accumulate_across_separate_lookups() -> None:
    """H8: the breaker has to survive the request that created it.

    Each lookup stands for a separate ``request()`` call -- the entry
    point builds a fresh protocol object every time, which is exactly why
    a breaker constructed in ``__init__`` met a zeroed count on every
    call and could never open. Here the count crosses the lookups.
    """
    destination = ('https', 'accumulating.example', 443)
    config = {'maximum_failures': 3}

    for _ in range(3):
        with pytest.raises(RetriesExhausted):
            await get_breaker(*destination, config).run(failing(Boom('dead')))

    reached = counting(Boom('never'), succeed_after=0)
    with pytest.raises(CircuitOpen):
        await get_breaker(*destination, config).run(reached)

    assert reached.calls == 0


def test_the_registry_is_bounded_and_evicts_the_least_recently_used(
) -> None:
    """Process-lifetime state that grows without limit is a leak.

    300 distinct hosts against the production cap of 256 leaves 256, and
    the 44 evicted are the oldest -- so the run also proves the bound is
    enforced on the way in rather than only measured afterwards.
    """
    for index in range(300):
        get_breaker('https', f'host-{index}.example', 443)

    assert registry_size() == BREAKER_REGISTRY_MAX


def test_eviction_is_by_least_recent_use_not_by_insertion() -> None:
    """A destination under active failure must never be the one evicted.

    Insertion-order eviction would discard the *first* host regardless of
    how recently it was used -- and the host being retried hardest is
    precisely the one whose accumulated count matters. Touching it keeps
    it, and the untouched neighbour goes instead.

    ``maximum`` is a parameter so the bound is exercised at a size a test
    can read, rather than by allocating 256 breakers to observe one
    eviction.
    """
    first = get_breaker('https', 'first.example', 443, maximum=3)
    get_breaker('https', 'second.example', 443, maximum=3)
    get_breaker('https', 'third.example', 443, maximum=3)

    # Use the oldest, then overflow by one.
    assert get_breaker('https', 'first.example', 443, maximum=3) is first
    get_breaker('https', 'fourth.example', 443, maximum=3)

    assert registry_size() == 3
    assert get_breaker('https', 'first.example', 443, maximum=3) is first
    # `second` was the least recently used, so it is the one that went.
    assert registry_size() == 3


def test_reset_clears_the_registry() -> None:
    """The seam a long-lived registry needs so tests cannot leak.

    Without it, one test's open circuit decides whether an unrelated
    later test's first request is dispatched at all -- and which tests
    fail depends on the order ``pytest-randomly`` happens to pick.
    """
    first = get_breaker('https', 'resettable.example', 443)
    assert registry_size() == 1

    reset()

    assert registry_size() == 0
    assert get_breaker('https', 'resettable.example', 443) is not first


# --- the destination key itself ------------------------------------------


@pytest.mark.parametrize(
    'protocol, url, port, expected',
    [
        pytest.param('HTTPS', 'https://a.example/p', None,
                     ('https', 'a.example', 443), id='https-default-port'),
        pytest.param('HTTP', 'http://a.example/p', None,
                     ('http', 'a.example', 80), id='http-default-port'),
        pytest.param('HTTPS', 'https://a.example:8443/p', None,
                     ('https', 'a.example', 8443), id='explicit-port'),
        pytest.param('HTTPS', 'https://A.Example/p', None,
                     ('https', 'a.example', 443), id='host-case-folded'),
        pytest.param('FTP', 'ftp.example', 2121,
                     ('ftp', 'ftp.example', 2121), id='ftp-bare-host'),
        pytest.param('SFTP', 'sftp.example', None,
                     ('sftp', 'sftp.example', 22), id='sftp-default-port'),
        pytest.param('HTTPS', 'https://[::1]:9000/p', None,
                     ('https', '::1', 9000), id='ipv6-with-port'),
        pytest.param('HTTPS', 'https://[::1]/p', None,
                     ('https', '::1', 443), id='ipv6-without-port'),
    ],
)
def test_destination_of_reads_the_family_host_and_port(
    protocol: str,
    url: str,
    port: Optional[int],
    expected: tuple[str, str, int],
) -> None:
    """The registry key, across the shapes each protocol actually passes.

    HTTP carries host and port inside the URL; FTP and SFTP take a bare
    host and their port from ``protocol_info``, which is why ``port`` is
    a parameter rather than something parsed out here.
    """
    assert destination_of(protocol, url, port) == expected


def test_an_unknown_scheme_does_not_collapse_onto_one_key() -> None:
    """The sentinel is ``-1``, and ``0`` would have been a bug.

    ``0`` is a legal port number, so using it to mean "unknown" gives
    every unknown-scheme destination the same *port* component -- which
    is survivable while the host differs, and is not the point. The point
    is that ``-1`` can never collide with a real port, so an unknown
    scheme cannot be confused with a destination genuinely on port 0.
    """
    family, host, port = destination_of('GOPHER', 'gopher://a.example/p')

    assert (family, host) == ('gopher', 'a.example')
    assert port == UNKNOWN_PORT
    assert port != 0
    assert destination_of('GOPHER', 'gopher://b.example/p') != (
        family, host, port)


# --- R7 behaviour 7: the seam, asserted against the facade ---------------


async def test_behaviour_7_a_full_cycle_never_touches_the_real_clock(
    monkeypatch: pytest.MonkeyPatch,
    fake_clock: FakeClock,
    recording_sleep: RecordingSleep,
) -> None:
    """R7's seventh behaviour, and the reason the facade exists.

    ``pyfailsafe`` provably has no seam -- it hardcodes
    ``time.monotonic()`` at ``circuit_breaker.py:139,142`` and
    ``await asyncio.sleep(...)`` at ``failsafe.py:103`` -- so this is
    asserted against *this library's* facade. A test written against the
    dependency would be a test of a known-false proposition.

    ``asyncio.sleep`` is replaced with a tripwire that fails on contact,
    and a full open -> half-open -> close cycle completes without it
    firing -- a stronger claim than "the recorder was used": that the
    real one was not.

    ``time.monotonic`` is deliberately **not** tripwired, and saying why
    matters more than the assertion would. asyncio's own event loop calls
    it on every scheduling decision, so a tripwire there fails on the
    loop's traffic and proves nothing about this facade. What proves the
    clock instead is the cycle's *shape*: the breaker's reset timeout is
    60 seconds and the only thing that elapses here is
    :meth:`FakeClock.advance`. A facade reading the real clock would find
    microseconds elapsed, refuse the trial, and this test would fail on
    the ``CircuitOpen`` that followed.
    """
    async def forbidden_sleep(seconds: float) -> None:
        """Fail the test on contact.

        Args:
            seconds: Ignored.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError('the facade awaited the real asyncio.sleep')

    monkeypatch.setattr(asyncio, 'sleep', forbidden_sleep)
    started = time.monotonic()

    subject = get_breaker(
        'https', 'seam.example', 443,
        {'maximum_failures': 1,
         'timeout': 60.0,
         'retry_config': {'name': 'r', 'allowed_retries': 1, 'delay': 1.0,
                          'jitter': False}},
        clock=fake_clock, sleep=recording_sleep)

    # Open: the failure counts, the retry waits, the circuit trips.
    with pytest.raises(CircuitOpen):
        await subject.run(failing(Boom('dead')))
    assert subject.state is BreakerState.OPEN
    assert recording_sleep.waits == [1.0]

    # Half-open: reached only by advancing the injected clock.
    fake_clock.advance(60.0)

    # Close: the trial succeeds, which it could not if the 60-second
    # countdown were being measured against the real clock.
    assert await subject.run(succeed) == 'ok'
    assert subject.state is BreakerState.CLOSED
    assert time.monotonic() - started < 60.0


def test_behaviour_7_the_seam_defaults_to_the_real_implementations() -> None:
    """The seam must not leak into a consumer's world.

    A test-only parameter with no default, or one defaulting to a stub,
    would make every production caller responsible for supplying a clock.
    Both default to the real functions, so a consumer never sees them.
    """
    subject = CircuitBreakerHelper()

    assert subject.clock is time.monotonic
    assert subject.sleep is asyncio.sleep


def test_behaviour_7_the_seam_is_not_part_of_the_registry_key(
    fake_clock: FakeClock,
) -> None:
    """Substituting a clock must not create a second breaker.

    If it did, a test's fake-clock lookup would silently get a *different*
    breaker from the one the code under test is using -- and the failure
    count it meant to observe would be on the other object.
    """
    first = get_breaker('https', 'keyed.example', 443, clock=fake_clock)
    second = get_breaker('https', 'keyed.example', 443)

    assert second is first
    assert registry_size() == 1


def test_behaviour_7_an_explicit_seam_argument_wins_over_the_config(
    fake_clock: FakeClock,
) -> None:
    """Both routes exist; the more specific statement of intent wins."""
    other = FakeClock(now=999.0)

    subject = get_breaker(
        'https', 'precedence.example', 443,
        {'clock': other}, clock=fake_clock)

    assert subject.clock is fake_clock


def test_the_default_abortable_set_is_this_library_s_own_refusals() -> None:
    """Pinned as a set, because the *absence* of an entry is the defect.

    Every member is this library refusing a call the caller made wrong.
    Counting one of them against the destination is what let a single
    caller's bad verb take that destination offline for everybody else.
    """
    assert ConfigurationError in DEFAULT_ABORTABLE_EXCEPTIONS
    assert ResponseTooLargeError in DEFAULT_ABORTABLE_EXCEPTIONS


# --- R28: the last two uncovered lines of the facade -----------------------


@pytest.mark.parametrize(
    'backoff',
    [
        pytest.param('exponentail', id='typo'),
        pytest.param('EXPONENTIAL', id='wrong-case'),
        pytest.param('linear', id='plausible-but-absent'),
        pytest.param(None, id='none'),
        pytest.param(2, id='non-string'),
    ],
)
def test_get_retry_policy_refuses_a_backoff_it_does_not_implement(
    backoff: Any,
) -> None:
    """R28: an unrecognised ``backoff`` is refused, not defaulted.

    The failure this replaces is silent: falling through to the
    ``constant`` arm would give a caller who asked for ``'exponentail'``
    a *working* retry loop with the wrong spacing, and nothing would ever
    say so. The message names the key and lists what is accepted, because
    a rejection that does not say what *was* acceptable leaves the caller
    guessing at the spelling -- which is how the typo was written.
    """
    with pytest.raises(ConfigurationError) as refusal:
        get_retry_policy('r', allowed_retries=2, backoff=backoff)

    assert 'retry_config["backoff"]' in str(refusal.value)
    assert repr(backoff) in str(refusal.value)
    for name in BACKOFF_NAMES:
        assert name in str(refusal.value)


async def test_a_breaker_without_a_retry_policy_never_waits() -> None:
    """R28, the observable half: no policy means no backoff wait.

    A breaker configured without ``retry_config`` fails on the first
    attempt and sleeps for nothing. This is the property a consumer can
    see; the guard that implements it is asserted directly below,
    because ``run`` cannot reach it.
    """
    sleeper = RecordingSleep()
    subject = breaker(maximum_failures=99, sleep=sleeper)

    with pytest.raises(RetriesExhausted):
        await subject.run(failing(Boom('once')))

    assert sleeper.waits == []


def test_the_backoff_wait_is_zero_when_no_retry_policy_exists() -> None:
    """R28: ``_backoff_for``'s guard arm, called directly.

    Deliberately a private-method test, which the rest of this module
    avoids. ``run`` cannot reach this line: with no policy,
    ``_should_retry`` is False and the loop raises ``RetriesExhausted``
    two statements earlier, so the guard is unreachable through the
    public surface -- as the test above demonstrates by observing the
    empty wait list rather than the return value.

    It is still worth having rather than pragma-ing away. The guard is
    what makes ``_backoff_for`` total: the day a caller-facing change
    lets a retry-less breaker reach the sleep, the alternative is an
    ``AttributeError`` on ``None`` raised from inside the retry loop,
    which is the least legible place in this module for one.
    """
    subject = breaker(maximum_failures=99)

    assert subject.retry_policy is None
    assert subject._backoff_for(1) == 0.0

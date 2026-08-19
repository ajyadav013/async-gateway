"""The circuit-breaker facade: the state machine and the retry loop.

This library's whole timing seam lives here. ``clock`` and ``sleep`` are
constructor arguments with real defaults (``time.monotonic`` and
``asyncio.sleep``), so a consumer never sees them and a test can drive
half-open eligibility and backoff spacing without waiting on a real clock
and without monkeypatching a module global.

**Why the facade owns so much** (orchestrator Ruling D). ``pyfailsafe``
0.6.0 has no such seam -- it hardcodes ``time.monotonic()`` at
``circuit_breaker.py:139,142`` and ``await asyncio.sleep(...)`` at
``failsafe.py:103`` -- and neither reading is separable from the code
around it. Counting and opening are one operation: ``record_failure``
increments and trips on consecutive lines, and ``open()`` immediately
stamps ``opened_at`` from the library's own clock, so a delegated count
engages that clock and defeats the injected one. The callbacks and the
wait are likewise inseparable from the loop: they are configured on
``RetryPolicy`` but invoked by ``Failsafe.run``, on either side of the
hardcoded sleep. So this module owns the three states and every
transition, the failure count, ``record_success``/``record_failure``, the
retry loop, the abort branch, the three callback invocations and the
backoff wait.

What is still delegated is exactly two pure functions' worth of
``pyfailsafe``: ``RetryPolicy.should_abort`` / ``_is_retriable_exception``
for exception classification, and ``Backoff.for_attempt`` for the backoff
computation. That narrow residual is R7's recorded cost, and it is what
makes this facade's interface identical whether R7 later keeps, replaces
or vendors what sits underneath.

Callers never construct a breaker here. They look one up per destination
through :mod:`async_gateway.helpers.internal.breaker_registry`, which is
what lets failures accumulate across ``request()`` calls (H8) without one
flaky host opening the circuit for every other (M16).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from enum import Enum
from typing import Any, Final, Optional

import aioftp

import aiohttp

import asyncssh

from failsafe import (Backoff, CircuitOpen, Delay, RetriesExhausted,
                      RetryPolicy)

from async_gateway.utils.constants import (CIRCUIT_BREAKER_BACKOFF,
                                           CIRCUIT_BREAKER_DELAY,
                                           CIRCUIT_BREAKER_JITTER,
                                           CIRCUIT_BREAKER_MAX_DELAY,
                                           CIRCUIT_BREAKER_RETRY,
                                           CIRCUIT_BREAKER_TIMEOUT)
from async_gateway.utils.exceptions import (AsyncGatewayError,
                                            ConfigurationError,
                                            LocalWriteError,
                                            PathContainmentError,
                                            ResponseTooLargeError)

#: A reading of the injected clock, in seconds. Monotonic by contract:
#: the facade only ever subtracts one reading from another.
Clock = Callable[[], float]

#: The injected wait. Returns an awaitable, so a test's recording double
#: can be a plain coroutine function that returns immediately.
Sleep = Callable[[float], Awaitable[None]]

#: What a retried call looks like to :meth:`CircuitBreakerHelper.run`.
Call = Callable[..., Awaitable[Any]]

#: The failures a retry is *for*: a transport that did not answer, or
#: answered with something the next attempt might not see.
#:
#: ``asyncio.TimeoutError`` is listed explicitly, and separately from
#: ``OSError``, because on Python 3.10 -- inside this project's declared
#: ``requires-python = ">=3.10"`` -- it is a distinct class:
#: ``asyncio.TimeoutError is TimeoutError`` is False there and it is not
#: an ``OSError`` subclass. On 3.11+ the two were merged and both hold.
#: Leaving it implicit therefore works everywhere except the interpreter
#: floor, where ``aiohttp``'s total-deadline timeout would escape this
#: loop un-retried *and uncounted* -- so a destination that times out on
#: every call could never open its circuit, silently, on exactly one
#: supported interpreter.
RETRIABLE_FAILURES: Final[tuple[type[BaseException], ...]] = (
    AsyncGatewayError,
    asyncio.TimeoutError,
    OSError,
    aiohttp.ClientError,
    asyncssh.Error,
    aioftp.AIOFTPException,
)

#: The failures that stop the loop dead and propagate unchanged. Every
#: one is this library refusing the call -- a body over the cap, a verb
#: outside the allowlist, a redirect hop to a scheme the caller did not
#: allow. Retrying them cannot help, and *counting* them is worse than
#: useless: one caller passing a bad verb would drive the destination's
#: circuit open for every other caller in the process.
#:
#: They also propagate as *themselves*. Wrapped in a ``RetriesExhausted``
#: -- whose own ``str()`` is ``''`` -- a refusal reaches the caller as a
#: blank message where a named reason belongs.
#:
#: ``aioftp.errors.InvalidCommand`` is the one entry raised by a
#: dependency rather than by this library, and it is here for the same
#: reason the other two are: it is ``aioftp`` refusing a CR or an LF in a
#: value *the caller supplied*, so no retry of it can ever succeed. It
#: reaches this set rather than being classified only at
#: ``logic.ftp_client.transport_error_for`` because that function runs
#: **outside** the loop -- by the time it maps the exception to a
#: ``ConfigurationError`` the retries have already been spent and the
#: failure already counted. Measured, not assumed: without this entry a
#: caller repeating one CR-bearing ``server_path`` five times drove the
#: destination's circuit to OPEN, so the sixth call -- and every other
#: caller's call to that host -- got ``CIRCUIT_OPEN`` for a typo (N7).
#:
#: The two **local-filesystem** entries are NEW-R10-1, and they are the
#: same argument reaching a surface nobody had applied it to. A download
#: writes through ``utils.paths.safe_writer``, which runs *inside* the
#: retried callable, so a missing parent directory or a symlink planted
#: at the destination was a failure the loop treated as the network's:
#: measured at **4x amplification** -- the entire body re-downloaded once
#: per attempt for a fault no remote can heal -- and at **breaker
#: poisoning**, where six local disk failures opened the destination's
#: circuit and the next healthy call to a healthy server got
#: ``CIRCUIT_OPEN``. Neither is evidence about the remote side, which is
#: the whole of what a breaker exists to measure.
DEFAULT_ABORTABLE_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    ConfigurationError,
    ResponseTooLargeError,
    LocalWriteError,
    PathContainmentError,
    aioftp.errors.InvalidCommand,
)

#: Backoff shapes a caller may name. Replaces the magic
#: ``name == 'backoff'`` string, which the README documented as "Any
#: name" -- so a caller following the docs silently got a constant
#: zero-second delay with their ``max_delay`` and ``jitter`` ignored
#: (M14).
BACKOFF_NAMES: Final[frozenset[str]] = frozenset({'constant', 'exponential'})

#: Keys :func:`validated_retry_config` accepts. Anything else is a typo,
#: and is named in the error rather than silently defaulted (M15).
RETRY_CONFIG_KEYS: Final[frozenset[str]] = frozenset({
    'name',
    'allowed_retries',
    'retriable_exceptions',
    'abortable_exceptions',
    'on_retries_exhausted',
    'on_failed_attempt',
    'on_abort',
    'delay',
    'max_delay',
    'jitter',
    'backoff',
})

#: Keys :func:`validated_breaker_config` accepts, for the same reason.
#: ``clock`` and ``sleep`` are here because the seam is part of the
#: interface, not a test-only back door.
BREAKER_CONFIG_KEYS: Final[frozenset[str]] = frozenset({
    'maximum_failures',
    'timeout',
    'retry_config',
    'clock',
    'sleep',
})


class BreakerState(Enum):
    """Which of the three states a destination's breaker is in.

    Attributes:
        CLOSED: Calls pass through and failures are counted.
        OPEN: Calls are refused until ``reset_timeout_seconds`` has
            elapsed on the injected clock.
        HALF_OPEN: Exactly one trial call is admitted. Its success closes
            the circuit; its failure re-opens it.
    """

    CLOSED = 'closed'
    OPEN = 'open'
    HALF_OPEN = 'half_open'


def is_number(value: object) -> bool:
    """Report whether ``value`` is a real number this config accepts.

    ``bool`` is excluded explicitly. It is an ``int`` subclass, so the
    plain ``isinstance(x, int)`` this replaces accepted ``True`` as the
    number 1 -- and *rejected* ``1.5``, which is a perfectly good number
    of seconds (L10).

    Args:
        value: The candidate setting.

    Returns:
        True for an ``int`` or ``float`` that is not a ``bool``.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validated_number(
    config: Mapping[str, Any],
    key: str,
    default: float,
    *,
    where: str = 'circuit_breaker_config',
    minimum: float = 0.0,
) -> float:
    """Return one numeric setting, or its default when the key is absent.

    Absence is what selects the default -- not falsiness. The expression
    this replaces was ``config.get(key) or default``, under which
    ``maximum_failures=0`` was unreachable: the caller's explicit 0 is
    falsy and was silently replaced by 5 (L9).

    Args:
        config: The caller's configuration mapping.
        key: The setting to read.
        default: What to use when the key is absent.
        where: How to name the enclosing structure in an error message.
        minimum: The smallest legal value.

    Returns:
        The setting as a float.

    Raises:
        ConfigurationError: If the value is not a non-bool number, or is
            below ``minimum``. Both name the key, so the caller is told
            which setting they got wrong.
    """
    if key not in config:
        return default
    value = config[key]
    if not is_number(value):
        raise ConfigurationError(
            f'{where}[{key!r}] must be a number, got '
            f'{type(value).__name__}')
    if value < minimum:
        raise ConfigurationError(
            f'{where}[{key!r}] must be >= {minimum}, got {value!r}')
    return float(value)


def _validated_bool(
    config: Mapping[str, Any],
    key: str,
    default: bool,
) -> bool:
    """Return one boolean setting, or its default when absent.

    Args:
        config: The caller's retry configuration.
        key: The setting to read.
        default: What to use when the key is absent.

    Returns:
        The setting.

    Raises:
        ConfigurationError: If the value is not a ``bool``. ``1`` is not
            accepted as ``True``: a caller who wrote a number where a
            flag belongs has made a mistake worth reporting.
    """
    if key not in config:
        return default
    value = config[key]
    if not isinstance(value, bool):
        raise ConfigurationError(
            f'retry_config[{key!r}] must be a bool, got '
            f'{type(value).__name__}')
    return value


def _reject_unknown_keys(
    config: Mapping[str, Any],
    allowed: frozenset[str],
    where: str,
) -> None:
    """Reject any key outside ``allowed``, naming it.

    The whole of M15: the configuration used to be an untyped ``**kwargs``
    bag read with ``.get()``, so ``max_failures`` (for
    ``maximum_failures``) and ``reset_timeout`` (for ``timeout``) were
    discarded in silence and the caller ran on defaults they never chose.

    Args:
        config: The caller's configuration mapping.
        allowed: The key names this structure accepts.
        where: How to name the structure in the error message.

    Returns:
        None.

    Raises:
        ConfigurationError: If any key is not in ``allowed``. The message
            names the unknown keys and lists what is accepted.
    """
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ConfigurationError(
            f'{where} has unknown key(s) {unknown}; accepted keys are '
            f'{sorted(allowed)}')


def _validated_exceptions(
    config: Mapping[str, Any],
    key: str,
) -> Optional[tuple[type[BaseException], ...]]:
    """Return an exception-class list setting, or None when absent.

    Args:
        config: The caller's retry configuration.
        key: ``retriable_exceptions`` or ``abortable_exceptions``.

    Returns:
        The classes as a tuple, or None when the caller named none.

    Raises:
        ConfigurationError: If the value is not a sequence, or holds
            anything that is not an exception class. Passing an
            *instance* is the ordinary mistake and is named as such.
    """
    if config.get(key) is None:
        return None
    value = config[key]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ConfigurationError(
            f'retry_config[{key!r}] must be a sequence of exception '
            f'classes, got {type(value).__name__}')
    for item in value:
        if not (isinstance(item, type) and issubclass(item, BaseException)):
            raise ConfigurationError(
                f'retry_config[{key!r}] must hold exception classes, got '
                f'{item!r}')
    return tuple(value)


def _validated_callable(
    config: Mapping[str, Any],
    key: str,
    where: str,
) -> Optional[Callable[..., Any]]:
    """Return a callback setting, or None when absent.

    Args:
        config: The caller's configuration mapping.
        key: The callback name.
        where: How to name the enclosing structure in an error message.

    Returns:
        The callback, or None when the caller named none.

    Raises:
        ConfigurationError: If the value is not callable.
    """
    if config.get(key) is None:
        return None
    value = config[key]
    if not callable(value):
        raise ConfigurationError(
            f'{where}[{key!r}] must be callable, got '
            f'{type(value).__name__}')
    return value


def get_retry_policy(
    name: Optional[str] = None,
    *,
    allowed_retries: int,
    delay: float = CIRCUIT_BREAKER_DELAY,
    max_delay: float = CIRCUIT_BREAKER_MAX_DELAY,
    jitter: bool = CIRCUIT_BREAKER_JITTER,
    backoff: str = CIRCUIT_BREAKER_BACKOFF,
    retriable_exceptions: Optional[Sequence[type[BaseException]]] = None,
    abortable_exceptions: Optional[Sequence[type[BaseException]]] = None,
) -> RetryPolicy:
    """Build the delegated classification-and-backoff object.

    This is the whole of what the facade still asks ``pyfailsafe`` for:
    ``should_abort`` and ``_is_retriable_exception`` to classify an
    exception, and ``backoff.for_attempt`` to compute one wait. The three
    callbacks are deliberately *not* configured on it -- the facade
    invokes them itself, because ``Failsafe.run``, the only thing that
    would otherwise invoke them, is the loop this facade replaced.

    Args:
        name: The caller's free-text label. Kept because the README
            documents it as "Any name" and callers pass it; it no longer
            *selects* anything. The shape is chosen by ``backoff`` (M14).
        allowed_retries: How many retries past the first attempt. 0 means
            a single attempt.
        delay: Base seconds between attempts.
        max_delay: Ceiling on one wait, in seconds.
        jitter: Whether to randomise each wait.
        backoff: ``'exponential'`` -- each wait doubles, clamped to
            ``max_delay`` -- or ``'constant'``.
        retriable_exceptions: Classes a retry is attempted for, or None
            to retry everything the facade caught.
        abortable_exceptions: Classes that abort the loop immediately, or
            None to rely on :data:`DEFAULT_ABORTABLE_EXCEPTIONS` alone.

    Returns:
        A ``RetryPolicy`` carrying the classification lists and the
        backoff computation.

    Raises:
        ConfigurationError: If ``backoff`` is not one of the two names.
    """
    if backoff not in BACKOFF_NAMES:
        raise ConfigurationError(
            f'retry_config["backoff"] must be one of '
            f'{sorted(BACKOFF_NAMES)}, got {backoff!r}')

    computation: Backoff
    if backoff == 'exponential':
        computation = Backoff(
            delay=timedelta(seconds=delay),
            max_delay=timedelta(seconds=max_delay),
            jitter=jitter)
    else:
        # `Delay` is `Backoff` with factor 1 and no jitter, which is
        # exactly what "constant" means.
        computation = Delay(timedelta(seconds=delay))

    return RetryPolicy(
        allowed_retries=allowed_retries,
        backoff=computation,
        retriable_exceptions=(
            list(retriable_exceptions)
            if retriable_exceptions is not None else None),
        abortable_exceptions=(
            list(abortable_exceptions)
            if abortable_exceptions is not None else None),
    )


def validated_retry_config(retry_config: Mapping[str, Any]) -> dict[str, Any]:
    """Return one caller ``retry_config`` once every key is known good.

    Args:
        retry_config: The caller's ``retry_config`` mapping.

    Returns:
        A new dict of validated settings: the ``get_retry_policy``
        arguments plus the three callbacks. The caller's own mapping is
        never written to (M12).

    Raises:
        ConfigurationError: If a key is unknown, ``allowed_retries`` is
            absent or not a non-negative whole number, a numeric setting
            is not a non-bool number, ``jitter`` is not a bool,
            ``backoff`` is not a known name, an exception list holds a
            non-class, or a callback is not callable.
    """
    if not isinstance(retry_config, Mapping):
        raise ConfigurationError(
            f'circuit_breaker_config["retry_config"] must be a mapping, '
            f'got {type(retry_config).__name__}')
    _reject_unknown_keys(retry_config, RETRY_CONFIG_KEYS, 'retry_config')

    if 'allowed_retries' not in retry_config:
        raise ConfigurationError('retry_config["allowed_retries"] is required')
    retries = retry_config['allowed_retries']
    if not is_number(retries) or retries != int(retries) or retries < 0:
        raise ConfigurationError(
            f'retry_config["allowed_retries"] must be a non-negative whole '
            f'number, got {retries!r}')

    name = retry_config.get('name')
    if name is not None and not isinstance(name, str):
        raise ConfigurationError(
            f'retry_config["name"] must be a string, got '
            f'{type(name).__name__}')

    backoff = retry_config.get('backoff', CIRCUIT_BREAKER_BACKOFF)
    if backoff not in BACKOFF_NAMES:
        raise ConfigurationError(
            f'retry_config["backoff"] must be one of '
            f'{sorted(BACKOFF_NAMES)}, got {backoff!r}')

    return {
        'name': name,
        'allowed_retries': int(retries),
        'delay': _validated_number(
            retry_config, 'delay', CIRCUIT_BREAKER_DELAY,
            where='retry_config'),
        'max_delay': _validated_number(
            retry_config, 'max_delay', CIRCUIT_BREAKER_MAX_DELAY,
            where='retry_config'),
        'jitter': _validated_bool(
            retry_config, 'jitter', CIRCUIT_BREAKER_JITTER),
        'backoff': backoff,
        'retriable_exceptions': _validated_exceptions(
            retry_config, 'retriable_exceptions'),
        'abortable_exceptions': _validated_exceptions(
            retry_config, 'abortable_exceptions'),
        'on_retries_exhausted': _validated_callable(
            retry_config, 'on_retries_exhausted', 'retry_config'),
        'on_failed_attempt': _validated_callable(
            retry_config, 'on_failed_attempt', 'retry_config'),
        'on_abort': _validated_callable(
            retry_config, 'on_abort', 'retry_config'),
    }


def validated_breaker_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return one caller ``circuit_breaker_config``, fully validated.

    Called on **every** request, not only when the registry misses. A
    validation that ran on the miss alone would raise
    ``ConfigurationError`` for ``{'max_failures': 3}`` -- R24's own named
    typo example -- on the first call to a destination and accept the
    very same typo in silence on every call after it. A contract that
    depends on cache state is not a contract.

    Args:
        config: The caller's ``circuit_breaker_config`` mapping.

    Returns:
        A new dict of keyword arguments for
        :class:`CircuitBreakerHelper`. The caller's mapping is not
        modified (M12).

    Raises:
        ConfigurationError: For an unknown key or any invalid setting;
            the message names the key.
    """
    if not isinstance(config, Mapping):
        raise ConfigurationError(
            f'circuit_breaker_config must be a mapping, got '
            f'{type(config).__name__}')
    _reject_unknown_keys(
        config, BREAKER_CONFIG_KEYS, 'circuit_breaker_config')

    failures = _validated_number(
        config, 'maximum_failures', float(CIRCUIT_BREAKER_RETRY))
    if failures != int(failures):
        raise ConfigurationError(
            f'circuit_breaker_config["maximum_failures"] must be a whole '
            f'number, got {config["maximum_failures"]!r}')

    settings: dict[str, Any] = {
        'maximum_failures': int(failures),
        'reset_timeout_seconds': _validated_number(
            config, 'timeout', CIRCUIT_BREAKER_TIMEOUT),
        'retry_policy': None,
        'on_retries_exhausted': None,
        'on_failed_attempt': None,
        'on_abort': None,
    }

    for seam in ('clock', 'sleep'):
        injected = _validated_callable(config, seam, 'circuit_breaker_config')
        if injected is not None:
            settings[seam] = injected

    if config.get('retry_config') is not None:
        retry = validated_retry_config(config['retry_config'])
        settings['on_retries_exhausted'] = retry.pop('on_retries_exhausted')
        settings['on_failed_attempt'] = retry.pop('on_failed_attempt')
        settings['on_abort'] = retry.pop('on_abort')
        settings['retry_policy'] = get_retry_policy(retry.pop('name'), **retry)

    return settings


class CircuitBreakerHelper:
    """One destination's breaker: its state machine and its retry loop.

    Constructed once per destination by
    :func:`async_gateway.helpers.internal.breaker_registry.get_breaker`
    and reused for the life of the process, which is what lets failures
    accumulate at all (H8). Never constructed per request.

    Attributes:
        maximum_failures: Consecutive failures that open the circuit. 0
            is legal and reachable (L9): it opens on the first failure.
        reset_timeout_seconds: Seconds an open circuit stays open.
        retry_policy: The delegated classifier and backoff computation.
        clock: The injected monotonic clock.
        sleep: The injected wait.
        state: Which of :class:`BreakerState` the breaker is in.
        failures: Consecutive failures counted since the last success.
    """

    def __init__(
        self,
        *,
        maximum_failures: int = CIRCUIT_BREAKER_RETRY,
        reset_timeout_seconds: float = CIRCUIT_BREAKER_TIMEOUT,
        retry_policy: Optional[RetryPolicy] = None,
        on_retries_exhausted: Optional[Callable[..., Any]] = None,
        on_failed_attempt: Optional[Callable[..., Any]] = None,
        on_abort: Optional[Callable[..., Any]] = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Build a breaker for one destination.

        Args:
            maximum_failures: Consecutive failures that open the circuit.
            reset_timeout_seconds: Seconds an open circuit stays open
                before one half-open trial is admitted.
            retry_policy: The classification-and-backoff object from
                :func:`get_retry_policy`, or None for no retries.
            on_retries_exhausted: Invoked once when the loop gives up.
            on_failed_attempt: Invoked after each counted failed attempt.
            on_abort: Invoked when an abortable exception ends the loop.
            clock: Reads monotonic seconds. The default is the real
                ``time.monotonic``; a test substitutes an advanceable
                counter. Every duration this class measures reads it.
            sleep: Awaits a number of seconds. The default is the real
                ``asyncio.sleep``; a test substitutes a recorder that
                returns immediately. Every wait this class performs calls
                it.
        """
        self.maximum_failures = maximum_failures
        self.reset_timeout_seconds = reset_timeout_seconds
        self.retry_policy = retry_policy
        self.on_retries_exhausted = on_retries_exhausted
        self.on_failed_attempt = on_failed_attempt
        self.on_abort = on_abort
        self.clock = clock
        self.sleep = sleep

        self.state: BreakerState = BreakerState.CLOSED
        self.failures: int = 0
        self._opened_at: float = 0.0
        # Whether the one half-open trial is currently taken. Claimed
        # before the call is awaited, not merely observed -- see
        # `allows_execution`.
        self._trial_taken: bool = False

    def allows_execution(self) -> bool:
        """Report whether a call may proceed, claiming the trial if so.

        Not a pure predicate, deliberately. Reading the state and *then*
        awaiting the call is a check-then-act race: fifty coroutines
        entering an open circuit whose reset has just elapsed each read
        HALF_OPEN and each reach the transport, so a destination meant to
        receive **one** trial receives fifty -- which is the whole point
        of half-open lost. The trial is therefore claimed here, in the
        same synchronous step that observes the state, and released in
        :meth:`run`'s ``finally``.

        Returns:
            True when the call may proceed. An OPEN circuit whose reset
            timeout has elapsed on the injected clock transitions to
            HALF_OPEN first.
        """
        if self.state is BreakerState.CLOSED:
            return True

        if self.state is BreakerState.OPEN:
            if self.clock() - self._opened_at < self.reset_timeout_seconds:
                return False
            self.state = BreakerState.HALF_OPEN
            self._trial_taken = False

        if self._trial_taken:
            return False
        self._trial_taken = True
        return True

    def record_success(self) -> None:
        """Close the circuit and forget the failure count.

        Returns:
            None.
        """
        self.state = BreakerState.CLOSED
        self.failures = 0

    def record_failure(self) -> None:
        """Count one failure, and open the circuit if that is enough.

        A failure during the half-open trial re-opens immediately,
        whatever the count says: the trial existed to ask one question
        and it was answered no.

        Returns:
            None.
        """
        if self.state is BreakerState.HALF_OPEN:
            self._open()
            return
        self.failures += 1
        if self.failures >= self.maximum_failures:
            self._open()

    def _open(self) -> None:
        """Open the circuit, stamping the injected clock.

        Returns:
            None.
        """
        self.state = BreakerState.OPEN
        self._opened_at = self.clock()
        self.failures = 0

    def _should_abort(self, error: BaseException) -> bool:
        """Report whether ``error`` must end the loop without counting.

        This library's own refusals are checked **first**, and above any
        policy guard. A caller with a breaker config but no
        ``retry_config`` has no policy at all, so a check that lived
        behind ``if self.retry_policy is not None`` never ran for them: a
        body over the cap, a disallowed verb or a hostile redirect hop
        was counted as a destination failure, and one caller with a bad
        verb took that destination offline for every other caller in the
        process.

        Args:
            error: What the attempt raised.

        Returns:
            True when the loop must stop and re-raise ``error`` as
            itself.
        """
        if isinstance(error, DEFAULT_ABORTABLE_EXCEPTIONS):
            return True
        if self.retry_policy is None:
            return False
        return bool(self.retry_policy.should_abort(error))

    def _should_retry(self, attempts: int, error: BaseException) -> bool:
        """Report whether another attempt is allowed for ``error``.

        Args:
            attempts: How many attempts have already run.
            error: What the last one raised.

        Returns:
            True when the budget allows another attempt and the policy
            classifies the exception as retriable.
        """
        policy = self.retry_policy
        if policy is None or attempts > policy.allowed_retries:
            return False
        # The delegated classifier. Named with a leading underscore by
        # `pyfailsafe`, and the only entry point to it: the public
        # `should_retry` also computes the backoff and takes a `Context`
        # this facade does not build.
        return bool(policy._is_retriable_exception(error))

    def _backoff_for(self, attempts: int) -> float:
        """Return the seconds to wait before attempt ``attempts + 1``.

        The one arithmetic still delegated: the exponential factor, the
        jitter and the ``max_delay`` clamp.

        Args:
            attempts: How many attempts have already run, at least 1.

        Returns:
            The wait in seconds, 0 when no policy is configured.
        """
        if self.retry_policy is None:
            return 0.0
        return float(self.retry_policy.backoff.for_attempt(attempts))

    @staticmethod
    def _invoke(callback: Optional[Callable[..., Any]]) -> None:
        """Invoke one caller callback, letting its failures propagate.

        ``pyfailsafe`` wraps these in a blanket handler that logs and
        swallows. This package bans that pattern outright: a callback
        that raises is the caller's bug and must reach them, not be
        buried in a log line underneath a failing request.

        Args:
            callback: The callback, or None when none was configured.

        Returns:
            None.
        """
        if callback is not None:
            callback()

    async def run(self, call: Call, *args: Any, **kwargs: Any) -> Any:
        """Run ``call`` under this destination's breaker and retry policy.

        Args:
            call: The coroutine function to attempt. Re-invoked per
                retry, so anything it consumes must be rebuilt inside it.
            *args: Positional arguments forwarded to ``call``.
            **kwargs: Keyword arguments forwarded to ``call``.

        Returns:
            Whatever ``call`` returned on the attempt that succeeded.

        Raises:
            CircuitOpen: When the circuit is open, or is half-open with
                its one trial already claimed. Chained from the most
                recent failure when this loop has seen one.
            RetriesExhausted: When every allowed attempt failed. Chained
                from the last failure, whose message
                :func:`~async_gateway.utils.exceptions.unwrap_cause`
                recovers -- ``str(RetriesExhausted())`` is ``''``.
            BaseException: An abortable exception, and anything outside
                :data:`RETRIABLE_FAILURES`, propagate as themselves. A
                refusal wrapped in ``RetriesExhausted`` would reach the
                caller as a blank message; a ``KeyError`` from this
                library is a bug and must not become a transport failure.
        """
        attempts = 0
        recent: Optional[BaseException] = None

        while True:
            if not self.allows_execution():
                if recent is None:
                    raise CircuitOpen()
                raise CircuitOpen() from recent

            trialling = self.state is BreakerState.HALF_OPEN
            try:
                attempts += 1
                try:
                    result = await call(*args, **kwargs)
                except RETRIABLE_FAILURES as error:
                    if self._should_abort(error):
                        self._invoke(self.on_abort)
                        raise
                    recent = error
                    self.record_failure()
                    self._invoke(self.on_failed_attempt)
                    if not self._should_retry(attempts, error):
                        self._invoke(self.on_retries_exhausted)
                        raise RetriesExhausted() from recent
                    await self.sleep(self._backoff_for(attempts))
                else:
                    self.record_success()
                    return result
            finally:
                # Released here, and not from `record_success` /
                # `record_failure`: a `finally` makes the claim's
                # lifetime exactly one attempt on *every* route out --
                # success, counted failure, abort, cancellation -- where
                # releasing it from a recorder leaves it held forever on
                # the routes that reach neither.
                if trialling:
                    self._trial_taken = False

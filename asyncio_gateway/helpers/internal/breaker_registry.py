"""The process-lifetime, per-destination circuit-breaker registry.

The only genuinely cross-request state this library holds, and it has to
be: a breaker constructed per request has a zeroed failure count against
its threshold on every call and can therefore never open (H8). Hoisting
it to a single module-global breaker would fix that and introduce
something worse -- one flaky host opening the circuit for *every*
destination in the process -- so the two fixes are one fix, and this
module is it (M16, FI-4).

Breakers are keyed ``(family, host, port)`` and held in a bounded LRU.
Eviction is by least-recent **use**, not by insertion order, so a
destination under active failure -- the one whose accumulated state
matters most -- is never the one evicted.

**The seam is not part of the key.** ``clock`` and ``sleep`` are
keyword-only with real defaults, take precedence over the same keys in
``config``, and are excluded from the lookup: substituting a fake clock
in a test must not create a second breaker for the same destination.
:func:`reset` clears the registry between tests so a fake clock cannot
leak into the next test's state.

**Thread and loop safety.** The library is single-event-loop by
assumption and this registry takes no lock. A consumer running several
loops in one process shares breaker state across them, which is intended:
a destination's health is a process-level fact, not a per-loop one.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, Final, Optional

from asyncio_gateway.helpers.internal.circuit_breaker_helper import (
    CircuitBreakerHelper, Clock, Sleep, validated_breaker_config)
from asyncio_gateway.utils.constants import BREAKER_REGISTRY_MAX

#: What one destination is keyed by: the protocol family, the host, and
#: the port -- which is why ``https://a`` and ``https://b`` cannot share
#: a breaker, and why one host on two ports does not either.
BreakerKey = tuple[str, str, int]

#: The live registry. An ``OrderedDict`` rather than a plain dict because
#: LRU needs the ordering operation a dict does not offer:
#: ``move_to_end`` on every hit is what makes eviction by least-recent
#: *use* rather than by insertion.
_BREAKERS: Final['OrderedDict[BreakerKey, CircuitBreakerHelper]'] = (
    OrderedDict())


def get_breaker(
    family: str,
    host: str,
    port: int,
    config: Optional[Mapping[str, Any]] = None,
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    maximum: int = BREAKER_REGISTRY_MAX,
) -> CircuitBreakerHelper:
    """Return the breaker for one destination, creating it if needed.

    The caller's configuration is validated on **every** call, before the
    registry is consulted, and the parsed result is then discarded on a
    hit. Two things have to be true at once and only this ordering gives
    both. An invalid config must be rejected the same way on the tenth
    call as on the first -- validating on the miss alone made
    ``{'max_failures': 3}`` raise once and pass silently forever after --
    and a hit must keep the *first* caller's settings, because rebuilding
    the breaker would discard the very failure history it exists to
    accumulate.

    Args:
        family: The protocol family, lower-cased: ``http``, ``https``,
            ``ftp``, ``sftp``.
        host: The host name or address, lower-cased by
            :func:`~asyncio_gateway.helpers.internal.base.destination_of`.
        port: The port, or
            :data:`~asyncio_gateway.utils.constants.UNKNOWN_PORT` when the
            scheme has no default.
        config: The caller's ``circuit_breaker_config``, or None for the
            documented "no configuration" call.
        clock: The monotonic clock the breaker reads, defaulting to the
            real one. Wins over ``config['clock']`` when both are given.
        sleep: The wait the breaker performs, defaulting to the real one.
            Wins over ``config['sleep']`` for the same reason.
        maximum: How many destinations to hold before evicting the least
            recently used. Parameterised so the bound itself is testable
            without allocating the production cap's worth of breakers.

    Returns:
        The breaker for this destination -- the same object on every call
        for the same key, which is the property H8 turns on.

    Raises:
        ConfigurationError: If the caller's configuration has an unknown
            key or an invalid setting, on every call and not merely the
            first.
    """
    settings = validated_breaker_config({} if config is None else config)
    # Keyword arguments win over the same keys in `config`: an explicit
    # argument at the call site is the more specific statement of intent.
    settings['clock'] = clock
    settings['sleep'] = sleep

    key: BreakerKey = (family, host, port)
    existing = _BREAKERS.get(key)
    if existing is not None:
        # A hit is a *use*, so it moves to the most-recent end -- the
        # difference between LRU and plain insertion-order eviction.
        _BREAKERS.move_to_end(key)
        return existing

    breaker = CircuitBreakerHelper(**settings)
    _BREAKERS[key] = breaker
    while len(_BREAKERS) > maximum:
        _BREAKERS.popitem(last=False)
    return breaker


def registry_size() -> int:
    """Return how many destinations currently hold a breaker.

    Returns:
        The number of live entries, which the LRU bound caps.
    """
    return len(_BREAKERS)


def reset() -> None:
    """Discard every breaker. Test-only.

    Nothing in the package calls this. It exists so one test's fake clock
    and accumulated failure count cannot leak into the next test through
    process-lifetime state, which is the one hazard a deliberately
    long-lived registry introduces.

    Returns:
        None.
    """
    _BREAKERS.clear()

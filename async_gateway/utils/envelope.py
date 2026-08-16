"""The single response shape every protocol returns, and its builders.

``request()`` seeds one envelope with :func:`new_envelope`, each protocol
*fills that same object*, and exactly one of :func:`finalise_ok` or
:func:`finalise_error` closes it. Nothing else anywhere constructs a
response shape. That is what makes the key set invariant across all five
protocols and across the success and failure paths (invariant E1), and it
is the fix for a seam at which one protocol used to build a fresh dict, one
mutated the caller's, and two returned ``True``.

Where redaction happens, and why here. The URL is masked when the envelope
is built, because nothing downstream reads it from the envelope. The
payload echo is masked when the envelope is *closed*, because the protocol
dispatches the payload the entry point put in the envelope -- masking it at
construction would put the sentinel on the wire. Either way the returned
envelope carries no credential value (invariant E9), which is what the
caller logs and stores.
"""

from collections.abc import Collection
from typing import Any, Optional, TypedDict

from async_gateway.helpers.common.date_helper import (
    elapsed_since,
    utc_now_iso,
)
from async_gateway.utils.exceptions import AsyncGatewayError, unwrap_cause
from async_gateway.utils.redaction import redact_payload, redact_url

# A parsed JSON body is an object, an array, or absent. An empty body and a
# body that was literally `{}` stay distinguishable.
JsonBody = Optional[dict[str, Any] | list[Any]]

# No status yet. Every finalised envelope replaces it, so it is visible only
# on an envelope still being filled -- never on one that was returned.
STATUS_UNSET = 0


class GatewayError(TypedDict):
    """The structured error a failed envelope carries.

    Attributes:
        type: The exception class name, e.g. ``'GatewayTimeoutError'``.
        code: The wire-stable machine-readable code, e.g. ``'TIMEOUT'``.
        message: Human-readable, and never empty when ``ok`` is False.
        cause: The unwrapped cause chain's deepest type and message, or
            None when the chain adds nothing.
    """

    type: str
    code: str
    message: str
    cause: Optional[str]


class GatewayResponse(TypedDict):
    """What ``request()`` returns, for every protocol, on every path.

    Attributes:
        ok: The success predicate. The only correct check.
        status_code: Always populated; see ``utils/status_map.py``.
        protocol: The protocol the call was dispatched on.
        url: The request URL, redacted.
        request_time: ISO-8601, UTC, with an explicit offset.
        latency: Seconds, measured with a monotonic clock.
        payload: The request payload, key-name-redacted.
        text: The decoded body; ``''`` when not applicable, never an
            exception object.
        json: The parsed body, or None.
        headers: Response headers, credential values redacted; ``{}`` for
            protocols that have none.
        cookies: Response cookies, values redacted; ``{}`` for protocols
            that have none.
        error: The failure, or None exactly when ``ok`` is True.
        protocol_details: Per-protocol extras, so the top-level key set
            stays invariant.
        request_tracer: Per-request trace results; ``[]`` when tracing is
            off.
        pre_processor_response: What the caller's pre-processor returned.
        post_processor_response: What the caller's post-processor returned.
    """

    ok: bool
    status_code: int
    protocol: str
    url: str
    request_time: str
    latency: float
    payload: Any
    text: str
    json: JsonBody
    headers: dict[str, str]
    cookies: dict[str, str]
    error: Optional[GatewayError]
    protocol_details: dict[str, Any]
    request_tracer: list[dict[str, Any]]
    pre_processor_response: Any
    post_processor_response: Any


def new_envelope(
    *,
    url: str,
    protocol: str,
    payload: Any,
    redact_query_params: Collection[str] = (),
) -> GatewayResponse:
    """Build the envelope skeleton for one call.

    Args:
        url: The URL as the caller supplied it; stored redacted.
        protocol: The protocol name the call was dispatched on.
        payload: The request payload, stored as the protocol will send it
            and masked when the envelope is closed.
        redact_query_params: The caller's additional sensitive
            query-parameter names, normalised from
            ``protocol_info['redact_query_params']``. The same set must
            reach the failure log, or the two would disagree about what a
            secret is.

    Returns:
        A fresh envelope with every key present. ``ok`` starts False, so an
        envelope that somehow escaped without being finalised reads as a
        failure rather than as a success.
    """
    return GatewayResponse(
        ok=False,
        status_code=STATUS_UNSET,
        protocol=protocol,
        url=redact_url(url, extra_params=redact_query_params),
        request_time=utc_now_iso(),
        latency=0.0,
        payload=payload,
        text='',
        json=None,
        headers={},
        cookies={},
        error=None,
        protocol_details={},
        request_tracer=[],
        pre_processor_response=None,
        post_processor_response=None,
    )


def finalise_ok(
    env: GatewayResponse,
    *,
    status_code: int,
    started: float,
) -> GatewayResponse:
    """Close ``env`` as a success.

    Args:
        env: The envelope the protocol filled.
        status_code: The status the protocol reports for success.
        started: The monotonic reference point the call started from.

    Returns:
        The same object, with ``ok`` True and ``error`` None.
    """
    env['ok'] = True
    env['error'] = None
    return _seal(env, status_code=status_code, started=started)


def finalise_error(
    env: GatewayResponse,
    exc: AsyncGatewayError,
    *,
    started: float,
    redact_query_params: Collection[str] = (),
) -> GatewayResponse:
    """Close ``env`` as a failure, adding the error and flipping ``ok``.

    It *adds*: ``headers``, ``cookies``, ``text`` and ``json`` are left
    exactly as the protocol wrote them, and ``status_code`` comes from the
    exception -- which, for a remote-status failure, is the status the
    protocol already recorded before raising. That is invariant E11:
    ``ok=False`` never costs the caller the response body.

    The message is never empty. :func:`unwrap_cause` walks the cause chain
    for the deepest message there is and falls back to the exception's class
    name, which is invariant E3.

    Args:
        env: The envelope the protocol filled before raising.
        exc: The failure to report.
        started: The monotonic reference point the call started from.
        redact_query_params: The caller's additional sensitive
            query-parameter names -- the same normalised set ``url`` was
            built with. The cause chain reaches into exceptions this
            library did not raise, and their text lands in
            ``error['message']`` and ``error['cause']``; passing the set
            widens what :func:`unwrap_cause` masks there beyond the
            built-in names it applies regardless.

    Returns:
        The same object, with ``ok`` False and ``error`` populated.
    """
    message, cause = unwrap_cause(exc, redact_params=redact_query_params)
    env['ok'] = False
    env['error'] = GatewayError(
        type=type(exc).__name__,
        code=exc.code,
        message=message,
        cause=cause,
    )
    return _seal(env, status_code=exc.status_code, started=started)


def _seal(
    env: GatewayResponse,
    *,
    status_code: int,
    started: float,
) -> GatewayResponse:
    """Apply what closing an envelope does whatever the outcome.

    Args:
        env: The envelope being closed.
        status_code: The status to report.
        started: The monotonic reference point the call started from.

    Returns:
        The same object, with the status, the latency and the redacted
        payload echo in place.
    """
    env['status_code'] = status_code
    env['latency'] = elapsed_since(started)
    env['payload'] = redact_payload(env['payload'])
    return env

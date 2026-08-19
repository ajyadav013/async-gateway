"""The typed exception hierarchy every module below the entry point raises.

Protocol code raises; exactly one place converts. Everything under
``asyncio_gateway.py`` raises an :class:`AsyncGatewayError` subclass, and
``request()`` is the single place that turns one into an ``ok=False``
envelope. Anything that is *not* an ``AsyncGatewayError`` -- a ``KeyError``,
a ``TypeError``, an ``asyncio.CancelledError`` -- propagates unchanged,
which is the property whose absence let three blanket handlers report every
library bug as a fabricated status code.

Each class declares a wire-stable ``code`` and takes its default status
from ``utils/status_map.py``, so a consumer can branch on ``error['code']``
without parsing message text.

Also home to :func:`unwrap_cause`, which is what stops an exception whose
own ``str()`` is empty -- ``str(RetriesExhausted()) == ''`` -- from reaching
a caller as a blank error message that reads as success.

``unwrap_cause`` is therefore the one seam at which text this library did
*not* author becomes a caller-visible string, and it enforces the matching
invariant: **no foreign exception text leaves this module without passing
through the redactor.** It holds by construction rather than by agreement
between call sites -- redaction is unconditional and the built-in
:data:`~asyncio_gateway.utils.redaction.SENSITIVE_NAMES` need no argument, so
a consumer that knows nothing about redaction still cannot leak through it.
The optional ``redact_params`` only ever widens the set. This matters
because ``aiohttp``'s ``InvalidUrlClientError``, ``NonHttpUrlClientError``
and the ``ClientResponseError`` family all stringify to the full URL,
query string included.
"""

from collections.abc import Collection, Sequence
from typing import ClassVar, Optional, Tuple

from asyncio_gateway.utils.redaction import redact_text
from asyncio_gateway.utils.status_map import status_for

CAUSE_CHAIN_MAX_DEPTH = 10

#: A protocol client's classification table: ordered ``(family, error
#: class)`` rows, most specific first, because the families overlap.
ClassificationTable = Sequence[Tuple[type, type]]


def faults_of(table: ClassificationTable) -> Tuple[type[BaseException], ...]:
    """Return the ``except`` clause derived from a classification table.

    The one function that makes "a client catches exactly what it can
    classify" hold by construction instead of by four tables happening
    to agree. Each protocol's dispatch spells its catch clause as
    ``faults_of(TRANSPORT_ERRORS) + (...)``, so a family added to the
    left column of that table is caught by the same client in the same
    commit, with no second edit to remember.

    The derivation reached only the two HTTP-family clients when it was
    introduced for AGW-R9-1: ``logic.ftp_client`` and
    ``logic.sftp_client`` kept hand-written tuples beside their own
    tables, so the round-9 claim that divergence was "no longer
    representable" covered **two of the four dispatch sites**. This
    function is where the remaining two join, which is what the claim
    was supposed to mean.

    Args:
        table: The client's ordered classification table.

    Returns:
        Its left column as a tuple, in the same order -- catchable by
        an ``except`` clause, and never out of step with what
        ``transport_error_for`` beside it can name.
    """
    return tuple(family for family, _ in table)


class AsyncGatewayError(Exception):
    """The one base a consumer catches.

    Every subclass calls ``super().__init__(message)``, so ``args`` is
    populated, ``str(exc)`` is the message, and instances survive pickling
    across a ``ProcessPoolExecutor`` or a Celery boundary.

    Attributes:
        code: The wire-stable machine-readable code for this failure.
        status_code: The status the envelope reports for this failure.
    """

    code: ClassVar[str] = 'GATEWAY'

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
    ) -> None:
        """Build a gateway error.

        Args:
            message: What went wrong, in human-readable form. It may change
                between releases; ``code`` may not.
            status_code: The status the remote side supplied -- an HTTP
                status, an FTP reply code -- or None to take the class
                default from ``utils/status_map.py``.
        """
        super().__init__(message)
        self.status_code: int = status_for(self.code, override=status_code)

    def __reduce__(self) -> tuple[type['AsyncGatewayError'],
                                  tuple[str, int]]:
        """Rebuild this exception after pickling, status included.

        ``Exception.__reduce__`` would replay ``args`` alone and lose a
        remote-supplied ``status_code``, so the pair is restored explicitly.

        Returns:
            The callable and arguments that reconstruct an equal instance.
        """
        return (self.__class__, (str(self), self.status_code))


class ConfigurationError(AsyncGatewayError):
    """Raised when caller configuration cannot form a valid call.

    The caller's fault, not the remote endpoint's, and never retryable: the
    call is rejected at the library boundary rather than dispatched and
    reported as a failed request.

    Usage:
        raise ConfigurationError('serialization must return str, got bytes')
    """

    code: ClassVar[str] = 'CONFIG'


class UnsupportedVerbError(ConfigurationError):
    """Raised when a caller names a verb outside a protocol's allowlist."""

    code: ClassVar[str] = 'CONFIG'


class ProcessorError(AsyncGatewayError):
    """Raised when a caller's own processor callback failed.

    Deliberately **not** a :class:`ConfigurationError`, because the two are
    different mistakes and a caller acts on them differently. A malformed
    ``pre_processor_config`` -- no ``"function"`` key, a ``"params"`` that
    is not a mapping, a config that is a list -- is a *configuration*
    mistake: the call could never have been formed, nothing ran, and it is
    refused before dispatch with ``CONFIG``/400. This one says the
    opposite: the configuration was well-formed, this library called
    exactly what the caller asked it to call, and *that function* raised.
    Reporting a bug inside the caller's own callback as though the caller
    had mis-spelled a config key would send them looking at the wrong
    thing.

    It exists at all because the one-conversion-point contract admits no
    exceptions: ``request()`` may only ever raise an
    ``AsyncGatewayError``, so a callback's ``RuntimeError`` cannot simply
    be let through, however clearly it belongs to the caller. The original
    is chained as ``__cause__``, so :func:`unwrap_cause` puts its type and
    message in ``error['cause']`` and nothing about it is lost.

    A post-processor raising therefore forfeits the envelope, response
    body included, and that is the accepted cost of keeping the invariant
    absolute -- converting it to an ``ok=False`` envelope instead would
    dress a bug in the caller's cleanup function up as a failed request
    and overwrite the very ``ok=True`` result the caller was about to
    read. A callback that must not cost its caller the response handles
    its own failures.
    """

    code: ClassVar[str] = 'PROCESSOR'


class SerializationError(AsyncGatewayError):
    """Raised when a body cannot be serialised or cannot be parsed.

    Defaults to the response side (502). The request side -- the caller's
    own body refusing to serialise -- passes 400 explicitly.
    """

    code: ClassVar[str] = 'SERIALIZATION'


class UnsafeXmlError(SerializationError):
    """Raised when XML is refused before parsing, e.g. for a DOCTYPE."""

    code: ClassVar[str] = 'XML_UNSAFE'


class PathContainmentError(AsyncGatewayError):
    """Raised when a resolved path escapes its target directory."""

    code: ClassVar[str] = 'PATH'


class LocalWriteError(AsyncGatewayError):
    """Raised when the *local* filesystem refuses a download's write.

    A missing parent directory, an unwritable one, a full disk: the
    remote side answered perfectly and this machine could not keep what
    it sent. It reports ``PATH``/400 alongside
    :class:`PathContainmentError` because the two answer the same
    question -- "the local destination you named will not take this
    file" -- and a caller acts on both the same way: fix the path or the
    disk, then call again.

    It is a sibling of :class:`PathContainmentError` rather than a
    subclass of it, and deliberately **not** a
    :class:`TransportError`. Both distinctions are load-bearing.

    Not a containment failure, because a caller reading ``PATH`` on a
    ``PathContainmentError`` is being told a *security* boundary was
    crossed -- a symlink, a server-composed path escaping the base --
    and a full disk is not that. The shared code keeps the caller's
    branch simple; the distinct class keeps a security finding
    distinguishable from an operational one.

    Not a transport failure, because a transport verdict carries a
    retry recommendation that is wrong here twice over. Re-dialling the
    remote cannot create the missing directory, and each attempt
    re-downloads the whole body -- measured at 4x amplification for one
    call. Worse, a transport verdict *counts*: six local disk failures
    drove the destination's breaker OPEN, so the next healthy call to a
    healthy server got ``CIRCUIT_OPEN`` for a full disk on this machine
    (NEW-R10-1). It is therefore also listed in
    :data:`~asyncio_gateway.helpers.internal.circuit_breaker_helper.DEFAULT_ABORTABLE_EXCEPTIONS`,
    which is what makes those two properties hold rather than merely be
    documented here.
    """

    code: ClassVar[str] = 'PATH'


class TransportError(AsyncGatewayError):
    """Base for a failure to exchange bytes with the remote side."""

    code: ClassVar[str] = 'TRANSPORT'


class ConnectError(TransportError):
    """Raised when a connection is refused, reset or otherwise fails."""

    code: ClassVar[str] = 'CONNECT'


class DnsError(TransportError):
    """Raised when a host name cannot be resolved."""

    code: ClassVar[str] = 'DNS'


class TlsError(TransportError):
    """Raised when a TLS handshake or certificate check fails."""

    code: ClassVar[str] = 'TLS'


class HostKeyError(TransportError):
    """Raised when an SSH host key is unknown or does not match."""

    code: ClassVar[str] = 'HOST_KEY'


class GatewayTimeoutError(TransportError):
    """Raised when a connect, read or total deadline expires."""

    code: ClassVar[str] = 'TIMEOUT'


class ResponseTooLargeError(TransportError):
    """Raised when a response body exceeds the configured byte cap."""

    code: ClassVar[str] = 'RESPONSE_TOO_LARGE'


class ResponseTooDeepError(TransportError):
    """Raised when a multipart response nests past the depth cap.

    The sibling of :class:`ResponseTooLargeError`, and separate from it
    because the two bound different resources: that one caps how many
    *bytes* a body may make this process read, this one caps how far
    *down* it may make it walk. A 221 KB body of 2000 empty nesting
    levels is nowhere near any realistic byte ceiling and still exhausted
    the interpreter's stack (NEW-H2), so the byte cap could not have
    caught it and a caller cannot tell the two refusals apart from a
    shared code.
    """

    code: ClassVar[str] = 'RESPONSE_TOO_DEEP'


class CircuitOpenError(AsyncGatewayError):
    """Raised when the breaker for a destination is open."""

    code: ClassVar[str] = 'CIRCUIT_OPEN'


class StackExhaustedError(AsyncGatewayError):
    """Raised when dispatching a call exhausted the interpreter stack.

    The backstop for the one-conversion-point invariant. A
    ``RecursionError`` raised inside ``handle_request`` is a failure like
    any other and the contract says a failure arrives as an ``ok=False``
    envelope -- but it is not an ``AsyncGatewayError``, so it escaped
    ``request()`` as a bare interpreter exception, which is precisely what
    a hostile 221 KB multipart body of 2000 nesting levels produced
    (NEW-H2).

    Distinct from :class:`ResponseTooDeepError` because they answer
    different questions. That one is a *bound this library states and
    enforces*: a named ceiling, crossed, refused before any harm. This one
    is a bound the **interpreter** imposed, reached by a path no cap
    anticipated -- so a caller seeing it is being told the library ran out
    of stack somewhere it did not expect to, which is worth a code of its
    own rather than being dressed up as a response that nested too far.
    """

    code: ClassVar[str] = 'STACK_EXHAUSTED'


class ProtocolError(AsyncGatewayError):
    """Base for a remote side that answered, and answered with a failure.

    Every subclass is raised *after* the protocol has already written the
    status, headers, cookies, body text and parsed body into the envelope,
    so ``ok=False`` never costs the caller the response (invariant E11).
    """

    code: ClassVar[str] = 'PROTOCOL'


class HttpStatusError(ProtocolError):
    """Raised for an HTTP 4xx or 5xx, carrying the real status."""

    code: ClassVar[str] = 'HTTP_STATUS'


class FtpStatusError(ProtocolError):
    """Raised for an FTP 4xx or 5xx reply, carrying the reply code."""

    code: ClassVar[str] = 'FTP_STATUS'


class SftpStatusError(ProtocolError):
    """Raised for an SFTP ``SSH_FX_*`` status, carrying its mapped code."""

    code: ClassVar[str] = 'SFTP_STATUS'


class SoapFaultError(ProtocolError):
    """Raised for a SOAP Fault, at any transport status including 200."""

    code: ClassVar[str] = 'SOAP_FAULT'


def unwrap_cause(
    exc: BaseException,
    *,
    max_depth: int = CAUSE_CHAIN_MAX_DEPTH,
    redact_params: Collection[str] = (),
) -> tuple[str, Optional[str]]:
    """Walk an exception's cause chain for a message that is not empty.

    ``str(RetriesExhausted())`` is ``''``. An empty error message reads as
    success and is worse than a fabricated status code, and the information
    the caller needs is one link down the ``__cause__`` chain. This walks
    that chain (then ``__context__``) and reports both the wrapper's own
    type and the deepest cause it found.

    Both returned strings are redacted, always. The exceptions this walks
    into are mostly *not* this library's -- and a third-party exception
    that stringifies to a URL with a live ``?api_key=`` in it is the
    ordinary case, not an exotic one. Redacting at the return rather than
    at each consumer is what makes the guarantee survive the next consumer
    somebody adds: there is no unredacted way out of this function.

    Args:
        exc: The exception to describe.
        max_depth: How many links to follow. The bound is what makes a
            cyclic or pathologically deep chain terminate.
        redact_params: The caller's additional sensitive query-parameter
            names, normalised once in ``request()``. Omitting it costs the
            caller's *extension* names only: the built-in set still
            applies, because it is the base contract and not an opt-in.

    Returns:
        A ``(message, cause)`` pair, both redacted. ``message`` is never
        empty: it falls back to the deepest non-empty cause prefixed with
        the wrapper's type, and finally to the exception's own class name
        -- neither of which redaction can empty, since it only ever
        rewrites the inside of a URL. ``cause`` is ``'<Type>: <message>'``
        for the deepest non-empty link, or None when the chain adds
        nothing.
    """
    chain = _cause_chain(exc, max_depth)
    deepest: Optional[BaseException] = None
    for link in chain[1:]:
        if str(link):
            deepest = link

    own = str(exc)
    if deepest is None:
        return redact_text(
            own or type(exc).__name__, extra_params=redact_params), None

    cause = f'{type(deepest).__name__}: {deepest}'
    message = own or f'{type(exc).__name__}: {deepest}'
    return (
        redact_text(message, extra_params=redact_params),
        redact_text(cause, extra_params=redact_params),
    )


def _cause_chain(
    exc: BaseException,
    max_depth: int,
) -> list[BaseException]:
    """Return ``exc`` and its causes, bounded and never revisiting a link.

    Args:
        exc: The exception to start from.
        max_depth: How many links to follow past ``exc``.

    Returns:
        The chain in order, shallowest first.
    """
    chain: list[BaseException] = [exc]
    seen = {id(exc)}
    link: BaseException = exc
    for _ in range(max_depth):
        following = link.__cause__ or link.__context__
        if following is None or id(following) in seen:
            break
        seen.add(id(following))
        chain.append(following)
        link = following
    return chain

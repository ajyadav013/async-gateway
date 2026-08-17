"""HTTP and HTTPS, the reference implementation of the protocol contract.

Fills the envelope it was handed and returns that same object. Every
failure it can produce is raised as a typed ``AsyncGatewayError`` for the
entry point to convert -- and for a remote-status failure the response is
copied into the envelope *before* the raise, so ``ok=False`` never costs
the caller the body (invariant E11).
"""

import asyncio
import ssl
from collections.abc import Collection, MutableMapping
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Text,
    Tuple,
)

import aiohttp
from async_gateway.helpers.internal import is_json_media_type, media_type_of
from async_gateway.helpers.internal.base import BaseRequestClass
from async_gateway.helpers.internal.filters_helper import is_get
from async_gateway.helpers.internal.request_helper import \
    HttpResult, handle_http_request
from async_gateway.helpers.internal.response_helper import \
    application_json_response
from async_gateway.utils.constants import (
    ALLOWED_SCHEMES,
    CREDENTIAL_HEADERS,
    HTTP_ERROR_STATUS,
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
)
from async_gateway.utils.envelope import GatewayResponse, finalise_ok
from async_gateway.utils.exceptions import (
    AsyncGatewayError,
    CircuitOpenError,
    ConfigurationError,
    ConnectError,
    DnsError,
    GatewayTimeoutError,
    HttpStatusError,
    SerializationError,
    TlsError,
    TransportError,
    unwrap_cause,
)
from async_gateway.utils.redaction import (
    redact_cookies,
    redact_headers,
    redact_url,
)
from async_gateway.utils.request_tracer import request_tracer
from failsafe import CircuitOpen, FailsafeError, RetriesExhausted
import orjson

JsonSerializer = Callable[[Any], Text]

# Ordered, because the families overlap: `ServerTimeoutError` is also a
# `ClientConnectionError`, and `ClientConnectorCertificateError` is also a
# `ClientConnectorError`. First match wins, so the most specific
# classification is listed first.
TRANSPORT_ERRORS: Sequence[Tuple[type, type]] = (
    (asyncio.TimeoutError, GatewayTimeoutError),
    (aiohttp.ClientSSLError, TlsError),
    (ssl.SSLError, TlsError),
    (aiohttp.ClientConnectorDNSError, DnsError),
    (aiohttp.ClientConnectionError, ConnectError),
    (aiohttp.ClientError, TransportError),
)


def default_json_serialize(obj: Any) -> Text:
    """Serialise ``obj`` to a JSON string.

    The default for ``ClientSession(json_serialize=...)``, which requires a
    ``str``-returning callable. ``orjson.dumps`` returns ``bytes``, so it can
    never be passed bare; this wrapper is what makes the migration safe.

    Args:
        obj: Any object ``orjson`` can serialise.

    Returns:
        The JSON encoding of ``obj`` as text.

    Raises:
        TypeError: If ``orjson`` cannot serialise ``obj``.
    """
    return orjson.dumps(obj).decode()


def validated_json_serializer(serialization: JsonSerializer) -> JsonSerializer:
    """Return ``serialization`` once proven callable and ``str``-returning.

    ``aiohttp`` calls ``json_serialize`` deep inside payload construction and
    a ``bytes`` return surfaces there as an opaque failure, long after the
    caller's mistake. Probing it here — with an empty mapping, the cheapest
    input every JSON serialiser accepts — turns that into a rejection at the
    boundary. The commonest mistake is passing ``orjson.dumps`` itself; a
    value that is not callable at all is the same class of caller error and
    is rejected the same way, rather than as a bare ``TypeError``.

    Args:
        serialization: The caller-supplied JSON serialiser, or the default.

    Returns:
        The same callable, unwrapped and unmodified.

    Raises:
        ConfigurationError: If the value is not callable, or does not
            return ``str``.
    """
    if not callable(serialization):
        raise ConfigurationError(
            f'protocol_info["serialization"] must be callable, but '
            f'{serialization!r} is of type '
            f'{type(serialization).__name__}')
    probe = serialization({})
    if not isinstance(probe, str):
        raise ConfigurationError(
            f'protocol_info["serialization"] must return str, but '
            f'{getattr(serialization, "__name__", serialization)!r} '
            f'returned {type(probe).__name__}')
    return serialization


def validated_upload_config(
    http_file_upload_config: Dict,
    request_type: Text,
) -> Dict:
    """Return ``http_file_upload_config`` once proven usable on this verb.

    A file upload on a GET had no defined behaviour: a dead ``if ...: pass``
    in the JSON filter suggested the file was meant to be dropped, while the
    code that actually ran attached it as a GET body. Rejecting the pair is
    the explicit decision R12 asks for -- dropping a file the caller asked
    to send is the one outcome that is silently wrong (L8).

    Checked here, at the construction boundary, and not in the transport
    helper that consumes the config: R11's rule is that a caller's
    configuration is validated once, where the caller's values are first
    read. It is also the only placement that satisfies the entry point's
    documented contract, which the transport helper cannot -- see
    :meth:`HttpRequest.__init__`.

    Args:
        http_file_upload_config: The caller's upload config; ``{}`` when
            they configured no upload, which is the case this permits.
        request_type: The verb the call will be dispatched on, matched
            case-insensitively.

    Returns:
        The same config, unmodified.

    Raises:
        ConfigurationError: If a non-empty upload config is combined with
            a GET.
    """
    if http_file_upload_config and is_get(request_type):
        raise ConfigurationError(
            'http_file_upload_config cannot be combined with '
            f'request_type {request_type!r}: a GET has no body to '
            'upload a file in. Use POST or PUT, or remove the upload '
            'config.')
    return http_file_upload_config


def validated_session(session: object) -> Optional[aiohttp.ClientSession]:
    """Return the caller's session once proven usable, or None.

    A session the caller supplies is *theirs*: it is used and never closed,
    which is what makes connection pooling across calls possible at all.
    That only works if the object really is a live session, so the ways it
    can fail are rejected here rather than several frames into
    ``aiohttp``. A closed session otherwise surfaces as
    ``RuntimeError: Session is closed`` from inside the transport, which
    reads as a network failure and is retried like one.

    A session carrying a **credential** is refused for a different and
    sharper reason. ``aiohttp`` merges a session's default headers and
    ``auth`` into every request it issues, underneath the ones this call
    passes, and the redirect loop can only withhold what it passes. A
    ``ClientSession(headers={'Authorization': ...})`` would therefore send
    that token to whatever host a hostile ``Location`` named -- a leak
    ``aiohttp``'s own loop did not have, introduced by taking the loop
    over. Refusing the combination keeps the documented promise on
    :func:`~async_gateway.helpers.internal.request_helper.make_http_request`
    true rather than true-except-on-this-path; the same credentials
    supplied per call, which is the route the message names, are stripped
    on a cross-origin hop exactly as before.

    Args:
        session: ``protocol_info['session']``, of whatever type the caller
            actually passed, or None when they passed nothing.

    Returns:
        The same session, or None when the library is to create its own.

    Raises:
        ConfigurationError: If the value is neither None nor a live
            ``aiohttp.ClientSession``, or if it carries a credential
            header or a session-level ``auth``.
    """
    if session is None:
        return None
    if not isinstance(session, aiohttp.ClientSession):
        raise ConfigurationError(
            f'protocol_info["session"] must be an aiohttp.ClientSession '
            f'or None, got {type(session).__name__}')
    if session.closed:
        raise ConfigurationError(
            'protocol_info["session"] is already closed; a closed session '
            'cannot carry a request, and this library never reopens one '
            'it does not own')
    carried = sorted({
        name.lower()
        for name in session.headers
        if name.lower() in CREDENTIAL_HEADERS
    })
    if carried:
        raise ConfigurationError(
            f'protocol_info["session"] carries the credential header(s) '
            f'{carried} as session defaults, which aiohttp merges into '
            f'every request and no redirect hop can withhold; a hostile '
            f'Location would receive them. Pass them in '
            f'protocol_info["headers"] instead, which this library strips '
            f'when a hop crosses an origin')
    if session.auth is not None:
        raise ConfigurationError(
            'protocol_info["session"] carries a session-level auth, which '
            'aiohttp merges into every request and no redirect hop can '
            'withhold; a hostile Location would receive it. Pass the auth '
            'argument of request() instead, which this library strips '
            'when a hop crosses an origin')
    return session


def validated_max_response_bytes(max_response_bytes: object) -> int:
    """Return the response cap once proven a usable ceiling.

    There is deliberately no "disable" sentinel -- no 0, no -1, no None. A
    caller who needs more names a bigger number, so that "unbounded" is
    never one typo away and every call has *some* ceiling.

    Args:
        max_response_bytes: ``protocol_info['max_response_bytes']``, of
            whatever type the caller passed, or the default.

    Returns:
        The ceiling in bytes.

    Raises:
        ConfigurationError: If the value is not a positive integer.
            ``bool`` is rejected with it: ``True`` is an ``int`` of value
            1, and a one-byte cap is not what anyone meant by it.
    """
    if isinstance(max_response_bytes, bool) or not isinstance(
            max_response_bytes, int):
        raise ConfigurationError(
            f'protocol_info["max_response_bytes"] must be a positive int, '
            f'got {type(max_response_bytes).__name__}')
    if max_response_bytes <= 0:
        raise ConfigurationError(
            f'protocol_info["max_response_bytes"] must be a positive int, '
            f'got {max_response_bytes}; there is no sentinel that disables '
            f'the cap')
    return max_response_bytes


def validated_timeout(timeout: object) -> float:
    """Return the call deadline once proven a usable number of seconds.

    The last of the ``protocol_info`` keys this class reads that nothing
    checked, and the owned redirect loop gave it a crash site: the chain
    deadline is ``time.monotonic() + timeout.total``, so a
    ``timeout='ten'`` raised ``TypeError: unsupported operand type(s) for
    +: 'float' and 'str'`` out of ``request()`` un-enveloped, and a
    ``timeout=True`` was accepted as a *one second* deadline and reported
    ``ok=True`` -- a caller's config error answered as a successful call.

    ``float`` is accepted alongside ``int`` because sub-second deadlines
    are an ordinary ask and the transport takes seconds as a float.

    Args:
        timeout: ``protocol_info['timeout']``, of whatever type the caller
            passed, or the default.

    Returns:
        The deadline in seconds.

    Raises:
        ConfigurationError: If the value is not a positive number.
            ``bool`` is rejected with it, as it is for the response cap
            and the redirect bound: ``True`` is an ``int`` of value 1 and
            a one-second deadline is not what anyone meant by it.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ConfigurationError(
            f'protocol_info["timeout"] must be a positive number of '
            f'seconds, got {type(timeout).__name__}')
    if timeout <= 0:
        raise ConfigurationError(
            f'protocol_info["timeout"] must be a positive number of '
            f'seconds, got {timeout}')
    return float(timeout)


def validated_allow_redirects(allow_redirects: object) -> bool:
    """Return the redirect-following flag once proven a bool.

    Not coerced with ``bool(...)``: every non-empty string is truthy, so
    ``allow_redirects='no'`` would *follow* redirects -- the caller's
    stated intent inverted, silently, on the one key whose whole purpose
    is to stop a hop being made.

    Args:
        allow_redirects: ``protocol_info['allow_redirects']``, of whatever
            type the caller passed, or the default.

    Returns:
        The flag.

    Raises:
        ConfigurationError: If the value is not a ``bool``.
    """
    if not isinstance(allow_redirects, bool):
        raise ConfigurationError(
            f'protocol_info["allow_redirects"] must be a bool, got '
            f'{type(allow_redirects).__name__}; no other value is '
            f'interpreted, because a truthy string would invert the '
            f'intent the caller stated')
    return allow_redirects


def validated_max_redirects(max_redirects: object) -> int:
    """Return the redirect bound once proven a usable count.

    Checked here rather than where it is compared, because the comparison
    is made only *when a redirect arrives*: a ``max_redirects='three'``
    otherwise reached the transport intact and raised
    ``TypeError: '>=' not supported between instances of 'int' and 'str'``
    out of ``request()`` un-enveloped, on the subset of calls whose
    endpoint happened to redirect.

    Zero is allowed and means "follow none": unlike the response cap
    there is nothing unsafe about it. It is **not** how a caller asks for
    the redirect response itself. Zero is a *bound the chain overran*, so
    a 302 answers ``ok=False``, ``TRANSPORT``, status 302 and an empty
    ``text`` -- the body is never read, because the loop raises where it
    would have followed. A caller who wants the 302 handed back, body and
    all, sets ``allow_redirects=False``, which answers ``ok=True``,
    status 302 and the redirect's own body. Measured side by side and
    pinned by ``test_zero_redirects_and_no_redirects_are_different_asks``.

    Args:
        max_redirects: ``protocol_info['max_redirects']``, of whatever
            type the caller passed, or the default.

    Returns:
        The number of hops that may be followed.

    Raises:
        ConfigurationError: If the value is not a non-negative int.
            ``bool`` is rejected with it, as it is for the response cap:
            ``True`` is an ``int`` of value 1 and nobody means a one-hop
            bound by it.
    """
    if isinstance(max_redirects, bool) or not isinstance(max_redirects, int):
        raise ConfigurationError(
            f'protocol_info["max_redirects"] must be a non-negative int, '
            f'got {type(max_redirects).__name__}')
    if max_redirects < 0:
        raise ConfigurationError(
            f'protocol_info["max_redirects"] must be a non-negative int, '
            f'got {max_redirects}')
    return max_redirects


def validated_allowed_schemes(allowed_schemes: object) -> frozenset[Text]:
    """Return the scheme allowlist once proven a set of scheme names.

    A bare ``str`` is rejected before anything else, because it is the
    mistake this key invites and the only one that is *silently* wrong:
    ``frozenset('https')`` is ``{'h', 't', 'p', 's'}``, an allowlist that
    admits no real scheme at all and prints as ``['h', 'p', 's', 't']`` in
    the rejection a hop then fails with.

    Names are lower-cased here, once, because the per-hop check compares a
    lower-cased scheme against this set -- so an ``allowed_schemes={'HTTPS'}``
    would otherwise refuse every hop it was written to permit.

    Args:
        allowed_schemes: ``protocol_info['allowed_schemes']``, of whatever
            type the caller passed, or the default.

    Returns:
        The scheme names, lower-cased.

    Raises:
        ConfigurationError: If the value is a bare ``str``, is not a
            collection, is empty, or names anything that is not a ``str``.
    """
    if isinstance(allowed_schemes, str):
        raise ConfigurationError(
            f'protocol_info["allowed_schemes"] must be a collection of '
            f'scheme names, not the single string {allowed_schemes!r}: a '
            f'str iterates as its characters, so this would allow '
            f'{sorted(frozenset(allowed_schemes))} and no real scheme')
    if not isinstance(allowed_schemes, Collection):
        raise ConfigurationError(
            f'protocol_info["allowed_schemes"] must be a collection of '
            f'scheme names, got {type(allowed_schemes).__name__}')
    if not allowed_schemes:
        raise ConfigurationError(
            'protocol_info["allowed_schemes"] must name at least one '
            'scheme; an empty allowlist refuses every url, including the '
            'one the call was made to')
    names: List[Text] = []
    for name in allowed_schemes:
        if not isinstance(name, str):
            raise ConfigurationError(
                f'protocol_info["allowed_schemes"] must name schemes as '
                f'str, got {type(name).__name__} {name!r}')
        names.append(name.lower())
    return frozenset(names)


def validated_serialization(
    protocol_info: Dict,
    session: Optional[aiohttp.ClientSession],
) -> JsonSerializer:
    """Return the JSON serialiser once proven usable *and* reachable.

    ``json_serialize`` exists only on ``ClientSession``, so a caller who
    supplies their own session leaves this library nowhere to apply one.
    The value used to be validated and then silently dropped on that path
    -- an undocumented no-op on the one key whose effect is invisible
    until the wrong bytes reach the wire. Rejecting the pair says so at
    the boundary and names the route that works.

    Args:
        protocol_info: The caller's ``protocol_info``; membership matters
            as well as the value, since only an explicitly supplied key is
            a conflict.
        session: The already-validated caller session, or None when the
            library will build its own.

    Returns:
        The serialiser to wire into the session this library creates.

    Raises:
        ConfigurationError: If ``serialization`` is combined with
            ``session``, or is not a ``str``-returning callable.
    """
    if session is not None and 'serialization' in protocol_info:
        raise ConfigurationError(
            'protocol_info["serialization"] cannot be combined with '
            'protocol_info["session"]: aiohttp exposes json_serialize on '
            'the session only, so this library cannot apply one to a '
            'session it did not create. Pass json_serialize to your own '
            'ClientSession, or drop the session and let this library '
            'build one')
    return validated_json_serializer(
        protocol_info.get('serialization', default_json_serialize))


def validated_trace_config(
    protocol_info: Dict,
    session: Optional[aiohttp.ClientSession],
) -> List[aiohttp.TraceConfig]:
    """Return the tracers to attach, once proven attachable and usable.

    ``trace_configs`` is a ``ClientSession`` constructor argument, so a
    caller who supplies their own session leaves this library nowhere to
    attach one -- the identical shape to ``serialization``, and refused
    the identical way. It used to be accepted, dropped, and then read
    back into the envelope's ``request_tracer``, which came out hollow
    because the collector it was read from had never been wired to
    anything. The route the refusal names -- attaching the tracer to the
    caller's own session -- is honoured by
    :func:`trace_collectors_for`, which reads the session's public
    ``trace_configs`` so the loop writes into the collector the caller
    can actually read.

    The value itself is validated for the reason its three redirect
    siblings are. It is read as ``tc.results_collector for tc in ...``
    and every malformed spelling escaped ``request()`` **un-enveloped**:
    ``[aiohttp.TraceConfig()]`` (no ``results_collector`` --
    ``request_tracer`` attaches that itself) and ``'tracer'`` both as
    ``AttributeError``, and a bare, unlisted ``aiohttp.TraceConfig()`` as
    ``TypeError: not iterable``. An empty list is legal and means "no
    tracing": unlike ``allowed_schemes``, nothing is refused by it.

    Args:
        protocol_info: The caller's ``protocol_info``; membership matters
            as well as the value, since only an explicitly supplied key is
            a conflict.
        session: The already-validated caller session, or None when the
            library will build its own.

    Returns:
        The tracers to attach to the session this library creates, or an
        empty list when the caller owns the session.

    Raises:
        ConfigurationError: If ``trace_config`` is combined with
            ``session``, is a bare ``str``, is not a collection, or names
            anything that is not an ``aiohttp.TraceConfig`` carrying a
            writable ``results_collector`` mapping.
    """
    if session is not None:
        if 'trace_config' in protocol_info:
            raise ConfigurationError(
                'protocol_info["trace_config"] cannot be combined with '
                'protocol_info["session"]: aiohttp accepts trace_configs '
                'on the session constructor only, so this library cannot '
                'attach one to a session it did not create. Pass '
                'trace_configs to your own ClientSession, or drop the '
                'session and let this library build one')
        return []
    if 'trace_config' not in protocol_info:
        return [request_tracer()]
    trace_config = protocol_info['trace_config']
    if isinstance(trace_config, str):
        raise ConfigurationError(
            f'protocol_info["trace_config"] must be a collection of '
            f'aiohttp.TraceConfig, not the single string '
            f'{trace_config!r}')
    if not isinstance(trace_config, Collection):
        raise ConfigurationError(
            f'protocol_info["trace_config"] must be a collection of '
            f'aiohttp.TraceConfig, got '
            f'{type(trace_config).__name__}; a single tracer is passed as '
            f'a one-element list')
    for tracer in trace_config:
        if not isinstance(tracer, aiohttp.TraceConfig):
            raise ConfigurationError(
                f'protocol_info["trace_config"] must name tracers as '
                f'aiohttp.TraceConfig, got {type(tracer).__name__}')
        if not isinstance(
                getattr(tracer, 'results_collector', None), MutableMapping):
            raise ConfigurationError(
                'protocol_info["trace_config"] names an aiohttp.TraceConfig '
                'with no results_collector mapping to write into; this '
                'library reads that attribute to fill the envelope key '
                '"request_tracer". Build tracers with '
                'async_gateway.utils.request_tracer.request_tracer(), '
                'which attaches one')
    return list(trace_config)


def trace_collectors_for(
    session: Optional[aiohttp.ClientSession],
    trace_config: Sequence[aiohttp.TraceConfig],
) -> List[Dict[Text, Any]]:
    """Return the collector mappings this call traces into.

    Derived once, in one place, so the collectors the redirect loop
    writes into are provably the same objects the envelope reports --
    they are literally the same dicts, not two reads of the same source.

    A **supplied** session is read for its own tracers rather than
    skipped. ``validated_trace_config`` tells such a caller to
    "pass trace_configs to your own ClientSession", and that route used
    to produce the very hollow output the refusal exists to prevent: the
    library derived nothing from the session, so :func:`record_redirect`
    wrote nowhere and the caller's collector reported ``is_redirect``
    False on a chain this library had just followed. ``trace_configs``
    is public on ``ClientSession`` and returns the same objects the
    caller constructed it with, so the collector is reachable and there
    was never a reason not to reach it.

    Members that carry no writable ``results_collector`` are skipped
    rather than refused. A caller's session is theirs, and a plain
    ``aiohttp.TraceConfig`` attached for their own callbacks is a
    legitimate thing to find on it -- it simply is not something this
    library can write a redirect event into. The
    ``protocol_info["trace_config"]`` route *is* refused, because there
    the value is this library's to validate.

    Args:
        session: The already-validated caller session, or None when the
            library builds its own.
        trace_config: The tracers this library will attach to the session
            it builds; empty when the caller supplied one.

    Returns:
        The ``results_collector`` mapping of every usable tracer, in the
        order the tracers were given.
    """
    if session is None:
        return [tracer.results_collector for tracer in trace_config]
    attached: Iterable[Any] = getattr(session, 'trace_configs', ())
    return [
        tracer.results_collector
        for tracer in attached
        if isinstance(
            getattr(tracer, 'results_collector', None), MutableMapping)
    ]


def transport_error_for(
    err: BaseException,
    *,
    redact_params: Collection[Text] = (),
) -> AsyncGatewayError:
    """Return the typed transport error for ``err``, or propagate a bug.

    The resilience layer wraps *every* exception the call raised in a
    ``RetriesExhausted`` whose own ``str()`` is empty, so the classification
    has to be made on the cause rather than on the wrapper -- and so the
    message has to come from :func:`unwrap_cause` rather than from
    ``str(err)``, which would be ``''``.

    Args:
        err: The exception caught around the dispatch.
        redact_params: The caller's additional sensitive query-parameter
            names, forwarded to :func:`unwrap_cause`. ``err`` is
            ``aiohttp``'s, not this library's, and several of its
            exceptions stringify to the full request URL; the built-in
            names are masked whether or not this is supplied.

    Returns:
        The ``AsyncGatewayError`` subclass matching the failure's family,
        carrying a message that is never empty and always redacted.

    Raises:
        BaseException: The original cause, unchanged, when it belongs to no
            transport family. A ``KeyError`` or ``TypeError`` from this
            library's own code is a bug, not a transport failure, and must
            escape ``request()`` rather than become an envelope.
    """
    cause = err.__cause__ if isinstance(err, FailsafeError) else err
    message, _ = unwrap_cause(err, redact_params=redact_params)
    for family, error_class in TRANSPORT_ERRORS:
        if isinstance(cause, family):
            return error_class(message)
    if cause is None:
        return TransportError(message)
    # `from None`: the wrapper is already this exception's `__context__`,
    # and suppressing it keeps the traceback pointing at the bug.
    raise cause from None


class HttpRequest(BaseRequestClass):
    """Implements Aiohttp Request to make http/https calls."""

    # There is no default verb to fall back on: a GET assumed for a caller
    # who meant DELETE is worse than a rejected call, so the key is
    # required and `BaseRequestClass` rejects the call without it.
    REQUIRED_INFO_KEYS: ClassVar[frozenset[Text]] = frozenset(
        {'request_type'})

    def __init__(self, *args, **kwargs) -> None:
        """Initializing the http request class.

        Raises:
            ConfigurationError: If ``protocol_info`` cannot form a valid
                call: a "serialization" that is not callable, does not
                return str, or is combined with a "session"; a
                "trace_config" combined with a "session", or that is not
                a collection of ``aiohttp.TraceConfig`` each carrying a
                ``results_collector``; an
                ``http_file_upload_config`` combined with a GET; a
                "session" that is not a live ``aiohttp.ClientSession`` or
                that carries a credential header or a session-level
                ``auth``; a "max_response_bytes" that is not a positive
                int; an "allow_redirects" that is not a bool; a
                "max_redirects" that is not a non-negative int; a
                "timeout" that is not a positive number of seconds; or an
                "allowed_schemes" that is not a non-empty collection of
                scheme names. All are raised here, in the
                constructor, because
                the entry point builds the protocol object *outside* the
                block that converts an ``AsyncGatewayError`` into an
                envelope -- so a configuration error raised here escapes to
                the caller unlogged and before anything is dispatched,
                which is the contract ``request()`` documents.
        """
        super(HttpRequest, self).__init__(*args, **kwargs)

        self.request_type: Text = self.info['request_type']
        self.cookies: Any = self.info.get('cookies')
        self.headers: Dict = self.info.get('headers', {})
        self.verify_ssl: bool = self.info.get('verify_ssl', True)
        self.http_file_upload_config: Dict = validated_upload_config(
            self.info.get('http_file_upload_config', {}), self.request_type)
        # No `{}` default: an empty config is a caller asking for a
        # download on every documented default, and absence is the only
        # way left to say "no download at all" (M10).
        self.file_download_config: Optional[Dict] = self.info.get(
            'http_file_download_config')
        self.timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(
            total=validated_timeout(self.timeout))
        # None means "build one and close it"; anything else is the
        # caller's and is never closed here. Read before the serialiser,
        # which cannot be honoured on a session this library did not
        # create and is refused with one.
        self.session: Optional[aiohttp.ClientSession] = validated_session(
            self.info.get('session'))
        self.serialization: JsonSerializer = validated_serialization(
            self.info, self.session)
        self.trace_config: List[aiohttp.TraceConfig] = validated_trace_config(
            self.info, self.session)
        # What the redirect loop writes its trace event into: the
        # collectors of the tracers this library attaches, or -- for a
        # caller-supplied session -- the ones already on it, which is the
        # route `validated_trace_config`'s refusal names.
        self.trace_collectors: List[Dict[Text, Any]] = trace_collectors_for(
            self.session, self.trace_config)
        # What the *envelope* reports, which is deliberately narrower: the
        # collectors of the tracers this library attached, so a supplied
        # session still reports `[]`. See `_copy_into_envelope`.
        self.reported_collectors: List[Dict[Text, Any]] = [
            tc.results_collector for tc in self.trace_config
        ]
        self.max_response_bytes: int = validated_max_response_bytes(
            self.info.get('max_response_bytes', MAX_RESPONSE_BYTES))
        self.allow_redirects: bool = validated_allow_redirects(
            self.info.get('allow_redirects', True))
        self.max_redirects: int = validated_max_redirects(
            self.info.get('max_redirects', MAX_REDIRECTS))
        self.allowed_schemes: frozenset[Text] = validated_allowed_schemes(
            self.info.get('allowed_schemes', ALLOWED_SCHEMES))

    async def handle_request(self) -> GatewayResponse:
        """Make the network call and fill the envelope with the response.

        A caller-supplied ``protocol_info['session']`` is used and left
        open, so consecutive calls reuse its connection pool; a session
        this method creates is closed on the way out. Either way the
        per-request ``timeout`` is applied on the request itself, so a
        supplied session's own deadline cannot silently outrank the one
        this call was configured with (R14).

        ``headers``, ``cookies`` and ``auth`` are applied per request
        rather than only on the session, for two reasons: a caller who
        supplies a session would otherwise have those three silently
        dropped, and the owned redirect loop needs to be able to withhold
        them on a hop that crosses an origin. That withholding is the
        reason a supplied session may not carry credentials of its own --
        ``aiohttp`` merges session defaults into every request and no hop
        can suppress them -- and :func:`validated_session` refuses one
        that does. ``serialization`` and ``trace_config`` are the two keys
        that cannot follow at all -- ``json_serialize`` and
        ``trace_configs`` exist only on the session constructor -- so both
        are refused alongside a supplied session rather than accepted and
        ignored. With no tracer to attach, ``request_tracer`` on the
        envelope is ``[]`` -- and stays ``[]`` even when the supplied
        session brings tracers of its own, which the redirect loop does
        write into: those collectors are the caller's, live as long as
        their session, and would alias one mutating dict across every
        envelope the session served.

        Returns:
            The same envelope object this request was constructed with,
            populated and finalised.

        Raises:
            HttpStatusError: On a 4xx or 5xx -- raised *after* the status,
                headers, cookies, body text and parsed body are already in
                the envelope (invariant E11).
            CircuitOpenError: When the breaker for this destination is open.
            GatewayTimeoutError: On a connect, read or total timeout.
            TlsError: On a handshake or certificate failure.
            DnsError: When the host name does not resolve.
            ConnectError: When the connection is refused or reset.
            ConfigurationError: When a redirect target's scheme is not in
                ``allowed_schemes``; raised before that hop is issued.
            ResponseTooLargeError: When a response body exceeds
                ``max_response_bytes``.
            TransportError: For any other client-side transport failure,
                and for a redirect chain longer than ``max_redirects`` --
                which carries the last status the chain reached.
            SerializationError: When the body is not decodable text, or is
                announced as JSON and is not valid JSON -- raised *after*
                the status has been judged, so a malformed body on a 404
                is still reported as the 404 it was.
        """
        if self.session is not None:
            return await self._exchange(self.session)
        async with aiohttp.ClientSession(
            trace_configs=self.trace_config, timeout=self.timeout,
            json_serialize=self.serialization
        ) as session:
            return await self._exchange(session)

    async def _exchange(
        self,
        session: aiohttp.ClientSession,
    ) -> GatewayResponse:
        """Dispatch on ``session`` and fill the envelope with the answer.

        Split out of :meth:`handle_request` so that the two ways a session
        is obtained -- the caller's, kept open, and this object's, closed
        on the way out -- share one body rather than one being a copy of
        the other that drifts.

        Args:
            session: The session to dispatch on, whoever owns it.

        Returns:
            The same envelope object, populated and finalised.

        Raises:
            AsyncGatewayError: As documented on :meth:`handle_request`.
        """
        try:
            result: HttpResult = await handle_http_request(
                session,
                self.url,
                self.request_type,
                self.circuit_breaker,
                headers=self.headers,
                cookies=self.cookies,
                auth=self.auth,
                payload=self.response['payload'],
                certificate=self.certificate,
                verify_ssl=self.verify_ssl,
                http_file_download_config=self.file_download_config,
                http_file_upload_config=self.http_file_upload_config,
                redact_params=self.redact_params,
                timeout=self.timeout,
                max_response_bytes=self.max_response_bytes,
                allowed_schemes=self.allowed_schemes,
                allow_redirects=self.allow_redirects,
                max_redirects=self.max_redirects,
                trace_collectors=self.trace_collectors,
            )
        except CircuitOpen as err:
            raise CircuitOpenError(
                f'circuit open for '
                f'{redact_url(self.url, extra_params=self.redact_params)}'
            ) from err
        except RetriesExhausted as err:
            raise transport_error_for(
                err, redact_params=self.redact_params) from err
        except (aiohttp.ClientError, asyncio.TimeoutError,
                ssl.SSLError) as err:
            raise transport_error_for(
                err, redact_params=self.redact_params) from err

        body_error = await self._copy_into_envelope(result)

        status: int = result['status_code']
        if status >= HTTP_ERROR_STATUS:
            raise HttpStatusError(
                f'{self.request_type.upper()} '
                f'{redact_url(self.url, extra_params=self.redact_params)}'
                f' returned HTTP {status}',
                status)
        if body_error is not None:
            raise body_error

        return finalise_ok(
            self.response, status_code=status, started=self.start_time)

    async def _copy_into_envelope(
        self,
        result: HttpResult,
    ) -> Optional[SerializationError]:
        """Copy a transport result into the envelope, redacting as it goes.

        Called before any error is raised, which is what invariant E11
        requires: a 404 carrying a JSON error body must still reach the
        caller with that body, its headers and its status intact.

        A body failure is *returned* rather than raised for the same
        reason. ``SerializationError`` carries its own 502, so raising it
        from here would overwrite the status the remote side actually sent
        -- a 404 with a truncated body would reach the caller as a 502 and
        the real answer would be gone. The caller raises it once the status
        has been judged.

        The response's media type is read with the same matcher the request
        side uses, so ``application/json; charset=utf-8`` parses and
        nothing else is parsed by accident. A body the response does not
        announce as JSON is left in ``text`` alone; ``json`` stays None,
        which is not an error.

        ``request_tracer`` is filled from ``reported_collectors``, not
        from the wider ``trace_collectors`` the redirect loop writes
        into. The two differ on exactly one path: a caller-supplied
        session, whose own tracers this library now writes to but does
        not put on the envelope. Those collectors belong to an object
        that outlives this call -- a session reused across several calls
        would have every envelope aliasing one mutating dict, so the
        first call's envelope would silently report the fifth call's
        timings. Reporting ``[]`` there keeps the documented contract
        ("tracing this library owns is off") and leaves the caller
        reading their own tracer, which is the object they already hold.

        Args:
            result: What the transport boundary returned.

        Returns:
            The body failure to report, or None when there was none. The
            envelope itself is filled in place.
        """
        self.response['status_code'] = result['status_code']
        self.response['headers'] = redact_headers(result['headers'])
        self.response['cookies'] = redact_cookies(result['cookies'])
        self.response['text'] = result['text']
        self.response['request_tracer'] = self.reported_collectors

        decode_error: Optional[Text] = result.get('decode_error')
        if decode_error is not None:
            return SerializationError(decode_error)

        if not is_json_media_type(media_type_of(result['headers'])):
            return None
        try:
            self.response['json'] = await application_json_response(
                result['text'])
        except SerializationError as err:
            # Caught to be re-raised by the caller, not suppressed: `json`
            # stays None and the caller reports SERIALIZATION, which is
            # what makes a malformed body distinguishable from `{}`.
            return err
        return None

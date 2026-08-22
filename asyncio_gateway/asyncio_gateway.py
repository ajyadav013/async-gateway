"""The library's one public entry point.

It seeds the envelope, dispatches to a protocol strategy, and is the single
place that converts a typed ``AsyncGatewayError`` into an ``ok=False``
envelope. Nothing below it catches broadly and nothing else builds a
failure response, which is what lets a library bug -- a ``KeyError``, a
``TypeError`` -- propagate to the caller instead of being reported as a
failed network call.
"""

import logging
import traceback
from collections.abc import Callable, Collection, Mapping
from typing import Any, Dict, Final, Optional, Tuple
from urllib.parse import urlsplit

from asyncio_gateway.helpers.internal.base import (
    BaseRequestClass,
    validated_port,
)
from asyncio_gateway.logic import protocol_mapping
from asyncio_gateway.utils.envelope import (
    GatewayResponse,
    finalise_error,
    new_envelope,
)
from asyncio_gateway.utils.exceptions import (
    AsyncGatewayError,
    ConfigurationError,
    ProcessorError,
    StackExhaustedError,
)
from asyncio_gateway.utils.redaction import (
    normalise_param_names,
    redact_text,
    redact_url,
    redact_value,
)
from asyncio_gateway.utils.status_map import WARNING_CODES

logger = logging.getLogger(__name__)

# Which URL schemes each protocol will dispatch on. Only the HTTP family
# appears: an FTP or SFTP `url` is a bare host name and has no scheme to
# check, so imposing one would reject every correct call.
#
# `'HTTPS'` allowing only `https` is the whole of H6: the two names used to
# map to the same class with nothing distinguishing them, so a caller who
# explicitly asked for TLS and passed an `http://` URL got silent plaintext.
HTTP_FAMILY_SCHEMES: Final[dict[str, frozenset[str]]] = {
    'HTTP': frozenset({'http', 'https'}),
    'HTTPS': frozenset({'https'}),
}

# Closed scheme allowlists for every selector whose target is a URL. The
# original public constant above is retained because callers and contract
# tests import it; these additive rows extend dispatch without relabelling the
# HTTP family.
PROTOCOL_SCHEME_ALLOWLISTS: Final[dict[str, frozenset[str]]] = {
    **HTTP_FAMILY_SCHEMES,
    'JSONRPC': frozenset({'http', 'https'}),
    'GRAPHQL': frozenset({'http', 'https'}),
    'S3': frozenset({'s3'}),
    'GCS': frozenset({'gs'}),
    'GRPC': frozenset({'grpc', 'grpcs'}),
}

# Every protocol whose `url` is dispatched as a URL, rather than read as
# a bare host name the way FTP's and SFTP's are.
#
# This is a *superset* of `HTTP_FAMILY_SCHEMES`' keys and is deliberately
# a second table rather than another column of the first: the question
# "which schemes may this protocol dispatch on?" and the question "is
# this `url` a URL at all?" have different answers for `'SOAP'`, which
# constrains no scheme and still goes out over `aiohttp`. Scoping the
# protocol-relative check to the allowlist would have refused `//host/p`
# under three protocols and let the fourth reach the same
# `assert port is not None` (F3).
URL_DISPATCHED_PROTOCOLS: Final[frozenset[str]] = (
    frozenset(HTTP_FAMILY_SCHEMES) | frozenset({'SOAP'}))


def resolve_protocol(
    protocol: object,
) -> Tuple[str, type[BaseRequestClass]]:
    """Normalise a caller's protocol name once and find its strategy.

    Once, and in one place: the guard and the registry lookup read the same
    normalised value, rather than the guard normalising and the lookup not
    (H4) -- which let ``protocol='http'`` pass the guard and then die with
    an uncaught ``KeyError`` from a function whose contract is to return a
    dict.

    Args:
        protocol: The caller's ``protocol`` argument, of whatever type they
            actually passed. The signature says ``str``; None and ``123``
            are what arrive in practice, and both are rejected here rather
            than crashing on ``.upper()``.

    Returns:
        The normalised protocol name and the class that implements it.

    Raises:
        ConfigurationError: If ``protocol`` is not a string, or names no
            registered protocol. The message lists what is supported.
    """
    if isinstance(protocol, str):
        name = protocol.strip().upper()
        protocol_class = protocol_mapping.get(name)
        if protocol_class is not None:
            return name, protocol_class
    raise ConfigurationError(
        f'protocol must be one of {sorted(protocol_mapping)}, '
        f'got {protocol!r}')


#: The keyword ``request()`` hands every processor callback. A caller's
#: ``params`` may not also carry it: ``f(response=env, **{'response': x})``
#: is a ``TypeError`` for multiple values, and silently dropping one of the
#: two would mean the callback saw an envelope its caller never chose.
PROCESSOR_RESERVED_KWARG: Final[str] = 'response'


def validated_processor_config(
    config: object,
    *,
    setting: str,
) -> Tuple[Callable[..., Any], Dict[str, Any]]:
    """Return a processor config's callable and params, shape proven.

    The ``validated_*`` family's newest member, and it exists for the
    reason every other one does: ``pre_processor_config['function']`` was
    indexed and awaited with **zero** checking, so a documented public
    parameter (README's argument table; this module's own entry-point
    docstring) was a direct route to a bare builtin. Eighteen of twenty
    hostile shapes a security review drove through the public API escaped
    ``request()`` un-enveloped -- ``KeyError('function')`` for a config
    missing the key, ``TypeError('list indices must be integers or
    slices, not str')`` for a config that is a list, ``TypeError('...
    argument after ** must be a mapping, not str')`` for a non-mapping
    ``params``. A caller cannot be asked to catch three builtin types for
    three spellings of one configuration mistake, and the contract does
    not name any of them.

    Checked here, at the boundary, and called from ``request()`` *outside*
    its one conversion ``try`` -- the placement
    :func:`~asyncio_gateway.helpers.internal.base.validated_protocol_info`
    and :func:`~asyncio_gateway.helpers.internal.base.credentials_of`
    already use, and the settled contract of AGW-35: an unretryable
    caller-configuration mistake raises once, synchronously, rather than
    becoming an ``ok=False`` envelope a retry loop would re-attempt
    forever.

    Both configs are validated *before* either runs, so a malformed
    ``post_processor_config`` is refused before the call is dispatched
    rather than after the remote side has already been contacted. A caller
    whose post-processor config has a typo in it should not discover that
    by having the request go out.

    What is deliberately **not** checked is whether ``function`` returns
    an awaitable, or whether its signature accepts ``response``. Both are
    knowable only by calling it, and a callback that refuses its argument
    or returns a plain value has *run* -- that is
    :class:`~asyncio_gateway.utils.exceptions.ProcessorError` territory, not
    configuration. What is checked is everything decidable without
    calling: the config's shape, the key's presence, the callable's
    callability, the params' mapping-ness, its keys' str-ness, and the
    one collision this library's own call would cause.

    Args:
        config: ``pre_processor_config`` or ``post_processor_config``
            exactly as the caller supplied it, of whatever type they
            actually passed. Only a truthy value reaches here; None and an
            empty mapping are the documented "no processor" call.
        setting: The parameter name to quote in a failure, so a caller
            with both configured is told which one they got wrong.

    Returns:
        The callable to await and a new dict of the keyword arguments to
        award it, copied so a caller mutating their own ``params`` between
        the check and the call cannot change what is passed.

    Raises:
        ConfigurationError: If the config is not a mapping, omits
            ``"function"``, names a ``"function"`` that is not callable,
            carries a ``"params"`` that is not a mapping or whose keys are
            not all ``str``, or whose ``params`` names ``"response"``,
            which this library supplies itself.
    """
    if not isinstance(config, Mapping):
        raise ConfigurationError(
            f'{setting} must be a mapping with a "function" key, got '
            f'{type(config).__name__}')
    if 'function' not in config:
        raise ConfigurationError(
            f'{setting} is missing required key "function"')

    function = config['function']
    if not callable(function):
        raise ConfigurationError(
            f'{setting}["function"] must be callable, got '
            f'{type(function).__name__}')

    params = config.get('params')
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise ConfigurationError(
            f'{setting}["params"] must be a mapping of keyword-argument '
            f'names to values, got {type(params).__name__}')
    for name in params:
        if not isinstance(name, str):
            raise ConfigurationError(
                f'{setting}["params"] keys must be str, since they are '
                f'passed as keyword arguments, got '
                f'{type(name).__name__}')
    if PROCESSOR_RESERVED_KWARG in params:
        raise ConfigurationError(
            f'{setting}["params"] may not name '
            f'{PROCESSOR_RESERVED_KWARG!r}: this library passes the '
            f'envelope under that keyword itself, so supplying it too '
            f'would give the callable two values for one argument')
    return function, dict(params)


async def run_processor(
    function: Callable[..., Any],
    params: Dict[str, Any],
    response: GatewayResponse,
    *,
    setting: str,
) -> Any:
    """Await one caller-supplied processor and return what it produced.

    The counterpart to :func:`validated_processor_config`, and the reason
    the two are separate functions rather than one. That one refuses a
    config that could never work; this one runs a config that *can* and
    reports what happens when the caller's own code fails anyway. Both
    outcomes used to be the same bare builtin escaping ``request()``, and
    collapsing them into one error would tell a caller their configuration
    was wrong when their function was.

    Everything the callable can do wrong is caught here, including the two
    shapes :func:`validated_processor_config` deliberately leaves alone
    because they are undecidable before the call: a function that does not
    accept ``response`` (``TypeError: got an unexpected keyword
    argument``) and one that returns a non-awaitable (``TypeError: 'int'
    object can't be awaited``). Both mean the callback did not honour the
    documented contract, and both arrive as the same
    :class:`~asyncio_gateway.utils.exceptions.ProcessorError` as a callback
    that raised outright.

    ``BaseException`` is not caught. A ``KeyboardInterrupt`` or an
    ``asyncio.CancelledError`` raised inside a caller's callback is not
    that callback failing -- it is the process or the task being torn
    down, and converting either into a gateway error would swallow a
    cancellation this library has no business absorbing.

    Args:
        function: The callable :func:`validated_processor_config`
            approved.
        params: The keyword arguments it approved, already copied.
        response: The envelope, passed under
            :data:`PROCESSOR_RESERVED_KWARG`.
        setting: The parameter name to quote in a failure.

    Returns:
        Whatever the callable's awaited result is, stored verbatim on the
        envelope. This library never inspects it.

    Raises:
        ProcessorError: If the callable raises, refuses the ``response``
            keyword, or returns something that cannot be awaited. The
            original is chained, so its type and message reach the caller
            through the cause chain.
        AsyncGatewayError: Unchanged, if the callback raised one itself. A
            caller who deliberately raises this library's own typed error
            from their callback has said what they want reported, and
            re-wrapping it as a ``ProcessorError`` would bury the code
            they chose.
    """
    try:
        return await function(**{PROCESSOR_RESERVED_KWARG: response},
                              **params)
    except AsyncGatewayError:
        raise
    except Exception as err:
        raise ProcessorError(
            f'{setting}["function"] '
            f'{_processor_name(function)} failed: '
            f'{type(err).__name__}') from err


#: The envelope keys this library **reads back and routes on**, and
#: therefore the ones a processor may not rewrite (NEW-3).
#:
#: Membership is decided by one question: can changing this value make the
#: hook appear to control a transport decision? ``protocol`` is read back for
#: the breaker key. ``url`` is the network destination even though dispatch
#: deliberately reads the original argument; accepting and discarding a URL
#: edit would falsely tell the callback that it retargeted the request.
#:
#: A frozenset rather than a literal at the check, so the set is one
#: named thing a future story can extend when it makes another key
#: routing-relevant, and so the docstring above and the code below
#: cannot disagree about what is frozen.
DISPATCH_CONTROLLING_KEYS: Final[frozenset[str]] = frozenset({
    'protocol',
    'url',
})

#: The named locations accepted by :func:`request`. Kept beside the residual
#: ``**kwargs`` check so its diagnostic cannot advertise an invented option.
REQUEST_ARGUMENT_LOCATIONS: Final[Tuple[str, ...]] = (
    'url',
    'data',
    'auth',
    'protocol',
    'protocol_info',
    'pre_processor_config',
    'post_processor_config',
)


def reject_unknown_request_kwargs(kwargs: Mapping[str, Any]) -> None:
    """Reject residual top-level keywords before any caller code runs.

    Args:
        kwargs: Keywords not bound by the public signature.

    Returns:
        None when there are no residual keywords.

    Raises:
        ConfigurationError: If one or more keywords are unknown. The message
            names keys but never values, which may contain credentials.
    """
    if not kwargs:
        return
    unknown = sorted(kwargs)
    raise ConfigurationError(
        f'request() received unknown keyword argument(s) {unknown}; '
        f'accepted top-level arguments are '
        f'{list(REQUEST_ARGUMENT_LOCATIONS)}. Put transport options in '
        f'protocol_info and callback keyword arguments in the processor '
        f'config "params" mapping')


def checked_envelope(
    response: GatewayResponse,
    *,
    setting: str,
    dispatch: Optional[Mapping[str, Any]] = None,
) -> GatewayResponse:
    """Return ``response`` once a processor has left it whole.

    A processor is handed the *live* envelope so it can reshape payload and
    add caller-owned metadata. Handing over a live mutable object does mean a
    callback can also *remove* from it, and a pre-processor that did was
    the ninth escape found while extending the invariant matrix:
    ``response.clear()``
    or ``del response['payload']`` left ``http_client`` reading
    ``self.response['payload']`` and ``soap_client`` reading it two lines
    into building the SOAP body, both as a bare ``KeyError('payload')``
    from inside a protocol object -- past the boundary and inside the one
    conversion ``try``, where the ``except AsyncGatewayError`` cannot see
    it.

    Checked here, once, rather than by scattering ``.get()`` defaults
    through the protocol clients. Those clients act on an envelope this
    module built and are entitled to assume its key set: making each read
    defensive would spread the invariant across four files and substitute
    a silent ``None`` for the caller's actual payload, dispatching a call
    the caller never asked for. Refusing says what happened instead.

    **Two things are refused: removing any key, and rewriting a
    dispatch-controlling one.** Everything else a processor writes is its
    own business, and deliberately so.

    The line between them is not "what looks dangerous" -- it is whether
    the field describes or controls transport dispatch. Two keys do:
    :data:`DISPATCH_CONTROLLING_KEYS` contains ``protocol`` and ``url``.
    ``BaseRequestClass.__init__`` reads ``response['protocol']`` to build
    the breaker registry's ``(family, host, port)`` key, so a
    pre-processor rewriting it did two things (NEW-3). A non-``str``
    value gave a bare ``AttributeError: 'int' object has no attribute
    'lower'`` from inside ``destination_of``, past the boundary and
    inside the one conversion ``try`` where nothing catches it. Worse, a
    *valid* string silently retargeted the key: an FTP call whose
    processor wrote ``'HTTPS'`` accumulated its failures under
    ``('https', host, 443)`` -- a breaker belonging to **other callers'
    HTTPS traffic to that host**. That is cross-caller state corruption
    rather than a crash, and it is the half a type check alone would
    have left in place.

    Refusing the rewrite is chosen over re-validating it, because there
    is no rewrite worth honouring: the protocol was already resolved from
    the caller's own ``protocol`` argument at the boundary, and a hook
    that could change it would be a second, undocumented way to choose a
    protocol client -- ``resolve_protocol`` running once is what makes
    the value checked and the value dispatched the same value.

    ``url`` is frozen because the old behavior accepted the edit and then
    overwrote it from the original argument. It did not retarget dispatch;
    it only made the callback appear to have done so. Refusing the edit keeps
    one authoritative destination and prevents a future refactor from turning
    the same write into an unvalidated network capability.

    Payload-shaping keys -- ``payload``, ``headers``, ``cookies``, and
    the caller's own stashed state -- stay writable for the same reason:
    nothing routes on them.

    Args:
        response: The envelope the processor was handed and may have
            mutated.
        setting: The parameter name to quote in a failure.
        dispatch: The dispatch-controlling values as this library
            resolved them, to compare the returned envelope against.
            None for the post-processor, which runs after every dispatch
            decision has been made and therefore has nothing left to
            corrupt.

    Returns:
        The same object, unchanged, once every key is still present and
        no dispatch-controlling key has been rewritten.

    Raises:
        ProcessorError: If the callback removed any key the envelope was
            built with, or rewrote a dispatch-controlling one, naming
            them. It is not a ``ConfigurationError`` for the same reason
            a raising callback is not: the config was valid and the
            callback ran.
    """
    missing = sorted(set(GatewayResponse.__annotations__) - set(response))
    if missing:
        raise ProcessorError(
            f'{setting}["function"] removed {missing} from the response '
            f'envelope it was passed. A processor may change what the '
            f"envelope holds, but the key set is this library's "
            f'contract with its caller and the protocol clients read it')
    # Iterated rather than subscripted, and that is a typing constraint
    # rather than a style choice: `GatewayResponse` is a `TypedDict`, so
    # `response[key]` for a non-literal `key` is a mypy error. Walking
    # `.items()` asks the same question -- does any snapshotted field
    # differ now? -- without ever indexing by a computed name.
    resolved = dispatch or {}
    changed = sorted(
        key for key, value in response.items()
        if key in resolved and resolved[key] != value)
    if changed:
        raise ProcessorError(
            f'{setting}["function"] rewrote {changed} on the response '
            f'envelope it was passed. A processor may change what the '
            f'envelope holds, but not the fields this library dispatches '
            f'on: they were resolved from the arguments to request() and '
            f'are read back to route the call, so rewriting one either '
            f'crashes inside a protocol client or silently retargets '
            f"another caller's circuit breaker")
    return response


def _processor_name(function: Callable[..., Any]) -> str:
    """Return a name for ``function`` safe to put in a message.

    ``repr()`` of an arbitrary caller object can be anything at all --
    including a credential, for an auth-ish object with a chatty
    ``__repr__``. A ``__qualname__`` is the function's own name and
    nothing else, and the type name is the fallback for a callable class
    instance or a builtin that has none.

    Args:
        function: The processor callable being described.

    Returns:
        The callable's qualified name, or its type's name.
    """
    name = getattr(function, '__qualname__', None)
    return name if isinstance(name, str) else type(function).__name__


def dispatch_url_for(
    protocol: str,
    url: str,
    *,
    redact_params: Collection[str] = (),
) -> str:
    """Return the URL this call will be dispatched to, scheme enforced.

    A schemeless URL under ``'HTTPS'`` is upgraded rather than rejected:
    the caller named the protocol explicitly and there is exactly one
    scheme that can satisfy it. Under ``'HTTP'`` it is left alone, because
    either scheme would satisfy that and guessing which is not this
    function's call.

    A URL whose scheme parses to something else -- ``ftp://`` under
    ``'HTTP'``, ``file://``, ``javascript:`` -- is rejected rather than
    dispatched. So is ``host:8080/p``, whose ``host`` reads as a scheme:
    the URL is malformed for every protocol here, and rejecting it says so
    where guessing at an intended scheme would not.

    And so is a **protocol-relative** ``//host/p``, which is the one
    schemeless shape "leave it alone" could not survive. It has no
    scheme *and* a real authority, so under ``'HTTP'`` it was passed
    through untouched, and ``aiohttp`` then failed an internal
    ``assert port is not None`` -- a bare ``AssertionError`` escaping
    ``request()`` un-enveloped, which is the same one-conversion-point
    break as the earlier escapes (F3). It cannot be upgraded the way a
    bare ``host/p`` is, either: ``https://` + `//host/p`` is a different
    URL, and RFC 3986 says the scheme is exactly what this reference is
    waiting for. Refusing names what is missing where guessing would
    dispatch somewhere the caller did not ask for.

    Args:
        protocol: The normalised protocol name from :func:`resolve_protocol`.
        url: The URL the call is about to be dispatched to.
        redact_params: The caller's additional sensitive query-parameter
            names, so a rejected URL is named in the exception message
            without its credentials.

    Returns:
        The URL to dispatch, upgraded to ``https://`` where that was the
        only legacy HTTP-family reading. Protocols outside the URL-backed
        selector table get theirs back unchanged.

    Raises:
        ConfigurationError: If the URL's scheme is one this protocol will
            not dispatch on, if a new selector's explicit scheme is absent,
            if it is a protocol-relative reference carrying an authority but
            no scheme, or if the URL cannot be parsed at all. The URL's
            *type* is checked earlier, in ``request()``, because the envelope
            is built from it before this function is reached.
    """
    allowed = PROTOCOL_SCHEME_ALLOWLISTS.get(protocol)
    if allowed is None and protocol not in URL_DISPATCHED_PROTOCOLS:
        return url

    try:
        parts = urlsplit(url)
    except ValueError as err:
        raise ConfigurationError(
            f'url is not parseable: '
            f'{redact_url(url, extra_params=redact_params)}') from err
    scheme = parts.scheme.lower()

    # Checked ahead of the scheme allowlist, and for every protocol that
    # dispatches on a URL rather than only the two with an allowlist.
    # `'SOAP'` has no allowlist -- it accepts whatever scheme the caller
    # gives -- but it reaches the same `aiohttp` call, so it failed the
    # same internal assertion, and scoping this to `HTTP_FAMILY_SCHEMES`
    # would have fixed three of the four protocols that could hit it.
    if not scheme and parts.netloc:
        raise ConfigurationError(
            f'url is a protocol-relative reference, which names an '
            f'authority but no scheme to reach it over; give it one: '
            f'{redact_url(url, extra_params=redact_params)}')
    if allowed is None:
        return url

    if not scheme:
        if protocol not in HTTP_FAMILY_SCHEMES:
            raise ConfigurationError(
                f'protocol {protocol!r} requires an explicit scheme from '
                f'{sorted(allowed)}: '
                f'{redact_url(url, extra_params=redact_params)}')
        return f'https://{url}' if protocol == 'HTTPS' else url
    if scheme not in allowed:
        raise ConfigurationError(
            f'protocol {protocol!r} dispatches only on '
            f'{sorted(allowed)}, but the url has scheme {scheme!r}: '
            f'{redact_url(url, extra_params=redact_params)}')
    return url


def log_failure(
    protocol: str,
    url: str,
    envelope: GatewayResponse,
    exc: AsyncGatewayError,
    redact_query_params: Collection[str] = (),
) -> None:
    """Log one failure, once, at the single conversion point.

    ``warning`` for a remote-side failure a caller may legitimately expect
    -- a 4xx, a Fault, an open circuit -- and ``error`` for a transport or
    configuration failure. The scalar values in ``extra`` go through
    ``redact_value``, so a URL carrying ``?api_key=`` cannot be masked in the
    envelope and in the clear in the log. ``traceback`` goes through
    ``redact_text`` instead, because it is prose containing URLs rather than a
    bare URL: ``redact_value``'s whole-string pass would truncate it at its
    first URL, discarding the rest of the trace.

    The traceback is rendered here and carried as a redacted string rather
    than emitted with ``exc_info=True``. A live ``exc_info`` is formatted by
    whichever handler the *application* installed, from the exception
    objects themselves -- and the chained ``aiohttp`` exception at the
    bottom of a transport failure stringifies to the unredacted URL. There
    is no way to mask that after the fact, so the rendering happens before
    the record leaves this function. The cost is that a handler reading
    ``record.exc_info`` finds nothing; the full traceback text is in
    ``extra['traceback']``.

    Args:
        protocol: The protocol the call was dispatched on.
        url: The URL the call was actually dispatched to, which is the one
            the failure is about -- it may carry a scheme the caller left
            off.
        envelope: The finalised envelope, read for its measured latency.
        exc: The failure being reported.
        redact_query_params: The caller's additional sensitive
            query-parameter names. It is the *same* normalised set the
            envelope was built with: a name the caller declared secret must
            not be masked in the returned ``url`` and written to the log in
            the clear.

    Returns:
        None.
    """
    level = logging.WARNING if exc.code in WARNING_CODES else logging.ERROR
    logger.log(
        level,
        'gateway request failed',
        extra={
            'protocol': redact_value(
                protocol, extra_params=redact_query_params),
            'url': redact_value(url, extra_params=redact_query_params),
            # `code`, `status_code` and `latency` take no set: the first is
            # this library's own wire-stable constant and the other two are
            # numbers, so none of them can be the caller-supplied string
            # `extra_params` exists to mask.
            'code': redact_value(exc.code),
            'status_code': redact_value(envelope['status_code']),
            'latency': redact_value(envelope['latency']),
            'traceback': redact_text(
                ''.join(traceback.format_exception(exc)),
                extra_params=redact_query_params),
        },
    )


async def request(
        url: str,
        data: object = None,
        auth: object = None,
        protocol: str = '',
        protocol_info: Optional[Dict[str, Any]] = None,
        pre_processor_config: Optional[Dict[str, Any]] = None,
        post_processor_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
) -> GatewayResponse:
    """Multiple protocols.

     calls with pre-processor, post processor and retry support.
    :param url: URL to call
    :param data: Selector-owned request data. JSONRPC accepts a mapping, list,
        or None for optional params; GRAPHQL accepts a mapping or None for
        optional variables; GRPC accepts bytes-like data without a serializer
        and arbitrary input when ``request_serializer`` is supplied; SOAP
        accepts XML text or an Element. S3 does not use this argument. GCS
        does not use this argument. Legacy selectors retain their existing
        payload behavior.
    :param protocol: one of the names registered in
        ``asyncio_gateway.logic.protocol_mapping`` -- HTTP, HTTPS, FTP,
        SFTP, SOAP, JSONRPC, GRAPHQL, S3, GCS, or GRPC. Matched with
        surrounding whitespace stripped and without regard to case, so
        'http', ' HTTP ' and 'Http' are the same protocol. HTTPS additionally
        requires that the call go out over TLS; see :raises: below
    :param auth: Optional aiohttp-compatible authentication for HTTP, HTTPS,
        SOAP, JSONRPC, and GRAPHQL, where None sends no credentials. FTP
        requires the legacy non-empty ``.login``/``.password`` fields. SFTP
        accepts ``SFTPAuth`` with a password, explicit client key, or both,
        and retains legacy ``.login``/``.password`` objects. For S3, None
        selects the normal AWS credential chain and a supplied object provides
        non-empty string ``.login``/``.password`` access and secret keys. GCS
        requires ``None``; this selects Application Default Credentials,
        including Workload Identity. GRPC requires None; request credentials
        use bounded metadata.
    :param protocol_info: {
        "request_type": "GET", #required
        "timeout": int, #Optional
        "certificate: "", #Optional,
        "verify_ssl": Boolean, #Optional,
        "cookies": "", #Optional,
        "headers": {}, #Optional,
        "redact_query_params": ["str"], #Optional, further query-parameter
            names whose *values* are masked in the returned envelope's
            `url` and in the failure log. Added to the built-in
            sensitive-name set, never replacing it, and matched
            case-insensitively. A value of the wrong shape is coerced
            rather than raising: it can only ever mask more
        "trace_config": request tracer object list,
            #Optional default is [aiohttp.TraceConfig()]
        "http_file_upload_config" {
            "local_filepath": "required",
            "file_key": "required",
            "delete_local_file": "boolean Optional"
        }, #optional,
        "serialization": callable taking an object and returning str,
            #Optional default serialises with orjson and decodes to str.
            A callable that returns bytes is rejected, see :raises: below
        "circuit_breaker_config": {
            "maximum_failures": "int optional",
            "timeout": "int optional",
            "retry_config": {
                "name": "str required",
                "allowed_retries": "int required",
                "retriable_exceptions": [Optional list of Exceptions],
                list of exception types indicating which
                    exceptions can cause a retry.
                    If None every exception is considered retriable
                "abortable_exceptions": [Optional list of Exceptions],
                    list of exception types indicating which
                    exceptions should abort failsafe run
                    immediately and be propagated out of failsafe. If None, no
                    exception is considered abortable.
                "on_retries_exhausted": Optional callable
                    that will be invoked on a retries exhausted event,
                "on_failed_attempt": Optional callable that
                    will be invoked on a failed attempt event,
                "on_abort": Optional callable that will be
                    invoked on an abort event,
                "delay": int seconds of delay between
                    retries Optional default 0,
                "max_delay": int seconds of max delay between
                    retries Optional default 0,
                "jitter": Boolean Optional, False when you want to
                    keep the wait between calls constant else True
            } #Optional Include this if you want retry
        } #Optional
    }
    :param pre_processor_config: Expects Dict {
        "function": function_address, #required, an async callable
        "params": {
            "param1": value1
        } #optional, a mapping of str keyword names. It may not name
            "response": this library passes the envelope under that
            keyword itself
    } Optional. Both configs are validated *before either runs*, so a
        malformed post-processor config is refused before the call is
        dispatched rather than after the remote side has been contacted.
        The callable is awaited with ``response=<the live envelope>``; it
        may change non-routing values and add caller metadata, but it may
        not rewrite ``url`` or ``protocol`` or remove a required key
    :param post_processor_config: Expects Dict
    {"function": function_address, "params": {"param1": value1}} Optional,
    same shape and same rules
    :param kwargs: Residual keywords are rejected as ``ConfigurationError``
        before processors or transport code. Transport options belong in
        ``protocol_info`` and processor arguments in the config's ``params``.
    :returns GatewayResponse: the same key set for every protocol, on both
        the success and the failure path. Check ``result['ok']`` -- it is
        the only success predicate, and it is False for every failure.
    :raises ConfigurationError: If the caller's own configuration cannot
        form a valid call. That is: a protocol that is not a string, is
        empty, or names nothing registered; a ``protocol_info`` that is
        neither None nor a mapping, or that omits a key the chosen
        protocol requires, such as HTTP's "request_type"; a URL that is
        not a string; a URL whose scheme the chosen protocol will not
        dispatch on, which for ``protocol='HTTPS'`` includes a plain
        ``http://`` URL; "headers" or "cookies" that are not mappings of
        str to str, or that carry a control character a header line
        cannot express; a
        "serialization" value that is not callable or does not return
        str; an HTTP "request_type" naming no verb in the R21 allowlist
        (``UnsupportedVerbError``, a subclass); an
        "http_file_upload_config" combined with a GET, which has no body
        to carry it; every other malformed value the HTTP and SOAP
        constructors check; malformed FTP credentials or SFTP password/key
        credentials which cannot form a connect; malformed JSONRPC
        method/id/params, GRAPHQL query/variables,
        S3 target/auth/command/path/cap, or GRPC target/auth/payload/hooks;
        and unknown or missing required options on any new selector. Note
        that ``auth`` is optional in this signature but **required by FTP
        and SFTP**, with their distinct credential shapes. These are
        programming errors on the caller's side and are not retryable, so they
        escape synchronously rather than becoming an envelope a retry
        loop would re-attempt forever -- and, escaping, they are
        reported to the caller exactly once and are not also logged.
        Every *remote* or *transport* failure, by contrast, is reported
        as an ``ok=False`` envelope.

        **This is not every ``ConfigurationError`` the library can
        produce, and what separates them is placement rather than kind**
        (AGW-35). The rule is the ``try`` below, which is the one
        conversion point: an ``AsyncGatewayError`` raised *before* it
        escapes to the caller, and one raised *inside* it -- that is,
        from within ``handle_request`` -- becomes an ``ok=False``
        envelope carrying ``error['code'] == 'CONFIG'`` and status 400,
        and is logged, because every envelope-producing failure is. The
        list above is the escaping set, not the whole set.

        The four deferred FTP/SFTP option checks are FTP's ``command``
        and ``server_path``, plus SFTP's ``mode`` and ``remote_path`` --
        absent, malformed, or outside the R21 allowlist. They are checked
        once the protocol object is running because ``protocol_info`` is
        optional for those two protocols at this boundary, so the object
        must stay constructible without one and the keys cannot be
        checked in the constructor with everything else.

        All four are nonetheless checked *before* their protocol opens a
        connection, so an unreachable host cannot answer for a caller's
        typo. Getting that ordering wrong is what made an unknown FTP
        ``command`` report ``CONNECT``/502 and an unknown SFTP ``mode``
        report ``HOST_KEY``/495: transport verdicts, carrying a retry
        recommendation, for calls that could never have run.

        S3 credential-provider discovery is a separate runtime case. The
        ambient AWS provider chain can confirm missing or partial
        credentials only during the operation, after construction. That
        failure therefore becomes an ``ok=False`` envelope carrying
        ``error['code'] == 'CONFIG'`` and status 400; it is not one of the
        four deferred FTP/SFTP option checks.

        A caller who wants to handle both alike should catch
        ``ConfigurationError`` *and* branch on
        ``result['error']['code'] == 'CONFIG'``; a caller who only checks
        ``result['ok']`` sees the envelope-borne ones and none of the
        escaping ones.

        A malformed ``pre_processor_config`` or
        ``post_processor_config`` joins the escaping set above: not a
        mapping, no ``"function"`` key, a non-callable ``"function"``, a
        ``"params"`` that is not a mapping of ``str`` keys, or a
        ``"params"`` naming ``"response"``. Both are checked before
        either runs and before anything is dispatched.
    :raises ProcessorError: If a *valid* processor config's callable
        fails -- it raised, it refused the ``response`` keyword, it
        returned something that cannot be awaited, removed a required key,
        or rewrote pre-dispatch ``url`` or ``protocol``. Deliberately
        distinct from ``ConfigurationError``: the configuration was
        accepted and this library called exactly what the caller asked for,
        so the fault is
        in the caller's own function rather than in how they configured
        it. Reports ``PROCESSOR``/500 and chains the original as
        ``__cause__``.

        It escapes rather than becoming an envelope, and for a
        post-processor that means forfeiting the response body. That is
        the accepted cost of the one-conversion-point invariant: an
        ``ok=False`` envelope would dress a bug in the caller's own
        cleanup function up as a failed request and overwrite the
        ``ok=True`` result they were about to read. A callback that must
        not cost its caller the response handles its own failures. A
        callback that raises an ``AsyncGatewayError`` itself is passed
        through untouched, since it has already said what it wants
        reported.
    """
    reject_unknown_request_kwargs(kwargs)

    # `protocol` and `protocol_info` -- including the keys the chosen
    # protocol requires -- are validated here, at the boundary, before an
    # envelope exists and before the caller's own pre-processor is given
    # anything to do. Layers below this one act on what they are handed and
    # do not check it again.
    #
    # The URL's *scheme* is the one exception and is deliberately not
    # checked here; see FI-14 below. Its *type* is checked here, and has
    # to be: `new_envelope` redacts the URL a few lines down, so a
    # non-str reached `redact_url` -- and `yarl.URL` after it -- as a
    # bare `TypeError` ("a bytes-like object is required, not 'str'") or
    # an `AttributeError`, escaping `request()` un-enveloped on every
    # protocol. The annotation says `str`; an annotation is not
    # enforcement, and passing None is a caller's configuration mistake
    # rather than a library bug, so it is reported as one.
    if not isinstance(url, str):
        raise ConfigurationError(
            f'url must be a str, got {type(url).__name__}')

    protocol_name, protocol_class = resolve_protocol(protocol)
    info: Dict[str, Any] = protocol_class.validate_protocol_info(
        protocol_info, protocol=protocol_name)

    # `port` is checked here, at the boundary, with the rest of the
    # `validated_*` family and *outside* the one conversion `try` -- the
    # placement AGW-35 settled, because an unretryable configuration
    # mistake should raise once and synchronously rather than become an
    # `ok=False` envelope a retry loop re-attempts forever.
    #
    # It has to be this early. Every protocol object's constructor
    # reaches `get_breaker(*destination_of(..., info.get('port')))`
    # before it does anything else, and that tuple is a **dict key**, so
    # an unhashable port raised a bare `TypeError` from inside the
    # registry on all five protocols (NEW-2). The return value is
    # discarded rather than written back: `destination_of` and the FTP
    # and SFTP clients read `info['port']` themselves, and re-writing a
    # validated copy into the caller's config is the mutation M12
    # already ruled out.
    validated_port(info.get('port'))

    # Both processor configs, checked here and *both before either runs*.
    # These are documented public parameters and were the last two read
    # with no validation at all: `pre_processor_config['function']` was
    # indexed and awaited raw, so a missing key, a non-callable, a
    # non-mapping config or a non-mapping `params` each escaped
    # `request()` as a bare builtin (NEW-2). Validating the *post* config
    # up here too is the deliberate half: a typo in it is refused before
    # the call is dispatched, rather than after the remote side has been
    # contacted and can no longer be un-contacted.
    pre_processor = (
        validated_processor_config(
            pre_processor_config, setting='pre_processor_config')
        if pre_processor_config else None)
    post_processor = (
        validated_processor_config(
            post_processor_config, setting='post_processor_config')
        if post_processor_config else None)

    if data is None and protocol_name not in {'JSONRPC', 'GRAPHQL'}:
        data = {}

    # The one place caller-supplied redaction config is read, so the one
    # place it is normalised. The envelope, the protocol object's exception
    # messages and the failure log then share the same set by construction
    # rather than by three call sites agreeing.
    redact_query_params = normalise_param_names(
        info.get('redact_query_params'))

    response: GatewayResponse = new_envelope(
        url=url,
        protocol=protocol_name,
        payload=data,
        redact_query_params=redact_query_params,
    )

    if pre_processor is not None:
        # Snapshot *before* the callback runs, so the comparison after it
        # is against what this library resolved rather than against
        # whatever the callback left behind.
        dispatch = {
            key: value for key, value in response.items()
            if key in DISPATCH_CONTROLLING_KEYS}
        response['pre_processor_response'] = await run_processor(
            *pre_processor, response, setting='pre_processor_config')
        # The envelope goes to a protocol client next, which reads keys
        # off it directly. A pre-processor that removed one is refused
        # here, at the boundary, rather than surfacing as a bare
        # `KeyError` from inside the conversion `try` where nothing
        # catches it. URL and protocol edits are also refused because both
        # describe routing, even though the old URL edit was discarded.
        response = checked_envelope(
            response, setting='pre_processor_config', dispatch=dispatch)

    # The scheme check produces the one URL handed to the protocol object.
    # Pre-processors cannot rewrite the destination, so the validated
    # function argument remains the sole authority for transport dispatch.
    target_url = dispatch_url_for(
        protocol_name, url, redact_params=redact_query_params)

    # The envelope was seeded with the caller's `url`, which under
    # `'HTTPS'` may have carried no scheme. It reports what was dispatched,
    # not what was asked for, or a caller reading `result['url']` would see
    # `host/p` for a call that went to `https://host/p` -- and would
    # disagree with the failure log, which is written from `target_url`.
    response['url'] = redact_url(
        target_url, extra_params=redact_query_params)

    protocol_obj = protocol_class(
        target_url, auth, response, info=info,
        redact_params=redact_query_params)

    # The protocol object's own reference point, so `latency` is measured
    # from the same instant whichever of `finalise_ok` and `finalise_error`
    # closes the envelope.
    started = protocol_obj.start_time
    try:
        try:
            response = await protocol_obj.handle_request()
        except RecursionError as err:
            # Named, and *only* this one. A KeyError or a TypeError here
            # is this library's bug and still propagates untouched --
            # that is the invariant, and widening this to `Exception`
            # would restore exactly the blindness the one-conversion-point
            # rule exists to prevent.
            #
            # A RecursionError is different in kind: it is not a mistake
            # in a line of code, it is the interpreter refusing to go
            # further, and *what drives it is the response* -- a hostile
            # 221 KB multipart body of 2000 nesting levels exhausted the
            # stack and this exception escaped `request()` as a bare
            # builtin, where the library's contract is that every failure
            # arrives as an `ok=False` envelope (NEW-H2). Remote input
            # deciding which of those two a caller gets is the defect.
            #
            # The multipart walk that provoked it is now iterative and
            # depth-capped, so nothing known reaches this line. That is
            # the point of keeping it: the *next* unbounded descent --
            # a cause chain, a nested payload, a parser yet to be written
            # -- fails as an envelope rather than as a stack trace, and
            # the contract holds without depending on having found every
            # recursion in advance. Converted rather than re-raised
            # because a caller cannot act on the difference, and a
            # `raise from` here would still be a non-`AsyncGatewayError`
            # escaping the one conversion point.
            raise StackExhaustedError(
                'dispatching this call exhausted the interpreter stack; '
                'the response was abandoned') from err
    except AsyncGatewayError as exc:
        # The one conversion point. Every other exception propagates,
        # deliberately: a KeyError here is this library's bug, not a
        # failed request, and reporting it as one is what hid most of an
        # audit for the life of the package.
        response = finalise_error(
            response, exc, started=started,
            redact_query_params=redact_query_params)
        log_failure(
            protocol_name, response['url'], response, exc,
            redact_query_params)

    if post_processor is not None:
        response['post_processor_response'] = await run_processor(
            *post_processor, response, setting='post_processor_config')
        # Checked on this side too, and for a different reason than the
        # pre-processor's: no protocol client reads the envelope after
        # this point, so nothing here can crash -- but the caller does,
        # and `result['ok']` is the documented and only success
        # predicate (R8-AC2). A post-processor that deleted it would
        # have this function return an object whose shape the library
        # promises and no longer has, which is a broken contract however
        # self-inflicted. Refusing is the honest report.
        #
        # No `dispatch` argument, and that omission is deliberate rather
        # than an oversight (NEW-3). Every dispatch decision has already
        # been made by the time this runs -- the protocol object was
        # built, the breaker was keyed, the call went out -- so there is
        # nothing left for a rewrite to corrupt, and freezing `protocol`
        # here would only stop a caller annotating the result they are
        # about to be handed. The rule is "immutable while it still
        # decides something", not "immutable forever".
        response = checked_envelope(
            response, setting='post_processor_config')
    return response

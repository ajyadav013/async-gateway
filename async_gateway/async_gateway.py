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
from collections.abc import Collection
from typing import Any, Dict, Final, Optional, Tuple, Union
from urllib.parse import urlsplit

from async_gateway.helpers.internal.base import (
    BaseRequestClass,
    validated_protocol_info,
)
from async_gateway.logic import protocol_mapping
from async_gateway.utils.envelope import (
    GatewayResponse,
    finalise_error,
    new_envelope,
)
from async_gateway.utils.exceptions import (
    AsyncGatewayError,
    ConfigurationError,
    StackExhaustedError,
)
from async_gateway.utils.redaction import (
    normalise_param_names,
    redact_text,
    redact_url,
    redact_value,
)
from async_gateway.utils.status_map import WARNING_CODES

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

    Args:
        protocol: The normalised protocol name from :func:`resolve_protocol`.
        url: The URL the call is about to be dispatched to.
        redact_params: The caller's additional sensitive query-parameter
            names, so a rejected URL is named in the exception message
            without its credentials.

    Returns:
        The URL to dispatch, upgraded to ``https://`` where that was the
        only reading. Protocols outside the HTTP family get theirs back
        unchanged.

    Raises:
        ConfigurationError: If the URL's scheme is one this protocol will
            not dispatch on, or if the URL cannot be parsed at all.
    """
    allowed = HTTP_FAMILY_SCHEMES.get(protocol)
    if allowed is None:
        return url

    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError as err:
        raise ConfigurationError(
            f'url is not parseable: '
            f'{redact_url(url, extra_params=redact_params)}') from err

    if not scheme:
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
        data: Optional[Union[Dict, str]] = None,
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
    :param data: Data to be sent in calls
    :param protocol: one of the names registered in
        ``async_gateway.logic.protocol_mapping`` -- HTTP, HTTPS, FTP,
        SFTP. Matched with surrounding whitespace stripped and without
        regard to case, so 'http', ' HTTP ' and 'Http' are the same
        protocol. HTTPS additionally requires that the call go out over
        TLS; see :raises: below
    :param auth: aiohttp.BasicAuth(username, password), or any auth
        object aiohttp accepts. Optional for the HTTP family and SOAP,
        where None means "send no credentials" and is the common case.
        **Required for FTP and SFTP**, which read ``.login`` and
        ``.password`` off it to build their connect: omitted there, the
        call raises ``ConfigurationError`` before anything is dispatched
        rather than crashing on ``None.login`` as it once did (H5)
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
        "function": function_address, #required
        "params": {
            "param1": value1
        } #optional
    } Optional
    :param post_processor_config: Expects Dict
    {"function": function_address, "params": {"param1": value1}} Optional
    :param kwargs: Accepted and ignored. Present so a caller passing a
        keyword this version does not read gets the call it asked for
        rather than a ``TypeError``; every option this library acts on is
        named above or lives inside ``protocol_info``.
    :returns GatewayResponse: the same key set for every protocol, on both
        the success and the failure path. Check ``result['ok']`` -- it is
        the only success predicate, and it is False for every failure.
    :raises ConfigurationError: If the caller's own configuration cannot
        form a valid call. That is: a protocol that is not a string, is
        empty, or names nothing registered; a ``protocol_info`` that is
        neither None nor a mapping, or that omits a key the chosen
        protocol requires, such as HTTP's "request_type"; a URL whose
        scheme the chosen protocol will not dispatch on, which for
        ``protocol='HTTPS'`` includes a plain ``http://`` URL; a
        "serialization" value that is not callable or does not return
        str; an HTTP "request_type" naming no verb in the R21 allowlist
        (``UnsupportedVerbError``, a subclass); an
        "http_file_upload_config" combined with a GET, which has no body
        to carry it; every other malformed value the HTTP and SOAP
        constructors check; and an ``auth`` carrying no string ``login``
        and ``password`` on FTP or SFTP, which cannot form a connect at
        all -- note that ``auth`` is optional in this signature but
        **required by those two protocols**. These are programming
        errors on the caller's side and are not retryable, so they
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

        The configuration errors that arrive the second way are exactly
        the ones a protocol defers by contract, and they are the whole
        of that set: FTP's ``command`` and ``server_path``, and SFTP's
        ``mode`` and ``remote_path`` -- absent, malformed, or outside
        the R21 allowlist. They are checked once the protocol object is
        running because ``protocol_info`` is optional for those two
        protocols at this boundary, so the object must stay
        constructible without one and the keys cannot be checked in the
        constructor with everything else.

        All four are nonetheless checked *before* their protocol opens a
        connection, so an unreachable host cannot answer for a caller's
        typo. Getting that ordering wrong is what made an unknown FTP
        ``command`` report ``CONNECT``/502 and an unknown SFTP ``mode``
        report ``HOST_KEY``/495: transport verdicts, carrying a retry
        recommendation, for calls that could never have run.

        A caller who wants to handle both alike should catch
        ``ConfigurationError`` *and* branch on
        ``result['error']['code'] == 'CONFIG'``; a caller who only checks
        ``result['ok']`` sees the envelope-borne ones and none of the
        escaping ones.
    """
    # `protocol` and `protocol_info` -- including the keys the chosen
    # protocol requires -- are validated here, at the boundary, before an
    # envelope exists and before the caller's own pre-processor is given
    # anything to do. Layers below this one act on what they are handed and
    # do not check it again.
    #
    # The URL's *scheme* is the one exception and is deliberately not
    # checked here; see FI-14 below.
    protocol_name, protocol_class = resolve_protocol(protocol)
    info: Dict[str, Any] = validated_protocol_info(
        protocol_info, required=protocol_class.REQUIRED_INFO_KEYS)

    if data is None:
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

    if pre_processor_config:
        response['pre_processor_response'] = await \
            pre_processor_config['function'](response=response,
                                             **pre_processor_config.get(
                                                 'params',
                                                 {}))

    # FI-14. The scheme check runs *after* the pre-processor and against
    # the value that is then handed to the protocol object -- one variable,
    # so the URL checked and the URL dispatched cannot differ. A
    # pre-processor mutating `response['url']` therefore cannot downgrade
    # an HTTPS call to plaintext. If a later story lets a pre-processor
    # rewrite the dispatched URL, this check moves with it: it has to stay
    # the last thing that touches the URL before dispatch.
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
            protocol_name, target_url, response, exc, redact_query_params)

    if post_processor_config:
        response['post_processor_response'] = \
            await post_processor_config['function'](
                response=response,
                **post_processor_config.get(
                    'params', {}))
    return response

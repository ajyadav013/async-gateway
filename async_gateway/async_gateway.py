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
from typing import Dict, Optional, Text, Union

from async_gateway.logic import protocol_mapping
from async_gateway.utils.envelope import (
    GatewayResponse,
    finalise_error,
    new_envelope,
)
from async_gateway.utils.exceptions import (
    AsyncGatewayError,
    ConfigurationError,
)
from async_gateway.utils.redaction import (
    normalise_param_names,
    redact_text,
    redact_value,
)
from async_gateway.utils.status_map import WARNING_CODES

logger = logging.getLogger(__name__)


def log_failure(
    protocol: Text,
    url: Text,
    envelope: GatewayResponse,
    exc: AsyncGatewayError,
    redact_query_params: Collection[str] = (),
) -> None:
    """Log one failure, once, at the single conversion point.

    ``warning`` for a remote-side failure a caller may legitimately expect
    -- a 4xx, a Fault, an open circuit -- and ``error`` for a transport or
    configuration failure. Every value in ``extra`` goes through
    ``redact_value`` first, so a URL carrying ``?api_key=`` cannot be masked
    in the envelope and in the clear in the log.

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
        url: The URL as the caller supplied it.
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
        url: Text,
        data: Optional[Union[Dict, Text]] = None,
        auth: object = None,
        protocol: Text = '',
        protocol_info: Dict = None,
        pre_processor_config: Dict = None,
        post_processor_config: Dict = None,
        **kwargs
) -> GatewayResponse:
    """Multiple protocols.

     calls with pre-processor, post processor and retry support.
    :param url: URL to call
    :param data: Data to be sent in calls
    :param protocol: values HTTP/HTTPS
    :param auth: aiohttp.BasicAuth(username, password)
        Optional field any auth abject is accepted supported by aiohttp
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
    :returns GatewayResponse: the same key set for every protocol, on both
        the success and the failure path. Check ``result['ok']`` -- it is
        the only success predicate, and it is False for every failure.
    :raises ConfigurationError: If the caller's own configuration cannot
        form a valid call -- an unknown protocol, or a "serialization"
        value that is not callable or does not return str. These are
        programming errors on the caller's side and are not retryable, so
        they escape synchronously rather than becoming an envelope a retry
        loop would re-attempt forever. Both are raised before dispatch: the
        protocol guard here, and the protocol object's own validation while
        it is constructed. Every *remote* or *transport* failure, by
        contrast, is reported as an ``ok=False`` envelope.
    """
    if data is None:
        data = {}

    # The one place caller-supplied redaction config is read, so the one
    # place it is normalised. The envelope, the protocol object's exception
    # messages and the failure log then share the same set by construction
    # rather than by three call sites agreeing.
    redact_query_params = normalise_param_names(
        (protocol_info or {}).get('redact_query_params'))

    response: GatewayResponse = new_envelope(
        url=url,
        protocol=protocol,
        payload=data,
        redact_query_params=redact_query_params,
    )

    if not protocol_mapping.get(protocol.upper()):
        raise ConfigurationError(
            f'protocol must be one of {sorted(protocol_mapping)}, '
            f'got {protocol!r}')

    if pre_processor_config:
        response['pre_processor_response'] = await \
            pre_processor_config['function'](response=response,
                                             **pre_processor_config.get(
                                                 'params',
                                                 {}))

    protocol_class = protocol_mapping[protocol]
    protocol_obj = protocol_class(
        url, auth, response, info=protocol_info,
        redact_params=redact_query_params)

    # The protocol object's own reference point, so `latency` is measured
    # from the same instant whichever of `finalise_ok` and `finalise_error`
    # closes the envelope.
    started = protocol_obj.start_time
    try:
        response = await protocol_obj.handle_request()
    except AsyncGatewayError as exc:
        # The one conversion point. Every other exception propagates,
        # deliberately: a KeyError here is this library's bug, not a
        # failed request, and reporting it as one is what hid most of an
        # audit for the life of the package.
        response = finalise_error(
            response, exc, started=started,
            redact_query_params=redact_query_params)
        log_failure(protocol, url, response, exc, redact_query_params)

    if post_processor_config:
        response['post_processor_response'] = \
            await post_processor_config['function'](
                response=response,
                **post_processor_config.get(
                    'params', {}))
    return response

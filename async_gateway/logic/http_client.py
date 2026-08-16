"""HTTP and HTTPS, the reference implementation of the protocol contract.

Fills the envelope it was handed and returns that same object. Every
failure it can produce is raised as a typed ``AsyncGatewayError`` for the
entry point to convert -- and for a remote-status failure the response is
copied into the envelope *before* the raise, so ``ok=False`` never costs
the caller the body (invariant E11).
"""

import asyncio
import ssl
from collections.abc import Collection
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
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
from async_gateway.utils.constants import HTTP_ERROR_STATUS
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
                call: a "serialization" that is not callable or does not
                return str, or an ``http_file_upload_config`` combined with
                a GET. Both are raised here, in the constructor, because
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
        self.trace_config: List[aiohttp.TraceConfig()] = self.info.get(
            'trace_config', [request_tracer()])
        self.http_file_upload_config: Dict = validated_upload_config(
            self.info.get('http_file_upload_config', {}), self.request_type)
        # No `{}` default: an empty config is a caller asking for a
        # download on every documented default, and absence is the only
        # way left to say "no download at all" (M10).
        self.file_download_config: Optional[Dict] = self.info.get(
            'http_file_download_config')
        self.serialization: JsonSerializer = validated_json_serializer(
            self.info.get('serialization', default_json_serialize))
        self.timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(
            total=self.timeout)

    async def handle_request(self) -> GatewayResponse:
        """Make the network call and fill the envelope with the response.

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
            TransportError: For any other client-side transport failure.
            SerializationError: When the body is not decodable text, or is
                announced as JSON and is not valid JSON -- raised *after*
                the status has been judged, so a malformed body on a 404
                is still reported as the 404 it was.
        """
        async with aiohttp.ClientSession(
            trace_configs=self.trace_config, cookies=self.cookies,
            headers=self.headers, timeout=self.timeout,
            auth=self.auth, json_serialize=self.serialization
        ) as session:
            try:
                result: HttpResult = await handle_http_request(
                    session,
                    self.url,
                    self.request_type,
                    self.circuit_breaker,
                    headers=self.headers,
                    payload=self.response['payload'],
                    certificate=self.certificate,
                    verify_ssl=self.verify_ssl,
                    http_file_download_config=self.file_download_config,
                    http_file_upload_config=self.http_file_upload_config,
                    redact_params=self.redact_params,
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
        self.response['request_tracer'] = [
            tc.results_collector for tc in self.trace_config
        ]

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

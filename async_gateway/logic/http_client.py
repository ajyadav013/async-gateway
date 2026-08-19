"""HTTP and HTTPS, the reference implementation of the protocol contract.

Fills the envelope it was handed and returns that same object. Every
failure it can produce is raised as a typed ``AsyncGatewayError`` for the
entry point to convert -- and for a remote-status failure the response is
copied into the envelope *before* the raise, so ``ok=False`` never costs
the caller the body (invariant E11).
"""

import asyncio
import ssl
from collections.abc import Collection, Mapping, MutableMapping
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import urlsplit

import aiohttp

from failsafe import CircuitOpen, FailsafeError, RetriesExhausted

import orjson

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
    CROSS_ORIGIN_SAFE_HEADERS,
    FORBIDDEN_COOKIE_VALUE_CHARS,
    FORBIDDEN_HEADER_CHARS,
    HTTP_ERROR_STATUS,
    LEGAL_COOKIE_NAME_CHARS,
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
    UnsupportedVerbError,
    faults_of,
    unwrap_cause,
)
from async_gateway.utils.http_file_config import HTTP_VERBS
from async_gateway.utils.redaction import (
    redact_cookies,
    redact_headers,
    redact_url,
)
from async_gateway.utils.request_tracer import (
    begin_trace_scope,
    request_tracer,
)

JsonSerializer = Callable[[Any], str]

# Ordered, because the families overlap: `ServerTimeoutError` is also a
# `ClientConnectionError`, and `ClientConnectorCertificateError` is also a
# `ClientConnectorError`. First match wins, so the most specific
# classification is listed first.
#
# The left column is typed `type[BaseException]` rather than a bare
# `type`, and that is load-bearing rather than tidiness: `TRANSPORT_FAULTS`
# below derives the dispatch's `except` clause from this column, and only
# the narrower annotation lets the type checker prove the derived tuple is
# catchable. A bare `type` admits a non-exception here and says nothing
# until the clause raises `TypeError` at runtime -- on the failure path,
# which is the one place this library must not itself fail.
TRANSPORT_ERRORS: Sequence[
    Tuple[type[BaseException], type[AsyncGatewayError]]] = (
    # BOTH timeout classes, for the reason `logic.ftp_client` and
    # `logic.sftp_client` name theirs: `asyncio.TimeoutError is
    # TimeoutError` only from 3.11, and `requires-python` is `>=3.10`.
    # The fix reached those two tables and not this one (AGW-N1), and
    # this table's failure mode is the worse of the two. It has no
    # residual `OSError` row to mis-file into, so on 3.10 a builtin
    # `TimeoutError` matched nothing at all and `transport_error_for`
    # re-raised it -- a raw `TimeoutError` out of `request()`, out of a
    # library whose whole contract is that only its own bugs escape the
    # envelope. `logic.soap_client` imports this table's derived catch
    # clause, so one omission was two protocols.
    #
    # Measured on a real 3.10.18: `transport_error_for(TimeoutError())`
    # re-raised, where the FTP table's answered `GatewayTimeoutError` /
    # `TIMEOUT`. `circuit_breaker_helper.RETRIABLE_FAILURES` lists the
    # builtin, so the breaker retried and handed over a
    # `RetriesExhausted` whose cause matched no row either.
    #
    # `aiohttp` wraps its *own* timeouts in `ServerTimeoutError`, a
    # subclass of `asyncio.TimeoutError` that the row above already
    # catches -- which is why nothing on the wire produced this and the
    # reachable triggers are a bare builtin from below `aiohttp` or one
    # a caller names in `abortable_exceptions`.
    (asyncio.TimeoutError, GatewayTimeoutError),
    (TimeoutError, GatewayTimeoutError),
    (aiohttp.ClientSSLError, TlsError),
    (ssl.SSLError, TlsError),
    (aiohttp.ClientConnectorDNSError, DnsError),
    (aiohttp.ClientConnectionError, ConnectError),
    (aiohttp.ClientError, TransportError),
)

# There is deliberately **no** `OSError` row here, where
# `logic.ftp_client` and `logic.sftp_client` both have one, and the
# asymmetry is argued rather than left as the gap it used to be
# (NEW-R10-1).
#
# The failure is real and was measured: a download writes through
# `utils.paths.safe_writer`, which runs *inside* the retried callable,
# so a missing parent directory or a full disk arrived at this dispatch
# as an ordinary `OSError`, matched no row, and `raise cause from None`
# handed the caller a raw `FileNotFoundError` out of a library whose
# whole contract is an envelope.
#
# It is fixed one layer down instead, and that placement is the point.
# `utils.paths.classify_refusal` is now **total** -- every `OSError` at
# a write seam gets a class, `LocalWriteError` for the residue -- so the
# fault reaches this module already typed, propagates as the
# `AsyncGatewayError` it is, and `request()`'s one conversion point
# turns it into a `PATH` envelope. Catching it here as well would be
# redundant on the download path and *wrong* on another: an upload's
# pre-dispatch open is a caller-side file problem this library
# deliberately lets escape as its own errno (AGW-38), and a blanket row
# here would silently convert those three documented escapes too.
#
# The two protocol tables keep their row because `aioftp` and
# `asyncssh` can hand their dispatch a socket-level `OSError` directly;
# `aiohttp` wraps every one of those in a `ClientConnectionError`, which
# the row above already names. So the absence is a fact about aiohttp,
# not an oversight -- and `LOCAL_IO` in the cross-protocol fault matrix
# is what holds all four to the same answer regardless of which layer
# each one answers at.

#: The families the HTTP-family dispatch catches, derived from the table
#: above rather than restated beside it.
#:
#: It is derived because restating it is the defect this replaces. The
#: catch clause used to be a hand-written tuple in each of the two modules
#: that dispatch over ``aiohttp`` -- ``(aiohttp.ClientError,
#: asyncio.TimeoutError, ssl.SSLError)`` here and the same tuple *minus*
#: ``ssl.SSLError`` in ``logic.soap_client``. The omission was invisible
#: for as long as every failure arrived wrapped in a ``RetriesExhausted``,
#: which the clause above this one catches; a caller who named
#: ``ssl.SSLError`` in ``abortable_exceptions`` -- a documented public
#: knob -- got it back **unwrapped**, matched no clause, and received a
#: raw ``ssl.SSLError`` where the library's whole contract is an envelope
#: (AGW-R9-1).
#:
#: One name, imported by both modules, is what makes that divergence
#: unrepresentable: a family added to the classification table is caught
#: by every client that maps through it, in the same commit, with no
#: second edit to remember.
#:
#: ``faults_of`` is the shared derivation rather than a local
#: comprehension because the same guarantee is now owed to all four
#: dispatch sites, not just the two that ride ``aiohttp`` -- see
#: :func:`~async_gateway.utils.exceptions.faults_of` (NEW-R10-1).
TRANSPORT_FAULTS: Tuple[type[BaseException], ...] = faults_of(
    TRANSPORT_ERRORS)


def default_json_serialize(obj: Any) -> str:
    """Serialise ``obj`` to a JSON string.

    The default for ``ClientSession(json_serialize=...)``, which requires a
    ``str``-returning callable. ``orjson.dumps`` returns ``bytes``, so it can
    never be passed bare; this wrapper is what makes the migration safe.

    Args:
        obj: Any object ``orjson`` can serialise.

    Returns:
        The JSON encoding of ``obj`` as text.

    Raises:
        ConfigurationError: If ``orjson`` cannot serialise ``obj``.
            ``aiohttp`` invokes this deep inside payload construction, so
            the ``TypeError`` it raises natively escaped ``request()``
            bare -- ``data={'k': object()}`` reached the caller as
            ``TypeError: Type is not JSON serializable: object`` rather
            than as an envelope. It is the caller's own ``data`` that
            cannot be encoded, so it is reported as their configuration.
    """
    try:
        return orjson.dumps(obj).decode()
    except TypeError as err:
        raise ConfigurationError(
            f'data is not JSON-serialisable: {err}') from err


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


def validated_request_type(request_type: Any) -> str:
    """Return ``request_type`` once the HTTP allowlist admits it.

    R21's allowlist decision for the HTTP family, asked here rather than
    at the transport's own ``resolve_verb`` call for the reason R11 gives
    generally: a caller's configuration is judged once, where the
    caller's values are first read. The placement is what decides how the
    caller hears about it. This constructor runs *outside* the entry
    point's ``AsyncGatewayError`` block, so a bad verb escapes
    synchronously and unlogged, before a socket is opened -- which is the
    contract ``request()`` documents for every other configuration error,
    and which the transport-level check cannot honour because by then the
    call is inside the block that turns a raise into an envelope.

    The transport still checks. That is not redundancy: it runs per
    redirect hop against a verb ``after_redirect`` may have rewritten,
    and it also guards the two file paths that never construct an
    ``HttpRequest`` at all.

    Args:
        request_type: ``protocol_info['request_type']`` exactly as the
            caller supplied it, of whatever type they passed.

    Returns:
        The verb unchanged, in the caller's own spelling. Not normalised:
        it is echoed back in ``protocol_details`` and in the
        ``HttpStatusError`` message, and a caller who wrote ``'Post'``
        should read ``'Post'`` there. Every *use* of it lower-cases at
        the point of use, which is what makes the two safe to differ.

    Raises:
        UnsupportedVerbError: If it names no verb in
            :data:`~async_gateway.utils.http_file_config.HTTP_VERBS`.
            ``request_type='close'`` is the documented case: it used to
            resolve to ``ClientSession.close(url, **filters)`` and raise
            a ``TypeError`` that belonged to no transport family and was
            reported as a fabricated status (M25).
    """
    name = (request_type.strip().lower()
            if isinstance(request_type, str) else None)
    if not name or name not in HTTP_VERBS:
        raise UnsupportedVerbError(
            f'protocol_info["request_type"] must name one of '
            f'{sorted(HTTP_VERBS)}, got {request_type!r}')
    return request_type


def validated_upload_config(
    http_file_upload_config: Dict[str, Any],
    request_type: str,
) -> Dict[str, Any]:
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


def validated_download_config(
    http_file_download_config: object,
) -> Optional[Dict[str, Any]]:
    """Return the caller's download config once proven usable, or None.

    The upload config next door has had a validator since R12; this one
    had none at all, and the asymmetry was invisible until the hostile
    ``protocol_info`` matrix grew a row for the key (NEW-R10-1). Two
    shapes escaped ``request()`` un-enveloped as a result: a config that
    is not a mapping earned an ``AttributeError`` on ``.get``, and a
    ``download_filepath`` that is not a path-like earned ``TypeError:
    expected str, bytes or os.PathLike object, not int`` from inside
    ``os.fspath`` -- both from several frames below the caller's
    mistake, in a library whose contract is that only its own bugs
    escape.

    Validated **here**, at construction, for the reason
    :func:`validated_upload_config` gives: a configuration error raised
    before the request goes out reaches the caller as an exception,
    where one raised during it would become an ``ok=False`` envelope a
    retry loop would re-attempt.

    Only the shape is checked, not the destination. Whether the path is
    writable, already taken, or a symlink is decided at the open, by
    ``utils.paths.safe_writer``, against the filesystem as it is *then*
    -- a check here would be a TOCTOU window, which is the defect R22
    exists to close.

    Args:
        http_file_download_config: ``protocol_info``'s
            ``'http_file_download_config'``, of whatever type the caller
            passed, or None when they asked for no download.

    Returns:
        The same config, unmodified, or None. None and ``{}`` stay
        distinguishable: absence means "no download", and an empty
        mapping means "download on every documented default" (M10).

    Raises:
        ConfigurationError: If the config is not a mapping, or names a
            ``download_filepath`` that is not a string.
    """
    if http_file_download_config is None:
        return None
    if not isinstance(http_file_download_config, Mapping):
        raise ConfigurationError(
            f'protocol_info["http_file_download_config"] must be a '
            f'mapping of download settings or absent, got '
            f'{type(http_file_download_config).__name__}')
    filepath = http_file_download_config.get('download_filepath')
    if filepath is not None and not isinstance(filepath, str):
        raise ConfigurationError(
            f'http_file_download_config["download_filepath"] must be a '
            f'string naming the local destination, got '
            f'{type(filepath).__name__}')
    return dict(http_file_download_config)


def validated_cross_origin_headers(
    cross_origin_headers: object,
) -> frozenset[str]:
    """Return the caller's extra cross-origin header names, lower-cased.

    The **escape hatch** on the cross-origin allowlist, and the reason
    the allowlist can afford to be short. Inverting to an allowlist
    (:data:`~async_gateway.utils.constants.CROSS_ORIGIN_SAFE_HEADERS`)
    means the library now decides, on the caller's behalf, that a header
    it has not heard of is not worth the risk of forwarding. That is the
    right default and the wrong absolute: a caller propagating
    ``X-Request-Id`` across a CDN redirect for tracing has a real need,
    knows their own header is not a secret, and would otherwise have to
    give up redirect-following entirely to keep it.

    So the hatch exists, and its shape is what makes it safe to offer:

    * **Opt-in and per call.** Empty by default, so the fail-closed
      position is what a caller gets without asking. Naming a header is
      an explicit, reviewable statement about that one header.
    * **It widens into the unknown region only.** A name in
      :data:`~async_gateway.utils.constants.CREDENTIAL_HEADERS` is
      refused outright rather than honoured. The hatch lets a caller say
      "this header of mine is not a secret"; it does not let them
      say ``Authorization`` is not a secret, because that is not a
      trade-off this library offers at any level of insistence -- and a
      hatch that could re-admit ``Authorization`` would simply be the
      leak again, spelled as a config key.

    A bare ``str`` is rejected first, for the reason
    :func:`validated_allowed_schemes` rejects one: ``frozenset('x-id')``
    is a set of five characters, an "allowlist" that would forward
    nothing the caller meant and would do it silently.

    Args:
        cross_origin_headers: ``protocol_info['cross_origin_headers']``,
            of whatever type the caller passed, or absent.

    Returns:
        The header names, lower-cased. Empty when the caller named none.

    Raises:
        ConfigurationError: If the value is a bare ``str``, is not a
            collection, names anything that is not a ``str``, or names a
            known credential header.
    """
    if cross_origin_headers is None:
        return frozenset()
    if isinstance(cross_origin_headers, str):
        raise ConfigurationError(
            f'protocol_info["cross_origin_headers"] must be a collection '
            f'of header names, not the single string '
            f'{cross_origin_headers!r}: a str iterates as its characters, '
            f'so this would forward '
            f'{sorted(frozenset(cross_origin_headers))} and no real header')
    if not isinstance(cross_origin_headers, Collection):
        raise ConfigurationError(
            f'protocol_info["cross_origin_headers"] must be a collection '
            f'of header names, got {type(cross_origin_headers).__name__}')
    names: List[str] = []
    for name in cross_origin_headers:
        if not isinstance(name, str):
            raise ConfigurationError(
                f'protocol_info["cross_origin_headers"] must name headers '
                f'as str, got {type(name).__name__} {name!r}')
        names.append(name.lower())
    refused = sorted(frozenset(names) & CREDENTIAL_HEADERS)
    if refused:
        raise ConfigurationError(
            f'protocol_info["cross_origin_headers"] names the credential '
            f'header(s) {refused}, which are never forwarded across an '
            f'origin boundary. The key widens the cross-origin allowlist '
            f'to headers this library does not recognise; it cannot '
            f're-admit one it recognises as a credential, because a '
            f'hostile Location would receive it')
    return frozenset(names)


def validated_session(
    session: object,
    cross_origin_forward: frozenset[str] = frozenset(),
) -> Optional[aiohttp.ClientSession]:
    """Return the caller's session once proven usable, or None.

    A session the caller supplies is *theirs*: it is used and never closed,
    which is what makes connection pooling across calls possible at all.
    That only works if the object really is a live session, so the ways it
    can fail are rejected here rather than several frames into
    ``aiohttp``. A closed session otherwise surfaces as
    ``RuntimeError: Session is closed`` from inside the transport, which
    reads as a network failure and is retried like one.

    A session carrying **anything the cross-origin allowlist would drop**
    is refused for a different and sharper reason. ``aiohttp`` merges a
    session's default headers and ``auth`` into every request it issues,
    underneath the ones this call passes, and the redirect loop can only
    withhold what it passes. A
    ``ClientSession(headers={'Authorization': ...})`` would therefore send
    that token to whatever host a hostile ``Location`` named -- a leak
    ``aiohttp``'s own loop did not have, introduced by taking the loop
    over.

    This refuses on the **allowlist**, not on the credential list, and
    the difference is the whole point of NEW-H1b. Checking session
    defaults against a list of known credentials left the same hole on
    this path that the hop had: a session carrying ``X-Vault-Token`` --
    or any header no list names -- passed validation and then leaked,
    because no hop can withhold a session default. Refusing whatever the
    hop itself would drop keeps the two surfaces answering one question,
    so a header added to the allowlist is admitted here automatically and
    one that is not is refused here for the same reason it does not
    cross.

    Args:
        session: ``protocol_info['session']``, of whatever type the caller
            actually passed, or None when they passed nothing.
        cross_origin_forward: The names
            ``protocol_info['cross_origin_headers']`` declared safe to
            cross, already validated. A session default may carry one of
            these, because the caller has said so about that header.

    Returns:
        The same session, or None when the library is to create its own.

    Raises:
        ConfigurationError: If the value is neither None nor a live
            ``aiohttp.ClientSession``, or if it carries a header the
            cross-origin allowlist would drop, or a session-level
            ``auth``.
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
    allowed = CROSS_ORIGIN_SAFE_HEADERS | cross_origin_forward
    carried = sorted({
        name.lower()
        for name in session.headers
        if name.lower() not in allowed
    })
    if carried:
        credentials = sorted(frozenset(carried) & CREDENTIAL_HEADERS)
        detail = (
            f'credential header(s) {credentials}' if credentials
            else f'header(s) {carried}, which are not on the cross-origin '
                 f'allowlist')
        raise ConfigurationError(
            f'protocol_info["session"] carries the {detail} as session '
            f'defaults, which aiohttp merges into every request and no '
            f'redirect hop can withhold; a hostile Location would receive '
            f'them. Pass them in protocol_info["headers"] instead, which '
            f'this library drops when a hop crosses an origin -- or, for a '
            f'header of yours that is not a secret, name it in '
            f'protocol_info["cross_origin_headers"] to forward it '
            f'deliberately')
    if session.auth is not None:
        raise ConfigurationError(
            'protocol_info["session"] carries a session-level auth, which '
            'aiohttp merges into every request and no redirect hop can '
            'withhold; a hostile Location would receive it. Pass the auth '
            'argument of request() instead, which this library strips '
            'when a hop crosses an origin')
    return session


def _checked_header_text(
    value: object,
    *,
    setting: str,
    part: str,
) -> str:
    """Return one header name or value once proven safe to serialise.

    The shared half of :func:`validated_headers`, applied to a name and
    to a value alike, because ``aiohttp``'s serialiser applies the same
    rule to both: :data:`FORBIDDEN_HEADER_CHARS` is the exact character
    class ``aiohttp.http_writer._safe_header`` refuses, so what this
    admits is what reaches the wire and nothing more.

    Args:
        value: The name or the value, of whatever type the caller passed.
        setting: The ``protocol_info`` key it came from, named in the
            failure so the caller is told which of their own keys to fix.
        part: ``'name'`` or ``'value'``, to say which half is at fault.

    Returns:
        The text unchanged. Not normalised: a header name is echoed back
        in the envelope, and case is the caller's to choose.

    Raises:
        ConfigurationError: If it is not a ``str``, or carries a
            character no header may. The offending character is named by
            ordinal rather than quoted raw, so a CR in a message cannot
            split a log line the way it would have split the request.
    """
    if not isinstance(value, str):
        raise ConfigurationError(
            f'protocol_info[{setting!r}] must map str to str, got a '
            f'{part} of type {type(value).__name__}')
    for char in value:
        if char in FORBIDDEN_HEADER_CHARS:
            raise ConfigurationError(
                f'protocol_info[{setting!r}] {part} may not contain the '
                f'control character U+{ord(char):04X}: it would end the '
                f'header line early and let the rest be read as headers '
                f'of its own')
    return value


def validated_headers(
    headers: object,
    *,
    setting: str = 'headers',
) -> Dict[str, str]:
    """Return the caller's request headers once proven safe to send.

    The caller-configuration half of a defect that had nothing to do
    with injection succeeding. A CR or an LF in a header *is* refused --
    by ``aiohttp``, correctly, in
    ``aiohttp.http_writer._safe_header`` -- but it is refused with a
    bare ``ValueError``, and that refusal happens at *serialisation*,
    which is after the connection is open. So the exception a caller saw
    for one and the same mistake depended on whether the host answered:
    an unreachable host produced a ``CONNECT`` envelope, because the
    connect failed first and the headers were never serialised, while a
    reachable one produced a ``ValueError`` escaping ``request()``
    un-enveloped. Remote reachability deciding which of those a caller
    gets is precisely the nondeterminism the one-conversion-point
    contract exists to remove (N6).

    Checked here, at construction, and therefore before any socket is
    opened -- the placement every other caller-configuration check in
    this module uses, and the reason the answer no longer depends on the
    network. The non-``str`` shapes are refused in the same pass and for
    the same reason: ``{'X': 1}`` reached ``aiohttp`` as a ``TypeError``
    and ``{1: 'v'}`` as an ``AttributeError``, both bare, both from the
    same key, and a caller cannot be asked to catch three exception
    types for three spellings of one configuration mistake.

    Args:
        headers: ``protocol_info['headers']``, of whatever type the
            caller passed, or absent.
        setting: The key name to quote in a failure. SOAP passes its own,
            because it merges the caller's mapping with headers of its
            own and needs the message to name the caller's key.

    Returns:
        A new dict of the caller's headers, unmodified. Empty when they
        supplied none, which is the documented default call.

    Raises:
        ConfigurationError: If the value is not a mapping, or names a
            header whose name or value is not a ``str`` or carries a
            character no header may.
    """
    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise ConfigurationError(
            f'protocol_info[{setting!r}] must be a mapping of header '
            f'names to values, got {type(headers).__name__}')
    return {
        _checked_header_text(name, setting=setting, part='name'):
        _checked_header_text(value, setting=setting, part='value')
        for name, value in headers.items()
    }


def validated_cookies(cookies: object) -> Optional[Dict[str, str]]:
    """Return the caller's cookies once proven safe to send.

    The same defect as :func:`validated_headers`, one key over, and
    reached through a different library: a cookie is serialised by
    ``http.cookies``, which refuses a control character with a
    ``CookieError`` and an illegal name with the same, and refuses both
    only once the request is being built. ``{1: 'v'}`` did not even get
    that far -- it raised a bare ``AttributeError`` on ``.lower()``, and
    a bare ``'nope'`` raised ``ValueError: not enough values to unpack``,
    which tells a caller nothing about the key they got wrong.

    ``None`` is preserved rather than normalised to ``{}``: ``aiohttp``
    distinguishes them -- ``cookies=None`` leaves the session's own cookie
    jar to answer, ``cookies={}`` overrides it with nothing -- and this
    library has no business collapsing that difference.

    Args:
        cookies: ``protocol_info['cookies']``, of whatever type the
            caller passed, or absent.

    Returns:
        A new dict of the caller's cookies, or None when they supplied
        none.

    Raises:
        ConfigurationError: If the value is not a mapping, or names a
            cookie whose name or value is not a ``str``, or whose name is
            not a legal cookie name, or whose value carries a control
            character.
    """
    if cookies is None:
        return None
    if not isinstance(cookies, Mapping):
        raise ConfigurationError(
            f'protocol_info["cookies"] must be a mapping of cookie names '
            f'to values, got {type(cookies).__name__}')
    checked: Dict[str, str] = {}
    for name, value in cookies.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ConfigurationError(
                f'protocol_info["cookies"] must map str to str, got '
                f'{type(name).__name__} -> {type(value).__name__}')
        if not name or not set(name) <= LEGAL_COOKIE_NAME_CHARS:
            raise ConfigurationError(
                f'protocol_info["cookies"] name {name!r} is not a legal '
                f'cookie name: a name is a token, so a space, a comma, a '
                f'semicolon or a control character cannot appear in one')
        for char in value:
            if char in FORBIDDEN_COOKIE_VALUE_CHARS:
                raise ConfigurationError(
                    f'protocol_info["cookies"] value for {name!r} may not '
                    f'contain the control character U+{ord(char):04X}: it '
                    f'would end the Cookie header early and let the rest '
                    f'be read as headers of its own')
        checked[name] = value
    return checked


def validated_http_auth(auth: object, url: str) -> object:
    """Return the caller's ``auth`` once proven usable on this call.

    The HTTP family's counterpart to
    :func:`~async_gateway.helpers.internal.base.credentials_of`, which
    FTP and SFTP already run for the same reason: ``auth`` is documented
    optional and typed ``object``, so whatever the caller passed reaches
    ``aiohttp`` unexamined. Two shapes crashed there, both bare, and
    both found by the entry-point invariant matrix:

    * anything that is not a ``BasicAuth`` raised
      ``TypeError: BasicAuth() tuple is required instead``, from inside
      ``ClientSession._request``;
    * a ``BasicAuth`` combined with a URL that already carries
      ``user:password@`` raised ``ValueError: Cannot combine AUTH
      argument with credentials encoded in URL`` -- a genuine ambiguity
      about *which* credential the caller meant, which is worth
      refusing, but not as an untyped exception.

    ``None`` is admitted: unlike FTP and SFTP, an HTTP call without
    credentials is the ordinary case rather than an impossible one.

    Args:
        auth: The caller's ``auth`` argument, of whatever type.
        url: The URL this call dispatches to, checked for embedded
            credentials so the conflict is named before dispatch.

    Returns:
        The same object, unmodified.

    Raises:
        ConfigurationError: If ``auth`` is neither None nor an
            ``aiohttp.BasicAuth``, or if it is combined with a URL that
            already carries credentials.
    """
    if auth is None:
        return None
    if not isinstance(auth, aiohttp.BasicAuth):
        raise ConfigurationError(
            f'auth must be an aiohttp.BasicAuth or None, got '
            f'{type(auth).__name__}')
    if '@' in urlsplit(url).netloc:
        raise ConfigurationError(
            'auth cannot be combined with credentials already encoded in '
            'the url: aiohttp refuses the pair rather than choosing '
            'between them. Pass one or the other')
    return auth


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


def validated_allowed_schemes(allowed_schemes: object) -> frozenset[str]:
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
    names: List[str] = []
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
    *,
    redact_params: Collection[str] = (),
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
        redact_params: The caller's additional sensitive query-parameter
            names, handed to the tracer this library builds so its
            exception report masks them. A tracer the *caller* built
            cannot be given them -- it already exists -- which is the
            documented cost of supplying one.

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
        return [request_tracer(redact_params=redact_params)]
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
) -> List[MutableMapping[str, Any]]:
    """Bind and return the collector mappings this call traces into.

    Derived once, in one place, so the collectors the redirect loop
    writes into are provably the same objects the envelope reports --
    they are literally the same mappings, not two reads of one source.

    It **binds** rather than merely reads, and that is what makes the
    results per-request. :func:`begin_trace_scope` puts a fresh mapping
    in the running task's context for each of this
    library's tracers and hands it straight back, so a tracer reused
    across two concurrent calls yields two mappings and neither call can
    see the other's events (H17). It must therefore be called from
    inside the coroutine that will make the request -- a context set in
    ``__init__`` belongs to whoever constructed the object, not to the
    task that awaits it.

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
        One mapping per usable tracer, in the order the tracers were
        given: this call's freshly bound scope for a tracer this library
        built, and the collector itself for one it did not.
    """
    if session is None:
        return begin_trace_scope(trace_config)
    attached: Iterable[Any] = getattr(session, 'trace_configs', ())
    return begin_trace_scope([
        tracer for tracer in attached
        if isinstance(tracer, aiohttp.TraceConfig)
    ])


def transport_error_for(
    err: BaseException,
    *,
    redact_params: Collection[str] = (),
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
    REQUIRED_INFO_KEYS: ClassVar[frozenset[str]] = frozenset(
        {'request_type'})

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build an HTTP request from a validated ``protocol_info``.

        Every caller-supplied value this protocol accepts is validated
        here, at construction, rather than at dispatch: a configuration
        error raised before the request goes out escapes to the caller as
        an exception, where one raised during it would become an
        ``ok=False`` envelope a retry loop would re-attempt.

        Args:
            *args: Forwarded verbatim to :class:`BaseRequestClass` --
                ``url``, ``auth``, ``response`` and ``info``, in that
                order.
            **kwargs: Forwarded verbatim to the base, which requires
                ``redact_params`` keyword-only.

        Raises:
            ConfigurationError: If ``protocol_info`` cannot form a valid
                call: a "serialization" that is not callable, does not
                return str, or is combined with a "session"; a
                "trace_config" combined with a "session", or that is not
                a collection of ``aiohttp.TraceConfig`` each carrying a
                ``results_collector``; an
                ``http_file_upload_config`` combined with a GET; a
                "session" that is not a live ``aiohttp.ClientSession`` or
                that carries a header the cross-origin allowlist would
                drop or a session-level
                ``auth``; a "max_response_bytes" that is not a positive
                int; an "allow_redirects" that is not a bool; a
                "max_redirects" that is not a non-negative int; a
                "timeout" that is not a positive number of seconds; an
                "allowed_schemes" that is not a non-empty collection of
                scheme names; a "cross_origin_headers" that is not a
                collection of header names, or that names a known
                credential header; a "headers" that is not a mapping of
                str to str or that carries a control character; or a
                "cookies" that is not a mapping of str to str, names an
                illegal cookie name, or carries a control character in a
                value. ``UnsupportedVerbError`` -- a
                ``ConfigurationError`` -- for a "request_type" naming no
                verb in the R21 allowlist. All are raised here, in the
                constructor, because
                the entry point builds the protocol object *outside* the
                block that converts an ``AsyncGatewayError`` into an
                envelope -- so a configuration error raised here escapes to
                the caller unlogged and before anything is dispatched,
                which is the contract ``request()`` documents.
        """
        super(HttpRequest, self).__init__(*args, **kwargs)

        self.request_type: str = validated_request_type(
            self.info['request_type'])
        self.auth = validated_http_auth(self.auth, self.url)
        self.cookies: Optional[Dict[str, str]] = validated_cookies(
            self.info.get('cookies'))
        self.headers: Dict[str, str] = validated_headers(
            self.info.get('headers'))
        self.verify_ssl: bool = self.info.get('verify_ssl', True)
        self.http_file_upload_config: Dict = validated_upload_config(
            self.info.get('http_file_upload_config', {}), self.request_type)
        # No `{}` default: an empty config is a caller asking for a
        # download on every documented default, and absence is the only
        # way left to say "no download at all" (M10).
        self.file_download_config: Optional[Dict] = (
            validated_download_config(
                self.info.get('http_file_download_config')))
        self.timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(
            total=validated_timeout(self.timeout))
        # Read before the session, which is validated *against* it: a
        # session default naming one of these is admissible precisely
        # because the caller has declared that header safe to forward.
        self.cross_origin_forward: frozenset[str] = (
            validated_cross_origin_headers(
                self.info.get('cross_origin_headers')))
        # None means "build one and close it"; anything else is the
        # caller's and is never closed here. Read before the serialiser,
        # which cannot be honoured on a session this library did not
        # create and is refused with one.
        self.session: Optional[aiohttp.ClientSession] = validated_session(
            self.info.get('session'), self.cross_origin_forward)
        self.serialization: JsonSerializer = validated_serialization(
            self.info, self.session)
        self.trace_config: List[aiohttp.TraceConfig] = validated_trace_config(
            self.info, self.session, redact_params=self.redact_params)
        # Both collector lists are *bound per call*, in `_exchange`, not
        # here. A tracer is a session-level object a caller may reuse
        # across concurrent calls, so the mapping this call's results go
        # into cannot be chosen at construction: `begin_trace_scope`
        # binds a fresh one inside the running task, whose context is its
        # own copy (H17). Empty until then, so a call that raises before
        # dispatch still reports `[]` rather than a stale mapping.
        #
        # `trace_collectors` is what the redirect loop writes its trace
        # event into: this call's mappings for the tracers this library
        # attaches, or -- for a caller-supplied session -- the ones
        # already on it, which is the route `validated_trace_config`'s
        # refusal names. `reported_collectors` is what the *envelope*
        # reports, deliberately narrower: only the tracers this library
        # attached, so a supplied session still reports `[]`.
        self.trace_collectors: List[MutableMapping[str, Any]] = []
        self.reported_collectors: List[MutableMapping[str, Any]] = []
        self.max_response_bytes: int = validated_max_response_bytes(
            self.info.get('max_response_bytes', MAX_RESPONSE_BYTES))
        self.allow_redirects: bool = validated_allow_redirects(
            self.info.get('allow_redirects', True))
        self.max_redirects: int = validated_max_redirects(
            self.info.get('max_redirects', MAX_REDIRECTS))
        self.allowed_schemes: frozenset[str] = validated_allowed_schemes(
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
        # Bound here, inside the coroutine, and deliberately not in
        # `__init__`: `trace_collectors_for` binds this call's trace
        # results into the *running task's* context, and a task started
        # by `asyncio.gather` gets its own copy of that context. Binding
        # at construction would put every concurrent call's results in
        # whichever context happened to build the objects -- which is
        # exactly the shared mapping H17 is about.
        self.trace_collectors = trace_collectors_for(
            self.session, self.trace_config)
        # Narrower on purpose: only the tracers this library attached, so
        # a caller-supplied session reports `[]`. Read from the same
        # bind, so the two lists cannot disagree about which mapping this
        # call filled.
        self.reported_collectors = (
            [] if self.session is not None else list(self.trace_collectors))
        # Put them on the envelope *now*, not after a successful read.
        # These are the live mappings the callbacks write into, so the
        # envelope tracks them in place -- which is the only way a
        # *failed* call carries a trace at all. Assigning in
        # `_copy_into_envelope` meant every failure reported `[]`,
        # including the connection error whose `on_request_exception` is
        # the one event M20 is about: the tracer recorded it faithfully
        # and the envelope threw it away.
        self.response['request_tracer'] = self.reported_collectors
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
                cross_origin_forward=self.cross_origin_forward,
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
        except TRANSPORT_FAULTS as err:
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

        ``request_tracer`` is **not** written here. It is assigned in
        :meth:`handle_request`, at the moment the trace scope is bound,
        because the mappings are live: the callbacks keep writing into
        them and the envelope tracks them in place. Assigning here
        instead made the key reachable only on the path that got a
        response back, so every failed call reported ``[]`` -- including
        the connection error whose ``on_request_exception`` the tracer
        had recorded correctly and the envelope then discarded.

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

        decode_error: Optional[str] = result.get('decode_error')
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

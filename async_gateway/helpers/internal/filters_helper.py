"""The request-side filter table, and the SSL config both sides share.

Which filter builds a request's ``aiohttp`` keyword arguments is decided by
the media type of the caller's ``Content-Type``, resolved by the one matcher
in ``helpers/internal/__init__.py`` and applied here. The table is named in
the release spec (R12) rather than left to the code, and its default is
deliberately *not* "the JSON filter for everything unknown": that entry is
what sent a SOAP envelope through a JSON encoder.

Every filter takes the same explicit, typed parameter set. None of them
reads ``kwargs['request_type']`` -- a hard ``KeyError`` for any caller that
did not supply one, which is every SOAP call -- and none of them writes to
the payload it was handed.

The other half of this module is :func:`get_ssl_config`, which decides the
TLS posture of every outbound call (R23). Three defects lived in six lines
of it and are repaired together, because each one hid the next:

* **H28.** The client context was built for the *client-auth* purpose --
  the one a **server** uses to authenticate incoming clients. It yields
  ``PROTOCOL_TLS_SERVER``, and CPython refuses to build a client socket
  from one, so the client-certificate path failed **closed** and had never
  once succeeded. The purpose names the peer being *authenticated*: on an
  outbound call that is the server, so ``ssl.Purpose.SERVER_AUTH`` is
  correct and the other one reads as the opposite of what it means. (It
  is spelled out here rather than written literally so that R23-AC1's
  grep over this package stays meaningful; ``logic/ftp_client.py``
  retains two prose mentions that predate this story.)
* **M1.** ``{'ssl': verify_ssl or True}`` is ``True`` for every input,
  including ``False``, so the documented flag was inoperative. The posture
  was safe by accident, and the obvious cleanup -- ``{'ssl': verify_ssl}``
  -- would have turned that accident into a live downgrade switch for
  every caller passing ``None``. It is replaced by an explicit three-way
  decision, not by a tidier expression.
* **AGW-36.** Both ``ssl.create_default_context`` (which reads the whole
  system CA bundle, 194 certificates) and ``load_cert_chain`` (which reads
  the caller's two files) are blocking filesystem reads, and both ran
  directly on the event loop -- on HTTP inside ``failsafe.run``, so once
  per *attempt*. They now run in :func:`build_client_ssl_context`, a plain
  ``def`` reached only through ``asyncio.to_thread``.
"""

import asyncio
import logging
import os
import ssl
from collections.abc import Sequence
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Tuple, Union

import aiohttp

import orjson

from async_gateway.utils.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

#: ``aiohttp`` query parameters as pairs rather than as a mapping: a list
#: value means a repeated parameter and a mapping cannot express one.
QueryParams = List[Tuple[str, str]]

#: The keyword arguments a filter contributes to the transport call.
RequestFilters = Dict[str, Any]

#: What a payload may be by the time it reaches a filter.
Payload = Optional[Union[Dict[str, Any], str, bytes]]

#: What :func:`get_ssl_config` answers with. Always exactly one key --
#: ``'ssl'``, the keyword ``aiohttp`` supports -- carrying either a
#: verifying client context or the bare flag. The deprecated
#: ``ssl_context=`` key this replaces is emitted by no branch (R23-AC3).
SslFilters = Dict[str, Union[bool, ssl.SSLContext]]

#: One end of the caller's ``certificate`` pair, before normalisation.
#: ``os.PathLike`` is accepted because ``load_cert_chain`` takes one
#: natively, so refusing a ``pathlib.Path`` would invent a restriction
#: OpenSSL does not have.
CertificatePath = Union[str, os.PathLike]


class _PassphraseProtectedKey(Exception):
    """Raised from the ``password`` callback when OpenSSL asks for one.

    OpenSSL calls the ``password`` callback only for a key it cannot
    decode unaided, so the callback firing *is* the detection: it is never
    invoked for an unencrypted key. Without it, an encrypted key surfaces
    as ``ssl.SSLError: [SSL] PEM lib``, which is the same message a
    corrupt PEM produces and tells the caller nothing about what to fix.

    Private, and never escapes this module: :func:`build_client_ssl_context`
    converts it to a ``ConfigurationError`` naming the limitation.
    """


def _refuse_passphrase() -> str:
    """Report that a passphrase-protected key was supplied.

    Returns:
        Never; the annotation is the signature ``load_cert_chain``
        requires of a ``password`` callable.

    Raises:
        _PassphraseProtectedKey: Always. Being called at all means
            OpenSSL could not decode the key without a passphrase.
    """
    raise _PassphraseProtectedKey()


def normalised_certificate(
    certificate: Any,
) -> Tuple[str, str]:
    """Return the caller's ``certificate`` value as a ``(cert, key)`` pair.

    Every rejection here is the caller's typo rather than a transport
    fault, so all of them raise ``ConfigurationError`` -- 400, never
    retried -- instead of reaching OpenSSL and coming back as a TLS
    failure that suggests the network is at fault.

    The rejections are ordered by *type* before *length*, because the
    length test alone is actively misleading for the two types that have
    one. ``b'ab'`` has length 2 and iterates as two integers; a 12-character
    path string has length 12. Reporting "got 12 value(s)" for
    ``certificate='/path/to/cert.pem'`` names the wrong problem, so the
    type is named instead.

    Args:
        certificate: Whatever the caller put in ``protocol_info``. Any
            type, because it arrives unvalidated.

    Returns:
        The certificate path and the key path, both as ``str``. An
        ``os.PathLike`` element is converted with ``os.fspath``, which is
        pure string manipulation and touches no filesystem.

    Raises:
        ConfigurationError: If the value is a single path rather than a
            pair, is not a sequence at all, does not hold exactly two
            elements, or holds an element that is not a path.
    """
    if isinstance(certificate, (str, bytes, os.PathLike)):
        raise ConfigurationError(
            "protocol_info['certificate'] must be a (certificate path, "
            f'key path) pair, not a single '
            f'{type(certificate).__name__}')
    if not isinstance(certificate, Sequence):
        raise ConfigurationError(
            "protocol_info['certificate'] must be a (certificate path, "
            f'key path) pair, not a {type(certificate).__name__}')
    if len(certificate) != 2:
        raise ConfigurationError(
            "protocol_info['certificate'] must be a (certificate path, "
            f'key path) pair; got {len(certificate)} value(s)')

    paths: List[str] = []
    for label, value in zip(('certificate path', 'key path'), certificate):
        if not isinstance(value, (str, os.PathLike)):
            raise ConfigurationError(
                f"protocol_info['certificate'] {label} must be a path, "
                f'not a {type(value).__name__}')
        paths.append(os.fspath(value))
    return paths[0], paths[1]


def build_client_ssl_context(
    certificate_path: str,
    key_path: str,
) -> ssl.SSLContext:
    """Build the verifying client context that carries a client cert.

    A plain ``def``, deliberately: both calls in it read files
    synchronously, so this is the module-level blocking helper
    ``tests/test_no_blocking_io.py`` describes as the *intended* contract
    for work handed to an executor. :func:`get_ssl_config` is the only
    caller and reaches it through ``asyncio.to_thread``. Rewriting it as
    an ``async def`` puts a banned ``ssl.`` call back inside a coroutine
    and that scan fails -- which is the check that keeps this placement
    from quietly regressing (AGW-36).

    ``ssl.create_default_context`` is *outside* the ``try`` on purpose. It
    reads the system CA bundle, which is the environment's problem and not
    the caller's; failing it is a transport-level fault the protocol
    clients report as ``TLS``. Everything inside the ``try`` reads the
    caller's own two files, and every way those can fail is the caller's
    configuration.

    Args:
        certificate_path: Path to the PEM certificate (or to a PEM holding
            both the certificate and its key, which OpenSSL accepts when
            both arguments name it).
        key_path: Path to the PEM private key.

    Returns:
        A context built for ``ssl.Purpose.SERVER_AUTH`` -- so it can
        actually open a client socket (H28) -- carrying the client
        certificate, verifying the server's chain and its host name.

    Raises:
        ConfigurationError: If the key is passphrase-protected (an
            unsupported case, named as such), or if either file is
            missing, unreadable, not a valid PEM, or a certificate and key
            that do not match. All of those arrive as ``OSError``
            subclasses -- ``ssl.SSLError`` is itself an ``OSError``, so
            catching both would be redundant -- and the message names the
            two files, because OpenSSL's own does not.
        OSError: If the system CA bundle cannot be read. Left to
            propagate: nothing about the caller's request can fix it.
    """
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    try:
        context.load_cert_chain(
            certificate_path, key_path, password=_refuse_passphrase)
    except _PassphraseProtectedKey as err:
        raise ConfigurationError(
            f'client key {key_path!r} is passphrase-protected, which '
            'async-gateway does not support: supply a decrypted PEM key'
        ) from err
    except OSError as err:
        raise ConfigurationError(
            f'client certificate {certificate_path!r} and key '
            f'{key_path!r} could not be loaded: {err}') from err

    # A `raise`, not an `assert`. `python -O` strips assert statements
    # outright, so a security guard written as one is present in
    # development and absent in exactly the optimised deployments that
    # most need it. R23-AC2 asks for asserts; this is strictly stronger
    # and the deviation is recorded in ticket AGW-17.
    if not (context.check_hostname
            and context.verify_mode == ssl.CERT_REQUIRED):
        raise ConfigurationError(
            'refusing a client TLS context that does not verify the '
            f'server (check_hostname={context.check_hostname}, '
            f'verify_mode={context.verify_mode!r}); this is a '
            'library defect, not a configuration error')
    return context


async def get_ssl_config(
        certificate: Any = None,
        verify_ssl: Optional[bool] = None) -> SslFilters:
    """Decide the TLS posture of one outbound call.

    Three-way and explicit (M1), because the value it replaced --
    ``verify_ssl or True`` -- collapsed to ``True`` for every input and
    made the library's posture an accident:

    * a **certificate** builds a verifying client context carrying it;
    * ``verify_ssl`` **exactly** ``False`` disables verification and logs
      a warning;
    * anything else, including ``None`` and an absent argument, verifies.

    "Exactly ``False``" is the identity test and not truthiness. ``0``,
    ``''`` and ``None`` are all falsy and none of them is a caller asking
    to talk to an unauthenticated peer, so only the ``False`` singleton
    turns verification off. There is no other path to
    ``check_hostname=False`` or ``CERT_NONE``.

    The certificate branch **overrides** ``verify_ssl=False`` rather than
    honouring it. That is deliberate and fail-secure: presenting a client
    identity to a peer whose own identity is unverified hands that
    identity to whoever answered, which is worse than the plain
    unverified session the flag asked for. A caller who wants no
    verification can have it by supplying no certificate.

    Args:
        certificate: The caller's ``(certificate path, key path)`` pair,
            or None. Typed ``Any`` because it arrives unvalidated;
            :func:`normalised_certificate` is what narrows it.
        verify_ssl: The caller's flag, or None when they did not set one.

    Returns:
        Exactly one keyword for the transport call, always under
        ``'ssl'`` -- ``aiohttp``'s supported key, never the deprecated
        ``ssl_context=`` (R23-AC3) -- carrying a verifying client context,
        or ``True``, or ``False``.

    Raises:
        ConfigurationError: If ``certificate`` is not a usable pair of
            paths, or names files that will not load.
        OSError: If the system CA bundle cannot be read.
    """
    if certificate:
        certificate_path, key_path = normalised_certificate(certificate)
        # In a thread: both calls inside read files, and on HTTP this
        # coroutine runs inside `failsafe.run`, so on the event loop the
        # CA-bundle read would repeat on every retried attempt (AGW-36).
        context = await asyncio.to_thread(
            build_client_ssl_context, certificate_path, key_path)
        return {'ssl': context}

    if verify_ssl is False:
        logger.warning(
            'verify_ssl is False: TLS certificate and host-name '
            'verification are disabled for this call, so the connection '
            'is not protected against an intercepting peer')
        return {'ssl': False}

    return {'ssl': True}


def is_get(request_type: str) -> bool:
    """Report whether ``request_type`` names the GET verb.

    Case-insensitively, and with surrounding whitespace ignored, because
    every other consumer of the verb in this library lowercases it. The one
    comparison that did not (``request_type == 'GET'``) is M5: a caller who
    wrote ``"get"`` had their payload attached as a JSON *body* on a GET.

    Args:
        request_type: The verb as the caller spelled it.

    Returns:
        True when the verb is GET in any casing.
    """
    return request_type.strip().lower() == 'get'


def coerce_query_value(value: Any) -> str:
    """Render one query-parameter value as the text an API expects.

    ``str(True)`` is ``'True'``; APIs expect ``'true'``, and ``aiohttp``
    refuses a bare ``bool`` outright. The ``bool`` test comes first because
    ``bool`` is itself a subclass of ``int``, so the ``int`` branch would
    otherwise claim every bool and render it as ``'1'``. Its ``isinstance``
    spelling is not load-bearing: ``bool`` cannot be subclassed in CPython,
    so no value distinguishes it from ``type(value) is bool``.

    Args:
        value: One value from the caller's payload, of any type.

    Returns:
        The value as text: ``'true'``/``'false'`` for a bool, ``''`` for
        None -- the query-string spelling of "present but empty" --
        ISO-8601 for a date, time or datetime, and compact JSON for a
        nested mapping or sequence. Anything else falls back to ``str``,
        which is the only thing left that can be put in a URL.
    """
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        # Nested containers have no query-string spelling of their own, so
        # they are serialised. `orjson` renders a nested bool as `true`,
        # which is why a bool two levels down needs no separate handling.
        return orjson.dumps(value).decode()
    return str(value)


def build_query_params(data: Dict[str, Any]) -> QueryParams:
    """Render a payload mapping as ``aiohttp`` query-parameter pairs.

    A *top-level* list or tuple becomes a repeated parameter
    (``?tag=a&tag=b``), which is what an HTTP API means by a multi-valued
    parameter; a container nested any deeper is serialised by
    :func:`coerce_query_value` instead.

    Args:
        data: The caller's payload. It is read and never written -- the
            coerced values go into a new list -- so the dict the caller
            still holds is unchanged after the call (M6).

    Returns:
        One ``(name, value)`` pair per scalar value, and one per element of
        a list or tuple value.
    """
    params: QueryParams = []
    for key, value in data.items():
        if isinstance(value, (list, tuple)):
            params.extend((key, coerce_query_value(item)) for item in value)
        else:
            params.append((key, coerce_query_value(value)))
    return params


async def form_x_www_form_urlencoded_filters(
        data: Dict[str, Any],
        *,
        request_type: str) -> RequestFilters:
    """Encode the payload as ``application/x-www-form-urlencoded``.

    Args:
        data: The caller's payload. Read, never written.
        request_type: The verb, accepted so every filter shares one
            signature. A form body is encoded the same way for every verb,
            so this filter does not branch on it.

    Returns:
        ``{'data': aiohttp.FormData(...)}``.
    """
    form_data = aiohttp.FormData()
    for form_key, form_value in data.items():
        # `.decode()` is load-bearing: `orjson.dumps` returns `bytes`, and
        # `FormData.add_field` accepts bytes but then emits the part without
        # a text content type -- which forces the whole request to
        # multipart. Decoding keeps the field, and therefore the request
        # encoding, byte-identical to what the previous serialiser produced.
        value = orjson.dumps(form_value).decode() if \
            isinstance(form_value, dict) else form_value
        form_data.add_field(form_key, value)
    filters = {'data': form_data}
    return filters


async def application_json_filters(
        data: Payload,
        *,
        request_type: str) -> RequestFilters:
    """Encode the payload as JSON, or as query parameters on a GET.

    Args:
        data: The caller's payload. Read, never written.
        request_type: The verb, compared case-insensitively. Required and
            named rather than pulled out of ``kwargs``, so a caller who
            supplies none is rejected as a configuration error at the
            entry point instead of raising ``KeyError`` in here.

    Returns:
        ``{'params': [...]}`` on a GET with a mapping payload;
        ``{'json': ...}`` on any other verb with a mapping payload;
        ``{'data': ...}`` for an already-serialised ``str``/``bytes`` body
        or a serialised scalar. A GET whose payload is not a mapping has
        nothing that can become a query string, so it contributes nothing.
    """
    if is_get(request_type):
        if isinstance(data, dict):
            return {'params': build_query_params(data)}
        return {}
    if isinstance(data, dict):
        return {'json': data}
    if isinstance(data, (str, bytes)):
        # Already serialised; re-encoding it would double-encode the body.
        return {'data': data}
    # Decoded because `filters['data']` must stay a `str`, as it was under
    # the previous serialiser.
    return {'data': orjson.dumps(data).decode()}


async def raw_body_filters(
        data: Payload,
        *,
        request_type: str) -> RequestFilters:
    """Attach the payload as the request body, verbatim.

    The filter the XML media types dispatch to, and the default for an
    unknown media type carrying a ``str`` or ``bytes`` payload. It does not
    encode, re-encode or re-serialise anything: what the caller passed is
    what reaches the wire, which is what lets a SOAP envelope arrive
    byte-identical to the one that was built.

    Args:
        data: The caller's payload, sent exactly as given.
        request_type: The verb, accepted so every filter shares one
            signature. A raw body is a raw body on every verb, so this
            filter does not branch on it.

    Returns:
        ``{'data': <payload>}``, or ``{}`` when there is no payload to
        send.
    """
    if data is None:
        return {}
    return {'data': data}

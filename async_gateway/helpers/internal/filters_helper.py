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
"""

import ssl
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Text, Tuple, Union

import aiohttp

import orjson

#: ``aiohttp`` query parameters as pairs rather than as a mapping: a list
#: value means a repeated parameter and a mapping cannot express one.
QueryParams = List[Tuple[Text, Text]]

#: The keyword arguments a filter contributes to the transport call.
RequestFilters = Dict[Text, Any]

#: What a payload may be by the time it reaches a filter.
Payload = Optional[Union[Dict[Text, Any], Text, bytes]]


async def get_ssl_config(
        certificate: Tuple[Text] = None,
        verify_ssl: bool = None) -> Dict:
    """Get the SSL config.

    :param certificate: Tuple[Text] - ('certificate path',
        'certificate key path')
    :param verify_ssl: bool - Flag to enable ssl verification
    """
    # verify_ssl, ssl_context, fingerprint and ssl parameters
    # are mutually exclusive
    # Some don't use ssl; but use an IP whereas some use
    # SSL Certificate and those combined
    # usage in aiohttp session_obj contradict each other
    # thereby raising a ValueError Exception
    if certificate:
        # `SERVER_AUTH` is the purpose of the peer being *authenticated*,
        # which on an outbound call is the server. `CLIENT_AUTH` reads as
        # "we are the client" and means the opposite: it is what a server
        # builds with to authenticate its clients, and it yields
        # `verify_mode=CERT_NONE` with `check_hostname` off. Supplying a
        # client certificate therefore negotiated TLS against an entirely
        # unauthenticated peer (M2).
        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.load_cert_chain(certificate[0], certificate[1])
        return {
            'ssl_context': ssl_context
        }
    return {
        'ssl': verify_ssl or True
    }


def is_get(request_type: Text) -> bool:
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


def coerce_query_value(value: Any) -> Text:
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


def build_query_params(data: Dict[Text, Any]) -> QueryParams:
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
        data: Dict[Text, Any],
        *,
        request_type: Text) -> RequestFilters:
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
        request_type: Text) -> RequestFilters:
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
        request_type: Text) -> RequestFilters:
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

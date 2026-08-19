"""Internal helpers, and the one media-type matcher both sides share.

A request's filter and a response's parser are chosen from the same
normalised media type, by the same functions. The two sides used to
disagree *by construction* -- the request side looked the whole
``Content-Type`` up as an exact dict key, so
``application/json; charset=utf-8`` found nothing and the code then called
``None``; the response side matched substrings, so it found JSON inside
media types that were not JSON. That disagreement is the evidence of H14
rather than an incidental difference between two call sites, so neither
spelling survives here.

The request-side filter table is R12's, named in the spec rather than left
to the code. There is deliberately no ``'default'`` entry routing every
unknown media type to the JSON filter: that entry is what sent a SOAP
envelope through a JSON encoder.

| Media type                                        | Filter    |
|---------------------------------------------------|-----------|
| ``application/json`` (and ``+json`` suffixes)      | JSON      |
| ``application/x-www-form-urlencoded``              | form      |
| ``text/xml``, ``application/soap+xml``,            | raw body  |
| ``application/xml``                                |           |
| anything else / absent, payload ``str``/``bytes``  | raw body  |
| anything else / absent, any other payload          | JSON      |
"""

from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from async_gateway.helpers.internal.filters_helper import (
    application_json_filters,
    form_x_www_form_urlencoded_filters,
    raw_body_filters,
)

#: A request filter: the payload and the verb in, transport keyword
#: arguments out. Every filter in the table above satisfies it, which is
#: what lets the dispatch treat them interchangeably.
RequestFilter = Callable[..., Awaitable[Dict[str, Any]]]

#: Matched case-insensitively as a *key*, because a caller's header dict is
#: an ordinary dict: ``{'content-type': '...'}`` is the same header as
#: ``{'Content-Type': '...'}`` and used to silently JSON-encode a form body.
CONTENT_TYPE_HEADER = 'content-type'

JSON_MEDIA_TYPE = 'application/json'
FORM_MEDIA_TYPE = 'application/x-www-form-urlencoded'
JSON_MEDIA_SUFFIX = '+json'
MULTIPART_MEDIA_PREFIX = 'multipart/'

#: Media types whose body is carried verbatim, in both directions. XML and
#: SOAP are byte-significant: re-encoding an envelope changes it.
RAW_BODY_MEDIA_TYPES = frozenset({
    'application/soap+xml',
    'application/xml',
    'text/xml',
})


def normalise_media_type(value: Optional[str]) -> str:
    """Reduce a ``Content-Type`` value to its bare media type.

    Parameters are stripped and case is folded, because
    ``application/json; charset=utf-8`` and ``Application/JSON`` are the
    same media type as ``application/json`` and HTTP says so.

    Args:
        value: A raw ``Content-Type`` header value, or None.

    Returns:
        The lower-cased media type with parameters and surrounding
        whitespace removed, or ``''`` when there was no value.
    """
    if not value:
        return ''
    return value.split(';', 1)[0].strip().lower()


def media_type_of(headers: Optional[Mapping[str, str]]) -> str:
    """Return the media type ``headers`` announces, normalised.

    Args:
        headers: Request or response headers, or None. The key is matched
            without regard to case, which an ordinary ``dict`` does not do
            for itself.

    Returns:
        The normalised media type, or ``''`` when the header is absent --
        an explicit, testable answer rather than the string ``'None'``
        that ``str(headers.get(...))`` used to produce.
    """
    if not headers:
        return ''
    for name, value in headers.items():
        if name.lower() == CONTENT_TYPE_HEADER:
            return normalise_media_type(value)
    return ''


def is_json_media_type(media_type: str) -> bool:
    """Report whether ``media_type`` names a JSON body.

    ``application/problem+json`` and its relatives are JSON by RFC 6839's
    structured-syntax suffix, and the substring matching this replaces
    accepted them. Accepting them here too keeps that working while
    refusing the media types substring matching accepted by accident.

    Args:
        media_type: A media type already through
            :func:`normalise_media_type`.

    Returns:
        True for ``application/json`` and for any ``+json`` suffix.
    """
    return (
        media_type == JSON_MEDIA_TYPE
        or media_type.endswith(JSON_MEDIA_SUFFIX)
    )


def filter_for_media_type(media_type: str, payload: Any) -> RequestFilter:
    """Choose the request filter for a media type and payload.

    The table in this module's docstring, as code. It returns a filter for
    every input, so there is no un-guarded ``mapping.get(...)(...)`` left
    to call ``None`` (H14).

    The payload's type decides only the *unknown* case, and it decides it
    the safe way round: an unknown media type carrying an already-encoded
    ``str`` or ``bytes`` body is passed through untouched rather than run
    through a JSON encoder, because a body the caller encoded themselves is
    a body they intended to send as-is.

    Args:
        media_type: The normalised request media type; ``''`` when absent.
        payload: The caller's payload, inspected only for its type.

    Returns:
        The filter to build the transport keyword arguments with.
    """
    if media_type == FORM_MEDIA_TYPE:
        return form_x_www_form_urlencoded_filters
    if is_json_media_type(media_type):
        return application_json_filters
    if media_type in RAW_BODY_MEDIA_TYPES:
        return raw_body_filters
    if isinstance(payload, (str, bytes)):
        return raw_body_filters
    return application_json_filters

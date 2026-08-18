"""SOAP 1.1 and 1.2, as a fourth strategy over the existing HTTP layer.

The protocol the package has advertised in its title, its parameter table
and its packaging description since the beginning, and has never had
(finding H3): ``logic/soap.py`` was a zero-byte file and ``'SOAP'`` mapped
to ``None`` in the registry, which reached callers as ``TypeError:
'NoneType' object is not callable``.

What is new here is exactly four things -- envelope construction,
version-correct transport headers, a hardened response parse, and Fault
mapping. Everything else is *reused, not reimplemented*: the session and
its pooling, the per-call deadline, the capped reader, the owned redirect
loop, the circuit breaker, the request tracer and the one response
envelope all come from the modules the earlier stories built. The
validators below are imported from ``logic/http_client.py`` rather than
copied, so a rule about ``timeout`` or ``session`` has one implementation
and cannot drift between two protocols.

**Deliberately out of scope**, and not an omission: WSDL introspection,
code generation, and any dict-to-XML mapping. A caller supplies the body
as an XML string or an ``Element`` and navigates the reply with
``ElementTree``'s own API. MTOM / ``multipart/related`` is refused with a
``ConfigurationError`` rather than mis-parsed.

**Why the stdlib parser.** Runtime XML parsing is ``xml.etree``, with no
new runtime dependency (R19). Per CPython's own vulnerability table
``xml.etree`` is safe against external-entity expansion and DTD retrieval
from 3.7.1 and vulnerable only to entity-expansion denial of service --
which requires an internal entity declaration, which requires a
``DOCTYPE``, which a SOAP envelope never legitimately carries. Refusing a
document whose *prolog* declares one therefore closes the whole relevant
class at zero dependency cost, and R14's byte cap -- which every read
below goes through -- bounds what remains: unbounded tree size and
decompression bombs, which the prolog guard does not address.
``defusedxml`` was rejected as itself dormant since 2021, and the
libxml2-backed alternative as a binary wheel in an otherwise pure-Python
client library. (That package is named in the spec and in this module's
tests, not here: R19-AC7 greps this package for its name and requires
zero matches, so the runtime decision is checkable rather than asserted.)

**The guard is prolog-scoped, and that is load-bearing.** It rejects a
``<!`` declaration appearing *before the root element's start tag* only.
A substring search over the whole body is explicitly forbidden: a Fault
``<detail>`` echoing an HTML snippet, a CDATA section, or a base64 field
whose decoded text contains the literal word ``DOCTYPE`` are all everyday
content, and rejecting a valid response as ``XML_UNSAFE`` because of one
is a worse failure than the attack the guard exists to stop.
"""

import asyncio
import logging
from typing import (
    Any,
    ClassVar,
    Dict,
    List,
    MutableMapping,
    Optional,
    Tuple,
    TypedDict,
    Union,
)
from xml.etree.ElementTree import Element, ParseError, fromstring, tostring

import aiohttp

from failsafe import CircuitOpen, RetriesExhausted

from async_gateway.helpers.internal import media_type_of
from async_gateway.helpers.internal.base import BaseRequestClass
from async_gateway.helpers.internal.request_helper import (
    HttpResult,
    handle_http_request,
)
from async_gateway.logic.http_client import (
    trace_collectors_for,
    transport_error_for,
    validated_allow_redirects,
    validated_allowed_schemes,
    validated_cookies,
    validated_headers,
    validated_http_auth,
    validated_max_redirects,
    validated_max_response_bytes,
    validated_session,
    validated_timeout,
    validated_trace_config,
)
from async_gateway.utils.constants import (
    ALLOWED_SCHEMES,
    HTTP_ERROR_STATUS,
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
)
from async_gateway.utils.envelope import GatewayResponse, finalise_ok
from async_gateway.utils.exceptions import (
    CircuitOpenError,
    ConfigurationError,
    HttpStatusError,
    SerializationError,
    SoapFaultError,
    UnsafeXmlError,
)
from async_gateway.utils.redaction import (
    redact_cookies,
    redact_headers,
    redact_url,
)

logger = logging.getLogger(__name__)

#: SOAP 1.1, the default. The namespace is the one identifying token: a
#: 1.1 envelope carries it and a 1.2 envelope carries the other.
SOAP_11 = '1.1'

#: SOAP 1.2.
SOAP_12 = '1.2'

#: The envelope namespace each version is defined by. Also the lookup that
#: recognises an inbound envelope's version from the document itself,
#: which is what lets a non-conformant server be reported rather than
#: mis-parsed.
ENVELOPE_NAMESPACE: Dict[str, str] = {
    SOAP_11: 'http://schemas.xmlsoap.org/soap/envelope/',
    SOAP_12: 'http://www.w3.org/2003/05/soap-envelope',
}

#: The reverse of :data:`ENVELOPE_NAMESPACE`: which version a document's
#: root namespace says it is.
VERSION_BY_NAMESPACE: Dict[str, str] = {
    namespace: version
    for version, namespace in ENVELOPE_NAMESPACE.items()
}

#: The media type each version's request -- and a conformant server's
#: response -- is carried as. 1.1 predates the SOAP media type and uses
#: the generic XML one.
REQUEST_MEDIA_TYPE: Dict[str, str] = {
    SOAP_11: 'text/xml',
    SOAP_12: 'application/soap+xml',
}

#: The prefix the constructed envelope binds the SOAP namespace to. A
#: prefix rather than a default namespace so that the ``<Body>`` a reader
#: sees on the wire is unambiguously the SOAP one, and a caller's own
#: default-namespaced body cannot be captured by it.
ENVELOPE_PREFIX = 'soap'

#: What a 1.1 request sends when the caller names no action. The 1.1
#: binding requires the header to be *present*; an empty quoted string is
#: how it says "no action", and omitting it altogether is what several
#: intermediaries reject.
EMPTY_SOAP_ACTION = '""'

#: Characters that cannot appear in a ``SOAPAction`` value or in the
#: ``action`` content-type parameter. The quote would terminate the quoted
#: string early; CR and LF would split the header and let a caller inject
#: one of their own.
FORBIDDEN_ACTION_CHARS = ('"', '\r', '\n')

#: How much of an unrecognised body is quoted back in the parse failure.
#: Enough to identify an HTML error page from a proxy at a glance, and
#: bounded so a 64 MiB body does not become a 64 MiB exception message.
BODY_EXCERPT_CHARS = 200


class SoapFault(TypedDict):
    """A SOAP Fault, flattened into one shape for both versions.

    The two versions spell the same four ideas differently -- 1.1's
    ``faultcode``/``faultstring``/``faultactor``/``detail`` against 1.2's
    ``Code/Value``, ``Reason/Text``, ``Role`` and ``Detail`` -- so a
    consumer branching on the version to read a fault would be writing the
    per-protocol handling this library exists to remove.

    Attributes:
        code: The fault code, as the server spelled it, e.g.
            ``'soap:Client'`` or ``'env:Sender'``; ``''`` when absent.
        subcodes: The 1.2 ``Subcode`` chain, outermost first, in full.
            Always empty for 1.1, which has no such concept.
        reason: The human-readable reason -- 1.1's ``faultstring`` or
            1.2's first ``Reason/Text``; ``''`` when absent.
        actor: 1.1's ``faultactor`` or 1.2's ``Role``, or None.
        detail: The ``detail``/``Detail`` element re-serialised as XML, or
            None when absent. Serialised rather than handed over as an
            ``Element`` because a Fault detail routinely carries nested
            application XML whose schema this library knows nothing about,
            and a string preserves it exactly without pretending to.
    """

    code: str
    subcodes: List[str]
    reason: str
    actor: Optional[str]
    detail: Optional[str]


def validated_soap_version(soap_version: object) -> str:
    """Return the SOAP version once proven one this library speaks.

    Rejected rather than defaulted, because the two versions differ on the
    wire in ways a server notices: a 1.2 endpoint handed a 1.1 envelope
    answers a ``VersionMismatch`` Fault, and a 1.1 endpoint handed a 1.2
    content type answers a 415. Silently picking one for a caller who
    named something else would produce exactly those, blamed on the
    server.

    Args:
        soap_version: ``protocol_info['soap_version']``, of whatever type
            the caller passed, or the default.

    Returns:
        ``'1.1'`` or ``'1.2'``.

    Raises:
        ConfigurationError: If the value is not one of those two strings.
            A ``float`` is rejected with everything else and named as such
            -- ``soap_version=1.1`` is the likely typo, and accepting it
            would make the key's type depend on which value was passed.
    """
    if not isinstance(soap_version, str):
        raise ConfigurationError(
            f'protocol_info["soap_version"] must be the string '
            f'{SOAP_11!r} or {SOAP_12!r}, got '
            f'{type(soap_version).__name__} {soap_version!r}')
    if soap_version not in ENVELOPE_NAMESPACE:
        raise ConfigurationError(
            f'protocol_info["soap_version"] must be {SOAP_11!r} or '
            f'{SOAP_12!r}, got {soap_version!r}')
    return soap_version


def validated_soap_action(soap_action: object) -> Optional[str]:
    """Return the SOAP action once proven safe to put in a header.

    The value reaches the wire inside a quoted string -- a ``SOAPAction``
    header on 1.1, a ``Content-Type`` parameter on 1.2 -- so a quote in it
    terminates that string early and a CR or LF splits the header. Both
    are header injection, and both are the caller's own input, so they are
    refused at the boundary rather than escaped into something that only
    looks like what they asked for.

    Args:
        soap_action: ``protocol_info['soap_action']``, of whatever type
            the caller passed, or None when they named none.

    Returns:
        The action, or None. An empty string is normalised to None: on 1.1
        both spell the same wire value (``SOAPAction: ""``) and on 1.2 an
        empty ``action=""`` parameter says less than omitting it.

    Raises:
        ConfigurationError: If the value is neither None nor a ``str``, or
            if it carries a quote, a carriage return or a newline.
    """
    if soap_action is None:
        return None
    if not isinstance(soap_action, str):
        raise ConfigurationError(
            f'protocol_info["soap_action"] must be a str or None, got '
            f'{type(soap_action).__name__}')
    if not soap_action:
        return None
    for char in FORBIDDEN_ACTION_CHARS:
        if char in soap_action:
            raise ConfigurationError(
                f'protocol_info["soap_action"] may not contain '
                f'{char!r}: the value is emitted inside a quoted header '
                f'string, so a quote or a line break would end that '
                f'string early and let the rest be read as headers of '
                f'its own')
    return soap_action


def validated_soap_headers(soap_headers: object) -> Optional[Element]:
    """Return the caller's SOAP header block once proven an element.

    Args:
        soap_headers: ``protocol_info['soap_headers']``, of whatever type
            the caller passed, or None when they supplied none.

    Returns:
        The element to place inside ``<Header>``, or None.

    Raises:
        ConfigurationError: If the value is neither None nor an
            ``xml.etree.ElementTree.Element``. A ``str`` is rejected with
            everything else and named: an unparsed string spliced into the
            header block is how a malformed envelope reaches the wire
            looking well-formed to the code that built it.
    """
    if soap_headers is None:
        return None
    if not isinstance(soap_headers, Element):
        raise ConfigurationError(
            f'protocol_info["soap_headers"] must be an '
            f'xml.etree.ElementTree.Element or None, got '
            f'{type(soap_headers).__name__}; build the header block with '
            f'ElementTree rather than as text, so this library can only '
            f'emit an envelope that is well formed')
    return soap_headers


def validated_soap_body(payload: Any) -> str:
    """Return the caller's request body as the XML text to be wrapped.

    Args:
        payload: What the caller passed as ``data``. An XML string, an
            ``Element``, or the empty mapping the entry point substitutes
            when they passed nothing.

    Returns:
        The body as XML text, or ``''`` for a call with no body -- a
        ``<Body/>`` is a legitimate request and several one-way operations
        are exactly that.

    Raises:
        ConfigurationError: If the payload is a non-empty mapping, or any
            other type. There is deliberately **no dict-to-XML mapping**:
            choosing element names, ordering and namespaces for a mapping
            requires the schema, which requires the WSDL, which R18
            declines. Guessing produces an envelope the server rejects and
            blames on the caller, so the refusal names the two shapes that
            do work.
    """
    if isinstance(payload, Element):
        return tostring(payload, encoding='unicode')
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict) and not payload:
        return ''
    raise ConfigurationError(
        f'a SOAP request body must be an XML str or an '
        f'xml.etree.ElementTree.Element, got '
        f'{type(payload).__name__}; async-gateway provides no '
        f'dict-to-XML mapping, because choosing element names, order and '
        f'namespaces for one needs the service schema and this library '
        f'does not read WSDL')


def root_element_name(xml_text: str) -> str:
    """Return the local name of ``xml_text``'s root element, or ``''``.

    A deliberately small scan rather than a parse: this is asked of the
    *caller's* body to decide whether it is already an envelope, and
    parsing it here would mean parsing every request body twice and
    failing the caller's malformed XML with this library's error rather
    than with the server's.

    Args:
        xml_text: XML text, with or without a prolog.

    Returns:
        The root element's name with any namespace prefix stripped, or
        ``''`` when the text holds no element at all.
    """
    start = scan_prolog(xml_text)
    if start < 0:
        return ''
    cursor = start + 1
    end = len(xml_text)
    while cursor < end and not (
            xml_text[cursor].isspace() or xml_text[cursor] in '>/'):
        cursor += 1
    return xml_text[start + 1:cursor].rpartition(':')[2]


def build_envelope(
    body: Union[str, Element],
    *,
    version: str,
    headers: Optional[Element] = None,
) -> str:
    """Wrap ``body`` in a SOAP envelope of ``version``, or pass it through.

    The string this returns is what reaches the wire, byte for byte once
    UTF-8 encoded: :class:`SoapRequest` dispatches it through R12's
    raw-body filter, which attaches a body verbatim, and never through the
    JSON filter, which would re-encode it (and would raise ``KeyError`` on
    the ``request_type`` a SOAP call has no reason to carry).

    A body that is **already a complete envelope** is returned unwrapped.
    A caller who has built the whole document -- because their service
    needs a header this library does not model, or because they hold a
    canned request -- would otherwise get an ``<Envelope>`` inside a
    ``<Body>``, which every server rejects and which is invisible in the
    code that produced it.

    Args:
        body: The request body as XML text or as an ``Element``.
        version: ``'1.1'`` or ``'1.2'``, already validated.
        headers: An element to place inside ``<Header>``, or None to emit
            no ``<Header>`` at all. An empty header block is legal but
            says nothing, and omitting it keeps the envelope minimal.

    Returns:
        The complete envelope as text, with an XML declaration.
    """
    body_text = body if isinstance(body, str) else tostring(
        body, encoding='unicode')
    if root_element_name(body_text) == 'Envelope':
        return body_text

    namespace = ENVELOPE_NAMESPACE[version]
    header_text = ''
    if headers is not None:
        header_text = (
            f'<{ENVELOPE_PREFIX}:Header>'
            f'{tostring(headers, encoding="unicode")}'
            f'</{ENVELOPE_PREFIX}:Header>')
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<{ENVELOPE_PREFIX}:Envelope '
        f'xmlns:{ENVELOPE_PREFIX}="{namespace}">'
        f'{header_text}'
        f'<{ENVELOPE_PREFIX}:Body>{body_text}</{ENVELOPE_PREFIX}:Body>'
        f'</{ENVELOPE_PREFIX}:Envelope>')


def soap_transport_headers(
    *,
    version: str,
    action: Optional[str] = None,
) -> Dict[str, str]:
    """Return the transport headers ``version`` requires for a request.

    The one place the two versions differ on the wire in a way a caller
    can get wrong, so it is one function rather than a branch at the call
    site:

    * **1.1** carries the action in its own ``SOAPAction`` header, and the
      header is emitted **always** -- as ``""`` when there is no action.
      The 1.1 binding requires it to be present, and intermediaries route
      on it; an absent header is not the same as an empty one to them.
    * **1.2** has no ``SOAPAction`` header at all. The action is a
      parameter of the content type, and emitting the 1.1 header
      alongside it is a conformance error rather than a compatibility
      measure -- no interop escape hatch is added here speculatively.

    Args:
        version: ``'1.1'`` or ``'1.2'``, already validated.
        action: The already-validated action, or None.

    Returns:
        The headers to send, ready to merge over the caller's own.
    """
    media_type = REQUEST_MEDIA_TYPE[version]
    if version == SOAP_11:
        return {
            'Content-Type': f'{media_type}; charset=utf-8',
            'SOAPAction': f'"{action}"' if action else EMPTY_SOAP_ACTION,
        }
    parameter = f'; action="{action}"' if action else ''
    return {'Content-Type': f'{media_type}; charset=utf-8{parameter}'}


def scan_prolog(xml_text: str) -> int:
    """Return the offset of the root element's ``<``, refusing a DOCTYPE.

    The security core of this module, and the reason no XML dependency was
    added. It walks the document *prolog* -- whitespace, an XML
    declaration, processing instructions and comments -- and stops at the
    first thing that is none of those. A ``<!`` token found on the way is
    a ``DOCTYPE`` declaration (nothing else may legally appear there), and
    a ``DOCTYPE`` is the entry condition for every entity-expansion attack
    ``xml.etree`` is vulnerable to: billion laughs, quadratic blowup, and
    the internal entity subset generally. A SOAP envelope never carries
    one, so refusing it costs nothing and closes the class.

    **It is not a substring search, and must never become one.** The scan
    stops the moment the root element begins, so a ``<detail>`` echoing an
    HTML error page, a CDATA section, or a base64 field whose decoded text
    reads ``DOCTYPE`` are all ordinary content and parse normally. A
    body-contains check would reject those as ``XML_UNSAFE`` -- a valid
    response refused, on the everyday case, for a threat that is not there.

    The returned offset is also what strips the XML declaration before
    parsing. ``ElementTree.fromstring`` refuses a ``str`` carrying an
    encoding declaration outright, and the declaration is stale by this
    point anyway: the transport has already decoded the bytes, so what it
    claims about their encoding can no longer be true or false.

    Args:
        xml_text: The response body as decoded text.

    Returns:
        The index of the ``<`` that opens the root element, or ``-1`` when
        the text holds no element -- an empty body, or a prolog and
        nothing after it.

    Raises:
        UnsafeXmlError: If a declaration is found in the prolog.
    """
    cursor = 0
    end = len(xml_text)
    while cursor < end:
        char = xml_text[cursor]
        if char.isspace() or char == '﻿':
            cursor += 1
            continue
        if not xml_text.startswith('<', cursor):
            # Text before the root element. Not well-formed XML, and not
            # this function's error to raise: the parser reports it with
            # the position information R19 asks be preserved.
            return -1
        if xml_text.startswith('<?', cursor):
            closed = xml_text.find('?>', cursor + 2)
            if closed < 0:
                return -1
            cursor = closed + 2
            continue
        if xml_text.startswith('<!--', cursor):
            closed = xml_text.find('-->', cursor + 4)
            if closed < 0:
                return -1
            cursor = closed + 3
            continue
        if xml_text.startswith('<!', cursor):
            # Every `<!` token legal in a prolog other than a comment is a
            # DOCTYPE, so this needs no case-insensitive spelling test and
            # cannot be evaded by one.
            raise UnsafeXmlError(
                'refusing an XML response whose prolog declares a '
                'DOCTYPE: a document type declaration is what makes '
                'entity-expansion attacks (billion laughs, quadratic '
                'blowup) possible, and a SOAP envelope never carries one')
        return cursor
    return -1


def parse_document(xml_text: str) -> Optional[Element]:
    """Parse ``xml_text`` into a tree, after the prolog has been cleared.

    A plain ``def`` and the only blocking work in this module: parsing a
    tree is CPU-bound and a body may be as large as R14's cap allows, so
    :class:`SoapRequest` reaches it through ``asyncio.to_thread`` rather
    than letting one large response stall every other call in flight.

    Args:
        xml_text: The response body as decoded text.

    Returns:
        The root element, or None when the body holds no element at all --
        an empty body, which an HTTP 202 legitimately produces.

    Raises:
        UnsafeXmlError: If the prolog declares a DOCTYPE. Raised by
            :func:`scan_prolog`, **before** ``fromstring`` is reached, so
            the hostile document is never handed to the parser.
        SerializationError: If the document is not well-formed, carrying
            the parser's own line and column.
    """
    start = scan_prolog(xml_text)
    if start < 0:
        return None
    try:
        return fromstring(xml_text[start:])
    except ParseError as err:
        raise SerializationError(
            f'SOAP response is not well-formed XML: {err}') from err


def first_element_child(parent: Element) -> Optional[Element]:
    """Return ``parent``'s first element child, warning if there are more.

    Args:
        parent: The element to read, normally a ``<Body>``.

    Returns:
        The first element child, or None when there is none.
    """
    children = list(parent)
    if not children:
        return None
    if len(children) > 1:
        # A warning and not an error. SOAP 1.1 permits several body
        # entries, so raising here would refuse legitimate traffic; the
        # narrowing to the first is documented, and the count is named so
        # a caller silently losing entries can see it happening.
        logger.warning(
            'SOAP response <Body> carries %d element children; returning '
            'the first as soap_body. SOAP 1.1 permits several body '
            'entries and this library surfaces only the first',
            len(children))
    return children[0]


def fault_from_11(fault: Element) -> SoapFault:
    """Read a SOAP 1.1 ``<Fault>`` into the shared fault shape.

    1.1's fault children are **unqualified** -- ``faultcode``, not
    ``{ns}faultcode`` -- which is the single most common way a 1.1 fault
    reader written against 1.2 finds nothing and reports a successful call.

    Args:
        fault: The ``<Fault>`` element.

    Returns:
        The fault, with an always-empty ``subcodes``: 1.1 has no subcode
        chain, and inventing one from the code string would be a guess.
    """
    detail = fault.find('detail')
    actor = fault.findtext('faultactor')
    return SoapFault(
        code=(fault.findtext('faultcode') or '').strip(),
        subcodes=[],
        reason=(fault.findtext('faultstring') or '').strip(),
        actor=actor.strip() if actor is not None else None,
        detail=None if detail is None else tostring(
            detail, encoding='unicode'),
    )


def fault_from_12(fault: Element, namespace: str) -> SoapFault:
    """Read a SOAP 1.2 ``<Fault>`` into the shared fault shape.

    The ``Subcode`` chain is walked to its end rather than read one level
    deep. The chain is where a 1.2 service says *which* of its own errors
    this is -- the outer ``Value`` is almost always the generic
    ``env:Sender`` -- so stopping at the first level discards the only
    part a caller can branch on.

    Args:
        fault: The ``<Fault>`` element.
        namespace: The 1.2 envelope namespace the document uses.

    Returns:
        The fault, with ``subcodes`` outermost first and complete.
    """
    qualified = f'{{{namespace}}}'
    code = fault.find(f'{qualified}Code')
    subcodes: List[str] = []
    value = ''
    if code is not None:
        value = (code.findtext(f'{qualified}Value') or '').strip()
        subcode = code.find(f'{qualified}Subcode')
        while subcode is not None:
            subcodes.append(
                (subcode.findtext(f'{qualified}Value') or '').strip())
            subcode = subcode.find(f'{qualified}Subcode')

    reason = fault.find(f'{qualified}Reason')
    reason_text = ''
    if reason is not None:
        reason_text = (reason.findtext(f'{qualified}Text') or '').strip()

    role = fault.findtext(f'{qualified}Role')
    detail = fault.find(f'{qualified}Detail')
    return SoapFault(
        code=value,
        subcodes=subcodes,
        reason=reason_text,
        actor=role.strip() if role is not None else None,
        detail=None if detail is None else tostring(
            detail, encoding='unicode'),
    )


def parse_soap_response(
    raw: str,
    *,
    version: str,
) -> Tuple[Optional[Element], Optional[SoapFault]]:
    """Parse one SOAP response into its body element and its Fault.

    The bytes handed here have already passed R14's ``max_response_bytes``
    cap -- this function does not read from the transport and has no way
    to bound its own input, so the cap is the thing that makes the
    stdlib-parser decision safe and it is enforced upstream, in
    ``request_helper.read_response``.

    The version is taken from the **document**, not from the caller's
    configuration, and the two are compared. A server that answers a 1.1
    envelope to a 1.2 request is a real and common misconfiguration, and
    parsing it against the requested version would find no ``<Body>`` and
    report a perfectly readable Fault as an empty success.

    Args:
        raw: The response body as decoded text.
        version: The version the request was sent as, ``'1.1'`` or
            ``'1.2'``. Used to detect and log a mismatch, not to decide
            how to read the document.

    Returns:
        ``(body_element, None)`` on success and ``(None, fault)`` on a
        Fault, where ``body_element`` is the **first element child** of
        ``<Body>`` -- not ``<Body>`` itself -- and is None when ``<Body>``
        is absent, is empty, or the response body was empty.

    Raises:
        UnsafeXmlError: If the prolog declares a DOCTYPE.
        SerializationError: If the document is not well-formed, or is
            well-formed and is not a SOAP envelope -- an HTML error page
            from a proxy being the everyday case, which must be reported
            as the parse failure it is rather than as an empty body.
    """
    root = parse_document(raw)
    if root is None:
        return None, None

    namespace = root.tag.partition('}')[0].lstrip('{')
    found = VERSION_BY_NAMESPACE.get(namespace)
    if found is None or not root.tag.endswith('}Envelope'):
        raise SerializationError(
            f'SOAP response is well-formed XML but is not a SOAP '
            f'envelope: its root element is {root.tag!r}. First '
            f'{BODY_EXCERPT_CHARS} characters of the body: '
            f'{raw[:BODY_EXCERPT_CHARS]!r}')
    if found != version:
        logger.warning(
            'SOAP response is a %s envelope but the request was sent as '
            '%s; parsing it as the %s document it actually is',
            found, version, found)

    body = root.find(f'{{{namespace}}}Body')
    if body is None:
        return None, None

    fault = body.find(f'{{{namespace}}}Fault')
    if fault is not None:
        return None, (
            fault_from_11(fault) if found == SOAP_11
            else fault_from_12(fault, namespace))
    return first_element_child(body), None


class SoapRequest(BaseRequestClass):
    """Dispatches a SOAP 1.1 or 1.2 call over the library's HTTP layer.

    A fourth strategy beside ``HttpRequest``, ``FTPRequest`` and
    ``SFTPRequest``, and the class ``'SOAP'`` maps to in the registry now
    that there is one to map to (H3). It reuses the HTTP transport whole:
    the same session and pooling, the same chain-wide deadline, the same
    capped reader, the same owned redirect loop, the same per-destination
    circuit breaker and the same request tracer -- every configuration
    validator below is imported from ``logic/http_client.py`` rather than
    copied, so a rule about ``timeout`` has one implementation.

    What it adds is the SOAP part and nothing else: the envelope, the
    version-correct headers, the hardened parse and the Fault mapping.

    ``protocol_info`` keys of its own:

    * ``soap_version`` -- ``'1.1'`` (default) or ``'1.2'``.
    * ``soap_action`` -- the action, or None.
    * ``soap_headers`` -- an ``Element`` for the ``<Header>`` block.

    The verb is always ``POST`` and is not configurable. Every SOAP
    binding this library speaks is a POST, and a ``request_type`` key
    would only let a caller construct a call no server answers.
    """

    #: Nothing. A SOAP call needs no configuration at all: the version
    #: defaults, the action is optional, and the verb is not the caller's
    #: to choose.
    REQUIRED_INFO_KEYS: ClassVar[frozenset[str]] = frozenset()

    #: The verb every SOAP binding uses.
    REQUEST_TYPE: ClassVar[str] = 'POST'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build a SOAP request and its envelope from ``protocol_info``.

        The envelope is built **here**, in the constructor, and not at
        dispatch time. Everything that can make one impossible is the
        caller's own configuration -- a version this library does not
        speak, an action carrying a quote, a header block that is not an
        element, a body that is a ``dict`` -- and the entry point builds
        the protocol object *outside* the block that converts an
        ``AsyncGatewayError`` into an envelope. Raising here is therefore
        what makes a configuration error reach the caller as an exception
        before anything is dispatched, which is the contract ``request()``
        documents.

        Args:
            args: Positional arguments for :class:`BaseRequestClass` --
                the URL, the auth object and the envelope.
            kwargs: Keyword arguments for :class:`BaseRequestClass`,
                including ``info`` and ``redact_params``.

        Raises:
            ConfigurationError: If ``protocol_info`` cannot form a valid
                call: a ``soap_version`` other than ``'1.1'`` or
                ``'1.2'``; a ``soap_action`` that is not a str or that
                carries a quote or a line break; a ``soap_headers`` that
                is not an ``Element``; a request body that is neither XML
                text nor an ``Element``; or any of the HTTP-layer keys
                (``session``, ``timeout``, ``max_response_bytes``,
                ``allow_redirects``, ``max_redirects``,
                ``allowed_schemes``, ``trace_config``, ``headers``,
                ``cookies``) failing the same checks they fail on an HTTP
                call.
        """
        super().__init__(*args, **kwargs)

        self.soap_version: str = validated_soap_version(
            self.info.get('soap_version', SOAP_11))
        self.soap_action: Optional[str] = validated_soap_action(
            self.info.get('soap_action'))
        self.soap_headers: Optional[Element] = validated_soap_headers(
            self.info.get('soap_headers'))
        #: The exact text that reaches the wire, UTF-8 encoded. Held on
        #: the object so a test -- and a caller debugging a rejected
        #: request -- can compare it against what the server received.
        self.envelope: str = build_envelope(
            validated_soap_body(self.response['payload']),
            version=self.soap_version,
            headers=self.soap_headers,
        )
        # The library's headers go *over* the caller's. Content-Type and
        # SOAPAction are what make the request a SOAP request of the
        # version they asked for, so they are not the caller's to
        # contradict -- and a contradicted Content-Type would also route
        # the envelope away from the raw-body filter and into the JSON
        # encoder, which is the routing hole this class exists inside.
        #
        # The caller's half is validated *before* the merge, not after:
        # the library's own two headers are built from values this
        # constructor has already checked, so re-checking them would
        # report a library bug as the caller's `protocol_info["headers"]`
        # -- naming the wrong key in the one message the caller acts on.
        self.headers: Dict[str, str] = {
            **validated_headers(self.info.get('headers')),
            **soap_transport_headers(
                version=self.soap_version, action=self.soap_action),
        }
        self.auth = validated_http_auth(self.auth, self.url)
        self.cookies: Optional[Dict[str, str]] = validated_cookies(
            self.info.get('cookies'))
        self.verify_ssl: bool = self.info.get('verify_ssl', True)
        self.timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(
            total=validated_timeout(self.timeout))
        self.session: Optional[aiohttp.ClientSession] = validated_session(
            self.info.get('session'))
        self.trace_config: List[aiohttp.TraceConfig] = validated_trace_config(
            self.info, self.session)
        self.trace_collectors: List[MutableMapping[str, Any]] = (
            trace_collectors_for(self.session, self.trace_config))
        # Derived from the bind above, exactly as `logic/http_client.py`
        # derives its own, rather than re-read off the tracers. Two
        # reasons, and the first is why this is a type fix and not a
        # rewrite: reading `tracer.results_collector` reached an
        # attribute `aiohttp.TraceConfig` does not declare -- this
        # library attaches it in `request_tracer()` -- so the only other
        # way to type the line was a `type: ignore` for a lookup the
        # module next door already avoids. The second is that the two
        # HTTP-family protocols now put the same kind of object into the
        # same envelope key, which is what `request_tracer` being one
        # documented key means. A caller-supplied session leaves
        # `trace_config` empty either way, so `[]` is still what such a
        # call reports.
        self.reported_collectors: List[MutableMapping[str, Any]] = (
            [] if self.session is not None else list(self.trace_collectors))
        self.max_response_bytes: int = validated_max_response_bytes(
            self.info.get('max_response_bytes', MAX_RESPONSE_BYTES))
        self.allow_redirects: bool = validated_allow_redirects(
            self.info.get('allow_redirects', True))
        self.max_redirects: int = validated_max_redirects(
            self.info.get('max_redirects', MAX_REDIRECTS))
        self.allowed_schemes: frozenset[str] = validated_allowed_schemes(
            self.info.get('allowed_schemes', ALLOWED_SCHEMES))

    async def handle_request(self) -> GatewayResponse:
        """POST the envelope and fill the envelope with what came back.

        A caller-supplied ``protocol_info['session']`` is used and left
        open so consecutive calls reuse its pool; one created here is
        closed on the way out. Either way the per-call deadline is applied
        on the request itself, so a supplied session's own deadline cannot
        outrank the one this call was configured with.

        Returns:
            The same envelope object this request was constructed with,
            populated and finalised.

        Raises:
            SoapFaultError: On a Fault, at whatever status the server
                sent -- 500 and 200 alike. Raised *after* the status,
                headers, body text and structured fault are already in the
                envelope (invariant E11).
            HttpStatusError: On a 4xx or 5xx that carried no Fault.
            UnsafeXmlError: When the response prolog declares a DOCTYPE.
            SerializationError: When the body is not decodable text, is
                not well-formed XML, or is not a SOAP envelope.
            ConfigurationError: When the response is ``multipart/related``
                (MTOM, unsupported), or a redirect targets a scheme
                outside ``allowed_schemes``.
            ResponseTooLargeError: When the body exceeds
                ``max_response_bytes`` -- raised before it is read into
                memory, and therefore before any parse.
            CircuitOpenError: When the breaker for this destination is
                open.
            GatewayTimeoutError: On a connect, read or total timeout.
            TlsError: On a handshake or certificate failure.
            DnsError: When the host name does not resolve.
            ConnectError: When the connection is refused or reset.
            TransportError: For any other client-side transport failure.
        """
        if self.session is not None:
            return await self._exchange(self.session)
        async with aiohttp.ClientSession(
            trace_configs=self.trace_config, timeout=self.timeout
        ) as session:
            return await self._exchange(session)

    async def _exchange(
        self,
        session: aiohttp.ClientSession,
    ) -> GatewayResponse:
        """Dispatch on ``session`` and fill the envelope with the answer.

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
                self.REQUEST_TYPE,
                self.circuit_breaker,
                headers=self.headers,
                cookies=self.cookies,
                auth=self.auth,
                # The envelope text, which the raw-body filter attaches
                # verbatim because the Content-Type above selects it. The
                # caller's own payload stays on the response envelope for
                # the echo and is never what goes on the wire.
                payload=self.envelope,
                certificate=self.certificate,
                verify_ssl=self.verify_ssl,
                http_file_download_config=None,
                http_file_upload_config={},
                redact_params=self.redact_params,
                timeout=self.timeout,
                max_response_bytes=self.max_response_bytes,
                allowed_schemes=self.allowed_schemes,
                allow_redirects=self.allow_redirects,
                max_redirects=self.max_redirects,
                trace_collectors=self.trace_collectors,
                # MTOM. Refused at the transport rather than here, which
                # is the only placement that works: `read_response` writes
                # a multipart body to disk as it reads it, so by the time
                # this method could inspect the media type the file the
                # caller never asked for already exists.
                refuse_multipart=True,
            )
        except CircuitOpen as err:
            raise CircuitOpenError(
                f'circuit open for '
                f'{redact_url(self.url, extra_params=self.redact_params)}'
            ) from err
        except RetriesExhausted as err:
            raise transport_error_for(
                err, redact_params=self.redact_params) from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise transport_error_for(
                err, redact_params=self.redact_params) from err

        return await self._answer(result)

    async def _answer(self, result: HttpResult) -> GatewayResponse:
        """Copy the exchange into the envelope and judge what it says.

        The order of the three judgements is the whole of R19's error
        contract and is not interchangeable:

        1. **A Fault beats the status.** A Fault is the server's
           considered answer and arrives with an HTTP 500 as often as with
           a 200; reporting the 500 as ``HTTP_STATUS`` would hide the
           reason the caller needs, and a Fault at 200 would be reported
           as a *success* by a transport-status-only check. The real
           status is preserved either way -- nothing is synthesised.
        2. **A parse failure beats the status too.** An HTML error page
           from a proxy arrives as a 502 with a body that is not an
           envelope, and R19 requires that to read as ``SERIALIZATION``
           rather than as an unexplained bad gateway.
        3. **Only then the status**, so a 500 that carried neither a Fault
           nor a parse problem is the ``HTTP_STATUS`` it is.

        Args:
            result: What the transport boundary returned.

        Returns:
            The same envelope object, finalised as a success.

        Raises:
            AsyncGatewayError: As documented on :meth:`handle_request`.
        """
        status: int = result['status_code']
        self.response['status_code'] = status
        self.response['headers'] = redact_headers(result['headers'])
        self.response['cookies'] = redact_cookies(result['cookies'])
        self.response['text'] = result['text']
        self.response['request_tracer'] = self.reported_collectors
        self.response['protocol_details'] = {
            'soap_version': self.soap_version,
            'soap_body': None,
            'soap_fault': None,
        }

        decode_error: Optional[str] = result.get('decode_error')
        if decode_error is not None:
            raise SerializationError(decode_error)

        self._warn_on_response_media_type(result['headers'])

        # In a thread: building a tree is CPU-bound and the body may be as
        # large as `max_response_bytes` allows, so parsing it inline would
        # stall every other call the consuming process has in flight for
        # as long as it takes.
        body, fault = await asyncio.to_thread(
            parse_soap_response, result['text'], version=self.soap_version)

        self.response['protocol_details']['soap_body'] = body
        self.response['protocol_details']['soap_fault'] = fault

        if fault is not None:
            raise SoapFaultError(
                f'SOAP Fault from '
                f'{redact_url(self.url, extra_params=self.redact_params)}'
                f': {fault["code"] or "<no code>"} '
                f'{fault["reason"] or "<no reason>"}',
                status)
        if status >= HTTP_ERROR_STATUS:
            raise HttpStatusError(
                f'POST '
                f'{redact_url(self.url, extra_params=self.redact_params)}'
                f' returned HTTP {status}',
                status)
        return finalise_ok(
            self.response, status_code=status, started=self.start_time)

    def _warn_on_response_media_type(
        self,
        headers: Dict[str, str],
    ) -> None:
        """Log a warning when the response media type is not the expected.

        Non-conformant servers are the rule rather than the exception
        here: a 1.2 endpoint answering ``text/xml`` is common enough that
        refusing it would break real integrations, so the response is
        parsed and the discrepancy is *reported* instead. Reported and not
        swallowed, because it is the first thing to look at when a 1.2
        call comes back with a body that reads like 1.1.

        Args:
            headers: The response headers, unredacted as received.

        Returns:
            None.
        """
        media_type = media_type_of(headers)
        expected = REQUEST_MEDIA_TYPE[self.soap_version]
        if media_type and media_type != expected:
            logger.warning(
                'SOAP %s response announced Content-Type %r, not the '
                'conformant %r; parsing it anyway',
                self.soap_version, media_type, expected)

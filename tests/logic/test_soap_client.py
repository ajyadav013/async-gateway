"""Tests for the SOAP client (spec R18, R19, Step 20 · finding H3).

Four things are under test here, and they are not equally interesting.

**The envelope and the headers** are pure functions and are tested as
such, but the criterion that matters is byte-identity on the wire: the
bytes the server received must equal ``build_envelope(...)`` encoded
UTF-8, with no re-serialisation and no whitespace normalisation anywhere
between. That is asserted off ``await request.read()`` on the real
loopback ``aiohttp.web`` handler -- against a server's own view of the
wire, not against a mock's record of what it was handed -- because the
routing hole it closes was invisible from the library's side: the request
filter table used to map every unknown media type to the JSON filter, so
a SOAP envelope would have been handed to a JSON encoder (which would
additionally have raised ``KeyError`` on the ``request_type`` a SOAP call
has no reason to carry).

**Fault mapping** is tested at HTTP 500 *and* at HTTP 200, and the 200
case is the one that earns its place: a Fault is the server's considered
answer and a transport-status-only check reports it as a success.

**The XML hardening is the security core**, and it is tested adversarially
rather than by example. Each guard gets a real attack payload -- a
billion-laughs envelope, an XXE file read, an external parameter entity
pointing at a network callback -- and each is confirmed refused *before*
``fromstring`` is reached, by patching ``fromstring`` itself and asserting
it was never called. The refusal is not merely observed; the corresponding
false-positive case is asserted in the same breath, because a guard that
rejects everything is not a guard. ``test_a_detail_containing_the_literal_
word_doctype_is_parsed`` is as load-bearing as the rejection tests: a
substring search over the body would pass every rejection test above and
fail that one, on the everyday case of a Fault echoing an HTML error page.

No test here opens a socket beyond loopback. The two fixtures that bind
one are the recording server and nothing else.
"""

import asyncio
import logging
import sys
from types import SimpleNamespace
from typing import Any, Final
from xml.etree.ElementTree import Element, fromstring, tostring

import aiohttp

import pytest

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.helpers.common.date_helper import monotonic_now
from asyncio_gateway.logic import soap_client
from asyncio_gateway.logic.soap_client import (
    ENVELOPE_NAMESPACE,
    SOAP_11,
    SOAP_12,
    SoapRequest,
    build_envelope,
    exceeds_depth,
    parse_soap_response,
    soap_transport_headers,
    validated_soap_action,
    validated_soap_headers,
)
from asyncio_gateway.utils.constants import (
    HTTP_TIMEOUT,
    MAX_FAULT_DETAIL_DEPTH,
)
from asyncio_gateway.utils.envelope import GatewayResponse, new_envelope
from asyncio_gateway.utils.exceptions import ConfigurationError
from asyncio_gateway.utils.redaction import REDACTED
from asyncio_gateway.utils.request_tracer import request_tracer

from tests.fixtures.http_server import RecordingHTTPServer

#: What a conformant 1.1 server announces.
XML_11: Final[dict[str, str]] = {'Content-Type': 'text/xml; charset=utf-8'}

#: What a conformant 1.2 server announces.
XML_12: Final[dict[str, str]] = {
    'Content-Type': 'application/soap+xml; charset=utf-8'}

#: A deadline in tens of milliseconds. The gated handler never answers, so
#: this is the only thing the timeout test waits on -- the behaviour under
#: test, not a sleep placed to let unrelated async work settle.
SHORT_DEADLINE: Final[float] = 0.05

#: Two orders of magnitude of slack, so a loaded CI box cannot fail the
#: deadline assertion and a missing deadline cannot pass it.
DEADLINE_TOLERANCE: Final[float] = 5.0


def envelope_for(version: str, inner: str = '<Result>1</Result>') -> bytes:
    """Return a minimal well-formed response envelope of ``version``.

    Args:
        version: ``'1.1'`` or ``'1.2'``.
        inner: The XML to place inside ``<Body>``.

    Returns:
        The envelope as UTF-8 bytes, ready to be served as a body.
    """
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<e:Envelope xmlns:e="{ENVELOPE_NAMESPACE[version]}">'
        f'<e:Body>{inner}</e:Body>'
        f'</e:Envelope>'
    ).encode()


def fault_11(detail: str = '') -> bytes:
    """Return a SOAP 1.1 Fault envelope.

    Args:
        detail: XML to place inside ``<detail>``, or ``''`` to omit the
            element entirely.

    Returns:
        The Fault envelope as UTF-8 bytes.
    """
    detail_element = f'<detail>{detail}</detail>' if detail else ''
    return envelope_for(SOAP_11, (
        f'<e:Fault>'
        f'<faultcode>soap:Client</faultcode>'
        f'<faultstring>Invalid account number</faultstring>'
        f'<faultactor>urn:billing</faultactor>'
        f'{detail_element}'
        f'</e:Fault>'
    ))


def fault_12(subcodes: tuple[str, ...] = ()) -> bytes:
    """Return a SOAP 1.2 Fault envelope, optionally with a Subcode chain.

    Args:
        subcodes: Subcode values, outermost first. Nested into a chain, so
            two values produce a ``Subcode`` inside a ``Subcode``.

    Returns:
        The Fault envelope as UTF-8 bytes.
    """
    chain = ''
    for value in reversed(subcodes):
        chain = f'<e:Subcode><e:Value>{value}</e:Value>{chain}</e:Subcode>'
    return envelope_for(SOAP_12, (
        f'<e:Fault>'
        f'<e:Code><e:Value>e:Sender</e:Value>{chain}</e:Code>'
        f'<e:Reason><e:Text xml:lang="en">Invalid account number'
        f'</e:Text></e:Reason>'
        f'<e:Role>urn:billing</e:Role>'
        f'</e:Fault>'
    ))


#: The sentinel distinguishing "serve the default envelope" from "serve
#: literally no body". ``b''`` is a meaningful response -- an HTTP 202
#: with an empty body is one of R18's own criteria -- so it cannot double
#: as the default, which ``body or ...`` would have made it.
UNSET: Final[bytes] = b'\x00<unset>'


async def soap_call(
    http_server: RecordingHTTPServer,
    *,
    body: bytes = UNSET,
    headers: dict[str, str] | None = None,
    status: int = 200,
    path: str = '/soap',
    payload: Any = '<Ping/>',
    **info: Any,
) -> GatewayResponse:
    """Drive one SOAP call through the whole library against the fixture.

    Args:
        http_server: The loopback recording server fixture.
        body: The response body the server returns; omitted for a minimal
            well-formed envelope of the requested version.
        headers: The response headers, or None for the 1.1 XML pair.
        status: The response status the server returns.
        path: The path to register and address.
        payload: The request body handed to ``request()`` as ``data``.
        info: Extra ``protocol_info`` keys, e.g. ``soap_version``.

    Returns:
        The finalised envelope ``request()`` returned.
    """
    if body is UNSET:
        # `ENVELOPE_NAMESPACE` rather than the raw key, because the
        # version-rejection tests pass values this helper must not itself
        # crash on -- their assertion is about `request()`'s refusal, and
        # a KeyError here would pre-empt it.
        version = info.get('soap_version', SOAP_11)
        body = envelope_for(
            version if version in ENVELOPE_NAMESPACE else SOAP_11)
    http_server.respond(
        path,
        status=status,
        body=body,
        headers=XML_11 if headers is None else headers,
    )
    return await request(
        url=http_server.url_for(path),
        data=payload,
        protocol='SOAP',
        protocol_info=dict(info),
    )


# --- R18-AC3: envelope construction, per version ---------------------------


@pytest.mark.parametrize('version', [SOAP_11, SOAP_12])
def test_r18_ac3_the_envelope_carries_the_version_correct_namespace(
    version: str,
) -> None:
    """Each version's envelope is bound to its own namespace URI."""
    root = fromstring(build_envelope('<Ping/>', version=version))

    assert root.tag == f'{{{ENVELOPE_NAMESPACE[version]}}}Envelope'
    assert root.find(f'{{{ENVELOPE_NAMESPACE[version]}}}Body') is not None


def test_r18_ac3_the_two_versions_do_not_share_a_namespace() -> None:
    """The namespace is the one token that distinguishes the versions.

    Asserted directly rather than left implied by the row above: if the
    table ever mapped both versions to one URI, every per-version test
    here would still pass and every real 1.2 endpoint would answer a
    ``VersionMismatch`` Fault.
    """
    assert ENVELOPE_NAMESPACE[SOAP_11] != ENVELOPE_NAMESPACE[SOAP_12]
    assert ENVELOPE_NAMESPACE[SOAP_11] == (
        'http://schemas.xmlsoap.org/soap/envelope/')
    assert ENVELOPE_NAMESPACE[SOAP_12] == (
        'http://www.w3.org/2003/05/soap-envelope')


@pytest.mark.parametrize('version', [SOAP_11, SOAP_12])
def test_r18_ac3_supplied_headers_appear_inside_the_header_block(
    version: str,
) -> None:
    """A caller's header element lands inside ``<Header>``, not beside it."""
    auth = fromstring('<AuthToken>abc</AuthToken>')

    root = fromstring(
        build_envelope('<Ping/>', version=version, headers=auth))

    namespace = ENVELOPE_NAMESPACE[version]
    header = root.find(f'{{{namespace}}}Header')
    assert header is not None
    assert [child.tag for child in header] == ['AuthToken']
    assert header[0].text == 'abc'


def test_r18_ac3_no_header_block_is_emitted_when_none_is_supplied() -> None:
    """An empty ``<Header>`` says nothing, so none is emitted."""
    root = fromstring(build_envelope('<Ping/>', version=SOAP_11))

    assert root.find(f'{{{ENVELOPE_NAMESPACE[SOAP_11]}}}Header') is None


# --- R18-AC4: an XML string or an Element, and nothing else ----------------


@pytest.mark.parametrize(
    'body',
    ['<Ping><id>7</id></Ping>', fromstring('<Ping><id>7</id></Ping>')],
    ids=['xml-string', 'element'])
def test_r18_ac4_the_body_may_be_a_string_or_an_element(body: Any) -> None:
    """Both accepted spellings produce the same body content."""
    root = fromstring(build_envelope(body, version=SOAP_11))

    entry = root.find(f'{{{ENVELOPE_NAMESPACE[SOAP_11]}}}Body')[0]
    assert entry.tag == 'Ping'
    assert entry.findtext('id') == '7'


async def test_r18_ac4_a_dict_body_is_refused_and_says_why(
    http_server: RecordingHTTPServer,
) -> None:
    """There is no dict-to-XML mapping, and the refusal names the reason.

    The refusal is the criterion, not a limitation being worked around:
    choosing element names, ordering and namespaces for a mapping needs
    the service schema, which needs the WSDL, which R18 declines. Guessing
    produces an envelope the server rejects and blames on the caller.
    """
    with pytest.raises(ConfigurationError) as raised:
        await soap_call(http_server, payload={'id': 7})

    message = str(raised.value)
    assert 'dict-to-XML' in message
    assert 'WSDL' in message
    assert http_server.requests == []


async def test_a_call_with_no_body_sends_an_empty_body_element(
    http_server: RecordingHTTPServer,
) -> None:
    """A one-way operation with no payload is a legitimate call."""
    result = await soap_call(http_server, payload=None)

    assert result['ok'] is True
    sent = fromstring(http_server.requests[0].body.decode())
    body = sent.find(f'{{{ENVELOPE_NAMESPACE[SOAP_11]}}}Body')
    assert list(body) == []


# --- R18 edge case: an already-complete Envelope is not double-wrapped -----


@pytest.mark.parametrize('version', [SOAP_11, SOAP_12])
def test_r18_a_complete_envelope_is_not_double_wrapped(version: str) -> None:
    """A caller who built the whole document gets it back unchanged.

    Double-wrapping is invisible in the code that produces it -- the
    envelope looks well formed to every check this library makes -- and
    is rejected by every server, so it is asserted rather than trusted.
    """
    complete = build_envelope('<Ping/>', version=version)

    assert build_envelope(complete, version=version) == complete
    root = fromstring(build_envelope(complete, version=version))
    body = root.find(f'{{{ENVELOPE_NAMESPACE[version]}}}Body')
    assert [child.tag for child in body] == ['Ping']


def test_r18_a_complete_envelope_supplied_as_an_element_is_not_wrapped(
) -> None:
    """The no-double-wrap check reads the document, not the argument type."""
    complete = fromstring(build_envelope('<Ping/>', version=SOAP_11))

    root = fromstring(build_envelope(complete, version=SOAP_11))

    assert root.tag.endswith('}Envelope')
    body = root.find(f'{{{ENVELOPE_NAMESPACE[SOAP_11]}}}Body')
    assert [child.tag for child in body] == ['Ping']


# --- R18-AC5: version-correct transport headers ----------------------------


def test_r18_ac5_soap_11_always_emits_a_soapaction_header() -> None:
    """1.1 requires the header present, so an absent action is ``""``.

    "Always" is the criterion. An omitted header is not the same as an
    empty one to an intermediary routing on it, and the 1.1 binding says
    the header must be there.
    """
    headers = soap_transport_headers(version=SOAP_11, action=None)

    assert headers['Content-Type'] == 'text/xml; charset=utf-8'
    assert headers['SOAPAction'] == '""'


def test_r18_ac5_soap_11_quotes_a_supplied_action() -> None:
    """A 1.1 action travels quoted, as the binding requires."""
    headers = soap_transport_headers(version=SOAP_11, action='urn:GetQuote')

    assert headers['SOAPAction'] == '"urn:GetQuote"'


def test_r18_ac5_soap_12_carries_the_action_as_a_content_type_parameter(
) -> None:
    """1.2 has no ``SOAPAction`` header at all; the action is a parameter.

    Emitting the 1.1 header alongside the 1.2 content type is a
    conformance error, not a compatibility measure -- no interop escape
    hatch is added here speculatively (OQ5).
    """
    headers = soap_transport_headers(version=SOAP_12, action='urn:GetQuote')

    assert headers['Content-Type'] == (
        'application/soap+xml; charset=utf-8; action="urn:GetQuote"')
    assert 'SOAPAction' not in headers


def test_r18_ac5_soap_12_omits_the_action_parameter_when_there_is_none(
) -> None:
    """An empty ``action=""`` parameter says less than omitting it."""
    headers = soap_transport_headers(version=SOAP_12, action=None)

    assert headers['Content-Type'] == 'application/soap+xml; charset=utf-8'
    assert 'SOAPAction' not in headers


@pytest.mark.parametrize(
    'action',
    ['urn:a"; x="y', 'urn:a\r\nX-Injected: 1', 'urn:a\nX-Injected: 1'],
    ids=['quote', 'crlf', 'lf'])
async def test_an_action_that_could_split_the_header_is_refused(
    http_server: RecordingHTTPServer,
    action: str,
) -> None:
    """A quote or a line break in the action is header injection.

    The value reaches the wire inside a quoted string on both versions, so
    a quote ends that string early and a line break lets whatever follows
    be read as headers of its own. Refused at the boundary rather than
    escaped into something that merely resembles what the caller asked
    for.
    """
    with pytest.raises(ConfigurationError):
        await soap_call(http_server, soap_action=action)

    assert http_server.requests == []


async def test_r18_ac5_the_11_headers_are_asserted_off_the_wire(
    http_server: RecordingHTTPServer,
) -> None:
    """The 1.1 headers the server received, not the ones we intended."""
    await soap_call(http_server, soap_action='urn:GetQuote')

    received = http_server.requests[0].headers
    assert received['Content-Type'] == 'text/xml; charset=utf-8'
    assert received['SOAPAction'] == '"urn:GetQuote"'
    assert http_server.requests[0].method == 'POST'


async def test_r18_ac5_the_12_headers_are_asserted_off_the_wire(
    http_server: RecordingHTTPServer,
) -> None:
    """The 1.2 headers the server received, ``SOAPAction`` absent."""
    await soap_call(
        http_server,
        body=envelope_for(SOAP_12),
        headers=XML_12,
        soap_version=SOAP_12,
        soap_action='urn:GetQuote')

    received = http_server.requests[0].headers
    assert received['Content-Type'] == (
        'application/soap+xml; charset=utf-8; action="urn:GetQuote"')
    assert 'SOAPAction' not in received


async def test_the_library_content_type_wins_over_a_caller_header(
    http_server: RecordingHTTPServer,
) -> None:
    """A caller cannot contradict the header that makes this a SOAP call.

    Not merely a precedence preference: a contradicted ``Content-Type``
    also routes the envelope away from the raw-body filter and into the
    JSON encoder, which is the routing hole this whole class sits inside.
    """
    http_server.respond('/soap', body=envelope_for(SOAP_11), headers=XML_11)

    await request(
        url=http_server.url_for('/soap'),
        data='<Ping/>',
        protocol='SOAP',
        protocol_info={'headers': {'Content-Type': 'application/json'}},
    )

    assert http_server.requests[-1].headers['Content-Type'] == (
        'text/xml; charset=utf-8')
    # The consequence, not just the header: routed by the caller's
    # `application/json` the envelope would have gone through the JSON
    # encoder and reached the wire quoted and escaped.
    assert http_server.requests[-1].body == build_envelope(
        '<Ping/>', version=SOAP_11).encode('utf-8')


async def test_a_caller_header_that_is_not_content_type_still_reaches_the_wire(
    http_server: RecordingHTTPServer,
) -> None:
    """Only the SOAP-defining headers are overridden, not every header."""
    http_server.respond('/hdr', body=envelope_for(SOAP_11), headers=XML_11)

    await request(
        url=http_server.url_for('/hdr'),
        data='<Ping/>',
        protocol='SOAP',
        protocol_info={'headers': {'X-Correlation-Id': 'abc123'}},
    )

    assert http_server.requests[-1].headers['X-Correlation-Id'] == 'abc123'


# --- R18-AC6: the envelope reaches the wire byte-identical -----------------


@pytest.mark.parametrize('version', [SOAP_11, SOAP_12])
async def test_r18_ac6_the_body_bytes_equal_build_envelope_exactly(
    http_server: RecordingHTTPServer,
    version: str,
) -> None:
    """Byte-identity against a real server's view of the wire.

    Not "equivalent XML" and not "the same after normalisation": the exact
    bytes. Anything that re-serialises an envelope -- a JSON encoder, a
    reformatting pass, an encode/decode round trip through the wrong codec
    -- changes them, and a SOAP service that signs or canonicalises the
    body notices.

    Asserted off ``await request.read()`` on the loopback handler rather
    than off a mock's ledger, because the failure it guards against was
    invisible from the library's side: the filter table used to map every
    unknown media type to the JSON filter, so this envelope would have
    been handed to a JSON encoder -- which would also have raised
    ``KeyError`` on the ``request_type`` a SOAP call has no reason to
    carry.
    """
    payload = '<Ping><id>7</id></Ping>'

    await soap_call(
        http_server,
        body=envelope_for(version),
        headers=XML_11 if version == SOAP_11 else XML_12,
        payload=payload,
        soap_version=version)

    expected = build_envelope(payload, version=version).encode('utf-8')
    assert http_server.requests[0].body == expected


async def test_r18_ac6_an_element_body_also_reaches_the_wire_intact(
    http_server: RecordingHTTPServer,
) -> None:
    """The ``Element`` spelling is serialised once, not twice."""
    element = fromstring('<Ping><id>7</id></Ping>')

    await soap_call(http_server, payload=element)

    expected = build_envelope(
        tostring(element, encoding='unicode'), version=SOAP_11)
    assert http_server.requests[0].body == expected.encode('utf-8')


# --- R18-AC7: soap_body is the first element child of <Body> ---------------


@pytest.mark.parametrize('version', [SOAP_11, SOAP_12])
async def test_r18_ac7_a_round_trip_returns_the_first_body_child(
    http_server: RecordingHTTPServer,
    version: str,
) -> None:
    """``soap_body`` is the body *entry*, not the ``<Body>`` wrapper."""
    result = await soap_call(
        http_server,
        body=envelope_for(version, '<Result><value>42</value></Result>'),
        headers=XML_11 if version == SOAP_11 else XML_12,
        soap_version=version)

    assert result['ok'] is True
    body = result['protocol_details']['soap_body']
    assert isinstance(body, Element)
    assert body.tag == 'Result'
    assert body.findtext('value') == '42'
    assert result['protocol_details']['soap_version'] == version
    assert result['protocol_details']['soap_fault'] is None


async def test_r18_ac7_an_empty_body_element_yields_none(
    http_server: RecordingHTTPServer,
) -> None:
    """An empty ``<Body/>`` is a perfectly good Element and still None.

    This is why ``soap_body`` is defined as the *first child* rather than
    as ``<Body>`` itself: under the other reading an empty body would be
    a truthy Element and the HTTP-202 criterion below could not hold.
    """
    result = await soap_call(
        http_server, body=envelope_for(SOAP_11, ''))

    assert result['ok'] is True
    assert result['protocol_details']['soap_body'] is None


async def test_r18_an_empty_response_on_202_is_ok_with_no_body(
    http_server: RecordingHTTPServer,
) -> None:
    """An accepted one-way call has no body and is not a failure."""
    result = await soap_call(http_server, body=b'', status=202)

    assert result['ok'] is True
    assert result['status_code'] == 202
    assert result['protocol_details']['soap_body'] is None
    assert result['error'] is None


async def test_r18_ac7_several_body_children_return_the_first_and_warn(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SOAP 1.1 permits several body entries, so this narrows, not raises.

    Raising here would reject legitimate traffic. The narrowing is
    documented and the count is logged, so a caller silently losing
    entries can see it happening.
    """
    caplog.set_level(logging.WARNING)

    result = await soap_call(
        http_server,
        body=envelope_for(SOAP_11, '<First/><Second/><Third/>'))

    assert result['ok'] is True
    assert result['protocol_details']['soap_body'].tag == 'First'
    # `getMessage()`, not `.message`: the count is an interpolated
    # argument, and the criterion is that the warning *names* it -- a
    # test reading the unformatted template would pass on a log line that
    # never told the caller how many entries it dropped.
    warnings = [record.getMessage() for record in caplog.records]
    assert any('3 element children' in message for message in warnings)


# --- R18-AC2: the version is validated, not guessed ------------------------


def test_r18_ac2_the_default_version_is_11() -> None:
    """1.1 is the default, asserted so a silent change is visible."""
    assert soap_client.validated_soap_version(SOAP_11) == SOAP_11
    assert SOAP_11 == '1.1'


@pytest.mark.parametrize(
    'version', ['1.0', '1.3', 1.1, '', None, b'1.1'],
    ids=['too-old', 'too-new', 'float', 'empty', 'none', 'bytes'])
async def test_r18_ac2_any_other_version_is_a_configuration_error(
    http_server: RecordingHTTPServer,
    version: Any,
) -> None:
    """Rejected rather than defaulted, and the float typo is named.

    Defaulting silently would produce a ``VersionMismatch`` Fault or a
    415 from the server and blame it on the endpoint. ``soap_version=1.1``
    -- the float -- is the likely typo, and accepting it would make the
    key's type depend on which value happened to be passed.
    """
    with pytest.raises(ConfigurationError) as raised:
        await soap_call(http_server, soap_version=version)

    assert '1.1' in str(raised.value)
    assert '1.2' in str(raised.value)
    assert http_server.requests == []


async def test_soap_headers_must_be_an_element_not_a_string(
    http_server: RecordingHTTPServer,
) -> None:
    """An unparsed string spliced into the header block is a malformed doc.

    It would look well formed to the code that built it, which is exactly
    the failure mode worth refusing at the boundary.
    """
    with pytest.raises(ConfigurationError) as raised:
        await soap_call(http_server, soap_headers='<AuthToken>a</AuthToken>')

    assert 'Element' in str(raised.value)


# --- R19: the DOCTYPE guard. Attack payloads, and the false positive -------

#: The classic entity-expansion bomb. Ten entities, each ten copies of the
#: one below it, expanding to roughly 10^9 characters on a parser that
#: honours the internal subset.
BILLION_LAUGHS: Final[bytes] = (
    b'<?xml version="1.0"?>\n'
    b'<!DOCTYPE lolz [\n'
    b' <!ENTITY lol "lol">\n'
    b' <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
    b' <!ENTITY lol2 '
    b'"&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">\n'
    b' <!ENTITY lol3 '
    b'"&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
    b' <!ENTITY lol4 '
    b'"&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">\n'
    b' <!ENTITY lol5 '
    b'"&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">\n'
    b' <!ENTITY lol6 '
    b'"&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">\n'
    b' <!ENTITY lol7 '
    b'"&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">\n'
    b' <!ENTITY lol8 '
    b'"&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">\n'
    b' <!ENTITY lol9 '
    b'"&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">\n'
    b']>\n'
    b'<e:Envelope xmlns:e="http://schemas.xmlsoap.org/soap/envelope/">'
    b'<e:Body><lolz>&lol9;</lolz></e:Body></e:Envelope>'
)

#: Classic XXE: a general entity resolving a local file into the body.
XXE_FILE_READ: Final[bytes] = (
    b'<?xml version="1.0"?>\n'
    b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
    b'<e:Envelope xmlns:e="http://schemas.xmlsoap.org/soap/envelope/">'
    b'<e:Body><Secret>&xxe;</Secret></e:Body></e:Envelope>'
)

#: Out-of-band XXE: an *external parameter* entity naming a URL, the shape
#: used to exfiltrate over the network rather than into the body.
XXE_EXTERNAL_PARAMETER: Final[bytes] = (
    b'<?xml version="1.0"?>\n'
    b'<!DOCTYPE foo [\n'
    b'<!ENTITY % ext SYSTEM "http://127.0.0.1:1/evil.dtd">\n'
    b'%ext;\n'
    b']>\n'
    b'<e:Envelope xmlns:e="http://schemas.xmlsoap.org/soap/envelope/">'
    b'<e:Body><Ping/></e:Body></e:Envelope>'
)

#: A DOCTYPE hidden behind a comment and a processing instruction, both of
#: which are legal prolog content. A guard that stops scanning at the
#: first `<` that is not `<?xml` would walk straight past this one.
DOCTYPE_BEHIND_PROLOG_NOISE: Final[bytes] = (
    b'<?xml version="1.0"?>\n'
    b'<!-- a perfectly innocent comment -->\n'
    b'<?xml-stylesheet href="x.xsl"?>\n'
    b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
    b'<e:Envelope xmlns:e="http://schemas.xmlsoap.org/soap/envelope/">'
    b'<e:Body><Secret>&xxe;</Secret></e:Body></e:Envelope>'
)


@pytest.fixture(name='never_parsed')
def never_parsed_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Record every call to ``fromstring``, so "before parse" is provable.

    R19 does not ask merely that a hostile document be *reported*; it asks
    that ``ElementTree.fromstring`` never see it. Observing the error code
    alone cannot tell a guard that ran first from one that ran after the
    parser had already expanded the bomb.

    Args:
        monkeypatch: The pytest patcher, which undoes this on teardown.

    Returns:
        A list the replacement appends each parsed document to. Empty
        means the parser was never reached.
    """
    seen: list[str] = []
    real = soap_client.fromstring

    def recording(text: str, *args: Any, **kwargs: Any) -> Element:
        """Record the document, then parse it as usual.

        Args:
            text: The document handed to the parser.
            args: Positional arguments, forwarded.
            kwargs: Keyword arguments, forwarded.

        Returns:
            Whatever the real parser returns.
        """
        seen.append(text)
        return real(text, *args, **kwargs)

    monkeypatch.setattr(soap_client, 'fromstring', recording)
    return seen


@pytest.mark.parametrize(
    'attack',
    [BILLION_LAUGHS, XXE_FILE_READ, XXE_EXTERNAL_PARAMETER,
     DOCTYPE_BEHIND_PROLOG_NOISE],
    ids=['billion-laughs', 'xxe-file-read', 'xxe-external-parameter',
         'doctype-behind-prolog-noise'])
async def test_r19_a_doctype_in_the_prolog_is_refused_before_any_parse(
    http_server: RecordingHTTPServer,
    never_parsed: list[str],
    attack: bytes,
) -> None:
    """Every entity-expansion payload is refused, and never parsed.

    One test per attack rather than one representative, because the four
    fail differently if the guard is wrong: billion laughs exhausts
    memory, the file read exfiltrates into the response body, the external
    parameter entity exfiltrates over the network, and the fourth walks
    past a guard that stops scanning too early.

    ``never_parsed`` is what makes this a *pre-parse* assertion. The error
    code alone would be satisfied by a guard that ran after the parser had
    already done the damage.
    """
    result = await soap_call(http_server, body=attack)

    assert result['ok'] is False
    assert result['error']['code'] == 'XML_UNSAFE'
    assert result['status_code'] == 502
    assert never_parsed == []
    assert 'DOCTYPE' in result['error']['message']


async def test_r19_the_billion_laughs_body_is_never_expanded(
    http_server: RecordingHTTPServer,
) -> None:
    """The refusal costs the size of the payload and nothing more.

    Memory is asserted structurally rather than by measurement: the
    envelope's ``text`` is the body as received, and if the bomb had been
    expanded the parse would have produced roughly 10^9 characters before
    anything could report on it. A completed call whose ``text`` is still
    the few hundred bytes that arrived is the proof that it was not.
    """
    result = await soap_call(http_server, body=BILLION_LAUGHS)

    assert result['error']['code'] == 'XML_UNSAFE'
    assert len(result['text']) == len(BILLION_LAUGHS.decode())
    assert result['protocol_details']['soap_body'] is None


async def test_r19_a_detail_containing_the_literal_word_doctype_is_parsed(
    http_server: RecordingHTTPServer,
) -> None:
    """The false-positive case, and it is as load-bearing as the rejections.

    **A substring search over the body would pass every rejection test
    above and fail this one.** A Fault ``<detail>`` echoing an HTML error
    page is the everyday case -- an upstream proxy's 502 page quoted back
    inside a fault detail -- and rejecting it as ``XML_UNSAFE`` refuses a
    valid response for a threat that is not there. The guard is
    prolog-scoped precisely so this parses.
    """
    detail = (
        '<html>&lt;!DOCTYPE html&gt; the upstream returned a DOCTYPE '
        'declaration in its error page</html>')

    result = await soap_call(http_server, body=fault_11(detail), status=500)

    assert result['error']['code'] == 'SOAP_FAULT'
    fault = result['protocol_details']['soap_fault']
    assert 'DOCTYPE' in fault['detail']


@pytest.mark.parametrize(
    'inner',
    ['<Note><![CDATA[<!DOCTYPE evil [<!ENTITY x "y">]>]]></Note>',
     '<Encoded>PCFET0NUWVBFIGZvbz4=</Encoded>',
     '<Text>the DOCTYPE keyword appears in this sentence</Text>'],
    ids=['cdata', 'base64', 'plain-text'])
async def test_r19_the_word_doctype_inside_the_document_is_content(
    http_server: RecordingHTTPServer,
    inner: str,
) -> None:
    """CDATA, base64 and prose all carry the word legitimately.

    Three spellings, because a body-contains check written to exclude one
    of them still rejects the other two.
    """
    result = await soap_call(
        http_server, body=envelope_for(SOAP_11, inner))

    assert result['ok'] is True
    assert result['error'] is None
    assert result['protocol_details']['soap_body'] is not None


def test_r19_the_guard_reads_the_prolog_and_stops_at_the_root() -> None:
    """``scan_prolog`` returns the root's offset, unit-tested directly.

    Driven at the function rather than only through the envelope, because
    the offset it returns is also what strips the XML declaration before
    parsing -- ``fromstring`` refuses a ``str`` carrying an encoding
    declaration outright -- so an off-by-one here breaks every well-formed
    response rather than any hostile one.
    """
    document = '<?xml version="1.0"?>\n<!-- c -->\n<Root/>'

    offset = soap_client.scan_prolog(document)

    assert document[offset:] == '<Root/>'


def test_r19_a_document_with_no_element_reports_no_offset() -> None:
    """An empty body, or a prolog and nothing else, holds no element."""
    assert soap_client.scan_prolog('') == -1
    assert soap_client.scan_prolog('   \n  ') == -1
    assert soap_client.scan_prolog('<?xml version="1.0"?>') == -1
    assert soap_client.scan_prolog('<!-- only a comment -->') == -1


def test_r19_an_unterminated_prolog_token_is_not_an_element() -> None:
    """A truncated declaration or comment yields no root, not a crash."""
    assert soap_client.scan_prolog('<?xml version="1.0"') == -1
    assert soap_client.scan_prolog('<!-- never closed') == -1


def test_r19_text_before_the_root_element_is_left_to_the_parser() -> None:
    """Junk before the root is malformed XML, reported with its position.

    Not this guard's error to raise: the parser's message carries the line
    and column, which R19 requires be preserved.
    """
    assert soap_client.scan_prolog('junk<Root/>') == -1


# --- R19: Faults map into the one error contract ---------------------------


async def test_r19_a_11_fault_maps_to_the_error_contract(
    http_server: RecordingHTTPServer,
) -> None:
    """1.1's unqualified fault children are read, and the code is stable."""
    result = await soap_call(http_server, body=fault_11(), status=500)

    assert result['ok'] is False
    assert result['error']['code'] == 'SOAP_FAULT'
    assert 'Invalid account number' in result['error']['message']
    fault = result['protocol_details']['soap_fault']
    assert fault['code'] == 'soap:Client'
    assert fault['reason'] == 'Invalid account number'
    assert fault['actor'] == 'urn:billing'
    assert fault['subcodes'] == []


async def test_r19_a_12_fault_maps_to_the_error_contract(
    http_server: RecordingHTTPServer,
) -> None:
    """1.2's qualified ``Code/Value`` and ``Reason/Text`` are read."""
    result = await soap_call(
        http_server,
        body=fault_12(),
        headers=XML_12,
        status=500,
        soap_version=SOAP_12)

    assert result['ok'] is False
    assert result['error']['code'] == 'SOAP_FAULT'
    fault = result['protocol_details']['soap_fault']
    assert fault['code'] == 'e:Sender'
    assert fault['reason'] == 'Invalid account number'
    assert fault['actor'] == 'urn:billing'


async def test_r19_a_12_subcode_chain_is_preserved_in_full(
    http_server: RecordingHTTPServer,
) -> None:
    """The chain is walked to its end, not read one level deep.

    The outer ``Value`` is almost always the generic ``env:Sender``; the
    chain is where a 1.2 service says which of its own errors this is, so
    stopping at the first level discards the only part a caller can
    branch on.
    """
    result = await soap_call(
        http_server,
        body=fault_12(('m:AccountUnknown', 'm:AccountClosed')),
        headers=XML_12,
        status=500,
        soap_version=SOAP_12)

    fault = result['protocol_details']['soap_fault']
    assert fault['subcodes'] == ['m:AccountUnknown', 'm:AccountClosed']


async def test_r19_a_fault_at_http_200_still_yields_not_ok(
    http_server: RecordingHTTPServer,
) -> None:
    """The case a transport-status-only check reports as a success.

    A Fault is the server's considered answer and it arrives with a 200
    often enough that this is not a corner. Judged by the *document*, not
    by the status.
    """
    result = await soap_call(http_server, body=fault_11(), status=200)

    assert result['ok'] is False
    assert result['error']['code'] == 'SOAP_FAULT'
    assert result['status_code'] == 200


@pytest.mark.parametrize('status', [200, 400, 500, 503])
async def test_r19_status_code_carries_the_real_status_on_a_fault(
    http_server: RecordingHTTPServer,
    status: int,
) -> None:
    """Nothing is synthesised: the Fault is signalled by ``ok``, not 502."""
    result = await soap_call(http_server, body=fault_11(), status=status)

    assert result['status_code'] == status
    assert result['error']['code'] == 'SOAP_FAULT'


async def test_r19_a_fault_beats_the_http_status_it_arrived_with(
    http_server: RecordingHTTPServer,
) -> None:
    """A 500 carrying a Fault is ``SOAP_FAULT``, not ``HTTP_STATUS``.

    Reporting the 500 would hide the reason the caller needs, which is the
    whole content of the answer.
    """
    result = await soap_call(http_server, body=fault_11(), status=500)

    assert result['error']['code'] == 'SOAP_FAULT'
    assert result['error']['code'] != 'HTTP_STATUS'


async def test_a_500_carrying_no_fault_is_reported_as_the_status_it_is(
    http_server: RecordingHTTPServer,
) -> None:
    """The other side of the ordering: no Fault means the status stands."""
    result = await soap_call(
        http_server, body=envelope_for(SOAP_11), status=500)

    assert result['ok'] is False
    assert result['error']['code'] == 'HTTP_STATUS'
    assert result['status_code'] == 500


@pytest.mark.parametrize('detail', ['', '<Nested><code>7</code></Nested>'],
                         ids=['absent', 'nested-application-xml'])
async def test_r19_a_fault_detail_may_be_absent_or_nested(
    http_server: RecordingHTTPServer,
    detail: str,
) -> None:
    """An absent detail is None; a nested one is preserved as XML.

    Preserved as a *string* rather than handed over as an Element,
    because a fault detail routinely carries application XML whose schema
    this library knows nothing about.
    """
    result = await soap_call(http_server, body=fault_11(detail), status=500)

    fault = result['protocol_details']['soap_fault']
    if detail:
        assert '<code>7</code>' in fault['detail']
    else:
        assert fault['detail'] is None


# --- N5: a Fault detail cannot choose the caller's error code -------------


def nested_detail(levels: int) -> str:
    """Return ``levels`` of nested elements for a Fault ``<detail>``.

    Args:
        levels: How many ``<a>`` elements to nest inside one another. The
            ``<detail>`` wrapping them is level 1, so the tree this
            produces is ``levels + 1`` deep.

    Returns:
        The nested XML as text.
    """
    return '<a>' * levels + 'x' + '</a>' * levels


@pytest.mark.parametrize(
    ('levels', 'kept'),
    [
        pytest.param(1, True, id='one-level'),
        pytest.param(MAX_FAULT_DETAIL_DEPTH - 1, True, id='at-the-limit'),
        pytest.param(MAX_FAULT_DETAIL_DEPTH, False, id='one-past-the-limit'),
        pytest.param(1000, False, id='the-reported-depth'),
    ],
)
async def test_n5_a_deep_fault_detail_is_still_reported_as_a_fault(
    http_server: RecordingHTTPServer,
    levels: int,
    kept: bool,
) -> None:
    """A remote server must not get to pick its caller's error code.

    ``tostring`` recurses one frame per element, so re-serialising a
    ``<detail>`` of 1000 levels -- **7 KB on the wire**, far under
    ``max_response_bytes``, which is why the byte cap could not see it --
    raised ``RecursionError``. ``request()``'s backstop converts that to
    ``STACK_EXHAUSTED``/502, so the identical Fault arrived as
    ``SOAP_FAULT`` with its real status when its detail was flat and as a
    stack exhaustion when it was deep. **The server chose**, which is the
    defect (N5).

    The assertions are therefore about *what survives*, not merely that
    nothing crashed. R19 requires a Fault to map to ``ok=False``,
    ``error.code == 'SOAP_FAULT'`` and the real HTTP status, and none of
    ``code``, ``reason`` or ``actor`` needs recursion to read -- so every
    one is asserted at every depth and only ``detail`` changes. A fix that
    refused the whole Fault would pass a "no ``RecursionError``" test and
    fail this one, which is why the fields are spelled out.

    The boundary rows sit either side of the limit because an off-by-one
    is invisible in behaviour here: both neighbours produce a valid
    envelope, and only ``detail``'s presence separates them.
    """
    result = await soap_call(
        http_server, body=fault_11(nested_detail(levels)), status=500)

    assert result['ok'] is False
    assert result['error']['code'] == 'SOAP_FAULT'
    assert result['status_code'] == 500

    fault = result['protocol_details']['soap_fault']
    assert fault['code'] == 'soap:Client'
    assert fault['reason'] == 'Invalid account number'
    assert fault['actor'] == 'urn:billing'
    if kept:
        assert fault['detail'] is not None
        assert '<a>' in fault['detail']
    else:
        # Reported as an *absent* detail -- the value R19 already defines
        # for a Fault carrying none -- rather than as a truncated string,
        # which would be XML no consumer could parse.
        assert fault['detail'] is None


async def test_n5_a_refused_fault_detail_says_so_in_the_log(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``None`` alone would let a real detail vanish silently.

    A withheld detail is deliberately indistinguishable from an absent
    one in the envelope -- that indistinguishability is what keeps the
    fault contract stable under hostile input. So the operator-facing
    signal has to be the log, and it has to name the limit crossed, or
    the first person debugging a missing detail has nothing to search
    for.
    """
    caplog.set_level(logging.WARNING, logger='asyncio_gateway')

    result = await soap_call(
        http_server, body=fault_11(nested_detail(1000)), status=500)

    assert result['protocol_details']['soap_fault']['detail'] is None
    warnings = [
        record.getMessage() for record in caplog.records
        if record.levelno == logging.WARNING
        and 'max_fault_detail_depth' in record.getMessage()
    ]
    assert len(warnings) == 1
    assert str(MAX_FAULT_DETAIL_DEPTH) in warnings[0]


def test_n5_the_depth_check_answers_a_tree_the_parser_can_hold() -> None:
    """The guard must survive the input it exists to refuse.

    50,000 levels is far past any interpreter's default frame budget and
    is what the XML *parser* already tolerates, so this pins the asymmetry
    the whole fix rests on: parsing was never the recursive step, and the
    guard must be able to answer for anything the parse can produce.
    """
    deep = fromstring(f'<detail>{nested_detail(50000)}</detail>')

    assert exceeds_depth(deep, MAX_FAULT_DETAIL_DEPTH) is True
    assert exceeds_depth(fromstring('<detail><a>x</a></detail>'), 64) is False


def recursive_exceeds_depth(
    node: Element,
    max_depth: int,
    depth: int = 1,
) -> bool:
    """Measure depth recursively, as ``exceeds_depth`` must not.

    Deliberately *not* production code: this is the counterfactual the
    row below rejects. It is the plausible implementation --
    self-bounded at ``max_depth``, so it never runs away on its own --
    and it is exactly what would pass every test that merely feeds
    ``exceeds_depth`` a deep tree and checks the answer. What separates
    it from the real one is that it spends a frame per level, so under
    a squeezed recursion budget it raises ``RecursionError`` where the
    iterative walk answers.

    Args:
        node: The subtree to measure, counted as level ``depth``.
        max_depth: The deepest level permitted.
        depth: The level ``node`` sits at. Defaults to 1.

    Returns:
        True when some node sits deeper than ``max_depth``.
    """
    if depth > max_depth:
        return True
    return any(
        recursive_exceeds_depth(child, max_depth, depth + 1)
        for child in node)


def test_n5_the_depth_check_needs_no_frames_of_its_own() -> None:
    """Iterative is a security property here, not a style preference.

    The row above does not on its own distinguish an iterative walk from
    a *recursive* one, and that is worth stating because the difference
    looks academic and is not. A recursive check bounded by ``max_depth``
    self-limits at 65 frames, so it passes every test that merely feeds
    it a deep tree -- which is precisely how a guard that still depends
    on the frame budget would ship.

    What it cannot do is answer without frames. ``sys.setrecursionlimit``
    belongs to the *application* embedding this library, not to this
    library, so a bound stated here has to hold whatever the process
    chose -- the same argument
    ``MAX_MULTIPART_DEPTH`` makes for declining to derive itself from the
    limit. With the headroom squeezed to under the depth cap, a recursive
    walk raises ``RecursionError`` on the exact input it exists to
    refuse, converting a clean refusal back into the crash N5 reported;
    an iterative one answers.

    The limit is restored in a ``finally`` because leaving it lowered
    would fail unrelated tests in whatever order they happen to run.

    **The headroom is a fraction of the depth cap, not a fixed count,
    and that is AGW-N2.** This row used to squeeze the budget to
    ``frames + 10``, chosen as "comfortably under 64" -- reasoning about
    the *walk's* frames alone, as though the walk were the only thing
    the interpreter runs. It is not. Under ``--cov`` the tracer's own
    callback needs frames of its own on top of every call the walk
    makes, and on 3.10/3.11 that overhead is enough to exhaust a
    ten-frame budget before the iterative walk can finish: measured on
    real 3.10.18 and 3.11.15, the smallest margin that completes under
    the project's ``ctrace`` core is **11**, against **3** on 3.12+
    where the tracer is cheaper. So the row raised ``RecursionError``
    from inside ``coverage``'s collector -- green under ``--no-cov``,
    red on the two oldest legs of a CI matrix that runs *with* coverage,
    and red for a reason that had nothing to do with the property being
    guarded.

    Widening it to a fixed larger number would only move the cliff. The
    margin is therefore ``MAX_FAULT_DETAIL_DEPTH // 2``: still strictly
    under the depth cap, so a recursive walk self-bounding at the cap
    provably cannot complete -- which is the whole discrimination this
    row exists for -- while leaving room for any tracer the suite runs
    under. And it is *tied to the constant*, so raising the cap widens
    the budget with it rather than silently re-creating the squeeze.

    The discrimination is asserted rather than assumed:
    :func:`recursive_exceeds_depth` below is the counterfactual this row
    must reject, and it is run under the same lowered limit. If the
    margin were ever widened far enough for a recursive walk to
    complete, that assertion fails and says so -- the guard cannot
    quietly stop discriminating.
    """
    deep = fromstring(f'<detail>{nested_detail(5000)}</detail>')

    frames = 0
    frame: Any = sys._getframe()
    while frame is not None:
        frames += 1
        frame = frame.f_back

    original = sys.getrecursionlimit()
    sys.setrecursionlimit(frames + MAX_FAULT_DETAIL_DEPTH // 2)
    try:
        assert exceeds_depth(deep, MAX_FAULT_DETAIL_DEPTH) is True

        # The counterfactual, under the same budget: a recursive walk
        # bounded by the same cap must still run out of frames. This is
        # what makes the row a test of *iterativeness* rather than a
        # test that some function returned True.
        with pytest.raises(RecursionError):
            recursive_exceeds_depth(deep, MAX_FAULT_DETAIL_DEPTH)
    finally:
        sys.setrecursionlimit(original)


def test_n5_a_wide_fault_detail_is_not_mistaken_for_a_deep_one() -> None:
    """Breadth is not depth, and the walk must not confuse them.

    The stack this check keeps holds siblings as well as descendants, so
    an implementation inferring depth from ``len(stack)`` -- which the
    multipart walk legitimately does, because *its* stack is a single
    nesting path -- would read 5000 siblings as 5000 levels and refuse an
    ordinary detail listing field errors. Carrying the depth per node is
    what avoids that, and this row is what fails if it is dropped.
    """
    wide = fromstring('<detail>' + '<a>x</a>' * 5000 + '</detail>')

    assert exceeds_depth(wide, MAX_FAULT_DETAIL_DEPTH) is False


async def test_the_response_body_is_still_on_the_envelope_after_a_fault(
    http_server: RecordingHTTPServer,
) -> None:
    """Invariant E11: ``ok=False`` never costs the caller the response."""
    result = await soap_call(http_server, body=fault_11(), status=500)

    assert result['text'] == fault_11().decode()
    assert result['headers'] != {}


# --- R19: malformed XML, and XML that is not a SOAP envelope ---------------


async def test_r19_malformed_xml_is_serialization_with_its_position(
    http_server: RecordingHTTPServer,
) -> None:
    """Never a silent ``{}``: the parser's line and column are preserved."""
    result = await soap_call(
        http_server, body=b'<e:Envelope><e:Body><Unclosed></e:Body>')

    assert result['ok'] is False
    assert result['error']['code'] == 'SERIALIZATION'
    assert 'line' in result['error']['message']


async def test_r19_an_html_error_page_is_serialization_not_an_empty_body(
    http_server: RecordingHTTPServer,
) -> None:
    """Well-formed XML that is not an envelope, the everyday proxy case.

    The first bytes of the body are quoted back, because "this is not a
    SOAP envelope" is useless without saying what it was instead.
    """
    page = b'<html><head><title>502 Bad Gateway</title></head></html>'

    result = await soap_call(http_server, body=page, status=502)

    assert result['ok'] is False
    assert result['error']['code'] == 'SERIALIZATION'
    assert 'html' in result['error']['message']


async def test_an_envelope_in_an_unknown_namespace_is_not_a_soap_envelope(
    http_server: RecordingHTTPServer,
) -> None:
    """An ``<Envelope>`` in some other namespace is not one of ours."""
    body = (
        b'<e:Envelope xmlns:e="urn:not-soap"><e:Body/></e:Envelope>')

    result = await soap_call(http_server, body=body)

    assert result['error']['code'] == 'SERIALIZATION'


async def test_an_envelope_with_no_body_element_yields_no_body(
    http_server: RecordingHTTPServer,
) -> None:
    """A ``<Body>``-less envelope is odd but well formed; it is not a crash."""
    body = (
        f'<e:Envelope xmlns:e="{ENVELOPE_NAMESPACE[SOAP_11]}">'
        f'<e:Header/></e:Envelope>').encode()

    result = await soap_call(http_server, body=body)

    assert result['ok'] is True
    assert result['protocol_details']['soap_body'] is None


def test_parse_soap_response_returns_the_pair_it_documents() -> None:
    """The two-tuple contract, unit-tested away from the transport."""
    body, fault = parse_soap_response(
        envelope_for(SOAP_11).decode(), version=SOAP_11)
    assert body.tag == 'Result'
    assert fault is None

    body, fault = parse_soap_response(fault_11().decode(), version=SOAP_11)
    assert body is None
    assert fault['code'] == 'soap:Client'


def test_parse_soap_response_reads_an_empty_body_as_no_document() -> None:
    """An empty response is neither a body nor a Fault, and is not an error."""
    assert parse_soap_response('', version=SOAP_11) == (None, None)


# --- R18 edge cases: non-conformant servers --------------------------------


async def test_a_12_endpoint_answering_text_xml_is_parsed_with_a_warning(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-conformance is reported, not refused.

    A 1.2 endpoint answering ``text/xml`` is common enough that refusing
    it would break real integrations -- but it is also the first thing to
    look at when a 1.2 call returns a body that reads like 1.1, so it is
    logged rather than swallowed.
    """
    caplog.set_level(logging.WARNING)

    result = await soap_call(
        http_server,
        body=envelope_for(SOAP_12),
        headers=XML_11,
        soap_version=SOAP_12)

    assert result['ok'] is True
    assert any('Content-Type' in record.message for record in caplog.records)


async def test_a_server_answering_the_other_soap_version_is_parsed(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The version is taken from the document, not from our configuration.

    Parsing a 1.1 answer against the requested 1.2 namespace would find no
    ``<Body>`` and report a perfectly readable Fault as an empty success.
    """
    caplog.set_level(logging.WARNING)

    result = await soap_call(
        http_server,
        body=fault_11(),
        headers=XML_12,
        status=500,
        soap_version=SOAP_12)

    assert result['error']['code'] == 'SOAP_FAULT'
    assert result['protocol_details']['soap_fault']['code'] == 'soap:Client'


async def test_r18_mtom_multipart_related_is_refused_not_mis_parsed(
    http_server: RecordingHTTPServer,
) -> None:
    """MTOM is out of scope and says so, rather than mis-parsing.

    Refused at the transport rather than by this client, and that
    placement is the point: reading a multipart body writes its parts to
    disk as it goes, so a refusal made after the read would leave behind a
    file the caller never asked for.
    """
    result = await soap_call(
        http_server,
        body=b'--b\r\nContent-Type: text/xml\r\n\r\n<a/>\r\n--b--\r\n',
        headers={'Content-Type': 'multipart/related; boundary=b'})

    assert result['ok'] is False
    assert result['error']['code'] == 'CONFIG'
    assert 'MTOM' in result['error']['message']


# --- R18-AC8: reuse, not reimplementation ----------------------------------


async def test_r18_ac8_a_soap_call_populates_the_request_tracer(
    http_server: RecordingHTTPServer,
) -> None:
    """The tracer is the HTTP layer's, reached without reimplementing it."""
    result = await soap_call(http_server, trace_config=[request_tracer()])

    assert result['ok'] is True
    assert result['request_tracer'] != []
    assert result['request_tracer'][0] != {}


async def test_the_tracer_this_client_builds_gets_the_callers_redactions(
    http_server: RecordingHTTPServer,
) -> None:
    """Reuse means reusing the arguments too, not just the function.

    ``validated_trace_config`` takes ``redact_params`` and hands it to
    the tracer it builds, so that tracer's exception record masks the
    caller's own sensitive query-parameter names as well as the built-in
    set. ``logic/http_client.py`` passes it; this client did not, and the
    omission was invisible precisely because both clients call the same
    function and get a working tracer either way.

    What it cost is one surface out of four. A transport exception can
    name the whole URL, query string included, and
    ``on_request_exception`` records ``unwrap_cause(...)`` of it into
    ``request_tracer``. So a caller who declared
    ``redact_query_params=['session_id']`` had it honoured on the
    envelope ``url`` and in ``error['message']``, and published in the
    clear in the trace record of the same call -- invariant E9 promises
    all four credential surfaces, not three.

    Asserted against the tracer this client actually builds rather than
    through a provoked transport failure, and deliberately so: *which*
    ``aiohttp`` exceptions carry a URL is that library's business and
    changes between releases, so a test resting on one would go quietly
    vacuous the day it stopped. What this library controls -- and
    therefore what is worth pinning -- is that the caller's set reaches
    the tracer at all.

    Args:
        http_server: The loopback recording server fixture.

    Returns:
        None.
    """
    secret = 'TRACERSECRET9'
    url = f'{http_server.url_for("/soap")}?session_id={secret}'

    client = SoapRequest(
        url,
        None,
        new_envelope(url=url, protocol='SOAP', payload='<Ping/>'),
        info={'operation': 'Ping'},
        redact_params=frozenset({'session_id'}),
    )

    # Exactly one tracer, built by this library because the call named
    # no session of its own.
    tracer, = client.trace_config
    context = SimpleNamespace(results={}, start=monotonic_now())
    for callback in tracer.on_request_exception:
        await callback(
            None,
            context,
            SimpleNamespace(exception=ValueError(f'refused {url}')),
        )

    recorded = context.results['on_request_exception_message']
    assert secret not in recorded
    assert REDACTED in recorded


async def test_r18_ac8_the_deadline_is_honoured(
    http_server: RecordingHTTPServer,
) -> None:
    """A SOAP call times out on the deadline it was configured with.

    The handler never answers, so the deadline is the only thing this
    waits on -- the behaviour under test, not a sleep to let async work
    settle.
    """
    http_server.gate('/slow')
    started = asyncio.get_running_loop().time()

    result = await request(
        url=http_server.url_for('/slow'),
        data='<Ping/>',
        protocol='SOAP',
        protocol_info={'timeout': SHORT_DEADLINE},
    )

    elapsed = asyncio.get_running_loop().time() - started
    assert result['ok'] is False
    assert result['error']['code'] == 'TIMEOUT'
    assert result['status_code'] == 504
    assert elapsed < DEADLINE_TOLERANCE


async def test_r18_ac8_an_over_cap_body_is_refused_before_it_is_parsed(
    http_server: RecordingHTTPServer,
    never_parsed: list[str],
) -> None:
    """R14's cap is what bounds the input R19's parser decision rests on.

    Asserted with ``never_parsed`` for the same reason the DOCTYPE tests
    are: the security argument for the stdlib parser depends on the body
    reaching it already bounded, so "refused" has to mean "refused before
    the parse", not "reported afterwards".
    """
    oversized = envelope_for(SOAP_11, f'<Big>{"x" * 4096}</Big>')

    result = await soap_call(
        http_server, body=oversized, max_response_bytes=512)

    assert result['ok'] is False
    assert result['error']['code'] == 'RESPONSE_TOO_LARGE'
    assert never_parsed == []


async def test_r18_ac8_an_over_cap_chunked_body_is_refused_mid_stream(
    http_server: RecordingHTTPServer,
    never_parsed: list[str],
) -> None:
    """A body that declares no length is still bounded, on the chunk."""
    http_server.respond(
        '/soap',
        headers=XML_11,
        chunks=[b'<e:Envelope>', b'x' * 4096, b'</e:Envelope>'])

    result = await request(
        url=http_server.url_for('/soap'),
        data='<Ping/>',
        protocol='SOAP',
        protocol_info={'max_response_bytes': 512},
    )

    assert result['error']['code'] == 'RESPONSE_TOO_LARGE'
    assert never_parsed == []


async def test_r18_ac8_the_envelope_key_set_is_the_one_every_protocol_returns(
    http_server: RecordingHTTPServer,
) -> None:
    """SOAP fills the shared envelope; it does not build a shape of its own.

    ``protocol_details`` is the only per-protocol variation, and it is
    nested precisely so the top-level key set stays invariant (E1).
    """
    ok = await soap_call(http_server)
    failed = await soap_call(http_server, body=fault_11(), status=500)

    assert set(ok) == set(failed)
    assert set(ok['protocol_details']) == {
        'soap_version', 'soap_body', 'soap_fault'}


async def test_a_configuration_error_escapes_before_anything_is_dispatched(
    http_server: RecordingHTTPServer,
) -> None:
    """A caller's own mistake is raised, not enveloped.

    The entry point builds the protocol object *outside* the block that
    converts an ``AsyncGatewayError`` into an envelope, so raising in the
    constructor is what makes this reach the caller as an exception before
    a single byte is sent -- the contract ``request()`` documents.
    """
    with pytest.raises(ConfigurationError):
        await soap_call(http_server, soap_version='1.9')

    assert http_server.requests == []


async def test_grep_lxml_finds_nothing_in_the_package() -> None:
    """R19-AC7: ``lxml`` stays dev-only, asserted mechanically.

    A grep in prose is a claim; a grep in a test is a check. The XML
    dependency decision (OQ6) rests on the runtime package adding none, so
    the absence is enforced rather than remembered.
    """
    from pathlib import Path

    package = Path(soap_client.__file__).parent.parent
    offenders = [
        path.name
        for path in package.rglob('*.py')
        if 'lxml' in path.read_text()
    ]

    assert offenders == []


# --- R28: the type arms of the two SOAP config validators ------------------


@pytest.mark.parametrize(
    'soap_action',
    [
        pytest.param(b'urn:Order', id='bytes'),
        pytest.param(['urn:Order'], id='list'),
        pytest.param(42, id='int'),
        pytest.param(Element('Action'), id='element'),
    ],
)
def test_a_non_string_soap_action_is_refused_by_type(
    soap_action: Any,
) -> None:
    """R28: ``soap_action`` must be a ``str`` or None, and nothing else.

    The type arm, distinct from the character arm below it. Without it a
    ``bytes`` action reaches the ``for char in ...`` scan, where
    ``'"' in b'...'`` raises ``TypeError`` rather than the
    ``ConfigurationError`` this key's other rejections produce -- so one
    wrong type would be reported as a library bug and every other as the
    caller's error. The message names the type it got, because that is
    the thing the caller has to change.
    """
    with pytest.raises(ConfigurationError) as refusal:
        validated_soap_action(soap_action)

    assert 'protocol_info["soap_action"]' in str(refusal.value)
    assert type(soap_action).__name__ in str(refusal.value)


@pytest.mark.parametrize(
    'soap_action, expected',
    [
        pytest.param(None, None, id='absent'),
        pytest.param('', None, id='empty-is-absent'),
        pytest.param('urn:Order#place', 'urn:Order#place', id='ordinary'),
    ],
)
def test_an_acceptable_soap_action_passes_through(
    soap_action: Any,
    expected: Any,
) -> None:
    """R28: the accepting arms, so the refusals above are not vacuous.

    An empty string normalises to None rather than being sent as an
    empty ``SOAPAction`` header: the two mean the same thing to a
    server, and collapsing them here is what keeps the header builder
    from having to know it.
    """
    assert validated_soap_action(soap_action) == expected


@pytest.mark.parametrize(
    'soap_headers',
    [
        pytest.param('<Security/>', id='xml-as-text'),
        pytest.param({'Security': {}}, id='mapping'),
        pytest.param([Element('Security')], id='list-of-elements'),
        pytest.param(7, id='int'),
    ],
)
def test_non_element_soap_headers_are_refused(soap_headers: Any) -> None:
    """R28: the header block is an ``Element``, never text or a mapping.

    ``'<Security/>'`` is the case worth naming: it is what a caller
    reaches for first, and accepting it would mean concatenating
    caller-supplied text into an envelope this library then claims is
    well formed. Refusing it at the boundary is what makes
    ``build_envelope`` unable to emit malformed XML at all, so the
    message says to build the block with ElementTree rather than only
    that the value was wrong.
    """
    with pytest.raises(ConfigurationError) as refusal:
        validated_soap_headers(soap_headers)

    assert 'protocol_info["soap_headers"]' in str(refusal.value)
    assert 'ElementTree' in str(refusal.value)


def test_an_element_or_none_is_accepted_as_the_soap_header_block() -> None:
    """R28: the accepting arms of the header validator.

    Returned by identity, not copied: the caller's element is what
    ``build_envelope`` appends, so a mutation after this call is the
    caller's own and this library does not silently snapshot it.
    """
    block = Element('Security')

    assert validated_soap_headers(None) is None
    assert validated_soap_headers(block) is block


# --- R28: a 1.2 Fault missing the child the reader looks for ---------------


def fault_12_of(children: str) -> bytes:
    """Return a 1.2 Fault envelope carrying exactly ``children``.

    :func:`fault_12` always emits ``Code``, ``Reason`` and ``Role``, so it
    cannot express the partial faults R28 needs. This one places whatever
    it is handed and nothing else.

    Args:
        children: The ``<Fault>`` children as XML text, possibly empty.

    Returns:
        The Fault envelope as UTF-8 bytes.
    """
    return envelope_for(SOAP_12, f'<e:Fault>{children}</e:Fault>')


async def test_a_12_fault_with_no_code_element_reports_an_empty_code(
    http_server: RecordingHTTPServer,
) -> None:
    """R28: a Fault whose ``Code`` is absent is read, not crashed on.

    ``Code`` is mandatory in the 1.2 schema, which is exactly why the
    arm that copes without one is never reached by a conformant server
    and is therefore the arm that rots. A non-conformant endpoint -- or
    an intermediary that rewrites a fault -- sends this, and the reader
    must still produce the flat shape every consumer branches on.
    Delete the ``if code is not None`` guard and ``fault.find(...)``
    returns None, so ``code.findtext(...)`` raises ``AttributeError``
    from inside a thread: this library's own bug, reported to the caller
    as neither a Fault nor a parse failure.

    ``reason`` is asserted beside the empty code so the test also proves
    the surrounding read still ran; a guard that bailed out of the whole
    function on a missing ``Code`` would satisfy the first assertion
    alone.
    """
    body = fault_12_of(
        '<e:Reason><e:Text xml:lang="en">no code here</e:Text></e:Reason>')

    result = await soap_call(
        http_server,
        body=body,
        headers=XML_12,
        status=500,
        soap_version=SOAP_12)

    assert result['error']['code'] == 'SOAP_FAULT'
    fault = result['protocol_details']['soap_fault']
    assert fault['code'] == ''
    assert fault['subcodes'] == []
    assert fault['reason'] == 'no code here'
    # The message says so out loud rather than showing a blank where the
    # code belongs, which is what a caller reading the log needs.
    assert '<no code>' in result['error']['message']


async def test_a_12_fault_with_no_reason_element_reports_an_empty_reason(
    http_server: RecordingHTTPServer,
) -> None:
    """R28: the same for ``Reason``, and it fails the same way.

    The pair to the test above, and not redundant with it: the two
    guards are separate statements over separate elements, so a reader
    that dropped only the ``Reason`` one would pass the ``Code`` test
    and raise ``AttributeError`` here. The ``Code`` side is asserted
    beside the empty reason for the same reason as above -- to prove the
    rest of the read survived the missing element.
    """
    body = fault_12_of('<e:Code><e:Value>e:Receiver</e:Value></e:Code>')

    result = await soap_call(
        http_server,
        body=body,
        headers=XML_12,
        status=500,
        soap_version=SOAP_12)

    assert result['error']['code'] == 'SOAP_FAULT'
    fault = result['protocol_details']['soap_fault']
    assert fault['reason'] == ''
    assert fault['code'] == 'e:Receiver'
    assert '<no reason>' in result['error']['message']


async def test_a_12_fault_with_neither_code_nor_reason_is_still_a_fault(
    http_server: RecordingHTTPServer,
) -> None:
    """R28: both arms at once, which is the case a bare ``<Fault/>`` is.

    Worth its own row because the two guards could each be correct and
    the *combination* still be reported as a success: a reader that
    treated "no code and no reason" as "not really a fault" would return
    ``(body, None)`` here, and an ``ok=True`` envelope is the one outcome
    an empty fault must never produce.
    """
    result = await soap_call(
        http_server,
        body=fault_12_of(''),
        headers=XML_12,
        status=500,
        soap_version=SOAP_12)

    assert result['ok'] is False
    assert result['error']['code'] == 'SOAP_FAULT'
    fault = result['protocol_details']['soap_fault']
    assert fault == {
        'code': '', 'subcodes': [], 'reason': '', 'actor': None,
        'detail': None}


# --- R28: the caller-supplied-session arm of handle_request ----------------


async def test_a_caller_supplied_session_carries_the_soap_call(
    http_server: RecordingHTTPServer,
) -> None:
    """R28: a supplied session is dispatched on, and left open.

    The arm above the ``async with aiohttp.ClientSession(...)`` block,
    and it is the arm a caller reaches for to get connection pooling
    across consecutive calls -- so closing it, or quietly building a
    second session and ignoring theirs, defeats the only reason the key
    exists. Untested, either regression is invisible: the call still
    succeeds and only the pool is gone.

    ``request_tracer`` is asserted empty in the same breath because that
    is the documented consequence of supplying one: trace configs are a
    session-level thing, so this library attaches none to a session it
    does not own and must not report collectors it never collected.
    """
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    try:
        result = await soap_call(http_server, session=session)

        assert result['ok'] is True
        assert result['protocol_details']['soap_body'].tag == 'Result'
        assert session.closed is False
        assert result['request_tracer'] == []
    finally:
        await session.close()


async def test_two_soap_calls_on_one_supplied_session_reuse_its_pool(
    http_server: RecordingHTTPServer,
) -> None:
    """R28: pooling is the point of the arm above, so it is measured.

    Read off the tracer's ``on_connection_reuseconn`` event, which fires
    only when a connection came out of the pool rather than being
    dialled. The tracer is attached to the caller's own session, because
    a session this library created is closed on the way out and could
    not show reuse at all -- which is precisely the property the
    supplied-session arm exists to provide, and the one a test asserting
    only ``ok is True`` would let regress.
    """
    tracer = request_tracer()
    session = aiohttp.ClientSession(
        trace_configs=[tracer],
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT))
    try:
        await soap_call(http_server, session=session)
        assert 'on_connection_reuseconn' not in tracer.results_collector

        await soap_call(http_server, session=session)

        assert 'on_connection_reuseconn' in tracer.results_collector
        assert session.closed is False
    finally:
        await session.close()


# --- R28: the breaker's own refusal, and an undecodable body ---------------


async def test_an_open_circuit_refuses_a_soap_call_without_dispatching_it(
    http_server: RecordingHTTPServer,
) -> None:
    """R28: ``CircuitOpen`` becomes ``CIRCUIT_OPEN``/503, not a 502.

    ``CircuitOpen`` is a ``failsafe`` exception with nothing underneath
    it the generic transport classifier can read, so without its own
    ``except`` clause it falls through to ``transport_error_for`` and
    reaches the caller as a 502 -- a status a client retries, into a
    circuit that is open precisely to stop them retrying. 503 says "not
    now" instead.

    Driven with two real calls rather than by raising the exception at a
    seam: the first has to *fail and be counted* for the second to be
    refused, and a breaker that never counted a SOAP failure would pass
    a test that injected the refusal directly. The recorded request count
    is what proves the second call never reached the wire -- the
    behaviour a fast-fail exists for, and the thing a status assertion
    alone cannot see.
    """
    http_server.respond('/loop', status=302, headers={'Location': '/loop'})
    info: dict[str, Any] = {
        'max_redirects': 0,
        'circuit_breaker_config': {'maximum_failures': 1},
    }

    first = await request(
        url=http_server.url_for('/loop'),
        data='<Ping/>',
        protocol='SOAP',
        protocol_info=info,
    )
    second = await request(
        url=http_server.url_for('/loop'),
        data='<Ping/>',
        protocol='SOAP',
        protocol_info=info,
    )

    assert first['error']['code'] == 'TRANSPORT'
    assert second['ok'] is False
    assert second['error']['code'] == 'CIRCUIT_OPEN'
    assert second['status_code'] == 503
    assert len(http_server.requests) == 1


async def test_a_transport_failure_the_breaker_aborted_on_is_still_mapped(
    http_server: RecordingHTTPServer,
) -> None:
    """R28: the arm that catches a transport error the retry loop re-raised.

    Two routes lead out of ``_exchange`` for the same underlying fault
    and they need separate clauses. When the retry loop *counts* the
    failure it wraps it in ``RetriesExhausted``; when the caller marks
    the exception **abortable** the loop re-raises it as itself, so an
    ``aiohttp.ClientError`` or an ``asyncio.TimeoutError`` arrives here
    unwrapped. Drop this clause and that second route escapes
    ``_exchange`` entirely -- past the one conversion point, out of
    ``request()`` as a raw ``aiohttp`` exception -- which is the shape
    this library exists to stop a caller having to handle per protocol.

    Driven with a gated handler and a deadline of tens of milliseconds,
    so the timeout is the only thing awaited: the behaviour under test,
    not a sleep to let unrelated async work settle.
    """
    http_server.gate('/slow')

    result = await request(
        url=http_server.url_for('/slow'),
        data='<Ping/>',
        protocol='SOAP',
        protocol_info={
            'timeout': SHORT_DEADLINE,
            'circuit_breaker_config': {
                'retry_config': {
                    'allowed_retries': 0,
                    # Abortable, so the loop re-raises the timeout as
                    # itself instead of wrapping it -- which is the whole
                    # point of the row.
                    'abortable_exceptions': [
                        aiohttp.ClientError, asyncio.TimeoutError],
                },
            },
        },
    )

    assert result['ok'] is False
    assert result['error']['code'] == 'TIMEOUT'
    assert result['status_code'] == 504


async def test_an_undecodable_response_body_is_reported_before_any_parse(
    http_server: RecordingHTTPServer,
    never_parsed: list[str],
) -> None:
    """R28: bytes that are not text are ``SERIALIZATION``, and are not parsed.

    The transport decodes lossily and records *why* rather than raising,
    so that the salvageable text still reaches the envelope (R13/M9). It
    is this client that has to act on that record. Drop the check and the
    lossy text -- with U+FFFD where the undecodable bytes were -- is
    handed to the parser as though it were the server's answer, which
    either raises a misleading "not well-formed XML" naming a position
    that does not exist in what the server sent, or, worse, parses into a
    document the server never sent.

    ``never_parsed`` is what makes that a *pre-parse* assertion rather
    than a claim about the error code alone, which a check placed after
    the parse would satisfy just as well.
    """
    result = await soap_call(
        http_server, body=b'\xff\xfe<e:Envelope/>', headers=XML_11)

    assert result['ok'] is False
    assert result['error']['code'] == 'SERIALIZATION'
    assert 'not decodable text' in result['error']['message']
    assert never_parsed == []
    # E11: the lossily-decoded body is still the caller's to inspect,
    # which is the whole reason the transport records the failure rather
    # than raising and losing the body with it.
    assert result['text']
    assert result['protocol_details']['soap_body'] is None


# --- NEW-R10-3: the cross-origin hatch, honoured on SOAP too ---------------


async def test_the_cross_origin_hatch_is_honoured_on_soap(
    http_server: RecordingHTTPServer,
) -> None:
    """A header SOAP's caller declares safe actually survives the hop.

    The load-bearing half of NEW-R10-3, and the one a refusal test
    cannot reach. ``SoapRequest`` passed ``validated_session``'s
    ``frozenset()`` default and never read
    ``protocol_info['cross_origin_headers']`` at all -- so the key was
    silently ignored, and the README's claim that every HTTP key applies
    to SOAP had an undocumented fourth exception.

    Wiring the *validator* in fixes the refusals. It does not fix this:
    a client that validated the value and then discarded it would pass
    every hostile-value row while the hatch still did nothing. Only
    driving a real cross-origin redirect and reading what the second
    origin received can tell the two apart, so that is what this does.

    ``Authorization`` rides along as the control. The hatch widens the
    allowlist into headers this library has no opinion about; it must
    never re-admit one it recognises as a credential, or it is the leak
    again spelled as a config key.
    """
    elsewhere = RecordingHTTPServer()
    await elsewhere.start()
    try:
        elsewhere.respond('/end', body=envelope_for(SOAP_11), headers=XML_11)
        http_server.respond(
            '/start',
            status=302,
            headers={'Location': elsewhere.url_for('/end')})

        envelope = await request(
            url=http_server.url_for('/start'),
            protocol='SOAP',
            data='<Ping/>',
            protocol_info={
                'headers': {
                    'X-Request-Id': 'trace-me',
                    'Authorization': 'Bearer supersecret',
                },
                'cross_origin_headers': ['X-Request-Id'],
            },
        )

        assert envelope['ok'] is True
        crossed = elsewhere.requests[-1].headers
        assert crossed.get('X-Request-Id') == 'trace-me', (
            'the declared header did not survive the cross-origin hop, '
            'so cross_origin_headers is validated and then discarded -- '
            'which is NEW-R10-3 with a nicer error message.')
        assert 'Authorization' not in crossed, (
            'the hatch must widen into the unknown region only; a '
            'credential header crossing an origin boundary is the leak '
            'the allowlist exists to prevent.')
        # The first origin, which the caller did address, gets both.
        assert http_server.requests[-1].headers.get(
            'Authorization') == 'Bearer supersecret'
    finally:
        await elsewhere.close()


async def test_an_undeclared_header_still_stops_at_the_origin_on_soap(
    http_server: RecordingHTTPServer,
) -> None:
    """The control: the hatch opens for the named header and no other.

    Without this row the one above is satisfied by a SOAP client that
    forwards *everything* across an origin boundary -- which would pass
    "the declared header crossed" while destroying the guard the
    declaration exists to make an exception to.
    """
    elsewhere = RecordingHTTPServer()
    await elsewhere.start()
    try:
        elsewhere.respond('/end', body=envelope_for(SOAP_11), headers=XML_11)
        http_server.respond(
            '/start',
            status=302,
            headers={'Location': elsewhere.url_for('/end')})

        envelope = await request(
            url=http_server.url_for('/start'),
            protocol='SOAP',
            data='<Ping/>',
            protocol_info={
                'headers': {'X-Request-Id': 'trace-me', 'X-Other': 'nope'},
                'cross_origin_headers': ['X-Request-Id'],
            },
        )

        assert envelope['ok'] is True
        crossed = elsewhere.requests[-1].headers
        assert crossed.get('X-Request-Id') == 'trace-me'
        assert 'X-Other' not in crossed, (
            'only the header the caller named may cross; forwarding the '
            'rest turns an opt-in hatch into no allowlist at all.')
    finally:
        await elsewhere.close()


# --- NEW-R10-5: a key that cannot apply is refused, not ignored ------------


@pytest.mark.parametrize(
    'serialization',
    [
        pytest.param(42, id='not-callable'),
        pytest.param(str, id='callable-returning-str'),
        pytest.param(
            lambda obj: b'{}', id='callable-returning-bytes'),
    ],
)
async def test_serialization_is_refused_on_soap_rather_than_ignored(
    http_server: RecordingHTTPServer,
    serialization: Any,
) -> None:
    """An inapplicable key earns a refusal, never silence (NEW-R10-5).

    ``serialization`` names the JSON encoder, and a SOAP body is never
    JSON: the Content-Type this client sets routes the envelope to the
    raw-body filter byte for byte. So there is genuinely nothing here
    for an encoder to encode, and building the session without
    ``json_serialize=`` is correct.

    What was wrong is that the key was then **accepted in silence**.
    ``42`` and a bytes-returning callable each raised
    ``ConfigurationError`` on HTTP and were accepted here, so the
    README's parity claim had a fifth undocumented exception -- the same
    shape as NEW-R10-3, one key over, found only because that finding
    prompted a sweep of the rest.

    The third row is the load-bearing one: a *valid* serialiser is
    refused too. Refusing only the malformed values would leave the
    silent no-op live for exactly the caller who did everything right
    and still had their encoder ignored.
    """
    with pytest.raises(ConfigurationError) as caught:
        await soap_call(http_server, serialization=serialization)

    assert caught.value.code == 'CONFIG'
    assert 'serialization' in str(caught.value)
    assert http_server.requests == []

"""Transport doubles for the five-protocol envelope contract (S9).

R8-AC2 asserts that ``request()`` returns one key set for every protocol on
both the success and the failure path. Proving that needs each protocol to
actually *reach* both paths, and none of them may open a socket doing so,
so every row is driven through ``request()`` with the last seam before the
wire replaced: ``handle_http_request`` for the HTTP family,
``aioftp.Client.context`` for FTP and ``asyncssh.connect`` for SFTP.
``'SOAP'`` rides the same transport boundary as the HTTP family but
imports it into its own module, so its seam is that module's name for it
-- patching ``http_client.handle_http_request`` alone would leave a SOAP
row opening a real socket, which is why the two are installed separately
rather than sharing one branch.

Doubling *there* and no deeper is the point. Everything between the seam
and the caller still runs for real -- the protocol client's own envelope
mapping and the entry point's one conversion point -- and that is precisely
what the contract is about. The stubs mirror each transport library's own
published surface rather than this package's current use of it, so a
rewritten FTP or SFTP client is expected to turn these same doubles green
without editing them.

One caveat for whoever rewrites FTP: **the FTP double has never run.**
On both FTP contract rows the installed ``aioftp.Client.context`` seam
and ``StubFTPClient`` are entered zero times, because
``logic/ftp_client.py`` raises ``UnboundLocalError`` while evaluating
``ssl=verify_ssl`` as a call argument -- before the context is ever
built. That is what the row's ``xfail`` records. So the FTP stubs here
are an unverified prediction of what the rewritten client will need,
and S10 is the first story that will actually execute them; expect to
correct them rather than to find them already right.
"""

import asyncio
import os
import pathlib
import socket
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Optional

import aioftp

import aiohttp
from aiohttp import BasicAuth
from aiohttp.client_reqrep import ConnectionKey

import asyncssh

import pytest

from async_gateway.helpers.internal.request_helper import HttpResult
from async_gateway.logic import http_client, soap_client

# What a doubled transport says when it refuses. One string, so a test that
# needs to recognise the double's own failure has something to match on.
REFUSED = 'the transport double refused the connection'

JSON_BODY = b'{"value": 1}'

#: What the SOAP double answers with: a minimal, well-formed 1.1 envelope.
#: The SOAP row cannot be served :data:`JSON_BODY` like the HTTP rows are
#: -- a body that is not an envelope is a `SERIALIZATION` failure by R19,
#: so the "success" row would take the failure path and assert nothing it
#: meant to.
SOAP_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<soap:Envelope '
    b'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    b'<soap:Body><Result>1</Result></soap:Body>'
    b'</soap:Envelope>'
)

AUTH = BasicAuth('user', 'password')

# One `request()` call per protocol that is valid for that protocol and for
# no other reason. FTP and SFTP address a bare host and name the operation
# they want in `protocol_info`; the HTTP family addresses a schemed URL and
# names a verb. `'SOAP'` addresses a schemed URL and needs no
# `protocol_info` at all: the version defaults to 1.1 and the verb is not
# the caller's to choose.
CONTRACT_CALL: dict[str, dict[str, Any]] = {
    'HTTP': {
        'url': 'http://host/p',
        'protocol_info': {'request_type': 'GET'},
    },
    'HTTPS': {
        'url': 'https://host/p',
        'protocol_info': {'request_type': 'GET'},
    },
    'FTP': {
        'url': 'host',
        'protocol_info': {'command': 'download', 'server_path': '/f'},
    },
    'SFTP': {
        'url': 'host',
        'protocol_info': {'mode': 'get', 'remote_path': '/f'},
    },
    'SOAP': {
        'url': 'http://host/p',
        'protocol_info': {},
    },
}


def contract_call(protocol: str) -> dict[str, Any]:
    """Return the ``request()`` keyword arguments for one contract row.

    Args:
        protocol: A protocol name from :data:`CONTRACT_CALL`.

    Returns:
        Keyword arguments for ``request()``: the URL, the protocol name,
        an auth object and a fresh copy of the protocol configuration, so
        a client that mutates what it was handed cannot reach the next
        row.
    """
    call = CONTRACT_CALL[protocol]
    return {
        'url': call['url'],
        'protocol': protocol,
        'auth': AUTH,
        'protocol_info': dict(call['protocol_info']),
    }


@asynccontextmanager
async def entered(value: Any) -> AsyncIterator[Any]:
    """Yield ``value`` from an async context manager that does nothing else.

    Args:
        value: The object the ``async with`` block should bind.

    Yields:
        ``value``, unchanged.
    """
    yield value


class RefusingTransport:
    """An async context manager whose entry refuses the connection.

    The shape a dead host produces in ``aioftp`` and ``asyncssh`` alike:
    the call that builds the context manager succeeds, and the connection
    attempt inside ``__aenter__`` is what fails.
    """

    async def __aenter__(self) -> Any:
        """Refuse the connection the way an unreachable host does.

        Returns:
            Never; this always raises.

        Raises:
            ConnectionRefusedError: Always.
        """
        raise ConnectionRefusedError(REFUSED)

    async def __aexit__(self, *exc_info: Any) -> bool:
        """Leave the context, suppressing nothing.

        Args:
            exc_info: The exception triple, unused.

        Returns:
            False, so any exception propagates.
        """
        return False


class StubFTPClient:
    """The slice of ``aioftp.Client`` the contract row's transfer uses.

    Only the ``download`` the row's ``command`` selects and the ``stat``
    that reads back what it moved. A row exercising another command adds
    that method here rather than the stub carrying every operation
    ``aioftp`` has in case one is wanted later.
    """

    async def download(self, *args: Any, **kwargs: Any) -> None:
        """Accept a download and report success by not raising.

        Args:
            args: The remote and local paths, unused.
            kwargs: Transfer options such as ``write_into``, unused.

        Returns:
            None, as ``aioftp`` does.
        """

    async def stat(self, path: str) -> dict[str, str]:
        """Return the metadata ``aioftp`` reports for a transferred file.

        Args:
            path: The remote path being described.

        Returns:
            A mapping of MLSx facts, the shape ``aioftp.Client.stat``
            returns.
        """
        return {'size': str(len(JSON_BODY)), 'type': 'file'}


class StubSFTPClient:
    """The slice of ``asyncssh``'s SFTP client a transfer uses."""

    async def lstat(self, path: str) -> asyncssh.SFTPAttrs:
        """Return the attributes ``asyncssh`` reports for a remote path.

        The real ``asyncssh.SFTPAttrs`` is returned rather than a
        stand-in, so a client reading it is held to the type the library
        actually hands back.

        Args:
            path: The remote path being described.

        Returns:
            Attributes describing a regular file.
        """
        return asyncssh.SFTPAttrs(size=len(JSON_BODY), permissions=0o100644)

    async def listdir(self, path: str) -> list[str]:
        """Return the entries ``asyncssh`` reports for a remote directory.

        Args:
            path: The remote directory being listed.

        Returns:
            One file name.
        """
        return ['f']

    async def get(self, *args: Any, **kwargs: Any) -> None:
        """Accept a download and report success by not raising.

        Args:
            args: The remote and local paths, unused.
            kwargs: Transfer options such as ``recurse``, unused.

        Returns:
            None, as ``asyncssh`` does.
        """


class StubSSHConnection:
    """The slice of an ``asyncssh`` connection an SFTP transfer uses."""

    def __init__(self, sftp: Optional[StubSFTPClient] = None) -> None:
        """Record which SFTP client this connection will yield.

        Args:
            sftp: The client the channel yields, or None for the
                recording default. Parametrised so a ``LOCAL_IO`` row
                can substitute one that actually writes.
        """
        self.sftp = sftp or StubSFTPClient()

    def start_sftp_client(
        self,
    ) -> AbstractAsyncContextManager[StubSFTPClient]:
        """Open an SFTP channel on this connection.

        Returns:
            An async context manager yielding the SFTP client.
        """
        return entered(self.sftp)


def _http_transport(
    *,
    succeeds: bool,
    media_type: str = 'application/json',
    body: bytes = JSON_BODY,
) -> Callable[..., Any]:
    """Build a stand-in for the HTTP family's transport boundary.

    Args:
        succeeds: True for a transport that answers, False for one that
            refuses the connection.
        media_type: The ``Content-Type`` the answer announces. SOAP needs
            an XML one, because its client reads the media type and warns
            on a non-conformant answer.
        body: The response body the answer carries.

    Returns:
        A coroutine function with ``handle_http_request``'s signature.
    """
    async def transport(*args: Any, **kwargs: Any) -> HttpResult:
        """Answer, or refuse, without touching a socket.

        Args:
            args: The session, URL, verb and breaker, unused.
            kwargs: Per-call transport configuration, unused.

        Returns:
            One decoded exchange.

        Raises:
            aiohttp.ClientConnectionError: When built to refuse.
        """
        if not succeeds:
            raise aiohttp.ClientConnectionError(REFUSED)
        return HttpResult(
            status_code=200,
            headers={'Content-Type': media_type},
            cookies={},
            text=body.decode(),
            body=body,
            redirect_chain=[],
        )
    return transport


def _ftp_transport(*, succeeds: bool) -> Callable[..., Any]:
    """Build a stand-in for ``aioftp.Client.context``.

    Args:
        succeeds: True for a transport that connects, False for one that
            refuses the connection.

    Returns:
        A callable returning the async context manager ``aioftp`` returns.
    """
    def context(*args: Any, **kwargs: Any) -> Any:
        """Open, or refuse to open, an FTP session.

        Args:
            args: Host, port and credentials, unused.
            kwargs: TLS and timeout options, unused.

        Returns:
            An async context manager yielding an FTP client, or one that
            refuses on entry.
        """
        return entered(StubFTPClient()) if succeeds else RefusingTransport()
    return context


def _sftp_transport(*, succeeds: bool) -> Callable[..., Any]:
    """Build a stand-in for ``asyncssh.connect``.

    Args:
        succeeds: True for a transport that connects, False for one that
            refuses the connection.

    Returns:
        A callable returning the async context manager ``asyncssh``
        returns.
    """
    def connect(*args: Any, **kwargs: Any) -> Any:
        """Open, or refuse to open, an SSH connection.

        Args:
            args: Positional connection options, unused.
            kwargs: Host, credentials and host-key policy, unused.

        Returns:
            An async context manager yielding an SSH connection, or one
            that refuses on entry.
        """
        return (
            entered(StubSSHConnection()) if succeeds else RefusingTransport()
        )
    return connect


def install_transport(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    *,
    succeeds: bool,
) -> None:
    """Replace ``protocol``'s transport seam for the duration of one test.

    Args:
        monkeypatch: The pytest patcher, which undoes this on teardown.
        protocol: A normalised protocol name. Every name must own a seam
            -- including ``'SOAP'``, which was exempt only while it had
            no client module to own one.
        succeeds: True to install a transport that completes the
            operation, False to install one that refuses the connection.

    Returns:
        None.

    Raises:
        ValueError: If ``protocol`` names no seam. Installing nothing for
            a name this function does not recognise would let the row
            reach the real network, so an unrecognised name fails closed
            here.
    """
    if protocol in {'HTTP', 'HTTPS'}:
        monkeypatch.setattr(
            http_client, 'handle_http_request',
            _http_transport(succeeds=succeeds))
    elif protocol == 'SOAP':
        # Patched on `soap_client`, not on `http_client`: both modules
        # bound `handle_http_request` into their own namespace at import,
        # so replacing one leaves the other pointing at the real
        # transport.
        monkeypatch.setattr(
            soap_client, 'handle_http_request',
            _http_transport(
                succeeds=succeeds,
                media_type='text/xml; charset=utf-8',
                body=SOAP_BODY))
    elif protocol == 'FTP':
        monkeypatch.setattr(
            aioftp.Client, 'context', _ftp_transport(succeeds=succeeds))
    elif protocol == 'SFTP':
        monkeypatch.setattr(
            asyncssh, 'connect', _sftp_transport(succeeds=succeeds))
    else:
        raise ValueError(
            f'{protocol!r} owns no transport seam. Returning quietly here'
            ' would install nothing and let the row open a real'
            ' connection.')


# --- The transport-fault categories every protocol must answer for -------
#
# The four rounds of divergence this table exists to end all had one
# shape: a property was fixed in one protocol client and its siblings were
# left behind, because nothing in the suite asserted the property *across*
# protocols. M20 (the tracer on the failure path), H17 (per-call collector
# binding) and `redact_params` on the SOAP tracer were the first three,
# and `CONTRACT_ROWS` in `tests/test_envelope.py` grew rows for them.
#
# AGW-R9-1 is the fourth and got past those rows, because they assert
# tracing behaviour and this is exception coverage: `logic/soap_client.py`
# caught `(aiohttp.ClientError, asyncio.TimeoutError)` where
# `logic/http_client.py` caught the same pair *plus* `ssl.SSLError`, so a
# corrupt system CA bundle reached a SOAP caller as a raw `ssl.SSLError`
# instead of an envelope. The fifth, found by comparing the four tables
# rather than waiting for a report, was `aioftp.AIOFTPException`: FTP's
# dispatch caught it and its `transport_error_for` classified it under
# nothing, so it re-raised bare.
#
# What makes those two the *same* defect is not the exception -- it is
# that each protocol's coverage was written by hand, once, and then only
# ever checked against itself. So the table below is by **fault
# category**, not by exception class: every protocol is asked the same
# five questions in the vocabulary of what went wrong on the wire, and
# each answers in the exception class its own transport library raises.
#
# Where a protocol legitimately differs, the row records *why* rather
# than being dropped. SFTP is the one exemption and it is a real one:
# `asyncssh` runs SFTP over SSH, which has no TLS layer, so `ssl.SSLError`
# is not a fault its transport can produce -- see `TLS_EXEMPT` below,
# which names the protocol, the reason, and is itself asserted rather
# than merely commented.

#: The five families of wire failure a caller can distinguish from the
#: envelope. Each maps to the ``error['code']`` the contract promises.
#:
#: **Wire** faults only, and that word is the sixth gap rather than a
#: description: a category vocabulary built entirely from what the
#: *network* can do has no place to put a fault the **local disk**
#: produces, so the four protocols were free to answer a full disk four
#: different ways and nothing in 2981 tests could see it (NEW-R10-1).
#: :data:`LOCAL_IO_CATEGORIES` below is the second axis, kept separate
#: because its rows are driven the opposite way round -- the transport
#: must *succeed* for the disk to be reached at all.
FAULT_CATEGORIES: tuple[str, ...] = (
    'TLS',
    'TIMEOUT',
    'CONNECT',
    'DNS',
    'PROTOCOL',
)

#: A body large enough to cross a small cap, for the ``OVER_CAP`` rows.
#: 256 KiB against a 1 KiB ceiling, the pair the finding was measured
#: with -- large enough that no buffer absorbs it and the write layer is
#: genuinely reached.
OVER_CAP_BODY: bytes = b'B' * 262144

#: The ceiling an ``OVER_CAP`` row sets.
OVER_CAP_LIMIT: int = 1024

#: The ``error['code']`` a body over the ceiling must produce, on every
#: protocol that writes locally.
OVER_CAP_CODE: str = 'RESPONSE_TOO_LARGE'

#: The non-wire fault categories, and the protocols each applies to.
#: One entry today; a tuple rather than a bare string because the axis
#: is the point -- the next non-wire fault (a caller-side resource
#: limit, a clock) gets a row here instead of a bespoke test per
#: protocol, which is how the wire axis came to have five gaps.
LOCAL_IO_CATEGORIES: tuple[str, ...] = ('LOCAL_IO', 'OVER_CAP')

#: Protocols that cannot produce a given category, with the reason. An
#: entry here is an exemption that had to be argued for, not a silent gap:
#: the guard reads this mapping, and a protocol absent from it is held to
#: every category.
CATEGORY_EXEMPT: dict[str, dict[str, str]] = {
    'OVER_CAP': {
        'SOAP': (
            'SOAP enforces max_response_bytes on the response body it '
            'reads -- it shares the HTTP transport, and its own row in '
            'tests/logic/test_soap_client.py covers that. What it has '
            'no row for here is a *local write* over the cap, because '
            'it writes no local file at all: the same reason it is '
            'exempt from LOCAL_IO, asserted by the same row.'
        ),
    },
    'LOCAL_IO': {
        'SOAP': (
            'SOAP passes http_file_download_config=None to the '
            'transport unconditionally -- MTOM is out of scope (R18) '
            'and a multipart body is refused before a byte of it is '
            'written -- so a SOAP call writes no local file and has no '
            'local destination for a disk to refuse. Verified by the '
            'row below, which asserts the key is absent from the '
            'protocol rather than trusting this note.'
        ),
    },
    'TLS': {
        'SFTP': (
            'asyncssh runs SFTP over SSH, which has no TLS layer, so no '
            'ssl.SSLError can arise from its transport. Its table maps '
            'the OSError family instead, which subsumes ssl.SSLError '
            'anyway -- an SSLError reaching it reports CONNECT, not TLS.'
        ),
    },
}


def _dns_error() -> aiohttp.ClientConnectorDNSError:
    """Return the resolution failure ``aiohttp`` itself raises.

    Built with a real ``ConnectionKey`` rather than a stand-in, because
    ``ClientConnectorDNSError.__str__`` reads ``connection_key.ssl`` and
    raises ``AttributeError`` on a ``None`` -- a fault double that cannot
    be stringified would fail the guard for a reason that is the double's
    and not the client's.

    Returns:
        A ``ClientConnectorDNSError`` for ``host:443``.
    """
    key = ConnectionKey(
        host='host',
        port=443,
        is_ssl=True,
        ssl=None,
        proxy=None,
        proxy_auth=None,
        proxy_headers_hash=None,
        server_hostname=None,
    )
    return aiohttp.ClientConnectorDNSError(
        connection_key=key, os_error=socket.gaierror('name not resolved'))


#: One fault per (protocol, category): the exception that protocol's own
#: transport library raises for that failure. The HTTP family, SOAP
#: included, dispatches over ``aiohttp``; FTP over ``aioftp``; SFTP over
#: ``asyncssh``. Each is the library's real class, so a client is held to
#: what it will actually be handed rather than to a stand-in.
_AIOHTTP_FAULTS: dict[str, Callable[[], BaseException]] = {
    'TLS': lambda: ssl.SSLError('handshake failed'),
    'TIMEOUT': lambda: asyncio.TimeoutError(),
    'CONNECT': lambda: aiohttp.ClientConnectionError('connection refused'),
    'DNS': _dns_error,
    'PROTOCOL': lambda: aiohttp.ClientPayloadError('malformed chunk'),
}

PROTOCOL_FAULTS: dict[str, dict[str, Callable[[], BaseException]]] = {
    'HTTP': _AIOHTTP_FAULTS,
    'HTTPS': _AIOHTTP_FAULTS,
    'SOAP': _AIOHTTP_FAULTS,
    'FTP': {
        'TLS': lambda: ssl.SSLError('handshake failed'),
        'TIMEOUT': lambda: asyncio.TimeoutError(),
        'CONNECT': lambda: ConnectionRefusedError('connection refused'),
        'DNS': lambda: socket.gaierror('name not resolved'),
        'PROTOCOL': lambda: aioftp.AIOFTPException('malformed reply'),
    },
    'SFTP': {
        'TIMEOUT': lambda: asyncio.TimeoutError(),
        'CONNECT': lambda: ConnectionRefusedError('connection refused'),
        'DNS': lambda: socket.gaierror('name not resolved'),
        'PROTOCOL': lambda: asyncssh.ProtocolError('bad packet'),
    },
}

#: The ``error['code']`` each category must produce. ``PROTOCOL`` is the
#: family's catch-all -- a wire-level failure that is neither a timeout,
#: a refusal, a resolution failure nor a TLS problem -- and reports as
#: ``TRANSPORT``.
EXPECTED_CODE: dict[str, str] = {
    'TLS': 'TLS',
    'TIMEOUT': 'TIMEOUT',
    'CONNECT': 'CONNECT',
    'DNS': 'DNS',
    'PROTOCOL': 'TRANSPORT',
}


def install_failing_transport(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    fault: Callable[[], BaseException],
) -> None:
    """Replace ``protocol``'s seam with one that raises ``fault()``.

    The sibling of :func:`install_transport`, which only offers a
    connection refusal. The fault-category guard needs each protocol's
    seam to raise an *arbitrary* exception so every category can be put
    to every client, and it patches the same seams for the same reason:
    everything between the seam and the caller stays real code.

    Args:
        monkeypatch: The pytest patcher, which undoes this on teardown.
        protocol: A normalised protocol name.
        fault: A zero-argument callable returning the exception to raise.

    Returns:
        None.

    Raises:
        ValueError: If ``protocol`` names no seam, for the same
            fail-closed reason :func:`install_transport` gives.
    """
    async def failing(*args: Any, **kwargs: Any) -> HttpResult:
        """Raise the configured fault instead of answering.

        Args:
            args: The transport's positional arguments, unused.
            kwargs: The transport's keyword arguments, unused.

        Returns:
            Never; this always raises.

        Raises:
            BaseException: Whatever ``fault()`` returns.
        """
        raise fault()

    def opening(*args: Any, **kwargs: Any) -> Any:
        """Return a context manager that raises the fault on entry.

        Args:
            args: Connection arguments, unused.
            kwargs: Connection options, unused.

        Returns:
            An async context manager whose ``__aenter__`` raises.
        """
        return FaultingTransport(fault)

    if protocol in {'HTTP', 'HTTPS'}:
        monkeypatch.setattr(http_client, 'handle_http_request', failing)
    elif protocol == 'SOAP':
        monkeypatch.setattr(soap_client, 'handle_http_request', failing)
    elif protocol == 'FTP':
        monkeypatch.setattr(aioftp.Client, 'context', opening)
    elif protocol == 'SFTP':
        monkeypatch.setattr(asyncssh, 'connect', opening)
    else:
        raise ValueError(
            f'{protocol!r} owns no transport seam. Returning quietly here'
            ' would install nothing and let the row open a real'
            ' connection.')


class FaultingTransport:
    """An async context manager whose entry raises a supplied fault.

    :class:`RefusingTransport` with the exception made a parameter, so the
    fault-category guard can put every category to the two protocols whose
    seam is a context manager rather than a coroutine.
    """

    def __init__(self, fault: Callable[[], BaseException]) -> None:
        """Record the fault this transport will raise on entry.

        Args:
            fault: A zero-argument callable returning the exception.
        """
        self.fault = fault

    async def __aenter__(self) -> Any:
        """Fail the connection with the configured fault.

        Returns:
            Never; this always raises.

        Raises:
            BaseException: Whatever ``fault()`` returns.
        """
        raise self.fault()

    async def __aexit__(self, *exc_info: Any) -> bool:
        """Leave the context, suppressing nothing.

        Args:
            exc_info: The exception triple, unused.

        Returns:
            False, so any exception propagates.
        """
        return False


# --- LOCAL_IO: the category that is not a wire fault -----------------------
#
# NEW-R10-1, and the reason it was invisible to 2981 tests. Every
# category above is a failure raised at the *transport seam* -- the
# exception a protocol's own library hands the client when the network
# refuses. The cross-protocol guard was built from exactly that
# vocabulary, so a fault raised on the **local filesystem**, on a
# transport that succeeded, had no row to occupy and no exemption to
# argue: it was not covered and its absence was not visible.
#
# It is the same class of divergence the guard exists to end, reached
# through a surface the guard could not see. FTP answered `CONNECT`,
# SFTP answered `CONNECT`, a directory target answered `CONFIG`, and
# HTTP handed the caller a raw `FileNotFoundError`. Four protocols, four
# answers, to one question: "the local destination will not take this
# file."
#
# The doubles below therefore fake the **wire only**, exactly as the
# fault doubles above do, and let each protocol's real local-write layer
# run: `utils.paths.safe_writer` for the HTTP family,
# `utils.contained_io.ContainedPathIO` for FTP,
# `utils.contained_io.ContainedLocalFS` for SFTP. What decides the
# answer is the code under test, not the double.
#
# --- The audit that stops a fourth instance --------------------------------
#
# Three defects in a row were green because of what a double did NOT do,
# so the doubles were audited against the real clients: every method the
# real `aioftp.Client` and `asyncssh.SFTPClient` invoke on the objects
# these doubles stand in for, and whether anything reaches it. Read off
# the libraries themselves (`self.path_io.<m>` in aioftp's client;
# `dstfs.<m>` in asyncssh's `_copy`/`_begin_copy`/`_SFTPFileCopier`) and
# confirmed empirically by instrumenting both contained classes and
# recording which methods a full run enters.
#
#   asyncssh -> ContainedLocalFS   reached by a client-driven test?
#     open, mkdir, isdir, setstat, symlink, compose_path .... yes
#     encode, limits ........................................ no (1)
#     stat, scandir, readlink, basename ..................... no (2)
#
#   aioftp -> ContainedPathIO      reached by a client-driven test?
#     mkdir, open(_open/write/close) ........................ yes
#     is_file, is_dir, list ................................. yes (3)
#
# (1) `encode` and `limits` are read by the real `_begin_copy`, which
#     both SFTP doubles *replace* -- that is the seam containment is
#     installed at, so a double standing in for it necessarily bypasses
#     its own calls. Both are pure delegation with no containment step
#     (`encode` deliberately so: it runs on the destination before any
#     entry name is composed), and `tests/utils/test_contained_io.py`
#     asserts each equals the real implementation's answer.
# (2) The four the *source* filesystem is asked for. On a download the
#     source is the remote side, so these are reached on a `dstfs` only
#     by an upload -- which asyncssh routes through `srcfs`, not this
#     object. Covered directly in `test_contained_io`, including their
#     containment.
# (3) On a *download* these are the client's own remote calls, not
#     `path_io` ones: `download` branches on the remote
#     `is_file(source)`/`is_dir(source)` and lists the remote tree. They
#     become `path_io` calls on an **upload**, where the local side is
#     the source -- and this row's argument for leaving them unreached
#     was that "this library does not dispatch recursively", which was
#     simply **false**. `command='upload'` with a directory
#     `client_path` is dispatched exactly like any other, reaches
#     `aioftp.Client.upload`'s directory arm, and calls all three.
#
#     The false premise cost a High defect (AGW-38).
#     `ContainedPathIO.list` returned *canonicalised* entries while
#     `upload` computed `entry.relative_to(source)` against the
#     uncanonicalised `source` it still held; on any `client_path` with
#     a symlinked component -- macOS `/tmp`, the README's own documented
#     example -- the two disagreed and `ValueError` escaped `request()`
#     un-enveloped, part-way through the upload. Nothing saw it because
#     no test uploaded a directory, and the reasoning above is why
#     nobody wrote one.
#
#     :attr:`WritingFTPClient.upload_tree` now takes the real arm, so
#     the three are reached client-driven the way every other row's
#     methods are. `test_contained_io` keeps its direct containment
#     rows, which remain the narrow proof that each one refuses.
#
# No unreached method is left unclassified, and none is both
# containment-bearing and unexercised. The three that were -- `mkdir` on
# the SFTP side, `setstat` on both, and this `list`/`is_file`/`is_dir`
# row -- are the findings this file's doubles were corrected to reach.

#: What a ``LOCAL_IO`` row asks each protocol to write to: a path whose
#: parent directory does not exist. ENOENT rather than a full disk
#: because it needs no privileges and no ``RLIMIT`` games, and it takes
#: the identical arm -- the end-to-end ``RLIMIT_FSIZE`` runs confirm
#: ENOSPC and EFBIG classify the same way.
LOCAL_IO_LEAF: str = 'no-such-dir/out.bin'

#: The ``error['code']`` a local write failure must produce, on every
#: protocol that writes locally.
LOCAL_IO_CODE: str = 'PATH'


#: What a writing double sends. Overridden per row so an ``OVER_CAP``
#: row can hand down a body larger than the ceiling while a ``LOCAL_IO``
#: row keeps the small one.
class WritingFTPClientBody:
    """Namespace for the body the writing doubles send.

    A module-level mutable would leak between rows; an attribute on a
    tiny holder is rebound per row by :func:`install_writing_transport`
    and read at write time.
    """

    payload: bytes = JSON_BODY


#: The mode a ``WritingSFTPClient`` reports the remote file carries, and
#: therefore what a ``preserve=True`` copy would apply locally if
#: nothing stopped it. World-everything, so that a double which reached
#: ``setstat`` unguarded produces a visibly wrong local mode rather than
#: one that happens to match 0600.
REMOTE_PERMISSIONS: int = 0o100777

#: The timestamp the same client reports, and the half of ``preserve``
#: that a download legitimately keeps. A fixed epoch second, so a test
#: can assert the value rather than merely that something was set.
REMOTE_MTIME: int = 1000000000

#: The entry name a directory copy writes inside the destination tree.
REMOTE_ENTRY: bytes = b'f.bin'


class WritingFTPClient(StubFTPClient):
    """An FTP client whose download writes through the real path layer.

    ``StubFTPClient.download`` returns without touching a disk, which is
    right for the rows that assert the envelope's *shape*. A
    ``LOCAL_IO`` row asserts what happens when the disk refuses, so this
    one performs the write -- through ``self.path_io``, the
    ``ContainedPathIO`` the client under test installed, so the refusal
    is produced and classified by the code being tested.

    **It must make every call the real client makes, in the real
    order.** NEW-R11-1 is what happens when it does not: this double
    opened the destination directly and never called ``mkdir``, while
    the real ``aioftp.Client.download`` calls
    ``path_io.mkdir(destination.parent, parents=True, exist_ok=True)``
    *first*. The ``LOCAL_IO`` row therefore passed 6/6 against a
    ``ContainedPathIO`` that was, on the real client, silently running
    an unbounded ``mkdir -p`` and answering ``ok=True``/200 for the
    missing destination directory this row exists to refuse. A double
    that is easier than reality certifies an arm it never exercised.

    The **directory** arm is the same lesson one level along.
    ``download`` branches on ``is_dir(source)`` and, for a directory,
    calls ``mkdir(destination_path, ...)`` on the destination *itself*
    rather than on its parent -- a different path, and the one a
    caller's occupied ``client_path`` collides with. Kept behind
    :attr:`recurse` so the existing rows keep exercising the single-file
    arm they were written for.

    The **upload** verb is the third instance of the same lesson, and
    the one the audit table above talked itself out of (AGW-38). Only
    ``upload`` asks ``path_io`` about the *local* side, so
    ``is_file``/``is_dir``/``list`` were reached by nothing -- and
    ``list``'s canonicalised return value crashed the real client's
    ``relative_to`` arithmetic on any symlinked ``client_path``. So
    :attr:`upload_tree` runs ``aioftp.Client.upload``'s **own code**
    bound to this object, exactly as :class:`~tests.fixtures.ftp.
    HostileFTPServer` runs the real ``download``: the recursion, the
    ``relative_to(source)`` arithmetic and every ``path_io`` call stay
    the library's, and only the two wire coroutines below are faked.
    It needs no flag of its own: ``download`` and ``upload`` are
    different verbs, so a row asking for one never reaches the other.

    Attributes:
        path_io: Set by :class:`WritingFTPContext` from the
            ``path_io_factory`` the client passed, exactly as
            ``aioftp.Client`` would.
        recurse: True to take ``download``'s directory arm.
        uploaded: What each remote path received, so a test can assert
            the whole tree arrived rather than merely that nothing
            raised.
    """

    path_io: Any = None
    recurse: bool = False

    def __init__(self) -> None:
        """Start with an empty record of what reached the server."""
        self.uploaded: dict[str, bytes] = {}

    async def make_directory(self, *args: Any, **kwargs: Any) -> None:
        """Accept a remote ``mkdir``, as a real server would.

        Args:
            args: The remote path, unused -- the local side is what
                this double is about.
            kwargs: ``aioftp``'s options, unused.

        Returns:
            None.
        """

    @asynccontextmanager
    async def upload_stream(
        self,
        destination: Any,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Record the bytes the real ``upload`` sends for one file.

        Args:
            destination: The remote path being written.
            kwargs: ``aioftp``'s stream options, unused.

        Yields:
            A stream with the ``write`` ``aioftp.Client.upload`` calls.
        """
        received = bytearray()

        class Stream:
            """The write half of ``aioftp``'s upload stream."""

            async def write(self, block: bytes) -> None:
                """Accept one block.

                Args:
                    block: The bytes read off the local file.

                Returns:
                    None.
                """
                received.extend(block)

        yield Stream()
        self.uploaded[str(destination)] = bytes(received)

    async def upload(self, *args: Any, **kwargs: Any) -> None:
        """Run ``aioftp``'s **real** upload over the local tree.

        The one method that must not be a stub, for the reason
        :class:`~tests.fixtures.ftp.HostileFTPServer.download` gives
        about its own: everything AGW-38 is about happens inside
        ``aioftp.Client.upload`` -- the ``is_dir`` branch, the ``list``
        of each local directory, the ``path.relative_to(source)``
        arithmetic against the operand the caller passed, and the
        recursion. A double that recorded the call and returned would
        assert nothing about any of it.

        Args:
            args: ``(source, destination)`` -- for an upload, source is
                **local**.
            kwargs: ``write_into`` and the block size.

        Returns:
            None, as ``aioftp`` does.
        """
        await aioftp.Client.upload(self, *args, **kwargs)

    async def download(self, *args: Any, **kwargs: Any) -> None:
        """Write the downloaded body to the local destination.

        Mirrors ``aioftp.Client.download``: the directory arm creates
        the destination and writes one entry inside it; the single-file
        arm creates the destination's *parent* and writes the file.

        Args:
            args: ``(source, destination)`` -- remote first.
            kwargs: ``write_into`` and the block size, unused.

        Returns:
            None, as ``aioftp`` does.
        """
        destination = pathlib.Path(str(args[1]))
        if self.recurse:
            await self.path_io.mkdir(
                destination, parents=True, exist_ok=True)
            destination = destination / os.fsdecode(REMOTE_ENTRY)
        await self.path_io.mkdir(
            destination.parent, parents=True, exist_ok=True)
        async with self.path_io.open(destination, mode='wb') as handle:
            await handle.write(WritingFTPClientBody.payload)


class WritingSFTPClient(StubSFTPClient):
    """An SFTP client whose download writes through the real local FS.

    The sibling of :class:`WritingFTPClient`. ``contained_download``
    reaches ``_begin_copy`` with the ``ContainedLocalFS`` the client
    built, so writing through that object is writing through the code
    under test.

    **It must make every call the real client makes, in the real
    order** -- the rule :class:`WritingFTPClient` states, and the rule
    this class broke twice.

    The first break was the **directory arm**. ``asyncssh``'s ``_copy``
    asks ``dstfs.isdir`` and then ``dstfs.mkdir`` before it copies a
    directory's entries, and this double went straight to ``open``. So
    ``ContainedLocalFS.mkdir`` -- the one method on the destination
    filesystem that creates rather than writes -- was reached by no
    test at all, and shipped without the classification its FTP
    counterpart had: measured against real loopback servers, a
    directory download onto an occupied local path answered ``PATH`` on
    SFTP and ``CONFIG`` on FTP.

    The second was ``setstat``. ``_copy`` ends with one for
    ``preserve=True``, carrying the **server's** permission bits, and
    no double reached it -- so nothing saw that ``asyncssh``'s
    ``_setstat`` chmods the local file to whatever the remote said,
    undoing the 0600 the guarded open had just established. Measured
    the same way: a remote file at 0777 left the local one at 0o777.

    Attributes:
        recurse: True to take ``_copy``'s directory arm -- ``isdir``,
            ``mkdir``, then one file inside -- instead of copying a
            single file.
        preserve: True to make the ``setstat`` tail run, as
            ``preserve=True`` does on the real client.
        symlink: True to take ``_copy``'s symbolic-link arm, which
            calls ``dstfs.symlink`` with the server's own target.
    """

    def __init__(
        self,
        *,
        recurse: bool = False,
        preserve: bool = False,
        symlink: bool = False,
    ) -> None:
        """Choose which of ``_copy``'s arms this client will exercise.

        Args:
            recurse: Take the directory arm.
            preserve: Run the ``setstat`` tail.
            symlink: Take the symbolic-link arm.
        """
        self.recurse = recurse
        self.preserve = preserve
        self.symlink = symlink

    async def _begin_copy(
        self,
        srcfs: Any,
        dstfs: Any,
        srcpaths: Any,
        dstpath: Any,
        copy_type: str,
        expand_glob: bool,
        *options: Any,
    ) -> None:
        """Write the downloaded body the way ``_copy`` writes it.

        Args:
            srcfs: The source filesystem, unused -- the remote side is
                what these doubles stand in for.
            dstfs: The contained destination filesystem under test.
            srcpaths: The remote source operand, unused.
            dstpath: The local destination operand.
            copy_type: ``'get'`` or ``'mget'``, unused.
            expand_glob: Whether the source is a glob, unused.
            options: ``_begin_copy``'s remaining options, unused --
                which arm runs is chosen at construction rather than
                read from here, because this double stands in for the
                *remote* side's shape and not for asyncssh's own
                option plumbing.

        Returns:
            None, as ``asyncssh`` does.
        """
        destination = os.fsencode(str(dstpath))
        if self.symlink:
            # `_copy`'s symlink arm: the target string is the server's.
            await dstfs.symlink(b'/etc/passwd', destination)
            return

        target = destination
        if self.recurse:
            # `_copy`'s directory arm, in its own order: ask before
            # creating, then compose the entry name onto the parent.
            if not await dstfs.isdir(destination):
                await dstfs.mkdir(destination)
            target = dstfs.compose_path(REMOTE_ENTRY, parent=destination)

        handle = await dstfs.open(target, 'wb')
        await handle.write(WritingFTPClientBody.payload, 0)
        await handle.close()

        if self.preserve:
            # `_copy`'s tail, with the fields it actually sends: the
            # remote's permission bits and times, and nothing else.
            await dstfs.setstat(
                target,
                asyncssh.SFTPAttrs(
                    permissions=REMOTE_PERMISSIONS,
                    atime=REMOTE_MTIME,
                    mtime=REMOTE_MTIME),
                follow_symlinks=True)


class WritingFTPContext:
    """``aioftp.Client.context`` yielding a client bound to the real layer.

    ``aioftp.Client.__init__`` calls ``path_io_factory(timeout=...)``
    and keeps the result as ``self.path_io``; the doubles here replace
    the whole client, so that binding has to be reproduced or the write
    would go through no containment layer at all -- and the row would
    pass while testing nothing.

    Attributes:
        recurse: Passed to the client it builds, selecting
            ``download``'s directory arm.
        client: The most recent client this yielded, so an upload row
            can read back what reached the server.
    """

    def __init__(self, *, recurse: bool = False) -> None:
        """Record which arm the client this yields will take.

        Args:
            recurse: True for ``download``'s directory arm.
        """
        self.recurse = recurse
        self.client: Any = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Open a session whose client writes through the caller's layer.

        Args:
            args: Host, port and credentials, unused.
            kwargs: ``aioftp`` options, of which ``path_io_factory`` is
                read.

        Returns:
            An async context manager yielding the bound client.
        """
        client = WritingFTPClient()
        client.recurse = self.recurse
        factory = kwargs['path_io_factory']
        client.path_io = factory(timeout=None)
        self.client = client
        return entered(client)


def install_writing_transport(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    body: bytes = JSON_BODY,
    *,
    recurse: bool = False,
    preserve: bool = False,
    symlink: bool = False,
) -> Any:
    """Replace ``protocol``'s seam with one that reaches the local disk.

    The ``LOCAL_IO`` counterpart of :func:`install_failing_transport`.
    That one makes the *wire* fail; this one makes the wire **succeed**
    so the local write actually happens and the destination is what
    refuses.

    ``HTTP``/``HTTPS`` are not doubled at all: their write is
    ``read_response``'s, several frames below the seam these doubles
    replace, so a doubled transport would skip the very code the row is
    about. They dial the loopback server instead.

    Args:
        monkeypatch: The pytest patcher, which undoes this on teardown.
        protocol: A normalised protocol name that writes locally.
        body: What the transport sends, so an over-cap row can hand
            down more bytes than the ceiling allows.
        recurse: SFTP only. Take ``_copy``'s directory arm, so
            ``isdir``/``mkdir`` on the destination filesystem are
            actually reached.
        preserve: SFTP only. Run ``_copy``'s ``setstat`` tail with the
            server's own permission bits.
        symlink: SFTP only. Take ``_copy``'s symbolic-link arm.

    Returns:
        The installed seam. FTP's is the :class:`WritingFTPContext`,
        whose ``client`` carries what an upload actually delivered;
        SFTP's is the client itself.

    Raises:
        ValueError: If ``protocol`` writes to no local destination, for
            the same fail-closed reason its siblings give: installing
            nothing would let the row pass having proven nothing.
    """
    monkeypatch.setattr(WritingFTPClientBody, 'payload', body)
    if protocol == 'FTP':
        context = WritingFTPContext(recurse=recurse)
        monkeypatch.setattr(aioftp.Client, 'context', context)
        return context
    if protocol == 'SFTP':
        client = WritingSFTPClient(
            recurse=recurse, preserve=preserve, symlink=symlink)
        monkeypatch.setattr(
            asyncssh, 'connect',
            lambda *a, **k: entered(StubSSHConnection(client)))
        return client
    raise ValueError(
        f'{protocol!r} has no doubled local-write seam. HTTP and '
        'HTTPS write below the seam this doubles and must dial the '
        'loopback server instead; a protocol that writes no local '
        'file belongs in CATEGORY_EXEMPT, argued.')


def local_io_call(protocol: str, destination: str) -> dict[str, Any]:
    """Return a contract call that downloads ``protocol`` to ``destination``.

    Each protocol names its local destination under a different key --
    ``http_file_download_config`` for the HTTP family, ``client_path``
    for FTP, ``local_path`` for SFTP -- which is precisely the reason
    one hand-written test per protocol drifted and a table did not.

    Args:
        protocol: A protocol name from the contract table.
        destination: The local path the download should write to.

    Returns:
        ``request()`` keyword arguments for the download.

    Raises:
        KeyError: If ``protocol`` names no local-write configuration.
    """
    call = contract_call(protocol)
    call['protocol_info'].update({
        'HTTP': {'http_file_download_config': {
            'download_filepath': destination}},
        'HTTPS': {'http_file_download_config': {
            'download_filepath': destination}},
        'FTP': {'client_path': destination},
        'SFTP': {'local_path': destination},
    }[protocol])
    return call

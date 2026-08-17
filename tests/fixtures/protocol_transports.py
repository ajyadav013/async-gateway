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

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import aioftp

import aiohttp
from aiohttp import BasicAuth

from async_gateway.helpers.internal.request_helper import HttpResult
from async_gateway.logic import http_client, soap_client

import asyncssh

import pytest

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

    def start_sftp_client(
        self,
    ) -> AbstractAsyncContextManager[StubSFTPClient]:
        """Open an SFTP channel on this connection.

        Returns:
            An async context manager yielding the SFTP client.
        """
        return entered(StubSFTPClient())


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

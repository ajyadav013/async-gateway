"""FTP, over TLS by default and never silently downgraded.

Fills the envelope it was handed and returns that same object, the way
``logic/http_client.py`` does, and raises a typed ``AsyncGatewayError``
for every remote or transport failure so that the entry point remains the
one place an error becomes an ``ok=False`` envelope.

Three defects were repaired together here, because repairing the first
exposes the other two on a live socket (FI-1). ``verify_ssl`` was read
before it was assigned, so *both* branches raised ``UnboundLocalError``
and no FTP call had ever reached a socket (C6). The default was plain
FTP where the HTTP client defaults to TLS, on the one protocol that sends
its credentials as literal ``USER``/``PASS`` lines (H1). And the TLS
branch read ``ssl_context.get('ssl_context')``, a key
``get_ssl_config`` returns *only* when a client certificate was supplied,
so asking for verification without one passed ``ssl=None`` -- which is
how ``aioftp`` is told to speak plaintext (M2).

M2's other half lived one branch across, and is repaired with it: the
context ``get_ssl_config`` *did* return for a caller with a client
certificate was built for ``ssl.Purpose.CLIENT_AUTH``, the server-side
purpose, so it verified no peer at all. Both halves negotiate FTPS
against an unauthenticated server; fixing only the first relocates the
defect rather than closing it.

Hence :func:`tls_context_for`: the TLS value is resolved through a
function whose *return type and verification settings* are the
guarantee, rather than by reading one key and hoping it was there.
"""

import asyncio
import logging
import socket
import ssl
from collections.abc import Collection, Mapping
from typing import Any, Final, Optional, Sequence, Text, Tuple, Union

import aioftp

from async_gateway.helpers.internal.base import BaseRequestClass
from async_gateway.helpers.internal.filters_helper import get_ssl_config
from async_gateway.utils.envelope import GatewayResponse, finalise_ok
from async_gateway.utils.exceptions import (
    AsyncGatewayError,
    CircuitOpenError,
    ConnectError,
    DnsError,
    FtpStatusError,
    GatewayTimeoutError,
    TlsError,
    TransportError,
    unwrap_cause,
)
from async_gateway.utils.redaction import redact_url, redact_value

from failsafe import CircuitOpen, FailsafeError

logger = logging.getLogger(__name__)

DEFAULT_FTP_PORT: Final[int] = 21

# `aioftp`'s high-level operations do not surface the reply code of the
# exchange they completed, so a success reports the documented fallback
# from the spec's status table: "the server's FTP reply code (2xx), else
# 200". A failure does carry one, and reports it -- see `reply_status`.
FTP_SUCCESS_STATUS: Final[int] = 200

# Commands that remove the path they are given. A `stat` afterwards reads
# something that is gone: it raises, and the deletion that *did* succeed
# used to be reported as a failure a retry then re-attempted (M3).
#
# All three names `aioftp.Client` exposes, not just `remove`: every one of
# them is reachable through the `getattr` lookup in `_run_command`, so a
# list naming only the first closes M3 under one name and leaves it live
# under the other two.
REMOVING_COMMANDS: Final[frozenset[Text]] = frozenset(
    {'remove', 'remove_file', 'remove_directory'})

# The keys `get_ssl_config` may answer with. Both are read because the
# helper's shape is owned by another requirement (R23) and is changing;
# reading only one of them is precisely the defect this replaces.
TLS_CONFIG_KEYS: Final[Tuple[Text, ...]] = ('ssl_context', 'ssl')

# Ordered, because the families overlap: `TimeoutError` is an `OSError`
# from Python 3.11, and `ssl.SSLError` and `socket.gaierror` are both
# `OSError` subclasses. First match wins, so the most specific
# classification is listed first.
TRANSPORT_ERRORS: Sequence[Tuple[type, type]] = (
    (asyncio.TimeoutError, GatewayTimeoutError),
    (ssl.SSLError, TlsError),
    (socket.gaierror, DnsError),
    (ConnectionError, ConnectError),
    (OSError, ConnectError),
)


def tls_context_for(ssl_config: Mapping[Text, Any]) -> ssl.SSLContext:
    """Return the verifying TLS context an FTPS session connects with.

    Fail closed on two axes, because M2 has two halves and this seam has
    failed open on both.

    By *type*: ``get_ssl_config`` answers with a real ``ssl.SSLContext``
    only when the caller supplied a client certificate; without one it
    answers with the flag it was handed. Forwarding that answer straight
    to ``aioftp.Client.context(ssl=...)`` is what made ``verify_ssl=True``
    open a plaintext session, so anything that is not a context is
    *replaced* by one here rather than passed on.

    By *verification settings*: a context that verifies no peer is not a
    weaker version of TLS, it is the plaintext downgrade wearing a
    handshake -- the session completes against anyone. Being an
    ``SSLContext`` is therefore necessary and not sufficient, and one
    built for ``ssl.Purpose.CLIENT_AUTH`` (the server-side purpose, whose
    ``verify_mode`` is ``CERT_NONE``) is refused rather than returned.
    That refusal is deliberately redundant with ``get_ssl_config``
    building for ``ssl.Purpose.SERVER_AUTH``: the helper's shape is owned
    by another requirement and is still changing, and this seam has now
    failed open three times.

    Args:
        ssl_config: Whatever :func:`get_ssl_config` returned.

    Returns:
        The caller's own context when they supplied a certificate,
        otherwise a default context -- which verifies the chain and the
        host name.

    Raises:
        TlsError: If the configuration carries a context that
            authenticates no peer. Refusing the call is the only safe
            answer: silently repairing the context would hide a helper
            that had started handing out unverifying ones again.
    """
    for key in TLS_CONFIG_KEYS:
        context = ssl_config.get(key)
        if isinstance(context, ssl.SSLContext):
            if context.verify_mode == ssl.CERT_NONE:
                raise TlsError(
                    'ftp tls context verifies no peer certificate '
                    '(verify_mode is CERT_NONE); refusing to open a '
                    'session no server has to authenticate itself for')
            return context
    return ssl.create_default_context()


def reply_status(error: aioftp.StatusCodeError) -> Optional[int]:
    """Return the FTP reply code a server answered with, if it gave one.

    Args:
        error: The status-code failure ``aioftp`` raised.

    Returns:
        The first received code that reads as a number -- ``550`` for a
        deletion of something that is not there -- or None to let the
        exception class take its documented default. The server's own
        code is reported rather than a library-chosen one, because a
        consumer distinguishing "not found" from "denied" can only do it
        from the real reply.
    """
    for code in error.received_codes:
        text = str(code)
        if text.isdigit():
            return int(text)
    return None


def transport_error_for(
    err: BaseException,
    *,
    redact_params: Collection[Text] = (),
) -> AsyncGatewayError:
    """Return the typed error for ``err``, or propagate a library bug.

    The resilience layer wraps everything the transfer raised in a
    ``FailsafeError`` whose own ``str()`` is empty, so both the
    classification and the message have to be taken from the cause
    rather than from the wrapper.

    Args:
        err: The exception caught around the session.
        redact_params: The caller's additional sensitive query-parameter
            names, forwarded to :func:`unwrap_cause`.

    Returns:
        The ``AsyncGatewayError`` subclass matching the failure's family,
        carrying a message that is never empty and always redacted.

    Raises:
        BaseException: The original cause, unchanged, when it belongs to
            no family here. A ``KeyError`` or ``TypeError`` from this
            library's own code is a bug, not a failed request, and must
            escape rather than become an envelope.
    """
    cause = err.__cause__ if isinstance(err, FailsafeError) else err
    message, _ = unwrap_cause(err, redact_params=redact_params)
    if isinstance(cause, aioftp.StatusCodeError):
        return FtpStatusError(message, reply_status(cause))
    for family, error_class in TRANSPORT_ERRORS:
        if isinstance(cause, family):
            return error_class(message)
    if cause is None:
        return TransportError(message)
    # `from None`: the wrapper is already this exception's `__context__`,
    # and suppressing it keeps the traceback pointing at the bug.
    raise cause from None


class FTPRequest(BaseRequestClass):
    """Implements Aioftp to make ftp calls."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the ftp request class."""
        super(FTPRequest, self).__init__(*args, **kwargs)

        self.port: int = self.info.get('port', DEFAULT_FTP_PORT)
        self.user: Text = self.auth.login
        self.password: Text = self.auth.password
        self.command_: Text = self.info.get('command', None)
        self.server_path: Text = self.info.get('server_path', None)
        self.client_path: Text = self.info.get('client_path', None)
        # `True`, matching the HTTP client. A default that puts the
        # caller's password on the wire is not a default anyone asked
        # for by name (H1).
        self.verify_ssl: bool = self.info.get('verify_ssl', True)

    async def handle_request(self) -> GatewayResponse:
        """Run one FTP operation and fill the envelope with its result.

        config structure-
        url = 'localhost'
        auth = aiohttp.BasicAuth('user','password'), only basicAuth allowed
        protocol = 'FTP'
        protocol_info = {
            'port': 21, # optional, default is 21
            'command': 'download', # download, upload, remove
            'server_path': '',
                # path from where to get/delete or upload file on server.
            'client_path': '',
                # path where file is downloaded/uploaded to.
            'verify_ssl': True, # optional, default is True. False opens
                # the session in plaintext and logs a warning.
            'certificate': ('cert path', 'key path'), # optional
            'timeout': 15, # optional. Bounds the connect, and bounds
                # each individual socket read and write during the
                # transfer -- see the note below for what that does and
                # does not guarantee.
        }

        What ``timeout`` bounds. It is passed as both
        ``connection_timeout`` and ``socket_timeout``. The first caps the
        whole connect. The second caps *each* socket operation, not the
        transfer as a whole: a server that keeps answering just inside
        the window can hold the transfer open indefinitely, and a large
        file legitimately takes many such operations. So the guarantee is
        "no single read or write stalls for longer than ``timeout``", not
        "the call returns within ``timeout``". What it removes is the
        H10-ftp hang -- a socket that goes silent forever, which the
        breaker cannot see because a hang raises nothing for it to count.
        A ceiling on total transfer time would need a deadline around the
        whole session and is not offered here.

        Returns:
            The same envelope object this request was constructed with,
            populated and finalised. ``protocol_details`` carries the
            command, the two paths and the file's stats -- None after a
            command that removed the path.

        Raises:
            TlsError: When the TLS configuration cannot be built, or the
                handshake fails -- including against a server that
                offers no TLS at all, which fails rather than falling
                back to plaintext.
            FtpStatusError: When the server answers with a failing reply
                code, which the envelope's ``status_code`` reports.
            CircuitOpenError: When the breaker for this destination is
                open.
            GatewayTimeoutError: When the connect, or any one socket
                operation of the transfer, exceeds ``timeout``.
            ConfigurationError: When ``certificate`` is not a
                ``(certificate path, key path)`` pair, or names files
                that will not load. Raised by ``get_ssl_config`` and
                deliberately not caught here -- see :meth:`_tls_value`.
            DnsError: When the host name does not resolve.
            ConnectError: When the connection is refused or reset.
            TransportError: For any other transport failure.
        """
        try:
            # Inside the `try`, so a certificate that will not load is
            # reported through the same contract as everything else
            # instead of escaping raw from an unguarded prologue.
            tls = await self._tls_value()
            async with aioftp.Client.context(
                self.url, self.port, self.user, self.password,
                ssl=tls,
                connection_timeout=self.timeout,
                socket_timeout=self.timeout,
            ) as client:
                file_stats = await self._run_command(client)
        except CircuitOpen as err:
            raise CircuitOpenError(
                f'circuit open for '
                f'{redact_url(self.url, extra_params=self.redact_params)}'
            ) from err
        except (FailsafeError, aioftp.AIOFTPException, OSError,
                asyncio.TimeoutError) as err:
            raise transport_error_for(
                err, redact_params=self.redact_params) from err

        self.response['protocol_details'] = {
            'command': self.command_,
            'server_path': self.server_path,
            'client_path': self.client_path,
            'file_stats': file_stats,
        }
        return finalise_ok(
            self.response,
            status_code=FTP_SUCCESS_STATUS,
            started=self.start_time)

    async def _tls_value(self) -> Union[ssl.SSLContext, bool]:
        """Return what this session hands ``aioftp`` as its ``ssl``.

        Returns:
            A verifying TLS context, or ``False`` when the caller
            explicitly disabled verification. Never ``None``: that value
            is how ``aioftp`` is told to speak plaintext, and reaching it
            by accident is the downgrade this method exists to refuse.

        Raises:
            TlsError: If the system CA bundle cannot be read, or if the
                configuration is built but authenticates no peer.
            ConfigurationError: If ``certificate`` is not a
                ``(certificate path, key path)`` pair, or names files
                that will not load -- missing, unreadable, not a valid
                PEM, a mismatched pair, or a passphrase-protected key.
                ``get_ssl_config`` raises it directly and it is *not*
                caught here: that is the caller's typo rather than a
                transport fault, so it is reported at 400 and never
                retried. R23 widened it from the two malformed-shape
                cases to every unloadable certificate, which moved the
                unloadable-file case from ``TLS``/502 to ``CONFIG``/400 --
                a deliberate contract change recorded in ticket AGW-17,
                on the same reasoning: nothing was attempted, so no retry
                can help, and it is what the HTTP client already reports.
        """
        if not self.verify_ssl:
            logger.warning(
                "protocol_info['verify_ssl'] is False: this FTP session "
                'is opened in plaintext, so the credentials and every '
                'byte transferred cross the network in the clear',
                extra={
                    'protocol': redact_value('FTP'),
                    'url': redact_value(
                        self.url, extra_params=self.redact_params),
                },
            )
            return False

        try:
            ssl_config = await get_ssl_config(self.certificate, True)
        except OSError as err:
            # The one `OSError` that still reaches here. Everything the
            # caller's own certificate files can do to fail is converted
            # to a `ConfigurationError` inside `get_ssl_config`, which is
            # not an `OSError` and so passes through untouched; what is
            # left is the *system* CA bundle failing to read, which is
            # the environment's fault and is a transport-level TLS
            # failure. The `(IndexError, TypeError)` arm that used to sit
            # below became unreachable when R23 made
            # `normalised_certificate` reject those shapes itself, and is
            # deleted rather than left as a branch no input can enter.
            raise TlsError(
                f'ftp tls configuration failed for '
                f'{redact_url(self.url, extra_params=self.redact_params)}'
            ) from err
        return tls_context_for(ssl_config)

    async def _run_command(
        self,
        client: aioftp.Client,
    ) -> Optional[dict[Text, Text]]:
        """Run the caller's command and read back what it left behind.

        Args:
            client: The connected ``aioftp`` client.

        Returns:
            The facts ``stat`` reports for ``server_path``, or None for a
            command that removed it. The read used to be unconditional,
            so a completed deletion ended in a ``stat`` on a path that no
            longer existed and was reported as a failure (M3).
        """
        command = self.command_.lower()
        operation = getattr(client, command)
        if self.client_path:
            await self.circuit_breaker.failsafe.run(
                operation,
                self.server_path,
                self.client_path,
                write_into=True)
        else:
            await self.circuit_breaker.failsafe.run(
                operation,
                self.server_path)

        if command in REMOVING_COMMANDS:
            return None
        return await client.stat(self.server_path)

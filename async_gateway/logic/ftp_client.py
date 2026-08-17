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
from types import MappingProxyType
from typing import Any, Final, Optional, Sequence, Text, Tuple, Union

import aioftp

from failsafe import CircuitOpen, FailsafeError

from async_gateway.helpers.internal.base import BaseRequestClass
from async_gateway.helpers.internal.filters_helper import get_ssl_config
from async_gateway.utils.contained_io import (
    contained_path_io_factory,
    local_base,
)
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

# The FTP operations this library will dispatch, and the whole of what
# `command` may name (R21-AC1, R15-AC8). Every one is a coroutine method
# of `aioftp.Client`: the transfer pair takes two paths, the three
# removals take one, and that split is what `_run_command` reads
# `client_path` to decide.
#
# What the set *excludes* is the point. An unbounded `getattr` on a
# connected `aioftp.Client` reached `close`, `quit`, `command` and every
# other attribute of a live session, so a mistyped command did not fail:
# it ran whatever else that name meant, or raised a `TypeError` the
# envelope reported as a fabricated status (M25). `list` and `stat` are
# absent deliberately rather than by oversight -- this protocol already
# reports `stat` in the `protocol_details` of every successful
# non-removing command, so admitting either as a command in its own right
# would be a second way to ask for what the envelope already carries.
FTP_COMMANDS: Final[frozenset[Text]] = (
    frozenset({'download', 'upload'}) | REMOVING_COMMANDS)

# The transfer commands, and which way round their two operands go
# (AGW-33). `aioftp.Client` takes `(source, destination)` on both verbs
# and the two verbs point in **opposite directions**, so one shared
# positional order is correct for exactly one of them:
#
#   download(source=remote, destination=local)
#   upload(source=local,    destination=remote)
#
# `_run_command` used to pass `(server_path, client_path)` to whichever
# command the caller named. For `download` that is right. For `upload`
# it reads `server_path` off the **local** filesystem and writes it to
# `client_path` on the **server**: usually a clean failure, but in a
# mirrored-tree deployment a local file exists at the remote path and
# the wrong file is transferred to the wrong place while the envelope
# reports `ok=True` (AGW-33, High).
#
# A table beside the command set rather than a conditional at the call
# site, and this comment rather than none: the reason the defect
# survived is that one positional order was applied to every verb, and
# the most likely future mistake is "simplifying" this back into one.
# True means the caller's LOCAL path is the source.
LOCAL_IS_SOURCE: Final[Mapping[Text, bool]] = MappingProxyType({
    'download': False,
    'upload': True,
})

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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build an FTP request from a validated ``protocol_info``.

        Reads the FTP-specific keys off ``self.info`` once, here, so the
        transfer methods below read attributes rather than re-reading the
        caller's mapping and disagreeing about its defaults.

        Args:
            *args: Forwarded verbatim to :class:`BaseRequestClass` --
                ``url``, ``auth``, ``response`` and ``info``, in that
                order.
            **kwargs: Forwarded verbatim to the base, which requires
                ``redact_params`` keyword-only.

        Raises:
            ConfigurationError: From the base, if ``info`` is neither
                None nor a mapping, or omits a key this protocol
                requires. FTP's own ``command`` is deliberately *not*
                checked here: it is validated once the request runs,
                because ``protocol_info`` is optional for this protocol
                and the object must stay constructible without one.
        """
        super(FTPRequest, self).__init__(*args, **kwargs)

        self.port: int = self.info.get('port', DEFAULT_FTP_PORT)
        self.user: Text = self.auth.login
        self.password: Text = self.auth.password
        self.command_: Text = self.info.get('command', None)
        self.server_path: Text = self.info.get('server_path', None)
        self.client_path: Text = self.info.get('client_path', None)
        # R22-AC3: refuse to overwrite by default, opt in by name. The
        # local side of a download is a caller-supplied path, so the
        # same decision that governs an HTTP download governs this one.
        self.overwrite: bool = self.info.get('overwrite') is True
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
                # path on the server: the SOURCE of a download and the
                # DESTINATION of an upload.
            'client_path': '',
                # path on this machine: the DESTINATION of a download
                # and the SOURCE of an upload. The two verbs point in
                # opposite directions and each is passed in its own --
                # see LOCAL_IS_SOURCE (AGW-33).
            'overwrite': False, # optional, default False. A download
                # refuses to replace an existing local file unless this
                # is True; a symbolic link at the destination is
                # refused either way.
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
            PathContainmentError: When a path a download would write --
                including one the *server* composed out of its own
                entry names -- resolves outside the directory
                ``client_path`` names, or is a symbolic link (R22).
            ConfigurationError: When ``certificate`` is not a
                ``(certificate path, key path)`` pair, or names files
                that will not load -- raised by ``get_ssl_config`` and
                deliberately not caught here, see :meth:`_tls_value` --
                or when a download's local destination already exists
                and ``overwrite`` is not True.
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
                path_io_factory=self._path_io_factory(),
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

    def _path_io_factory(self) -> Any:
        """Return the local-filesystem layer this session is confined to.

        R22's actual threat model, and the half that is not visible
        anywhere in this module: ``aioftp.Client.download`` takes the
        entry names a server sent, composes them onto the local
        destination itself, recurses, and writes through its own
        ``path_io``. Nothing this class can do to ``client_path`` before
        the call reaches those composed paths -- a server listing
        ``/pub/data/../victimdir/OWNED`` under ``/pub/data`` produced
        ``downloads/../victimdir/OWNED`` and wrote it, at mode 0644,
        outside the directory the caller named (reproduced against the
        real client with only the wire faked).

        The ``path_io_factory`` is the one seam ``aioftp`` offers
        between its recursion and the disk, so containment is installed
        there. Every path the client composes is checked against the
        directory the caller's ``client_path`` names, and every write
        goes out with ``O_NOFOLLOW`` and mode 0600.

        The base is ``client_path`` **itself**, not its parent. The
        caller named that path as the whole local side of the transfer
        -- for a directory download it is the tree's root, for a single
        file it is the file -- so it is the boundary they asked for.
        Confining to the parent instead lets a hostile entry name climb
        one level and still count as contained, which is a real escape
        from the directory that was named: measured,
        ``../victimdir/OWNED`` landed as ``downloads/victimdir/OWNED``
        beside the target tree.

        Returns:
            The bound path-IO factory when the call names a local path,
            or ``aioftp``'s own default when it does not -- a command
            with no ``client_path`` touches no local file, so there is
            nothing to confine and no directory to confine it to.
        """
        if not self.client_path:
            return aioftp.pathio.PathIO
        return contained_path_io_factory(
            local_base(self.client_path), overwrite=self.overwrite)

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

        Raises:
            UnsupportedVerbError: If ``command`` names nothing in
                :data:`FTP_COMMANDS` (R15-AC8). Raised here, inside
                ``handle_request``'s ``try``, so it reaches the caller as
                a ``CONFIG`` envelope rather than escaping -- and raised
                *before* the operation is looked up, so an unknown name
                never resolves to an attribute of the connected session.
                It replaces the ``TypeError`` such a name used to produce
                from inside the transport, which belonged to no family
                and was reported as a fabricated status.
        """
        # Resolved before the name is normalised for the `REMOVING_COMMANDS`
        # test below, not after: `command` absent is `None`, and
        # `None.strip()` is the `AttributeError` this criterion exists to
        # replace. Only a name the allowlist admitted is normalised here,
        # so the read cannot raise.
        operation = self.resolve_verb(
            client, self.command_, allowed=FTP_COMMANDS, setting='command')
        command = self.command_.strip().lower()
        if self.client_path:
            source, destination = self._operands(command)
            await self.circuit_breaker.run(
                operation,
                source,
                destination,
                write_into=True)
        else:
            await self.circuit_breaker.run(
                operation,
                self.server_path)

        if command in REMOVING_COMMANDS:
            return None
        # The remote path, for every verb. An upload's `client_path` is
        # local and stat-ing it would report the file that was read
        # rather than the one that was written.
        return await client.stat(self.server_path)

    def _operands(self, command: Text) -> Tuple[Text, Text]:
        """Return ``(source, destination)`` in this verb's own direction.

        AGW-33. See :data:`LOCAL_IS_SOURCE` for why a table decides this
        and a single shared positional order cannot.

        Args:
            command: The normalised command name.

        Returns:
            The two operands, source first, in the order ``aioftp``
            defines for *this* verb. A command outside
            :data:`LOCAL_IS_SOURCE` that nonetheless carries a
            ``client_path`` keeps the historical
            ``(server_path, client_path)`` order: this method's job is
            the operand direction of the two transfer verbs, and
            inventing a direction for a verb nobody has established one
            for would be a guess. Which commands are dispatchable at all
            is R21's allowlist, at S19.
        """
        if LOCAL_IS_SOURCE.get(command, False):
            return self.client_path, self.server_path
        return self.server_path, self.client_path

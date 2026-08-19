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
from typing import Any, Final, Optional, Sequence, Tuple, Union

import aioftp

from failsafe import CircuitOpen, FailsafeError

from async_gateway.helpers.internal.base import (
    BaseRequestClass,
    credentials_of,
)
from async_gateway.helpers.internal.filters_helper import get_ssl_config
from async_gateway.logic.http_client import validated_max_response_bytes
from async_gateway.utils.constants import MAX_RESPONSE_BYTES
from async_gateway.utils.contained_io import (
    TransferBudget,
    contained_path_io_factory,
    local_base,
    local_operand,
)
from async_gateway.utils.envelope import GatewayResponse, finalise_ok
from async_gateway.utils.exceptions import (
    AsyncGatewayError,
    CircuitOpenError,
    ConfigurationError,
    ConnectError,
    DnsError,
    FtpStatusError,
    GatewayTimeoutError,
    LocalWriteError,
    TlsError,
    TransportError,
    faults_of,
    unwrap_cause,
)
from async_gateway.utils.http_file_config import validated_verb
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
REMOVING_COMMANDS: Final[frozenset[str]] = frozenset(
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
FTP_COMMANDS: Final[frozenset[str]] = (
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
LOCAL_IS_SOURCE: Final[Mapping[str, bool]] = MappingProxyType({
    'download': False,
    'upload': True,
})

# The keys `get_ssl_config` may answer with. Both are read because the
# helper's shape is owned by another requirement (R23) and is changing;
# reading only one of them is precisely the defect this replaces.
TLS_CONFIG_KEYS: Final[Tuple[str, ...]] = ('ssl_context', 'ssl')

# Ordered, because the families overlap: `TimeoutError` is an `OSError`
# from Python 3.11, and `ssl.SSLError` and `socket.gaierror` are both
# `OSError` subclasses. First match wins, so the most specific
# classification is listed first.
#
# `aioftp.InvalidCommand` is deliberately *not* here, and the omission is
# load-bearing rather than an oversight -- see `transport_error_for`,
# which classifies it above this table as configuration. It is a
# `ValueError` subclass, so the tempting one-line fix for N7 was to add
# `(ValueError, ConnectError)` to this table. That would have been wrong
# twice over: it declares a caller's own CR/LF a *transport* failure,
# carrying the retry recommendation a transport verdict carries, and it
# would swallow every other `ValueError` this library's own code can
# raise -- a genuine bug -- as a failed network call, which is the exact
# blindness the one-conversion-point rule exists to prevent.
TRANSPORT_ERRORS: Sequence[Tuple[type, type]] = (
    # BOTH timeout classes, and naming both is load-bearing on the
    # interpreter floor. `asyncio.TimeoutError is TimeoutError` only from
    # 3.11; on 3.10 they are unrelated classes, so a socket timeout --
    # which `aioftp` raises as the *builtin* `TimeoutError` -- matched
    # nothing here and fell all the way to the residual `OSError` row,
    # reporting `PATH`/400 for a slow server. That is the worst possible
    # direction for this particular mistake: `PATH` tells the caller
    # their local disk is at fault, and it keeps the timing-out
    # destination out of its own circuit breaker.
    #
    # Measured, not reasoned: green on 3.12-3.14 and failing on a real
    # 3.10 interpreter, which is why nothing on the development machine
    # ever saw it. `circuit_breaker_helper.RETRIABLE_FAILURES` already
    # spells both out for exactly this reason; these two tables did not.
    (asyncio.TimeoutError, GatewayTimeoutError),
    (TimeoutError, GatewayTimeoutError),
    (ssl.SSLError, TlsError),
    (socket.gaierror, DnsError),
    (ConnectionError, ConnectError),
    # The residual `OSError`, and it reports `PATH` rather than the
    # `CONNECT` it used to. This row is reached by two very different
    # failures and used to call both a connection problem: a socket
    # error `ConnectionError` above does not name, and a **local
    # filesystem** failure from the download's own write. The second is
    # the common one and the verdict was wrong for it -- a full disk is
    # not evidence the server is unhealthy, and `CONNECT` both invited
    # a retry that re-downloads the body and counted the failure
    # against that destination's breaker (NEW-R10-1).
    #
    # All four protocols now answer `PATH` for a local write failure.
    # The cost is named rather than hidden: a socket-level `OSError`
    # that reaches this row -- one `ConnectionError` does not already
    # cover -- now also reports `PATH`. That is the rarer case, and it
    # is the direction to err in: mislabelling a network fault as local
    # costs a caller one retry they must ask for, while mislabelling a
    # local fault as network takes a healthy destination offline for
    # every caller in the process.
    (OSError, LocalWriteError),
)

#: The families this dispatch catches, derived from the table above.
#: Hand-written here until NEW-R10-1: the round-9 fix claimed the
#: divergence was "no longer representable" because the clause was
#: derived, but the derivation reached only the two HTTP-family
#: clients, and this module kept its own tuple beside its own table --
#: two of the four dispatch sites, which is the same gap one round
#: later. ``aioftp.AIOFTPException`` is appended because
#: ``transport_error_for`` classifies it below the table, as the
#: family's catch-all, rather than in it.
TRANSPORT_FAULTS: Tuple[type[BaseException], ...] = (
    faults_of(TRANSPORT_ERRORS) + (aioftp.AIOFTPException,))


def tls_context_for(ssl_config: Mapping[str, Any]) -> ssl.SSLContext:
    """Return the verifying TLS context an FTPS session connects with.

    A plain ``def``, deliberately, and the same contract
    ``filters_helper.build_client_ssl_context`` is held to: its fallback
    branch calls ``ssl.create_default_context()``, which reads the whole
    system CA bundle (194 certificates), so it is a module-level
    blocking helper and :meth:`FTPRequest._tls_value` -- its only caller
    -- reaches it through ``asyncio.to_thread``. Rewriting it as an
    ``async def`` puts a banned ``ssl.`` call back inside a coroutine and
    ``tests/test_no_blocking_io.py`` fails on it; leaving it a plain
    ``def`` called *directly* from the coroutine is the defect that scan
    structurally cannot see, and is what ticket **AGW-37** was
    (``tests/logic/test_ftp_client.py`` pins the call site by thread
    identity instead).

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


def _being_cancelled(err: BaseException) -> bool:
    """Report whether ``err`` is a cancellation wearing another class.

    The question NEW-R11-2 turns on. ``aioftp``'s session ``__aexit__``
    sends QUIT on a socket the cancel has already torn down, so the
    resulting ``ConnectionResetError`` *replaces* the ``CancelledError``
    in flight and reaches the dispatch looking like an ordinary network
    fault. Answering it wrongly either way is a real defect: say yes to
    a genuine reset and the caller gets an exception where the contract
    promises an envelope; say no to a cancellation and every
    structured-concurrency primitive built on it stops working.

    Two signals, because neither alone spans the supported range.
    ``Task.cancelling()`` is the precise one -- non-zero only while this
    task is really being cancelled -- and it arrived in **3.11**, where
    ``requires-python`` is ``>=3.10``. Calling it unguarded raised
    ``AttributeError`` on the floor, from the failure path.

    The fallback reads the exception chain instead of the task, and is
    not a weaker approximation: Python sets ``__context__`` to whatever
    was in flight when the replacing exception was raised, so a
    cancelled call carries the ``CancelledError`` there and a server
    that genuinely reset does not. Verified on real 3.10, 3.12 and 3.14
    interpreters, in both directions.

    Args:
        err: The transport failure the dispatch caught.

    Returns:
        True when this task is being cancelled and ``err`` is the
        cleanup's replacement for that cancellation.
    """
    task = asyncio.current_task()
    cancelling = getattr(task, 'cancelling', None)
    if cancelling is not None:
        return bool(cancelling())
    return isinstance(err.__context__, asyncio.CancelledError)


def transport_error_for(
    err: BaseException,
    *,
    redact_params: Collection[str] = (),
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
        carrying a message that is never empty and always redacted. An
        ``aioftp.InvalidCommand`` -- a CR or an LF in a value the caller
        supplied -- maps to ``ConfigurationError`` rather than to a
        transport class, because it is the caller's configuration and no
        retry of it can succeed. Any other ``aioftp.AIOFTPException`` is
        a protocol-level failure and maps to ``TransportError``.

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
    # Above the table, and matched on the concrete class rather than on
    # its `ValueError` base: `aioftp` refuses a CR or an LF in a command
    # line itself (`aioftp.Client.command`), correctly, but it refuses
    # with an `InvalidCommand` -- an `AIOFTPException` *and* a
    # `ValueError` -- which belonged to no family here and so escaped
    # `request()` bare (N7). What reached that refusal is a `command`,
    # a `server_path` or a credential the caller supplied, so the honest
    # classification is `CONFIG`/400: the caller's own configuration
    # cannot form a valid call. Not a transport failure, which would
    # invite a retry of a value that can never succeed and would count
    # a spelling mistake against the destination's circuit breaker.
    if isinstance(cause, aioftp.errors.InvalidCommand):
        return ConfigurationError(message)
    for family, error_class in TRANSPORT_ERRORS:
        if isinstance(cause, family):
            return error_class(message)
    # `AIOFTPException` last, as the family's catch-all, exactly as
    # `logic.sftp_client` treats `asyncssh.Error`: it is `aioftp`'s own
    # base class, so a protocol-level failure that is neither a status
    # reply nor an `OSError` -- a malformed reply line, an unusable
    # response to `PASV` -- is a transport failure and reports as one.
    #
    # It was missing, and the omission is the same cross-client
    # divergence as AGW-R9-1: the FTP dispatch already *catches*
    # `aioftp.AIOFTPException`, so the class was named as a failure this
    # protocol answers for, and then classified by nothing. Everything
    # under it that is not a `StatusCodeError`, an `InvalidCommand` or an
    # `OSError` fell past this table and `raise cause from None` handed
    # the caller a bare `aioftp` exception -- the same broken contract,
    # on the protocol nobody re-checked.
    if isinstance(cause, aioftp.AIOFTPException) or cause is None:
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
                requires; and from
                :func:`~async_gateway.helpers.internal.base.credentials_of`
                if ``auth`` carries no credentials, which FTP cannot
                connect without. Both escape synchronously, because the
                entry point constructs this object outside its one
                conversion ``try``. FTP's own ``command`` is deliberately
                *not* checked here: it is validated once the request
                runs, because ``protocol_info`` is optional for this
                protocol and the object must stay constructible without
                one.
        """
        super(FTPRequest, self).__init__(*args, **kwargs)

        self.port: int = self.info.get('port', DEFAULT_FTP_PORT)
        # Not `self.auth.login`: `auth` defaults to None at the entry
        # point and is documented optional, so the read that looks
        # unconditional here is the documented default call crashing with
        # an `AttributeError` that escapes the envelope entirely (H5).
        self.user: str
        self.password: str
        self.user, self.password = credentials_of(self.auth, protocol='FTP')
        # Optional, and genuinely so: `protocol_info` is optional for this
        # protocol (R11-AC3), so an `FTPRequest` stays constructible with
        # none of these present. `resolve_verb` is what refuses a missing
        # or non-string `command`, by name and against the allowlist, and
        # `_run_command` reads `client_path` for None to decide whether a
        # local path is involved at all. Annotating them `str` asserted a
        # non-None the constructor never established.
        self.command_: Optional[str] = self.info.get('command')
        self.server_path: Optional[str] = self.info.get('server_path')
        self.client_path: Optional[str] = self.info.get('client_path')
        # R22-AC3: refuse to overwrite by default, opt in by name. The
        # local side of a download is a caller-supplied path, so the
        # same decision that governs an HTTP download governs this one.
        self.overwrite: bool = self.info.get('overwrite') is True
        # R14's ceiling, stated globally in the spec and implemented on
        # the HTTP family alone until NEW-R10-2. The same call with a
        # 1 KiB cap and a 256 KiB payload had HTTP refuse with
        # RESPONSE_TOO_LARGE and write nothing, while this protocol
        # returned ok=True having written all 262144 bytes -- so the
        # bound a caller sets to stop a hostile endpoint filling their
        # disk did not exist here. Validated with the HTTP family's own
        # validator, so one bad value is one message on every protocol.
        self.max_response_bytes: int = validated_max_response_bytes(
            self.info.get('max_response_bytes', MAX_RESPONSE_BYTES))
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
                and ``overwrite`` is not True, or when ``server_path``
                is absent or is not a non-empty string.
            DnsError: When the host name does not resolve.
            ConnectError: When the connection is refused or reset.
            TransportError: For any other transport failure.
        """
        self._validate_request()
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
        except (FailsafeError,) + TRANSPORT_FAULTS as err:
            # A cancellation that reaches here has already been
            # *replaced*, and only on this protocol. `aioftp`'s session
            # context manager runs `await client.quit()` from a
            # `finally` in its `__aexit__`, which sends QUIT on a socket
            # the cancel has already torn down; that raises
            # `ConnectionResetError`, which supersedes the
            # `CancelledError` in flight and matches the clause above as
            # an ordinary transport failure.
            #
            # So FTP alone answered a cancelled call with
            # `ok=False`/`CONNECT`/502 where HTTP and SFTP propagate.
            # Measured with an explicit `task.cancel()` at five points
            # in a 64 MiB download: HTTP and SFTP cancelled at all five,
            # FTP swallowed four. That is worse than a wrong code -- it
            # defeats every structured-concurrency primitive built on
            # cancellation, so an `asyncio.timeout()` around an FTP call
            # did not fire and the caller got a fabricated transport
            # verdict against a healthy server, counted against that
            # destination's breaker (NEW-R10-4).
            #
            # `cancelling()` is the question that actually separates the
            # two cases: it is non-zero only while this task is really
            # being cancelled, so a `ConnectionResetError` from a server
            # that genuinely reset the connection is still classified,
            # and re-raising restores what the cleanup discarded.
            #
            # It arrived in 3.11, and `requires-python` is `>=3.10`, so
            # on the floor this line raised `AttributeError` -- from the
            # failure path, replacing the envelope this library's whole
            # contract promises with a crash, on the one interpreter CI
            # claims and nothing had run. `_being_cancelled` is where
            # both interpreters are reconciled.
            if _being_cancelled(err):
                raise asyncio.CancelledError from err
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
            local_base(self.client_path),
            overwrite=self.overwrite,
            # One budget per call, built here rather than per file: a
            # recursive download's file count is the *server's* choice,
            # so a per-file allowance is no ceiling at all (R14).
            budget=TransferBudget(self.max_response_bytes))

    async def _tls_value(self) -> Union[ssl.SSLContext, bool]:
        """Return what this session hands ``aioftp`` as its ``ssl``.

        Both blocking reads this needs happen off the event loop: the
        caller's own certificate files inside ``get_ssl_config``
        (AGW-36), and the system CA bundle the no-certificate fallback
        reads inside :func:`tls_context_for` (AGW-37).

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
        # In a thread: the no-certificate branch of `tls_context_for`
        # builds a default context, and that reads the whole system CA
        # bundle synchronously -- on the event loop, once per FTPS
        # session, blocking every other in-flight request while it runs
        # (AGW-37). The same treatment `get_ssl_config` gives its own
        # blocking builder, for the same reason.
        return await asyncio.to_thread(tls_context_for, ssl_config)

    def _validate_request(self) -> None:
        """Check that the caller named a command and a path to run it on.

        Called at the top of :meth:`handle_request` rather than from the
        constructor, and for the same reason ``SFTPRequest`` validates
        its ``mode`` there: ``protocol_info`` is optional for this
        protocol at the entry point (R11-AC3), so an ``FTPRequest`` has
        to remain constructible without one. Called before the connect
        all the same, so nothing is opened for a call that cannot run.

        ``command`` is checked here as well as at the ``getattr`` in
        :meth:`_run_command`, and the redundancy is the point: the
        allowlist reading is one function
        (:func:`~async_gateway.utils.http_file_config.validated_verb`,
        which ``resolve_verb`` also calls), but deferring the *only*
        check to the attribute lookup meant an unreachable host reported
        a caller's typo as ``CONNECT``/502 -- a transport verdict, on a
        call that could never have run, inviting a retry of a spelling
        mistake. Checked before the socket, the caller's own error is
        what they are told about; the lookup keeps its own check so no
        ordering resolves an unadmitted name against a live client.

        Surfaced by removing mypy's ``ignore_errors``: with
        ``server_path`` annotated honestly as ``Optional[str]``, the
        checker showed it reaching ``aioftp.Client.stat``, whose
        signature is ``str | PurePosixPath``. Absent, it arrived there as
        ``None`` and raised ``TypeError: argument should be a str or an
        os.PathLike object`` from inside ``PurePosixPath`` -- and a
        ``TypeError`` belongs to no transport family, so it escaped
        ``request()`` un-enveloped as a library bug rather than being
        reported as the caller's configuration error it is.

        Returns:
            None.

        Raises:
            ConfigurationError: If ``server_path`` is absent or is not a
                non-empty string, or if ``client_path`` is present and is
                not a non-empty string. ``client_path`` is checked for
                the same reason and against the same shape: absent is the
                documented "no local operand" call, but a present
                non-string reached ``Path()`` inside
                :func:`~async_gateway.utils.contained_io.local_base` and
                raised a ``TypeError`` belonging to no transport family,
                which escaped ``request()`` un-enveloped.
            UnsupportedVerbError: If ``command`` names nothing in
                :data:`FTP_COMMANDS` (R15-AC8). A
                ``ConfigurationError``, so it reaches the caller as a
                ``CONFIG``/400 envelope like the path rejection beside
                it.
        """
        if not isinstance(self.server_path, str) or not self.server_path:
            raise ConfigurationError(
                "protocol_info['server_path'] must be a non-empty string "
                f'naming the path on the server, got {self.server_path!r}')
        # `is not None` rather than a truth test: absent is the
        # documented no-local-operand call, which `_run_command` reads
        # for None, and `''` is a caller who meant a path and supplied
        # none -- `Path('')` is the current directory, not an error.
        if self.client_path is not None and (
                not isinstance(self.client_path, str) or not self.client_path):
            raise ConfigurationError(
                "protocol_info['client_path'] must be a non-empty string "
                f'naming the local path, got {self.client_path!r}')
        validated_verb(
            self.command_, allowed=FTP_COMMANDS, setting='command')

    async def _run_command(
        self,
        client: aioftp.Client,
    ) -> Optional[Mapping[str, Any]]:
        """Run the caller's command and read back what it left behind.

        Args:
            client: The connected ``aioftp`` client.

        Returns:
            The facts ``stat`` reports for ``server_path``, or None for a
            command that removed it. The read used to be unconditional,
            so a completed deletion ended in a ``stat`` on a path that no
            longer existed and was reported as a failure (M3).

            ``Mapping[str, Any]``, not ``dict[str, str]``, on both
            halves. ``aioftp.Client.stat`` answers with a
            ``BasicListInfo`` or a ``UnixListInfo``, which are
            ``TypedDict``s: not assignable to a ``dict`` even where the
            fields match, and ``UnixListInfo['unix.mode']`` is an ``int``,
            so the value type was wrong too. The value is forwarded into
            ``protocol_details`` and never mutated, so the read-only type
            is both accurate and sufficient.

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
        # `resolve_verb` returns only for a name it matched in the
        # allowlist, so `command_` is a non-empty str by the time this
        # line runs. mypy cannot see that across the call, and `str()` is
        # how the local re-narrows it without asserting anything the line
        # above has not already enforced -- a no-op for the str this
        # always is, and unreachable for the None it never is here.
        command = str(self.command_).strip().lower()
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
        #
        # `str()` re-narrows what `_validate_server_path` established at
        # the top of `handle_request`; mypy cannot see that across the
        # call, and the local asserts nothing the validator has not
        # already enforced.
        return await client.stat(str(self.server_path))

    def _operands(
        self,
        command: str,
    ) -> Tuple[Optional[str], Optional[str]]:
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

            Either operand may be None: ``protocol_info`` is optional for
            this protocol, so neither path is guaranteed present. The
            caller reaches this method only inside ``if self.client_path``,
            which establishes one of the two; ``aioftp`` refuses the other
            on its own terms if it is missing, and that refusal is a
            transport failure this class already classifies.
        """
        # Absolutised, and by the same function that computes the
        # containment base. `_path_io_factory` confines this transfer to
        # `local_base(self.client_path)`, and `paths.under` splits what
        # `aioftp` composed by *textual* prefix -- so handing the client
        # the caller's original relative spelling gave the two sides two
        # spellings of one directory, which share no prefix, and every
        # write was refused `PATH`/400 (AGW-N3). HTTP resolved the same
        # relative path against the working directory and wrote it.
        local = (
            local_operand(self.client_path)
            if self.client_path is not None else None)
        if LOCAL_IS_SOURCE.get(command, False):
            return local, self.server_path
        return self.server_path, local

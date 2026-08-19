"""SFTP: a verified host key, and an honest report of what was done.

Fills the envelope it was handed and returns that same object, the way
``logic/ftp_client.py`` and ``logic/http_client.py`` do, and raises a
typed ``AsyncGatewayError`` for every remote or transport failure so the
entry point remains the one place an error becomes an ``ok=False``
envelope.

**The host key.** This module used to hardcode a ``known_hosts`` of
``None``, so ``asyncssh`` accepted **any** server key on **every**
session and no consumer could turn verification on: no ``known_hosts``
key was read from ``self.info`` at all. ``client_keys`` was not passed
either, so the host process's ambient ``~/.ssh/id_*`` identities, and its
ssh-agent's, were offered to whatever answered (C4). Four rules replace
that, and they are what this module owns:

* **Verification is on, and the default is asyncssh's own.**
  ``asyncssh.connect`` is called with no ``known_hosts`` argument at all.
  Omitting it is how asyncssh is asked for its documented
  ``~/.ssh/known_hosts`` resolution; passing a value -- any value,
  including the empty tuple that is its default today -- would be this
  library restating a decision it does not own, and is how the next
  reader comes to change the default by editing one literal here.
* **Pinning is explicit.** ``protocol_info['known_hosts']`` takes
  anything asyncssh accepts, and ``protocol_info['host_key']`` takes one
  trusted key and is expressed in asyncssh's own ``(host keys, CA keys,
  revoked keys)`` pinning form.
* **The bypass has to be asked for by its own name.** Only the boolean
  ``insecure_skip_host_key_check=True`` disables verification, and it
  logs a warning naming the risk on every use. Nothing else reaches it:
  not any other truthy value, so a config file's ``'false'`` cannot turn
  verification off by being a non-empty string; not ``verify_ssl``,
  which is the HTTP family's TLS switch and says nothing about SSH host
  keys; and not a caller-supplied ``known_hosts`` of ``None``, which is
  asyncssh's spelling of the same thing and is rejected rather than
  quietly honoured.
* **No ambient identity is ever offered.** ``client_keys=None`` is
  passed by default -- ``None``, and not the empty list that reads like
  "offer nothing". asyncssh's ``prepare`` tests ``client_keys`` for
  truthiness, so ``[]`` falls through to the same
  ``load_default_keypairs()`` branch an omitted argument takes and
  additionally points ``agent_path`` at ``SSH_AUTH_SOCK``: it offers the
  host process's ``~/.ssh/id_*`` files *and* every identity its
  ssh-agent holds. Only ``None`` reaches the branch that offers neither.
  A caller doing key-based authentication supplies
  ``protocol_info['client_keys']`` and the value is forwarded unchanged;
  supplied together with a password, both are offered in asyncssh's own
  order -- public key first, password as the fallback.

**What the session reports.** Three defects sat between a completed
operation and the caller, and they compounded: ``remote_files`` was bound
only inside the directory branch and read unconditionally, so every
single-file target raised ``UnboundLocalError`` *after* the transfer or
the deletion had already happened on the server (H2); a blanket handler
catching every exception then turned that into ``ok=False``,
``error=None`` and a fabricated status code no consumer could read, which
a caller following the documented retry policy re-ran -- deleting the
same file twice and being told both times that nothing had happened.
Three rules replace them:

* **The name cannot be unbound, because there is no name.**
  :meth:`SFTPRequest._run_session` *returns* the attributes and the
  listing, so the directory branch cannot leave a later read reaching for
  something that was never assigned. ``files`` is ``None`` for a target
  that is not a directory and ``[]`` for one that is empty: an empty
  directory and a single file are different answers and stay so.
* **Metadata comes from the typed fields.** ``sftp.lstat`` answers with an
  ``asyncssh.SFTPAttrs``, and the file type is the ``type`` field on it.
  It used to be recovered by splitting ``str(attrs)`` on ``','`` and
  ``':'``, which raised ``IndexError`` on any fragment without a colon
  and ``KeyError`` whenever asyncssh omitted the type -- which it does for
  every unknown type -- and raised it *before the transfer was
  attempted*, so a metadata read decided whether the caller's operation
  ran at all (M4).
* **Nothing the caller owns is mutated.** ``recurse=True`` was written
  into the caller's own ``additional_arguments`` dict, which arrives here
  by reference. One directory ``get`` left it there for every later call
  sharing that ``protocol_info`` -- the ``asyncio.gather`` with one shared
  config this library documents -- so a single-file ``remove`` went out
  with a keyword its caller never wrote (M28).

**Caller-configuration errors leave this class two ways, and the line
between them is the entry point's one conversion ``try`` -- which is to
say, constructor versus method** (AGW-35). The credentials read and the
host-key policy checks in :meth:`SFTPRequest._connect_options` run in
``__init__``, which ``request()`` calls *outside* that ``try``, so their
``ConfigurationError`` escapes **synchronously** and unlogged the way
every other constructor rejection does;
:meth:`SFTPRequest._validate_mode` runs at the top of
:meth:`SFTPRequest.handle_request`, which is *inside* it, so its
``ConfigurationError`` is converted at the one conversion point and
arrives as a logged ``CONFIG``/400 **envelope**.

An earlier reading named *dispatch* as the line. It does not survive
measurement: ``_validate_mode`` is the first statement of
``handle_request`` and returns before any connect is opened, so nothing
has been dispatched when it raises -- and it envelopes anyway, because
what decides is the ``try`` it sits in. The split is otherwise
inherited: ``protocol_info`` is optional for this protocol (R11-AC3),
so an ``SFTPRequest`` has to stay constructible with no ``mode`` in it,
and ``mode`` therefore cannot be checked in ``__init__``.

Failures are classified rather than swallowed: an ``SFTPError`` carries
the server's own ``SSH_FX_*`` code to the status table, and everything
else is matched to a transport family. Anything belonging to no family --
a ``KeyError``, a ``TypeError`` -- propagates unchanged, because it is
this library's bug and not a failed request.
"""

import asyncio
import logging
import os.path
import socket
from collections.abc import Collection
from types import MappingProxyType
from typing import (
    Any,
    Dict,
    Final,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

import asyncssh

from failsafe import CircuitOpen, FailsafeError

from async_gateway.helpers.internal.base import (
    BaseRequestClass,
    credentials_of,
)
from async_gateway.logic.http_client import validated_max_response_bytes
from async_gateway.utils.constants import MAX_RESPONSE_BYTES
from async_gateway.utils.contained_io import (
    TransferBudget, contained_download, local_base, local_operand)
from async_gateway.utils.envelope import GatewayResponse, finalise_ok
from async_gateway.utils.exceptions import (
    AsyncGatewayError,
    CircuitOpenError,
    ConfigurationError,
    ConnectError,
    DnsError,
    GatewayTimeoutError,
    HostKeyError,
    LocalWriteError,
    SftpStatusError,
    TransportError,
    faults_of,
    unwrap_cause,
)
from async_gateway.utils.http_file_config import validated_verb
from async_gateway.utils.redaction import redact_url, redact_value

logger = logging.getLogger(__name__)

# asyncssh's SFTP operations report success by returning None -- there is
# no protocol status on the success path to report -- so a completed
# operation reports the status the spec's table assigns SFTP.
SFTP_SUCCESS_STATUS: Final[int] = 200

# The status each `SSH_FX_*` code the spec's table names maps to. Read off
# `SFTPError.code`, which is that wire code, rather than off the exception
# class: asyncssh's class taxonomy is its own and has grown between
# releases, while the codes are the protocol's.
SFTP_STATUS_BY_FX_CODE: Final[Mapping[int, int]] = MappingProxyType({
    asyncssh.FX_NO_SUCH_FILE: 404,
    asyncssh.FX_PERMISSION_DENIED: 403,
})

# The caller-facing name for each file type asyncssh reports. `SFTPAttrs.
# type` is a `FILEXFER_TYPE_*` integer, and a caller reading an envelope
# should not have to import asyncssh's constants to learn whether they
# downloaded a directory -- so the integer is translated into a small
# stable vocabulary of this library's own. It is deliberately *not* the
# old string parse's vocabulary: that one republished whatever fragment
# `str(SFTPAttrs)` happened to contain, asyncssh's leading space included
# (`' regular'` for a plain file), on the occasions it recovered one at
# all.
FILE_TYPE_NAMES: Final[Mapping[int, str]] = MappingProxyType({
    asyncssh.FILEXFER_TYPE_REGULAR: 'file',
    asyncssh.FILEXFER_TYPE_DIRECTORY: 'directory',
    asyncssh.FILEXFER_TYPE_SYMLINK: 'symlink',
    asyncssh.FILEXFER_TYPE_SPECIAL: 'special',
})

# What asyncssh reports when it cannot classify the target, which is also
# what a v3 server that sent no permission bits leaves behind.
UNKNOWN_FILE_TYPE: Final[str] = 'unknown'

# The `SFTPAttrs` fields `file_stats` carries. Listed explicitly because
# this is the envelope's documented shape: it should change when someone
# decides to change it, not because asyncssh added a field.
FILE_STAT_FIELDS: Final[Tuple[str, ...]] = (
    'size',
    'permissions',
    'uid',
    'gid',
    'owner',
    'group',
    'atime',
    'mtime',
)

# Ordered, because the families overlap: `TimeoutError` is an `OSError`
# from Python 3.11, and `socket.gaierror` is one too. First match wins, so
# the most specific classification is listed first.
TRANSPORT_ERRORS: Sequence[Tuple[type, type]] = (
    # Both timeout classes -- see the identical row in
    # `logic.ftp_client`'s table for the measurement. On 3.10
    # `asyncio.TimeoutError is TimeoutError` is False, so a socket
    # timeout raised as the builtin fell through to the residual
    # `OSError` row and this protocol reported `PATH`/400 for a slow
    # server, blaming the caller's disk and keeping the destination out
    # of its own breaker.
    (asyncio.TimeoutError, GatewayTimeoutError),
    (TimeoutError, GatewayTimeoutError),
    (socket.gaierror, DnsError),
    (ConnectionError, ConnectError),
    # The residual `OSError`, reporting `PATH` rather than the `CONNECT`
    # it used to -- the same change, for the same reason, as the row at
    # the foot of `logic.ftp_client`'s table. A `get` writes to the
    # local disk through `utils.contained_io`, so a full disk or an
    # unwritable directory reached this row and was reported as a
    # *connection* failure: retried, and counted against the remote
    # host's breaker (NEW-R10-1).
    (OSError, LocalWriteError),
)

#: The families this dispatch catches, derived from the table above --
#: the fourth of the four dispatch sites the derivation now reaches
#: (NEW-R10-1). ``asyncssh.Error`` is appended because
#: ``transport_error_for`` classifies it below the table, as this
#: family's catch-all, rather than in it.
TRANSPORT_FAULTS: Tuple[type[BaseException], ...] = (
    faults_of(TRANSPORT_ERRORS) + (asyncssh.Error,))

# The modes that accept a `recurse` keyword, so a directory target can be
# expressed to them at all. `asyncssh.SFTPClient.remove` takes a path and
# nothing else -- it removes a file or a symbolic link, and the directory
# operation is the separate `rmtree` -- so sending it `recurse=True` is a
# `TypeError` from the library, raised for every directory `remove` and
# belonging to no transport family, which means it escapes `request()`
# uncaught rather than becoming an envelope.
#
# All six names asyncssh spells the capability under, not just the two
# this release dispatches: every one of them is reachable through the
# `getattr` lookup in `_run_operation`, so a set naming only `get` and
# `put` would leave the same defect live under `mget` the day R21's
# allowlist admits it.
RECURSING_MODES: Final[frozenset[str]] = frozenset(
    {'copy', 'get', 'mcopy', 'mget', 'mput', 'put'})

# The SFTP operations this library will dispatch, and the whole of what
# `mode` may name (R21-AC1, R17-AC7). Exactly the three this release
# documents -- the class docstring below and the README both say "'get',
# 'put' or 'remove'" -- so the allowlist and the documentation state the
# same thing, which is what R21-AC3 asks of them.
#
# The set is deliberately *narrower* than `RECURSING_MODES`, and the two
# are not in conflict. `RECURSING_MODES` answers "which asyncssh
# operations accept `recurse`", and is broad on purpose so that admitting
# `mget` here later cannot reintroduce the `TypeError` it guards; this
# set answers "which operations may a caller ask for", and admitting a
# verb is a documentation and operand-order commitment, not a lookup.
# `mput`, `mget`, `copy` and `mcopy` are therefore excluded until a story
# owns them: R22 makes the local/remote operand order normative for
# `mput` specifically, and admitting it here first would ship a verb
# whose argument order this module is still passing inverted.
#
# `rmtree`, `chmod`, `symlink`, `exit` and every other attribute of an
# open `asyncssh.SFTPClient` were reachable through the unbounded
# `getattr` this replaces (M25).
SFTP_MODES: Final[frozenset[str]] = frozenset({'get', 'put', 'remove'})

# Which way round each transfer mode's two operands go (AGW-33). The
# real asyncssh signatures do **not** agree on one order:
#
#   get(remotepaths, localpath)    mget(remotepaths, localpath)
#   put(localpaths,  remotepath)   mput(localpaths,  remotepath)
#
# `_run_operation` used to pass `(remote_path, local_path)` positionally
# to every mode. That is correct for `get`/`mget` and **inverted** for
# `put`/`mput`, which then glob the *remote* path on the local disk and
# write to the *local* path on the server. Usually a clean failure; in a
# mirrored-tree deployment a local file exists at the remote path and
# the wrong file goes to the wrong destination with `ok=True`.
#
# A per-mode table beside `RECURSING_MODES`, not a conditional at the
# call site, and this comment rather than none: one positional order
# applied to every verb is exactly how the defect survived, and
# "simplifying" the table back into one is the next reader's most likely
# mistake. `copy`/`mcopy` are absent deliberately -- both operands are
# remote, so neither is local and the question does not arise; whether
# they are dispatchable at all is R21's allowlist, at S19.
# True means the caller's LOCAL path is the source.
LOCAL_IS_SOURCE: Final[Mapping[str, bool]] = MappingProxyType({
    'get': False,
    'mget': False,
    'put': True,
    'mput': True,
})

# The modes that write to the **local** filesystem, and therefore need
# the local destination contained (R22). `_copy` filters `scandir`
# entry names that are `.` or `..` exactly and then `posixpath.join`s
# them onto the destination, so a single server-supplied name that
# *contains* a separator -- `../victimdir/OWNED` -- composes straight
# through and writes outside the target. Reproduced against the real
# `_copy` with only the remote side faked: mode 0644, outside the
# directory the caller named.
DOWNLOADING_MODES: Final[frozenset[str]] = frozenset({'get', 'mget'})

# The three `protocol_info` keys that each name a host-key policy. They
# are alternatives, not layers: naming two of them is a caller asking for
# two different policies at once, and there is no reading of that pair
# which is not a guess.
HOST_KEY_POLICY_KEYS: tuple[str, ...] = (
    'host_key',
    'insecure_skip_host_key_check',
    'known_hosts',
)


def file_stats_for(attrs: asyncssh.SFTPAttrs) -> Dict[str, Any]:
    """Return the metadata an envelope reports for one remote path.

    Read off the typed fields of the object ``sftp.lstat`` answered with.
    The parse this replaces read ``str(attrs)``, an undocumented rendering
    that omits every field asyncssh considers unset, quietly kept the
    space after each ``':'`` inside the values it did recover, and dropped
    any field whose own value contained a comma.

    Args:
        attrs: The attributes ``asyncssh`` reported for the path.

    Returns:
        The POSIX stat facts, with ``type`` translated from asyncssh's
        ``FILEXFER_TYPE_*`` integer into the name a caller reads. A field
        the server did not send is None, which is distinguishable from
        one it sent as zero.
    """
    stats: Dict[str, Any] = {
        'type': FILE_TYPE_NAMES.get(attrs.type, UNKNOWN_FILE_TYPE),
    }
    for name in FILE_STAT_FIELDS:
        stats[name] = getattr(attrs, name)
    return stats


def transport_error_for(
    err: BaseException,
    *,
    redact_params: Collection[str] = (),
) -> AsyncGatewayError:
    """Return the typed error for ``err``, or propagate a library bug.

    The resilience layer wraps whatever the operation raised in a
    ``FailsafeError`` whose own ``str()`` is empty, so both the
    classification and the message have to be taken from the cause rather
    than from the wrapper. A failure raised outside that layer -- an
    ``lstat`` on a path that is not there -- arrives unwrapped and is
    classified directly.

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
    if isinstance(cause, asyncssh.SFTPError):
        return SftpStatusError(message, SFTP_STATUS_BY_FX_CODE.get(cause.code))
    for family, error_class in TRANSPORT_ERRORS:
        if isinstance(cause, family):
            return error_class(message)
    if isinstance(cause, asyncssh.Error) or cause is None:
        return TransportError(message)
    # `from None`: the wrapper is already this exception's `__context__`,
    # and suppressing it keeps the traceback pointing at the bug.
    raise cause from None


class SFTPRequest(BaseRequestClass):
    """Implements asyncssh to make sftp calls."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build an SFTP request from a validated ``protocol_info``.

        Reads the SFTP-specific keys off ``self.info`` once, here, and
        resolves the connect options -- including the host-key policy --
        so the transfer methods below read attributes rather than
        re-deriving them per operation.

        Args:
            *args: Forwarded verbatim to :class:`BaseRequestClass` --
                ``url``, ``auth``, ``response`` and ``info``, in that
                order.
            **kwargs: Forwarded verbatim to the base, which requires
                ``redact_params`` keyword-only.

        Raises:
            ConfigurationError: From the base, if ``info`` is neither
                None nor a mapping, or omits a key this protocol
                requires; from
                :func:`~async_gateway.helpers.internal.base.credentials_of`
                if ``auth`` carries no credentials, which SFTP cannot
                connect without; and from :meth:`_connect_options` if the
                host-key configuration is self-contradictory. All escape
                synchronously, because the entry point constructs this
                object outside its one conversion ``try``. SFTP's own
                ``mode`` is deliberately *not* checked here: it is
                validated once the request runs, because
                ``protocol_info`` is optional for this protocol and the
                object must stay constructible without one.
        """
        super(SFTPRequest, self).__init__(*args, **kwargs)

        self.port: int = self.info.get('port', 22)
        # Not `self.auth.login`: `auth` defaults to None at the entry
        # point and is documented optional, so the read that looks
        # unconditional here is the documented default call crashing with
        # an `AttributeError` that escapes the envelope entirely (H5).
        self.user: str
        self.password: str
        self.user, self.password = credentials_of(
            self.auth, protocol='SFTP')
        # Optional, and genuinely so: `protocol_info` is optional for this
        # protocol (R11-AC3), so an `SFTPRequest` stays constructible with
        # no `mode` in it -- which is exactly why `_validate_mode` runs at
        # the top of `handle_request` and not in `__init__`. Annotating
        # these `str` asserted a non-None the constructor never
        # established, and contradicted the validator that exists because
        # it cannot.
        self.mode_: Optional[str] = self.info.get('mode')
        self.remote_path: Optional[str] = self.info.get('remote_path')
        self.local_path: Optional[str] = self.info.get('local_path')
        # R22-AC3: refuse to overwrite by default, opt in by name.
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
        # Read, never written: `recurse` is added to a copy at the one
        # place it is needed, because this is the caller's own dict and
        # writing into it is M28.
        self.additional_arguments: Dict[str, Any] = self.info.get(
            'additional_arguments', {})

        # `Any` on all three deliberately: what asyncssh accepts for
        # `known_hosts` and `client_keys` is its own union of paths, key
        # objects, byte strings and callables, and restating that union
        # here would only go stale against the library. These values are
        # forwarded, never interpreted.
        self.known_hosts: Any = self.info.get('known_hosts')
        self.host_key: Any = self.info.get('host_key')
        self.client_keys: Any = self.info.get('client_keys') or None
        # `is True`, not `bool()`: a config file's string `'false'` is
        # truthy, and a bypass that a caller believes they turned off is
        # the one failure mode this keyword exists to make impossible.
        self.insecure_skip_host_key_check: bool = (
            self.info.get('insecure_skip_host_key_check') is True)

        self.connect_options: Dict[str, Any] = self._connect_options()

    def _validate_mode(self) -> None:
        """Check that the caller named an operation to run.

        Called at the top of :meth:`handle_request` rather than from the
        constructor: ``protocol_info`` is optional for this protocol at
        the entry point (R11-AC3), so an ``SFTPRequest`` has to remain
        constructible without one. Called before the connect all the
        same, so nothing is opened for a call that cannot run.

        Returns:
            None.

        Raises:
            ConfigurationError: If ``mode`` is absent or is not a
                non-empty string. It used to reach
                ``self.mode_.lower()`` inside the session, after
                ``lstat`` had already run against the server, and
                surface as an ``AttributeError`` on ``None``.
            UnsupportedVerbError: If ``mode`` names nothing in
                :data:`SFTP_MODES` (R17-AC7). A ``ConfigurationError``,
                so it reaches the caller as ``CONFIG``/400 like the
                rejections beside it. Checked here as well as at the
                ``getattr`` in :meth:`_run_session`, and the redundancy
                is the point: the allowlist reading is one function
                (:func:`~async_gateway.utils.http_file_config.validated_verb`,
                which ``resolve_verb`` also calls), but deferring the
                *only* check to the attribute lookup meant the connect
                reported the caller's typo first -- a server whose host
                key did not verify turned an unknown ``mode`` into
                ``HOST_KEY``/495, a transport verdict on a call that
                could never have run. Checked before the connect, the
                caller's own error is what they are told about; the
                lookup keeps its own check so no ordering resolves an
                unadmitted name against a live client. Also raised when
                ``remote_path`` is absent or is not a non-empty string,
                or when ``local_path`` is present and is not one.
        """
        if not isinstance(self.mode_, str) or not self.mode_:
            raise ConfigurationError(
                "protocol_info['mode'] must be a non-empty string naming "
                f'the SFTP operation to run, got {self.mode_!r}')
        validated_verb(self.mode_, allowed=SFTP_MODES, setting='mode')
        # The same check, on the same shape, for the path every mode
        # acts on. Surfaced by removing `mypy`'s `ignore_errors`: with
        # `remote_path` annotated honestly as `Optional[str]`, the
        # checker showed it reaching `sftp.lstat(...)`, whose signature
        # is `bytes | str | PurePath`. Absent, it arrived there as
        # `None` and raised `TypeError: expected str, bytes or
        # os.PathLike object` from inside asyncssh -- which belongs to
        # no transport family, so it escaped `request()` un-enveloped
        # as a library bug rather than being reported as the caller's
        # configuration error it is. Checked here beside `mode` and
        # before the connect, so nothing is opened for a call that
        # cannot run.
        if not isinstance(self.remote_path, str) or not self.remote_path:
            raise ConfigurationError(
                "protocol_info['remote_path'] must be a non-empty string "
                f'naming the path on the server, got {self.remote_path!r}')
        # And the local operand, for the same reason one step further
        # out: absent is the documented no-local-path call, which the
        # transfer reads for None, but a *present* non-string reached
        # `Path()` inside `local_base` and raised a `TypeError` that
        # belonged to no family and escaped un-enveloped. `is not None`
        # rather than a truth test, because `''` is a caller who meant a
        # path and gave none -- `Path('')` is the current directory.
        if self.local_path is not None and (
                not isinstance(self.local_path, str) or not self.local_path):
            raise ConfigurationError(
                "protocol_info['local_path'] must be a non-empty string "
                f'naming the local path, got {self.local_path!r}')

    def _connect_options(self) -> Dict[str, Any]:
        """Return the keyword arguments ``asyncssh.connect`` is called with.

        Built once, at construction, so the caller-configuration errors it
        can raise escape ``request()`` synchronously the way every other
        ``ConfigurationError`` does, rather than being caught by
        ``handle_request``'s own handling and reported to the caller as a
        failed network call.

        Returns:
            The connection keyword arguments. ``known_hosts`` is *absent*
            from them unless the caller named a policy, because its
            absence is precisely how asyncssh is asked for its own
            ``~/.ssh/known_hosts`` resolution.

        Raises:
            ConfigurationError: If ``protocol_info`` names more than one
                host-key policy, or spells the bypass as a
                ``known_hosts`` of ``None`` rather than by its own name.
        """
        if 'known_hosts' in self.info and self.known_hosts is None:
            raise ConfigurationError(
                'protocol_info["known_hosts"] = None turns SSH host key '
                'verification off entirely, and this library accepts '
                'that only under its own name: set '
                'protocol_info["insecure_skip_host_key_check"] = True if '
                'that is what you mean')

        # Read off the parsed attributes rather than off `self.info`: a
        # caller who wrote `insecure_skip_host_key_check=False` has named
        # no policy at all, and a membership test on the raw mapping
        # would read that explicit "no" as a second policy and reject a
        # call that is not ambiguous.
        declared = [
            name for name, asked in (
                ('host_key', self.host_key is not None),
                ('insecure_skip_host_key_check',
                 self.insecure_skip_host_key_check),
                ('known_hosts', self.known_hosts is not None),
            ) if asked
        ]
        if len(declared) > 1:
            raise ConfigurationError(
                f'protocol_info sets {declared}, but the host key policy '
                f'is one decision: name exactly one of '
                f'{list(HOST_KEY_POLICY_KEYS)}, or none of them to take '
                f'the asyncssh default known_hosts resolution')

        options: Dict[str, Any] = {
            'host': self.url,
            'username': self.user,
            'password': self.password,
            'port': self.port,
            # H10-sftp. `base.py` computes `self.timeout` for every
            # protocol and SFTP was the one that never used it, so a
            # server that completed the TCP handshake and then went
            # silent held the coroutine open forever. The breaker cannot
            # help: a hang raises nothing for it to count, so no failure
            # is recorded and the circuit never opens. These bound the
            # connect and the authentication that follows it -- asyncssh
            # offers no deadline for the transfer itself, so a large file
            # is not capped and is not meant to be.
            'connect_timeout': self.timeout,
            'login_timeout': self.timeout,
            # `None` offers nothing, and only `None` does: an empty list
            # is falsy but is not `None`, so asyncssh's `prepare` reads
            # it as unset and hands the host process's own `~/.ssh/id_*`
            # identities -- and its ssh-agent's -- to whatever answered,
            # exactly as an omitted argument does.
            'client_keys': self.client_keys,
        }

        if self.insecure_skip_host_key_check:
            logger.warning(
                'SSH host key verification is disabled for this SFTP '
                'session: any host that answers can impersonate the '
                'endpoint, collect the credentials offered to it and '
                'read or alter every byte transferred. Pin the server '
                'with protocol_info["known_hosts"] or '
                'protocol_info["host_key"] instead.',
                extra={
                    'host': redact_value(
                        self.url, extra_params=self.redact_params),
                },
            )
            # The one place asyncssh's own opt-out is reachable from, and
            # it is reachable only through the keyword just warned about.
            options['known_hosts'] = None
        elif self.host_key is not None:
            # asyncssh's pinning form: trusted host keys, trusted CA
            # keys, revoked keys.
            options['known_hosts'] = ([self.host_key], [], [])
        elif self.known_hosts is not None:
            options['known_hosts'] = self.known_hosts

        return options

    def _host_key_error(self, err: asyncssh.Error) -> HostKeyError:
        """Return the typed failure for an unverifiable server host key.

        Args:
            err: The ``asyncssh`` handshake failure.
                ``HostKeyNotVerifiable`` is the presented key not matching
                what is trusted; ``KeyExchangeFailed`` is the handshake
                ending without an agreed host key algorithm, and its own
                text names the algorithms, which is what makes that case
                actionable. asyncssh reports a cipher or MAC negotiation
                failure through that same class, so classifying it here
                over-reports the cause and under-reports nothing --
                the direction this module errs in.

        Returns:
            A ``HostKeyError``: code ``HOST_KEY``, status 495, with a
            message naming the option that fixes it, so a host simply
            absent from ``~/.ssh/known_hosts`` -- a container, every time
            -- reports something the caller can act on rather than a bare
            asyncssh string.
        """
        return HostKeyError(
            f'SSH host key verification failed for '
            f'{redact_url(self.url, extra_params=self.redact_params)}: '
            f'{err}. Trust this server explicitly with '
            f'protocol_info["known_hosts"] or protocol_info["host_key"] '
            f'if it is not in ~/.ssh/known_hosts.')

    async def handle_request(self) -> GatewayResponse:
        """Run one SFTP operation and fill the envelope with its result.

        config structure-
        url = 'localhost'
        auth = aiohttp.BasicAuth('user','password')
        protocol = 'SFTP'
        protocol_info = {
            'port': 22, # optional, default is 22
            'mode': 'get', # required. The asyncssh SFTP operation to
                # run: 'get', 'put' or 'remove'.
            'remote_path': '',
                # path on the server: the SOURCE of a 'get' and the
                # DESTINATION of a 'put'.
            'local_path': '',
                # path on this machine: the DESTINATION of a 'get' and
                # the SOURCE of a 'put'. The modes point in opposite
                # directions and each is passed in its own -- see
                # LOCAL_IS_SOURCE (AGW-33). Omitted for an operation
                # that names only a remote path.
            'overwrite': False, # optional, default False. A download
                # refuses to replace an existing local file unless this
                # is True; a symbolic link at the destination is
                # refused either way.
            'additional_arguments': {}, # optional. Forwarded to the
                # asyncssh operation. Never mutated: 'recurse' is added
                # to a copy for a directory target.
            'timeout': 15, # optional. Bounds the connect and the login.
            'known_hosts': ..., # optional, see the module docstring
            'host_key': ..., # optional, one trusted server key
            'insecure_skip_host_key_check': False, # optional
            'client_keys': ..., # optional, for key-based authentication
        }

        Returns:
            The same envelope object this request was constructed with,
            populated and finalised. ``protocol_details`` carries the
            mode, the two paths, the file's stats and the directory
            listing -- ``files`` is None for a target that is not a
            directory, and ``[]`` for one that is empty.

        Raises:
            HostKeyError: When the server's host key does not verify
                against the configured policy, or when the handshake ends
                without an agreed host key algorithm.
            SftpStatusError: When the server answers an operation with an
                ``SSH_FX_*`` failure, whose mapped status the envelope's
                ``status_code`` reports.
            CircuitOpenError: When the breaker for this destination is
                open.
            GatewayTimeoutError: When the connect or the login exceeds
                ``timeout``.
            DnsError: When the host name does not resolve.
            ConnectError: When the connection is refused or reset.
            TransportError: For any other transport or SSH failure.
            PathContainmentError: When a path a download would write --
                including one asyncssh composed out of the *server's*
                own entry names -- resolves outside the directory
                ``local_path`` names, or is a symbolic link (R22).
            ConfigurationError: When ``mode`` names no operation. Raised
                before the connect, so nothing is opened for a call that
                cannot run, and -- because it is raised here rather than
                in ``__init__`` -- reported as a ``CONFIG`` envelope. The
                host-key policy errors this class also raises are the
                other case: they are raised in ``__init__``, before
                dispatch, and so escape ``request()`` synchronously
                instead. See the module docstring.
        """
        self._validate_mode()
        try:
            attrs, remote_files = await self._run_session()
        except (asyncssh.HostKeyNotVerifiable,
                asyncssh.KeyExchangeFailed) as host_key_failure:
            raise self._host_key_error(host_key_failure) from host_key_failure
        except CircuitOpen as err:
            raise CircuitOpenError(
                f'circuit open for '
                f'{redact_url(self.url, extra_params=self.redact_params)}'
            ) from err
        except (FailsafeError,) + TRANSPORT_FAULTS as err:
            raise transport_error_for(
                err, redact_params=self.redact_params) from err

        self.response['protocol_details'] = {
            'mode': self.mode_,
            'remote_path': self.remote_path,
            'local_path': self.local_path,
            'file_stats': file_stats_for(attrs),
            'files': remote_files,
        }
        return finalise_ok(
            self.response,
            status_code=SFTP_SUCCESS_STATUS,
            started=self.start_time)

    async def _run_session(
        self,
    ) -> Tuple[asyncssh.SFTPAttrs, Optional[Sequence[str]]]:
        """Open one SSH session and run the caller's operation on it.

        Returns:
            The attributes ``lstat`` reported for ``remote_path``, and the
            directory's entries -- None for a target that is not a
            directory, which is what keeps an empty directory (``[]``)
            distinguishable from a single file. Both are *returned*
            rather than left in a name a later statement reads: a name
            bound only inside the directory branch and read whatever the
            branch did is precisely H2, and returning them makes that
            shape unavailable rather than merely unused.

        **When the remote is stat-ed depends on which way the transfer
        goes** (AGW-40). It used to be unconditionally *before* the
        operation, which is right for every mode whose ``remote_path``
        is a source or a target that must already exist -- ``get``,
        ``remove`` -- and wrong for the uploading modes, whose
        ``remote_path`` is the **destination**. A ``put`` to a path that
        does not exist yet is the ordinary case, and it failed with
        ``SFTP_STATUS`` / "No such file" from a pre-flight ``lstat``
        before the transfer was ever attempted: the library could only
        upload over a file that was already there.

        No unit test could see it. ``tests/fixtures/sftp.py``'s double
        answers ``lstat`` with configured attributes for *any* path, so
        the fixture reports every destination as pre-existing and the
        pre-flight call always succeeded. It took a live server.

        So an uploading mode runs the transfer first and stats the
        destination afterwards -- which is also the only ordering under
        which the reported ``file_stats`` describe the file that was
        *written* rather than whatever happened to be there before.
        """
        # `_validate_mode` has already refused an absent or non-string
        # `remote_path`, and `handle_request` runs it before reaching
        # here. mypy cannot see that across the call, so the local
        # re-narrows what the validator established rather than
        # re-asserting it.
        remote_path = str(self.remote_path)
        mode = str(self.mode_).strip().lower()
        uploading = LOCAL_IS_SOURCE.get(mode, False)

        async with asyncssh.connect(**self.connect_options) as conn:
            async with conn.start_sftp_client() as sftp:
                if uploading:
                    # The source is local, so that is what decides
                    # whether this is a directory transfer. Reading it
                    # off the *remote* would ask about the destination,
                    # which for an upload may not exist at all.
                    is_directory = await asyncio.to_thread(
                        os.path.isdir, self.local_path) \
                        if self.local_path else False
                    await self._run_operation(
                        sftp, is_directory=is_directory)
                    attrs = await sftp.lstat(remote_path)
                    remote_files = (
                        await sftp.listdir(remote_path)
                        if attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY
                        else None)
                    return attrs, remote_files

                attrs = await sftp.lstat(remote_path)
                is_directory = (
                    attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY)
                remote_files = (
                    await sftp.listdir(remote_path)
                    if is_directory else None)
                await self._run_operation(sftp, is_directory=is_directory)
                return attrs, remote_files

    async def _run_operation(
        self,
        sftp: asyncssh.SFTPClient,
        *,
        is_directory: bool,
    ) -> None:
        """Run the caller's mode against the open SFTP channel.

        Args:
            sftp: The open ``asyncssh`` SFTP client.
            is_directory: True when ``lstat`` reported the target as a
                directory. Whether that fact can be *expressed* to the
                operation is this method's decision and not the caller's:
                only :data:`RECURSING_MODES` accept ``recurse``, and a
                mode outside that set is run against the directory
                unmodified so the server's own answer -- not a
                ``TypeError`` from a keyword the library never declared --
                is what the caller is told.

        Returns:
            None.

        Raises:
            UnsupportedVerbError: If ``mode`` names nothing in
                :data:`SFTP_MODES` (R17-AC7). Raised inside
                ``handle_request``'s ``try``, so it reaches the caller as
                a ``CONFIG`` envelope. :meth:`_validate_mode` has already
                refused a mode that is absent or is not a string, before
                the connect; this is the narrower question of whether the
                name is one this library dispatches, and it is asked
                *before* the attribute is read so an unadmitted name
                never resolves against the open channel.
        """
        # Resolved first, so nothing below acts on a mode the allowlist
        # would refuse -- including the `recurse` decision, which would
        # otherwise be made for an operation that is never run.
        operation = self.resolve_verb(
            sftp, self.mode_, allowed=SFTP_MODES, setting='mode')

        # A copy, always. `recurse` used to be written into the caller's
        # own `additional_arguments`, which arrives here by reference
        # through three layers, so one directory transfer left it set for
        # every later call sharing that `protocol_info` (M28).
        options: Dict[str, Any] = dict(self.additional_arguments)
        # `resolve_verb` above returns only for a name it matched in the
        # allowlist, so `mode_` is a non-empty str by the time this line
        # runs. mypy cannot see that across the call, and `str()` re-narrows
        # the local without asserting anything the resolution has not
        # already enforced.
        mode = str(self.mode_).strip().lower()
        if is_directory and mode in RECURSING_MODES:
            options['recurse'] = True

        if not self.local_path:
            await self.circuit_breaker.run(
                operation, self.remote_path, **options)
            return

        if mode in DOWNLOADING_MODES:
            # R22's actual threat model. `get` composes the server's
            # own entry names onto the local destination inside
            # `_copy`, so nothing this class does to `local_path`
            # before the call reaches the paths that are written.
            # `contained_download` runs the same transfer through a
            # local filesystem view that refuses to leave the
            # directory `local_path` names.
            await self.circuit_breaker.run(
                contained_download,
                sftp,
                mode,
                self.remote_path,
                # The operand and the base are one absolutisation, not
                # two spellings of it: `contained_download` judges every
                # composed path against `base` by textual prefix, so a
                # relative `local_path` here refused the whole transfer
                # `PATH`/400 (AGW-N3).
                local_operand(self.local_path),
                base=local_base(self.local_path),
                overwrite=self.overwrite,
                # One budget per call, not per file: the server chooses
                # how many files a recursive `get` writes, so a
                # per-file allowance bounds nothing (R14).
                budget=TransferBudget(self.max_response_bytes),
                **options)
            return

        source, destination = self._operands(mode)
        await self.circuit_breaker.run(
            operation, source, destination, **options)

    def _operands(
        self,
        mode: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(source, destination)`` in this mode's own direction.

        AGW-33. See :data:`LOCAL_IS_SOURCE` for why a per-mode table
        decides this and one shared positional order cannot.

        Args:
            mode: The normalised mode name.

        Returns:
            The two operands, source first, in the order asyncssh
            defines for *this* mode. A mode outside
            :data:`LOCAL_IS_SOURCE` that nonetheless carries a
            ``local_path`` keeps the historical
            ``(remote_path, local_path)`` order -- ``copy``/``mcopy``
            are the live case, and both their operands are remote, so
            neither is the local one and there is no direction here to
            establish. Whether they are dispatchable at all is R21's
            allowlist, at S19.

            Either operand may be None: ``protocol_info`` is optional for
            this protocol, so neither path is guaranteed present. The
            caller reaches this method only inside ``if self.local_path``,
            which establishes one of the two; asyncssh refuses the other
            on its own terms if it is missing, and that refusal is a
            transport failure this class already classifies.
        """
        # Absolutised through the same function that computes the
        # containment base, for the reason AGW-N3 gives: a relative
        # `local_path` handed to asyncssh while the base is absolute
        # gives `paths.under` two spellings of one directory to compare
        # textually, and every write is refused `PATH`/400.
        local = (
            local_operand(self.local_path)
            if self.local_path is not None else None)
        if LOCAL_IS_SOURCE.get(mode, False):
            return local, self.remote_path
        return self.remote_path, local

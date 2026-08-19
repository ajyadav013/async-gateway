"""The protocol-strategy contract every protocol client implements.

One envelope, created by the entry point, is handed to the subclass, which
fills it and returns the same object. A subclass never builds a response
shape and never returns ``True``: the shape is owned by
``utils/envelope.py`` and nothing else constructs one.

Which keys a protocol *requires* is the protocol's own knowledge, so each
subclass declares them in :attr:`BaseRequestClass.REQUIRED_INFO_KEYS` --
that is what lets the entry point check them without naming a single
protocol. The check itself runs there, at the boundary, and not again here:
this class acts on configuration that has already been validated.
"""

import abc
from collections.abc import Collection, Mapping
from typing import Any, ClassVar, Optional, Tuple
from urllib.parse import urlsplit

from asyncio_gateway.helpers.common.date_helper import monotonic_now
from asyncio_gateway.helpers.internal.breaker_registry import get_breaker
from asyncio_gateway.helpers.internal.circuit_breaker_helper import (
    CircuitBreakerHelper,
)
from asyncio_gateway.utils.constants import (DEFAULT_PORTS, HTTP_TIMEOUT,
                                             PORT_RANGE_HIGH, PORT_RANGE_LOW,
                                             UNKNOWN_PORT)
from asyncio_gateway.utils.envelope import GatewayResponse
from asyncio_gateway.utils.exceptions import ConfigurationError
from asyncio_gateway.utils.http_file_config import resolve_verb
from asyncio_gateway.utils.redaction import redact_url


def validated_protocol_info(
    info: Optional[Mapping[str, Any]],
    *,
    required: Collection[str] = (),
) -> dict[str, Any]:
    """Return ``protocol_info`` as a dict once its shape is known good.

    The one implementation of "is this ``protocol_info`` usable", called
    once, at the one boundary caller data enters through: ``request()``,
    which has to read ``redact_query_params`` off it and check the chosen
    protocol's required keys before anything is dispatched or the caller's
    pre-processor runs. :class:`BaseRequestClass` does not call it again --
    a second check on already-validated data is how one of them comes to
    accept what the other rejects.

    ``None`` is the documented "no protocol_info" call and reads as an
    empty mapping rather than as an error -- the crash it replaces was an
    ``AttributeError`` on ``None.get`` (H5). Empty is not a bypass, though:
    it is still held to ``required``, because a protocol that cannot run
    without a key cannot run without it when the caller passed nothing
    either.

    Args:
        info: ``protocol_info`` exactly as the caller supplied it, or None.
        required: Key names this protocol cannot run without, from the
            protocol class's own :attr:`BaseRequestClass.REQUIRED_INFO_KEYS`.

    Returns:
        A new dict of the caller's configuration, empty when they supplied
        nothing.

    Raises:
        ConfigurationError: If ``info`` is neither None nor a mapping, or if
            any required key is absent. Both are the caller's own
            configuration failing to form a valid call, so both are reported
            as configuration rather than as an ``AttributeError`` or a
            ``KeyError`` from somewhere further in.
    """
    if info is None:
        info = {}
    elif not isinstance(info, Mapping):
        raise ConfigurationError(
            f'protocol_info must be a mapping or None, got '
            f'{type(info).__name__}')
    missing = sorted(set(required) - set(info))
    if missing:
        raise ConfigurationError(
            f'protocol_info is missing required key(s) {missing}')
    return dict(info)


def validated_port(port: Any) -> Optional[int]:
    """Return ``protocol_info['port']`` once proven a usable port number.

    The ``validated_*`` family's newest member, and it exists for the
    reason every other one does: ``protocol_info['port']`` was read with
    no checking at all and handed straight to
    :func:`destination_of`, whose return value becomes the breaker
    registry's ``(family, host, port)`` **dict key**. An unhashable
    value -- a list, a dict, a set -- therefore reached
    ``_BREAKERS.get(key)`` and raised ``TypeError: cannot use 'tuple' as
    a dict key (unhashable type: 'list')`` out of ``request()``
    un-enveloped, on every one of the five protocols. A caller cannot be
    asked to catch a builtin for a configuration mistake the contract
    does not name.

    **Hashability is not the check, though, and checking only that is
    the trap this validator is written to avoid.** A ``port='21'`` is
    perfectly hashable and would have sailed past a hashability guard
    straight into a *second* key for a destination that already has one:
    ``('ftp', 'h', '21')`` and ``('ftp', 'h', 21)`` are distinct keys, so
    the two calls accumulate their failure counts separately and neither
    reaches the threshold -- H8's "the circuit can never open", restored
    quietly for one caller. Worse, FTP passes the value on to
    ``aioftp``'s connect call, and SFTP puts it in its
    ``protocol_details``. So the type is checked, and so is the range.

    ``bool`` is rejected for the reason the whole family rejects it:
    ``True`` is an ``int`` of value 1, and a call dispatched to port 1
    is not what anyone meant by ``port=True``.

    None is returned unchanged and means "the caller named no port",
    which :func:`destination_of` already answers by reading one out of
    the URL or falling back to the family default. That is a documented
    call, not an omission.

    Args:
        port: ``protocol_info['port']`` exactly as the caller supplied
            it, of whatever type they actually passed, or None when they
            supplied none.

    Returns:
        The port as an ``int``, or None when the caller named none.

    Raises:
        ConfigurationError: If the value is not None and is not an
            integer in ``0..65535``. A port outside that range cannot
            reach any socket -- ``aioftp`` and ``asyncssh`` both raise
            an ``OverflowError`` from deep inside the transport for one
            -- and :data:`~asyncio_gateway.utils.constants.UNKNOWN_PORT`
            is ``-1``, so accepting a negative port would let a caller
            collide with the registry's own "no port known" sentinel.
    """
    if port is None:
        return None
    if isinstance(port, bool) or not isinstance(port, int):
        raise ConfigurationError(
            f'protocol_info["port"] must be an integer port number in '
            f'{PORT_RANGE_LOW}..{PORT_RANGE_HIGH}, got '
            f'{type(port).__name__}')
    if not PORT_RANGE_LOW <= port <= PORT_RANGE_HIGH:
        raise ConfigurationError(
            f'protocol_info["port"] must be an integer port number in '
            f'{PORT_RANGE_LOW}..{PORT_RANGE_HIGH}, got {port}')
    return port


def credentials_of(auth: Any, *, protocol: str) -> Tuple[str, str]:
    """Return the ``(user, password)`` a credentialled protocol will use.

    FTP and SFTP have no anonymous mode in this library: both interpolate
    a user name and a password into their connect call, so a call without
    credentials cannot be formed at all. ``request()`` nonetheless
    defaults ``auth`` to None and documents it as optional, which made
    the *documented default* call for those two protocols an
    ``AttributeError`` on ``None.login`` raised from the constructor
    (H5). That escaped the envelope, and it contradicted this library's
    own contract that a non-``AsyncGatewayError`` reaching the caller
    means a bug in here -- a missing credential is the caller's
    configuration, not a library bug.

    Read once, here, rather than as two attribute reads per protocol, so
    both protocols reject the same shapes with the same message and
    neither can drift into accepting what the other refuses.

    Args:
        auth: The caller's ``auth`` argument, of whatever type they
            actually passed. None is what arrives on the documented
            default call; an object carrying no ``login``/``password``
            is what arrives from a caller who passed some other kind of
            auth object.
        protocol: The protocol name to name in the message, so the
            caller is told which of their calls needs credentials.

    Returns:
        The login and the password to connect with.

    Raises:
        ConfigurationError: If ``auth`` is None, or carries no string
            ``login`` and ``password``. Raised from the protocol
            constructor, which the entry point runs *outside* its one
            conversion ``try``, so it escapes to the caller
            synchronously and unlogged -- the same contract every other
            pre-dispatch configuration rejection follows.
    """
    login = getattr(auth, 'login', None)
    password = getattr(auth, 'password', None)
    if not isinstance(login, str) or not isinstance(password, str):
        raise ConfigurationError(
            f'{protocol} requires credentials: auth must carry a string '
            f'"login" and "password", as aiohttp.BasicAuth(user, '
            f'password) does, got {type(auth).__name__}')
    return login, password


def destination_of(
    protocol: str,
    url: str,
    port: Optional[int] = None,
) -> Tuple[str, str, int]:
    """Return the ``(family, host, port)`` this call is dispatched to.

    The breaker registry's key, and the whole of what "per destination"
    means (M16). Two calls share a breaker exactly when all three parts
    match, so ``https://a`` and ``https://b`` never do, and one host on
    two ports does not either.

    The HTTP family carries its host and port inside the URL; FTP and
    SFTP take a bare host name and their port from ``protocol_info``,
    which is why ``port`` is a parameter rather than something parsed
    here.

    Args:
        protocol: The normalised protocol name -- ``'HTTP'``, ``'HTTPS'``,
            ``'FTP'``, ``'SFTP'``.
        url: The URL or bare host this call is dispatched to.
        port: The port the protocol resolved, for protocols that carry it
            outside the URL. None means "take it from the URL, or from
            the family's default".

    Returns:
        The family lower-cased, the host lower-cased, and the port. A
        scheme with no known default and no port in the URL reports
        :data:`~asyncio_gateway.utils.constants.UNKNOWN_PORT`, which is
        ``-1`` and deliberately not ``0``: ``0`` is a legal port number,
        so using it as "unknown" would collapse every unknown-scheme
        destination onto a single key and re-create M16 for exactly the
        callers whose scheme this library did not anticipate.

    Raises:
        ConfigurationError: If the URL cannot be parsed at all.
            ``urlsplit`` itself raises ``ValueError('Invalid IPv6 URL')``
            on an unclosed bracket, and this is the *first* thing every
            protocol does with a URL. ``dispatch_url_for`` already
            converts that same failure, but only for the HTTP family --
            so ``http://[::1`` reached FTP, SFTP and SOAP here as a bare
            ``ValueError`` escaping ``request()`` un-enveloped. Found by
            the entry-point invariant matrix in
            ``tests/test_entrypoint_invariant.py``, which is what that
            file is for.
    """
    try:
        parsed = urlsplit(url if '://' in url else f'//{url}')
    except ValueError as err:
        raise ConfigurationError(
            f'url is not parseable: '
            f'{redact_url(url)}') from err
    family = (parsed.scheme or protocol).lower()
    host = (parsed.hostname or '').lower()

    if port is None:
        # `parsed.port` raises on a port that is not a number, which is a
        # malformed URL the dispatch guard has already rejected for the
        # HTTP family; `netloc` is what remains for everything else.
        port = parsed.port if _has_numeric_port(parsed.netloc) else None
    if port is None:
        port = DEFAULT_PORTS.get(family, UNKNOWN_PORT)
    return family, host, port


def _has_numeric_port(netloc: str) -> bool:
    """Report whether ``netloc`` ends in a port this library can read.

    Args:
        netloc: The network-location part of a split URL.

    Returns:
        True when a numeric port follows the last colon outside any
        IPv6 bracket, so reading ``parsed.port`` cannot raise.
    """
    tail = netloc.rpartition(']')[2]
    _, colon, candidate = tail.rpartition(':')
    return bool(colon) and candidate.isdigit()


class BaseRequestClass(abc.ABC):
    """Base class for handling json requests."""

    #: Keys this protocol cannot run without. The default requires nothing,
    #: so a protocol whose every key has a default inherits it untouched.
    REQUIRED_INFO_KEYS: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self, url: str,
        auth: Any,
        response: GatewayResponse,
        info: Optional[Mapping[str, Any]],
        *,
        redact_params: Collection[str]
    ) -> None:
        """Initialize the request as per the config.

        :param url: url to make http/ftp/sftp call.
        :param auth: whatever auth object the caller passed, e.g.
        ``aiohttp.BasicAuth(username, password)``. ``Any``, matching
        ``request()``'s own ``auth: object = None`` and the README --
        this library forwards it to the HTTP transport untouched and
        reads ``.login``/``.password`` off it on FTP and SFTP, so it
        accepts anything aiohttp does and, at this layer, ``None``. The
        previous ``aiohttp.BasicAuth`` annotation named neither the
        default nor the alternatives, and made the entry point's own
        documented signature a type error.
        :param response: the ``GatewayResponse`` skeleton created by the
        entry point. The subclass fills it and returns it; it never
        replaces it with a dict of its own.
        :param info: protocol_info as ``validated_protocol_info`` returned
        it at the entry point, or None for the documented "no
        protocol_info" call. Copied into ``self.info``; every read below
        this line goes through that and never through the raw parameter,
        which is the defect (H5) that made ``protocol_info=None`` an
        ``AttributeError``. It is *not* re-validated: the required-key and
        shape checks belong at the boundary, which has already run them.
        :param redact_params: the caller's additional sensitive
        query-parameter names, already normalised by
        ``normalise_param_names``. Required rather than defaulted, and
        handed down rather than re-read from ``info``: a subclass that
        derived its own set from ``info['redact_query_params']`` would be
        a second normalisation, and two normalisations are how a URL came
        to be masked in the envelope and echoed in the clear in an
        exception message. Every URL a subclass interpolates into an
        exception message is redacted with *this* set.
        """
        self.url = url
        self.auth = auth
        self.response = response
        self.info: dict[str, Any] = {} if info is None else dict(info)
        self.redact_params: frozenset[str] = frozenset(redact_params)
        # Monotonic, so a wall-clock step cannot make `latency` negative,
        # and float, because that is what a clock reading is (L4).
        self.start_time: float = monotonic_now()
        # `Any`, and deliberately, on both of these. Each is a raw
        # caller-supplied value that has *not yet been validated*, and the
        # annotation has to say so or it is a claim this line does not
        # establish:
        #
        # * `timeout` is whatever the caller wrote -- the HTTP and SOAP
        #   subclasses replace it with an `aiohttp.ClientTimeout` built
        #   through `validated_timeout`, FTP and SFTP pass it to their
        #   transports as seconds. Annotating `int` here contradicted both
        #   subclasses and described a `'ten'` as impossible when
        #   `validated_timeout` exists precisely because it is not.
        # * `certificate` is `None` far more often than it is a pair, and
        #   its shape is checked by `get_ssl_config`, which takes `Any`
        #   for the same reason. `Tuple[str]` was wrong three ways: the
        #   absent case, the arity (it is a *pair*), and the validation
        #   this class does not perform.
        self.timeout: Any = self.info.get('timeout', HTTP_TIMEOUT)
        self.certificate: Any = self.info.get('certificate')

        # Read, never written to. The old code wrote a live `RetryPolicy`
        # into this very dict, so a `protocol_info` reused across two
        # calls came back to its owner carrying a resilience object they
        # never put there (M12).
        self.circuit_breaker_config: dict[str, Any] = self.info.get(
            'circuit_breaker_config', {})
        # Looked up, not constructed. A breaker built here is a breaker
        # with a zeroed failure count on every request, which is why the
        # advertised circuit could never open (H8); the registry keys one
        # per destination so accumulating that count does not make one
        # flaky host everyone's outage (M16).
        self.circuit_breaker: CircuitBreakerHelper = get_breaker(
            *destination_of(
                self.response.get('protocol', ''),
                url,
                self.info.get('port'),
            ),
            self.circuit_breaker_config,
        )

    def resolve_verb(
        self,
        client: Any,
        verb: Any,
        *,
        allowed: Collection[str],
        setting: str,
    ) -> Any:
        """Return the operation ``verb`` names, once the allowlist admits it.

        The contract R21 puts on this base class, so every protocol
        subclass reaches a caller-named operation the same way and no
        subclass has to remember to check first. The rule the check
        enforces is that the allowlist is consulted *in the same
        function* as the attribute read, which is what a shared
        implementation gives and a per-subclass one only promises.

        Which verbs a protocol admits is the protocol's own knowledge and
        is passed in, exactly as
        :attr:`BaseRequestClass.REQUIRED_INFO_KEYS` is declared by the
        subclass: this class holds the *mechanism* and names no verb of
        any protocol.

        Args:
            client: The transport client the operation is read off.
            verb: The verb exactly as the caller supplied it.
            allowed: The verbs this protocol admits, lower-cased.
            setting: The ``protocol_info`` key the verb came from.

        Returns:
            The bound operation, ready to call.

        Raises:
            UnsupportedVerbError: If ``verb`` names nothing in
                ``allowed``. A ``ConfigurationError``, so a subclass
                raising it from inside ``handle_request`` reports
                ``CONFIG``/400.
        """
        return resolve_verb(client, verb, allowed=allowed, setting=setting)

    @abc.abstractmethod
    async def handle_request(self) -> GatewayResponse:
        """Dispatch the call and return the filled envelope.

        Returns:
            The same ``GatewayResponse`` this object was constructed with,
            populated and finalised.

        Raises:
            AsyncGatewayError: For every remote or transport failure. The
                entry point is the one place that converts one into an
                ``ok=False`` envelope; anything else propagates.
        """
        raise NotImplementedError

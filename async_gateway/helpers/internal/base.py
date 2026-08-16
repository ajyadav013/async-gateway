"""The protocol-strategy contract every protocol client implements.

One envelope, created by the entry point, is handed to the subclass, which
fills it and returns the same object. A subclass never builds a response
shape and never returns ``True``: the shape is owned by
``utils/envelope.py`` and nothing else constructs one.
"""

import abc
from collections.abc import Collection
from typing import Text, Tuple

import aiohttp
from async_gateway.helpers.common.date_helper import monotonic_now
from async_gateway.helpers.internal.circuit_breaker_helper import CircuitBreakerHelper
from async_gateway.utils.constants import HTTP_TIMEOUT
from async_gateway.utils.envelope import GatewayResponse


class BaseRequestClass(abc.ABC):
    """Base class for handling json requests."""

    def __init__(
        self, url: Text,
        auth: aiohttp.BasicAuth,
        response: GatewayResponse,
        info: dict,
        *,
        redact_params: Collection[Text]
    ) -> None:
        """Initializing the request as per the config.

        :param url: url to make http/ftp/sftp call.
        :param auth: auth object for ex aiohttp.BasicAuth(username, password)
        :param response: the ``GatewayResponse`` skeleton created by the
        entry point. The subclass fills it and returns it; it never
        replaces it with a dict of its own.
        :param info: protocol_info passed in request function
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
        self.info = info if info else {}
        self.redact_params: frozenset[Text] = frozenset(redact_params)
        # Monotonic, so a wall-clock step cannot make `latency` negative,
        # and float, because that is what a clock reading is (L4).
        self.start_time: float = monotonic_now()
        self.timeout: int = info.get('timeout', HTTP_TIMEOUT)
        self.certificate: Tuple[Text] = info.get('certificate')

        self.circuit_breaker_config: dict = self._get_circuit_breaker_config(
            info.get('circuit_breaker_config', {}))
        self.circuit_breaker = CircuitBreakerHelper(**self.circuit_breaker_config)

    def _get_circuit_breaker_config(self, circuit_breaker_config: dict) -> dict:
        """return retry policy for circuit breaker config."""
        if circuit_breaker_config.get('retry_config'):
            retry_policy_dict: dict = circuit_breaker_config['retry_config']
            retry_policy = CircuitBreakerHelper.get_retry_policy(**retry_policy_dict)
            circuit_breaker_config['retry_policy'] = retry_policy
        return circuit_breaker_config

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

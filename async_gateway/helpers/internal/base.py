"""This file contains base classes for json and soap."""

import abc
import time
from typing import Text, Tuple
import aiohttp
from async_gateway.helpers.internal.circuit_breaker_helper import CircuitBreakerHelper
from async_gateway.utils.constants import HTTP_TIMEOUT


class BaseRequestClass(abc.ABC):
    """Base class for handling json requests."""

    def __init__(
        self, url: Text,
        auth: aiohttp.BasicAuth,
        response: dict,
        info: dict
    ) -> None:
        """Initializing the request as per the config.

        :param url: url to make http/ftp/sftp call.
        :param auth: auth object for ex aiohttp.BasicAuth(username, password)
        :param response: Dict object with following parameters url,
        payload, external_call_request_time, text, error_message
        :param info: protocol_info passed in request function
        """
        self.url = url
        self.auth = auth
        self.response = response
        self.info = info if info else {}
        self.start_time: int = time.time()
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
    async def handle_request(self):
        """This is the main class to implement any protocol."""
        raise NotImplementedError

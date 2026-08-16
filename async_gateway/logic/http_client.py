"""Http."""

import time
from typing import Any, Callable, Dict, List, Text

import aiohttp
from async_gateway.helpers.internal import header_response_mapping
from async_gateway.helpers.internal.base import BaseRequestClass
from async_gateway.helpers.internal.request_helper import \
    handle_http_request
from async_gateway.utils.exceptions import ConfigurationError
from async_gateway.utils.request_tracer import request_tracer
import orjson

JsonSerializer = Callable[[Any], Text]


def default_json_serialize(obj: Any) -> Text:
    """Serialise ``obj`` to a JSON string.

    The default for ``ClientSession(json_serialize=...)``, which requires a
    ``str``-returning callable. ``orjson.dumps`` returns ``bytes``, so it can
    never be passed bare; this wrapper is what makes the migration safe.

    Args:
        obj: Any object ``orjson`` can serialise.

    Returns:
        The JSON encoding of ``obj`` as text.

    Raises:
        TypeError: If ``orjson`` cannot serialise ``obj``.
    """
    return orjson.dumps(obj).decode()


def validated_json_serializer(serialization: JsonSerializer) -> JsonSerializer:
    """Return ``serialization`` once proven callable and ``str``-returning.

    ``aiohttp`` calls ``json_serialize`` deep inside payload construction and
    a ``bytes`` return surfaces there as an opaque failure, long after the
    caller's mistake. Probing it here — with an empty mapping, the cheapest
    input every JSON serialiser accepts — turns that into a rejection at the
    boundary. The commonest mistake is passing ``orjson.dumps`` itself; a
    value that is not callable at all is the same class of caller error and
    is rejected the same way, rather than as a bare ``TypeError``.

    Args:
        serialization: The caller-supplied JSON serialiser, or the default.

    Returns:
        The same callable, unwrapped and unmodified.

    Raises:
        ConfigurationError: If the value is not callable, or does not
            return ``str``.
    """
    if not callable(serialization):
        raise ConfigurationError(
            f'protocol_info["serialization"] must be callable, but '
            f'{serialization!r} is of type '
            f'{type(serialization).__name__}',
            {'received': type(serialization).__name__},
        )
    probe = serialization({})
    if not isinstance(probe, str):
        raise ConfigurationError(
            f'protocol_info["serialization"] must return str, but '
            f'{getattr(serialization, "__name__", serialization)!r} '
            f'returned {type(probe).__name__}',
            {'returned': type(probe).__name__},
        )
    return serialization


class HttpRequest(BaseRequestClass):
    """Implements Aiohttp Request to make http/https calls."""

    def __init__(self, *args, **kwargs) -> None:
        """Initializing the http request class."""
        super(HttpRequest, self).__init__(*args, **kwargs)

        self.request_type: Text = self.info['request_type']
        self.cookies: Any = self.info.get('cookies')
        self.headers: Dict = self.info.get('headers', {})
        self.verify_ssl: bool = self.info.get('verify_ssl', True)
        self.trace_config: List[aiohttp.TraceConfig()] = self.info.get(
            'trace_config', [request_tracer()])
        self.http_file_upload_config: Dict = self.info.get('http_file_upload_config', {})
        self.file_download_config: Dict = self.info.get('http_file_download_config', {})
        self.serialization: JsonSerializer = validated_json_serializer(
            self.info.get('serialization', default_json_serialize))
        self.timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(
            total=self.timeout)

    async def handle_request(self):
        """Make network call using aiohttp and returns the response."""
        async with aiohttp.ClientSession(
                    trace_configs=self.trace_config, cookies=self.cookies,
                    headers=self.headers, timeout=self.timeout,
                    auth=self.auth, json_serialize=self.serialization
            ) as session:
                try:
                    self.response = await handle_http_request(
                        session,
                        self.url,
                        self.request_type,
                        self.circuit_breaker,
                        headers=self.headers,
                        payload=self.response['payload'],
                        certificate=self.certificate,
                        verify_ssl=self.verify_ssl,
                        http_file_download_config=self.file_download_config,
                        http_file_upload_config=self.http_file_upload_config,
                    )
                    res_content_type: Text = self.response['headers'].get(
                        'Content-Type', 'default').lower()
                    self.response['json'] = ''
                    for content_type_value in header_response_mapping.keys():
                        if content_type_value in res_content_type:
                            self.response['json'] = await header_response_mapping[
                                content_type_value](self.response['text'])
                    self.response['request_tracer'] = [
                        tc.results_collector for tc in self.trace_config
                    ]
                    self.response['latency'] = (time.time() - self.start_time)
                except Exception as request_error:
                    self.response['status_code'] = 999
                    self.response['latency'] = (time.time() - self.start_time)
                    self.response['text'] = request_error

                return self.response

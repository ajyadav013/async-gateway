"""Http."""

import time
from typing import Any, Callable, Dict, List, Text

import aiohttp
from async_gateway.helpers.internal import header_response_mapping
from async_gateway.helpers.internal.base import BaseRequestClass
from async_gateway.helpers.internal.request_helper import \
    handle_http_request
from async_gateway.utils.request_tracer import request_tracer
import ujson


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
        self.serialization: Callable = self.info.get('serialization', ujson.dumps)
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

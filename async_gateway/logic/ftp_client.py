"""Ftp."""

import time
from typing import Dict, Text

import aioftp
from async_gateway.helpers.internal.base import BaseRequestClass
from async_gateway.helpers.internal.filters_helper import get_ssl_config


class FTPRequest(BaseRequestClass):
    """Implements Aioftp to make ftp calls."""

    def __init__(self, *args, **kwargs) -> None:
        """Initializing the ftp request class."""
        super(FTPRequest, self).__init__(*args, **kwargs)

        self.port: int = self.info.get('port', 21)
        self.user: Text = self.auth.login
        self.password: Text = self.auth.password
        self.command_: Text = self.info.get('command', None)
        self.server_path: Text = self.info.get('server_path', None)
        self.client_path: Text = self.info.get('client_path', None)
        self.verify_ssl: bool = self.info.get('verify_ssl', False)

    async def handle_request(self):
        """Aioftp Request.

        config structure-
        url = 'localhost'
        auth = aiohttp.BasicAuth('user','password'), only basicAuth allowed
        protocol = 'FTP'
        protocol_info = {
            'port': 21, # optional ddefault is 21
            'command': 'download', # download, upload, remove
            'server_path': '',
                # path from where to get/delete or upload file on server.
            'client_path': '',
                # path where file is downloaded/uploaded to.
        }
        """
        if self.verify_ssl:
            ssl_context: Dict = await get_ssl_config(self.certificate, verify_ssl)
            verify_ssl = ssl_context.get('ssl_context')
        try:
            async with aioftp.Client.context(
                self.url, self.port, self.user, self.password,
                ssl=verify_ssl,
                connection_timeout=self.timeout
            ) as client:
                make_ftp_request = getattr(client, self.command_.lower())
                if self.client_path:
                    await self.circuit_breaker.failsafe.run(
                        make_ftp_request,
                        self.server_path,
                        self.client_path,
                        write_into=True)
                else:
                    await self.circuit_breaker.failsafe.run(
                        make_ftp_request,
                        self.server_path)
                self.response['mode'] = self.command_
                self.response['file'] = self.server_path
                self.response['file_stats'] = await client.stat(self.server_path)

                return True

        except Exception as request_error:
            self.response['status_code'] = 999
            self.response['latency'] = (time.time() - self.start_time)
            self.response['text'] = request_error

            return self.response

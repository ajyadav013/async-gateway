"""SFTP."""

import time
from typing import Dict, List, Optional, Text, Union
from async_gateway.helpers.internal.base import BaseRequestClass
import asyncssh


class SFTPRequest(BaseRequestClass):
    """Implements asyncssh to make sftp calls."""

    def __init__(self, *args, **kwargs) -> None:
        """Initializing the ftp request class."""
        super(SFTPRequest, self).__init__(*args, **kwargs)

        self.port: int = self.info.get('port', 22)
        self.user: Text = self.auth.login
        self.password: Text = self.auth.password
        self.mode_: Text = self.info.get('mode', None)
        self.remote_path: Text = self.info.get('remote_path', None)
        self.local_path: Text = self.info.get('local_path', None)
        self.remote_files: Optional[Union[List, Text]] = self.remote_path
        self.additional_arguments: Dict = self.info.get('additional_arguments', {})

    async def handle_request(self):
        """Make network call using asyncssh over sftp protocol."""
        try:
            async with asyncssh.connect(host=self.url,
                                        username=self.user,
                                        password=self.password,
                                        port=self.port,
                                        known_hosts=None) as conn:
                async with conn.start_sftp_client() as sftp:
                    lstat = {
                        i.split(':')[0]: i.split(':')[1]
                        for i in str(await sftp.lstat(self.remote_path)).split(',')
                        }
                    # getting files list for directory
                    if 'directory' in lstat['type']:
                        remote_files: List = await sftp.listdir(self.remote_path)
                        self.additional_arguments.update({'recurse': True})

                    make_sftp_request = getattr(sftp, self.mode_.lower())
                    if self.local_path:
                        await self.circuit_breaker.failsafe.run(
                            make_sftp_request,
                            self.remote_path,
                            self.local_path,
                            **self.additional_arguments)
                    else:
                        await self.circuit_breaker.failsafe.run(
                            make_sftp_request,
                            self.remote_path,
                            **self.additional_arguments)

                    self.response['mode'] = self.mode_
                    self.response['file_stats'] = lstat
                    self.response['files'] = remote_files
                    self.response['tat'] = (time.time() - self.start_time)
                return True

        except Exception as request_error:
            self.response['status_code'] = 999
            self.response['latency'] = (time.time() - self.start_time)
            self.response['text'] = request_error

            return self.response

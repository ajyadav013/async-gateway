"""One FTP download, over FTPS, into a path that must not already exist.

Run it against a local FTP server::

    python examples/ftp_example.py 127.0.0.1

Two defaults are worth knowing before you copy this. ``verify_ssl``
defaults to **True**, so the session is FTPS and is never silently
downgraded to plaintext -- pass ``False`` only if you accept sending the
password in the clear. And a download **refuses to replace an existing
local file**: pass ``overwrite=True`` to opt in.
"""

import asyncio
import sys
from typing import Any, Dict

import aiohttp

from async_gateway.async_gateway import request

DEFAULT_HOST = '127.0.0.1'


async def download(host: str = DEFAULT_HOST) -> Dict[str, Any]:
    """Download one file from ``host`` over FTPS.

    Args:
        host: The FTP server's bare host name -- no scheme, since FTP
            takes the port from ``protocol_info`` instead.

    Returns:
        The response envelope, whatever the outcome.
    """
    result = await request(
        url=host,
        protocol='FTP',
        auth=aiohttp.BasicAuth('user', 'password'),
        protocol_info={
            'port': 21,
            'command': 'download',
            'server_path': '/pub/report.csv',   # SOURCE, on the server
            'client_path': '/tmp/report.csv',   # DESTINATION, here
            'verify_ssl': True,
            'timeout': 30,
        },
    )
    print(result['ok'], result['status_code'], result['protocol_details'])
    return result


if __name__ == '__main__':
    asyncio.run(download(*sys.argv[1:]))

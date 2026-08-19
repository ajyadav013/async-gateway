"""One SFTP download, with the SSH host key actually verified.

Run it against a local SSH server::

    python examples/sftp_example.py 127.0.0.1

Host-key verification is **on by default**: omitting ``known_hosts``
asks asyncssh for its own ``~/.ssh/known_hosts`` resolution rather than
trusting whatever key the server offers. An unknown host therefore fails
with ``error['code'] == 'HOST_KEY'`` instead of connecting. Turning it
off requires naming it -- ``insecure_skip_host_key_check=True`` -- and a
falsy ``verify_ssl`` does not do it for you.
"""

import asyncio
import sys
from types import SimpleNamespace
from typing import Any, Dict

from async_gateway.async_gateway import request

DEFAULT_HOST = '127.0.0.1'


async def download(host: str = DEFAULT_HOST) -> Dict[str, Any]:
    """Download one file from ``host`` over SFTP.

    Args:
        host: The SSH server's bare host name.

    Returns:
        The response envelope, whatever the outcome.
    """
    result = await request(
        url=host,
        protocol='SFTP',
        auth=SimpleNamespace(login='user', password='password'),
        protocol_info={
            'port': 22,
            'mode': 'get',                     # get, put or remove
            'remote_path': '/pub/report.csv',  # SOURCE, on the server
            'local_path': '/tmp/report.csv',   # DESTINATION, here
            'timeout': 30,
        },
    )
    print(result['ok'], result['status_code'], result['protocol_details'])
    return result


if __name__ == '__main__':
    asyncio.run(download(*sys.argv[1:]))

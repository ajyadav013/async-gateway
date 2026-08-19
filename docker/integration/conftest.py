"""Shared configuration for the live-server integration suite.

Every value here comes from the environment `docker-compose.yml` sets, so
the suite has no knowledge of the topology beyond "these names resolve".
"""

import os
from types import SimpleNamespace
from typing import Any

import pytest

pytest_plugins = ('pytest_asyncio',)


def env(name: str, default: str = '') -> str:
    """Read one configuration value from the environment.

    Args:
        name: The variable name compose sets.
        default: What to use when it is unset, for a local run.

    Returns:
        The value.
    """
    return os.environ.get(name, default)


HTTP_HOST = env('HTTP_HOST', '127.0.0.1')
HTTP_PORT = env('HTTP_PORT', '8080')
HTTPS_PORT = env('HTTPS_PORT', '8443')
OTHER_HOST = env('OTHER_HOST', '127.0.0.1')
OTHER_PORT = env('OTHER_PORT', '8081')

FTP_HOST = env('FTP_HOST', '127.0.0.1')
FTP_PORT = int(env('FTP_PORT', '21'))
FTP_USER = env('FTP_USER', 'gateway')
FTP_PASSWORD = env('FTP_PASSWORD', 'gateway-test-pw')

SFTP_HOST = env('SFTP_HOST', '127.0.0.1')
SFTP_PORT = int(env('SFTP_PORT', '22'))
SFTP_USER = env('SFTP_USER', 'gateway')
SFTP_PASSWORD = env('SFTP_PASSWORD', 'gateway-test-pw')

SOAP_HOST = env('SOAP_HOST', '127.0.0.1')
SOAP_PORT = env('SOAP_PORT', '8080')

WRONG_TLS_PORT = env('WRONG_TLS_PORT', '9443')

BASE_HTTP = f'http://{HTTP_HOST}:{HTTP_PORT}'
BASE_HTTPS = f'https://{HTTP_HOST}:{HTTPS_PORT}'
# Same host, a port whose certificate names somebody else.
BASE_WRONG_HOST = f'https://{HTTP_HOST}:{WRONG_TLS_PORT}'
BASE_OTHER = f'http://{OTHER_HOST}:{OTHER_PORT}'
BASE_SOAP = f'http://{SOAP_HOST}:{SOAP_PORT}'

KNOWN_HOSTS = '/hostkeys/known_hosts'
CA_BUNDLE = '/pki/ca.pem'

# A writable path on a real volume rather than the image's own overlayfs.
# The filesystem tests that assert mode, ownership and link counts run
# there as well as under `tmp_path`, because those are exactly the
# properties a volume driver may answer differently from the container's
# writable layer -- and differently again from APFS, which is all the
# host suite has ever seen. Empty when no volume is mounted, which those
# tests read as a reason to skip rather than to pass.
SCRATCH_DIR = env('SCRATCH_DIR', '')

# The fixture both file servers seed at start. Asserted byte for byte, so
# it is stated once here and read from the servers' entrypoints.
REMOTE_FIXTURE = b'pair,rate\nEURUSD,1.0842\nGBPUSD,1.2671\n'


@pytest.fixture()
def ftp_auth() -> Any:
    """Return the credential object the FTP client reads.

    Returns:
        An object carrying ``login`` and ``password``, which is the whole
        of what the FTP and SFTP clients require of ``auth``.
    """
    return SimpleNamespace(login=FTP_USER, password=FTP_PASSWORD)


@pytest.fixture()
def sftp_auth() -> Any:
    """Return the credential object the SFTP client reads.

    Returns:
        An object carrying ``login`` and ``password``.
    """
    return SimpleNamespace(login=SFTP_USER, password=SFTP_PASSWORD)

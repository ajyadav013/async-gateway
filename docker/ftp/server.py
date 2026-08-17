"""An implicit-FTPS server, for the FTP lane.

Why this and not vsftpd. The library's only TLS knob for FTP is
``aioftp.Client(ssl=...)``, which wraps the control connection in TLS
from the first byte -- *implicit* FTPS. vsftpd's `implicit_ssl` mode
brings the control channel up but then stalls the passive data channel
against this client, and the combination is not one a real deployment
uses either. Chasing that interop is a study of vsftpd, not of the
library under test.

``aioftp``'s own server speaks exactly the dialect its client speaks, so
what this proves is precise and honest: the library performs real FTPS
file transfers, over a real TLS socket, against a real server process,
with a certificate it verifies. What it deliberately does *not* claim is
interoperability with the whole FTPS ecosystem -- see the note in
``docker/ftp/vsftpd.conf.rejected`` and the README.

Users, passwords and the served directory are fixtures, seeded fresh on
every start so a stale file cannot make an upload assertion pass.
"""

import asyncio
import os
import pathlib
import ssl

import aioftp

HOME = pathlib.Path(os.environ.get('FTP_HOME', '/srv/ftp'))
USER = os.environ.get('FTP_USER', 'gateway')
PASSWORD = os.environ.get('FTP_PASSWORD', 'gateway-test-pw')
PORT = int(os.environ.get('FTP_PORT', '990'))
CERT = os.environ.get('TLS_CERT', '/pki/ftp-server.pem')
KEY = os.environ.get('TLS_KEY', '/pki/ftp-server-key.pem')

# The download fixture. Byte-for-byte what the tests assert, so it is
# defined in one place and the value is duplicated only in the
# integration suite's own constant, where a mismatch fails loudly.
REPORT = b'pair,rate\nEURUSD,1.0842\nGBPUSD,1.2671\n'


def seed() -> None:
    """Reset the served tree to a known state.

    Called at start rather than baked into the image: the upload tests
    assert that a file appeared, and a leftover from a previous run at
    the same path is exactly how that assertion passes without the
    transfer having happened.
    """
    pub = HOME / 'pub'
    incoming = HOME / 'incoming'
    pub.mkdir(parents=True, exist_ok=True)
    incoming.mkdir(parents=True, exist_ok=True)

    (pub / 'report.csv').write_bytes(REPORT)
    (pub / 'disposable.txt').write_bytes(b'delete me\n')

    for stale in incoming.iterdir():
        if stale.is_file():
            stale.unlink()


async def main() -> None:
    """Serve implicit FTPS until the container is stopped."""
    seed()

    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(CERT, KEY)

    server = aioftp.Server(
        [aioftp.User(USER, PASSWORD, home_path=HOME,
                     permissions=[aioftp.Permission(
                         '/', readable=True, writable=True)])],
        # Implicit FTPS: TLS from the first byte, which is what
        # `aioftp.Client(ssl=...)` -- and therefore this library --
        # speaks. A plaintext client reading this port gets a handshake
        # failure, which is the property the security lane asserts.
        ssl=context,
    )
    await server.start(host='0.0.0.0', port=PORT)      # noqa: S104
    print(f'ftps: listening on {PORT}, home {HOME}', flush=True)
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())

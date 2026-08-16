"""Doubles for the FTP transport seam, and a server that offers no TLS.

Three things S10's guards cannot be proven without.

The first is a *recording* stand-in for ``aioftp.Client.context``. The
fail-closed rule is a claim about the **value** the client hands
``aioftp`` -- ``ssl=`` is never ``None`` when verification is on, and the
connect and the transfer are both bounded -- and a stub that discards its
arguments cannot see any of it. The recorded call log is also what lets a
test assert that ``stat`` did **not** run after a ``remove``, which no
return value can carry.

The second is a loopback listener that answers with a plaintext FTP
greeting. "A server that does not offer TLS fails with ``TLS`` rather
than completing in plaintext" is a claim about a real handshake; a double
that never handshakes would pass whatever the client did with its
``ssl`` value, including passing ``None``.

The third is a throwaway certificate pair on disk. The certificate
branch of ``get_ssl_config`` is the half of M2 that a flag alone cannot
reach: ``load_cert_chain`` needs real files, so "a client certificate
does not buy an unverified peer" can only be measured against a chain
that actually loads. ``cryptography`` is used to mint it -- it is already
a hard transitive requirement of ``asyncssh``, so no test-only
dependency is introduced.

``tests/fixtures/protocol_transports.py`` keeps the five-protocol
contract row's own minimal FTP stub and is deliberately left alone: it is
S9's file and its shape is shared with four other protocols.
"""

import asyncio
import datetime
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Optional, Text

import aioftp

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import pytest

# One recorded call: the method name and the positional arguments it was
# given. The name is recorded rather than a count, because M3 is about
# *which* call ran -- a `stat` after a `remove` -- and not how many.
FTPCall = tuple[Text, tuple[Any, ...]]

# What `aioftp.Client.stat` reports for a file that is still there.
FILE_STATS: dict[Text, Text] = {'size': '12', 'type': 'file'}


class RecordingFTPClient:
    """The slice of ``aioftp.Client` an FTP operation touches, recorded.

    Only the three commands R15-AC6 parametrises and the ``stat`` that
    reads back what they left behind. A command the client invokes that
    is not here raises ``AttributeError`` rather than being quietly
    accepted, which is the same answer ``aioftp`` itself would give.
    """

    def __init__(
        self,
        *,
        stat_error: Optional[BaseException] = None,
        command_error: Optional[BaseException] = None,
    ) -> None:
        """Build a client double.

        Args:
            stat_error: Raised by :meth:`stat`. The default None models a
                path that is still there; an error models the real
                server's answer to a ``stat`` on a path a ``remove`` has
                just deleted, which is the failure M3 reported as a
                failed deletion.
            command_error: Raised by whichever command is invoked, so a
                test can drive a mid-transfer failure.
        """
        self.calls: list[FTPCall] = []
        self.stat_error = stat_error
        self.command_error = command_error

    async def _invoked(self, name: Text, args: tuple[Any, ...]) -> None:
        """Record one command invocation and fail if configured to.

        Args:
            name: The command the client called.
            args: The positional arguments it passed.

        Returns:
            None.

        Raises:
            BaseException: ``command_error``, when one was configured.
        """
        self.calls.append((name, args))
        if self.command_error is not None:
            raise self.command_error

    async def download(self, *args: Any, **kwargs: Any) -> None:
        """Accept a download and report success by not raising.

        Args:
            args: The remote and local paths.
            kwargs: Transfer options such as ``write_into``, unused.

        Returns:
            None, as ``aioftp`` does.
        """
        await self._invoked('download', args)

    async def upload(self, *args: Any, **kwargs: Any) -> None:
        """Accept an upload and report success by not raising.

        Args:
            args: The source and destination paths.
            kwargs: Transfer options such as ``write_into``, unused.

        Returns:
            None, as ``aioftp`` does.
        """
        await self._invoked('upload', args)

    async def remove(self, *args: Any, **kwargs: Any) -> None:
        """Accept a deletion and report success by not raising.

        Args:
            args: The path to remove.
            kwargs: Unused.

        Returns:
            None, as ``aioftp`` does.
        """
        await self._invoked('remove', args)

    async def remove_file(self, *args: Any, **kwargs: Any) -> None:
        """Accept a single-file deletion and report success by not raising.

        ``aioftp.Client`` exposes this alongside ``remove``, and it is
        reachable through the ``getattr`` lookup, so it removes the path
        it is given exactly as ``remove`` does.

        Args:
            args: The path to remove.
            kwargs: Unused.

        Returns:
            None, as ``aioftp`` does.
        """
        await self._invoked('remove_file', args)

    async def remove_directory(self, *args: Any, **kwargs: Any) -> None:
        """Accept a directory deletion and report success by not raising.

        Args:
            args: The path to remove.
            kwargs: Unused.

        Returns:
            None, as ``aioftp`` does.
        """
        await self._invoked('remove_directory', args)

    async def stat(self, path: Text) -> dict[Text, Text]:
        """Return the MLSx facts ``aioftp`` reports for ``path``.

        Args:
            path: The remote path being described.

        Returns:
            A mapping of facts, the shape ``aioftp.Client.stat`` returns.

        Raises:
            BaseException: ``stat_error``, when one was configured.
        """
        self.calls.append(('stat', (path,)))
        if self.stat_error is not None:
            raise self.stat_error
        return dict(FILE_STATS)


class RefusingContext:
    """An async context manager whose entry raises a given error.

    The shape a dead or angry server produces in ``aioftp``: building the
    context manager succeeds, and the connection attempt inside
    ``__aenter__`` is what fails.
    """

    def __init__(self, error: BaseException) -> None:
        """Build a context manager that refuses on entry.

        Args:
            error: The exception ``__aenter__`` raises.
        """
        self.error = error

    async def __aenter__(self) -> Any:
        """Refuse the connection.

        Returns:
            Never; this always raises.

        Raises:
            BaseException: The error this was built with.
        """
        raise self.error

    async def __aexit__(self, *exc_info: Any) -> bool:
        """Leave the context, suppressing nothing.

        Args:
            exc_info: The exception triple, unused.

        Returns:
            False, so any exception propagates.
        """
        return False


class FTPContextDouble:
    """A stand-in for ``aioftp.Client.context`` that records its call.

    Installed on the class, so the client's own
    ``aioftp.Client.context(host, port, user, password, ssl=...)`` reaches
    this object with every argument intact -- which is the point, because
    the TLS and timeout criteria are assertions about those arguments.
    """

    def __init__(
        self,
        *,
        client: Optional[RecordingFTPClient] = None,
        connect_error: Optional[BaseException] = None,
    ) -> None:
        """Build a transport double.

        Args:
            client: The client the session yields; a fresh recording one
                by default.
            connect_error: When given, the session refuses on entry with
                this error instead of yielding a client.
        """
        self.client = RecordingFTPClient() if client is None else client
        self.connect_error = connect_error
        self.calls: list[tuple[tuple[Any, ...], dict[Text, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Open, or refuse to open, an FTP session, recording the call.

        Args:
            args: Host, port and credentials.
            kwargs: TLS and timeout options.

        Returns:
            An async context manager yielding the client double, or one
            that refuses on entry.
        """
        self.calls.append((args, dict(kwargs)))
        if self.connect_error is not None:
            return RefusingContext(self.connect_error)
        return _entered(self.client)

    @property
    def kwargs(self) -> dict[Text, Any]:
        """The keyword arguments of the most recent call.

        Returns:
            The recorded mapping, so a test reads ``double.kwargs['ssl']``
            rather than indexing into the call log.

        Raises:
            AssertionError: If the seam was never reached. A test that
                asserts on the arguments of a call that never happened
                would otherwise pass vacuously.
        """
        assert self.calls, 'aioftp.Client.context was never called'
        return self.calls[-1][1]


@asynccontextmanager
async def _entered(client: Any) -> AsyncIterator[Any]:
    """Yield ``client`` from an async context manager that does nothing.

    Args:
        client: The object the ``async with`` block should bind.

    Yields:
        ``client``, unchanged.
    """
    yield client


def install_ftp_double(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: Optional[RecordingFTPClient] = None,
    connect_error: Optional[BaseException] = None,
) -> FTPContextDouble:
    """Replace ``aioftp.Client.context`` for the duration of one test.

    Args:
        monkeypatch: The pytest patcher, which undoes this on teardown.
        client: The client double the session yields.
        connect_error: When given, the session refuses on entry.

    Returns:
        The installed double, for the test to assert against.
    """
    double = FTPContextDouble(client=client, connect_error=connect_error)
    monkeypatch.setattr(aioftp.Client, 'context', double)
    return double


@contextmanager
def certificate_pair() -> Iterator[tuple[Text, Text]]:
    """Mint a throwaway client certificate and yield the two paths.

    Self-signed and short-lived: nothing verifies it, because the
    assertions it supports are about the ``SSLContext``
    ``load_cert_chain`` produces, not about a peer accepting the chain.

    Yields:
        The certificate path and the private-key path, both PEM, in the
        ``('cert', 'key')`` order ``get_ssl_config`` indexes them in. The
        containing directory is removed on exit.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, 'async-gateway-test')])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )

    with tempfile.TemporaryDirectory() as directory:
        certificate_path = Path(directory) / 'cert.pem'
        key_path = Path(directory) / 'key.pem'
        certificate_path.write_bytes(
            certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()))
        yield str(certificate_path), str(key_path)


@asynccontextmanager
async def plaintext_ftp_server() -> AsyncIterator[int]:
    """Serve a plaintext FTP greeting on loopback and yield its port.

    A TLS client that connects here reads ``220 ...`` where a ServerHello
    should be -- exactly what a real FTP server with no TLS support gives
    it -- so the handshake fails. Nothing beyond the greeting is
    implemented: a client that gets past the handshake has already failed
    the assertion this server exists for.

    Yields:
        The port the listener is bound to on 127.0.0.1.
    """
    async def greet(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Answer one connection with a plaintext FTP banner.

        Args:
            reader: The connection's reader, unused.
            writer: The connection's writer.

        Returns:
            None.
        """
        writer.write(b'220 plaintext ftp service ready\r\n')
        await writer.drain()

    server = await asyncio.start_server(greet, '127.0.0.1', 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()

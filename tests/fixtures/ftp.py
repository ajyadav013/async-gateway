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
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path, PurePosixPath
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

# Three payloads that are distinguishable on sight, because the defects
# they witness are all "the right operation moved the wrong bytes".
# AGW-33's killer assertion compares the first two, so they must never
# be equal.
LOCAL_CONTENT: bytes = b'the file the caller meant'
REMOTE_CONTENT: bytes = b'the old remote copy'
HARMLESS_CONTENT: bytes = b'an ordinary file from an ordinary server'
ESCAPED_CONTENT: bytes = b'written outside the directory you named'


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
        # The path layer `FTPContextDouble` builds from the factory the
        # code under test passed. Only `HostileFTPServer`, which runs
        # aioftp's real `download`, ever reads it.
        self.path_io: Any = None

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


class TransferringFTPClient(RecordingFTPClient):
    """A client that actually *moves bytes*, for the operand-order rows.

    AGW-33 is a transposition of two path-like positionals, and neither
    a recording double nor a signature-faithful one can see it: both
    operands bind cleanly either way round (proven in S12). Only a test
    that models a filesystem -- asserting which path was **read** and
    which was **written** -- can catch it.

    So ``upload`` here reads its source operand off the real disk, the
    way ``aioftp.Client.upload`` does, and keeps what it read as
    :attr:`uploaded`. A test then asserts on the *content* that reached
    the remote side, which is the only assertion that distinguishes the
    right file from a same-named wrong one.

    Attributes:
        uploaded: The bytes the server received, or None if no upload
            reached it.
        upload_destination: The remote path they were written to.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Build a transferring client double.

        Args:
            kwargs: Forwarded to :class:`RecordingFTPClient`.
        """
        super().__init__(**kwargs)
        self.uploaded: Optional[bytes] = None
        self.upload_destination: Optional[Text] = None

    async def upload(self, *args: Any, **kwargs: Any) -> None:
        """Read the source off disk and record what the server received.

        Args:
            args: ``(source, destination)`` in ``aioftp``'s order --
                for an upload, source is **local**.
            kwargs: Transfer options, unused.

        Returns:
            None, as ``aioftp`` does.

        Raises:
            FileNotFoundError: If the source operand names nothing on
                the local disk -- which is what a real ``upload`` does,
                and is the honest-failure half of AGW-33.
        """
        await self._invoked('upload', args)
        source, destination = args[0], args[1]
        self.uploaded = Path(source).read_bytes()
        self.upload_destination = str(destination)


class HostileFTPServer(RecordingFTPClient):
    """A server whose directory listing carries an escaping entry name.

    R22's Description says the remote server supplies the entry names on
    a recursive download, so this supplies them -- including one
    containing ``../``, which is exactly what it warns about.

    Only the **wire** is faked. The four coroutines replaced here are
    the ones that would talk to a socket; the recursion, the
    ``name.relative_to(source)`` arithmetic and every write stay
    ``aioftp.Client.download``'s own real code. That is what makes the
    escape this drives a real escape rather than a modelled one:
    measured against the unfixed library, the hostile entry landed
    outside the target directory at mode 0644.

    Attributes:
        listing: What ``list`` reports for the root, as
            ``(entry path, kind)`` pairs.
        contents: The bytes each file path yields.
    """

    #: The remote directory a test asks to download.
    ROOT: Text = '/pub/data'

    def __init__(
        self,
        listing: Sequence[tuple[Text, Text]],
        contents: Mapping[Text, bytes],
    ) -> None:
        """Build a server presenting one directory.

        Args:
            listing: The entries the server claims the root holds.
            contents: The bytes behind each file path.
        """
        super().__init__()
        self.listing = list(listing)
        self.contents = dict(contents)

    async def download(self, *args: Any, **kwargs: Any) -> None:
        """Run ``aioftp``'s **real** recursive download over this listing.

        The one method that must not be a recording stub. Everything
        R22's threat model is about happens inside
        ``aioftp.Client.download`` -- the recursion over the listing,
        the ``name.relative_to(source)`` arithmetic, the ``path_io``
        writes -- so a double that recorded the call and returned would
        assert nothing about where a hostile entry name lands.

        The real method is bound to *this* object, so its four wire
        coroutines resolve to the fakes above and everything else is
        aioftp's own code. :attr:`path_io` is whatever
        :class:`FTPContextDouble` read off the client's own
        ``path_io_factory`` keyword -- so if the code under test ever
        stopped passing one, the containment would vanish here exactly
        as it would in production.

        Args:
            args: ``(source, destination)`` -- remote first, for a
                download.
            kwargs: ``write_into`` and the block size.

        Returns:
            None, as ``aioftp`` does.
        """
        await self._invoked('download', args)
        await aioftp.Client.download(self, *args, **kwargs)

    @classmethod
    def harmless_only(cls) -> 'HostileFTPServer':
        """Return a server whose listing is entirely well-behaved.

        Returns:
            The control server, for asserting that containment does not
            break an ordinary transfer.
        """
        return cls(
            [(f'{cls.ROOT}/harmless.txt', 'file')],
            {f'{cls.ROOT}/harmless.txt': HARMLESS_CONTENT},
        )

    @classmethod
    def with_escaping_entry(cls, sibling: Text) -> 'HostileFTPServer':
        """Return a server that also lists an entry name containing ``../``.

        Args:
            sibling: The name of the directory beside the download
                target that the escape aims at.

        Returns:
            A server whose listing carries one harmless entry and one
            that walks out of the destination.
        """
        escaping = f'{cls.ROOT}/../{sibling}/OWNED'
        return cls(
            [
                (f'{cls.ROOT}/harmless.txt', 'file'),
                (escaping, 'file'),
            ],
            {
                f'{cls.ROOT}/harmless.txt': HARMLESS_CONTENT,
                escaping: ESCAPED_CONTENT,
            },
        )

    async def is_file(self, path: Any) -> bool:
        """Report whether the server calls a remote path a file.

        Args:
            path: The remote path.

        Returns:
            True when the server has contents for it.
        """
        return str(PurePosixPath(path)) in self.contents

    async def is_dir(self, path: Any) -> bool:
        """Report whether the server calls a remote path a directory.

        Args:
            path: The remote path.

        Returns:
            True for the root, which is the only directory served.
        """
        return str(PurePosixPath(path)) == self.ROOT

    async def list(self, path: Any, **kwargs: Any) -> list[Any]:
        """Return the entries the server claims a directory holds.

        Args:
            path: The directory listed.
            kwargs: ``aioftp``'s listing options, unused.

        Returns:
            ``(path, info)`` pairs in ``aioftp``'s own shape, entry
            names verbatim -- a hostile one included.
        """
        return [
            (PurePosixPath(name), {'type': kind})
            for name, kind in self.listing
        ]

    def download_stream(self, path: Any, **kwargs: Any) -> Any:
        """Open a byte stream over one remote file.

        Args:
            path: The remote file.
            kwargs: ``aioftp``'s stream options, unused.

        Returns:
            An async context manager yielding a block iterator.
        """
        return _byte_stream(self.contents[str(PurePosixPath(path))])


@asynccontextmanager
async def _byte_stream(data: bytes) -> AsyncIterator[Any]:
    """Yield ``data`` in the shape ``aioftp``'s download stream has.

    Args:
        data: The file's contents.

    Yields:
        An object with ``aioftp``'s ``iter_by_block``.
    """
    class Stream:
        """One remote file's bytes, block by block."""

        async def iter_by_block(self, size: int = 8192) -> AsyncIterator[
                bytes]:
            """Yield the file in blocks.

            Args:
                size: The block size, unused -- the payloads here are
                    small enough to arrive in one.

            Yields:
                The file's bytes.
            """
            yield data

    yield Stream()


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
        quit_error: Optional[BaseException] = None,
    ) -> None:
        """Build a transport double.

        Args:
            client: The client the session yields; a fresh recording one
                by default.
            connect_error: When given, the session refuses on entry with
                this error instead of yielding a client.
            quit_error: When given, the session's *exit* raises this --
                modelling ``aioftp``'s ``finally: await client.quit()``,
                which is what replaces a cancellation with a
                ``ConnectionResetError`` (NEW-R10-4).
        """
        self.client = RecordingFTPClient() if client is None else client
        self.connect_error = connect_error
        self.quit_error = quit_error
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
        # Build the client's path layer out of the factory the code
        # under test actually passed, and give it to the double. A
        # server that runs aioftp's real `download` needs a real
        # `path_io` to write through -- and taking it from the recorded
        # keyword rather than constructing one here is what makes the
        # containment claim a claim about the *library's* configuration:
        # if `ftp_client` stopped passing a factory, the fallback below
        # is aioftp's own uncontained default and the escape rows go
        # red, exactly as they would in production.
        factory = kwargs.get('path_io_factory', aioftp.pathio.PathIO)
        self.client.path_io = factory(timeout=kwargs.get('path_timeout'))
        return _entered(self.client, self.quit_error)

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
async def _entered(
    client: Any,
    quit_error: Optional[BaseException] = None,
) -> AsyncIterator[Any]:
    """Yield ``client``, optionally failing the way ``aioftp`` teardown does.

    The no-op exit is right for almost every row and **wrong** for the
    one that matters most, so the behaviour is a parameter.
    ``aioftp.Client.context.__aexit__`` runs ``await client.quit()``
    from a ``finally``, which sends QUIT on the control socket. When
    the block is unwinding because the task was cancelled that socket
    is already gone, so the QUIT raises ``ConnectionResetError`` -- and
    an exception raised while unwinding **replaces** the one in flight.
    That is the whole mechanism of NEW-R10-4, and a double whose exit
    does nothing cannot reproduce it: the ``CancelledError`` reaches
    the caller untouched and the row passes against the unfixed client.

    Args:
        client: The object the ``async with`` block should bind.
        quit_error: Raised from the exit path, as a failing ``quit()``
            would. None -- the default -- keeps every existing row's
            silent teardown.

    Yields:
        ``client``, unchanged.

    Raises:
        BaseException: ``quit_error``, on the way out, whether the
            block succeeded or is unwinding.
    """
    try:
        yield client
    finally:
        if quit_error is not None:
            raise quit_error


def install_ftp_double(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: Optional[RecordingFTPClient] = None,
    connect_error: Optional[BaseException] = None,
    quit_error: Optional[BaseException] = None,
) -> FTPContextDouble:
    """Replace ``aioftp.Client.context`` for the duration of one test.

    Args:
        monkeypatch: The pytest patcher, which undoes this on teardown.
        client: The client double the session yields.
        connect_error: When given, the session refuses on entry.
        quit_error: When given, the session's exit raises this, the way
            ``aioftp``'s own ``finally: await client.quit()`` does.

    Returns:
        The installed double, for the test to assert against.
    """
    double = FTPContextDouble(
        client=client, connect_error=connect_error, quit_error=quit_error)
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

    **The handler must close its own writer, and that is not tidiness.**
    On CPython 3.12 ``Server.wait_closed()`` waits for every *handler
    task and transport* the server ever accepted, so a handler that
    returns while its transport is still open makes the ``finally``
    below block forever -- the whole suite hangs on this one test, with
    an idle event loop and no traceback pointing anywhere near here.
    CPython 3.13 changed that (gh-104344) and macOS runs 3.14, which is
    why this was invisible on the development machine and reproduces
    100% of the time in a 3.12 container. 3.12 is in this project's
    committed CI matrix, so the fixture is simply wrong, not merely
    unlucky.

    Yields:
        The port the listener is bound to on 127.0.0.1.
    """
    async def greet(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Answer one connection with a plaintext FTP banner, then hang up.

        Args:
            reader: The connection's reader, unused.
            writer: The connection's writer.

        Returns:
            None.
        """
        writer.write(b'220 plaintext ftp service ready\r\n')
        await writer.drain()
        writer.close()
        # The peer is a TLS client that has already given up on a
        # plaintext banner, so it may have reset the connection before
        # this runs. That is the expected path here, not an error worth
        # propagating out of a fixture whose job is finished.
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()

    server = await asyncio.start_server(greet, '127.0.0.1', 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()

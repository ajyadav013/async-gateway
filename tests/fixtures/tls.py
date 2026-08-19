"""A throwaway PKI and a loopback TLS server that demands client auth.

FI-2 makes R23's live-handshake test non-optional: the H28 fix makes
client certificates connect *for the first time ever*, so the path would
otherwise ship untested. A double cannot discharge that -- the claim is
about what OpenSSL does with the context ``get_ssl_config`` builds, and a
stub that never handshakes would pass whatever the library handed it,
including the ``PROTOCOL_TLS_SERVER`` context that was the defect.

So this module mints a real two-level PKI in a temporary directory and
runs a real ``aiohttp.web`` server over TLS on 127.0.0.1 with
``verify_mode=CERT_REQUIRED``. Nothing here reaches a host that is not
loopback, and the whole PKI is deleted when the fixture exits.

Three things about the certificates are load-bearing and each was found
by a handshake failing, so none of them is boilerplate:

* **``KeyUsage`` on the CA.** Without ``key_cert_sign``, OpenSSL rejects
  the chain with ``CA cert does not include key usage extension`` -- and
  it does so for the *good* client too, so every row of the test passes
  for the wrong reason.
* **``AuthorityKeyIdentifier`` on the leaves.** Without it, verification
  fails with ``Missing Authority Key Identifier``, again for every row.
* **``maximum_version = TLSv1_2`` on the server.** This is the only one
  that is about *observability* rather than validity. Under TLS 1.3 the
  client certificate is sent after the server has already finished its
  half of the handshake, so a rejected client cert surfaces at the
  ``aiohttp`` layer as ``ServerDisconnectedError`` -- indistinguishable
  from a crashed server, and not a ``TLS`` error at all. Under TLS 1.2
  the client-certificate check happens inside the handshake, so a
  rejection is an ``ssl.SSLError`` the library classifies as ``TLS``.
  Measured 15/15 deterministic on this machine, where the TLS-1.3 shape
  was 15/15 the *wrong* one. The cap is a property of the test server,
  never of the library.

The server runs on a background thread with blocking sockets rather than
through ``asyncio.start_server``. Under asyncio the rejection arrives as
a bare ``ConnectionResetError`` -- the loop tears the transport down
before the alert is read -- which the library classifies as ``CONNECT``,
not ``TLS``. A synchronous accept loop lets the alert reach the client,
which is what makes the ``TLS`` classification assertable at all.

``cryptography`` mints the PKI. It is a declared dev dependency (S17
added it): it was already present transitively through ``asyncssh``, but
a direct import must be declared directly or the suite breaks the day
``asyncssh`` drops it.
"""

import datetime
import ipaddress
import socket
import ssl
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Text, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateKey)
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

#: What the loopback server answers a successfully authenticated client
#: with. A complete HTTP/1.1 response, written by hand because the server
#: is a raw socket rather than a framework.
CANNED_RESPONSE: bytes = (
    b'HTTP/1.1 200 OK\r\n'
    b'Content-Length: 2\r\n'
    b'Connection: close\r\n'
    b'\r\n'
    b'ok'
)

#: Signing authority for the CA certificate: it signs certificates and
#: CRLs. Omitting ``key_cert_sign`` makes OpenSSL refuse the chain with
#: ``CA cert does not include key usage extension``.
_CA_KEY_USAGE = x509.KeyUsage(
    digital_signature=True,
    content_commitment=False,
    key_encipherment=False,
    data_encipherment=False,
    key_agreement=False,
    key_cert_sign=True,
    crl_sign=True,
    encipher_only=False,
    decipher_only=False,
)

#: What an end-entity certificate is allowed to do: participate in a
#: handshake, and nothing to do with signing other certificates.
_LEAF_KEY_USAGE = x509.KeyUsage(
    digital_signature=True,
    content_commitment=False,
    key_encipherment=True,
    data_encipherment=False,
    key_agreement=False,
    key_cert_sign=False,
    crl_sign=False,
    encipher_only=False,
    decipher_only=False,
)


@dataclass(frozen=True)
class CertificateFiles:
    """One identity's certificate and private key, on disk as PEM.

    Attributes:
        certificate: Path to the PEM certificate.
        key: Path to the PEM private key, unencrypted.
    """

    certificate: Text
    key: Text

    def as_pair(self) -> Tuple[Text, Text]:
        """Return the paths in the order ``get_ssl_config`` expects.

        Returns:
            ``(certificate path, key path)`` -- the shape a caller puts
            in ``protocol_info['certificate']``.
        """
        return self.certificate, self.key


@dataclass(frozen=True)
class TlsWorld:
    """A complete throwaway PKI, written to one temporary directory.

    Attributes:
        ca_file: Path to the PEM holding the trusted CA certificate. A
            client that loads this verifies the loopback server; one
            that does not sees an untrusted peer, which is how the
            "verification is genuinely on" assertions are made.
        server: The server's identity, valid for ``127.0.0.1``.
        client: A client identity the server accepts.
        rogue: A client identity signed by a *different* CA, so the
            server rejects it at the handshake.
        expired: A client identity signed by the trusted CA whose
            validity window has already closed.
        mismatched: A certificate and a key that belong to two different
            key pairs. Never reaches a socket -- it exists so
            ``load_cert_chain`` itself can be made to fail.
        encrypted_key: The client's certificate paired with a
            passphrase-protected copy of its key.
        garbage: A file that is not a PEM at all, paired with a real key.
    """

    ca_file: Text
    server: CertificateFiles
    client: CertificateFiles
    rogue: CertificateFiles
    expired: CertificateFiles
    mismatched: CertificateFiles
    encrypted_key: CertificateFiles
    garbage: CertificateFiles


def _certificate_pem(certificate: x509.Certificate) -> bytes:
    """Render a certificate as PEM bytes.

    Args:
        certificate: The certificate to encode.

    Returns:
        Its PEM encoding.
    """
    return certificate.public_bytes(serialization.Encoding.PEM)


def _key_pem(
    key: EllipticCurvePrivateKey,
    passphrase: Optional[bytes] = None,
) -> bytes:
    """Render a private key as PKCS#8 PEM bytes.

    Args:
        key: The key to encode.
        passphrase: Encrypt the key with this passphrase, or None to
            write it unencrypted. The encrypted form exists only so the
            "passphrase-protected keys are unsupported" path has a real
            file to fail on.

    Returns:
        The key's PEM encoding.
    """
    encryption: serialization.KeySerializationEncryption = (
        serialization.NoEncryption() if passphrase is None
        else serialization.BestAvailableEncryption(passphrase))
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption)


def _mint_ca(
    common_name: Text,
    now: datetime.datetime,
) -> Tuple[EllipticCurvePrivateKey, x509.Certificate]:
    """Mint a self-signed certificate authority.

    Args:
        common_name: The CA's subject and issuer common name.
        now: The instant its validity window is centred on.

    Returns:
        The CA's private key and its certificate.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_CA_KEY_USAGE, critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _mint_leaf(
    common_name: Text,
    ca_key: EllipticCurvePrivateKey,
    ca_certificate: x509.Certificate,
    usage: x509.ObjectIdentifier,
    now: datetime.datetime,
    *,
    subject_alternative_name: Optional[x509.SubjectAlternativeName] = None,
    not_valid_after: Optional[datetime.datetime] = None,
) -> Tuple[EllipticCurvePrivateKey, x509.Certificate]:
    """Mint an end-entity certificate signed by ``ca_key``.

    Args:
        common_name: The subject common name.
        ca_key: The issuing CA's private key.
        ca_certificate: The issuing CA's certificate, read for its
            subject and its public key.
        usage: ``SERVER_AUTH`` or ``CLIENT_AUTH`` -- which side of a
            handshake this identity may take.
        now: The instant the validity window is centred on.
        subject_alternative_name: The SAN extension, required for a
            server certificate because host-name verification reads it
            and ignores the common name.
        not_valid_after: Override the expiry, so an already-expired
            certificate can be minted.

    Returns:
        The identity's private key and its certificate.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=2))
        .not_valid_after(
            not_valid_after or now + datetime.timedelta(days=1))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_LEAF_KEY_USAGE, critical=True)
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()),
            critical=False)
    )
    if subject_alternative_name is not None:
        builder = builder.add_extension(
            subject_alternative_name, critical=False)
    return key, builder.sign(ca_key, hashes.SHA256())


@contextmanager
def tls_world() -> Iterator[TlsWorld]:
    """Mint the whole throwaway PKI and yield the paths to it.

    Everything is written under one :func:`tempfile.TemporaryDirectory`
    and removed on exit, so no key material outlives the test that used
    it.

    Yields:
        The :class:`TlsWorld` describing every identity minted.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, ca_certificate = _mint_ca('asyncio-gateway-test-ca', now)
    rogue_ca_key, rogue_ca_certificate = _mint_ca(
        'asyncio-gateway-rogue-ca', now)

    server_key, server_certificate = _mint_leaf(
        'asyncio-gateway-test-server', ca_key, ca_certificate,
        ExtendedKeyUsageOID.SERVER_AUTH, now,
        subject_alternative_name=x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]))
    client_key, client_certificate = _mint_leaf(
        'asyncio-gateway-test-client', ca_key, ca_certificate,
        ExtendedKeyUsageOID.CLIENT_AUTH, now)
    rogue_key, rogue_certificate = _mint_leaf(
        'asyncio-gateway-rogue-client', rogue_ca_key, rogue_ca_certificate,
        ExtendedKeyUsageOID.CLIENT_AUTH, now)
    expired_key, expired_certificate = _mint_leaf(
        'asyncio-gateway-expired-client', ca_key, ca_certificate,
        ExtendedKeyUsageOID.CLIENT_AUTH, now,
        not_valid_after=now - datetime.timedelta(days=1))
    other_key, _ = _mint_leaf(
        'asyncio-gateway-other-client', ca_key, ca_certificate,
        ExtendedKeyUsageOID.CLIENT_AUTH, now)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        def write(name: Text, payload: bytes) -> Text:
            """Write one PEM file and return its path.

            Args:
                name: The file name inside the temporary directory.
                payload: The bytes to write.

            Returns:
                The absolute path, as ``str``.
            """
            path = root / name
            path.write_bytes(payload)
            return str(path)

        client_cert_path = write(
            'client.pem', _certificate_pem(client_certificate))
        garbage_path = write('garbage.pem', b'this is not a certificate\n')

        yield TlsWorld(
            ca_file=write('ca.pem', _certificate_pem(ca_certificate)),
            server=CertificateFiles(
                certificate=write(
                    'server.pem', _certificate_pem(server_certificate)),
                key=write('server.key', _key_pem(server_key))),
            client=CertificateFiles(
                certificate=client_cert_path,
                key=write('client.key', _key_pem(client_key))),
            rogue=CertificateFiles(
                certificate=write(
                    'rogue.pem', _certificate_pem(rogue_certificate)),
                key=write('rogue.key', _key_pem(rogue_key))),
            expired=CertificateFiles(
                certificate=write(
                    'expired.pem', _certificate_pem(expired_certificate)),
                key=write('expired.key', _key_pem(expired_key))),
            mismatched=CertificateFiles(
                certificate=client_cert_path,
                key=write('other.key', _key_pem(other_key))),
            encrypted_key=CertificateFiles(
                certificate=client_cert_path,
                key=write(
                    'client-encrypted.key',
                    _key_pem(client_key, passphrase=b'correct horse'))),
            garbage=CertificateFiles(
                certificate=garbage_path,
                key=write('garbage.key', _key_pem(client_key))),
        )


class MutualTlsServer:
    """A loopback HTTPS listener that demands a trusted client cert.

    Blocking sockets on a background thread, deliberately -- see this
    module's docstring for why an asyncio server cannot be used: it
    reports a rejected client certificate as a bare
    ``ConnectionResetError``, which classifies as ``CONNECT`` and would
    make the ``TLS`` assertion untestable.

    Every accepted connection is answered with :data:`CANNED_RESPONSE`
    and closed. There is no routing and no request parsing: the assertion
    this server exists for is that the handshake completed, and a
    response body is only how that is observed from the client side.
    """

    def __init__(self, world: TlsWorld) -> None:
        """Bind the listener and start accepting in the background.

        Args:
            world: The PKI to serve from and to verify clients against.
        """
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(world.server.certificate, world.server.key)
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=world.ca_file)
        # See the module docstring: TLS 1.3 defers the client-certificate
        # check past the handshake, where its rejection is no longer an
        # `SSLError` at the client.
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        self._context = context

        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(('127.0.0.1', 0))
        self._listener.listen(8)
        self.port: int = int(self._listener.getsockname()[1])

        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    @property
    def url(self) -> Text:
        """Return the absolute URL of this server's only endpoint.

        Returns:
            ``https://127.0.0.1:<port>/``.
        """
        return f'https://127.0.0.1:{self.port}/'

    def _accept_loop(self) -> None:
        """Accept connections until :meth:`close` stops the server.

        Returns:
            None.
        """
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            worker = threading.Thread(
                target=self._serve_one, args=(connection,), daemon=True)
            worker.start()

    def _serve_one(self, connection: socket.socket) -> None:
        """Handshake one connection and answer it, or drop it.

        A failed handshake is swallowed rather than logged: it is the
        *expected* outcome for the rogue and expired rows, and the
        assertion is made on the client side, where the alert arrives.

        Args:
            connection: The freshly accepted socket.

        Returns:
            None.
        """
        try:
            secured = self._context.wrap_socket(connection, server_side=True)
        except OSError:
            connection.close()
            return
        try:
            secured.recv(65536)
            secured.sendall(CANNED_RESPONSE)
        except OSError:
            pass
        finally:
            secured.close()

    def close(self) -> None:
        """Stop accepting and release the listening socket.

        Returns:
            None.
        """
        self._stopping.set()
        self._listener.close()
        self._thread.join(timeout=5)


@contextmanager
def mutual_tls_server(world: TlsWorld) -> Iterator[MutualTlsServer]:
    """Run a :class:`MutualTlsServer` for the duration of a block.

    Args:
        world: The PKI the server serves from.

    Yields:
        The running server.
    """
    server = MutualTlsServer(world)
    try:
        yield server
    finally:
        server.close()

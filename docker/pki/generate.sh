#!/bin/sh
# Mint a throwaway CA and the three server certificates the stack needs.
#
# Self-signed, generated fresh on every run, and never committed: these
# keys exist for the lifetime of a `docker compose up` and are worthless
# outside it. Committing a private key -- even a test one -- trains people
# to ignore the scanner that finds it, so the stack generates its own.
#
# Why a real CA rather than three self-signed leaves: the library's TLS
# path is the thing under test, and it verifies the *chain* and the *host
# name*. A self-signed leaf could only be accepted by disabling the
# verification this exists to prove, which would test nothing. So the
# client trusts exactly one CA (via SSL_CERT_FILE), each server presents a
# leaf signed by it, and a certificate for the wrong host still fails --
# which is what `test_security.py` asserts.
#
# The SANs are the compose service names, because that is the authority
# the client dials on the compose network.
set -eu

PKI=${PKI_DIR:-/pki}
DAYS=${PKI_DAYS:-2}

if [ -f "$PKI/ca.pem" ]; then
    echo "pki: already present at $PKI, leaving it alone"
    exit 0
fi

mkdir -p "$PKI"
cd "$PKI"

echo 'pki: minting CA'
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days "$DAYS" \
    -keyout ca-key.pem -out ca.pem \
    -subj '/CN=asyncio-gateway integration test CA' \
    -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' 2>/dev/null

# One leaf per server, each named for the compose service it belongs to.
# `wrong-host` is the odd one out: a perfectly valid certificate from the
# same trusted CA, issued for a name nothing serves. It is what proves the
# client checks the *host name* and not merely the chain -- a client that
# only validated the signature would accept it.
for host in http-server ftp-server soap-server wrong-host; do
    echo "pki: minting leaf for $host"
    openssl req -newkey rsa:2048 -nodes -sha256 \
        -keyout "$host-key.pem" -out "$host.csr" \
        -subj "/CN=$host" 2>/dev/null
    openssl x509 -req -in "$host.csr" -CA ca.pem -CAkey ca-key.pem \
        -CAcreateserial -days "$DAYS" -sha256 \
        -extfile /dev/stdin -out "$host.pem" <<EOF 2>/dev/null
subjectAltName = DNS:$host, DNS:localhost, IP:127.0.0.1
extendedKeyUsage = serverAuth
EOF
    rm -f "$host.csr"
    # vsftpd wants the key and the certificate in one file.
    cat "$host-key.pem" "$host.pem" > "$host-bundle.pem"
done

# The servers run as their own users; the client only ever reads ca.pem.
chmod 0644 "$PKI"/*.pem
echo 'pki: done'
ls -1 "$PKI"

#!/bin/sh
# Generate the host key, publish its public half, seed fixtures, run sshd.
#
# The published public key is the whole reason this is a script and not a
# plain `CMD sshd`. The client's default is to REFUSE an unknown host key,
# and proving that needs the negative and the positive: a connect that
# fails `HOST_KEY` with nothing pinned, and the same connect succeeding
# against a `known_hosts` built from this exact key. The harness reads it
# from the shared volume at /hostkeys.
set -eu

KEY_DIR=/etc/ssh/keys
PUB_DIR=${HOSTKEY_PUBLISH_DIR:-/hostkeys}
HOME_DIR=/home/gateway
OWNER=${SFTP_USER:-gateway}

mkdir -p "$KEY_DIR"
if [ ! -f "$KEY_DIR/ssh_host_ed25519_key" ]; then
    ssh-keygen -t ed25519 -N '' -f "$KEY_DIR/ssh_host_ed25519_key" >/dev/null
fi
chmod 600 "$KEY_DIR/ssh_host_ed25519_key"

# Publish in `known_hosts` form, keyed by every authority the client may
# dial on: the compose service name, and the same with the port, which is
# the form asyncssh looks up for a non-default port.
mkdir -p "$PUB_DIR"
KEY_TYPE=$(cut -d' ' -f1 < "$KEY_DIR/ssh_host_ed25519_key.pub")
KEY_DATA=$(cut -d' ' -f2 < "$KEY_DIR/ssh_host_ed25519_key.pub")
{
    echo "sftp-server $KEY_TYPE $KEY_DATA"
    echo "[sftp-server]:22 $KEY_TYPE $KEY_DATA"
    echo "localhost $KEY_TYPE $KEY_DATA"
    echo "127.0.0.1 $KEY_TYPE $KEY_DATA"
} > "$PUB_DIR/known_hosts"
cp "$KEY_DIR/ssh_host_ed25519_key.pub" "$PUB_DIR/host_ed25519.pub"
chmod 0644 "$PUB_DIR/known_hosts" "$PUB_DIR/host_ed25519.pub"

# Fixtures. Seeded at start, for the same reason as the FTP lane: a stale
# file at the upload destination is how an operand-order assertion passes
# without the transfer having happened.
mkdir -p "$HOME_DIR/pub" "$HOME_DIR/incoming" "$HOME_DIR/.ssh"
printf 'pair,rate\nEURUSD,1.0842\nGBPUSD,1.2671\n' > "$HOME_DIR/pub/report.csv"
printf 'delete me\n' > "$HOME_DIR/pub/disposable.txt"
rm -f "$HOME_DIR/incoming"/*

chown -R "$OWNER:$OWNER" "$HOME_DIR"
chmod 700 "$HOME_DIR/.ssh"

echo 'sftp: host key published, fixtures seeded' >&2
exec /usr/sbin/sshd -D -e

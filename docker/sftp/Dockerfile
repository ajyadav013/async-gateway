# An OpenSSH server restricted to SFTP, for the SFTP lane.
#
# Built rather than pulled for one reason above all: the host key must be
# **predictable**, because the security assertion is that the client
# refuses an *unknown* host key by default. Proving that needs both
# halves -- a connection that fails because the key is unknown, and the
# same connection succeeding once the key is pinned -- so the test needs
# to read the server's public key. A generated-at-first-boot key in an
# opaque image gives you the first half only.
#
# So the key is generated at container start into a known path on a shared
# volume, and the test harness reads it from there to build the
# `known_hosts` it pins with.
FROM alpine:3.20

RUN apk add --no-cache openssh openssh-sftp-server

ARG SFTP_USER=gateway
ARG SFTP_PASSWORD=gateway-test-pw
RUN adduser -D -h /home/gateway "$SFTP_USER" \
 && echo "$SFTP_USER:$SFTP_PASSWORD" | chpasswd

COPY docker/sftp/sshd_config /etc/ssh/sshd_config
COPY docker/sftp/entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/entrypoint

EXPOSE 22

ENTRYPOINT ["/usr/local/bin/entrypoint"]

# Mints the throwaway CA and server leaves the stack's TLS rests on.
#
# A tiny image of its own rather than `alpine` plus a command, because
# bare alpine ships no `openssl` binary -- the first run of this stack
# exited 127 with the CA half-written, which is a confusing way to
# discover a missing package.
FROM alpine:3.20

RUN apk add --no-cache openssl

COPY docker/pki/generate.sh /usr/local/bin/generate-pki
RUN chmod +x /usr/local/bin/generate-pki

ENV PKI_DIR=/pki

ENTRYPOINT ["/usr/local/bin/generate-pki"]

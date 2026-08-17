# The client side of the integration stack.
#
# Installs the built wheel -- the same artifact `Dockerfile` produces --
# and runs `docker/integration/test_integration.py` against the live
# protocol servers compose brings up. Nothing here mocks anything: the
# suite in this image is the one that cannot be written against doubles.
FROM python:3.12-slim AS build

WORKDIR /src
RUN python -m pip install --no-cache-dir --upgrade pip build
COPY pyproject.toml MANIFEST.in README.md LICENSE CHANGELOG.md ./
COPY async_gateway/ ./async_gateway/
RUN python -m build --wheel --outdir /dist

FROM python:3.12-slim AS runtime

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# `openssl` for the PKI the stack mints, and `lftp` as an INDEPENDENT
# check on the FTP server's TLS posture -- when the assertion is "this
# server refuses plaintext", proving it with the library under test would
# be circular.
# `openssh-client` supplies ssh-keygen for the wrong-key test, which
# mints a second host key to pin -- ruling out "any known_hosts file
# satisfies the check".
RUN apt-get update \
 && apt-get install --no-install-recommends -y openssl lftp openssh-client \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /dist/ /dist/
RUN python -m pip install --no-cache-dir "$(ls /dist/*.whl)" pytest pytest-asyncio

COPY docker/integration/ ./integration/
COPY docker/pki/generate.sh /usr/local/bin/generate-pki
RUN chmod +x /usr/local/bin/generate-pki

# The library verifies TLS against the system trust store, so the
# throwaway CA is pointed at through the standard variables rather than
# by weakening any verification in the tests.
ENV SSL_CERT_FILE=/pki/ca.pem \
    REQUESTS_CA_BUNDLE=/pki/ca.pem

# `openssh-client` for the wrong-key test, which mints a second host key
# to pin so that "any known_hosts satisfies the check" is ruled out.
WORKDIR /app/integration
ENTRYPOINT ["pytest", "-p", "no:cacheprovider"]
CMD ["test_integration.py"]

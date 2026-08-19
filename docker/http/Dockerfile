# The HTTP/HTTPS peer for the integration stack. aiohttp only, no wheel:
# this container is the *server*, and installing the library under test
# into it would blur which side a failure came from.
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN python -m pip install --no-cache-dir "aiohttp>=3.14,<4"

COPY docker/http/server.py /app/server.py

RUN useradd --create-home --uid 10002 httpd
USER httpd

EXPOSE 8080 8443
CMD ["python", "/app/server.py"]

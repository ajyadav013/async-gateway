# The SOAP peer. No standard SOAP container image exists, so this is a
# small aiohttp app that answers SOAP 1.1 and 1.2 and, on demand, a Fault
# in each version's own shape. See docker/soap/server.py for what it
# implements and what it deliberately does not.
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN python -m pip install --no-cache-dir "aiohttp>=3.14,<4"

COPY docker/soap/server.py /app/server.py

RUN useradd --create-home --uid 10003 soapd
USER soapd

EXPOSE 8080
CMD ["python", "/app/server.py"]

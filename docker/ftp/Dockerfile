# The FTPS peer for the integration stack.
#
# `aioftp`'s own server rather than vsftpd. The library's only TLS knob
# for FTP produces *implicit* FTPS, and vsftpd's implicit mode brings the
# control channel up but then stalls the passive data channel against
# this client -- see the header of `docker/ftp/server.py`, and
# `vsftpd.conf.rejected` for the configuration that got that far.
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN python -m pip install --no-cache-dir "aioftp>=0.28,<1"

COPY docker/ftp/server.py /app/server.py

RUN useradd --create-home --uid 10004 ftpd \
 && mkdir -p /srv/ftp \
 && chown -R ftpd:ftpd /srv/ftp
USER ftpd

EXPOSE 990
CMD ["python", "/app/server.py"]

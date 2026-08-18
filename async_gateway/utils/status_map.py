"""The one table mapping a wire-stable error code to a status code.

There is no protocol-neutral status concept, so the library states one
explicitly here rather than inventing a sentinel: every failure gets a
status a consumer already knows how to read, and the fabricated
three-digit code this replaces exists nowhere in the package.

``utils/exceptions.py`` reads each class's default status from this table,
so an exception class declares only its ``code`` and the numbers live in
exactly one place. Codes are wire-stable -- the human-readable message may
change freely between releases, the code may not -- which is what lets a
consumer branch on ``error['code']`` instead of on message text.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Optional

DEFAULT_STATUS: Final[int] = 502

STATUS_BY_CODE: Final[Mapping[str, int]] = MappingProxyType({
    # The base class, and the two intermediate bases, are reachable only if
    # something raises them directly; they map to the same 502 a bad gateway
    # would report.
    'GATEWAY': 502,
    'TRANSPORT': 502,
    'PROTOCOL': 502,

    'CONFIG': 400,
    # The caller's own callback raised. 500 rather than 400: the
    # configuration was accepted, so this is not a request the caller
    # malformed -- it is code inside their own function failing while this
    # library ran it.
    'PROCESSOR': 500,
    # 502 is the response side (a body that will not parse). The request
    # side -- a body that will not serialise -- is the caller's mistake and
    # passes 400 explicitly.
    'SERIALIZATION': 502,
    'XML_UNSAFE': 502,
    'PATH': 400,
    'CONNECT': 502,
    'DNS': 502,
    'TLS': 502,
    # Library-assigned and documented: no registered status describes an SSH
    # host-key mismatch, and 495 (a client-certificate rejection) is the
    # nearest honest neighbour.
    'HOST_KEY': 495,
    'TIMEOUT': 504,
    'RESPONSE_TOO_LARGE': 502,
    'RESPONSE_TOO_DEEP': 502,
    'CIRCUIT_OPEN': 503,
    'STACK_EXHAUSTED': 502,

    # A protocol error always carries the real remote status, so these
    # defaults apply only when the remote side supplied none.
    'HTTP_STATUS': 502,
    'FTP_STATUS': 500,
    'SFTP_STATUS': 500,
    'SOAP_FAULT': 502,
})

# Codes whose failure is the remote side's answer rather than this
# library's problem, and which the caller may legitimately expect. They log
# at `warning`; everything else logs at `error`.
WARNING_CODES: Final[frozenset[str]] = frozenset({
    'HTTP_STATUS',
    'FTP_STATUS',
    'SFTP_STATUS',
    'SOAP_FAULT',
    'CIRCUIT_OPEN',
})


def status_for(code: str, *, override: Optional[int] = None) -> int:
    """Return the status code a failure reports.

    Args:
        code: The wire-stable error code, e.g. ``'TIMEOUT'``.
        override: The status the remote side actually supplied -- an HTTP
            status, an FTP reply code -- or None to take the default.

    Returns:
        ``override`` when one was given, else the code's entry in
        :data:`STATUS_BY_CODE`, else :data:`DEFAULT_STATUS`.
    """
    if override is not None:
        return override
    return STATUS_BY_CODE.get(code, DEFAULT_STATUS)

"""The protocol registry: one normalised name, one strategy class.

``request()`` resolves a caller's ``protocol`` against this mapping and
constructs whatever it finds. The mapping is typed, which is the whole
point of it being declared here rather than inline: an entry that is not a
:class:`BaseRequestClass` subclass -- the ``None`` this file used to map
SOAP to, which reached callers as ``TypeError: 'NoneType' object is not
callable`` -- is now a type error rather than a runtime one.

``'SOAP'`` is here now. It was absent for exactly as long as there was
nothing to map it to -- an absent key is an unknown protocol, which the
entry point rejects with a ``ConfigurationError`` naming what it does
support, and that was a better answer than an entry dispatching nothing.
Story S22 wrote ``logic/soap_client.py``, so the key names a real class
and the library finally serves the protocol its own title has always
advertised (H3).
"""

from typing import Final

from async_gateway.helpers.internal.base import BaseRequestClass
from async_gateway.logic.ftp_client import FTPRequest
from async_gateway.logic.http_client import HttpRequest
from async_gateway.logic.sftp_client import SFTPRequest
from async_gateway.logic.soap_client import SoapRequest

protocol_mapping: Final[dict[str, type[BaseRequestClass]]] = {
    'HTTP': HttpRequest,
    'HTTPS': HttpRequest,
    'FTP': FTPRequest,
    'SFTP': SFTPRequest,
    'SOAP': SoapRequest,
}

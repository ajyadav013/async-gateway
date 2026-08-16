"""The protocol registry: one normalised name, one strategy class.

``request()`` resolves a caller's ``protocol`` against this mapping and
constructs whatever it finds. The mapping is typed, which is the whole
point of it being declared here rather than inline: an entry that is not a
:class:`BaseRequestClass` subclass -- the ``None`` this file used to map
SOAP to, which reached callers as ``TypeError: 'NoneType' object is not
callable`` -- is now a type error rather than a runtime one.

``'SOAP'`` is deliberately absent until ``logic/soap_client.py`` exists. An
absent key is an unknown protocol, which the entry point rejects with a
``ConfigurationError`` naming what it does support.
"""

from typing import Final

from async_gateway.helpers.internal.base import BaseRequestClass
from async_gateway.logic.ftp_client import FTPRequest
from async_gateway.logic.http_client import HttpRequest
from async_gateway.logic.sftp_client import SFTPRequest

protocol_mapping: Final[dict[str, type[BaseRequestClass]]] = {
    'HTTP': HttpRequest,
    'HTTPS': HttpRequest,
    'FTP': FTPRequest,
    'SFTP': SFTPRequest,
}

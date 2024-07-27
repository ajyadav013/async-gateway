"""Logic."""

from async_gateway.logic.ftp import FTPRequest
from async_gateway.logic.http import HttpRequest
from async_gateway.logic.sftp import SFTPRequest

protocol_mapping = {
    'HTTP': HttpRequest,
    'HTTPS': HttpRequest,
    'SOAP': None,
    'FTP': FTPRequest,
    'SFTP': SFTPRequest
}

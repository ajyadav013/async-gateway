"""Logic."""

from async_gateway.logic.ftp_client import FTPRequest
from async_gateway.logic.http_client import HttpRequest
from async_gateway.logic.sftp_client import SFTPRequest

protocol_mapping = {
    'HTTP': HttpRequest,
    'HTTPS': HttpRequest,
    'SOAP': None,
    'FTP': FTPRequest,
    'SFTP': SFTPRequest
}

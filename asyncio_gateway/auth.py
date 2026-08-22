"""Typed authentication models exposed by asyncio-gateway.

FTP retains its legacy ``.login``/``.password`` contract. SFTP needs a
separate model because SSH can authenticate with a password, an explicit
client key, or both, and because ssh-agent use must be an explicit choice.
"""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class SFTPAuth:
    """Describe credentials for one SFTP connection.

    Args:
        username: Non-empty SSH user name.
        password: Optional password. An empty string remains a deliberate
            password for compatibility with legacy authentication objects.
        client_keys: Explicit private keys in any form accepted by asyncssh.
        key_passphrase: Optional passphrase for encrypted client keys.
        use_ssh_agent: Whether asyncssh may consult ``SSH_AUTH_SOCK`` in
            addition to the explicit credentials. Defaults to False.

    Validation happens when :func:`asyncio_gateway.asyncio_gateway.request`
    constructs the SFTP client, so malformed runtime values raise the same
    ``ConfigurationError`` as legacy authentication objects.
    """

    username: str
    password: Optional[str] = None
    client_keys: Any = None
    key_passphrase: Optional[str] = None
    use_ssh_agent: bool = False

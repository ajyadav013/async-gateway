"""Exceptions."""

from typing import Dict, Optional, Text


class CustomGlobalException(Exception):
    """Class CustomGlobalException."""

    def __init__(self,
                 headline: Text,
                 error_code: int,
                 error_msg: Optional[Text] = '',
                 error_data: Optional[Dict] = None):
        """Custom exception with headline."""
        self.headline = headline
        self.error_code = error_code
        self.error_msg: Optional[Text] = error_msg
        self.error_data = error_data or {}

    def __str__(self) -> Text:
        """Return appropriate error exception str."""
        return f'{self.headline} | ' \
               f'{self.error_code} | ' \
               f'{self.error_msg} | ' \
               f'{self.error_data}'


class ConfigurationError(CustomGlobalException):
    """Raised when caller configuration cannot form a valid call.

    The caller's fault, not the remote endpoint's, and never retryable: the
    call is rejected at the library boundary rather than dispatched and
    reported as a failed request.

    Usage:
        raise ConfigurationError('serialization must return str, got bytes')
    """

    def __init__(self,
                 error_msg: Text,
                 error_data: Optional[Dict] = None) -> None:
        """Build a configuration error.

        Args:
            error_msg: What the caller got wrong, naming the offending key
                or value.
            error_data: Optional structured context for the caller.
        """
        super().__init__(
            headline='ConfigurationError',
            error_code=400,
            error_msg=error_msg,
            error_data=error_data,
        )

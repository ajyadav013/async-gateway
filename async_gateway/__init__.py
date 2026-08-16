"""async-gateway: one async call for HTTP, SOAP, FTP and SFTP.

Every module logs through ``logging.getLogger(__name__)``, so all records
land under the ``async_gateway`` tree and a consuming application can
configure or silence the library with one call.

A ``NullHandler`` is attached here and nothing else is: a library that
attaches a real handler duplicates its host application's log output, and
one that configures the root logger hijacks it. Until the application adds
a handler of its own, the library's records go nowhere -- which is the
correct default for a library.
"""

import logging

logging.getLogger(__name__).addHandler(logging.NullHandler())

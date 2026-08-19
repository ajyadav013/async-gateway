"""asyncio-gateway: one async call for HTTP, SOAP, FTP and SFTP.

Every module logs through ``logging.getLogger(__name__)``, so all records
land under the ``asyncio_gateway`` tree and a consuming application can
configure or silence the library with one call.

A ``NullHandler`` is attached here and nothing else is: a library that
attaches a real handler duplicates its host application's log output, and
one that configures the root logger hijacks it. Until the application adds
a handler of its own, the library's records go nowhere -- which is the
correct default for a library.

``__version__`` is *read* from the installed distribution's metadata, never
restated here. ``pyproject.toml``'s ``[project] version`` is the single
source of truth (R5-AC1), so a release edits one line and no second copy
can drift out of step with it.
"""

import logging
from importlib.metadata import version

#: The installed distribution's version, read from packaging metadata.
#:
#: This deliberately carries no ``PackageNotFoundError`` fallback. A fallback
#: string would be a *second* version claim -- exactly the four-way
#: contradiction (H22) that R5 exists to collapse -- and it would turn "the
#: package is not installed" into a silently wrong version rather than a
#: loud, immediate failure. Importing asyncio-gateway from a source tree that
#: was never installed raises, and that is the intended behaviour.
__version__: str = version('asyncio-gateway')

logging.getLogger(__name__).addHandler(logging.NullHandler())

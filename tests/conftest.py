"""Pytest fixture registrations for the async-gateway test suite.

Convention C-2 of the v1.0.0 release: this file holds only imports and
registrations. Every test double lives in its own ``tests/fixtures/<name>.py``
module owned by exactly one story, so stories adding a double never contend
for this file.
"""

from tests.fixtures.http_server import http_server

__all__ = ['http_server']

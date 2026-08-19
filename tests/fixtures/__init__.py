"""Test doubles for the asyncio-gateway suite, one module per double.

Convention C-2 of the v1.0.0 release: every test double lives in its own
``tests/fixtures/<name>.py`` module owned by exactly one story, and
``tests/conftest.py`` holds only the imports that register them. Six stories
add a double during this release; a single shared module would serialise
them all onto one lane.
"""

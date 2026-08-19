"""Tests for the protocol clients in ``asyncio_gateway/logic``.

One module per client, mirroring the source layout. The package exists
because the clients' tests are about a single protocol's own behaviour --
its TLS decision, its command mapping, its error translation -- where the
top-level ``tests/`` modules are about contracts every protocol shares.
"""

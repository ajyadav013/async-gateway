"""The two helper tiers, split by who is allowed to depend on them.

``helpers/common/`` is the stable, protocol-agnostic tier: small
utilities with no knowledge of any transport, safe for anything in the
tree to import. ``helpers/internal/`` is the request-machinery tier: the
abstract base every protocol class derives from, the request filters, the
response translation and the circuit breaker. It is internal in the sense
that it carries the library's own request contract, and it is not a
supported import surface for a consumer.

Keeping that split visible in the directory layout is why this package
exists at all. A helper that starts in ``common/`` and grows a dependency
on the request contract belongs in ``internal/``, and the move is then a
rename anyone can review rather than a quiet inversion of the dependency
direction.

This module intentionally exports nothing itself: importers name the
submodule they want.
"""

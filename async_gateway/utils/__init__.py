"""Protocol-independent building blocks the four transports all share.

This package holds what is true regardless of which protocol a call uses:
the response envelope every transport fills, the typed exception
hierarchy every module raises, the redaction one logger and one envelope
both apply, path and filesystem containment, and the tunable defaults.

Nothing here imports a protocol module, and that direction is deliberate.
It is what lets ``logic/`` depend on ``utils/`` without the reverse edge,
so a new protocol reuses these pieces rather than growing its own copy of
the envelope shape or its own idea of what counts as a credential.

This module intentionally exports nothing itself: importers name the
submodule they want, so the import graph states what a caller actually
depends on.
"""

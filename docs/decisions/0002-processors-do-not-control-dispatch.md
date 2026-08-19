# ADR-0002: Processor hooks do not control dispatch

## Status

Accepted.

## Date

2026-08-20

## Context

`request()` gives processor hooks the live response envelope. This supports
payload enrichment and caller metadata, but the envelope also contains `url`
and `protocol`. The protocol was already protected because it selects shared
circuit-breaker state. The URL remained writable but was discarded: dispatch
continued to use the original function argument. A hook could therefore appear
to retarget a request while the library silently contacted a different host.

Allowing URL rewrite would require re-running URL, scheme, destination,
redaction, and breaker validation after arbitrary caller code. It would also
make the hook a second request-construction API beside `request()` and
`protocol_info`.

## Decision

Pre-processors may enrich or reshape non-routing envelope fields, but may not
change `url` or `protocol`. A change raises `ProcessorError` before the
protocol object is constructed. Error text names fields and never includes
their values.

Post-processors may change those fields because transport and breaker decisions
have already completed; at that point the fields are report-only. Both hook
types must preserve the required envelope key set.

The 1.x API retains the live envelope for compatibility. A future major version
may introduce a typed request context with explicit mutable and immutable
fields rather than extending the response envelope further.

## Alternatives considered

### Honor a rewritten URL

Rejected. It creates a second dispatch path and requires duplicating the public
boundary's validation after untrusted hook code. The current implementation did
not honor the rewrite, so accepting it now would also turn an apparent feature
into a new network capability.

### Silently restore routing fields

Rejected. This is the current URL behavior and hides caller mistakes. A hook
which tried to retarget a request must learn that the operation was refused.

### Pass an immutable envelope

Deferred to a major release. It would also remove supported payload and custom
metadata edits, making it broader than the P0 correction.

## Consequences

- Processor configuration errors and callback failures remain distinct.
- Existing payload and metadata processors keep working.
- Existing processors which assign a different URL now fail before network
  activity instead of having their change ignored.
- The dispatch target has one authority: the validated arguments to
  `request()`.

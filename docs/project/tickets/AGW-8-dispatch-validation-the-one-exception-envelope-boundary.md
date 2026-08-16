# AGW-8: Dispatch, validation, the one exception→envelope boundary

- **Status:** OPEN
- **Story:** S8 — spec Step 6, size M (`docs/specs/v1_release_stories.md` §4, Phase 1)
- **Spec:** `docs/specs/v1_release_spec.md` — R11-AC1,2,3,4,6,7 (Group D — Entry point and protocol dispatch) · R10-AC3 (Group C — The public response contract and the error model)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; Architecture overview, `request()` and the protocol registry
- **Decisions:** none
- **Files (declared scope):** ~`async_gateway.py`, `logic/__init__.py`, `helpers/internal/base.py` · +`tests/test_entrypoint.py`

## Why

R11 requires dispatch that validates its input and cannot crash on the documented call: the protocol
string is normalised once and the same value serves both the guard and the registry lookup, and
`protocol_info` is checked rather than read raw. R10-AC3 fixes the single exception→envelope boundary
so that a library error becomes a populated envelope while a programming error still escapes. FI-14
puts the scheme check *after* the pre-processors, against the URL actually dispatched — the property
whose absence concealed C6, H6 and most of the audit.

Closes findings: H4, H5, H6, L2. Discharges FI-14.

## Definition of Done

- protocol normalised **once**, same value for guard and lookup (parametrised: lower/mixed/whitespace × 5 protocols)
- `None`/`''`/`123`/unknown → `ConfigurationError` naming supported protocols
- `protocol_info` `None` and `{}` work where nothing is required, else `ConfigurationError` naming the key; `base.py:31-37` no longer reads the **raw** `info` after guarding it
- **FI-14:** `'HTTPS'`+`http://` → error, schemeless → upgraded, `'HTTP'` accepts either — checked against the URL **actually dispatched**, after pre-processors
- registry typed `dict[str, type[BaseRequestClass]]`
- **an injected `KeyError`/`TypeError`/`UnboundLocalError` escapes `request()`** rather than becoming an envelope (the property whose absence concealed C6, H6 and most of the audit)

## Dependencies

- **blockedBy:** AGW-7
- **blocks:** AGW-9

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

# AGW-13: HTTP request construction and response handling

- **Status:** OPEN
- **Story:** S13 — spec Step 11, size **L** (`docs/specs/v1_release_stories.md` §4, Phase 2; sizing exception §12 — one step's worth of a single requirement pair over a fixed file set)
- **Spec:** `docs/specs/v1_release_spec.md` — R12 all, R13 all (Group E — HTTP protocol correctness)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; the request/response filter table
- **Decisions:** none
- **Files (declared scope):** ~`helpers/internal/{filters_helper,__init__,request_helper,response_helper}.py`, `logic/http_client.py` · +`tests/logic/test_http_client.py`, `tests/helpers/test_request_helper.py` · ~`tests/helpers/test_filters_helper.py`

## Why

R12 requires request construction that dispatches on the real Content-Type and never mutates the
caller's data; R13 requires response handling that distinguishes empty from broken. Today the same
media type is matched by exact key on one side and by substring on the other, unknown types route to
the JSON encoder (which is what sends a SOAP envelope through a JSON serializer), and the filters read
`kwargs['request_type']` — a hard `KeyError` for every SOAP call. The new raw-body branch added here is
the seam AGW-22 dispatches its envelope bytes through.

Closes findings: H14, M5, M6, M7, M8, M9, M10, L8.

## Definition of Done

- media-type dispatch strips parameters and is case-insensitive; **the same helper serves request and response sides** — the exact-key vs substring inconsistency *is* the evidence of the bug and must not survive
- **the spec's filter table exactly**, incl. the **new raw-body branch** for `text/xml`/`application/soap+xml`/`application/xml`; the `'default' → JSON` entry is **replaced** (unknown + `str`/`bytes` → raw, unknown + `dict` → JSON) — routing unknown types to JSON is what sends a SOAP envelope through a JSON encoder
- filters take a typed parameter set, **never `kwargs['request_type']`** (today a hard `KeyError` for every SOAP call)
- `grep -n "mapping.get(.*)("` shows no un-guarded immediate call
- `"get"`/`"GET"`/`"Get"` all attach the payload as **query params**
- bool → `"true"`/`"false"` via `isinstance`; `None`/`list`/`datetime`/nested each covered
- **no caller dict mutated** (byte-identical after the call)
- download keys have documented defaults; omitting both still downloads
- dead `if …: pass` removed and the GET+upload case decided explicitly
- malformed JSON → `json=None` + `ok=False` + `SERIALIZATION`, legitimate `{}` → `json={}` + `ok=True`, and a literal `null` distinguishable from a parse failure
- `UnicodeDecodeError` sets **both** `text` and `error`, no `KeyError` escapes
- multipart writes **bytes to a binary file** (`\x89PNG`, not `b'\\x89PNG'`), reads a `chunk_size*3+7` part in full, terminates cleanly on a `None` part, and accumulates into a buffer not `data + str(data)`

## Dependencies

- **blockedBy:** AGW-10
- **blocks:** AGW-14

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

# AGW-5: Finish the `orjson` migration — **four** call sites

- **Status:** OPEN
- **Story:** S5 — spec Step 4, size M (`docs/specs/v1_release_stories.md` §4, Phase 0)
- **Spec:** `docs/specs/v1_release_spec.md` — R3-AC1,3,4,5,6,7 (Group A — Runtime dependencies that are declared, imported, and correct)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** none
- **Files (declared scope):** ~`helpers/internal/response_helper.py`, `helpers/internal/filters_helper.py`, `logic/http.py` *(pre-rename)*, `pyproject.toml` · +`tests/helpers/{__init__,test_filters_helper,test_response_helper}.py`

> **Path note (spec deferral D1):** the boundary names `logic/http.py`, **not** `http_client.py` — the
> rename is Step 4.5 (AGW-6) and this story lands at Step 4. Sequence wins.

## Why

R3 requires the runtime dependencies to be declared, imported and correct. `ujson` is still imported
at four call sites while the declared/target serializer is `orjson`, so the package does not import
from a clean install. This story finishes the migration and removes the stray `requests` import.

Closes findings: C1, MG2. Discharges FI-6.

## ⚠ Four call sites, not three

Three *modules*, **four** *call sites*; `filters_helper.py` carries two. Both numbers are used
deliberately and are not interchangeable. **A story that migrates three sites has not finished.**

1. `response_helper.py:14` `ujson.loads` → `orjson.loads` — a test asserts **both** `str` and `bytes` parse.
2. `filters_helper.py:45` `ujson.dumps(form_value)` in `FormData.add_field` → `orjson.dumps(v).decode()` — test asserts the field value is `str` and byte-identical to today's.
3. `filters_helper.py:78` `data = ujson.dumps(data)` → `.decode()` — test asserts `isinstance(filters['data'], str)`.
4. `logic/http.py:30` the `json_serialize` wiring — the default is a **wrapper** (`lambda o: orjson.dumps(o).decode()`), **never bare `orjson.dumps`**. **FI-6:** `ClientSession(json_serialize=)` requires a `str`-returning callable, and this breaks *after* the clean-venv import starts passing, so it looks like the migration succeeded. A test posts a JSON body through the default serializer against the AGW-1 recording handler and asserts the received body.

## Definition of Done

- All four call sites above migrated and individually asserted
- `grep -rn "ujson" async_gateway/` → 0
- `grep -rn "^import requests\|^from requests" async_gateway/` → 0
- a `bytes`-returning caller serializer is rejected at the boundary with `ConfigurationError`; `serialization=json.dumps` keeps working
- the AGW-4 clean-venv job — failing in reality while passing in CI's pre-installed env — now passes against a truly clean venv
- `orjson.loads` on empty bytes raises a `ValueError` subclass, and the handler catches the `ValueError` **base** so the migration does not change which type escapes

## Dependencies

- **blockedBy:** AGW-4
- **blocks:** AGW-6

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

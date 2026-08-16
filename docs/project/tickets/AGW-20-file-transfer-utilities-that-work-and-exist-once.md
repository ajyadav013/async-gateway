# AGW-20: File-transfer utilities that work and exist once

- **Status:** OPEN
- **Story:** S20 — spec Step 18, size M (`docs/specs/v1_release_stories.md` §4, Phase 3)
- **Spec:** `docs/specs/v1_release_spec.md` — R25-AC1,2,3,4,5,7 (export half) (Group L — Utilities and observability primitives)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; utilities
- **Decisions:** none
- **Files (declared scope):** −`helpers/common/file_helper.py` · ~`helpers/internal/request_helper.py` *(delete `fetch_file`)*, `utils/http_file_config.py`, `utils/constants.py` · +`tests/utils/test_http_file_config.py`

## Why

R25 requires file-transfer utilities that work, exist once, and check more than one status code.
H15's duplicate module is a second copy with a **different parameter order**, so importing the wrong
one and calling positionally writes the bucket name to a local path; H16's URL download only checks
403, so a 404 body or an HTML login redirect is saved as the requested file and any later upload ships
the error page. FI-11 requires the S3 path to be written **once** against aioboto3 15.x — no earlier
story exercises it.

Closes findings: H15, H16, MG5, L13. Discharges FI-11 (written once, against the AGW-2 set).

## Definition of Done

- the duplicate module and the unreferenced `fetch_file` are **deleted**; a test asserts the module no longer imports *(the duplicate is H15's second copy with a **different parameter order**, so importing the wrong one and calling positionally writes the bucket name to a local path)*
- one `download_file_from_s3` on `aioboto3.Session().client(...)` with the correct **`Filename=`**; a mocked test asserts the exact `download_file(Bucket=, Key=, Filename=)` signature
- parameters **keyword-only** so a positional mistake is a `TypeError` at the call site
- URL download raises on **any** non-success status — parametrised 200/301-followed/400/403/404/500/502, only success writes, every failure gives `ok=False` + the status and **no file on disk** *(today only 403 is checked, so a 404 body or HTML login redirect is saved as the requested file and any later upload ships the error page)*
- `STATUS_CODE_403` removed
- absent S3 credentials wrapped in `ConfigurationError`
- a zero-length success body is written and documented as legitimate
- FI-11: written **once** against aioboto3 15.x; no test exercises the S3 path earlier

## Dependencies

- **blockedBy:** AGW-19
- **blocks:** AGW-22

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

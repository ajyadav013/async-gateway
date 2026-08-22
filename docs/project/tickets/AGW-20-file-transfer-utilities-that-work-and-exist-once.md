# AGW-20: File-transfer utilities that work and exist once

- **Status:** DONE
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

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`54dbdf0`](https://github.com/ajyadav013/asyncio-gateway/commit/54dbdf0); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


### 2026-08-17 — implementation (lane `lane/s20`, worktree `wt/s20`, commit `54dbdf0`)

**The two deletions unblock each other, and neither was safe alone.** The only live
reference to the duplicate was `helpers/internal/request_helper.py:52`, which imported
`download_file_from_s3` *solely* for use inside `fetch_file` (`:188`) — the dead function
this story also deletes. So deleting the module alone would have broken the import, and
deleting `fetch_file` alone would have left the duplicate importable by users. Removing
`fetch_file` orphaned the `HTTP_TIMEOUT` import in that module, which is dropped with it;
`aiofiles` and `aiohttp` both remain in use there and are untouched.

**H15 — the survivor is rewritten, not patched.** `aioboto3.Session().client('s3')`
replaces the module-level `aioboto3.client(...)` removed in 9.0, and `Filename=` replaces
`file_save_path=`, which `download_file` does not accept. Written once against the
installed aioboto3 15.5.0 (FI-11 discharged). Parameters are keyword-only, so the
positional call that wrote a bucket name to a local path is now a `TypeError`.

**H16 — `response.status >= HTTP_ERROR_STATUS`** replaces `== STATUS_CODE_403`, reusing the
constant `logic/http_client.py:853` already judges HTTP failure by rather than adding a
second definition of "failed". A followed 301 is judged on its final hop, and a
zero-length success is still written.

**L13 —** `STATUS_CODE_403` is deleted from `utils/constants.py`. The name survives only
inside the comment on `HTTP_ERROR_STATUS` that explains what it used to do.

**Verification** (venv `wt/venv-s20`): `pytest -q` 800 → **824 passed**, 2 xfailed, exit 0;
coverage 96.23% → **97.49%** (floor 95.70 untouched, per the ratchet's own rule that a
story may raise but never lower it — raising it is a later step's job). `mypy async_gateway`
clean. `flake8 async_gateway tests` **17 findings, all pre-existing** — the new test file
contributes none.

**Tests proven able to fail.** Five mutations, each inside the protected code, each
restored from a `cp` backup afterwards: the status check back to 403-only → 5 red; `Filename=`
→ `file_save_path=` → 2 red; keyword-only marker removed → 1 red; the credential `except`
narrowed away → 2 red; the deleted module and `STATUS_CODE_403` restored → 2 red.

**One deviation from the declared file scope, reported not widened:** `tests/utils/__init__.py`
was created because `tests/` uses package-style test dirs (`tests/helpers/__init__.py`,
`tests/logic/__init__.py`) and `tests/utils/` did not exist. The ticket's declared scope names
the new test module but not the package marker it needs.

**Untouched deliberately:** R25-AC6/AC7's README half (`README.md:891`, `:1041` name the
non-existent `http_file_upload_config`, and `:1062` passes `file_download_path` to the S3
function). Those lines are R29/S29's, and `README.md` is outside this story's file boundary —
but note the S3 example there will not run against this signature until it is rewritten.

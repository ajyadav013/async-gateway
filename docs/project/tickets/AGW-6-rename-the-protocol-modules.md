# AGW-6: Rename the protocol modules

- **Status:** DONE
- **Story:** S6 — spec Step 4.5, size M (`docs/specs/v1_release_stories.md` §4, Phase 0)
- **Spec:** `docs/specs/v1_release_spec.md` — R27-AC4 (Group M — Quality gates turned on); performed here, verified at AGW-25
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; File structure — current vs target
- **Decisions:** **OQ9 RESOLVED — RATIFIED.** See the gate box below.
- **Files (declared scope):** `git mv logic/{http,ftp,sftp}.py → logic/{http,ftp,sftp}_client.py` · −`logic/soap.py` (0 bytes; AGW-22 creates `soap_client.py` under its final name — **not** a fourth rename) · ~`logic/__init__.py` + importers · ~`tests/helpers/test_filters_helper.py` (imports only)

> ## ✅ HUMAN GATE — OQ9: **RESOLVED — RATIFIED** (recorded 2026-08-16, before implementation)
>
> **Question:** ratify `logic/*.py` → `logic/*_client.py`, or accept a per-module `# noqa: A005` with a
> written justification. *(Spec recommendation: ratify the rename.)*
>
> **Answer: RATIFY the rename.** The story proceeded on that answer; the cancellation branch below did
> not fire and no downstream path reverts. The rationale of record is Ruling E in the orchestrator's
> working memory: the spec itself recommends ratifying (`v1_release_spec.md:2719`), the
> EM-approved/DA-confirmed plan embeds the rename at Step 4.5 (FI-15), the `# noqa: A005` alternative
> contradicts R27's zero-suppression criterion (so it is a spec change, not an implementation choice),
> and the change is a **two-way door** — one `git revert` of a pure-rename commit, zero published
> consumers, nothing externalised.

> **OQ9 blocks Phase 0, not Phase 6.** FI-15 moved the rename from Step 24 because every test path,
> traceability row and later step in the spec is written in post-rename names. If OQ9 instead answers
> "per-module `# noqa: A005`", **AGW-6 is cancelled, AGW-25 absorbs the suppression + justification, and
> every downstream path in this breakdown reverts** — the breakdown must then be re-issued. Get the
> answer before implementation starts.

## Why

R27-AC4 requires the `A005` stdlib-shadowing finding (`logic/http.py` shadows stdlib `http`) to be
resolved by renaming the three protocol modules rather than by suppressing the rule — it is free today
because there are zero consumers, and it leaves no suppression to justify. FI-15 pulls the rename into
Phase 0: every test module, traceability row and later step in the plan is written in post-rename
names, so landing it late would mean twenty steps of work written against filenames that do not exist,
followed by the largest and least reviewable diff in the release. This story performs the rename;
AGW-25 verifies it.

Discharges FI-15 (performed here, verified at AGW-25).

## Definition of Done

- **No behaviour change in this commit** — a pure rename, reviewable as one diff
- `flake8` reports zero `A005`
- `grep -rn "logic/http\.py\|logic/ftp\.py\|logic/sftp\.py\|logic/soap\.py" .` → nothing outside spec/audit
- AGW-4's import job still green
- **no compatibility alias** (A4: zero consumers)

## Dependencies

- **blockedBy:** AGW-5
- **blocks:** AGW-7

## Decisions

- **OQ9: RATIFIED** — see the gate box above.
- **`git mv`, so git records renames rather than a delete plus an add.** `git diff -M --stat` reports
  `{http.py => http_client.py} | 0` for all three: **zero lines changed**, which is the mechanical
  form of "no behaviour change in this commit" and is what makes the diff reviewable at a glance.
- **`logic/soap.py` is deleted, not renamed.** It is a 0-byte file (`wc -c` = 0) imported by nothing;
  AGW-22 creates `logic/soap_client.py` from scratch under its final name. Treating it as a fourth
  rename would have produced an empty `soap_client.py` for sixteen stories, which reads as
  "implemented" to anyone scanning the tree. `protocol_mapping`'s `'SOAP': None` entry is **left
  alone** — AGW-22 owns it.
- **No compatibility alias, no `logic/http.py` shim.** Zero published consumers (the package has never
  installed-and-imported successfully, which is what this release is fixing), so a shim would be
  backwards compatibility with nobody, and CLAUDE.md's *Surgical Changes* rule is explicit: delete the
  path you superseded unless compatibility is a stated requirement.
- **`ftp` and `sftp` were renamed too, though only `http` triggered `A005`.** Renaming one of three
  sibling protocol modules to a different naming convention is worse than either convention applied
  consistently, and the spec's target layout (`v1_release_spec.md:2925-2928`) names all three.

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`0eef6e4`](https://github.com/ajyadav013/asyncio-gateway/commit/0eef6e4); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


**2026-08-16 — implemented, self-verified, committed.**

*Change:* `git mv logic/{http,ftp,sftp}.py → logic/{http,ftp,sftp}_client.py` · `git rm
logic/soap.py` · `~logic/__init__.py` (three import lines) · `~tests/helpers/test_filters_helper.py`
(one import, plus two prose references to the old path in the module docstring and a section comment
— left stale they would have been documentation this commit itself falsified).

### Every importer was found before anything moved

`grep -rn "async_gateway\.logic\.\(http\|ftp\|sftp\|soap\)"` over the tree, plus a search of
`README.md` and `docs/source/` (the Sphinx config references no modules), gave exactly four import
sites: three in `logic/__init__.py` and one in `tests/helpers/test_filters_helper.py` (added by
AGW-5 the commit before). `async_gateway.py:6` imports `from async_gateway.logic import
protocol_mapping` — the package, not a module — so it is untouched by the rename.

### Evidence — commands and exit codes

| Check | Result |
|---|---|
| `git diff --cached -M --stat` | `{ftp,http,sftp}.py => *_client.py` — **0 insertions, 0 deletions** on all three; `soap.py` deleted, 0 lines |
| `flake8 . \| grep A005` | **no matches** (grep exit 1). Was `./async_gateway/logic/http.py:0:1: A005 the module is shadowing a Python builtin module "http"` |
| `flake8 .` total | 1284 → **1281** — only the A005 line and the deleted file's entries went; nothing new appeared |
| `flake8 tests/` | **exit 0, no output** |
| `grep -rn "async_gateway\.logic\.\(http\|ftp\|sftp\|soap\)\b"` | **exit 1** — zero module-style references to the old names anywhere |
| `pytest` | **27 passed, exit 0** — identical count and set to the pre-rename commit |
| `pytest -p randomly --randomly-seed=N` × 5 committed seeds | **27 passed × 5, exit 0** |
| `mypy async_gateway` | `Success: no issues found in **21** source files` (was 22 — `soap.py` is gone) |
| `python -m build` | exit 0 · `twine check` **PASSED ×2** |
| wheel contents under `logic/` | `__init__.py`, `ftp_client.py`, `http_client.py`, `sftp_client.py` — **no old names, no `soap.py`** |
| clean-venv submodule import (fresh 3.12 venv, `--no-cache-dir`, run from `/tmp`) | `SUBMODULE IMPORT OK` — **exit 0**, AGW-4's job stays green |
| `protocol_mapping` resolved from the installed wheel | `{'HTTP': 'HttpRequest', 'HTTPS': 'HttpRequest', 'SOAP': None, 'FTP': 'FTPRequest', 'SFTP': 'SFTPRequest'}` — unchanged |

Stale `__pycache__/{http,ftp,sftp}.cpython-314.pyc` were removed from the working tree so no ghost of
the old module names survives locally. They are untracked and were never in the artifact.

### Residual — the path-string grep, stated precisely

`grep -rn "logic/http\.py\|logic/ftp\.py\|logic/sftp\.py\|logic/soap\.py" .` is **not** zero. It
returns 61 hits in `docs/specs/v1_release_spec.md` and 6 in `v1_release_stories.md` — both explicitly
exempted by the criterion — and **10 more in `docs/project/tickets/`**: AGW-2 (1), AGW-5 (7) and this
ticket (2). Those are historical records: AGW-5's work log describes a commit that genuinely did edit
`logic/http.py`, because it landed one step before the rename by the plan's own design (spec deferral
D1), and AGW-6's own two hits are the `A005` finding description. Rewriting them would falsify the
project's audit trail to satisfy a grep. **Zero hits in code, config, tests or the built artifact** —
flagged to the orchestrator in case the criterion's exemption list should name the ticket store
alongside `docs/specs/` and the audit report.

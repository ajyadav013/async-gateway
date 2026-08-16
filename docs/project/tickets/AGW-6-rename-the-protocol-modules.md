# AGW-6: Rename the protocol modules

- **Status:** OPEN
- **Story:** S6 — spec Step 4.5, size M (`docs/specs/v1_release_stories.md` §4, Phase 0)
- **Spec:** `docs/specs/v1_release_spec.md` — R27-AC4 (Group M — Quality gates turned on); performed here, verified at AGW-25
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; File structure — current vs target
- **Decisions:** none — **blocked on the human answer to OQ9** (see the gate box below)
- **Files (declared scope):** `git mv logic/{http,ftp,sftp}.py → logic/{http,ftp,sftp}_client.py` · −`logic/soap.py` (0 bytes; AGW-22 creates `soap_client.py` under its final name — **not** a fourth rename) · ~`logic/__init__.py` + importers · ~`tests/helpers/test_filters_helper.py` (imports only)

> ## 🚦 HUMAN GATE — OQ9 (`docs/specs/v1_release_spec.md` §Open questions, OQ9)
>
> **Question:** ratify `logic/*.py` → `logic/*_client.py`, or accept a per-module `# noqa: A005` with a
> written justification. *(Spec recommendation: ratify the rename.)*
>
> **When it must be answered (§9):** *"Before implementation starts — the rename is in Phase 0."*
>
> **If unanswered / declined (§9, verbatim):** *"S6 is cancelled, S25 absorbs a justified
> `# noqa: A005`, and **every downstream path in this breakdown reverts** — re-issue required."*
>
> Do not start this ticket until the answer is recorded here and in the work log.

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

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

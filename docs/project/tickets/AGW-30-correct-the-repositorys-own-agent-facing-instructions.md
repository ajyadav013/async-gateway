# AGW-30: Correct the repository's own agent-facing instructions

- **Status:** OPEN
- **Story:** S30 — spec Step 29, size S (`docs/specs/v1_release_stories.md` §4, Phase 7). *Free-floating lane: fully file-disjoint from all 31 other stories (§7) — developable in any wave from W0 onward, merged at step-order position 29.*
- **Spec:** `docs/specs/v1_release_spec.md` — R32 all (Group N — Documentation)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** none — **blocked on the human answer to OQ8** (see the gate box below)
- **Files (declared scope):** ~`CLAUDE.md`, `.claude/rules/fastapi-patterns.md`

> ## 🚦 HUMAN GATE — OQ8 (`docs/specs/v1_release_spec.md` §Open questions, OQ8)
>
> **Question:** *"approve the edit, or decline and accept that every future agent session starts from a
> wrong command block."* Both files describe a FastAPI service (`uvicorn app.main:app`, `ruff`, an
> `app/` package, a router → service → repository recipe) that this library does not have, and both are
> on the **project-wide-files list requiring explicit approval**. *(Spec recommendation: approve.)*
>
> **When it must be answered (§9):** *"Before W22."*
>
> **If unanswered / declined (§9, verbatim):** *"MG7 is recorded **'rejected by human'** in the
> traceability table — **not silently dropped**."* This ticket then closes as **rejected**, not skipped.

## Why

R32 requires the repository's own agent-facing instructions to be correct: `CLAUDE.md` and
`.claude/rules/fastapi-patterns.md` both describe a FastAPI service this library is not, so every
future agent session starts from a wrong command block and a resource recipe that does not apply. If
engineering here is agent-driven, these files are load-bearing infrastructure rather than
documentation. Both are on the project-wide-files list, so the edit needs recorded approval before it
is made.

Closes findings: MG7 *(or records it "rejected by human" if OQ8 declines)*.

## Definition of Done

- the Commands block names the real commands: `pip install -e '.[dev]'` · `pytest` · `flake8 .` · `mypy async_gateway` · the chosen formatter · `python -m build`. **There is no "Run" command and the block says so explicitly** — this is a library, not a service
- the FastAPI resource recipe is replaced by the adding-a-protocol recipe (registry entry → protocol class → envelope mapping → error mapping → tests → README section)
- `grep -n "uvicorn\|app.main\|ruff\|app/" CLAUDE.md` → no stale references
- **explicit user approval is recorded in the work log before the edit** — both files are on the project-wide list. **If the human declines, MG7 is recorded "rejected by human" in the traceability table, not silently dropped**, and this story closes as rejected rather than skipped

## Dependencies

- **blockedBy:** AGW-27
- **blocks:** AGW-32

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

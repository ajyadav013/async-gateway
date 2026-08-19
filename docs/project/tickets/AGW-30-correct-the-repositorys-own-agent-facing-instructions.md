# AGW-30: Correct the repository's own agent-facing instructions

- **Status:** DONE
- **Story:** S30 — spec Step 29, size S (`docs/specs/v1_release_stories.md` §4, Phase 7). *Free-floating lane: fully file-disjoint from all 31 other stories (§7) — developable in any wave from W0 onward, merged at step-order position 29.*
- **Spec:** `docs/specs/v1_release_spec.md` — R32 all (Group N — Documentation)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** OQ8 **approved by the human on 2026-08-16**, scoped to the stack-specific sections
- **Files (declared scope):** ~`CLAUDE.md`, `.claude/rules/fastapi-patterns.md`

> ## ✅ HUMAN GATE — OQ8: **APPROVED** (2026-08-16)
>
> **Question:** *"approve the edit, or decline and accept that every future agent session starts from a
> wrong command block."* Both files describe a FastAPI service (`uvicorn app.main:app`, `ruff`, an
> `app/` package, a router → service → repository recipe) that this library does not have, and both are
> on the **project-wide-files list requiring explicit approval**. *(Spec recommendation: approve.)*
>
> **Answer:** **approved**, before the edit was made, scoped to the stack-specific sections; the
> agnostic SDLC content was left untouched. Recorded in the commit body of `7f87275`.
>
> **Consequence:** the declined branch below did **not** occur. MG7 is **closed as implemented**, and
> the "rejected by human" wording the spec pre-committed to is therefore not the outcome to record —
> see the *Decisions* section.
>
> *(Superseded, retained for the audit trail — the pre-answer text read:* "**If unanswered / declined
> (§9, verbatim):** *MG7 is recorded 'rejected by human' in the traceability table — not silently
> dropped.* This ticket then closes as **rejected**, not skipped."*)*

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

- **OQ8: approved (2026-08-16), before the edit.** Both files are on the project-wide list, so the
  approval was obtained first and scoped to the stack-specific sections only; the agnostic SDLC
  content was not touched. Recorded in the commit body of `7f87275`.
- **MG7 is recorded as *implemented*, not "rejected by human".** The spec's §9 contingency
  pre-committed to the rejected wording *if OQ8 went unanswered or was declined*. It was neither, so
  recording MG7 as rejected would now assert the opposite of what happened. The stale note in
  `docs/specs/v1_release_spec.md` was corrected alongside this ticket.
- **The rule file keeps the name `fastapi-patterns.md`.** Its contents describe this library, but
  `CLAUDE.md` references it by path, so renaming it was outside this story's boundary. The filename
  is a leftover from the claude-kit template and is retained deliberately.

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`7f87275`](https://github.com/ajyadav013/asyncio-gateway/commit/7f87275); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


**2026-08-16 — OQ8 approved by the human.** Scoped to the stack-specific sections of both
project-wide files. This is the approval the Definition of Done requires *before* the edit; it
preceded the commit below.

**2026-08-17 — implemented in commit `7f87275`,** `docs(agents): describe the library this
repository actually is [AGW-30]`. Three files, +481 lines: `CLAUDE.md` (+301),
`.claude/rules/fastapi-patterns.md` (+176), `.gitignore` (+4). Only these two agent-facing files
were added to version control; whether the rest of the claude-kit install should be tracked was left
as a separate, untaken decision.

The Commands block now names the real commands and — as importantly — the ones that do **not** exist
here: there is no run command (this is a library, not a service) and no autoformatter is configured.
It also records that `python -m build` needs `build` installed separately, that `flake8 .` still
exits non-zero on findings a later story owns, and that `mypy async_gateway` reported clean at the
time only because `ignore_errors = true` was still set — so a green run would not be mistaken for a
clean one. The FastAPI resource recipe was replaced by the adding-a-protocol recipe (protocol class →
registry entry → envelope mapping → error mapping → tests → README section).

**2026-08-18 — ledger corrected to match the tree.** This ticket had remained `OPEN` with an empty
work log for a story whose code had already shipped, and the spec still carried the pre-committed
"rejected by human" note. Both were stale, not wrong-in-substance: the audit trail disagreed with the
repository. Definition of Done re-verified against the working tree at the time of this correction:

```
grep -cE 'uvicorn|app\.main|ruff|app/' CLAUDE.md                        -> 0
grep -cE 'uvicorn|app\.main|ruff'      .claude/rules/fastapi-patterns.md -> 0
grep -c  'no run command'              CLAUDE.md                        -> 1
```

All four DoD bullets are met, including the approval-recorded-before-the-edit bullet (see the
2026-08-16 entry). Closes MG7 as **implemented**.

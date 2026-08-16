# AGW-32: Final gate run and the tag

- **Status:** OPEN
- **Story:** S32 — spec Step 31, size S (`docs/specs/v1_release_stories.md` §4, Phase 7)
- **Spec:** `docs/specs/v1_release_spec.md` — R1-AC10 (Group A — Scaffolding and quality-gate infrastructure) · R5-AC4 (the tag) · R4/R27's five-item PR gate (Group M — Quality gates turned on)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; Rollback and reversal
- **Decisions:** none
- **Files (declared scope):** no file edits; verification + one annotated tag

## Why

R1-AC10 and R4/R27's five-item PR gate require the whole release to be verified on a clean checkout
rather than trusted from the per-step gates, and R5-AC4 puts the repository's **first** annotated tag
on the release commit. Nothing is uploaded: the PyPI upload is a manual human action and is out of
scope under locked decision 4. This ticket edits no file — it is the proof that everything before it
holds together.

## Definition of Done

- the **five-item PR gate** passes on a clean checkout: fresh-venv install of the built **wheel** imports a submodule · linter zero violations · mypy zero errors with **no `ignore_errors`** · full suite green at **100% line + branch** · `sdist` + `wheel` build and pass `twine check`
- every CI step is a **required check** and none is `continue-on-error`
- an annotated **`v1.0.0`** tag on the release commit, and it is the repository's **first** tag
- **nothing is uploaded** — the PyPI upload is a manual human action, out of scope (locked decision 4)

## Dependencies

- **blockedBy:** AGW-29, AGW-30, AGW-31
- **blocks:** none — terminal story

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

# AGW-26: Remove `mypy ignore_errors`; drive to zero; then `py.typed`

- **Status:** OPEN
- **Story:** S26 — spec Step 25, size M (`docs/specs/v1_release_stories.md` §4, Phase 6)
- **Spec:** `docs/specs/v1_release_spec.md` — R27-AC5,6 (Group M — Quality gates turned on) · R4-AC5 (`py.typed`) (Group A — Scaffolding and quality-gate infrastructure)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** none — the OQ2 count discrepancy (40 vs 44) is ratification-only and **is not allowed to matter**
- **Files (declared scope):** ~`pyproject.toml`, `.github/workflows/ci.yml` · +`async_gateway/py.typed` · ~`tests/test_packaging.py` · ~the files mypy reports — **type conformance only**

## Why

R27-AC5 requires the type checker to actually report: `ignore_errors` turns a real error list into
`Success: no issues found in 22 source files`, which is H18 — a gate configured to see nothing.
R4-AC5 adds `py.typed`, and FI-13 fixes its position: publishing the marker before mypy is clean
exports this library's type errors into every downstream consumer's build, so it is added **last**,
only once the checker is green. AGW-3 deliberately excluded it for this reason.

Closes findings: H18. Discharges FI-13.

## Definition of Done

- `grep -rn "ignore_errors" .` returns nothing outside the spec and the audit report; `mypy async_gateway` exits 0
- `ignore_missing_imports` only per-module, each with a comment naming the package — **never globally**, or the gate is back where it started
- **`py.typed` is added only now, last** (FI-13): publishing it before mypy is clean exports this library's type errors to every downstream consumer's build
- a packaging test asserts `py.typed` is in the wheel
- the count discrepancy (40 vs 44, OQ2) **is not allowed to matter** — the criterion is count-independent

## Dependencies

- **blockedBy:** AGW-25
- **blocks:** AGW-27

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

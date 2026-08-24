# Technical wiki — how it works

An **index**, not a copy. The developer documentation lives in Part B of the spec; this page points at
it and at the plan that sequences the work.

## Developer documentation

[`docs/specs/v1_release_spec.md`](../../specs/v1_release_spec.md) — **Part B: Developer Documentation
(asyncio-gateway v1.0.0)**, beginning at the `# Developer Documentation` heading. It carries:

- **Architecture overview** — `request()` as the only public entry point, the protocol-strategy layer,
  and the seams being fixed.
- **File structure — current vs target** — including the `logic/*.py` → `logic/*_client.py` rename.
- **Dependencies §Runtime (target state)** — the upgraded, range-pinned runtime set.
- **The response envelope and the invariants E1–E11** — the contract every protocol returns.
- **Requirement ordering** — the hard dependencies between requirements.
- **Fix-interaction constraints FI-1 … FI-16** — the orderings that cannot be rearranged.
- **Reaching branch coverage on the hard shapes** — the coverage strategy behind AGW-27.
- **Rollback and reversal** — one revertible commit per implementation step.

There is no separate `*_design-spec.md`: this project's technical design is Part B of the single spec
file.

## Implementation plan

[`docs/specs/v1_release_stories.md`](../../specs/v1_release_stories.md) — the story breakdown:

| Section | What it holds |
|---|---|
| §3 | The three conventions (C-1 ratchet protocol, C-2 fixture protocol, C-3 merge order = step order) |
| §4 | The story catalogue — file boundary and definition of done per story |
| §5 | FI → owning story |
| §6 | The dependency graph, edge list and acyclicity proof |
| §7 | The parallel-lane plan — 25 waves, peak width 3 |
| §8 | Serialization points — files that forbid concurrency |
| §10 | Traceability — every acceptance criterion → ≥ 1 story |
| §12 | Sizing exceptions and the one sanctioned split (AGW-24) |

## Phase map (story → ticket, 1:1 — AGW-*n* is story S*n*)

| Phase | Tickets |
|---|---|
| 0 — Scaffolding | AGW-1 … AGW-6 |
| 1 — The contract | AGW-7 … AGW-9 |
| 2 — Protocol correctness | AGW-10 … AGW-14 |
| 3 — Resilience, security and utilities | AGW-15 … AGW-21 |
| 4 — SOAP | AGW-22 |
| 5 — The resilience-library decision | AGW-23 *(spec Step 21 is vacated and carries no story)* |
| 6 — Gates on, one per commit | AGW-24 … AGW-27 |
| 7 — Release | AGW-28 … AGW-32 |

## Architecture decisions

See [`decisions.md`](decisions.md).

## Post-1.0 contract correction

[`docs/specs/p0_contract_corrections_spec.md`](../../specs/p0_contract_corrections_spec.md)
maps each P0 requirement to its code, tests, and documentation. It deliberately
changes no response schema, protocol registry, dependency, or CI workflow.

## GCS selector

- [`docs/specs/gcs_selector_spec.md`](../../specs/gcs_selector_spec.md) — the
  bounded GCS selector specification and developer contract.
- [`docs/specs/gcs_selector_stories.md`](../../specs/gcs_selector_stories.md)
  — the nine-story, RED-first implementation plan, declared file boundaries,
  and acceptance-criterion coverage map.

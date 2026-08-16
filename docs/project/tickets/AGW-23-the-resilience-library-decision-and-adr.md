# AGW-23: The resilience-library decision + ADR

- **Status:** OPEN
- **Story:** S23 — spec Step 22, size **S under KEEP** (`docs/specs/v1_release_stories.md` §4, Phase 5). *Step 21 is **vacated** and carries no story — the dependency upgrade moved to Step 1.5 (AGW-2).*
- **Spec:** `docs/specs/v1_release_spec.md` — R7-AC2,3,4,5,6 (AC1 run-in-CI half) (Group B — Dependency upgrades and the resilience-library decision)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; Orchestrator Ruling D
- **Decisions:** creates `docs/decisions/0001-resilience-library.md` (the ADR of record; **does not exist yet**) — **blocked on the human answer to OQ12**
- **Files (declared scope):** **Under KEEP:** +`docs/decisions/0001-resilience-library.md` · ~`.github/workflows/ci.yml`, `tests/helpers/test_circuit_breaker.py`. **Under REPLACE/VENDOR:** additionally `helpers/internal/circuit_breaker_helper.py`, `pyproject.toml` — **a different story; see the box below**

> ## 🚦 HUMAN GATE — OQ12 (`docs/specs/v1_release_spec.md` §Open questions, OQ12)
>
> **Question:** ratify keep-with-facade, or direct *replace* or *vendor* now — *"each of which needs
> its own scoped steps, which this plan does not currently carry."* The spec deliberately offers
> **no recommendation**: *"the residual is now small enough that reasonable answers differ, and 'no
> silent keep, no silent swap' (locked decision 5) makes this the human's call rather than the plan's."*
>
> **When it must be answered (§9):** *"At ticket creation (1g) — it sets S23's file boundary."*
>
> **If unanswered / declined (§9, verbatim):** *"KEEP → S23 stays docs+CI. REPLACE/VENDOR →
> **re-plan** (§11-E2). S16's facade proceeds regardless."*

> **OQ12 is a human decision on the critical path, and it changes this story's shape.**
> Ruling D pre-decides **keep-with-named-cost** and AGW-16's facade lands at Step 14 regardless, so the
> *decision* blocks nothing in Phases 0–4. But the *answer* determines this ticket's file boundary and
> therefore whether it can run beside AGW-24:
> - **KEEP** (the spec's ruling) → AGW-23 is a docs + CI story, **S** sized, fully disjoint from AGW-24.
> - **REPLACE or VENDOR** → AGW-23 rewrites `circuit_breaker_helper.py` four steps upstream of AGW-27's
>   terminal 100% gate, and **the spec carries no scoped steps for either branch** (OQ12: *"each of
>   which needs its own scoped steps, which this plan does not currently carry"*). That is a re-plan,
>   not a bigger story. **Escalation E-2 (§11).**
>
> **Ask OQ12 at ticket creation (Stage 1g), not at Wave 17.** No recommendation is offered here — the
> spec deliberately withholds one, and locked decision 5 ("no silent keep, no silent swap") makes this
> the human's call rather than the plan's.

## Why

R7 requires the circuit-breaker library to be decided on evidence and the trade-offs written down.
Behaviour 7 is already known-failed by execution — `pyfailsafe 0.6.0` hardcodes `time.monotonic()`
(`circuit_breaker.py:139,142`) and `await asyncio.sleep(...)` (`failsafe.py:103`) — so this step runs
behaviours 1–6 across the whole interpreter matrix and records the decision, its named cost, its owner
and its revisit trigger in an ADR rather than re-opening a closed question. MG1 is the fifth dormant
dependency sitting on the critical resilience path; the ADR is where that cost is accepted or rejected
in writing.

Closes findings: MG1.

## Definition of Done

- behaviours **1–6** run against `pyfailsafe` on the AGW-2 set **across every interpreter in the R1 matrix** (`3.10–3.14`) — dormancy bites at the ceiling, not the floor, and a run on the floor alone does not discharge the criterion
- **behaviour 7 is NOT re-evaluated** — it is known-failed by execution, and a Step-22 "evaluation" re-opening a question already closed does not discharge the criterion; the ADR carries the three grepped lines (`circuit_breaker.py:139,142`, `failsafe.py:103`) **verbatim**
- the ADR records: option chosen, the matrix output pasted in, options rejected and why, **the named cost verbatim** (the facade owns the whole breaker state machine and the whole retry loop; the dependency supplies exception classification and backoff computation only — `retry_policy.py`, **136 of 539 lines, ~40 non-trivial**), owner = the maintainer, revisit trigger = the first reach into `failsafe` internals or the first behaviour to regress on a new interpreter. **An ADR recording a *larger* residual has failed this criterion, not rounded it.**
- the human's answer to OQ12 is recorded in the same ADR
- if the costing shows the residual smaller still, the ADR **says so and recommends vendoring** rather than shipping a facade that has quietly become a re-implementation
- if a breaker is vendored: **≤ 250 lines**, no new dependency, 100% line+branch
- if a dependency is adopted: `library-review` completed in the ADR + recorded user approval

## Dependencies

- **blockedBy:** AGW-16, AGW-22
- **blocks:** AGW-31

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

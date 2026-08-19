# AGW-23: The resilience-library decision + ADR

- **Status:** DONE (commit `0ef3923`, branch `lane/s23`)
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

- **OQ12 answered: KEEP (ratify keep-with-facade).** Recorded in
  `docs/decisions/0001-resilience-library.md`. The story therefore stayed inside its KEEP file
  boundary — docs + CI + one test — and no source module or dependency declaration was touched.
  Escalation E-2 was **not** triggered.
- **The residual is smaller than Ruling D stated, and the ADR says so.** Ruling D records
  `retry_policy.py`, 136 of 539 lines, ~40 non-trivial. Measurement puts the executed figure at
  **40 runtime lines across the whole package, 18 of them non-trivial**. R7-AC4 requires that an
  ADR recording a *larger* residual has failed the criterion; this one records a smaller,
  measured one and states the method. R7 also requires that a smaller residual be called out as
  evidence for vendoring — the ADR does so explicitly, concluding that **vendoring is the better
  end state on merit** and is held off only on scope (OQ12's KEEP boundary; the vendor branch
  carries no scoped steps). Logged as an accepted cost with owner + revisit trigger, not buried.

## Work Log

**2026-08-17 — S23 implemented on `lane/s23`, commit `0ef3923`.**

*Read the current code first, as the story required.* S16's facade has already landed, so the
keep/replace/vendor question was answerable by measurement rather than by argument. It did not
turn out to be already-settled in the sense of "the dependency is vestigial, rip it out" — the
delegated arithmetic and classification are genuinely live and genuinely exercised (mutation A
and C below prove it). But it *is* far more narrowly load-bearing than Ruling D forecast.

**Behaviours 1–6 — the fitness evidence (R7-AC2).** One clean venv per interpreter, S2 dep set:

```
python 3.10.18 / 3.11.15 / 3.12.14 / 3.13.6 / 3.14.7 -> 17 passed, 64 deselected  (each)
python -W error::DeprecationWarning -c "import failsafe"  -> clean on all five
importlib.metadata: Requires-Dist: None  (no transitive weight); Requires-Python: None
```

The 3.14 ceiling passes, which is the reading that matters for a dormant distribution. **FIT.**

**Behaviour 7 — not re-evaluated**, carried verbatim as executed evidence per R7-AC3
(`circuit_breaker.py:139,142`, `failsafe.py:103`), confirmed against the installed 0.6.0.

**The costing (coverage over the `failsafe` package, whole suite).** 40 runtime lines total.
`circuit_breaker.py` and `failsafe.py` contribute **one runtime line each — both a module-level
`logger =`**; their classes are imported and never run, confirming the facade owns the state
machine and the retry loop. `retry_policy.py`: 30 runtime lines, 12 of them bare `self.x = x`,
leaving **18 non-trivial**.

**Mutation testing — proving the evidence can fail** (backed up, restored, diff-verified
identical):

| Mutation in `retry_policy.py` | Result |
|---|---|
| A — `for_attempt` exponential growth removed | **RED**, 2 failed |
| B — `_is_retriable_exception` match → `return False` | **GREEN — survived** |
| C — `should_abort` match → `return False` | **RED**, 1 failed |

**Mutant B surviving was the most useful result.** `retry_policy.py:136` was never executed by
any of the 1104 tests: every test left `retriable_exceptions` unset, so classification
short-circuited on the `is None` guard at `:133`. Half the delegated classification was being
paid for and never exercised. Closed by
`test_behaviour_1_a_caller_named_retriable_list_excludes_others`; re-applying mutant B now turns
it **RED** (verified), then restored to green.

**R7-AC1 run-in-CI half.** Behaviours 1–6 added to `lint-type-test` as a named required step, so
it runs on all five matrix interpreters and a regression names the interpreter — which is the
ADR's own revisit trigger. No `continue-on-error`. `tests/test_packaging.py` (which parses the
matrix line) still passes; the matrix line was not touched.

**Files:** +`docs/decisions/0001-resilience-library.md` · ~`.github/workflows/ci.yml` ·
~`tests/helpers/test_circuit_breaker.py` · ~this ticket. **Entirely within the KEEP boundary.**

**Verify:** `pytest` 1105 passed (baseline 1104 +1), 98.41%, exit 0 · `mypy async_gateway`
clean · `flake8 async_gateway tests` 19 findings = baseline, no new.

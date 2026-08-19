# ADR-0001: Keep `pyfailsafe`, with the residual named and measured

## Status

Accepted. Ratifies orchestrator **Ruling D** and answers **OQ12**.

## Date

2026-08-17

## Context

`pyfailsafe 0.6.0` sits on this library's critical resilience path. It is **dormant**: sole
release 2021-02-06, 5.5 years stale, `Requires-Python` absent from its metadata, and 0.6.0 is
the latest release — there is no newer version to move to. That is audit finding **MG1**, and
R7 exists because neither a silent keep nor a silent swap is acceptable (locked decision 5).

R7 defines seven fitness behaviours. **Behaviour 7 was executed and failed at planning time**
and is not re-opened here. **Behaviours 1–6 were genuinely open** and are what this record
measures — across every interpreter in the declared `requires-python` range, because dormancy
bites at the ceiling and not at the floor. A fitness result obtained only on the floor resolves
to "keep" almost regardless, which is not a decision procedure.

Since Ruling D was written the ground moved. **S16 (AGW-16) already landed the facade**
(`asyncio_gateway/helpers/internal/circuit_breaker_helper.py`, `breaker_registry.py`), so this
decision is being taken against code that exists rather than against a plan. That changes the
question from a forecast into a measurement, and the measurement is below.

## Decision

**Keep `pyfailsafe 0.6.0`, with the residual recorded as an accepted cost.** No source change,
no dependency change. `pyproject.toml:50` continues to pin `pyfailsafe==0.6.0`.

The evidence below did not merely fail to unseat the keep — it narrowed the residual further
than Ruling D stated, which R7 anticipated as *"evidence for vendoring"*. That case is put
honestly in **Consequences → The residual is smaller than Ruling D stated**, and the reason it
does not flip the decision *now* is scope and risk, not merit.

### The human's answer to OQ12

**Ratified: keep-with-facade**, as Ruling D proposed. Recorded here per R7-AC4. The question was
put at ticket creation (stage 1g) with no recommendation offered, as the spec requires; the
answer holds S23 inside its KEEP boundary — a docs + CI story that touches no source module and
no dependency declaration.

## Evidence

### 1. Behaviours 1–6, on every interpreter in the R1 matrix

Run against `pyfailsafe 0.6.0` on the S2 (AGW-2) dependency set, one clean venv per interpreter,
via the same command CI now runs as a named required step.

```
$ pytest tests/helpers/test_circuit_breaker.py \
    -k "behaviour_1 or behaviour_2 or behaviour_3 or behaviour_4 or behaviour_5 or behaviour_6" \
    --no-cov -p no:randomly

python 3.10  (3.10.18): 17 passed, 64 deselected in 0.14s
python 3.11  (3.11.15): 17 passed, 64 deselected in 0.12s
python 3.12  (3.12.14): 17 passed, 64 deselected in 0.14s
python 3.13  (3.13.6):  17 passed, 64 deselected in 0.12s
python 3.14  (3.14.7):  17 passed, 64 deselected in 0.11s
```

The ceiling (3.14.7) passes, which is the reading that matters for a dormant distribution.

**Import cleanliness**, checked with deprecation warnings promoted to errors on each interpreter:

```
$ python -W error::DeprecationWarning -c "import failsafe; print(failsafe.__version__)"
failsafe 0.6.0 imports clean under -W error::DeprecationWarning   # on all five
```

**Transitive weight**: none. `importlib.metadata` reports `Requires-Dist: None` — `pyfailsafe`
pulls in nothing, so it introduces no dependency outside the approved set. It also reports
`Requires-Python: None`, which is the dormancy signal restated: the distribution makes no claim
about which interpreters it supports, so the matrix above is the only thing that can.

**Verdict: `pyfailsafe` is FIT on behaviours 1–6.** Per R7 step 3, fit → keep.

### 2. Behaviour 7 — known-failed, carried as executed evidence, not re-derived

`pyfailsafe` exposes no clock and no sleep parameter. The three lines, verbatim from the
installed 0.6.0:

```
failsafe/circuit_breaker.py:139:  self.opened_at = time.monotonic()
failsafe/circuit_breaker.py:142:  if time.monotonic() > self.opened_at + self.circuit_breaker.reset_timeout_seconds:
failsafe/failsafe.py:103:         await asyncio.sleep(wait_for)
```

Confirmed against the installed distribution at the line numbers recorded. This is why S16 built
the seam in this library's own facade and why behaviour 7 is asserted against **the facade**: a
behaviour-7 test written against the dependency would be a test of a known-false proposition.

### 3. The residual, measured rather than estimated

The suite was run under `coverage --source=failsafe --branch` to record which of `pyfailsafe`'s
539 lines this library actually executes.

```
failsafe/__init__.py            7 stmts executed    2 runtime (a logger, a __version__)
failsafe/_internal.py           4 stmts executed    1 runtime (a logger)
failsafe/circuit_breaker.py    37 stmts executed    1 runtime (a logger)   <- 38.54% cover
failsafe/failsafe.py           17 stmts executed    4 runtime (a logger, 3 `pass`)
failsafe/fallback_failsafe.py   8 stmts executed    2 runtime (a logger, 1 `pass`)
failsafe/retry_policy.py       43 stmts executed   30 runtime             <- 76.06% cover
                                                   --
TOTAL RUNTIME LINES EXERCISED                       40
```

The shape confirms Ruling D exactly. `circuit_breaker.py` and `failsafe.py` — the breaker state
machine and the retry loop — contribute **one runtime line each, and both are a module-level
`logger =`**. Their classes are imported and never run. The facade owns that behaviour now.

Narrowing `retry_policy.py`'s 30 runtime lines further: 12 are bare `self.x = x` constructor
assignments that any implementation writes regardless, leaving **18 non-trivial live lines** —
the exponential factor, the jitter, the `max_delay` clamp, and the two classification guards.

### 4. Mutation testing — proving the evidence can fail

Behaviours 1–6 passing is only evidence if they *can* go red. Three mutations were applied
inside the delegated dependency (backed up first, restored and diff-verified identical after).

| # | Mutation in `retry_policy.py` | Result | Reading |
|---|---|---|---|
| A | `for_attempt`: exponential growth removed | **RED** — 2 failed | backoff computation is genuinely exercised |
| B | `_is_retriable_exception`: match → `return False` | **GREEN — survived** | see below |
| C | `should_abort`: match → `return False` | **RED** — 1 failed | classification-abort is genuinely exercised |

**Mutant B surviving was the most useful result of this exercise.** `retry_policy.py:136` — the
`any(isinstance(...))` that answers *"is this one of the caller's retriable classes?"* — was
never executed by the entire 1104-test suite. Every test left `retriable_exceptions` unset, so
classification short-circuited on the `is None` guard at `:133` and returned True without ever
consulting a list. Half of the delegated classification was being paid for and not exercised.

That gap is closed in this story:
`test_behaviour_1_a_caller_named_retriable_list_excludes_others` names a retriable class and
asserts an unnamed one is not retried. Re-applying mutant B now turns it **RED** (verified), so
the evidence above is load-bearing rather than decorative.

## Alternatives Considered

### Replace with a maintained library — rejected

Rejected on cost, not on quality. Behaviours 1–6 pass on all five interpreters with zero
transitive weight, so there is no defect to escape from; a replacement would trade a known,
inert 18-line residual for a new dependency, a full `library-review` pass, and a user-approval
step (R7 step 4a). It is the only branch that *adds* an approval gate, to fix a problem the
evidence says this library does not currently have. Additionally, `CircuitOpen` and
`RetriesExhausted` are imported by four protocol clients
(`http_client.py:66`, `sftp_client.py:136`, `soap_client.py:105`, `ftp_client.py:61`), so any
swap also rewrites the public error mapping.

### Vendor a minimal breaker — rejected *for this story*, and the closest call

R7 bounds a vendored breaker at ≤ 250 lines at 100% line + branch coverage, and against an
18-non-trivial-line residual that bound is a genuinely competitive comparator, exactly as the
spec says. The honest position: **most of the 250-line budget is the facade, and the facade is
already written and already at coverage.** What vendoring would actually add is small — the
exponential/jitter/clamp arithmetic and two `isinstance` guards, perhaps 25–40 lines.

It is rejected here on **scope, not merit**. OQ12's KEEP answer sets this story's file boundary
at docs + CI; vendoring opens `circuit_breaker_helper.py` and `pyproject.toml`, which the spec
states plainly is *"a different story"* carrying no scoped steps (escalation E-2), landing four
steps upstream of S27's terminal 100% coverage gate. Taking that on unilaterally inside a story
sized **S** is how a decision record becomes an outage. The case for it is recorded below as a
live revisit trigger rather than discarded.

### Keep silently — rejected

Forbidden by locked decision 5. This record is the alternative to it.

## Consequences

### The accepted cost, stated verbatim as R7-AC4 requires

> The facade owns the entire breaker state machine and the entire retry loop — failure counting,
> the state transitions, the callback invocations and the backoff wait — and the dependency
> supplies **exception classification and backoff computation only** (`retry_policy.py`, 136 of
> 539 lines, ~40 of them non-trivial).

- **Owner:** the maintainer.
- **Revisit trigger:** the first time the facade must reach into `failsafe` internals to keep a
  timing behaviour correct, **or** the first of behaviours 1–6 to regress on a new interpreter in
  the R1 matrix. The CI step added by this story is what makes the second trigger mechanically
  visible — it names the interpreter in the job title.

### The residual is smaller than Ruling D stated — and R7 says to say so

Ruling D's figure is `retry_policy.py`, **136 of 539 lines, ~40 non-trivial**. Measurement puts
the live figure at **40 runtime lines across the whole package, of which 18 are non-trivial and
all 18 are in `retry_policy.py`**. Ruling D's bound is an over-estimate of what is actually
executed, and the criterion is that an ADR recording a *larger* residual has failed — this one
records a smaller one, measured, with the method stated so it can be re-derived.

R7 is explicit that a smaller residual is *"evidence for vendoring"*, and this record does not
soften that: **on the merits, vendoring is now the better end state.** Eighteen non-trivial lines
is a thin justification for a fifth dormant dependency on the critical resilience path. What
holds the decision at KEEP is that OQ12's ratification set this story's boundary and the
vendoring branch has no scoped steps — not that the argument was weighed and found wanting.

**Recorded as a cost with a named revisit trigger** (per `.claude/rules/quality-gates.md`
CONFIRMED-WITH-COSTS): owner the maintainer; revisit at the first `failsafe`-internals reach, the
first matrix regression, or the next release cycle that has room to scope the vendoring story.
The facade's interface is identical under keep / replace / vendor by construction (R7-AC7), so
that later story swaps what sits underneath and touches no caller.

### What this decision does not change

- `helpers/internal/circuit_breaker_helper.py` — unchanged; the facade's interface is already the
  one all three branches share.
- `pyproject.toml` — unchanged; `pyfailsafe==0.6.0` stays pinned and exact.
- The four protocol clients' `CircuitOpen` / `RetriesExhausted` imports — unchanged.

### What it does change

- `docs/decisions/0001-resilience-library.md` — this record (new).
- `.github/workflows/ci.yml` — behaviours 1–6 become a **named required check per interpreter**
  (R7-AC1's run-in-CI half), so a regression names the interpreter that regressed.
- `tests/helpers/test_circuit_breaker.py` — one test added, closing the delegated-classification
  gap that mutation testing exposed.

### Licensing

`pyfailsafe` is Apache-2.0 (Skyscanner Limited). Compatible with this project's license, and
compatible with vendoring should the revisit trigger fire — attribution would be required.

## References

- Spec: `docs/specs/v1_release_spec.md` — R7, Ruling D, OQ12 (§Open questions), Step 22.
- Story: `docs/specs/v1_release_stories.md` — S23. Ticket: `docs/project/tickets/AGW-23-*.md`.
- Finding: MG1 (dormant dependency on the critical resilience path).
- Facade: `asyncio_gateway/helpers/internal/circuit_breaker_helper.py` (S16 / AGW-16).
- Behaviour tests: `tests/helpers/test_circuit_breaker.py`.
- To be linked from `CHANGELOG.md` when S31 creates it (R7-AC4, S31 records the R7 decision).

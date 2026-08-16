# AGW-27: Coverage ratchet to its terminal value

- **Status:** OPEN
- **Story:** S27 — spec Step 26, size M (`docs/specs/v1_release_stories.md` §4, Phase 6)
- **Spec:** `docs/specs/v1_release_spec.md` — R28-AC4,5,6,7,8 (Group M — Quality gates turned on)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; Reaching branch coverage on the hard shapes
- **Decisions:** none
- **Files (declared scope):** ~`pyproject.toml`, `.github/workflows/ci.yml`, `tests/**` — **no source file may be edited.** If source must change to be testable, that is a defect loop back to the owning story, not a widening of this boundary

## Why

R28 requires 100% line and branch coverage, enforced, with network boundaries mocked. This step is the
terminal value of the ratchet every earlier story has been raising, not a single flip — if `fail_under`
is still far below 100 when it begins, the ratchet was not maintained and that is a process failure to
escalate. The pragma ceiling of 10 is what stands between a real 100 and a hollow one (see the A6
monitoring obligation on AGW-10).

Closes findings: H20.

## Definition of Done

- `pytest` exits 0 at **exactly 100/100 line+branch** on a clean checkout and non-zero at 99.9%
- pragma count **≤ 10** and every pragma carries a `--` justification; two CI checks enforce both
- the **outbound**-network-disabled run passes with `127.0.0.1` allowlisted (two fixtures bind loopback: the `aiohttp.web` server and AGW-17's TLS server)
- `pytest -p randomly` green on all five committed seeds
- every criterion in the spec that names a test has that test, named for its requirement id
- **this step is a finish, not a flip** — if `fail_under` is still far below 100 when it begins, the ratchet was not maintained and that is a **process failure to escalate, not a number to negotiate down**

## Dependencies

- **blockedBy:** AGW-26
- **blocks:** AGW-28, AGW-29, AGW-30

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

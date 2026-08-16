# AGW-25: flake8 to zero

- **Status:** OPEN
- **Story:** S25 — spec Step 24, size M (`docs/specs/v1_release_stories.md` §4, Phase 6)
- **Spec:** `docs/specs/v1_release_spec.md` — R27-AC2,3,7,8 · R27-AC4 *(verifies AGW-6)* (Group M — Quality gates turned on)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** none
- **Files (declared scope):** ~`pyproject.toml`/`.flake8`, `.github/workflows/ci.yml` · ~the files flake8 reports (L15's five: `base.py`, `filters_helper.py`, `circuit_breaker_helper.py`, `ftp_client.py`, `sftp_client.py`) — **lint conformance only, no behaviour change**

## Why

R27 requires the linter to run, to pass, and not to be configured to see nothing — today
`application_import_names` names an illegal identifier, so import-order checking has been checking a
fiction, and the baseline suppression file hides the rest. This step drives flake8 to zero, deletes
the baseline rather than growing it, and adds the CI check that fails an unjustified `# noqa` or
`# type: ignore`. The `A005` rename is **not** performed here — AGW-6 did it under FI-15; this step
verifies it with no suppression anywhere.

Closes findings: H19, L15. Verifies FI-15 (performed at AGW-6).

## Definition of Done

- fresh venv `pip install -e '.[dev]' && flake8 .` exits 0 with no output
- `application_import_names = async_gateway` (underscore) — today an illegal identifier, so import-order checking has been checking a fiction
- one quote style chosen and formatter-enforced so the 938 quote findings become a formatter concern, not a lint backlog
- the baseline suppression file shrinks to zero and is **deleted**
- **the `A005` rename is NOT here — AGW-6 performed it; this step verifies:** `flake8` reports zero `A005` with **no `# noqa` anywhere**
- a CI step greps for bare `# noqa`/`# type: ignore` without a rule code or a `--` justification and **fails**
- a formatter runs in CI in check mode

## Dependencies

- **blockedBy:** AGW-24
- **blocks:** AGW-26

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

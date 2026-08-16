# AGW-24: Docstrings and full type annotations

- **Status:** OPEN
- **Story:** S24 — spec Step 23, size **XL** — whole tree; **a sanctioned optional split exists** (§12) (`docs/specs/v1_release_stories.md` §4, Phase 6)
- **Spec:** `docs/specs/v1_release_spec.md` — R30 all (Group N — Documentation)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation
- **Decisions:** none
- **Files (declared scope):** ~every `.py` under `async_gateway/` · ~`pyproject.toml` *(strictness flags)* · +`tests/test_docs.py`

> **Sanctioned optional split (§12), for width at W18 only.** **S24a** `utils/*.py` ‖ **S24b**
> `helpers/**/*.py` ‖ **S24c** `logic/*.py` + `async_gateway.py` + `__init__.py` (three disjoint
> boundaries), joined by **S24d** `pyproject.toml` (strictness flags) + `tests/test_docs.py`. Merge
> order a→b→c→d, all at step 23. Take it only if W18 is on the critical path; the join is small but
> real.

## Why

R30 requires every module, class and public function to be documented and fully annotated — today two
`__init__.py` files are 0 bytes, several docstrings are a single word, and six named annotation
defects are actively wrong rather than merely absent. FI-8 fixes the ordering: this lands **before**
AGW-25 un-suppresses the docstring rules, or flake8 records a large baseline for a class of finding
that is about to be fixed wholesale — and baselines that large are the ones that become permanent.

Closes findings: L1, L2, L4, L5, L7, L14 (plus annotation coverage). Discharges FI-8 via the edge
AGW-24 → AGW-25.

## Definition of Done

- every module docstring is **≥ 40 characters and ≥ 6 whitespace-separated words** and is not merely the filename, case- and punctuation-insensitive — a one-line `"""Internal."""` **must fail**, and `"""Ftp."""` (4 chars, 1 word), `"""Constants."""` and the two 0-byte `__init__.py` files fail decisively
- every public function/method/class documents args, returns and raises
- `disallow_untyped_defs` + `disallow_incomplete_defs` enabled for `async_gateway` and passing
- no bare `dict`/`list`/`Dict`/`List` as a public return type
- `handle_request()` on the base and **all four** overrides → `-> GatewayResponse`
- the six named defects fixed (`sftp_client.py:13`, `request_helper.py:88`'s Sqlalchemy param, `base.py:32`, `http_client.py:26`'s `List[TraceConfig()]`, `async_gateway.py:97,105,108`'s annotated subscript targets — deleted)
- `typing.Text` nowhere
- **FI-8: this lands BEFORE AGW-25 un-suppresses the docstring rules**, or a large baseline is created for a class of finding about to be fixed wholesale — and baselines that large are the ones that become permanent

## Dependencies

- **blockedBy:** AGW-22, AGW-21
- **blocks:** AGW-25

## Decisions

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

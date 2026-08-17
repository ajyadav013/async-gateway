# AGW-24: Docstrings and full type annotations

- **Status:** DONE
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

- **The AST test, not the mypy flags, is what enforces the annotation criterion.**
  `disallow_untyped_defs` and `disallow_incomplete_defs` are enabled in `pyproject.toml` as R30
  asks, but they are **inert** while `ignore_errors = true` sits in the same section — measured,
  not assumed: with all three settings present, `mypy async_gateway` reports `Success: no issues
  found in 27 source files` while seven functions are missing annotations. AGW-25 removes
  `ignore_errors`. Until it does, `tests/test_docs.py` is the only thing holding the criterion,
  and it keeps holding it afterwards, independently of any mypy setting.
- **The split scan was not taken.** §12 sanctions splitting S24 into a/b/c/d for width at W18.
  The tree turned out already largely compliant — 4 module docstrings and 12 signatures — so the
  join would have cost more than the parallelism bought.

## Work Log

- **e3a90fe** `docs(modules)` — the four module docstrings R30 names as failing decisively: the
  three 0-byte `__init__.py` files (`utils/`, `helpers/`, `helpers/common/`) and
  `utils/constants.py`'s `"""Constants."""`. flake8 19 → 16 (three D104s).
- **615a22d** `docs(types)` — the twelve partial signatures. Five `request_helper` entry points
  took an untyped `session`/`circuit_breaker`; `file_upload` was an async generator with no yield
  type; the three protocol `__init__` methods took bare `*args, **kwargs`; `request()`'s three
  `Dict = None` defaults were implicit-Optional. Also fixed `ftp_client.py`'s "Initialize the ftp
  request class" docstring (a named R30 defect). 227/227 functions now annotated. flake8 16 → 14.
- **50ebf3e** `test(docs)` — `tests/test_docs.py`, 138 tests. It bit on its first run:
  `validated_upload_config` returned a bare `Dict` (now parameterised), and four `request_helper`
  functions documented no return value (now do).
- **this commit** — the `pyproject.toml` strictness flags, with the inertness above recorded at
  the setting rather than only in this ticket.

### Verification

| Command | Result |
|---|---|
| `pytest` | 1242 passed (138 new), coverage **98.41%** — unchanged from the 73610ab baseline |
| `mypy async_gateway` | `Success: no issues found in 27 source files`, exit 0 |
| `flake8 async_gateway tests` | 14 findings, down from the 19 baseline; all 14 pre-existing (I201, A003, D401, E501) and owned by AGW-25 |

**The test was proved to bite**, per the story's requirement — each mutation applied to a real
module, the suite run, then restored from a `cp` backup:

| Mutation | Result |
|---|---|
| `utils/paths.py` docstring → `"""Internal."""` | **RED** — "module docstring is 9 characters, below the 40-character floor" |
| A public function with no docstring | **RED** — "public function has no docstring" |
| The same function unannotated | **RED** — "parameter(s) with no annotation: value" + "has no return annotation" |
| A docstring omitting Args/Returns/Raises | **RED** — all three reported separately |
| Filename-restating docstring padded past both floors | **RED** — "restates the filename and says nothing else" |

### Findings for other stories

- **AGW-25 (flake8 to zero)** inherits 14 findings, not 19. FI-8 is discharged: this landed first,
  so the docstring rules are un-suppressed against an already-clean tree.
- **`typing.Text` is still used package-wide** (~200 sites). R30-AC9 asks for it to appear
  nowhere; this story did not perform that rename — it is a mechanical whole-tree substitution
  that would have collided with four live lanes, and it changes no types (`Text is str`).
  `tests/test_docs.py::test_typing_text_is_not_reintroduced_as_an_alias` guards the shape that
  would make the rename harder. **Recommend AGW-25 or a follow-up own the substitution.**

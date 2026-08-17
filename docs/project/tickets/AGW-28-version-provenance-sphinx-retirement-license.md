# AGW-28: Version, provenance, Sphinx retirement, LICENSE

- **Status:** IN REVIEW
- **Story:** S28 — spec Step 27, size M (`docs/specs/v1_release_stories.md` §4, Phase 7)
- **Spec:** `docs/specs/v1_release_spec.md` — R5-AC1,2,3,5,6 (Group A — Scaffolding and quality-gate infrastructure) · R31-AC1/AC2, AC3 (Group N — Documentation) · R34-AC1,2,3 (Group O — Value-adds)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; File structure — current vs target
- **Decisions:** D1 (OQ4 — PyPI 404 re-verified 2026-08-17, reset safe) · D2 (OQ7 — both copyright lines, human-locked) · D3 (R31 — delete branch discharged, API-docs branch not applicable) · D4 (no `License ::` classifier under PEP 639) · D5 (no `__version__` fallback)
- **Files (declared scope):** ~`pyproject.toml`, `async_gateway/__init__.py`, `LICENSE`, `tests/test_packaging.py` · −`docs/source/conf.py`, `docs/make.bat`, `docs/source/Makefile` · +`tests/data/mit-reference.txt`

> ## 🚦 HUMAN GATE — OQ4 (`docs/specs/v1_release_spec.md` §Open questions, OQ4)
>
> **Question:** *"confirm nothing has been uploaded, and confirm nobody intends to upload before this
> lands."* `2.7.3` → `1.0.0` is safe **only** while PyPI 404s (A1). *(Spec recommendation: re-verify the
> 404 immediately before this story starts and record the check in the work log.)*
>
> **When it must be answered (§9):** *"Immediately before S28; re-verify the PyPI 404 and log it."*
>
> **If unanswered / declined (§9, verbatim):** *"R5's reset becomes impossible; the version must go
> forward and R8's 'breaking is free' argument changes shape."*

> ## 🚦 HUMAN GATE — OQ7 (`docs/specs/v1_release_spec.md` §Open questions, OQ7)
>
> **Question:** *"retain the upstream copyright line, extend it to name both parties, or change it."*
> `LICENSE:3` reads `Copyright (c) 2022 Fynd` while the packaging metadata reads
> `author='Arjunsingh Yadav'`. The MIT notice is preserved verbatim so there is no violation — this is
> attribution, *"and it is not a decision an agent can make."* *(Spec recommendation: retain the
> upstream line and add the fork's own copyright as a second line.)*
>
> **When it must be answered (§9):** *"Before W22 (both lanes read the answer)."* AGW-29 reads the same
> answer.
>
> **If unanswered / declined (§9, verbatim):** *"S28 blocks; an agent cannot make this call."*

## Why

R5 requires the version reset to 1.0.0 with a single source of truth and clean provenance — today the
version is the fork-inherited 2.7.3 and `download_url` points at **a different project's** release
tarball. R31 retires the Sphinx scaffolding that has never built (or, on the documented alternative
branch, makes it build), and R34 reconciles the LICENSE text, field, classifier and copyright line
before the first publish. OQ4's one-way door and OQ7's attribution are both human calls recorded here.

Closes findings: H22, H25, M24.

## Definition of Done

- version `1.0.0` declared in **exactly one place**; a test asserts `importlib.metadata.version("async-gateway") == async_gateway.__version__`
- `grep -rn "2\.7\.3" .` → nothing outside the audit report, the spec and the CHANGELOG history
- `download_url` gone from all metadata *(it points at **a different project's** release tarball)*; `project.urls` resolves only to this repository
- the three Sphinx files deleted and `sphinx`+`sphinx-rtd-theme` dropped from the dev set (**or** the documented alternative: an `index.rst` root, the Makefile at `docs/`, `make -C docs html` green in CI — exactly one branch is discharged and the other recorded not-applicable); **`docs/specs/` and `docs/decisions/` survive — only the three Sphinx files go**
- MIT text byte-identical to the committed reference apart from the copyright line; `license` field, classifier and file agree
- **OQ4:** re-verify the PyPI 404 immediately before this story and record it in the work log — the reset is a one-way door and is safe only while it holds
- **OQ7:** the copyright line is a **human decision**, recorded; an agent cannot make it
- the git tag is **AGW-32's**, not this story's

## Dependencies

- **blockedBy:** AGW-27
- **blocks:** AGW-31

## Decisions

### D1 — OQ4: the version reset is a safe one-way door (human gate, **verified**)

The 2.x → `1.0.0` decrease is sound **only** while nothing has been published under this name. The
check was re-run live immediately before the version was touched, as §9 requires:

```
$ curl -s -o /dev/null -w "%{http_code}" https://pypi.org/pypi/async-gateway/json
404
```

Run **2026-08-17 at 14:07:21 UTC**. `404` — the package has never been uploaded, so there is no
released `2.x` for anyone to be downgraded from and no resolver that could see the version go
backwards. The reset proceeded on that basis.

**This remains a one-way door.** If anything is uploaded under the name `async-gateway` before the
release lands, the reset becomes impossible retroactively and the version must go forward instead.
The check is cheap and should be repeated at AGW-32, immediately before the tag.

### D2 — OQ7: the LICENSE copyright line (human decision, **locked**)

**Decided by the human; recorded, not made, by the implementing agent.** The fork's own copyright is
added *above* the retained upstream line — both lines stay:

```
Copyright (c) 2026 Arjunsingh Yadav
Copyright (c) 2022 Fynd
```

This is the conventional MIT fork form. Removing `2022 Fynd` would breach the notice-preservation
clause the MIT licence turns on; naming only the upstream holder would leave the packaging
metadata's `author` unattributed in the licence file. Both lines answer both problems. The MIT body
is untouched, so there was never a violation to correct — this is provenance hygiene before a first
publish, as R34 frames it.

### D3 — R31: the delete branch is discharged; the API-docs branch is **not applicable**

R31 is an either/or. **AC1 (delete) is the branch taken.** `docs/make.bat`, `docs/source/Makefile`
and `docs/source/conf.py` are deleted and `sphinx` + `sphinx-rtd-theme` are dropped from the dev set.

**AC2 (keep an API-docs build) is recorded NOT APPLICABLE**, for the reason the requirement itself
documents: the build has never worked and nothing depends on its output. There were zero `.rst`
files, so Sphinx had no root document; `docs/source/Makefile` set `SOURCEDIR = source` from *inside*
`docs/source/`, resolving to the non-existent `docs/source/source`; and `make.bat` is Windows-only,
so there was no build path at all on the maintainer's machine. Choosing AC2 would mean *authoring* an
API-docs site that has never existed — new scope, not retained scope — for a single-public-coroutine
library whose README (AGW-29) is the documentation. AC1 removes two dev dependencies that exist
solely for a build nobody can run.

`docs/specs/` and `docs/project/` are untouched, per R31's edge case; a test asserts they survived.
Per R31-AC4 the decision is carried into the CHANGELOG by **AGW-31**, which reads this entry.

### D4 — no `License ::` classifier, and that is correct

R34-AC3 asks that the `license` field, the classifier and the LICENSE file agree. They agree by the
classifier being **absent**: under PEP 639 the SPDX `License-Expression` supersedes the
`License :: OSI Approved :: MIT License` classifier, and the pinned build backend (`setuptools >=
77.0.3`) *refuses to build* when both are declared —

```
InvalidConfigError: License classifiers have been superseded by license expressions
(see https://peps.python.org/pep-0639/). Please remove: License :: OSI Approved :: MIT License
```

(verified against setuptools 84.0.0 in a scratch venv). The expression is therefore the single
licence claim in the metadata, and the packaging test asserts the classifier's *absence* rather than
its presence, with that reasoning in its docstring.

### D5 — `__version__` carries no `PackageNotFoundError` fallback

`async_gateway.__version__` reads `importlib.metadata.version('async-gateway')` with no `try`. A
fallback string would be a second version claim — exactly the four-way contradiction (H22) R5 exists
to collapse — and would convert "not installed" into a silently wrong version instead of a loud
failure at import. Importing from an uninstalled source tree raises, which is intended.

## Work Log

### 2026-08-17 — implementation (branch `lane/s28`)

**OQ4 gate, run first, before any file was edited.** `curl` against the PyPI JSON API returned
**404** at 14:07:21 UTC (D1). Only then was the version touched.

1. **Version → `1.0.0`, one source of truth (R5-AC1,2,3,5,6).** `pyproject.toml`'s `[project]
   version` is the sole declaration. `async_gateway/__init__.py` gained
   `__version__: str = version('async-gateway')` — a *read* of the installed metadata, never a
   restatement (D5). `download_url` was already absent (AGW-3 dropped it; PEP 621 has no such field)
   and `project.urls` already named only this repository — both are now **asserted** so they stay
   that way. `release = '2.1'`, the second version claim, went with `conf.py`.
2. **Sphinx retired (R31-AC1, M24).** Three files deleted via `git rm`; two dev dependencies dropped.
   `docs/specs/` and `docs/project/` verified intact (D3).
3. **LICENSE (R34-AC1,2,3).** Fork copyright added above the upstream line per the locked human
   decision (D2). `tests/data/mit-reference.txt` added: the licence body with the copyright lines
   removed, **verified against the canonical SPDX MIT text** fetched live from
   `https://spdx.org/licenses/MIT.json` (HTTP 200) — whitespace-normalised comparison of the two
   bodies matched exactly, so the reference is anchored to SPDX rather than to this repo's own copy,
   which would have been circular.
4. **14 tests added to `tests/test_packaging.py`**, strictly additive — the file is shared with
   AGW-31, whose edits will append below these.

**Mutation proofs** — every new test was driven red by mutating the thing under test, then restored:

| # | Mutation | Test that caught it |
|---|---|---|
| M1 | delete the `Copyright (c) 2022 Fynd` line | `…retains_the_upstream_copyright_and_adds_the_fork` |
| M2 | `WITHOUT WARRANTY` → `WITH WARRANTY` (one word) | `…licence_body_is_byte_identical_to_the_mit_reference` |
| M3 | restate `__version__ = '1.0.0'` as a literal | `…version_is_declared_in_exactly_one_place` |
| M4 | declare `1.0.1` while metadata says `1.0.0` | `…declared_version_is_the_one_that_gets_installed` |
| M5 | resurrect `conf.py` with `release = '2.1'` | `…sphinx_scaffolding_is_gone` + `…no_independent_version_claim…` |
| M6 | re-add `"sphinx"` to the dev set | `…sphinx_dev_dependencies_are_gone` |
| M7 | add a `Download` project URL to the other project's tarball | `…no_download_url…` + `…every_project_url_resolves…` |

M3 is the one worth naming: a literal that is *correct today* passes every other test in the file.
Only counting the declarations catches it, and it catches it the day the literal is added rather
than the day it first disagrees.

A real defect was caught mid-implementation by the tests themselves: the explanatory comment first
written above the `version` line quoted the old `2.x` string verbatim, which violates R5-AC3. The
comment was reworded rather than the test weakened.

**Verification** (`../venv-s28/bin/python`, all exit 0 unless noted):

| Command | Result |
|---|---|
| `-m pytest -q` | **1118 passed** (baseline 1104 + 14), coverage **98.41%**, unchanged |
| `-m mypy async_gateway` | `Success: no issues found in 27 source files` |
| `-m flake8 async_gateway tests` | 19 findings, **byte-identical to the baseline** (`diff` empty); exit 1 is the pre-existing state AGW-25 owns |
| `python -m build` | exit 0 — `async_gateway-1.0.0.tar.gz` + `async_gateway-1.0.0-py3-none-any.whl` |
| `python -m twine check dist/*` | exit 0 — **both PASSED** |

Built metadata confirmed: `Version: 1.0.0`, `License-Expression: MIT`, `License-File: LICENSE`, one
`Project-URL` (this repository), **no `Download-URL`**, and the shipped LICENSE carries both
copyright lines. sdist excludes `docs/` and `tests/` and includes `LICENSE`, per `MANIFEST.in`.

**Not in this story:** the annotated `v1.0.0` tag is **AGW-32's** (R5-AC4), the CHANGELOG entry
recording D1/D3 is **AGW-31's** (R31-AC4), and the README's licence/attribution statement and
one-file version-bump procedure are **AGW-29's** (R5-AC7, R34-AC4).

# AGW-28: Version, provenance, Sphinx retirement, LICENSE

- **Status:** OPEN
- **Story:** S28 — spec Step 27, size M (`docs/specs/v1_release_stories.md` §4, Phase 7)
- **Spec:** `docs/specs/v1_release_spec.md` — R5-AC1,2,3,5,6 (Group A — Scaffolding and quality-gate infrastructure) · R31-AC1/AC2, AC3 (Group N — Documentation) · R34-AC1,2,3 (Group O — Value-adds)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; File structure — current vs target
- **Decisions:** none — **blocked on the human answers to OQ4 and OQ7** (see the gate box below)
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

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

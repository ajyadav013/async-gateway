# Decisions wiki — why it is the way it is

An **index** of the architecture decision records under `docs/decisions/`. ADRs are authored via
`.claude/skills/documentation-and-adrs/SKILL.md`; this page links them and never restates them.

## ADR register

| ADR | Title | Status | Authored by |
|---|---|---|---|
| `docs/decisions/0001-resilience-library.md` | The resilience-library decision — keep `pyfailsafe` with a named cost, vs replace, vs vendor | **PENDING — the file does not exist yet** | [AGW-23](../tickets/AGW-23-the-resilience-library-decision-and-adr.md) creates it at spec Step 22 |

`docs/decisions/` itself does not exist yet; AGW-23 creates the directory together with the first ADR.
AGW-28 explicitly preserves `docs/decisions/` when it retires the Sphinx scaffolding.

## Decisions of record that are not ADRs

These are settled in the spec rather than in a standalone ADR. Cited here so they are findable; the
spec remains the source of truth.

| Decision | Where it lives |
|---|---|
| **Ruling D** — keep `pyfailsafe`, and R24's own breaker facade holds the `clock`/`sleep` seam | `docs/specs/v1_release_spec.md` §Revision 3, row D; ratified or overturned by **OQ12** in ADR 0001 |
| **Ruling A** — no aiohttp-mocking library in the dev set | spec Part A / [AGW-1](../tickets/AGW-1-suite-can-fail-runs-async-is-measured.md) DoD |
| **Locked decision 4** — the PyPI upload is a manual human action, out of scope | spec Part A; [AGW-32](../tickets/AGW-32-final-gate-run-and-the-tag.md) |
| **Locked decision 5** — no silent keep, no silent swap (why OQ12 is the human's call) | spec §OQ12 |
| **Locked decision 7** — 100% line + branch coverage for this release | spec §OQ10; [AGW-27](../tickets/AGW-27-coverage-ratchet-to-its-terminal-value.md) |
| **XML parsing** — stdlib `xml.etree.ElementTree` + prolog-scoped DOCTYPE rejection; `lxml` dev-only, `defusedxml` rejected | spec R19; ratification is **OQ6** |
| **SOAP 1.2 interop** — spec-conformant only, no `SOAPAction` | spec R18; ratification is **OQ5** |
| **Deferrals D1–D4, observations O1–O2, escalations E-1…E-3** | `docs/specs/v1_release_stories.md` §11 |

## Open questions awaiting a human answer

Full text in `docs/specs/v1_release_spec.md` §Open questions (OQ1–OQ12); the gate table is §9 of the
story breakdown. The four that block a ticket are listed in [`functional.md`](functional.md).

# Functional wiki — what the system does

An **index**, not a copy. Every statement of behaviour lives in the spec; this page points at it.

## The product

`asyncio-gateway` is a Python library with **one public entry point** — `asyncio_gateway.asyncio_gateway.request()` —
and a protocol-strategy layer behind it (HTTP, FTP, SFTP, SOAP). It is a library, not a service: there
are no endpoints, no environment variables, no health check.

## Source of truth

| Document | What it holds |
|---|---|
| [`docs/specs/v1_release_spec.md`](../../specs/v1_release_spec.md) — **Part A** | The functional specification: R1–R35, 258 acceptance criteria, 89 audit findings, the open questions (OQ1–OQ12) and the fix-interaction constraints (FI-1–FI-16). |
| [`docs/specs/v1_release_stories.md`](../../specs/v1_release_stories.md) | The story breakdown: 32 stories, the coverage gate result, and §10's criterion → story traceability table. |
| [`docs/specs/p0_contract_corrections_spec.md`](../../specs/p0_contract_corrections_spec.md) | The 1.x compatibility contract for immutable dispatch fields, explicit SFTP authentication, and unknown-option handling. |
| [`docs/specs/gcs_selector_spec.md`](../../specs/gcs_selector_spec.md) | The bounded Google Cloud Storage selector functional specification: strict selection and validation, isolated provider lifecycle, five commands, error vocabulary, and no-live-GCP scope. |

## Requirement groups (Part A)

| Group | Requirements | Subject |
|---|---|---|
| A | R1–R5 | Scaffolding and quality-gate infrastructure |
| B | R6–R7 | Dependency upgrades and the resilience-library decision |
| C | R8–R10 | The public response contract and the error model |
| D | R11 | Entry point and protocol dispatch |
| E | R12–R14 | HTTP protocol correctness |
| F | R15 | FTP protocol correctness |
| G | R16–R17 | SFTP protocol correctness |
| H | R18–R19 | SOAP |
| I | R20 | Async correctness |
| J | R21–R22 | The caller-controlled capability surface |
| K | R23–R24 | Transport security and resilience |
| L | R25–R26 | Utilities and observability primitives |
| M | R27–R28 | Quality gates turned on |
| N | R29–R32 | Documentation |
| O | R33–R35 | Value-adds (proposed, not assumed) |

## Human gates open against the functional scope

Answers are recorded on the owning ticket and, where they are decisions of record, in an ADR.
See [`decisions.md`](decisions.md) and §9 of the story breakdown.

| Open question | Owning ticket |
|---|---|
| OQ9 — ratify the `logic/*.py` → `logic/*_client.py` rename | [AGW-6](../tickets/AGW-6-rename-the-protocol-modules.md) |
| OQ12 — keep / replace / vendor the resilience library | [AGW-23](../tickets/AGW-23-the-resilience-library-decision-and-adr.md) |
| OQ4 — the version reset is still a safe one-way door · OQ7 — the LICENSE copyright line | [AGW-28](../tickets/AGW-28-version-provenance-sphinx-retirement-license.md) |
| OQ8 — approval to edit `CLAUDE.md` + `.claude/rules/fastapi-patterns.md` | [AGW-30](../tickets/AGW-30-correct-the-repositorys-own-agent-facing-instructions.md) |

## Per-change view

One ticket per story, in [`docs/project/tickets/`](../tickets/) — index at
[`index.json`](../tickets/index.json).

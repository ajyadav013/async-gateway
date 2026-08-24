# SECURITY REVIEW — GCS Selector, Cycle 2

Target: `codex/gcs-selector` @ `981a022dc7f2b05ac5559ae16396517d18774b13`

Final gate verdict after the reviewed defect loop and affected rechecks:
**SECURITY CLEAR** at `362e184f29ed42a53e3e54984027add599582531`,
with Critical 0 / High 0 / Medium 0 / Low 0. The sections below preserve the
initial blocked aggregation and its remediation history.

The initial aggregation stopped when `uv tree --all-groups --offline` unexpectedly created an untracked `uv.lock`. No tracked source changed. The orchestrator later moved that exact file recoverably to `/tmp/async-gateway-uvlock.iXquwP/uv.lock` after verifying its untracked status, size, and SHA-256; the repository returned to its prior tracked state.

| Scanner | Status | Result |
|---|---|---|
| secret-scanner | ✓ | 1 Medium |
| dependency-scanner | ✓ | 1 High, 1 Medium |
| owasp-reviewer | ✓ | Clean |
| policy-validator | ✓ | Clean |
| pentest-scanner | SKIPPED | No authorized non-production target; no live GCP permission |

## Findings

| ID | Severity | Source | File:Line | Issue | Remediation / routing |
|---|---|---|---|---|---|
| DEP-V-001 | High | dependency-scanner, independently confirmed | `pyproject.toml:112` | Direct `dev` constraint `setuptools<76` resolves to `75.9.1`, affected by CVE-2025-47273 / GHSA-5rjg-fvgr-3xxf. It is reachable in CI's `pip install -e '.[dev]'` path, though not in the runtime GCS chain. | Backend/build-dev lane: upgrade `flake8-import-order` to a compatible release, remove the `<76` ceiling, and require a fixed-safe setuptools range; validate a clean dev install and lint. |
| DEP-V-002 | Medium | dependency-scanner, independently confirmed | `pyproject.toml:112` | The same resolved direct dev dependency is affected by CVE-2026-59890 / GHSA-h35f-9h28-mq5c. | Same single remediation as DEP-V-001. |
| SEC-S-001 | Medium | secret-scanner, independently confirmed | `.gitignore:129` | Only `.env` is ignored; `.env.local`, `.env.*.local`, `*.pem`, and `*.key` are not. No secret or key file is currently tracked. | Repository hygiene/DevOps lane: add the four exclusions and retain the no-tracked-secret check. |

## Adjudication

- GCS-SEC-001 / AC1.3: closed. `asyncio_gateway/asyncio_gateway.py:908-912` rejects every non-empty/non-exact-dict GCS `data` value before `new_envelope` at line 923, processors, provider construction, ADC, breaker, or filesystem work. The frozen adversarial tests prove hostile mappings are rejected without invoking hostile hooks or exposing values.
- `SEC-S-001` is not a false positive. It is pre-existing—`.gitignore:129` originates in initial commit `6602e8d`—and unchanged from the feature baseline, but remains an open repository-policy Medium until remediated or formally scoped by the owner.
- The Google runtime chain resolves cleanly (`google-cloud-storage==3.13.1` and its reported transitive chain had zero advisories). `setuptools==75.9.1` is instead a direct dev dependency selected by `setuptools<76` for the legacy `flake8-import-order==0.18.2` path. The PEP 517 build requirement is separately `setuptools>=77.0.3`; without a lockfile it does not prove an exact safe dev resolution. The High/Medium are real dev/build exposure, not runtime GCS exposure.
- OWASP and policy reports are C0/H0/M0. This is a library surface, so tenant isolation, CORS, session cookies, auth-endpoint rate limiting, and HTTP headers are N/A rather than omitted controls.

## Project auto-Critical checks

- Tenant isolation: PASS / N/A — no tenant-scoped datastore or application route.
- No banned sync work on the async GCS path: PASS — private `_GCS_EXECUTOR` is used and the AST guard rejects default-executor forms.
- No hardcoded secrets: PASS — scanner found no tracked/history credential material; pattern hits were test/demo/redaction or environment-sourced fixtures.
- No secrets/PII in logs: PASS — gateway failures redact URL and traceback; GCS capacity logs contain bounded counters/reasons only.

## Initial Cycle 2 verdict: BLOCKED

Open findings: Critical 0 / High 1 / Medium 2 / Low 0.

Route DEP-V-001 and DEP-V-002 to the backend/build-dev defect lane. Route SEC-S-001 to repository hygiene/DevOps. Re-run only dependency-scanner and secret-scanner after fixes. Security Clear cannot be issued while these findings remain open.

## RARV

The reviewer reasoned from the approved GCS threat surface and Cycle 1 defect; aggregated all four completed scanner reports; independently verified AC1.3 ordering, `.gitignore` provenance, and the declared/resolved setuptools path; and stopped immediately when a read-only resolution command created `uv.lock`. The orchestrator preserved that stop condition and restored the checkout through an isolated one-file cleanup worker.

## Final affected rechecks and aggregation

Target: `codex/gcs-selector` @ `362e184f29ed42a53e3e54984027add599582531`.

Open findings: Critical 0 / High 0 / Medium 0 / Low 0.

| Finding | Prior severity | Final status |
|---|---:|---|
| GCS-SEC-001 / AC1.3 | High | Closed — GCS rejects every request-data value except `None` or an exact empty built-in dict before processors/providers |
| DEP-V-001 | High | Closed — fresh graph resolves `setuptools==84.0.0`; advisory fixed |
| DEP-V-002 | Medium | Closed — fresh graph resolves `setuptools==84.0.0`; advisory fixed |
| SEC-S-001 | Medium | Closed — exact environment/key exclusions added without hiding tracked files |
| SEC-S-002 | Scanner candidate: High | False positive — deterministic isolated test credential, not a secret or production-reachable unsafe default |
| OWASP review | — | Clean, C0/H0/M0 |
| Policy validation | — | Clean, C0/H0/M0 |

The dependency recheck scanned a fresh 76-package resolution and returned zero OSV records, including the complete GCS runtime chain. The secret recheck closed SEC-S-001 and found no tracked or reachable-history secret material.

SEC-S-002 adjudication: `gateway-test-pw` is a deterministic integration-test credential. `docker-compose.yml` explicitly defines an integration-test stack, publishes no host ports, and confines FTP/SFTP access to the ephemeral Compose network. The servers use disposable fixtures, the SFTP account is restricted to `internal-sftp`, and the integration client is the only intended consumer. The literal is test-marked, overrideable, has no release/deployment/workflow references, and all cited files are identical to `origin/master` and unchanged by the GCS feature or security fix. It protects no external or production asset, so the actual-hardcoded-secret auto-Critical rule does not apply.

Project auto-Critical checks: tenant isolation PASS/N/A; no banned sync work PASS; no actual hardcoded secrets PASS; no secrets/PII in logs PASS.

Dynamic pentest: SKIPPED — no authorized non-production target and no live-GCP authorization.

Final RARV: the reviewer traced every original and recheck finding through the immutable fix, fresh dependency graph, ignore behavior, Compose topology, server configuration, fixtures, Git provenance, and release automation. All Critical/High/Medium findings are closed.

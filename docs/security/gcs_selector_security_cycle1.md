# Security Review — GCS Selector (Cycle 1)

Immutable target: `codex/gcs-selector` at `a6d8368a9541febe20e7a0d9acf9c9fb6f3c6f54`
Exact diff: `057bc367bed24518456814545f357b331548590a..a6d8368a9541febe20e7a0d9acf9c9fb6f3c6f54`

## Gate verdict: BLOCKED

Open findings: Critical 0 / High 1 / Medium 0 / Low 0.

Security Clear cannot be issued because GCS-SEC-001 is a reproduced High and
the mandatory four-scanner evidence set could not complete after the permitted
infrastructure retry.

## Scanner completion

| Scanner | Status | Evidence |
|---|---|---|
| secret-scanner | INCOMPLETE | Reused scanner exceeded its bound and did not hand off; fresh spawn and explicit retry both failed `agent thread limit reached`. |
| dependency-scanner | NOT RUN | Local `pip check` passed, but that is not CVE evidence. |
| owasp-reviewer | NOT RUN | Independent review found GCS-SEC-001. |
| policy-validator | NOT RUN | Structural applicability was reviewed, but no mandatory validator verdict exists. |
| pentest-scanner | SKIPPED | No authorized non-production target; live-service/network access prohibited. |

## GCS-SEC-001 — High

Locations: `docs/specs/gcs_selector_spec.md:94`,
`asyncio_gateway/asyncio_gateway.py:907,917`,
`asyncio_gateway/utils/redaction.py:103`,
`asyncio_gateway/utils/envelope.py:249`, and
`asyncio_gateway/logic/gcs_client.py:1181`.

GCS accepts caller `data` containing service-account/key material and returns
it in the shared payload envelope. `private_key` is not a recognized sensitive
key, so it remains intact after finalization. This violates AC1.3's ADC-only
boundary.

Safe no-network reproduction replaced `GcsRequest.handle_request` with a
deterministic in-memory handler and called public `request()` with a service-
account-shaped mapping containing test-only `type`, `private_key`,
`client_email`, and `token_uri` values. Result:

```text
accepted=True
private_key_preserved=True
payload_keys=['client_email', 'private_key', 'token_uri', 'type']
```

No live ADC/network/GCP call occurred. This is High, not auto-Critical, because
no library-controlled log disclosure was proven. Returned credential material
is nevertheless a routinely consumed public failure/success surface.

Required remediation: reject non-empty GCS `data` at the public boundary before
envelope creation, processors, ADC, breaker lookup, or I/O; add credential-
shaped and safe-empty regressions proving no echo/entry. Defense-in-depth GCP
credential-field redaction should be evaluated without weakening the primary
boundary or expanding unrelated protocol behavior.

## Other evidence

- Focused GCS/no-blocking 548 pass.
- Bandit 0 Medium/High; `pip check` passes.
- `google-cloud-storage 3.13.1` installed under declared `>=3,<4`.
- No tracked `.env`, key, credential, or service-account file found in the
  bounded tree check; no actual secret found by the coordinator sweep.
- Dependency CVE clearance remains unproven because `pip-audit` was unavailable
  and the mandatory dependency scanner did not run.
- Tracked tree stayed clean and `git diff --check` passed.

## RARV

Reason—inventory ADC, signer, bearer, token, path, diagnostic, payload, async,
and supply-chain boundaries. Act—read the full spec/diff/runtime, run safe
static/focused checks and the public in-memory repro, and attempt mandatory
scanner dispatch/retry. Reflect—the ADC implementation is strict, but the
unrestricted public data channel bypasses that boundary. Verify—the flaw is
reproducible with exact lines and no live service; Security Clear is refused.

Next: backend RED-first defect loop, then rerun all four scanners once capacity
is restored. No later enterprise gate is authorized before Security Clear.

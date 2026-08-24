# GCS private-capacity admission evidence

This record validates the local fail-fast admission boundary when all four
private GCS leases are occupied. It does not measure Google Cloud Storage or
IAM service latency or availability.

## Immutable context

- Date: 2026-08-23
- Branch: `codex/gcs-selector`
- HEAD: `9c5115ee526983a2cb2a827ecd1916e8a8103409`
- Scope: deterministic local admission, rejection telemetry, lease release,
  and recovery only

## Predeclared SLI and SLO

The SLI population is a fixed, synchronized batch of 100 calls to the public
gateway while four private GCS lifecycle leases remain held. A successful
admission decision is a typed `GCS_CAPACITY` response with status 503 returned
before any held permit is released and before the request's explicit
1.0-second deadline, with no provider-capable work reached.

The batch passes only if all of these predeclared thresholds hold:

| Measure | Threshold |
| --- | --- |
| Rejection contract | 100 of 100 responses are `GCS_CAPACITY`/503 before held-permit release and before the 1.0-second request deadline; zero other outcomes |
| Admission latency | Nearest-rank p99, maximum per-request latency, and batch wall time are each `<1.0s` |
| Admission throughput | At least 100 decisions/s across the fixed batch of 100 |
| Rejected-path work | Zero provider, ADC, storage, generation, close, breaker, or network work |
| Lease and telemetry invariants | Four leases remain held through the batch; exactly 100 rejection events have the frozen fields and contain no sentinel secret |
| Release and recovery | Releasing all four leases permits one successful request with exactly one generation and one close; final active leases are zero |

## Method and load shape

The probe ran with Python 3.14.7. Its interpreter and dependencies came from
the prior source-distribution verification environment, while
`PYTHONPATH="$PWD"` caused the immutable HEAD checkout to supply the
`asyncio_gateway` implementation and deterministic GCS test doubles. It
installed an in-memory signed-URL provider and breaker double,
captured capacity log records in memory, and used synthetic sentinel target,
signer, credential, and signed-URL values to test telemetry containment.
There was no live GCP account, metadata server, storage emulator, or network
endpoint.

The probe acquired the four private leases directly, confirmed the active
lease count was four, then created 100 asynchronous public `request` calls.
An `asyncio.Event` released all tasks as one intentional saturation spike.
Each call used the GCS `signed_url` GET shape, an explicit 1.0-second timeout,
and an in-memory response capture. Per-request and batch durations used
`time.perf_counter()`; percentiles used nearest rank
(`ceil(p * n)`, one-indexed). Provider, generation, close, and breaker doubles
kept exact call ledgers, while captured capacity records supplied the
telemetry count, field-shape, value, and sentinel-safety checks.

After the rejected batch completed, the probe confirmed all four leases were
still held, released them, and issued one recovery request through the same
public boundary. That request had to succeed with exactly one in-memory URL
generation and one resource close, then return the active-lease count to zero.

The exact invocation was:

```bash
PYTHONPATH="$PWD" /tmp/gcs-pipeline-green-authorized.6aYFwB/sdist-venv/bin/python /tmp/gcs-load-probe.mNYEts/probe.py
```

The `/tmp` locations above were ephemeral evidence-run paths. They are
included to identify the executed environment, not as a reusable command or a
checked-in load harness. The method and results in this document are the
durable evidence.

## Raw concise output

```text
n=100
percentile=nearest-rank
p50=0.000193708s
p95=0.000237250s
p99=0.000294375s
max=0.001090708s
batch_wall=0.021209210s
throughput=4714.933 decisions/s
outcomes=100 GCS_CAPACITY/503, 0 other
contract_misses=0
held_leases_before=4
held_leases_after_batch=4
rejection_telemetry=100
telemetry_fields_exact=true
telemetry_secret_safe=true
rejection_provider/ADC/storage/generation/close/breaker/network_work=false
breaker_run_calls=0
recovery=ok
recovery_generation_calls=1
recovery_close_calls=1
final_active_leases=0
RESULT=PASS
```

## Results and verdict

| Measure | Result | Verdict |
| --- | --- | --- |
| Rejection contract | 100 `GCS_CAPACITY`/503, 0 other, 0 misses, before lease release | PASS |
| Admission latency | p99 `0.000294375s`; max `0.001090708s`; batch `0.021209210s` | PASS: every threshold is `<1.0s` |
| Admission throughput | `4714.933 decisions/s` for 100 decisions | PASS: `>=100 decisions/s` |
| Rejected-path work | No provider, ADC, storage, generation, close, breaker, or network work; breaker run calls `0` | PASS |
| Lease and telemetry invariants | Leases `4` before and after the batch; 100 exact, secret-safe rejection events | PASS |
| Release and recovery | Recovery succeeded; generation `1`; close `1`; final active leases `0` | PASS |

Overall verdict: **PASS** for the predeclared local private-capacity admission
SLO at the immutable context above.

## Terminal integrity proof

The evidence run also recorded:

```text
inventory_exact_equal=True
git_state_exact_equal=True
branch_head_index_tracked_clean=True
```

## Applicability and limitations

This result applies only to the in-process capacity-admission decision while
all four private leases are occupied. It makes no claim about external GCS,
ADC, IAM, DNS, or network latency or availability, and the run made no live
network request. An initial-launch A/B comparison is not applicable because
there is no alternate user-facing algorithm or live traffic cohort. Consuming
services own their external availability and latency SLOs, rejection-rate and
drain-duration thresholds, dashboards, and alerts.

The evidence file is repository documentation only. `MANIFEST.in` prunes
`docs`, so this record is excluded from both wheel and source-distribution
artifacts.

## Accepted operational costs

| Accepted cost | Why it remains accepted | Owner | Revisit trigger |
| --- | --- | --- | --- |
| An admitted synchronous operation retains its lease while deadline or cancellation cleanup drains, so observed return latency may exceed the request deadline and saturation may persist. | Retaining the lease keeps cleanup capacity bounded and prevents a still-running provider future or resource from escaping lifecycle accounting. | asyncio-gateway maintainers; consuming-service operators own their local rejection and drain budgets. | Revisit if `gcs_drain_finished.duration_seconds` or `gcs_capacity_rejected` breaches a consuming service's declared threshold, all leases remain occupied beyond provider deadlines, or a supported cancellable async provider API becomes available. |
| One gateway signed-URL generation may cause credential-internal IAM retries and therefore more than one remote signing attempt. | The gateway performs one generation invocation, but supported google-auth credential implementations own their internal retry policy; a custom signer remains out of scope. | Consuming service IAM/platform owner for quota and cost; asyncio-gateway maintainers for adapter behavior. | Revisit if IAM audit data exceeds the consuming service's request or cost budget, google-auth exposes a supported retry control, or a custom signer is approved into scope. |

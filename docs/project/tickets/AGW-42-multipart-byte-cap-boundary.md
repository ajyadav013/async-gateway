# AGW-42: Pin the multipart byte-cap boundary

- **Status:** DONE
- **Story:** Post-release HTTP resource-limit regression coverage
- **Spec:** existing bounded multipart response contract
- **Relations:** none

## Why

The multipart response limit already existed, but its exact inclusive/exclusive
boundary needed a regression test so later streaming work could not weaken it.

## Definition of Done

- Tests cover a body exactly at the configured cap.
- Tests cover the first byte over the configured cap.
- The complete coverage gate remains at 100% line and branch coverage.

## Work Log

### 2026-08-20 — reconstructed from default-branch history

Commit [`7db7ea8`](https://github.com/ajyadav013/asyncio-gateway/commit/7db7ea8)
pins the exact boundary and is present in release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)).

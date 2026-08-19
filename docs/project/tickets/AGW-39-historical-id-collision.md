# AGW-39: Historical ID collision — URL authority redaction and wheel smoke

- **Status:** DONE
- **Story:** Post-release hardening; two independently completed changes reused
  this ID
- **Spec:** historical commit contracts and their regression tests
- **Relations:** none

## Why

The repository history contains two unrelated commits carrying `AGW-39`. The
ticket file was missing, so silently assigning either meaning would erase the
other. This reconstructed record preserves both uses and marks the identifier as
retired.

## Definition of Done

- Authority parsing, including IPv6 userinfo cases, is held by regression tests.
- The built wheel is exercised in a clean Docker image.
- The ID collision is visible in the project ledger and must not be reused.

## Work Log

### 2026-08-20 — reconstructed from default-branch history

- [`d9e8143`](https://github.com/ajyadav013/asyncio-gateway/commit/d9e8143)
  fixed URL-authority redaction based on parser structure.
- [`39a1596`](https://github.com/ajyadav013/asyncio-gateway/commit/39a1596)
  added the clean-image built-wheel Docker test.
- Both changes are in release merge
  [`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
  ([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)).

# AGW-40: Historical ID collision — redaction scan and SFTP destination stat

- **Status:** DONE
- **Story:** Post-release hardening; two independently completed changes reused
  this ID
- **Spec:** historical commit contracts and their regression tests
- **Relations:** none

## Why

The repository history contains two unrelated commits carrying `AGW-40`. This
reconstructed ticket preserves both meanings and retires the collided ID.

## Definition of Done

- URL redaction uses one userinfo scan without weakening coverage.
- SFTP upload completion checks the destination after transfer.
- The ID collision is visible and the identifier is not reused.

## Work Log

### 2026-08-20 — reconstructed from default-branch history

- [`75fad95`](https://github.com/ajyadav013/asyncio-gateway/commit/75fad95)
  consolidated the userinfo redaction scan.
- [`3d2961e`](https://github.com/ajyadav013/asyncio-gateway/commit/3d2961e)
  corrected SFTP upload destination verification.
- Both changes are in release merge
  [`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
  ([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)).

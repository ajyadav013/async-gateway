# AGW-41: Historical ID collision — URL validation and live Docker protocols

- **Status:** DONE
- **Story:** Post-release hardening; two independently completed changes reused
  this ID
- **Spec:** historical commit contracts and their regression tests
- **Relations:** none

## Why

The repository history contains two unrelated commits carrying `AGW-41`. This
reconstructed ticket preserves both meanings and retires the collided ID.

## Definition of Done

- Protocol-relative URLs fail at the entrypoint instead of reaching aiohttp.
- Docker integration exercises HTTP, FTP, SFTP, and SOAP against live servers.
- The ID collision is visible and the identifier is not reused.

## Work Log

### 2026-08-20 — reconstructed from default-branch history

- [`d00d9f8`](https://github.com/ajyadav013/asyncio-gateway/commit/d00d9f8)
  added protocol-relative URL rejection.
- [`8362cc7`](https://github.com/ajyadav013/asyncio-gateway/commit/8362cc7)
  added the four-protocol Docker integration proof.
- Both changes are in release merge
  [`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
  ([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)).

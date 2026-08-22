# P0 contract corrections specification

## Status

Approved for implementation on `fix/p0-contract-corrections` (AGW-43).

## Goal

Close the three public-contract defects found after 1.0.0 without changing the
response envelope or protocol registry: processor hooks must not retarget a
request, SFTP must support explicit password and key authentication without
ambient credentials, and misspelled options must fail at the public boundary.
The package metadata and ticket ledger must describe the product that actually
ships.

## Compatibility contract

- `request()` keeps its existing positional and named parameters. Unknown
  top-level keywords now raise `ConfigurationError` before processors or
  transport code run; they were previously accepted and ignored.
- Pre-processors may still edit `payload`, `headers`, `cookies`, custom
  metadata, and other non-routing envelope fields. Editing `url` or `protocol`
  raises `ProcessorError` before a protocol object is constructed. Required
  envelope keys still cannot be removed.
- Post-processors still run after dispatch and may rewrite report fields,
  including `url` and `protocol`, provided they preserve the envelope key set.
- FTP authentication is unchanged: legacy objects with string `.login` and
  `.password` remain required.
- SFTP accepts the new `SFTPAuth` model with a non-empty username and at least
  one of password or explicit client keys. Legacy `.login`/`.password` objects
  remain supported, including legacy key-only calls that carry `client_keys`
  in `protocol_info`.
- SFTP never reads default private keys or `SSH_AUTH_SOCK` unless the caller
  explicitly supplies keys and opts into agent use. Host-key verification and
  the explicit insecure bypass retain their current behavior.
- Each protocol publishes its accepted `protocol_info` key set. Unknown
  security-sensitive keys fail immediately. Other unknown keys emit a
  `DeprecationWarning` during the 1.x compatibility window and are scheduled
  to become errors in 2.0.
- The response envelope, exception codes, protocol names, package name, and
  package version remain unchanged.

## Acceptance criteria

### AC1 — immutable dispatch target

1. A pre-processor changing `url` or `protocol` raises `ProcessorError` before
   any socket, protocol constructor, or breaker lookup can use the change.
2. The error names changed fields but does not include original or replacement
   values, so credential-bearing URLs cannot leak.
3. Non-routing metadata remains writable and reaches the protocol client.
4. A removed required envelope key is still refused.
5. A post-processor may change report-only `url` and `protocol` fields.

### AC2 — explicit SFTP authentication

1. `SFTPAuth(username=..., password=...)`,
   `SFTPAuth(username=..., client_keys=...)`, and both together are accepted.
2. Missing/empty username and absence of both password and keys raise
   `ConfigurationError` before `asyncssh.connect`.
3. `key_passphrase` is forwarded as asyncssh's `passphrase` only when set.
4. `agent_path=None` is passed by default. Agent use is enabled only by
   `use_ssh_agent=True`; agent-only authentication is not sufficient.
5. Legacy `.login`/`.password` objects and legacy
   `protocol_info['client_keys']` calls remain supported.
6. Existing host-key verification defaults and the named insecure bypass are
   unchanged.
7. `asyncio.CancelledError` propagates unchanged.

### AC3 — typo detection

1. Unknown top-level keywords, including representative misspellings
   `protcol_info`, `preprocesor_config`, and `timeuot`, raise
   `ConfigurationError` before processors or network code.
2. The error lists the unknown keys and points to the accepted top-level
   arguments, `protocol_info`, and processor-config locations.
3. Each protocol's accepted key set covers every documented and implemented
   option.
4. A security-sensitive unknown key such as `verify_sll` raises immediately.
5. A non-security unknown key warns with `DeprecationWarning` in 1.x and the
   warning lists the accepted keys.
6. Validation copies and never mutates the caller's mapping.

### AC4 — project metadata and traceability

1. The PEP 621 description names HTTP, HTTPS, SOAP, FTP, and SFTP and makes no
   Redis or XML-protocol claim.
2. Project URLs include Homepage, Documentation, Source, Issues, Changelog,
   and Security policy, each pointing at a real repository resource.
3. README, changelog, security policy, ADR, wiki indexes, and AGW-43 describe
   the behavior and migration path.
4. Stale AGW ticket states are reconciled against implementation, test,
   release, and commit evidence. Historical ID collisions are recorded rather
   than silently overwritten.

## Edge and error cases

- A processor assigning the same routing value is allowed because it has not
  changed the dispatch decision.
- URL mutation comparisons happen against the raw caller URL, while error text
  names only the field.
- Empty key collections count as no SFTP key and cannot reactivate asyncssh's
  default-key search.
- A typed SFTP model and legacy `protocol_info['client_keys']` may not both
  provide keys; the ambiguous source is rejected.
- `key_passphrase` without explicit client keys is rejected rather than
  accepted as an option asyncssh cannot apply.
- Unknown non-string `protocol_info` keys fail immediately because accepted
  keyword names are strings.
- If sensitive and ordinary unknown keys arrive together, the call fails and
  reports the complete unknown set.

## Implementation map

| Requirement | Owning code | Tests | Documentation |
|---|---|---|---|
| AC1 | `asyncio_gateway/asyncio_gateway.py` | `tests/test_entrypoint.py` | README, ADR-0002 |
| AC2 | `asyncio_gateway/auth.py`, package root, `logic/sftp_client.py` | `tests/logic/test_sftp_client.py`, packaging/docs tests | README, changelog |
| AC3 | `helpers/internal/base.py`, protocol class key declarations, entry point | entrypoint and protocol tests | README, changelog |
| AC4 | `pyproject.toml`, ticket store | packaging/docs tests | project docs and ticket wiki |

No dependency, CI-workflow, response-schema, registry, or version change is
required. A future major version may replace the mutable processor envelope
with a typed request context and make every unknown `protocol_info` key an
error; this work establishes the fail-fast boundary needed for that migration.

## Verification

- Focused red/green tests for AC1–AC3.
- Full suite with 100% line and branch coverage.
- Five deterministic randomized-order seeds.
- flake8 source gate, suppression check, mypy, Bandit, and example compile.
- Build, `twine check`, and clean wheel/sdist install-import smoke tests.
- Python 3.10 floor and Python 3.14 ceiling where available locally.

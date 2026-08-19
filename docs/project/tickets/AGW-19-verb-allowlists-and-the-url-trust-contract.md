# AGW-19: Verb allowlists and the URL-trust contract

- **Status:** DONE
- **Story:** S19 — spec Step 17, size **L** *(serialization point — 5 `getattr` sites in 5 files)* (`docs/specs/v1_release_stories.md` §4, Phase 3; sizing exception §12 — irreducible)
- **Spec:** `docs/specs/v1_release_spec.md` — R21-AC1,2,5 (Group J — The caller-controlled capability surface) · **R15-AC8** (Group F — FTP protocol correctness) · **R17-AC7** (Group G — SFTP protocol correctness)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; the capability surface
- **Decisions:** none — see §11-D3 (R15-AC8 / R17-AC7 forward-reference this step from Steps 8 and 10)
- **Files (declared scope):** ~`helpers/internal/base.py`, `logic/{http,ftp,sftp}_client.py`, `helpers/internal/request_helper.py`, `utils/http_file_config.py`, `tests/logic/test_ftp_client.py`, `tests/logic/test_sftp_client.py`, `tests/helpers/test_request_helper.py`

## Why

R21 requires bounded, documented verb allowlists and an explicit URL-trust contract. The defect *is*
that five `getattr` sites across five files reach for a caller-named attribute with no allowlist, so
fixing four of them still ships a fail-open surface — which is why §12 records this story as
irreducible. It also closes R15-AC8 and R17-AC7, whose text forward-references "the R21 allowlist"
from Steps 8 and 10 (§11-D3). Per-hop scheme enforcement already landed at AGW-15 under FI-16; this
story owns the entry-point contract.

Closes findings: M25, H13-ptr, M21-partial.

## Definition of Done

- each protocol declares an explicit allowlist; `grep -n "getattr("` shows **every remaining call preceded by an allowlist check in the same function**
- unknown verb → `ConfigurationError` naming the verb and listing allowed values — **fail closed**; parametrised over **all five sites** (`sftp_client.py:43`, `ftp_client.py:51`, `request_helper.py:31`, `:100`, `http_file_config.py:57`)
- `request_type='close'` — today `TypeError` swallowed as `999` — is now `ConfigurationError`
- a verb that exists but is not a coroutine function is covered; verb case/whitespace normalisation matches AGW-8's
- `allowed_schemes` defaults `{'http','https'}` and rejects the rest; `ftp://`/`file://`/`javascript:` passed to `'HTTP'` tested *(per-hop enforcement already landed in AGW-15 — FI-16)*
- **closes R15-AC8 and R17-AC7**, whose text forward-references "the R21 allowlist" from Steps 8 and 10 — see §11-D3

## Dependencies

- **blockedBy:** AGW-16, AGW-18
- **blocks:** AGW-20

## Decisions

- **One resolver, in `utils/http_file_config.py`, not five checks.** The five sites span two
  layers: `helpers/` (the transport loop, the protocol base) and `utils/`. `utils/` may not import
  `helpers/`, so a resolver in `base.py` would have left the two `utils/`-side sites needing a
  second implementation — and two implementations of one allowlist is how one comes to admit what
  the other refuses, which is M25 reopened under a new name. It lives beside `guard_declared_length`
  for the reason that module's own docstring already gives.
- **`BaseRequestClass.resolve_verb` is a thin delegate.** The spec names it as the base class's
  contract (§"Provides to subclasses"), so it exists; it forwards to the one implementation rather
  than being a second one. Which verbs a protocol admits stays the protocol's own knowledge, passed
  in, exactly as `REQUIRED_INFO_KEYS` is declared by the subclass.
- **HTTP's verb is judged twice, deliberately.** `validated_request_type` runs in
  `HttpRequest.__init__` — outside the entry point's conversion block, so a bad verb escapes
  synchronously and unlogged like every other pre-dispatch config error. The transport's own
  `resolve_verb` still runs per redirect hop, because `after_redirect` *rewrites* the verb (a 303
  turns any verb into a GET), and it also guards the two file paths that never build an
  `HttpRequest`. Not redundancy: different values, different call sites.
- **SFTP admits exactly `get`/`put`/`remove`** — the three the class docstring and README document,
  so allowlist and documentation agree (R21-AC3). Deliberately narrower than `RECURSING_MODES`,
  which answers a different question ("which asyncssh operations accept `recurse`") and is broad on
  purpose. `mput` in particular is withheld because R22 makes its local/remote operand order
  normative and this module still passes it inverted; admitting it here first would ship a verb
  with a known-wrong argument order.
- **`head` is admitted although the README's verb table omits it.** The allowlist is the source of
  the README (R21-AC3), so the gap is R29's to close from this set. Narrowing the allowlist to match
  today's prose would turn a documentation fix into a breaking change for every caller probing a
  resource without fetching it.
- **`fetch_file`'s site was fixed even though AGW-20 deletes the function.** Leaving the last
  unbounded `getattr` behind on the grounds that it is scheduled for removal would make the grep
  this criterion is written against non-empty, and the story's own claim false for as long as the
  deletion is pending.

## Work Log

### 2026-08-20 — default-branch ledger reconciliation

Re-verified this ticket's Definition of Done against release merge
[`11d6e26`](https://github.com/ajyadav013/asyncio-gateway/commit/11d6e26c4c3893f84983d5c8375dd713b8233113)
([PR #4](https://github.com/ajyadav013/asyncio-gateway/pull/4)). The implementing
history is [`1635116`](https://github.com/ajyadav013/asyncio-gateway/commit/1635116), [`8487cb0`](https://github.com/ajyadav013/asyncio-gateway/commit/8487cb0), [`363897a`](https://github.com/ajyadav013/asyncio-gateway/commit/363897a); the source and regression coverage remain present, and the
post-release suite passes with 3,161 tests, 8 skips, and 100% line/branch
coverage. The primary status is therefore normalized to `DONE`; the original
work log below is retained as historical context.


- **Implementation** (`1635116`) — `resolve_verb` + `HTTP_VERBS` in `utils/http_file_config.py`;
  `BaseRequestClass.resolve_verb` in `helpers/internal/base.py`; `FTP_COMMANDS` and `SFTP_MODES`
  declared beside their clients; `validated_request_type` in `logic/http_client.py`. All five
  caller-controlled `getattr` sites replaced. `grep -n "getattr(" async_gateway/` now returns six
  hits, none of them on a caller-supplied name: four read fixed literal attribute names
  (`'__name__'`, `'results_collector'` ×2, `'trace_configs'`), one reads a library-owned
  `SFTPAttrs` field name from this package's own `FILE_STAT_FIELDS`, and the sixth is the single
  guarded read inside `resolve_verb` itself, preceded by the allowlist check in the same function.
- **Tests** (`8487cb0`) — parametrised over all five sites. Each protocol's rows include a verb
  that is a *real* attribute of the transport client (`aioftp`'s `close`/`list`, `asyncssh`'s
  `rmtree`/`exit`, `aiohttp`'s `close`/`ws_connect`/`detach`), because those are the names the old
  lookup resolved rather than refused. Two tests assert the client was never addressed at all, which
  an envelope assertion alone cannot distinguish from "resolved the name, then declined to call it".
  831 passed / 2 xfailed, up from a 800/2 baseline at `fe9b351`.
- **AGW-35 docstring** — `request()`'s `:raises:` claimed its escaping list was universal. Corrected
  per Ruling T: placement relative to the entry point's one conversion `try` is what decides, the
  listed set is the escaping set, and FTP/SFTP verb errors arrive as logged `CONFIG` envelopes.
  Verified by execution, not inference: HTTP `request_type='close'` raises `UnsupportedVerbError`;
  FTP `command='close'` returns `ok=False`/`CONFIG`/400.

## Findings closed

- **M25** — the five unallowlisted `getattr` sites. Closed.
- **H13-ptr** — retained id pointing at M25. Closed with it.
- **M21-partial** — the `allowed_schemes` half was already delivered by AGW-15 (FI-16) and is
  verified here rather than reimplemented: the default is `{'http','https'}` and `ftp://`,
  `file://` and `javascript:` under `'HTTP'` are all refused. The README's "You own URL validation"
  section (R21-AC4) is **not** this story's — it is R29/AGW-29's, and the story row scopes this
  ticket to R21-AC1,2,5.

## Not done, and why

- **Ruling V (relocating S14's missing-file guard into `validated_upload_config`) — declined.**
  R21 does not support it. R21 is about *verb* allowlists and the URL-trust contract; a
  `local_filepath` that does not exist is neither. The traceability table routes local-path handling
  to **R22** (path containment) and the upload-config surface to R25, so moving the raise under this
  story would attach it to a requirement whose criteria do not mention it. There is also a technical
  objection independent of scope: `validated_upload_config` is synchronous and runs in
  `HttpRequest.__init__`, while the existing guard is an `await aiofiles.open(...)` — the relocation
  would need either blocking I/O on a constructor the event loop is on (banned, AST-enforced) or a
  `stat`, and the current site's own comment records that `stat` was rejected because it succeeds on
  a directory and on a mode-000 file where an open does not. Recommend R22 as the owner.
- **R21-AC3** (a test asserting allowlists and README agree) and **R21-AC4** (the "You own URL
  validation" README section) both require editing `README.md`, which is outside this ticket's
  declared file scope and belongs to R29. The allowlists are now the single source those tests will
  read from. Note for that story: `HTTP_VERBS` includes `head`, which the README's current verb
  table omits, so the test will fail until the table is extended.

# AGW-22: `logic/soap_client.py`, from scratch

- **Status:** DONE
- **Story:** S22 — spec Step 20, size **L** — 8 files, 16 criteria, cannot be split (`docs/specs/v1_release_stories.md` §4, Phase 4; sizing exception §12)
- **Spec:** `docs/specs/v1_release_spec.md` — R18-AC1…8, R19 all (Group H — SOAP) · R11-AC5 (Group D — Entry point and protocol dispatch) · R27-AC6 (Group M — Quality gates turned on)
- **Design:** `docs/specs/v1_release_spec.md` — Part B, Developer Documentation; the SOAP client
- **Decisions:** none — reads the human ratification of **OQ5** (SOAP 1.2 `SOAPAction`) and **OQ6** (XML parsing); both are ratification-only and block nothing
- **Files (declared scope):** +`logic/soap_client.py`, `tests/logic/test_soap_client.py` · ~`logic/__init__.py`, `helpers/internal/{__init__,filters_helper,request_helper}.py`, `utils/{exceptions,status_map}.py`, `tests/test_entrypoint.py` *(remove SOAP `xfail`s)*

## Why

R18 requires an async SOAP 1.1 and 1.2 client over the existing aiohttp layer — today `'SOAP': None`
dispatches to nothing and mypy reports `"None" not callable`. R19 requires Faults to map into the
library's error contract and XML parsing to be hardened against entity expansion, with the DOCTYPE
rejection scoped to the prolog so a legitimate `<detail>` containing the literal text `DOCTYPE` is
still parsed. This is genuinely new code in a remediation release, flagged in the spec's own balance
sheet; it cannot ship without its Fault handling (§12). It reuses rather than reimplements the session,
capped reader, breaker, tracer, request path and envelope built in earlier stories.

Closes findings: H3.

## Definition of Done

- `'SOAP'` dispatches to a real `SoapRequest`; `grep -rn "'SOAP': None"` → 0; mypy no longer reports `"None" not callable`
- `soap_version` ∈ {1.1 default, 1.2}, else `ConfigurationError`
- version-correct namespaces (`schemas.xmlsoap.org/soap/envelope/` · `www.w3.org/2003/05/soap-envelope`) + optional `soap_headers` inside `<Header>`; an already-complete `<Envelope>` is **not double-wrapped**
- body accepted as an XML **string** or an `Element`; **no dict-to-XML mapping**
- headers asserted off the recording handler: **1.1** `text/xml; charset=utf-8` **and** `SOAPAction` emitted **always** (`""` when absent, quoted when supplied); **1.2** `application/soap+xml; charset=utf-8` with `;action="…"` and **no** `SOAPAction` (OQ5)
- **byte-identity:** the body bytes equal `build_envelope(...).encode('utf-8')` exactly — dispatched through AGW-13's **raw-body filter**, never the JSON filter — asserted via `await request.read()` on the real loopback handler for both versions
- `soap_body` = **the first element child of `<Body>`**, `None` when absent/empty/empty-response, first-with-a-`warning` when several (1.1 permits several; raising would reject legitimate traffic)
- reuse not reimplementation: same session/timeout/**capped reader** (AGW-15), breaker (AGW-16), tracer (AGW-21), request path (AGW-13), envelope (AGW-7) — a test asserts `request_tracer` populated, `timeout` honoured, over-cap → `RESPONSE_TOO_LARGE`
- Faults for both versions (incl. the 1.2 `Code/Subcode` chain) → `ok=False`, `SOAP_FAULT`, structured `SoapFault`; **a Fault at HTTP 200 still yields `ok=False`**; `status_code` is the real status, nothing synthesised
- **DOCTYPE rejection is prolog-scoped** — before the root element's start tag, by prolog scan or an `expat` `StartDoctypeDeclHandler`, **before** `fromstring`; **a substring search over the body is explicitly forbidden**. Two tests, both required: billion-laughs → `XML_UNSAFE`, memory flat, `fromstring` never called; **and** a well-formed response whose `<detail>` contains the literal text `DOCTYPE` is **accepted and parsed normally**
- malformed XML and valid-but-not-SOAP (an HTML proxy page) → `SERIALIZATION` with position/first bytes, never `{}`
- `multipart/related` (MTOM) → `ConfigurationError`, never mis-parsed
- `grep -rn "lxml" async_gateway/` → 0
- SOAP rows of AGW-9 go green; all five protocols now satisfy E1 and E11

## Dependencies

- **blockedBy:** AGW-17, AGW-20, AGW-21
- **blocks:** AGW-23, AGW-24

## Decisions

- **`soap_body` is the first element child of `<Body>`, never `<Body>` itself.** Taken from the spec
  and asserted, because the two readings differ on exactly the case R18 names: an empty `<Body/>` is
  a perfectly good `Element`, so under the other reading the HTTP-202 criterion could not hold.
- **The response version is read from the document, not from `protocol_info`.** A server answering
  1.1 to a 1.2 request is an ordinary misconfiguration; parsing it against the *requested* namespace
  would find no `<Body>` and report a readable Fault as an empty success. The mismatch is logged.
- **MTOM is refused in `read_response`, not in `SoapRequest`.** This is a placement decision, not a
  layering slip. `read_response` writes multipart parts to disk *as it reads them*, so a refusal made
  after the read leaves behind a file the caller never asked for. Mutation 6 below produced exactly
  that file, which is the evidence for the placement. It is one additive keyword defaulting False, so
  no other caller's behaviour changes.
- **`soap_action` is validated for quotes and line breaks.** Not in the spec's criteria; added because
  the value reaches the wire inside a quoted string on both versions, so a quote ends that string
  early and a CR/LF lets what follows be read as headers. Header injection from caller input.
- **The library's `Content-Type` overrides the caller's.** Not a precedence preference: a contradicted
  content type also routes the envelope away from the raw-body filter into the JSON encoder, which is
  the routing hole R18-AC6 exists to close.
- **Parsing runs in `asyncio.to_thread`.** Building a tree is CPU-bound and the body may be as large
  as `max_response_bytes` allows, so `parse_document` is a module-level plain `def` reached only
  through a thread — the same contract `build_client_ssl_context` follows.

## Work Log

**Commits on `lane/s22`:** `b17216f` (client, registry, transport keyword, `xfail` removal) ·
`b9a9763` (test suite) · this ticket update.

**Files changed (8 of the 10 declared; two needed no edit).** `+async_gateway/logic/soap_client.py`,
`+tests/logic/test_soap_client.py` · `~logic/__init__.py`, `~helpers/internal/request_helper.py`,
`~tests/test_envelope.py`, `~tests/test_entrypoint.py`, `~tests/fixtures/protocol_transports.py`,
`~docs/project/tickets/AGW-22-*.md`. **`utils/exceptions.py` and `utils/status_map.py` were declared
in scope and are unchanged**: `UnsafeXmlError` (→ 502 `XML_UNSAFE`) and `SoapFaultError` (→ real
status, `SOAP_FAULT`) were already present and already correct, so editing them to satisfy a file
list would have been change for its own sake. `helpers/internal/{__init__,filters_helper}.py` were
likewise already correct — AGW-13's raw-body branch covers `text/xml` and `application/soap+xml`,
which is precisely what R18-AC6 needs.

**The `xfail`s were in `test_envelope.py`, not `test_entrypoint.py`** as the story row records. One
`pytest.param` shared by two tests — the "2 SOAP xfail markers" — both now removed; the CONTRACT_ROWS
list is entirely unmarked, so all five protocols satisfy E1 and E11. `test_entrypoint.py`'s SOAP test
was rewritten rather than deleted: it asserted `'SOAP' not in protocol_mapping`, and a membership
test passes on the exact `None` defect H3 was about, so it now asserts the entry *is* `SoapRequest`
and that every registry value is a `BaseRequestClass` subclass.

### Verification

| Command | Result |
|---|---|
| `pytest` | **884 passed**, exit 0 (baseline at `fe9b351`: 800 passed + 2 xfailed) |
| coverage | **96.16%** total, `soap_client.py` **95.71%** — ratchet floor 95.7 holds |
| `pytest --randomly-seed=1 / 20250816 / 424242` | 884 passed on all three |
| `mypy async_gateway` | Success, 26 source files, exit 0 |
| `flake8 async_gateway tests` | **17 findings, unchanged from baseline** — zero in the new files |
| `grep -rn "'SOAP': None" async_gateway/` | 0 |
| `grep -rn "lxml" async_gateway/` | 0 (R19-AC7) |
| `grep -rn "except Exception" async_gateway/` | 0 |
| `tests/test_no_blocking_io.py` | 8 passed — the AST scan sees no banned form on any async path |

R19-AC7 caught a real defect during the run: the module docstring argued the XML decision by naming
the rejected libxml2-backed package, and a docstring is a grep match. The argument moved to the spec
and the tests, where it is already made; the module points at them.

### Attack payloads, and how each is refused

Four payloads rather than one representative, because they fail differently if the guard is wrong.

| Attack | Shape | Refused how |
|---|---|---|
| **Billion laughs** | `<!DOCTYPE lolz [` + nine nested entities, ~10⁹ chars on expansion | `scan_prolog` raises `UnsafeXmlError` at the `<!` token → `XML_UNSAFE`/502. `fromstring` never called; envelope `text` is still the few hundred bytes that arrived, which is the proof nothing expanded |
| **XXE file read** | `<!ENTITY xxe SYSTEM "file:///etc/passwd">` interpolated into `<Secret>` | same guard, same point — refused before the parser sees it |
| **XXE out-of-band** | external *parameter* entity `%ext;` → `http://127.0.0.1:1/evil.dtd` | same guard. No network callback is possible because no parse occurs |
| **DOCTYPE behind prolog noise** | a comment and an `<?xml-stylesheet?>` PI *before* the `<!DOCTYPE` | the scan walks comments and PIs rather than stopping at the first non-`<?xml` token, so it still reaches the declaration. A guard that stopped early would walk straight past this one |

**The false-positive side is asserted in the same breath, and carries equal weight.** A Fault
`<detail>` echoing an HTML error page containing the literal word `DOCTYPE` is *parsed normally*, as
are CDATA, base64 and plain prose carrying it. A substring search over the body passes all four
rejections above and fails all four of these — which is why the spec forbids that form and why both
directions are tested.

### Mutation proofs

Each guard was disabled, the suite re-run to confirm it went red, and the file restored from a `cp`
backup (`/tmp/soap_pristine.py`). No `git checkout`/`restore`/`stash` was used at any point.

| # | Mutation | Result |
|---|---|---|
| 1 | `scan_prolog`'s `<!` branch made unreachable (`if False and ...`) | **5 red** — all four attacks + the never-expanded assertion |
| 2 | Replaced with the forbidden `if 'DOCTYPE' in xml_text` substring form | **7 red** — the four attacks still pass their *rejection*, but cdata, plain-text and the HTML `<detail>` all go red. This is the mutation that proves the tests discriminate prolog-scoped from body-contains |
| 3 | An unguarded `fromstring` inserted *before* `scan_prolog`, guard otherwise intact | **4 red** — the error code is still correct on all four, and the `never_parsed` fixture catches the ordering anyway. Without that fixture this mutation would have shipped green |
| 4 | Fault raised only when `status >= 400` | **2 red** — the HTTP-200 Fault and the status-200 row. The exact case a transport-status-only check reports as success |
| 5 | `soap_action` injection characters emptied to `()` | **3 red** — quote, CRLF, LF |
| 6 | `refuse_multipart` branch made unreachable | **1 red**, *and it left a stray `response.txt` in the worktree* — the direct evidence that the MTOM refusal has to live in the transport, above the read that writes parts to disk |

Restoration verified: `git status --porcelain` clean, 82 SOAP tests green, no stray file on a clean run.

### How a Fault maps to the envelope

A Fault is a *successful HTTP exchange carrying a protocol-level failure*, so nothing about the
transport is synthesised:

- `ok=False` — decided by the **document**, never by the status. A Fault at HTTP 200 yields
  `ok=False`; tested at 200, 400, 500 and 503.
- `error.code == 'SOAP_FAULT'` (wire-stable), `error.message` carries the fault code and reason.
- `status_code` is **the real HTTP status the server sent** — 200 stays 200, 500 stays 500. No 502 is
  invented.
- `protocol_details['soap_fault']` carries a structured `SoapFault` flattened across both versions:
  1.1's unqualified `faultcode`/`faultstring`/`faultactor`/`detail` and 1.2's qualified
  `Code/Value`, `Reason/Text`, `Role`, `Detail` reach the same five keys, so a consumer never
  branches on the version to read a fault. The 1.2 `Subcode` chain is walked to its end — the outer
  `Value` is almost always the generic `env:Sender`, so stopping one level deep would discard the
  only part a caller can branch on.
- `text` and `headers` are on the envelope **before** the raise (invariant E11).

Ordering, which is not interchangeable: a **Fault beats the status** (a 500 carrying one is
`SOAP_FAULT`, not `HTTP_STATUS`), a **parse failure also beats the status** (an HTML proxy page at
502 is `SERIALIZATION`, not an unexplained bad gateway), and **only then the status** — so a 500
carrying neither is the `HTTP_STATUS` it is. All three directions are tested.

### Not done here, and why

- **README SOAP section (R18-AC8's last clause, R29).** Owned by AGW-28/S28, which rewrites the
  README wholesale and adds `tests/test_docs.py` to check the three consumer disclosures
  mechanically. Writing a section here that S28 would rewrite, against a doc test that does not yet
  exist, would be the duplicated work FI-15 warns about.
- **Packaging description still advertises XML and redis** (same criterion, same clause). Not in this
  story's declared file scope — `pyproject.toml` is not listed — and other lanes are concurrently
  editing it. Flagged rather than widened.

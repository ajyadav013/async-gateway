# AGW-22: `logic/soap_client.py`, from scratch

- **Status:** OPEN
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

_None recorded yet._

## Work Log

_Empty — opened at stage 1g, before implementation._

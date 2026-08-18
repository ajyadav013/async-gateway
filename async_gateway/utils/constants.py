"""The library's tunable defaults, in one place with their reasoning.

Every default a caller may override without writing code lives here rather
than as a literal at its use site, so changing one is a reviewable edit to
a named constant instead of a hunt through four protocol modules.

Several of these values were themselves findings: the transfer chunk was
64 times smaller than conventional, the retry base and its ceiling were
both zero (which is not backoff), and the breaker registry was unbounded.
Each therefore carries the comment explaining why the number is what it
is, because a bare number invites the next reader to change it back.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, FrozenSet

# The one intra-`utils` import, and it only goes this way: `redaction`
# imports nothing from the package, so naming it here cannot cycle. It is
# imported rather than duplicated because `CREDENTIAL_HEADERS` below is
# *derived* from it -- see the reasoning there.
from async_gateway.utils.redaction import SENSITIVE_HEADERS

HTTP_TIMEOUT = 15

# The lowest status a protocol treats as the remote side reporting failure.
# The URL download reads it too: it used to test one hand-named status
# (``STATUS_CODE_403 = 403``, a constant named after its own value -- L13),
# which meant every other failing status wrote its body to disk as the
# requested file.
HTTP_ERROR_STATUS = 400

#: How many consecutive failures against one destination open its circuit.
CIRCUIT_BREAKER_RETRY: Final[int] = 5

#: Seconds an open circuit stays open before one half-open trial is let
#: through. Named ``timeout`` in ``circuit_breaker_config``, which is the
#: key the README has always documented.
CIRCUIT_BREAKER_TIMEOUT: Final[float] = 60.0

#: Base seconds between retries. Non-zero (M13): a zero base with a zero
#: ceiling produced immediate, un-spaced, perfectly synchronised retries
#: against a dependency that was already failing -- the textbook
#: thundering herd, and the one shape a retry must never take.
CIRCUIT_BREAKER_DELAY: Final[float] = 0.1

#: Ceiling on one backoff wait, in seconds. Non-zero for the same reason:
#: the backoff computation clamps to it, so a zero ceiling silently
#: flattens exponential backoff back into no backoff at all.
CIRCUIT_BREAKER_MAX_DELAY: Final[float] = 10.0

#: Retry waits are randomised by default (M13). Two callers that start
#: retrying the same dead host at the same instant must not keep marching
#: in step, and jitter is the only thing that breaks the lockstep.
CIRCUIT_BREAKER_JITTER: Final[bool] = True

#: The backoff shape when the caller names none. Exponential, and
#: selected by this named key rather than by the magic
#: ``name == 'backoff'`` string the README never documented (M14).
CIRCUIT_BREAKER_BACKOFF: Final[str] = 'exponential'

#: How many destinations the breaker registry holds before it evicts the
#: least recently *used* one (M16). Process-lifetime state has to be
#: bounded or a client fanning out over many hosts leaks a breaker per
#: host; 256 is far above any realistic fan-out from one process and
#: small enough that the bound costs nothing.
BREAKER_REGISTRY_MAX: Final[int] = 256

#: The port recorded for a destination whose scheme has no default port.
#: Deliberately not ``0``: ``0`` is a legal port number, so using it as
#: "unknown" collapses every unknown-scheme destination onto one registry
#: key and re-creates M16 for exactly the callers whose scheme this
#: library did not anticipate.
UNKNOWN_PORT: Final[int] = -1

#: The port each protocol family answers on when the caller names none.
DEFAULT_PORTS: Final[Mapping[str, int]] = MappingProxyType({
    'http': 80,
    'https': 443,
    'ftp': 21,
    'sftp': 22,
})

#: 64 KiB, the conventional transfer chunk. 1024 bytes cost one await cycle
#: per kilobyte -- about 100,000 of them on a 100 MB upload -- for no
#: memory saving worth having (R14). Overridable per call through
#: ``http_file_upload_config['file_upload_chunk_size']`` and
#: ``http_file_download_config['file_download_chunk_size']``.
CHUNK_SIZE_CONSTANT: Final[int] = 65536

#: 64 MiB: the default ceiling on how much of a response body this library
#: will read. Comfortably above any realistic API response and above the
#: file sizes the README's examples download, and small enough that a
#: hostile endpoint cannot exhaust a default-sized container. Overridable
#: per call through ``protocol_info['max_response_bytes']``; there is no
#: "disable" sentinel, so a caller who needs more names a bigger number.
MAX_RESPONSE_BYTES: Final[int] = 67108864

#: How many redirects the library follows before it gives up. Overridable
#: per call through ``protocol_info['max_redirects']``.
MAX_REDIRECTS: Final[int] = 10

#: How deep a ``multipart/*`` response may nest before the read is
#: refused. The byte cap above bounds how *much* a hostile body can make
#: this process read; this bounds how far *down* it can make it walk, and
#: the two are independent: 2000 levels of empty nesting is 221 KB, far
#: under any realistic byte ceiling, and it exhausted the interpreter's
#: stack outright -- a ``RecursionError`` escaping ``request()`` where the
#: contract says every failure arrives as an ``ok=False`` envelope
#: (NEW-H2).
#:
#: 64 is generous past anything real. RFC 2046 nests ``multipart/mixed``
#: inside ``multipart/related`` inside ``multipart/signed`` in the deepest
#: shapes email and MTOM actually produce -- a handful of levels -- so a
#: legitimate body has orders of magnitude of headroom, and no honest
#: sender is anywhere near it. It is deliberately not derived from
#: ``sys.getrecursionlimit()``: the walk is iterative and has no frame
#: budget to track, so tying the limit to one would restate a dependency
#: that no longer exists.
MAX_MULTIPART_DEPTH: Final[int] = 64

#: How deep a SOAP Fault's ``<detail>`` may nest before this library
#: declines to re-serialise it. The same bound as the multipart cap above,
#: for the same reason and against the same shape of input: 1000 levels of
#: nesting is 7 KB on the wire -- nowhere near ``MAX_RESPONSE_BYTES`` --
#: and ``ElementTree.tostring`` recurses one frame per level, so a remote
#: server could exhaust the interpreter's stack and have its own Fault
#: reported as ``STACK_EXHAUSTED``/502. That let the *server* choose which
#: error code its caller saw for the server's own Fault (N5).
#:
#: The XML *parse* is already iterative and safe past 50k levels; only the
#: serialisation of the detail subtree recurses, so this bounds that step
#: alone. Crossing it does not discard the Fault: code, reason, subcodes
#: and actor are all read without recursing, so the Fault is still
#: reported as a Fault and only ``detail`` is withheld -- see
#: ``fault_detail_text`` in ``logic/soap_client.py``.
#:
#: 64 is generous past anything real. A Fault detail carries an
#: application error structure -- a code, a message, perhaps a list of
#: field errors -- which is a handful of levels; SOAP itself imposes no
#: nesting limit on it. Deliberately not derived from
#: ``sys.getrecursionlimit()``: an application is free to raise or lower
#: that, and a bound this library states is one it can hold to whatever
#: the embedding process chose.
MAX_FAULT_DETAIL_DEPTH: Final[int] = 64

#: The URL schemes an HTTP-family call may be dispatched to, on the initial
#: URL and on every redirect hop alike. R21 (a later story) reads it for
#: the initial URL; the owned redirect loop in ``request_helper`` reads it
#: for every hop, which is the half without which the other is not
#: enforcement at all (FI-16).
ALLOWED_SCHEMES: Final[FrozenSet[str]] = frozenset({'http', 'https'})

#: The statuses whose ``Location`` header the redirect loop follows.
REDIRECT_STATUSES: Final[FrozenSet[int]] = frozenset(
    {301, 302, 303, 307, 308})

#: The statuses that turn a POST into a bodyless GET when followed, and
#: the one that does so for every verb but HEAD. Both reproduce what
#: ``aiohttp`` did while it owned the loop: owning it ourselves must
#: preserve the semantics, not invent new ones. 307 and 308 are absent
#: from both, which is their whole point -- they re-issue the same verb
#: with the same body.
POST_TO_GET_REDIRECTS: Final[FrozenSet[int]] = frozenset({301, 302})
SEE_OTHER_STATUS: Final[int] = 303

#: The request headers a hop that **crosses an origin** still carries.
#: Everything not named here is dropped. Matched lower-cased.
#:
#: **This is an allowlist, and the inversion is the fix.** The library
#: shipped a denylist twice and leaked twice. The first version named
#: three headers and forwarded ``X-Api-Key`` (H1); the second took the
#: union of every credential list in the codebase and still forwarded
#: ``X-Vault-Token``, ``Private-Token``, ``X-Goog-Api-Key`` and seven
#: more, each measured arriving at a hostile host with its exact secret
#: (NEW-H1b). Both fixes were correct about the headers they named and
#: wrong about the shape: a denylist answers "is this one of the secrets
#: we thought of?", and the header that leaks is by definition the one
#: nobody thought of. Every vendor that invents a new auth header --
#: and they invent them continuously -- silently re-opens it.
#:
#: An allowlist asks the opposite question, "is this one of the few
#: headers we know is safe to hand a stranger?", and answers *no* by
#: default. That makes the unknown header the **safe** case rather than
#: the exploitable one, so the set stops needing to keep up with every
#: vendor on the internet. It fails closed: a header this library has
#: never heard of does not cross an origin, and the cost of being wrong
#: about an entry is one benign header the caller re-sends, not one
#: credential the caller cannot un-send.
#:
#: What earns a place here is a header that (a) carries no secret in any
#: deployment, and (b) describes the *request itself* rather than the
#: caller's relationship with the origin it was addressed to:
#:
#: * content negotiation -- ``accept``, ``accept-charset``,
#:   ``accept-encoding``, ``accept-language``. These describe what the
#:   client can parse and are meaningless as a credential.
#: * the body -- ``content-type``, ``content-length``,
#:   ``content-encoding``, ``content-language``, ``content-disposition``.
#:   A 307 or 308 re-sends the body verbatim, and a body whose type
#:   did not survive with it arrives as something the peer cannot parse.
#: * ``range``, which names bytes of the resource being fetched. The
#:   download path exists to be redirected at a CDN and a dropped
#:   ``Range`` silently turns a resumed download into a whole one.
#: * ``cache-control`` and ``pragma`` -- freshness directives.
#: * ``user-agent``, which identifies the client software. Dropping it
#:   makes the second hop look like a different client to every server
#:   that varies on it, and it has never been a secret.
#:
#: Three headers are pointedly **absent** despite looking benign, and
#: each absence is a decision rather than an oversight. ``referer``
#: carries the previous URL, and this library's own redaction machinery
#: exists because callers put tokens in query strings -- forwarding it
#: cross-origin would hand a stranger a URL the envelope redacts.
#: ``host`` and ``origin`` name the origin that was just left, so
#: forwarding either to a *different* origin is wrong on its face;
#: ``aiohttp`` sets ``host`` per connection anyway. ``if-none-match``
#: and ``if-modified-since`` carry validators minted by the previous
#: origin, which mean nothing to the new one.
CROSS_ORIGIN_SAFE_HEADERS: Final[FrozenSet[str]] = frozenset({
    'accept',
    'accept-charset',
    'accept-encoding',
    'accept-language',
    'cache-control',
    'content-disposition',
    'content-encoding',
    'content-language',
    'content-length',
    'content-type',
    'pragma',
    'range',
    'user-agent',
})

#: Credential-bearing request headers this module names in its own right,
#: beyond the ones :data:`~async_gateway.utils.redaction.SENSITIVE_HEADERS`
#: already classifies. Every entry is a header whose value is a bearer
#: token in the plain sense: possessing it is sufficient to act as the
#: caller, so a hostile ``Location`` receiving one is a full credential
#: leak and not merely an information disclosure. Matched lower-cased.
#:
#: The last ten are the ones NEW-H1b measured leaking. They are named
#: here even though :data:`CROSS_ORIGIN_SAFE_HEADERS` already drops them
#: for not being on it, because this set now has a *second* job the
#: allowlist cannot do -- see :data:`CREDENTIAL_HEADERS`.
_EXTRA_CREDENTIAL_HEADERS: Final[FrozenSet[str]] = frozenset({
    'x-api-key',
    'x-auth-token',
    'x-amz-security-token',
    'api-key',
    'x-csrf-token',
    'x-access-token',
    'x-auth-key',
    'x-session-token',
    'x-functions-key',
    'private-token',
    'x-goog-api-key',
    'dd-api-key',
    'x-shopify-access-token',
    'x-vault-token',
    'authentication',
})

#: Request headers known to carry a credential. Matched lower-cased.
#:
#: **This is no longer what decides a cross-origin hop** --
#: :data:`CROSS_ORIGIN_SAFE_HEADERS` is, and it drops everything absent
#: from it, so a credential header is dropped for the same reason any
#: unknown header is. What this set decides now is the two questions an
#: allowlist genuinely cannot answer:
#:
#: 1. **What a caller may not opt back in.** The per-call escape hatch
#:    ``protocol_info['cross_origin_headers']`` widens the allowlist into
#:    the *unknown* region, never into this one. A caller can declare
#:    ``X-Request-Id`` safe to forward; no caller can declare
#:    ``Authorization`` safe, because a header this library is certain is
#:    a credential is not a trade-off it offers.
#: 2. **What a supplied session may not carry.** ``aiohttp`` merges
#:    session defaults into every request and no hop can withhold them,
#:    so ``logic.http_client.validated_session`` refuses them at the
#:    boundary. It refuses on the allowlist for the same fail-closed
#:    reason the hop does; this set is what makes the *diagnostic* say
#:    "credential" and not merely "unrecognised".
#:
#: It stays derived from the redaction set for the reason it always was:
#: two literals answering "is this a credential?" drifted once, and the
#: drift was H1 -- ``x-api-key`` was redacted in the returned envelope
#: while being forwarded verbatim to whatever host a hostile ``Location``
#: named. Redacting a value asserts it is secret; forwarding it denies
#: that. The union keeps the contradiction unrepresentable.
CREDENTIAL_HEADERS: Final[FrozenSet[str]] = frozenset({
    'authorization',
    'cookie',
    'proxy-authorization',
}) | _EXTRA_CREDENTIAL_HEADERS | SENSITIVE_HEADERS

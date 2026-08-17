"""Constants."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, FrozenSet

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

#: Request headers that carry a credential and must not cross an origin
#: boundary on a redirect. Matched lower-cased.
CREDENTIAL_HEADERS: Final[FrozenSet[str]] = frozenset({
    'authorization',
    'cookie',
    'proxy-authorization',
})

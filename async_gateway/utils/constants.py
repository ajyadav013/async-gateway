"""Constants."""

from typing import Final, FrozenSet

HTTP_TIMEOUT = 15

STATUS_CODE_403 = 403
# The lowest status a protocol treats as the remote side reporting failure.
HTTP_ERROR_STATUS = 400

CIRCUIT_BREAKER_RETRY = 5
CIRCUIT_BREAKER_TIMEOUT = 60
CIRCUIT_BREAKER_DELAY = 0
CIRCUIT_BREAKER_MAX_DELAY = 0

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

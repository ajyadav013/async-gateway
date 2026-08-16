"""Credential redaction shared by the response envelope and the logger.

One implementation serves both, so what the envelope hides and what a log
record hides cannot drift apart -- a URL carrying ``?api_key=`` masked in
the returned envelope but written to the log in the clear would defeat the
point of redacting it at all.

Nothing here raises on the caller's data: a redactor that can fail turns a
logging call into an outage. The one parse that can fail (``redact_url``)
returns its input unchanged instead.

The masking this module performs is deliberately bounded and the bound is
part of the contract (invariant E9): header and cookie values, URL userinfo
and sensitive-named query parameters, URLs embedded anywhere inside a
free-text string, and payload values whose *key name* is sensitive down to
:data:`PAYLOAD_REDACTION_DEPTH`. Below that depth, and for a payload that
is not a mapping, the caller's own data is echoed verbatim.

One further bound is measured rather than assumed. ``redact_text`` only
matches URLs carrying an explicit ``scheme://``, so a scheme-less URL --
``host/p?api_key=...`` -- passes through it completely unchanged. Its
consumers therefore still carry that URL's query secrets:
``error['message']``, ``error['cause']`` and the log record's
``extra['traceback']``. The log record's ``extra['url']`` does not,
because ``redact_value`` composes ``redact_url`` over the result. The
remaining three are an open finding tracked separately: widening the
pattern to scheme-less strings would also mask arbitrary prose, so it is
a contract decision rather than a bug fix.
"""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import BasicAuth

REDACTED: Final[str] = '***redacted***'

SENSITIVE_HEADERS: Final[frozenset[str]] = frozenset({
    'authorization',
    'proxy-authorization',
    'cookie',
    'set-cookie',
    'x-api-key',
})

SENSITIVE_NAMES: Final[frozenset[str]] = frozenset({
    'api_key',
    'apikey',
    'access_token',
    'refresh_token',
    'token',
    'secret',
    'password',
    'passwd',
    'signature',
    'sig',
    'key',
    'auth',
})

# A mapping handed to the logger may be headers or a payload and nothing in
# its type says which, so `redact_value` masks against both name sets.
_ALL_SENSITIVE_NAMES: Final[frozenset[str]] = (
    SENSITIVE_NAMES | SENSITIVE_HEADERS)

PAYLOAD_REDACTION_DEPTH: Final[int] = 4

# `scheme://` up to the first character that cannot appear unescaped in a
# URL. Deliberately greedy about what it swallows: over-capturing trailing
# punctuation folds it into the query value that is about to be masked, so
# the bound can only ever mask more, never less.
#
# The scheme repetition is bounded because it is unbounded backtracking
# otherwise: on a long string carrying no `://` the engine retries the
# scheme run from every offset, which measured 35s on 200KB -- inside
# `log_failure`, on the event loop. 32 characters is far longer than any
# registered scheme, so the bound costs no match.
_EMBEDDED_URL: Final[re.Pattern[str]] = re.compile(
    r'[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s<>"\'`]+')


def normalise_param_names(raw: Any) -> frozenset[str]:
    """Turn a caller's ``redact_query_params`` into usable sensitive names.

    The one place caller-supplied data enters the redaction path, so it is
    the one place that has to cope with the shape being wrong. It fails
    *safe* in both directions: it never raises -- a typo in a call's
    configuration must not become a request that dies -- and it never
    returns fewer names than it was given, because the result is unioned
    with :data:`SENSITIVE_NAMES` and so can only ever mask more.

    A bare string is one name rather than an iterable of characters, which
    is what the caller who wrote ``'session_id'`` instead of
    ``['session_id']`` meant. Anything else iterable contributes each of
    its items, stringified; anything not iterable at all contributes
    itself, stringified.

    Args:
        raw: ``protocol_info['redact_query_params']`` exactly as the caller
            supplied it, including None when the key is absent.

    Returns:
        The casefolded names to add to the sensitive set, empty when the
        caller supplied nothing.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        return frozenset({raw.casefold()})
    if isinstance(raw, Iterable):
        return frozenset(str(name).casefold() for name in raw)
    return frozenset({str(raw).casefold()})


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Mask the value of every credential-bearing header.

    Header *names* are preserved, so a caller can still see that an
    ``Authorization`` header was sent without seeing what it carried.

    Args:
        headers: The header mapping to copy and mask.

    Returns:
        A new mapping whose sensitive values are :data:`REDACTED`.
    """
    return {
        name: REDACTED if name.casefold() in SENSITIVE_HEADERS else value
        for name, value in headers.items()
    }


def redact_cookies(cookies: Mapping[str, str]) -> dict[str, str]:
    """Mask every cookie value, keeping the cookie names.

    Unlike a header name, a cookie name carries no reliable signal about
    whether its value is a credential: a session identifier is every bit as
    sensitive as an ``Authorization`` header and is conventionally named
    anything at all. Invariant E9 requires that no cookie value reaches the
    envelope, so every value is masked rather than a guessed subset.

    Args:
        cookies: The cookie mapping to copy and mask.

    Returns:
        A new mapping whose every value is :data:`REDACTED`.
    """
    return {name: REDACTED for name in cookies}


def redact_url(url: str, *, extra_params: Collection[str] = ()) -> str:
    """Strip URL userinfo and mask sensitive query-parameter values.

    ``https://user:pw@host/p?api_key=x`` becomes
    ``https://host/p?api_key=***redacted***``. Parameter order and every
    other component are preserved, and a URL needing no masking is returned
    as it came in rather than re-encoded.

    Parameters are split on ``&`` only, which is what ``parse_qsl`` does by
    default and is the hardened reading: a legacy ``;`` separator would be
    seen as part of the preceding value, so it is masked *with* it rather
    than escaping as a name of its own.

    Args:
        url: The URL to redact.
        extra_params: Further query-parameter names to treat as sensitive,
            from ``protocol_info['redact_query_params']``, already
            normalised by :func:`normalise_param_names`.

    Returns:
        The redacted URL, or ``url`` unchanged when nothing needed masking
        or when it cannot be parsed. An unparseable URL is kept rather than
        discarded: it is diagnostic data, not a security boundary, and
        losing it hurts more than the residual risk.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    netloc = parts.netloc
    if '@' in netloc:
        netloc = netloc.rsplit('@', 1)[1]

    query = parts.query
    if query:
        sensitive = SENSITIVE_NAMES.union(
            name.casefold() for name in extra_params)
        pairs = parse_qsl(query, keep_blank_values=True)
        masked = [
            (name, REDACTED if name.casefold() in sensitive else value)
            for name, value in pairs
        ]
        if masked != pairs:
            # `safe` keeps the sentinel's asterisks literal. Percent-encoded
            # they would still hide the value, but a caller reading the
            # redacted URL could not tell a masked parameter from a real one.
            query = urlencode(masked, safe='*')

    if netloc == parts.netloc and query == parts.query:
        return url
    return urlunsplit(
        (parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_text(text: str, *, extra_params: Collection[str] = ()) -> str:
    """Mask every URL embedded anywhere inside a free-text string.

    :func:`redact_url` needs the whole string to be a URL. This one does
    not, which is what it exists for: the strings that reach a caller
    through ``error['message']``, ``error['cause']`` and the logged
    traceback are *sentences* with a URL somewhere in them --
    ``'RetriesExhausted: ftpx://host/p?api_key=...'`` -- and a
    whole-string test does not see that URL at all.

    Text that contains no URL is returned unchanged, so this is safe to
    apply to any message rather than only to ones suspected of carrying
    one.

    Args:
        text: Any human-readable string about to reach a caller.
        extra_params: Further query-parameter names to treat as
            sensitive, already normalised by
            :func:`normalise_param_names`. The built-in
            :data:`SENSITIVE_NAMES` apply either way -- this only ever
            adds to them.

    Returns:
        ``text`` with each embedded URL replaced by its redacted form.
    """
    def mask(match: 're.Match[str]') -> str:
        """Redact the one URL this match spans.

        Args:
            match: The matched URL.

        Returns:
            The redacted URL, substituted back in place.
        """
        return redact_url(match.group(0), extra_params=extra_params)

    return _EMBEDDED_URL.sub(mask, text)


def redact_payload(
    payload: Any,
    *,
    depth: int = PAYLOAD_REDACTION_DEPTH,
) -> Any:
    """Mask mapping values whose key name is sensitive, down to ``depth``.

    Args:
        payload: The request payload to echo back safely. Any type is
            accepted, because a caller may post a string, a file body or an
            arbitrary object.
        depth: How many container levels to descend. Each nested mapping or
            sequence consumes one level; below the last one the caller's
            own data is returned unchanged.

    Returns:
        A redacted copy for containers, or the value itself for anything
        else. The caller's own object is never mutated.
    """
    return _redact_recursive(payload, SENSITIVE_NAMES, depth)


def redact_value(
    value: Any,
    *,
    extra_params: Collection[str] = (),
) -> Any:
    """Redact one value of unknown type, dispatching on what it is.

    The logger's ``extra`` mapping carries URLs, header mappings, payloads
    and credential objects side by side, so it needs one entry point that
    applies the right redactor to each. Dispatching here rather than at
    each call site is what keeps a value in a log record and the same value
    in the envelope masked identically.

    A string gets *both* string maskers, unconditionally. There is no test
    of whether it "looks like a URL" first, because that predicate is
    itself the defect: it has now failed open twice, on two different
    strings. It first demanded a scheme *and* a netloc, so
    ``http:///p?api_key=...`` was echoed; narrowed to demanding only a
    scheme, ``host/p?api_key=...`` was echoed. Composing both maskers
    instead makes this mask **whatever the envelope masks, by
    construction** -- the envelope masks its ``url`` with
    :func:`redact_url`, and so, always, does this -- rather than only on
    the strings some predicate happened to classify correctly. The claim
    is about *what is masked*, not about byte-identical rendering: where
    :func:`redact_text` masks first, :func:`redact_url` finds nothing left
    to mask and returns the string untouched, so
    ``'http://h/p?api_key=S&x=<tag>'`` comes back from here as
    ``'http://h/p?api_key=***redacted***&x=<tag>'`` where
    :func:`redact_url` alone would re-encode the tail to
    ``'...&x=%3Ctag%3E'``. Both mask the same secret; only the
    percent-encoding of the untouched remainder differs. Neither masker
    needs the guard: :func:`redact_text`
    substitutes nothing when it matches nothing, and :func:`redact_url`
    returns its input unchanged when it has nothing to mask or cannot
    parse it.

    The *order* remains load-bearing. :func:`redact_text` runs first
    because it is the only one of the two that can find a URL buried in
    prose. Running :func:`redact_url` first on a traceback line would mask
    nothing: ``'ValueError: http://host/p?api_key=S'`` has the truthy
    scheme ``valueerror``, so the whole string parses as one URL whose
    query is empty -- and would then have consumed the real URL. With the
    text pass first that same line comes back already masked, after which
    :func:`redact_url` finds nothing to do and returns it unchanged.

    :func:`redact_url` earns its second pass on the strings
    :func:`redact_text` masks only partly: the pattern ends each match at
    the first character that cannot appear unescaped in a URL, so a secret
    value containing a space, a quote, a backtick or an angle bracket has
    its tail left outside the match and echoed. Its cost is that a
    whole-string parse reads any prose *after* a URL as part of the query
    it is masking and swallows it, and that :func:`urllib.parse.urlsplit`
    strips every tab, carriage return and newline from the whole string
    while it is at it -- ``'multi'``, a newline and ``'line ?api_key=S'``
    come back joined as ``'multiline ?api_key=***redacted***'``. Both are
    the fail-closed
    direction, both fire only when masking does, and both are harmless for
    the single-line ``url`` and ``protocol`` fields this entry point
    serves. They are also confined to it: the one field that is genuinely
    multi-line prose, the traceback, is masked by :func:`redact_text`
    alone.

    Args:
        value: Anything about to be written to a log record.
        extra_params: The caller's additional sensitive query-parameter
            names, forwarded to both string passes. It must be the *same*
            set the envelope was built with, or the log and the envelope
            would disagree about what a secret is.

    Returns:
        The redacted equivalent, or ``value`` itself when no redactor
        applies to its type.
    """
    if isinstance(value, BasicAuth):
        return REDACTED
    if isinstance(value, str):
        return redact_url(
            redact_text(value, extra_params=extra_params),
            extra_params=extra_params)
    return _redact_recursive(
        value, _ALL_SENSITIVE_NAMES, PAYLOAD_REDACTION_DEPTH)


def _redact_recursive(
    value: Any,
    names: frozenset[str],
    depth: int,
) -> Any:
    """Copy ``value``, masking values under a sensitive key name.

    Args:
        value: The value to descend into.
        names: Casefolded key names whose values are masked.
        depth: Remaining container levels to descend.

    Returns:
        A redacted copy for a mapping or a non-text sequence, otherwise the
        value unchanged.
    """
    # Ahead of the depth guard deliberately. The bound exists because below
    # it a *key name* proves nothing about the value under it -- but a
    # `BasicAuth` needs no key name to be recognised, so there is nothing
    # for the bound to protect against and every reason to keep masking.
    if isinstance(value, BasicAuth):
        return REDACTED
    if depth <= 0:
        return value
    if isinstance(value, Mapping):
        return {
            key: REDACTED if str(key).casefold() in names
            else _redact_recursive(item, names, depth - 1)
            for key, item in value.items()
        }
    if isinstance(value, (str, bytes, bytearray)):
        return value
    if isinstance(value, Sequence):
        return [_redact_recursive(item, names, depth - 1) for item in value]
    return value

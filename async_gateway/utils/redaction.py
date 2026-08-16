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
and sensitive-named query parameters, sensitive-named ``name=value`` pairs
anywhere after a ``?`` or ``&`` -- whose names are matched percent-decoded,
so ``api%5Fkey`` is the ``api_key`` every server will read it as -- URLs
embedded anywhere inside a free-text string, and payload values whose
*key name* is sensitive down to
:data:`PAYLOAD_REDACTION_DEPTH`. Below that depth, and for a payload that
is not a mapping, the caller's own data is echoed verbatim.

That query-pair rule asks nothing about the string around it, and the
asking is what it replaces. A predicate classifying "is this a URL?"
failed open three times on one leak: it first demanded a scheme *and* a
netloc, then only a scheme, and a caller who wrote ``host/p?api_key=...``
has neither -- so ``redact_text`` was a total no-op on that URL and its
secret reached ``error['message']``, ``error['cause']`` and the log
record's ``extra['traceback']`` in the clear. Each narrowing was a
further guess at where URL-ness begins. Masking on the ``?``/``&``
structure alone needs no such predicate, so no fourth guard exists to
fail open. Its cost is over-masking prose that happens to read
``...?password=hunter2``, which is the direction every other bound here
errs in too.
"""

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, Final
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

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

# One `name=value` pair introduced by `?` or `&`, wherever it sits. There
# is deliberately no test of what surrounds it: see the module docstring
# for why URL-ness is not a question this module is willing to ask again.
#
# Linear overall, which the measurement above makes non-optional -- but
# not because nothing backtracks. A `?` with no `=` after it does make
# the greedy name run give back one character at a time before the match
# fails. What bounds it is that the retries cannot compound: every class
# excludes the delimiter that ends it -- a name cannot hold `=`, a value
# cannot hold `?` or `&` -- so a run is capped by its own length, the
# runs after two different delimiters cannot overlap, and only `?` and
# `&` can start a match at all. The work therefore sums to O(n) across
# the string rather than multiplying the way the scheme run above did.
_QUERY_PAIR: Final[re.Pattern[str]] = re.compile(
    r'([?&])([^?&=\s]+)=([^?&\s]*)')


def _sensitive_names(extra_params: Collection[str]) -> frozenset[str]:
    """Combine the built-in sensitive names with the caller's own.

    Both string maskers resolve their set through here rather than each
    building one, because a log record and an envelope that disagreed
    about which names are secret would leak on whichever surface held the
    smaller set.

    Args:
        extra_params: Further query-parameter names to treat as
            sensitive, already normalised by
            :func:`normalise_param_names`.

    Returns:
        The casefolded names whose query values are masked.
    """
    return SENSITIVE_NAMES.union(name.casefold() for name in extra_params)


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
        sensitive = _sensitive_names(extra_params)
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
    """Mask sensitive query values and embedded URLs inside free text.

    :func:`redact_url` needs the whole string to be a URL. This one does
    not, which is what it exists for: the strings that reach a caller
    through ``error['message']``, ``error['cause']`` and the logged
    traceback are *sentences* with a URL somewhere in them --
    ``'RetriesExhausted: ftpx://host/p?api_key=...'`` -- and a
    whole-string test does not see that URL at all.

    Two passes run, in this order.

    The first masks the value of every ``name=value`` pair introduced by
    ``?`` or ``&`` whose *name* is sensitive -- as written or
    percent-decoded, so ``?api%5Fkey=`` is masked exactly as
    ``?api_key=`` is -- wherever the pair appears.
    It never asks whether the text around the pair is a URL, so a
    scheme-less ``host/p?api_key=...`` is masked exactly as
    ``https://host/p?api_key=...`` is. That symmetry is the whole point:
    the predicate that used to decide it was this module's
    longest-lived leak, and the module docstring records how it failed.

    The second redacts each embedded ``scheme://`` URL whole, which the
    first cannot -- userinfo carries no ``?``, so ``user:pw@`` is
    invisible to a query-pair rule. It runs *second* because the first
    then leaves it nothing to rewrite: :func:`redact_url` rebuilds a
    query only when its own masking changed something, so an
    already-masked query keeps the caller's percent-encoding rather than
    being normalised through :func:`urllib.parse.urlencode`.

    Text carrying neither is returned unchanged, so this is safe to apply
    to any message rather than only to ones suspected of carrying a
    secret, and masking is idempotent -- a value already
    :data:`REDACTED` is replaced by itself.

    Args:
        text: Any human-readable string about to reach a caller.
        extra_params: Further query-parameter names to treat as
            sensitive, already normalised by
            :func:`normalise_param_names`. The built-in
            :data:`SENSITIVE_NAMES` apply either way -- this only ever
            adds to them.

    Returns:
        ``text`` with every sensitive query value and every embedded URL
        replaced by its redacted form. A masked value ends where the
        pair does, and whitespace ends a pair: ``?api_key=my secret
        key`` comes back as ``?api_key=***redacted*** secret key``,
        because a value permitted to run past a space would swallow the
        rest of the sentence. :func:`redact_value` rescues that case for
        the fields it serves by composing :func:`redact_url` over the
        result, but the three surfaces masked by this function alone --
        ``error['message']``, ``error['cause']`` and the logged
        traceback -- get no such second pass and keep the tail.
    """
    sensitive = _sensitive_names(extra_params)

    def mask_pair(match: 're.Match[str]') -> str:
        """Redact the one query value this match spans, if it is secret.

        The name is tested both as written and percent-decoded, because
        ``api%5Fkey`` is ``api_key`` to every server that will read it
        and a raw-text comparison sees two different names. Testing both
        rather than only the decoded form keeps the extension point from
        being the narrower of the two: a caller who declared a name
        containing a literal ``%`` means that name, and decoding it away
        would drop a secret the caller took the trouble to declare.
        :func:`urllib.parse.unquote` never raises -- a malformed ``%zz``
        comes back verbatim -- so this adds no way for a redactor to
        fail. ``+`` is left alone deliberately: it means a space only
        under the form-encoding convention, no sensitive name contains a
        space, and decoding it could only ever lose a caller's literal
        ``+``.

        Args:
            match: The matched ``?``/``&``, name and value.

        Returns:
            The pair with its value masked, or the match unchanged when
            the name is not a sensitive one. A masked pair keeps the
            caller's own spelling of the name, encoding and all.
        """
        delimiter, name, _value = match.groups()
        if (name.casefold() not in sensitive
                and unquote(name).casefold() not in sensitive):
            return match.group(0)
        return f'{delimiter}{name}={REDACTED}'

    def mask_url(match: 're.Match[str]') -> str:
        """Redact the one URL this match spans.

        Args:
            match: The matched URL.

        Returns:
            The redacted URL, substituted back in place.
        """
        return redact_url(match.group(0), extra_params=extra_params)

    return _EMBEDDED_URL.sub(mask_url, _QUERY_PAIR.sub(mask_pair, text))


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
    :func:`redact_text` masks only partly. Its query-pair pass ends a
    value at whitespace -- a value permitted to run past a space would
    swallow the rest of the sentence -- so ``?api_key=my secret key``
    comes back from it with two thirds of the secret still attached. The
    other characters that end its *URL* pass, the ones that cannot appear
    unescaped in a URL, no longer need rescuing here: a quote, a backtick
    or an angle bracket does not end a query-pair value. Its cost is that a
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

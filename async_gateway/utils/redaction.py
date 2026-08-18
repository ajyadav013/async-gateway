"""Credential redaction shared by the response envelope and the logger.

One implementation serves both, so what the envelope hides and what a log
record hides cannot drift apart -- a URL carrying ``?api_key=`` masked in
the returned envelope but written to the log in the clear would defeat the
point of redacting it at all.

Nothing here raises on the caller's data: a redactor that can fail turns a
logging call into an outage. The one parse that can fail (``redact_url``)
falls back to the regex passes instead, which need no parse to succeed.

The masking this module performs is deliberately bounded and the bound is
part of the contract (invariant E9): header and cookie values, URL userinfo
-- in a URL that parses *and* in one that does not -- and sensitive-named
query parameters, sensitive-named ``name=value`` pairs
anywhere after a ``?``, ``&`` or ``;`` -- whose names are matched
percent-decoded, so ``api%5Fkey`` is the ``api_key`` every server will read
it as -- URLs
embedded anywhere inside a free-text string, and payload values whose
*key name* is sensitive down to
:data:`PAYLOAD_REDACTION_DEPTH`. Below that depth, and for a payload that
is not a mapping, the caller's own data is echoed verbatim.

No masker returns its input untouched on a parse failure. ``redact_url``
used to, and the echo was a leak: the URL that fails to parse is precisely
the one the caller is about to be told about by name, so the failure path
is the *most* exposed surface rather than an obscure one. It now falls
back to the regex passes, which need no parse to succeed.

The userinfo rule has the same history for the same reason, and its
fourth round is what settled it. Each round, a hand-rolled recogniser
here decided where an authority begins and a URL parser decided
differently -- ``%2F``, then a tab, then a backslash, then whitespace --
and each fix taught the recogniser the reported spelling while leaving the
disagreement itself in place, so the next spelling was always available.
Two independent readings of one string will always have a fifth. So they
are no longer independent: ``_parser_view`` deletes exactly what
``urllib.parse`` deletes, from ``urllib.parse``'s own constant, before
the scan runs. The offsets scanned are the offsets the parser will walk,
and the two cannot disagree by construction rather than by having
remembered enough examples.

Where the *parsers themselves* disagree -- ``aiohttp`` finds an authority
in ``http: //u:PW@h/p`` where ``urlsplit`` finds a path -- no
normalisation can reconcile them, so the separator run accepts every
spelling and masks on any reading. That is the fail-closed direction this
module errs in throughout.

The query rule has now had the same history, one level up, and it took
three rounds to stop unifying the wrong thing. Not two readings of one
string but two *maskers*: ``redact_text``'s split on ``;`` and
``redact_url``'s did not, so ``?x=1;api_key=S`` was masked in
``error['message']`` and published in the clear in the envelope ``url``
and in ``validated_url``'s ``ConfigurationError`` -- the surfaces that
call ``redact_url`` alone (N1). Round one taught the ``;`` to one rule.
Round two shared the separator *set* between both. Round three found
them disagreeing anyway, because sharing what a separator *is* says
nothing about *where each masker looks*: ``redact_url`` scanned
``parts.query`` alone while ``redact_text`` scanned the whole string, so
``/p;api_key=S`` in a path segment and ``/p#frag?api_key=S`` after a
fragment leaked on exactly the same surfaces as before.

So the unified thing is now the **surface**, not the constant. There is
one pair scan, ``_mask_in_string``, run over the whole string by both
maskers; ``redact_url`` adds only the userinfo drop that needs a parsed
netloc. No component is enumerated anywhere, so no component can be
forgotten -- which is the property the two previous rounds each failed
to buy, having unified a value rather than the code path.

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
import urllib.parse as _parse
from bisect import bisect_left
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, Final, Optional
from urllib.parse import unquote, urlsplit, urlunsplit

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

# The characters that separate one query pair from the next. **This is
# the module's one definition of what a query separator is**, and there
# is now exactly one rule reading it -- `_QUERY_PAIR`, which both
# maskers run via `_mask_in_string`. It used to be read by two, which is
# the arrangement the module docstring records three rounds of findings
# against.
#
# `;` is in the set because it is a query separator too. It is the legacy
# form -- once recommended by the HTML 4.01 spec, still parsed by PHP,
# Java servlet containers and CGI code -- and a server that reads it
# reads `?a=1;api_key=S` as two parameters, the second of them secret.
_QUERY_SEPARATORS: Final[str] = '&;'

# What introduces the *first* pair of a run. `?` opens a query; `#`
# opens a fragment, and a fragment carries `name=value` pairs in the one
# flow that matters most here -- OAuth 2.0's implicit grant returns
# `#access_token=...`, a live credential, by design. Both are here for
# the same reason: a pair after either is read as a pair by something.
_PAIR_INTRODUCERS: Final[str] = '?#'

# Every character that can bound a pair, for the classes below. Derived
# rather than retyped, for the reason `_SEPARATOR_RUN` is.
_QUERY_DELIMITERS: Final[str] = _PAIR_INTRODUCERS + _QUERY_SEPARATORS

# One `name=value` pair introduced by any of those delimiters, wherever it
# sits. There is deliberately no test of what surrounds it: see the module
# docstring for why URL-ness is not a question this module is willing to
# ask again.
#
# Linear overall, which the 35s measurement above makes non-optional --
# but not because nothing backtracks. A `?` with no `=` after it does make
# the greedy name run give back one character at a time before the match
# fails. What bounds it is that the retries cannot compound: every class
# excludes the delimiters that end it -- a name cannot hold `=`, a value
# cannot hold a delimiter -- so a run is capped by its own length, the
# runs after two different delimiters cannot overlap, and only a delimiter
# can start a match at all. The work therefore sums to O(n) across the
# string rather than multiplying the way the scheme run above did.
#
# Building the classes from `_QUERY_DELIMITERS` preserves that argument by
# construction rather than by review: a separator is excluded from both
# classes in the same breath it is added to the set, so the linearity
# argument cannot be invalidated by adding one.
#
# Both pair patterns are built by this one function, from that one
# delimiter set, and they differ in exactly one axis: whether whitespace
# ends a value. That axis is the *only* legitimate difference between
# the two maskers -- see `_URL_QUERY_PAIR` -- so making it the sole
# parameter is what stops a second difference being introduced by
# writing a second pattern out by hand, which is how the last three
# findings arrived.


def _pair_pattern(*, whitespace_ends_a_value: bool) -> 're.Pattern[str]':
    """Build a ``name=value`` pair pattern over the shared delimiters.

    Args:
        whitespace_ends_a_value: True for free text, where a value that
            ran past a space would swallow the rest of a sentence; False
            for a whole string already known to be one URL, where a
            space is inside the value and the tail is still the secret.

    Returns:
        The compiled pattern, capturing the delimiter, the name and the
        value.
    """
    delims = re.escape(_QUERY_DELIMITERS)
    space = r'\s' if whitespace_ends_a_value else ''
    return re.compile(
        r'([{delims}])([^{delims}{space}=]+)=([^{delims}{space}]*)'.format(
            delims=delims, space=space))


_QUERY_PAIR: Final[re.Pattern[str]] = _pair_pattern(
    whitespace_ends_a_value=True)

# The same rule for a string that is *entirely* one URL, where a space
# inside a value is part of the value rather than the end of it.
#
# `redact_url` needs this and `redact_text` must not have it. A secret
# spelled `?api_key=my secret key` reaches the envelope whole, so ending
# the value at the space would publish two thirds of it; the same value
# inside a *sentence* has no end but the space, and a rule that ran past
# it would mask the rest of the traceback. That difference is real, it
# predates this fix, and it is the one difference between the two
# maskers that is not a defect.
#
# So it is expressed as an argument to one shared builder rather than as
# a second hand-written pattern. The delimiters -- and therefore which
# components a pair can be found in -- come from the same constants for
# both, which is the property three rounds of findings were about: a
# component or separator cannot be taught to one masker and missed by
# the other, because neither masker enumerates any.
_URL_QUERY_PAIR: Final[re.Pattern[str]] = _pair_pattern(
    whitespace_ends_a_value=False)

# `scheme:` plus whatever separator run follows it, captured separately.
# Only *this* much is a pattern; where the userinfo ends is decided by
# `_mask_userinfo` below, offset by offset, and deliberately not by a
# character class -- see that function for why.
#
# The scheme run is bounded at 32 for the same reason `_EMBEDDED_URL`'s
# is: unbounded, it retries from every offset on a long string carrying
# no `:`. 32 characters is far longer than any registered scheme.
#
# `//` is the separator RFC 3986 gives; every other spelling is what
# group 2 exists to consume -- `/\/`, `\\`, `///`, ` //`, or nothing at
# all. A browser, `yarl` and `urlsplit` disagree about which of those
# introduce an authority, and that disagreement is precisely the defect,
# so this takes no side and accepts them all.
#
# Whitespace is in the set because the two readings genuinely differ on
# it and one of them leaks: `urlsplit` reads the space in
# `http: //user:PW@host/p` as the start of a *path* and reports no
# password, while `aiohttp` round-trips the same string to
# `http:///%20//user:PW@host/p` and puts the password in the envelope
# `url`, `extra['url']` and the logged traceback (NEW-M1c).
_AUTHORITY_SEPARATORS: Final[str] = '/\\ \t\n\r\v\f'

# Each separator in *both* spellings it can reach this module in: the
# character, and its percent-encoded form, which is what a round trip
# through `aiohttp` produces -- the backslash in `http:/\/user:PW@host/p`
# arrives back as `http:///%5C/user:PW@host/p`, and the space as `%20`.
# Masking one spelling and not the other masks the URL the caller wrote
# and publishes the one the library reports.
#
# Generated from the set rather than written out, and that is the whole
# point of the line. The hand-written alternation it replaces listed
# `%2F` and `%5C` and not `%20`, so adding the space to the character
# class above fixed `http: //` and left `http:///%20//` -- the same
# spelling, after one round trip, still leaking. Deriving both forms from
# one list makes that class of miss unrepresentable: a separator cannot
# be half-added.
#
# ``(?i:...)`` scopes the case-insensitivity to the escapes alone, so
# ``%5c`` and ``%5C`` both match while nothing else in the pattern --
# the scheme run, the character class -- changes meaning.
_SEPARATOR_RUN: Final[str] = '(?:[{}]|(?i:{}))*'.format(
    re.escape(_AUTHORITY_SEPARATORS),
    '|'.join(
        f'%{ord(char):02x}' for char in sorted(_AUTHORITY_SEPARATORS)),
)

# What this does **not** do is try to enumerate the characters `urlsplit`
# *ignores*. That job belongs to `_parser_view` below, which deletes them
# the way CPython does before this pattern ever runs -- because guessing
# at them by character class is the mistake that produced four rounds of
# findings.
#
# The run is a group of this pattern rather than a second pattern
# matched at an offset, because `Pattern.match` returns `Optional` and a
# run of `*` never fails -- so the None arm would be unreachable code
# guarded by an untestable branch. A group cannot be absent.
_SCHEME_PREFIX: Final[re.Pattern[str]] = re.compile(
    r'([A-Za-z][A-Za-z0-9+.\-]{0,31}:)(' + _SEPARATOR_RUN + ')')

# The three characters that end an authority in RFC 3986 -- the start of
# the path, the query, or the fragment. Userinfo cannot reach past one.
_AUTHORITY_END: Final[frozenset[str]] = frozenset('/?#')

_AT_SIGN: Final[frozenset[str]] = frozenset('@')

# The characters CPython deletes from a URL before parsing it, taken from
# `urllib.parse` itself rather than restated. They are tab, newline and
# carriage return -- WHATWG's rule -- and importing the constant is the
# point of this line: a hand-written copy is a fifth spelling waiting to
# diverge, which is the entire history of this module.
#
# A private name, so it is read defensively: on an interpreter that has
# renamed or dropped it, the fallback is the same three characters the
# WHATWG URL standard fixes, and the standard is what CPython is
# tracking. A test asserts the two agree on this interpreter, so the
# fallback cannot quietly become the operative value.
_URL_IGNORED: Final[frozenset[str]] = frozenset(
    getattr(_parse, '_UNSAFE_URL_BYTES_TO_REMOVE', ('\t', '\n', '\r')))


def _offsets_of(text: str, characters: frozenset[str]) -> list[int]:
    """List every offset in ``text`` holding one of ``characters``.

    One linear pass, so :func:`_mask_userinfo` can answer "where is the
    next one after here?" by bisection rather than by re-scanning from
    each scheme. That is what keeps it linear overall: a per-scheme
    ``str.find`` loop is quadratic on a string that is mostly schemes,
    and this masker runs inside ``log_failure`` on the event loop, where
    the previous pattern's backtracking measured 35s on 200KB.

    Args:
        text: The string to index.
        characters: The characters whose offsets are recorded.

    Returns:
        The offsets, ascending.
    """
    return [offset for offset, char in enumerate(text) if char in characters]


def _parser_view(text: str) -> tuple[str, Optional[list[int]]]:
    """Return ``text`` as ``urlsplit`` sees it, plus the map back to it.

    **This is the fix for the class of defect, not for one more input
    shape.** Four rounds of findings have now come from the same root
    cause: a hand-rolled recogniser here deciding where an authority
    begins, and ``urlsplit`` deciding differently. The scanner and the
    parser being two independent readings of one string is what
    guarantees a fifth spelling exists; the only durable answer is to
    stop having two.

    So the deletion ``urlsplit`` performs is performed here first, from
    ``urllib.parse``'s own constant. ``http:<TAB>//user:PW@[::1/p``
    reaches a server as ``http://user:PW@[::1/p`` -- the tab is simply
    gone -- while the scanner read the ``//`` after it as the start of a
    *path*, ended the authority before the ``@``, and masked nothing. It
    now scans the same characters the parser will, so the two cannot
    disagree about where the credential is (NEW-M1c).

    Masking has to land on the caller's *original* string, though: the
    returned diagnostic is a record of what they actually passed, and
    silently deleting the tab from it would misreport that. Hence the
    map -- offset in the parser's view to offset in the original -- so
    the scan happens in one coordinate system and the slicing in the
    other.

    None rather than an identity list when nothing was deleted, which is
    the overwhelmingly common case and the one whose cost is measured:
    this runs inside ``log_failure`` on the event loop and a 200 KB
    traceback would otherwise buy a 200,000-element list to say nothing.

    Args:
        text: The string about to be scanned for credentials.

    Returns:
        The text with every character :data:`_URL_IGNORED` deleted, and a
        list whose *i*-th entry is where view offset *i* sits in the
        original -- one entry longer than the view, so the end offset
        maps too. The list is None when nothing was deleted and the two
        coordinate systems are therefore the same.
    """
    if not _URL_IGNORED.intersection(text):
        return text, None
    origin = [
        offset for offset, char in enumerate(text)
        if char not in _URL_IGNORED
    ]
    view = ''.join(text[offset] for offset in origin)
    origin.append(len(text))
    return view, origin


def _mask_userinfo(text: str) -> str:
    r"""Mask the credential in every ``scheme:...userinfo@`` in ``text``.

    The credential a query-pair rule structurally cannot see, because
    userinfo carries no ``?`` and no ``=``. :func:`redact_url` drops it
    for a URL it can *parse*; this covers the ones it cannot -- and it is
    the third round of findings against that job. Each round was one more
    input shape defeating one more character class:

    * ``http://user:PASS@[::1/p`` -- ``urlsplit`` raises on the unclosed
      bracket and the fallback had no userinfo rule at all, so the
      password reached the ``ConfigurationError`` a public ``request()``
      raises (M1/AGW-34).
    * ``http://user:TAB<tab>SECRET@[::1/p`` -- the class excluded
      ``\\s``, so the match failed and *nothing* was masked. ``urlsplit``
      strips tabs, newlines and carriage returns outright, so the server
      reads a password the pattern could not even see (NEW-M1b).
    * ``http://u:PARTA@SSPARTB@[::1/p`` -- the pattern stopped at the
      first ``@`` and published the tail. RFC 3986 ends userinfo at the
      **last** ``@`` before the host, and so does ``urlsplit``: the real
      password there is ``PARTA@SSPARTB`` (NEW-M1b).
    * ``http:/\\/user:BACKSLASHPW@host/p`` -- ``//`` was required
      literally. That URL *parses*, into an empty netloc, so neither this
      rule nor the unparseable fallback ran and the password reached the
      envelope ``url`` and the log record's ``extra['url']`` in the clear
      (NEW-M1b).
    * ``http:<TAB>//user:SECRETPW@[::1/p`` -- the separator run accepted
      ``/``, ``\\``, ``%2F`` and ``%5C`` but not whitespace, while
      ``urlsplit`` *deletes* tab, newline and carriage return before it
      parses. The scanner therefore read the ``//`` as the start of a
      path where the parser read an authority, ended before the ``@``,
      and masked nothing (NEW-M1c).

    So this stops asking what a userinfo *looks like*. It takes the two
    facts RFC 3986 fixes -- an authority follows a scheme, and it ends at
    the first ``/``, ``?`` or ``#`` -- and masks up to the **last** ``@``
    before that end, whatever lies between. Three of the five bypasses
    above are characters some class excluded, and there is no class here
    to exclude them from: tab, newline, space, backslash, percent escape
    and non-ASCII are all simply *inside* the credential.

    The remaining two are the same defect twice: this scanner and
    ``urlsplit`` reading one string differently, which is guaranteed to
    keep producing spellings for as long as they are two independent
    readings. So they are no longer independent. The scan runs over
    :func:`_parser_view` -- the string with exactly the characters
    CPython deletes deleted, from ``urllib.parse``'s own constant -- so
    the offsets this walks are the offsets the parser will walk. That is
    the structural half; accepting any separator run rather than ``//``
    alone is the other, and covers the readings that differ without any
    character being deleted (``aiohttp`` sees an authority in
    ``http: //u:PW@h/p`` where ``urlsplit`` sees a path).

    That is the fail-closed direction, and it is the point: over-masking
    a string that merely looks like an authority costs a diagnostic,
    under-masking one publishes a password. A rule that enumerates what
    a credential may contain has to be right about every hostile
    spelling; this one has to be right about where an authority ends,
    which RFC 3986 already decided.

    Two bounds keep it off prose. A scheme is required, so a bare
    ``user:PASS@host`` and an email address in a sentence are untouched.
    And a *bare* userinfo -- one with no ``:`` -- is masked only when a
    separator run was present, so ``mailto:bob@corp.example`` keeps its
    address while ``https://TOKEN@host`` is masked whole: that is how
    several APIs pass a token, nothing distinguishes it from a username,
    and guessing wrong in the other direction publishes the token.

    A ``user:password`` keeps the user and masks the password -- the same
    bargain :func:`redact_headers` strikes, that a name is diagnostic and
    a value is not.

    Args:
        text: The string to mask.

    Returns:
        ``text`` with every such credential replaced by :data:`REDACTED`,
        or ``text`` itself when it holds no ``@`` to mask before. What
        comes back is built from the caller's *original* string, deleted
        characters and all: the parser view exists to decide where the
        credential is, never to rewrite the diagnostic the caller reads.
    """
    if '@' not in text:
        return text

    view, origin = _parser_view(text)
    ats = _offsets_of(view, _AT_SIGN)
    ends = _offsets_of(view, _AUTHORITY_END)
    masked: list[str] = []
    read = 0
    for scheme in _SCHEME_PREFIX.finditer(view):
        # A `scheme:` found *inside* a credential already masked is not a
        # second authority; `read` is where the last mask ended, in view
        # coordinates like everything else in this loop.
        if scheme.start() < read:
            continue
        start = scheme.end()
        separators = scheme.group(2)

        after = bisect_left(ends, start)
        stop = ends[after] if after < len(ends) else len(view)
        # The last `@` before the authority ends: RFC 3986's rule, and
        # `urlsplit`'s. `bisect_left(ats, stop) - 1` is the last one at
        # or before `stop`; it belongs to *this* authority only if it is
        # also at or after `start`.
        last = bisect_left(ats, stop) - 1
        if last < 0 or ats[last] < start:
            continue
        credential = ats[last]

        userinfo = view[start:credential]
        colon = userinfo.find(':')
        keep_to = start if colon < 0 else start + colon + 1
        if colon < 0 and not separators:
            continue
        # Offsets decided in the view, sliced from the original: the
        # caller reads back the string they passed, minus the secret.
        masked.append(text[_at(origin, read):_at(origin, keep_to)])
        masked.append(REDACTED)
        read = credential

    masked.append(text[_at(origin, read):])
    return ''.join(masked)


def _at(origin: Optional[list[int]], offset: int) -> int:
    """Translate a parser-view offset into an offset in the original.

    Args:
        origin: The map :func:`_parser_view` returned, or None when it
            deleted nothing and the two coordinate systems coincide.
        offset: The offset in the view.

    Returns:
        The corresponding offset in the original string.
    """
    return offset if origin is None else origin[offset]


def _mask_in_string(
    text: str,
    sensitive: frozenset[str],
    *,
    pairs: 're.Pattern[str]' = _QUERY_PAIR,
) -> str:
    """Apply the two masking passes that cannot recurse.

    **The one masking surface both public maskers run.**
    :func:`redact_text` calls it on free text and :func:`redact_url`
    calls it on a whole URL, and neither has a rule of its own -- which
    is the fix for three rounds of the two disagreeing. They differ only
    in the ``pairs`` argument, and only about whitespace.

    Split out of :func:`redact_text` so :func:`redact_url` can reuse it
    without the two calling each other forever: :func:`redact_text`'s
    third pass hands each embedded URL to :func:`redact_url`, so a
    re-entry into :func:`redact_text` would hand the same URL straight
    back. Everything here is pure regex substitution and re-enters
    nothing.

    Args:
        text: The string to mask.
        sensitive: The casefolded names whose query values are masked,
            already resolved by :func:`_sensitive_names`.
        pairs: Which pair pattern to scan with -- :data:`_QUERY_PAIR`
            for free text, where whitespace ends a value, or
            :data:`_URL_QUERY_PAIR` for a string that is entirely one
            URL, where it does not. Both come from
            :func:`_pair_pattern` and therefore from one delimiter set,
            so they cannot differ about which components a pair can sit
            in.

    Returns:
        ``text`` with sensitive query values and URL userinfo masked.
    """
    def mask_pair(match: 're.Match[str]') -> str:
        """Redact the one query value this match spans, if it is secret.

        Whether the name is sensitive is :func:`_is_sensitive`'s
        question, and asking it there rather than here keeps the answer
        in one place, alongside the caller-supplied names
        :func:`_sensitive_names` folds in.

        Args:
            match: The matched delimiter, name and value.

        Returns:
            The pair with its value masked, or the match unchanged when
            the name is not a sensitive one. A masked pair keeps the
            caller's own spelling of the name, encoding and all, and its
            own delimiter -- rewriting a legacy ``;`` to ``&`` would
            change what the string says the caller sent.
        """
        delimiter, name, _value = match.groups()
        if not _is_sensitive(name, sensitive):
            return match.group(0)
        return f'{delimiter}{name}={REDACTED}'

    return _mask_userinfo(pairs.sub(mask_pair, text))


def _is_sensitive(name: str, sensitive: frozenset[str]) -> bool:
    """Say whether this parameter name is one whose value is a secret.

    The one place the question is answered, so the two maskers cannot
    answer it differently. It was already the case that both tested the
    name as written *and* percent-decoded -- ``api%5Fkey`` is ``api_key``
    to every server that will read it, and a caller who declared a name
    containing a literal ``%`` means that name -- but they tested it in
    two separate expressions, which is the same shape of duplication that
    produced N1 one level up.

    Args:
        name: The parameter name exactly as the caller spelled it.
        sensitive: The casefolded names whose values are masked, already
            resolved by :func:`_sensitive_names`.

    Returns:
        True when the value belonging to this name must be masked.
    """
    return (name.casefold() in sensitive
            or unquote(name).casefold() in sensitive)


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
    """Strip URL userinfo and mask sensitive ``name=value`` pairs.

    ``https://user:pw@host/p?api_key=x`` becomes
    ``https://host/p?api_key=***redacted***``. Parameter order and every
    other component are preserved, and a URL needing no masking is returned
    as it came in rather than re-encoded.

    **This function no longer has a masking rule of its own.** The pair
    masking is :func:`_mask_in_string`, exactly as :func:`redact_text`
    runs it, over the whole URL; the only thing this adds is dropping the
    userinfo out of a netloc ``urlsplit`` was able to identify. That
    division is the fix for a class of defect, and the class is worth
    naming because this is its fourth round:

    * round 1 -- ``_QUERY_PAIR`` did not know ``;`` was a separator.
    * round 4 (N1) -- the separator set was unified into
      :data:`_QUERY_SEPARATORS`, but only :func:`redact_text` used it;
      this function still split with ``parse_qsl``.
    * this round -- the separator set *is* shared, and the two maskers
      still disagreed, because they scanned different **components**.
      This one masked ``parts.query`` alone, so a secret anywhere else in
      the URL was published: ``/p;api_key=S`` in the path (RFC 3986 path
      parameters, which PHP and servlet containers read as parameters)
      and ``/p#frag?api_key=S`` after a fragment were both masked by
      :func:`redact_text` and published in the clear here -- in the
      envelope ``url`` on the ``ok=True`` and ``ok=False`` paths alike,
      and in ``validated_url``'s ``ConfigurationError``.

    Each round unified one more thing the two rules had a copy of: first
    the separator character, then the separator *set*. Sharing a constant
    stops them disagreeing about what a separator is; it says nothing
    about *where each one looks*, and that was the next place to
    disagree. So the surface is unified rather than the constant: there
    is one scan over one string, and a component cannot be forgotten
    because no component is enumerated. A future URL grammar that puts a
    pair somewhere new is covered by construction -- the scan never asked
    which component it was in.

    What remains here is only reassembly, and it is deliberately the
    smaller half: :func:`urlsplit` is consulted to find a *credential*,
    never to find a pair.

    Args:
        url: The URL to redact.
        extra_params: Further query-parameter names to treat as sensitive,
            from ``protocol_info['redact_query_params']``, already
            normalised by :func:`normalise_param_names`.

    Returns:
        The redacted URL, or ``url`` unchanged when nothing needed
        masking. A URL that cannot be *parsed* is still masked, by the
        same scan -- it is kept rather than discarded, because it is
        diagnostic data rather than a security boundary, but keeping it
        is not the same as keeping its credentials.
    """
    # The pair scan first and unconditionally, over the whole string. It
    # needs no parse to succeed, which is why the unparseable URL below
    # needs no separate rule: `_mask_in_string` has already run on it.
    #
    # `_URL_QUERY_PAIR` because the argument is a whole URL: a space in
    # `?api_key=my secret key` is inside the value, and ending there
    # would publish two thirds of the secret in the envelope `url`.
    masked = _mask_in_string(
        url, _sensitive_names(extra_params), pairs=_URL_QUERY_PAIR)

    try:
        parts = urlsplit(masked)
    except ValueError:
        # Echoing the input here was the leak (M1/AGW-34). The ticket
        # accepted it on the condition that no caller-visible surface
        # reached this function uncomposed, and that condition was
        # false: `validated_url`'s own "url is not parseable" message
        # calls it directly, so a public `request()` raised
        # `ConfigurationError: url is not parseable:
        # http://user:SUPERSECRET123@[::1/p` with the password intact --
        # the one input that reaches this branch being, definitionally,
        # the one the caller gets told about.
        #
        # An unclosed IPv6 bracket defeats `urlsplit` and not a
        # character class, so the scan above stands on its own here.
        return masked

    netloc = parts.netloc
    if '@' not in netloc:
        # A parse that *succeeded* is not proof there is no credential to
        # drop -- only proof that `urlsplit` found no authority to put it
        # in. `http:/\/user:BACKSLASHPW@host/p` splits happily, into an
        # empty netloc and a path holding the whole credential, so a
        # netloc-only rule dropped nothing and the password reached the
        # envelope `url` and the log record's `extra['url']` in the clear
        # (NEW-M1b).
        #
        # `_mask_in_string` already ran `_mask_userinfo` over the whole
        # string, so that credential is masked wherever the parser put
        # it. There is nothing further to reassemble: rebuilding through
        # `urlunsplit` could only re-encode components this function was
        # asked to leave alone.
        return masked

    netloc = netloc.rsplit('@', 1)[1]
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def redact_text(text: str, *, extra_params: Collection[str] = ()) -> str:
    """Mask sensitive query values and embedded URLs inside free text.

    :func:`redact_url` needs the whole string to be a URL. This one does
    not, which is what it exists for: the strings that reach a caller
    through ``error['message']``, ``error['cause']`` and the logged
    traceback are *sentences* with a URL somewhere in them --
    ``'RetriesExhausted: ftpx://host/p?api_key=...'`` -- and a
    whole-string test does not see that URL at all.

    Three passes run, in this order.

    The first masks the value of every ``name=value`` pair introduced by
    ``?``, ``&`` or ``;`` whose *name* is sensitive -- as written or
    percent-decoded, so ``?api%5Fkey=`` is masked exactly as
    ``?api_key=`` is -- wherever the pair appears.
    It never asks whether the text around the pair is a URL, so a
    scheme-less ``host/p?api_key=...`` is masked exactly as
    ``https://host/p?api_key=...`` is. That symmetry is the whole point:
    the predicate that used to decide it was this module's
    longest-lived leak, and the module docstring records how it failed.

    The second masks ``scheme://user:pw@`` userinfo, which the first
    structurally cannot see -- userinfo carries no ``?`` and no ``=``.
    Only :func:`redact_url` used to cover this, and only for a URL that
    parses, which left ``http://user:pw@[::1/p`` echoing its password
    (M1). Being a regex it needs no parse, so it covers both.

    The third redacts each embedded ``scheme://`` URL whole. It runs
    *last* because the first two then leave it nothing to rewrite:
    :func:`redact_url` rebuilds a
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

    def mask_url(match: 're.Match[str]') -> str:
        """Redact the one URL this match spans.

        Args:
            match: The matched URL.

        Returns:
            The redacted URL, substituted back in place.
        """
        return redact_url(match.group(0), extra_params=extra_params)

    return _EMBEDDED_URL.sub(mask_url, _mask_in_string(text, sensitive))


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

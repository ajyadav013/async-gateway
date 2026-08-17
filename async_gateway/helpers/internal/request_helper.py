"""Request Helper -- the HTTP transport boundary.

Everything here returns **data**, never a response shape: an
:class:`HttpResult` that ``logic/http_client.py`` copies into the envelope
it was handed. Only ``utils/envelope.py`` ever constructs a
``GatewayResponse``.

That boundary is the fix for FI-7. ``make_http_request`` used to start from
a response dict pulled out of an optional keyword no caller ever passed, so
it built a *fresh* dict and returned it as if it were the caller's envelope,
silently dropping the url, payload and timestamp the entry point had already
put there.

R14 adds three things to that boundary. Every exchange carries an explicit
deadline, and it bounds the exchange as a *whole* -- every hop of a
redirect chain spends the one budget the caller set, because handing each
hop a fresh ``total`` would multiply the stated deadline by
``max_redirects + 1``. Every read is bounded by ``max_response_bytes``,
before the read where a ``Content-Length`` says so and mid-stream where it
does not. And **this module owns the redirect loop**: the transport is
called with ``allow_redirects=False`` and every ``Location`` is resolved
and re-checked against ``allowed_schemes`` *before* the next request is
issued, which is the only version of that check that can refuse a hop
rather than observe one already made (FI-16).

Owning the loop means inheriting what ``aiohttp`` used to provide, not
only what it used to prevent. The chain-wide deadline is one of the two
guarantees that came with its loop; the redirect trace event
(:func:`record_redirect`) is the other.
"""

import asyncio
import mimetypes
import time
from collections.abc import Collection
from pathlib import PurePath
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Text,
    Tuple,
    TypedDict,
)
from urllib.parse import urljoin, urlsplit

import aiofiles
import aiohttp
from async_gateway.helpers.common.file_helper import download_file_from_s3
from async_gateway.helpers.internal import (
    MULTIPART_MEDIA_PREFIX,
    filter_for_media_type,
    media_type_of,
)
from async_gateway.helpers.internal.filters_helper import get_ssl_config
from async_gateway.utils.constants import (
    CHUNK_SIZE_CONSTANT,
    CREDENTIAL_HEADERS,
    HTTP_TIMEOUT,
    MAX_RESPONSE_BYTES,
    POST_TO_GET_REDIRECTS,
    REDIRECT_STATUSES,
    SEE_OTHER_STATUS,
)
from async_gateway.utils.exceptions import (
    ConfigurationError,
    TransportError,
)
from async_gateway.utils.http_file_config import (
    HTTP_VERBS,
    guard_declared_length,
    iter_capped,
    resolve_verb,
    response_too_large,
)
from async_gateway.utils.paths import resolve_caller_path, safe_writer
from async_gateway.utils.redaction import redact_url

#: Where a download lands when the caller names no path. The README
#: documents ``download_filepath`` as part of an *optional* config block, so
#: the config has to be usable without it rather than reading it with a
#: bare ``.get()`` and handing None to ``open()`` (M10).
DEFAULT_DOWNLOAD_FILEPATH = 'response.txt'

#: What a file part declares when its name yields no media-type guess.
#: Named rather than inlined because aiohttp injects this exact value
#: itself when no ``content_type`` is passed, and telling the two apart is
#: the whole point of :func:`build_upload_form` passing one.
DEFAULT_UPLOAD_MEDIA_TYPE = 'application/octet-stream'

#: The response header naming a redirect's target. Read off a
#: ``CIMultiDict``, so the spelling here is presentational only.
LOCATION_HEADER = 'Location'

#: The ``results_collector`` key ``request_tracer``'s ``on_request_start``
#: callback writes the current request's start instant into. Named here
#: because :func:`record_redirect` has to read it to reproduce
#: ``aiohttp``'s base instant for ``on_request_redirect``, and a key read
#: by one module and written by another is exactly the string that goes
#: stale silently.
REQUEST_START_KEY = 'on_request_start'

#: Builds the transport keyword arguments carrying one request's body.
#:
#: A *factory* rather than the body itself, because the two things that
#: re-issue a request -- a retry and a redirect hop -- both need a body the
#: previous send has not already consumed. An ``aiohttp.FormData`` yields
#: its parts once and an async generator is exhausted by the first read, so
#: a body carried over is sent as **zero bytes** while the server answers
#: 200: a truncated upload reported as a success (H9, AGW-38). Every
#: producer of a body in this module is therefore a coroutine function
#: called once per attempt *and* once per hop.
BodyFactory = Callable[[], Awaitable[Dict[Text, Any]]]

#: One followed redirect hop, measured but not yet written: each attached
#: tracer's ``results_collector`` paired with the seconds from *that
#: collector's own* ``on_request_start`` to the hop, or None where it
#: carried no usable one.
#:
#: A list of pairs rather than a single float, because the base instant is
#: per tracer and two tracers may legitimately disagree about it -- one
#: attached to a caller's long-lived session, one to this call. Collapsing
#: them to one number would silently pick a winner.
RedirectEvent = List[Tuple[Dict[Text, Any], Optional[float]]]

# Populate the mimetypes tables here, at import, rather than letting the
# first `guess_type` call do it. `guess_type` initialises lazily, and that
# initialisation stats every path in `mimetypes.knownfiles` and reads the
# ones that exist (on macOS, `/etc/apache2/mime.types`) -- blocking
# filesystem I/O, which `build_upload_form` would otherwise perform on the
# event loop during the first upload of the process. That is exactly what
# R20 bans, and it is invisible to the AST scan in
# `tests/test_no_blocking_io.py` because the read is lazy and indirect.
# Import is where synchronous I/O is legitimate, on the expectation that
# the consuming process imports this package while starting up rather than
# from inside a coroutine -- an expectation about the consumer, not a
# guarantee this module can make: a lazy `import async_gateway` inside a
# running coroutine pays this read on the loop, once. Afterwards
# `guess_type` is an in-memory dict lookup either way.
mimetypes.init()


class _HttpResultOptional(TypedDict, total=False):
    """The keys of :class:`HttpResult` that are present only sometimes.

    Split into its own base because ``typing.NotRequired`` is 3.11+ and
    this package supports 3.10.

    Attributes:
        decode_error: Why the body could not be decoded as text. Present
            *only* when it could not, so its absence is the success
            signal and there is no None to confuse with "no error".
    """

    decode_error: Text


class HttpResult(_HttpResultOptional):
    """One HTTP exchange, as data.

    Attributes:
        status_code: The status the remote side returned.
        headers: Response headers, exactly as received and unredacted --
            redaction happens where this is copied into the envelope.
        cookies: Response cookie values by name.
        text: The decoded body; ``''`` when there was none. When
            ``decode_error`` is present this is the *lossily* decoded body
            rather than nothing at all, because a caller who cannot have
            the exact bytes is still better served by what can be salvaged
            than by a key that was never set (M9).
    """

    status_code: int
    headers: Dict[Text, Text]
    cookies: Dict[Text, Text]
    text: Text


async def fetch_file(file_config: Dict):
    """Download file from s3 or from given link or.

    just read from the local pod file path.

    The HTTP branch writes through
    :func:`~async_gateway.utils.paths.safe_writer` (R22): the path is
    canonicalised before the open, ``O_NOFOLLOW`` refuses a symlink at
    it, the mode is 0600, an existing file is refused unless
    ``overwrite`` is True, and a failure part-way leaves no partial
    file. The S3 branch writes inside ``aioboto3`` and is not reachable
    from here.

    :param file_config: Dict contains s3 config,
    download link, local filepath etc.
    file_config[local_filepath] is mandatory
    :param file_config['overwrite']: optional, default False.
    :raises PathContainmentError: if ``local_filepath`` is a symbolic
    link, or names a directory rather than a file.
    :raises ConfigurationError: if it exists and ``overwrite`` is False.
    :raises UnsupportedVerbError: If ``request_type`` names no verb in
    ``HTTP_VERBS``.
    """
    if file_config.get('s3_config'):
        await download_file_from_s3(
            file_config['local_filepath'],
            **file_config['s3_config'],
        )
    elif file_config.get('file_download_path'):
        # Add separate aio params when required
        request_type = file_config.get('request_type', 'get')
        target = await resolve_caller_path(file_config['local_filepath'])
        async with aiohttp.ClientSession(
                headers=file_config.get('headers'),
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as session:
            # R21 covers this site too, even though R25 deletes the whole
            # function one story later. Leaving the *last* unbounded
            # `getattr` behind on the grounds that it is scheduled for
            # removal is how the grep this criterion is written against
            # comes back non-empty, and it would make the story's own
            # claim -- that no caller-named verb reaches an attribute
            # unchecked -- false for as long as the deletion is pending.
            request_obj = resolve_verb(
                session, request_type, allowed=HTTP_VERBS,
                setting='request_type')
            session_obj = request_obj(file_config['file_download_path'])
            async with session_obj as response:
                contents = await response.content.read()
                async with safe_writer(
                    target,
                    overwrite=file_config.get('overwrite') is True,
                ) as file_obj:
                    await file_obj.write(contents)


async def file_upload(
        file_name=None,
        file_upload_chunk_size=CHUNK_SIZE_CONSTANT):
    """Generates the chunk of file in a file stream."""
    async with aiofiles.open(file_name, 'rb') as f:
        chunk = await f.read(file_upload_chunk_size)
        while chunk:
            yield chunk
            chunk = await f.read(file_upload_chunk_size)


async def handle_multipart_response(
    resp: aiohttp.ClientResponse,
    http_file_download_config: Optional[Dict[Text, Any]],
    *,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> Text:
    r"""Write a multipart response to disk and return what was written.

    Three things this does that its predecessor did not (M7). It writes the
    part **bytes** to a binary file, where ``response_file.write(str(data))``
    wrote the literal text ``b'\x89PNG'`` into a text file and corrupted
    every download. It reads each part **to completion**, where a single
    ``read_chunk()`` silently truncated any part over 8 KiB. And it stops
    when ``reader.next()`` returns None, which happens before ``at_eof()``
    goes True and used to raise ``AttributeError`` on the very next line.

    The bytes are accumulated in a list joined once, not with
    ``response_data = response_data + ...`` inside a ``while True``, which
    is quadratic in the body size.

    The file is opened and written through ``aiofiles``, so a part arriving
    while other requests are in flight suspends this coroutine rather than
    the whole event loop (R20).

    It is opened through
    :func:`~async_gateway.utils.paths.safe_writer` (R22). This is a
    caller-supplied path reached from a *response*, and the caller does
    not choose when a multipart body arrives -- so the same guarantees
    the streamed download gets apply here: the path is canonicalised
    before it is opened, ``O_NOFOLLOW`` refuses a symlink planted at it,
    the mode is 0600, an existing file is refused unless
    ``download_filepath``'s config carries ``overwrite: True``, and a
    part that fails mid-read leaves no partial file behind.

    The running total is bounded by ``max_response_bytes`` for the same
    reason the other two read paths are (R14): the accumulator below is an
    in-memory list of every part, so an endpoint choosing the body size
    used to choose this process's allocation.

    Args:
        resp: The response to read the multipart body from.
        http_file_download_config: The caller's download config, or None.
            ``download_filepath`` defaults to
            :data:`DEFAULT_DOWNLOAD_FILEPATH`.
        max_response_bytes: Ceiling on the total bytes read across all
            parts, defaulting to the documented 64 MiB.

    Returns:
        Everything written, decoded with errors replaced. Multipart carries
        arbitrary binary, so a ``str`` rendering of it is best-effort by
        construction; the file on disk is the faithful copy.

    Raises:
        ResponseTooLargeError: Once the parts read exceed
            ``max_response_bytes``. The remainder of the body is not read.
        PathContainmentError: If ``download_filepath`` is a symbolic
            link, or names a directory rather than a file.
        ConfigurationError: If it exists and the config does not carry
            ``overwrite: True``.
    """
    config = http_file_download_config or {}
    response_file_name = (
        config.get('download_filepath') or DEFAULT_DOWNLOAD_FILEPATH)
    target = await resolve_caller_path(response_file_name)
    reader = aiohttp.MultipartReader.from_response(resp)
    parts: List[bytes] = []
    total = 0
    async with safe_writer(
            target, overwrite=config.get('overwrite') is True
    ) as response_file:
        while not reader.at_eof():
            part = await reader.next()
            if part is None:
                break
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                total += len(chunk)
                if total > max_response_bytes:
                    raise response_too_large(total, max_response_bytes)
                parts.append(chunk)
                await response_file.write(chunk)
    return b''.join(parts).decode(errors='replace')


def redirect_target(
    current_url: Text,
    location: Text,
    *,
    allowed_schemes: Collection[Text],
    hop: int,
    redact_params: Collection[Text],
) -> Text:
    """Resolve one ``Location`` and prove its scheme is allowed.

    Resolved against the URL that produced it, so a relative ``Location``
    -- ``/next``, ``../sibling`` -- becomes the absolute URL the next
    request will actually be issued to, and it is *that* value the scheme
    check reads. Checking the header verbatim would pass every relative
    location without looking at anything.

    The rejection happens here, before the caller issues the next request,
    which is the whole of FI-16: ``aiohttp``'s own loop follows first and a
    trace callback could only observe a hop already made, so an
    ``https://`` call redirected to ``ftp://``, ``file://`` or plain
    ``http://`` bypassed the initial-URL scheme check entirely.

    Args:
        current_url: The URL whose response carried ``location``.
        location: The raw ``Location`` header value.
        allowed_schemes: The schemes this call may be dispatched to.
        hop: Which redirect this is, counting from 1, named in the
            rejection so a chain's offending link is identifiable.
        redact_params: The caller's additional sensitive query-parameter
            names; the message names a URL and must not leak its
            credentials.

    Returns:
        The absolute URL to issue the next request to.

    Raises:
        ConfigurationError: If the resolved target's scheme is not in
            ``allowed_schemes``, or if the target cannot be parsed at all.
    """
    try:
        target = urljoin(current_url, location)
        parts = urlsplit(target)
        # ``urlsplit`` parses the authority *lazily*: a ``Location`` whose
        # port is out of range or not a number splits without complaint
        # and raises only when the port is first read. Read it here,
        # inside the guard, so a hostile header answers ConfigurationError
        # like every other unusable target -- rather than escaping
        # ``request()`` as a bare ValueError from whichever later reader
        # happened to touch it first, which was :func:`same_origin`.
        _ = (parts.hostname, parts.port)
        scheme = parts.scheme.lower()
    except ValueError as err:
        raise ConfigurationError(
            f'redirect hop {hop} from '
            f'{redact_url(current_url, extra_params=redact_params)} '
            f'names a Location that is not parseable as a url') from err
    if scheme not in allowed_schemes:
        raise ConfigurationError(
            f'redirect hop {hop} from '
            f'{redact_url(current_url, extra_params=redact_params)} '
            f'targets scheme {scheme!r}, which is not in allowed_schemes '
            f'{sorted(allowed_schemes)}; it was not followed')
    return target


def same_origin(left: Text, right: Text) -> bool:
    """Report whether two URLs share a scheme, host and port.

    Args:
        left: One absolute URL.
        right: The other absolute URL.

    Returns:
        True when the two are the same origin. A URL that names its
        default port explicitly reads as a *different* origin from one
        that omits it, which errs towards stripping credentials that did
        not have to be stripped rather than forwarding ones that did.
    """
    first, second = urlsplit(left), urlsplit(right)
    return (first.scheme, first.hostname, first.port) == (
        second.scheme, second.hostname, second.port)


def without_credentials(
    headers: Optional[Dict[Text, Text]],
) -> Optional[Dict[Text, Text]]:
    """Return ``headers`` with every credential-bearing entry removed.

    Applied to a hop that crosses an origin boundary, which is what
    ``aiohttp`` did for itself while it owned the redirect loop. Owning the
    loop without reproducing it would forward the caller's
    ``Authorization`` header to whatever host a hostile endpoint named --
    a credential leak introduced *by* the fix for one.

    Args:
        headers: The headers sent on the previous hop, or None.

    Returns:
        A new mapping without the credential headers, or None when there
        were no headers at all.
    """
    if not headers:
        return headers
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in CREDENTIAL_HEADERS
    }


def measure_redirect(
    trace_collectors: Collection[Dict[Text, Any]],
) -> RedirectEvent:
    """Measure a followed hop against each tracer's own view of its start.

    ``aiohttp`` timed ``on_request_redirect`` as ``loop.time() -
    context.on_request_start``, and while it owned the loop
    ``on_request_start`` was **the redirecting hop's own start** -- its
    trace context is per-request and each hop of its chain is one request.
    Reproducing that is the whole job here: a value measured from the
    start of the *chain* instead diverges from ``aiohttp``'s by everything
    the chain spent before the last redirecting hop, which on a chain
    whose delay sits on an earlier hop is the entire delay (0.30s against
    ``aiohttp``'s 0.001s, measured -- AGW-15 round 4).

    Measured **here, at the hop**, and not where the value is written.
    The write happens after the loop, by which time the tracer's
    ``on_request_start`` has been overwritten by the request the hop led
    to -- so reading the base instant then would measure the *final*
    request, not the redirecting one. Capturing at the hop is what makes
    the base instant the right one.

    Measured **per collector**, against that collector's own
    ``on_request_start``, rather than against one instant chosen for all
    of them. Each tracer keeps its own clock reading and this library does
    not get to decide which of two disagreeing tracers is right; a
    collector carrying no usable start instant yields None and is reported
    below as a followed hop whose *timing* is unknown, which is the honest
    answer and not a zero that would read as "instantaneous".

    Args:
        trace_collectors: The ``results_collector`` mapping of each
            attached tracer, or an empty collection when tracing is off.

    Returns:
        One ``(collector, elapsed)`` pair per collector, in the order
        given. ``elapsed`` is seconds from that collector's own
        ``on_request_start`` to now, or None when it holds no numeric one.
    """
    now = asyncio.get_running_loop().time()
    measured: RedirectEvent = []
    for collector in trace_collectors:
        started = collector.get(REQUEST_START_KEY)
        measured.append((
            collector,
            now - started if isinstance(started, (int, float)) else None))
    return measured


def record_redirect(event: Optional[RedirectEvent]) -> None:
    """Write the redirect trace event ``aiohttp`` can no longer fire.

    ``on_request_redirect`` cannot fire once the transport is called with
    ``allow_redirects=False``, so the two keys it used to set went
    permanently dead the moment this module took the loop over:
    ``request_tracer[*]['is_redirect']`` reported False on a chain this
    library had just followed. ``request_tracer`` is a top-level
    ``GatewayResponse`` key that consumers branch on, so owning the loop
    has to keep writing what the callback wrote -- preserving the
    semantics rather than inventing new ones.

    Written **once, after the last hop**, rather than as each hop is
    followed. ``aiohttp`` ran its loop inside a single traced request, so
    ``on_request_start`` fired once for the whole chain; this loop issues
    each hop as its own request, so that callback fires per hop and resets
    ``is_redirect`` to False again. A flag written at the hop would be
    erased by the request the hop leads to. The *timing* the write carries
    is measured at the hop for that same reason -- see
    :func:`measure_redirect`.

    "After the last hop" means after the loop, **however it ended**. The
    caller invokes this from a ``finally``, because a chain that ran out
    of time, outran ``max_redirects`` or died in the transport had still
    provably followed the hops it followed -- and recording only on the
    successful return reported ``is_redirect`` False on every one of
    them, which is the same dead flag this function was written to fix,
    surviving on the exits nothing measured.

    Args:
        event: What :func:`measure_redirect` captured at the last hop
            followed, or None when no hop was followed -- in which case
            nothing is recorded at all and ``is_redirect`` keeps the False
            ``on_request_start`` gave it. That None is what keeps a
            *refused* hop untraced: the caller assigns the value only
            after the scheme check has passed. A collector whose measured
            elapsed is None is still flagged as redirected; only its
            timing is withheld, because a hop was provably followed and
            reporting a base-less zero would be a fabricated measurement.

    Returns:
        None.
    """
    if event is None:
        return
    for collector, elapsed in event:
        if elapsed is not None:
            collector['on_request_redirect'] = elapsed
        collector['is_redirect'] = True


def hop_deadline(
    timeout: aiohttp.ClientTimeout,
    deadline: Optional[float],
    *,
    url: Text,
    redact_params: Collection[Text],
    hop: int,
) -> aiohttp.ClientTimeout:
    """Return the deadline for the next hop out of what the chain has left.

    ``aiohttp``'s ``ClientTimeout(total=)`` bounded the whole redirect
    chain while ``aiohttp`` owned the loop. Owning the loop and passing
    the caller's ``timeout`` unchanged to each hop hands every one of them
    a fresh ``total``, so a chain of slow hops costs the caller up to
    ``max_redirects + 1`` times the deadline they set -- eleven times, on
    the default bound. Subtracting what the chain has already spent is
    what keeps the caller's number the caller's number (R14).

    Args:
        timeout: The deadline the call was configured with, whose
            non-total fields are per-connection and carry through
            unchanged.
        deadline: The monotonic instant the chain must finish by, or None
            when ``timeout`` sets no total and there is nothing to divide.
        url: The URL the chain started at, for the diagnostic.
        redact_params: The caller's additional sensitive query-parameter
            names, so that diagnostic carries no credential.
        hop: How many redirects have been followed so far, named in the
            message so a chain that ran out of time says where.

    Returns:
        ``timeout`` itself when there is no total to divide, else a copy
        of it whose ``total`` is the time the chain has left.

    Raises:
        asyncio.TimeoutError: When the chain has no time left. The same
            exception ``aiohttp`` raises for its own total, so it is
            classified and reported as the ``TIMEOUT``/504 it always was.
    """
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError(
            f'the exchange with '
            f'{redact_url(url, extra_params=redact_params)} exceeded its '
            f'{timeout.total}s deadline after {hop} redirect hops')
    return aiohttp.ClientTimeout(
        total=remaining,
        connect=timeout.connect,
        sock_connect=timeout.sock_connect,
        sock_read=timeout.sock_read)


def after_redirect(
    status: int,
    request_type: Text,
    body_filters: Dict,
) -> Tuple[Text, Dict]:
    """Return the verb and body the next hop is issued with.

    Reproduces ``aiohttp``'s own rule rather than inventing one: a 301 or
    302 answering a POST, and a 303 answering anything but a HEAD, are
    re-issued as a bodyless GET; a 307 or 308 repeats the verb and the
    body. ``params`` is dropped on every hop because the ``Location``
    carries its own query string.

    Args:
        status: The redirect status just received.
        request_type: The verb the previous hop was issued with.
        body_filters: The transport keyword arguments carrying the body
            **built for this hop**, never the ones the previous hop was
            sent. A repeated body is returned through unchanged, so
            handing it a consumed one is what re-sends zero bytes; the
            caller builds it from a :data:`BodyFactory` for that reason.

    Returns:
        The ``(verb, body_filters)`` pair for the next hop.
    """
    verb = request_type.strip().upper()
    to_get = (
        (status in POST_TO_GET_REDIRECTS and verb == 'POST')
        or (status == SEE_OTHER_STATUS and verb != 'HEAD'))
    if to_get:
        return 'GET', {}
    return request_type, {
        name: value
        for name, value in body_filters.items()
        if name != 'params'
    }


async def read_response(
    resp: aiohttp.ClientResponse,
    *,
    url: Text,
    redact_params: Collection[Text],
    max_response_bytes: int,
    http_file_download_config: Optional[Dict[Text, Any]],
) -> HttpResult:
    """Read one final (non-redirect) response into an :class:`HttpResult`.

    Every read here is bounded. The declared length is refused before a
    byte is read, and a body that declares nothing is refused on the chunk
    that crosses the cap -- so ``max_response_bytes`` holds whether or not
    the endpoint is honest about its size (R14).

    Args:
        resp: The response to read.
        url: The URL this response came from, for the decode diagnostic.
        redact_params: The caller's additional sensitive query-parameter
            names, so that diagnostic carries no credential.
        max_response_bytes: Ceiling on the bytes read from the body.
        http_file_download_config: The caller's download config, or None
            when they asked for no download.

    Returns:
        The status, headers, cookies and decoded text, plus a
        ``decode_error`` when the body was not decodable text.

    Raises:
        ResponseTooLargeError: If the body declares, or streams, more than
            ``max_response_bytes``.
    """
    headers = dict(resp.headers)
    result = HttpResult(
        status_code=resp.status,
        headers=headers,
        # `.value`, not the Morsel: a live Morsel is not JSON
        # serialisable, and the envelope must be (invariant E5).
        cookies={
            name: morsel.value for name, morsel in resp.cookies.items()
        },
        text='',
    )
    guard_declared_length(resp.headers, max_response_bytes)

    if media_type_of(headers).startswith(MULTIPART_MEDIA_PREFIX):
        result['text'] = await handle_multipart_response(
            resp,
            http_file_download_config,
            max_response_bytes=max_response_bytes,
        )
        return result

    # `is not None`, not truthiness: an empty config is a caller asking
    # for a download on every default, and the defaults exist so that
    # it works (M10).
    if http_file_download_config is not None:
        filepath = (
            http_file_download_config.get('download_filepath')
            or DEFAULT_DOWNLOAD_FILEPATH)
        chunk_size = (
            http_file_download_config.get('file_download_chunk_size')
            or CHUNK_SIZE_CONSTANT)
        # `aiofiles`, and awaited per chunk: a synchronous `write()` here
        # blocked the event loop once for every chunk of the download, so
        # one large transfer stalled every other request the consuming
        # process had in flight (R20).
        #
        # Through `safe_writer` rather than `aiofiles.open` directly
        # (R22): canonicalised before the open, `O_NOFOLLOW` and mode
        # 0600 on it, refusing an existing file unless the config asked
        # to overwrite, and removing what it wrote if the read fails
        # part-way -- which the cap below makes an ordinary outcome, not
        # an exotic one.
        target = await resolve_caller_path(filepath)
        async with safe_writer(
            target,
            overwrite=http_file_download_config.get('overwrite') is True,
        ) as read_file:
            async for chunk in iter_capped(
                resp.content,
                chunk_size=chunk_size,
                max_response_bytes=max_response_bytes,
            ):
                await read_file.write(chunk)
    # resp.content is a StreamReader. After a streamed download it is
    # already exhausted, so this is `b''` rather than a second copy.
    body = b''.join([
        chunk
        async for chunk in iter_capped(
            resp.content,
            chunk_size=CHUNK_SIZE_CONSTANT,
            max_response_bytes=max_response_bytes,
        )
    ])
    try:
        result['text'] = body.decode()  # convert to str
    except UnicodeDecodeError as err:
        # Both, per R13. Raising here would lose the body entirely and
        # would be retried by the breaker as though the network had
        # failed; recording it lets `http_client` put the salvageable
        # text on the envelope *and then* report the failure.
        result['text'] = body.decode(errors='replace')
        result['decode_error'] = (
            f'Response body from '
            f'{redact_url(url, extra_params=redact_params)} is not '
            f'decodable text: {err}')
    return result


async def make_http_request(
        session,
        url: Text,
        build_body: BodyFactory,
        request_type: Text,
        *,
        redact_params: Collection[Text],
        timeout: aiohttp.ClientTimeout,
        max_response_bytes: int,
        allowed_schemes: Collection[Text],
        allow_redirects: bool,
        max_redirects: int,
        trace_collectors: Collection[Dict[Text, Any]] = (),
        **kwargs) -> HttpResult:
    """Make the API call, follow its redirects, and return what came back.

    The loop is this library's, not ``aiohttp``'s. Each request goes out
    with ``allow_redirects=False`` and each ``Location`` is resolved and
    scheme-checked by :func:`redirect_target` before the next one is
    issued, because a guardrail that only sees the caller's own URL guards
    nothing an attacker chose (FI-16). Crossing an origin also strips the
    credential headers and the caller's ``auth``, which is what
    ``aiohttp`` did for itself. The credential headers it can strip are
    the *per-request* ones, which is why a caller-supplied session
    carrying any of them is refused at the boundary by
    ``logic.http_client.validated_session`` rather than here: a session
    default is merged in by ``aiohttp`` itself and no hop can withhold it.

    **Every hop builds its own body.** ``aiohttp`` refused this outright
    -- ``Cannot follow redirect with a consumed request body`` -- and
    owning the loop deleted that refusal, so a 307 or 308 on an upload
    re-sent a ``FormData`` or an async generator the first hop had already
    exhausted: zero bytes, HTTP 200, ``ok=True``. Calling
    ``build_body`` per hop restores the guarantee by making the body
    replayable instead of by refusing to replay it (H9, AGW-38).

    ``timeout`` is passed on the *request* rather than only on the session,
    so a caller-supplied session's own deadline cannot silently outrank the
    one this call was configured with. It bounds the **chain**, not each
    hop: :func:`hop_deadline` spends one budget across every request the
    loop issues, because the deadline ``aiohttp`` used to enforce over its
    own loop is one of the two guarantees owning the loop inherited. The
    other is the redirect trace event, measured at the hop by
    :func:`measure_redirect` and written by :func:`record_redirect` from a
    ``finally`` -- so a chain that ended in a timeout, in
    ``max_redirects``, or in a transport failure still reports the hops it
    followed, exactly as ``aiohttp`` did, timed from the same instant
    ``aiohttp`` timed it from. A hop *refused* by :func:`redirect_target`
    is the one case that stays unrecorded, because it was never followed
    and the value the ``finally`` reads is assigned only after that check
    has passed.

    :param session - aiohttp.ClientSession session object
    :param url - url to hit the api
    :param build_body - a :data:`BodyFactory` returning the transport
    keyword arguments that carry the body. Awaited once per hop, and --
    because ``failsafe.run`` re-invokes this function -- once per retry.
    What it returns is read, not mutated: the ssl configuration is merged
    into a separate mapping, so a caller-owned dict is unchanged.
    :param request_type - type of request
    :param redact_params - the caller's additional sensitive
    query-parameter names, normalised once in ``request()`` and handed
    down through the filter methods. This module has no ``protocol_info``
    to read and must not grow one: deriving the set here would be a
    second normalisation, and the point of the shared redactor is that
    the envelope, the log and this exception message cannot disagree
    about what is a secret. Required rather than defaulted, so a filter
    method that dropped it fails loudly instead of quietly falling back
    to the built-in names and leaking the caller's.
    :param timeout - the deadline for the whole exchange, as an
    ``aiohttp.ClientTimeout``. Required for the same reason. Its ``total``
    is spent once across every hop, not handed afresh to each.
    :param max_response_bytes - ceiling on the bytes read from a response.
    :param allowed_schemes - the schemes a hop may target.
    :param allow_redirects - whether to follow at all. False returns the
    redirect response itself, exactly as the transport would.
    :param max_redirects - how many hops to follow before giving up.
    :param trace_collectors - the ``results_collector`` mapping of each
    tracer attached to the session, so a followed hop can be recorded the
    way ``aiohttp``'s own ``on_request_redirect`` recorded it. Defaults to
    none, which is what a call with tracing off wants; the transport does
    not read them and nothing else here depends on them being supplied.
    :returns HttpResult - the status, headers, cookies and decoded text,
    plus a ``decode_error`` when the body was not decodable text. The
    caller copies these into the envelope it owns; this function never sees
    an envelope.
    :raises ConfigurationError: If a redirect target's scheme is not in
    ``allowed_schemes``. Raised before the hop is issued.
    :raises TransportError: If the chain is longer than ``max_redirects``,
    carrying the last status the chain reached so the envelope reports it.
    :raises ResponseTooLargeError: If a response body exceeds
    ``max_response_bytes``.
    :raises asyncio.TimeoutError: If the chain runs out of time between
    hops, which ``logic.http_client`` reports as ``TIMEOUT``/504 exactly
    as it reports one raised by the transport itself.
    """
    http_file_download_config = kwargs.get('http_file_download_config')
    ssl_filters: Dict = await get_ssl_config(
        certificate=kwargs.get('certificate'),
        verify_ssl=kwargs.get('verify_ssl'))

    # Taken per *invocation*, not per caller: ``failsafe.run`` re-invokes
    # this function, and a retried attempt has always started the caller's
    # deadline over rather than inheriting the exhausted remains of the
    # previous one.
    deadline: Optional[float] = (
        None if timeout.total is None else time.monotonic() + timeout.total)

    body_filters: Dict = await build_body()
    hop_headers: Optional[Dict[Text, Text]] = kwargs.get('headers')
    hop_cookies = kwargs.get('cookies')
    hop_auth = kwargs.get('auth')
    verb = request_type
    target = url
    hop = 0
    redirected_at: Optional[RedirectEvent] = None

    # The trace event is written from a `finally` so that it is written on
    # *every* way out of the loop, not only the one that returns a
    # response. A chain that timed out between hops, outran
    # `max_redirects`, or hit a refused scheme had still followed the hops
    # it followed, and `aiohttp` reported `is_redirect` True on all three.
    try:
        while True:
            # Checked on every hop rather than once before the loop, and
            # that is not belt-and-braces: `after_redirect` *rewrites*
            # `verb` -- a 303 turns any verb into a GET -- so the name
            # resolved here on hop two is not always the one the caller
            # supplied, and a check that ran only on the way in would be
            # checking a value this loop then replaced. Both readings are
            # in `HTTP_VERBS`, so the repeat costs a set membership test
            # per hop and closes the case where they would not be (R21).
            request_obj = resolve_verb(
                session, verb, allowed=HTTP_VERBS, setting='request_type')
            session_obj = request_obj(
                target,
                allow_redirects=False,
                timeout=hop_deadline(
                    timeout,
                    deadline,
                    url=url,
                    redact_params=redact_params,
                    hop=hop),
                headers=hop_headers,
                cookies=hop_cookies,
                auth=hop_auth,
                **ssl_filters,
                **body_filters)
            async with session_obj as resp:
                # A blank ``Location`` is *absent*, not a hop. The header
                # is a string, so ``''`` is not None and the loop used to
                # enter on it -- and ``urljoin(target, '')`` is ``target``,
                # so the library re-requested the same url until
                # ``max_redirects``: one 302 turned into eleven requests,
                # and forty-four with retries on. ``aiohttp`` returns the
                # 302 itself, which is what stripping to None restores.
                header = (
                    resp.headers.get(LOCATION_HEADER, '')
                    if allow_redirects and resp.status in REDIRECT_STATUSES
                    else '')
                location = header.strip() or None
                if location is None:
                    return await read_response(
                        resp,
                        url=target,
                        redact_params=redact_params,
                        max_response_bytes=max_response_bytes,
                        http_file_download_config=http_file_download_config)
                if hop >= max_redirects:
                    raise TransportError(
                        f'redirect chain from '
                        f'{redact_url(url, extra_params=redact_params)} '
                        f'exceeded max_redirects={max_redirects}',
                        resp.status)
                hop += 1
                following = redirect_target(
                    target,
                    location,
                    allowed_schemes=allowed_schemes,
                    hop=hop,
                    redact_params=redact_params)
                # After the scheme check, so a hop that was *refused* is
                # not traced as one that was followed. Measured here and
                # written after the loop, because the base instant each
                # tracer measures against is overwritten by the request
                # this hop is about to issue.
                redirected_at = measure_redirect(trace_collectors)
                # Built afresh, and *then* judged: a hop that repeats the
                # body must send a body nothing has read yet. A hop that
                # turns into a GET discards what was built, which costs an
                # unstarted generator and nothing else.
                verb, body_filters = after_redirect(
                    resp.status, verb, await build_body())
                if not same_origin(target, following):
                    hop_headers = without_credentials(hop_headers)
                    hop_cookies = None
                    hop_auth = None
                target = following
    finally:
        record_redirect(redirected_at)


async def make_http_filters_with_stream_file_upload(
    session,
    url: Text,
    request_type: Text,
    circuit_breaker,
    **kwargs
) -> HttpResult:
    """Make filters for http call involving file upload in chunks.

    :param session - aiohttp.ClientSession session object
    :param url - url to hit the api
    :param request_type - type of request
    :param circuit_breaker - circuit breaker object.

    The body is a :data:`BodyFactory`, so the transport builds one per
    attempt *and* per redirect hop (R14/H9, ticket AGW-38). It used to be
    one ``file_upload(...)`` generator created outside ``failsafe.run``
    and handed to it: an async generator is one-shot, so the first send
    carried the file and every send after it carried **zero bytes** while
    the server answered 200 -- a truncated upload reported as a success.
    Measured, not inferred: the recorded second request carried an empty
    body, on a retry and again on a 307.

    ``make_http_filters_without_stream_uploads`` was converted first, and
    to a per-*attempt* closure that a redirect hop still defeated; the
    factory is the shape that covers both, so both paths use it.

    :raises FileNotFoundError: If ``local_filepath`` does not exist, or
    ``OSError`` for any other reason it cannot be read. Raised here, from
    outside ``failsafe.run``, for the reason spelled out on the sibling
    method: inside it, a caller-side path typo is wrapped in
    ``RetriesExhausted``, classified as a ``ConnectError``, and reported
    as a 502 from an endpoint that was never dialled -- with the caller's
    absolute local path in the message.
    """
    http_file_upload_config = kwargs.get('http_file_upload_config')
    local_filepath = http_file_upload_config['local_filepath']
    chunk_size = http_file_upload_config[
        'file_upload_chunk_size']
    async with aiofiles.open(local_filepath, 'rb'):
        pass

    async def build_body() -> Dict[Text, Any]:
        """Open a fresh stream over the file for one send.

        Returns:
            The transport keyword arguments carrying this send's body.
        """
        return {'data': file_upload(
            file_name=local_filepath,
            file_upload_chunk_size=chunk_size)}

    return await circuit_breaker.run(
        make_http_request,
        session,
        url,
        build_body,
        request_type,
        **kwargs)


def build_upload_form(
    local_filepath: Text,
    file_key: Text,
) -> aiohttp.FormData:
    """Build a multipart form that streams ``local_filepath`` as it sends.

    A *fresh* form every call, and that is the point rather than a detail.
    The body is an async generator over the file, which the attempt that
    sends it consumes; one body reused across attempts uploads the file
    once and then nothing at all.

    ``filename`` is derived here instead of being left to aiohttp, which
    read it off the synchronous file object's ``.name`` -- there is no such
    object any more, and a file part that lost its filename is a different
    request on the wire. ``PurePath`` is the pure-string half of
    ``pathlib``: it has no filesystem methods at all, so naming the part
    cannot touch the disk (R20).

    The part's ``Content-Type`` has to be derived here for the same reason
    and is the easier half to miss. ``AsyncIterablePayload`` supplies
    :data:`DEFAULT_UPLOAD_MEDIA_TYPE` itself when the caller passes no
    ``content_type``, and doing so short-circuits the filename-based guess
    the old file handle reached -- so an unqualified streaming body
    declares every upload as octet-stream, and a ``.pdf`` or ``.jpg`` is
    refused by any endpoint that validates the declared part type.
    ``mimetypes`` guesses from the *name*, so restoring the old value costs
    no filesystem access.

    Accepted consequence of streaming: the request carries
    ``Transfer-Encoding: chunked`` and no ``Content-Length``, where the
    synchronous handle let aiohttp size the body. The body bytes are
    unchanged. There is no way to keep both -- ``AsyncIterablePayload``
    fixes its ``_size`` at None with no constructor knob -- and a length
    derived from a ``stat()`` would be a *lying* ``Content-Length`` if the
    file changed under the upload, which is worse than chunked framing.

    Args:
        local_filepath: Path of the file to upload.
        file_key: Form field name to send the file under.

    Returns:
        A single-field :class:`aiohttp.FormData` whose value streams the
        file in ``CHUNK_SIZE_CONSTANT`` chunks, so the whole file is never
        held in memory.
    """
    name = PurePath(local_filepath).name
    form = aiohttp.FormData()
    form.add_field(
        file_key,
        file_upload(file_name=local_filepath),
        filename=name,
        content_type=(
            mimetypes.guess_type(name)[0] or DEFAULT_UPLOAD_MEDIA_TYPE),
    )
    return form


async def make_http_filters_without_stream_uploads(
    session,
    url: Text,
    request_type: Text,
    circuit_breaker,
    **kwargs
) -> HttpResult:
    """Make filters for file upload over http.

    :param session - aiohttp.ClientSession session object
    :param url - url to hit the api
    :param request_type - type of request
    :param circuit_breaker - circuit breaker object.

    The body is a :data:`BodyFactory`, so the transport builds one per
    attempt *and* per redirect hop. It used to be a synchronous file
    handle opened outside ``failsafe.run``: aiohttp read it with blocking
    calls while the upload was in flight, and -- because a handle at EOF
    still reads cleanly -- every retry after the first uploaded zero bytes
    and reported success (R14, R20). Converting it to a per-attempt
    closure fixed the retry and left the hop: an ``aiohttp.FormData``
    yields its parts once, so a 307 re-sent the boundary and no file.

    :raises FileNotFoundError: If ``local_filepath`` does not exist, or
    ``OSError`` for any other reason it cannot be read. Raised here, from
    outside ``failsafe.run``, which is where the old ``open()`` raised it.
    Inside the retried callable it would be wrapped in
    ``RetriesExhausted`` and classified as a ``ConnectError``, reporting a
    caller-side path typo as a 502 from an endpoint that was never
    dialled. It is not an ``AsyncGatewayError``, so it propagates past the
    entry point's one conversion point, which is the contract for a bug in
    the calling code. The guard reports the file's state at check time and
    nothing later: a file removed, replaced or made unreadable between it
    and an attempt's own open raises inside ``failsafe.run`` instead, and
    surfaces as ``RetriesExhausted`` -> ``ConnectError`` -> a 502
    ``CONNECT`` envelope carrying the local path -- the very shape this
    guard exists to avoid. That window is *new*, not inherited: the old
    ``open()`` held one handle across the whole of ``failsafe.run``, so a
    mid-flight deletion could not reach the upload. It is the accepted
    cost of building the body once per attempt, which is what makes a
    retry send the file rather than zero bytes.
    """
    http_file_upload_config = kwargs.get('http_file_upload_config')
    local_filepath = http_file_upload_config['local_filepath']
    file_key = http_file_upload_config['file_key']
    # Opened and closed, rather than `stat`ed: the contract above is that
    # every reason the file cannot be *read* raises here, and `stat`
    # succeeds on a directory and on a mode-000 file. Those two reached
    # `aiohttp` inside the retried callable instead and came back as a 502
    # from an endpoint that was never dialled, with the caller's absolute
    # local path in the message. An open asks the same question the upload
    # itself asks, which is what makes the errnos match.
    async with aiofiles.open(local_filepath, 'rb'):
        pass

    async def build_body() -> Dict[Text, Any]:
        """Build a fresh multipart form for one send.

        Returns:
            The transport keyword arguments carrying this send's body.
        """
        return {'data': build_upload_form(local_filepath, file_key)}

    return await circuit_breaker.run(
        make_http_request,
        session,
        url,
        build_body,
        request_type,
        **kwargs)


async def make_http_filters_without_file(
    session,
    url: Text,
    request_type: Text,
    circuit_breaker,
    **kwargs
) -> HttpResult:
    """Make filters to make http call.

    :param session - aiohttp.ClientSession session object
    :param url - url to hit the api
    :param request_type - type of request
    :param circuit_breaker - circuit breaker object.

    The chosen filter is invoked per send rather than once, for the reason
    the two upload methods are: ``form_x_www_form_urlencoded_filters``
    returns an ``aiohttp.FormData``, and a body object shared across a
    retry or a redirect hop is a body some earlier send may already have
    consumed. Re-running the filter is cheap and takes this path out of
    that class of defect entirely rather than leaving it depending on
    which body shape the caller's ``Content-Type`` happened to select.
    """
    headers = kwargs.get('headers')
    payload = kwargs.get('payload')
    # Bound, then called. The dispatch it replaces was
    # `header_filter_mapping.get(content_type)(...)`, which called whatever
    # the lookup returned -- including None, for every `Content-Type` that
    # carried a `charset` parameter (H14).
    request_filter = filter_for_media_type(media_type_of(headers), payload)

    async def build_body() -> Dict[Text, Any]:
        """Encode the caller's payload afresh for one send.

        Returns:
            The transport keyword arguments carrying this send's body.
        """
        return await request_filter(payload, request_type=request_type)

    return await circuit_breaker.run(
        make_http_request,
        session,
        url,
        build_body,
        request_type,
        **kwargs)


filter_methods = {
    'http_with_stream_file_upload': make_http_filters_with_stream_file_upload,
    'http_without_stream_uploads': make_http_filters_without_stream_uploads,
    'http_filters_without_file': make_http_filters_without_file
}


async def handle_http_request(
    session: object,
    url: Text,
    request_type: Text,
    circuit_breaker: object,
    **kwargs
) -> HttpResult:
    """Identify filter metods to be called before http request.

    :param session - aiohttp.ClientSession session object
    :param url - url to hit the api
    :param request_type - type of request
    :param circuit_breaker - circuit breaker object.
    :param kwargs - per-call configuration forwarded verbatim through the
    chosen filter method to ``make_http_request``. It must include
    ``redact_params``, which ``make_http_request`` requires; a caller that
    omits it gets a ``TypeError`` rather than a URL redacted with the
    built-in names only.

    A file upload combined with a GET is refused, but not here: the pair is
    rejected by ``logic.http_client.validated_upload_config`` when the
    request object is constructed, which is before dispatch and outside the
    entry point's ``AsyncGatewayError`` conversion. Raising it at this depth
    turned a configuration error into an ``ok=False`` envelope. This
    function acts on what it is handed and does not check it again (R11).
    """
    http_file_upload_config = kwargs.get('http_file_upload_config')
    if http_file_upload_config:
        if http_file_upload_config.get('file_upload_chunk_size'):
            filter_method = filter_methods.get('http_with_stream_file_upload')
        else:
            filter_method = filter_methods.get('http_without_stream_uploads')
    else:
        filter_method = filter_methods.get('http_filters_without_file')

    return await filter_method(
        session,
        url,
        request_type,
        circuit_breaker,
        **kwargs
    )

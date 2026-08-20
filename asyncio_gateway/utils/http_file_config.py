"""Http file config utils, and the read-side cap both HTTP paths share.

The two cap primitives -- :func:`guard_declared_length` and
:func:`iter_capped` -- live here rather than beside their busiest caller in
``helpers/internal/request_helper.py`` because both HTTP read paths need
them and ``helpers/`` may import ``utils/`` while the reverse direction
would be a layering inversion. One implementation means the transport
boundary and this module cannot disagree about what "too large" is, or
report it differently when they refuse (R14).

:func:`resolve_verb` is here for that same layering reason, and it is the
more load-bearing case. R21's rule is that *no* caller-named verb reaches
``getattr`` without an allowlist consulted in the same function, and the
five sites that did span both layers: two in ``helpers/`` (the transport
loop and the protocol classes' shared base) and one here. A second
implementation for the ``utils/`` side is how one of them comes to admit
what the other refuses, which is the whole defect (M25) reopened under a
different name.
"""

import asyncio
import inspect
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Collection,
    Mapping,
)
from typing import Any, Final, Optional, TypedDict

import aioboto3

import aiohttp

from botocore.exceptions import NoCredentialsError, PartialCredentialsError

from .constants import (
    CHUNK_SIZE_CONSTANT,
    HTTP_ERROR_STATUS,
    HTTP_TIMEOUT,
    MAX_RESPONSE_BYTES,
)
from .exceptions import (
    ConfigurationError,
    HttpStatusError,
    ResponseTooDeepError,
    ResponseTooLargeError,
    SerializationError,
    TransportError,
    UnsupportedVerbError,
)
from .paths import (
    _await_shielded_operation,
    resolve_caller_path,
    safe_unlink,
    safe_writer,
    stream_to_path,
)

#: The HTTP methods this library will dispatch on, and the whole of what
#: ``request_type`` may name. Every one is a method ``aiohttp.ClientSession``
#: exposes as a request shortcut, so the allowlist and the transport agree
#: by construction rather than by two lists being kept in step.
#:
#: What it excludes is the point. ``ClientSession`` carries ``close``,
#: ``ws_connect``, ``detach`` and every other attribute of a live session
#: object, and an unbounded ``getattr`` reached all of them: the documented
#: failure is ``request_type='close'``, which resolved to
#: ``ClientSession.close(url, **filters)`` and produced a ``TypeError`` the
#: envelope reported as a fabricated status rather than as the caller's
#: configuration error it is (M25).
#:
#: ``head`` is admitted and the README's table does not yet list it. That
#: is a documentation gap for R29 to close from this set -- the allowlist
#: is the source (R21-AC3) -- and not a licence to narrow the set: ``HEAD``
#: is an ordinary HTTP method that works today, and refusing it here would
#: turn a documentation fix into a breaking change for every caller
#: probing a resource without fetching it.
HTTP_VERBS: Final[frozenset[str]] = frozenset(
    {'delete', 'get', 'head', 'options', 'patch', 'post', 'put'})


def validated_verb(
    verb: Any,
    *,
    allowed: Collection[str],
    setting: str,
) -> str:
    """Return the normalised verb, once the allowlist admits the name.

    The allowlist half of :func:`resolve_verb`, split out so a protocol
    that connects before it dispatches can refuse an inadmissible name
    *before* opening the session, and still refuse it against the same
    set and with the same message. ``resolve_verb`` calls this rather
    than repeating the check, so there is one allowlist reading and not
    two that can drift.

    The split exists because deferring the whole check to the attribute
    lookup meant a caller's configuration mistake was reported as
    whatever the *connect* did first: an unreachable host turned an
    unknown FTP ``command`` into ``CONNECT``/502, and an SFTP ``mode``
    typo into ``HOST_KEY``/495 -- transport verdicts, on calls that could
    never have run, telling the caller to retry their own typo.

    Args:
        verb: The verb exactly as the caller supplied it, of whatever
            type they actually passed. None and non-strings arrive here
            in practice and are refused rather than crashing on
            ``.lower()``.
        allowed: The verbs this protocol admits, already lower-cased.
        setting: The ``protocol_info`` key the verb came from, named in
            the failure so the caller is told which of their own keys to
            fix.

    Returns:
        The verb, stripped and lower-cased -- the spelling the transport
        client is addressed by.

    Raises:
        UnsupportedVerbError: If ``verb`` is not a non-empty string, or
            names nothing in ``allowed``. The message names the verb and
            lists every allowed value.
    """
    name = verb.strip().lower() if isinstance(verb, str) else None
    if not name or name not in allowed:
        raise UnsupportedVerbError(
            f'protocol_info[{setting!r}] must name one of '
            f'{sorted(allowed)}, got {verb!r}')
    return name


def resolve_verb(
    client: Any,
    verb: Any,
    *,
    allowed: Collection[str],
    setting: str,
) -> Any:
    """Return the operation ``verb`` names, once the allowlist admits it.

    The one answer to "which attribute of a client object may a caller
    reach by name", called from every site that used to reach one with a
    bare ``getattr`` (M25). Those sites named an attribute of a live
    ``aiohttp``, ``aioftp`` or ``asyncssh`` object from a caller-supplied
    string with nothing consulted in between, so the surface a caller
    could address was the client class's whole public API and a typo
    failed *open* -- into whatever else that name happened to mean.

    Fail closed is the property, and it is why the allowlist is checked
    before the attribute is read rather than after: a name the allowlist
    does not carry is refused without ``client`` being touched at all, so
    there is no ordering in which a refused name still resolves to
    something.

    Normalisation matches the rest of the library: surrounding whitespace
    is stripped and the name is lower-cased, the same reading
    :func:`~asyncio_gateway.helpers.internal.filters_helper.is_get` and
    :func:`~asyncio_gateway.asyncio_gateway.resolve_protocol` take, so
    ``' GET '`` and ``'get'`` are one verb here exactly as they are one
    verb there.

    Args:
        client: The transport client the operation is read off -- an
            ``aiohttp.ClientSession``, an ``aioftp.Client`` or an
            ``asyncssh.SFTPClient``.
        verb: The verb exactly as the caller supplied it, of whatever
            type they actually passed. None and non-strings arrive here
            in practice and are refused rather than crashing on
            ``.lower()``.
        allowed: The verbs this protocol admits, already lower-cased.
        setting: The ``protocol_info`` key the verb came from, named in
            the failure so the caller is told which of their own keys to
            fix rather than being told a verb is wrong somewhere.

    Returns:
        The bound operation, ready to call.

    Raises:
        UnsupportedVerbError: If ``verb`` is not a non-empty string, or
            names nothing in ``allowed``. The message names the verb and
            lists every allowed value, because a rejection that does not
            say what *was* acceptable leaves the caller guessing at the
            spelling. It is a ``ConfigurationError`` subclass, so it
            carries ``CONFIG``/400 and is reported as the caller's own
            error and not as a failure of the remote side.
    """
    name = validated_verb(verb, allowed=allowed, setting=setting)

    operation = getattr(client, name, None)
    if not callable(operation):
        # Reachable only if an allowlist names something the installed
        # transport library does not offer as an operation -- a version
        # skew, not a caller error. Refused rather than returned, because
        # the alternative is an `AttributeError` or a "not callable" from
        # somewhere further in, raised against a caller who supplied a
        # verb this library told them was allowed.
        raise UnsupportedVerbError(
            f'protocol_info[{setting!r}]={verb!r} is allowed but '
            f'{type(client).__name__} offers no callable {name!r}; the '
            f'installed transport library does not support it')
    return operation


#: The response header that declares a body's length before it is read.
#: Matched case-insensitively, because a plain ``dict`` of headers does not
#: do that for itself and both a ``CIMultiDict`` and such a dict reach the
#: guard.
CONTENT_LENGTH_HEADER = 'content-length'


def guard_declared_length(
    headers: Mapping[str, str],
    max_response_bytes: int,
) -> None:
    """Refuse a response whose declared length already exceeds the cap.

    Checked before a single body byte is read, so an endpoint announcing a
    gigabyte costs the caller one header parse rather than a gigabyte of
    allocation. A response that declares nothing -- a chunked one -- is not
    refused here; :func:`iter_capped` catches it mid-stream instead.

    A ``Content-Length`` that is not an integer is ignored rather than
    rejected: it is the remote side's framing error, the transport will
    fail on it in its own way, and turning it into a size failure here
    would report the wrong thing.

    A response that declares the header **more than once** is judged on
    the largest value it declared, and an unparseable one no longer stops
    the others being read. Both used to be decided by whichever line came
    first, so a conflicting pair -- itself a framing error, and the shape
    a smuggling attempt takes -- could put an over-sized declaration
    behind a small one and pass a guard whose whole job is to refuse it
    before the read.

    Args:
        headers: The response headers, as sent. A multi-dict, so a
            repeated header is iterated once per line rather than
            collapsed.
        max_response_bytes: The ceiling in bytes.

    Returns:
        None.

    Raises:
        ResponseTooLargeError: If the largest declared length exceeds the
            cap.
    """
    longest: Optional[int] = None
    for name, value in headers.items():
        if name.lower() != CONTENT_LENGTH_HEADER:
            continue
        try:
            declared = int(value)
        except ValueError:
            continue
        longest = declared if longest is None else max(longest, declared)
    if longest is not None and longest > max_response_bytes:
        raise ResponseTooLargeError(
            f'response declares Content-Length {longest}, which is '
            f'over max_response_bytes={max_response_bytes}; the body '
            f'was not read')


def response_too_large(
    read_bytes: int,
    max_response_bytes: int,
) -> ResponseTooLargeError:
    """Build the error a mid-stream read raises when it crosses the cap.

    A function rather than three copies of an f-string: the in-memory read,
    the streamed download and the multipart reader all abandon a body the
    same way, and a caller must not have to tell which of them refused from
    the wording.

    Args:
        read_bytes: How much had been read when the cap was crossed.
        max_response_bytes: The ceiling that was crossed.

    Returns:
        The error to raise; it is not raised here, so the traceback points
        at the read that abandoned rather than at this line.
    """
    return ResponseTooLargeError(
        f'response body exceeds max_response_bytes={max_response_bytes}; '
        f'the read was abandoned after {read_bytes} bytes')


def response_too_deep(
    depth: int,
    max_multipart_depth: int,
) -> ResponseTooDeepError:
    """Build the error a multipart walk raises when it nests too far.

    Beside :func:`response_too_large` for the reason that one is a
    function: a refusal this library issues is worded in one place, so a
    caller reads the same shape of sentence whichever bound they crossed.

    Args:
        depth: The nesting level the walk had reached, counting the
            outermost reader as 1.
        max_multipart_depth: The ceiling that was crossed.

    Returns:
        The error to raise; it is not raised here, so the traceback points
        at the walk that abandoned rather than at this line.
    """
    return ResponseTooDeepError(
        f'multipart response nests deeper than '
        f'max_multipart_depth={max_multipart_depth}; the read was '
        f'abandoned at nesting level {depth}')


async def iter_capped(
    content: aiohttp.StreamReader,
    *,
    chunk_size: int,
    max_response_bytes: int,
) -> AsyncIterator[bytes]:
    """Yield a response body in chunks, refusing to exceed the cap.

    The refusal happens on the chunk that crosses the cap, so at most
    ``max_response_bytes + chunk_size`` bytes are ever held -- which is what
    makes this a cap rather than a report. ``resp.content.read()`` has no
    such bound: it allocates whatever the endpoint chooses to send.

    Args:
        content: The response's stream reader.
        chunk_size: How many bytes to request per read.
        max_response_bytes: The ceiling in bytes.

    Yields:
        Each chunk read, in order, while the running total is within the
        cap.

    Raises:
        ResponseTooLargeError: On the chunk that takes the running total
            past the cap. The remainder of the body is never read.
    """
    total = 0
    async for chunk in content.iter_chunked(chunk_size):
        total += len(chunk)
        if total > max_response_bytes:
            raise response_too_large(total, max_response_bytes)
        yield chunk


async def _close_s3_body(
    body: Any,
) -> None:
    """Close an S3 streaming body, supporting sync and async doubles.

    Args:
        body: SDK streaming body carrying a ``close`` method.

    Returns:
        None after cleanup completes.
    """
    try:
        close = getattr(body, 'close', None)
    except Exception as exc:
        raise SerializationError(
            'S3 returned a malformed streaming body') from exc
    if not callable(close):
        raise SerializationError(
            'S3 returned a streaming body without a close operation')
    try:
        result = close()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise TransportError(
            'S3 response body cleanup failed') from exc
    if not inspect.isawaitable(result):
        return
    closing = asyncio.ensure_future(result)
    try:
        await _await_shielded_operation(closing)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise TransportError(
            'S3 response body cleanup failed') from exc


class _S3BodyCloser:
    """Idempotently own one provider body until cleanup has completed."""

    def __init__(self, body: Any) -> None:
        """Store the body whose close operation this instance owns.

        Args:
            body: Provider streaming body to close exactly once.
        """
        self.body = body
        self.closed = False

    async def close(self) -> None:
        """Close the body exactly once, including on failure/cancellation."""
        if self.closed:
            return
        self.closed = True
        await _close_s3_body(self.body)


def _positive_transfer_value(value: object, *, setting: str) -> int:
    """Validate one positive byte/chunk option.

    Args:
        value: Proposed value.
        setting: Public setting name for the refusal.

    Returns:
        The positive integer.

    Raises:
        ConfigurationError: If the value is not a positive non-bool int.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(
            f'{setting} must be a positive int, got {value!r}')
    return value


class S3DownloadResult(TypedDict):
    """One streamed S3 download result for the first-class strategy.

    Attributes:
        response: The SDK response mapping, retained for safe metadata
            normalization by the strategy.
        bytes_written: Observed body bytes committed locally.
    """

    response: Mapping[str, Any]
    bytes_written: int


def _s3_response_body(
    response: Any,
) -> tuple[Mapping[str, Any], Any]:
    """Extract one body while retaining the validated response mapping.

    Args:
        response: SDK value returned by ``get_object``.

    Returns:
        The response mapping and its streaming body.

    Raises:
        SerializationError: If the mapping has no safely retrievable body.
    """
    if not isinstance(response, Mapping):
        raise SerializationError(
            'S3 get_object returned a malformed response mapping')
    try:
        body = response['Body']
    except Exception as exc:
        raise SerializationError(
            'S3 get_object returned a malformed response mapping') from exc
    return response, body


def _s3_response_parts(
    response: Mapping[str, Any],
    body: Any,
) -> Optional[int]:
    """Validate body operations and return an optional declared length.

    Args:
        response: Validated SDK response mapping.
        body: Its already-extracted body, which the caller now owns and closes.

    Returns:
        The non-negative declared length, or None when absent.

    Raises:
        SerializationError: If body operations or length are malformed.
    """
    try:
        advertised = response.get('ContentLength')
    except Exception as exc:
        raise SerializationError(
            'S3 get_object returned a malformed response mapping') from exc
    try:
        close = getattr(body, 'close', None)
        iterator = getattr(body, 'iter_chunks', None)
    except Exception as exc:
        raise SerializationError(
            'S3 returned a malformed streaming body') from exc
    if not callable(close):
        raise SerializationError(
            'S3 returned a streaming body without a close operation')
    if not callable(iterator):
        raise SerializationError(
            'S3 returned a streaming body without an async chunk iterator')
    if advertised is not None and (
        isinstance(advertised, bool)
        or not isinstance(advertised, int)
        or advertised < 0
    ):
        raise SerializationError(
            'S3 get_object returned an invalid ContentLength')
    return advertised


async def _s3_body_chunks(
    body: Any,
    closer: _S3BodyCloser,
    *,
    chunk_size: int,
) -> AsyncIterator[Any]:
    """Yield SDK chunks and close their body before finalization returns.

    Args:
        body: Validated S3 streaming body.
        closer: Idempotent owner used by this iterable and the outer fallback.
        chunk_size: Positive read size.

    Yields:
        Each provider chunk. The path primitive validates bytes-like values.

    Raises:
        SerializationError: If the iterator itself is malformed.
        TransportError: If a provider iterator fails while streaming.
        asyncio.CancelledError: Unchanged.
    """
    failure: Optional[BaseException] = None
    try:
        try:
            chunks = body.iter_chunks(chunk_size=chunk_size)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise SerializationError(
                'S3 response body could not create its chunk iterator') \
                from exc
        if not isinstance(chunks, AsyncIterable):
            raise SerializationError(
                'S3 response body returned a non-async chunk iterator')
        try:
            async for chunk in chunks:
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TransportError(
                'S3 response body streaming failed') from exc
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            await closer.close()
        except BaseException:
            if failure is None:
                raise


async def stream_s3_download(
    client: Any,
    *,
    bucket_name: str,
    s3_filepath: str,
    local_filepath: str,
    overwrite: bool,
    max_response_bytes: Optional[int],
    chunk_size: int = CHUNK_SIZE_CONSTANT,
) -> S3DownloadResult:
    """Get and stream one S3 object through the shared path primitive.

    This seam owns both SDK actions that must never drift apart: obtaining the
    streaming body and closing it on every success/failure/cancellation path.
    Filesystem policy and atomicity remain in :func:`stream_to_path`.

    Args:
        client: Open aioboto3 S3 client.
        bucket_name: Bucket holding the object.
        s3_filepath: Object key.
        local_filepath: Final caller-named destination.
        overwrite: False for strategy exclusive creation, True for the
            compatibility helper's atomic replacement.
        max_response_bytes: Positive observed-byte cap, or None only for the
            legacy helper's uncapped compatibility mode.
        chunk_size: Positive bytes requested from the body per iteration.

    Returns:
        The SDK response and observed committed byte count.

    Raises:
        BaseException: SDK, stream, path, cap, or cancellation failures after
            body/temp cleanup.
    """
    read_size = _positive_transfer_value(chunk_size, setting='chunk_size')
    if not isinstance(overwrite, bool):
        raise ConfigurationError(
            f'overwrite must be a bool, got {type(overwrite).__name__}')
    limit = (
        None
        if max_response_bytes is None
        else _positive_transfer_value(
            max_response_bytes, setting='max_response_bytes')
    )
    target = await resolve_caller_path(local_filepath)
    raw_response = await client.get_object(
        Bucket=bucket_name,
        Key=s3_filepath,
    )
    response, body = _s3_response_body(raw_response)
    closer = _S3BodyCloser(body)
    failed: Optional[BaseException] = None
    try:
        advertised = _s3_response_parts(response, body)
        written = await stream_to_path(
            target,
            _s3_body_chunks(body, closer, chunk_size=read_size),
            overwrite=overwrite,
            max_bytes=limit,
            advertised_bytes=advertised,
        )
        return S3DownloadResult(
            response=response,
            bytes_written=written,
        )
    except BaseException as exc:
        failed = exc
        raise
    finally:
        try:
            await closer.close()
        except BaseException:
            if failed is not None:
                raise failed
            raise


def _sanitize_credential_error(error: BaseException) -> None:
    """Remove provider-controlled credential details from a caught error."""
    error.args = ()
    setattr(error, 'kwargs', {})
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None


async def download_file_from_s3(
    *,
    bucket_name: str,
    s3_filepath: str,
    local_filepath: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: Optional[str] = None,
    overwrite: bool = True,
    max_response_bytes: Optional[int] = None,
    **kwargs: Any,
) -> None:
    """Download an object from AWS S3 to a local path.

    This function could not previously have succeeded, in either of the
    two copies it existed in (H15). It called ``aioboto3.client(...)``,
    a module-level factory **removed in aioboto3 9.0**, and it passed
    the destination as ``file_save_path=`` to ``download_file``, whose
    keyword is ``Filename=``. Both are corrected here, against the
    aioboto3 15.x API this release ships with, and this is now the only
    copy: ``helpers/common/file_helper.py`` held a second one whose
    parameters were in a *different order*, so a caller who imported the
    wrong module and called positionally wrote their bucket name to a
    local path (MG5).

    Every parameter is **keyword-only**. That is the structural half of
    the same fix: with the duplicate gone there is no longer a second
    order to get wrong, and with ``*`` there is no positional order to
    get wrong either -- a positional call is now a ``TypeError`` at the
    call site rather than a silently misdirected download.

    Args:
        bucket_name: The S3 bucket holding the object.
        s3_filepath: The object's key within the bucket.
        local_filepath: Where to write the downloaded object.
        access_key: AWS access key id, or None to let botocore's
            credential chain resolve one (environment, shared config,
            instance metadata).
        secret_key: AWS secret access key, paired with ``access_key``.
        region: AWS region name, or None to take it from the same chain.
        overwrite: Whether to atomically replace an existing target. True
            preserves the historical helper behavior.
        max_response_bytes: Optional positive cap. None preserves the
            historical uncapped helper behavior.
        **kwargs: Ignored. Accepted because the README documents this
            function as a ``pre_processor_config`` callable, which is
            invoked with the caller's whole parameter mapping.

    Returns:
        None. The object's bytes are at ``local_filepath``.

    Raises:
        ConfigurationError: If no usable credentials can be resolved.
            The credential chain's own error is the caller's
            misconfiguration rather than a transport failure, so it is
            wrapped and re-raised under this library's vocabulary
            instead of surfacing as a bare botocore type.
    """
    session = aioboto3.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )
    try:
        async with session.client('s3') as s3_client:
            await stream_s3_download(
                s3_client,
                bucket_name=bucket_name,
                s3_filepath=s3_filepath,
                local_filepath=local_filepath,
                overwrite=overwrite,
                max_response_bytes=max_response_bytes,
            )
    except (NoCredentialsError, PartialCredentialsError) as exc:
        _sanitize_credential_error(exc)
        raise ConfigurationError(
            'S3 credentials could not be resolved') from None


async def download_file_from_url(
        file_download_path: str,
        local_filepath: str,
        request_type: str = 'get',
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        chunk_size: int = CHUNK_SIZE_CONSTANT,
        overwrite: bool = False, **kwargs: Any) -> None:
    """Download File from url.

    The session carries an explicit ``timeout``: without one ``aiohttp``
    applies its own five-minute default to the connect and nothing at all
    to the transfer, so an endpoint that dribbled bytes pinned the calling
    task indefinitely (R14).

    The body is streamed to disk through :func:`iter_capped` instead of
    being read whole with ``response.content.read()``. Two things change.
    The download is bounded -- a hostile endpoint can no longer decide how
    much memory this allocates -- and the file no longer has to fit in
    memory alongside itself on the way there.

    The status is judged *before* the body is touched. It was judged after,
    which meant the whole body of a 403 was read and then discarded.

    The write goes through :func:`~asyncio_gateway.utils.paths.safe_writer`
    (R22). Three things change. The path is canonicalised before it is
    opened, so ``..`` and a symlinked intermediate component resolve to
    where they actually point (M17). The open carries ``O_NOFOLLOW`` and
    mode 0600, so the symlink a hostile local process pre-created at the
    README's fixed ``/tmp/test.pdf`` is refused rather than written
    through, and the result is not world-readable (M18). And a failure
    part-way through the body removes what was written instead of
    orphaning it (M19).

    **Every** non-success status is refused, not only 403 (H16). The check
    used to be ``status == 403`` alone, so a 404 body, a 500 stack trace or
    an HTML login-redirect page was written to ``local_filepath`` as though
    it were the requested file -- and any later upload step then shipped
    that error page to the destination as the caller's data. Nothing is
    written unless the status says the body is the file that was asked
    for; on refusal no file is created at all, so a failure cannot be
    mistaken for a zero-length success by whatever runs next.

    A success carrying a **zero-length body is legitimate** and is written
    as an empty file. An empty object is a real thing to download, and
    refusing it here would mean this library, not the endpoint, deciding
    that the caller's file is invalid.

    Redirects are followed by ``aiohttp`` within its own cap, so
    ``response.status`` is the status of the *final* hop and that is the
    one that decides.

    :param file_download_path: complete url from where to download
    :param local_filepath: machine file path to download and store it
    :param request_type: HTTP method
    :param headers: API headers if any
    :param timeout: total seconds allowed for the whole exchange,
        defaulting to ``HTTP_TIMEOUT``
    :param max_response_bytes: ceiling on the bytes read from the response
    :param chunk_size: bytes requested per read
    :param overwrite: whether an existing ``local_filepath`` may be
        replaced. **False by default** (R22-AC3): a download that
        silently replaces a file is how a caller loses one, and a
        symlink planted at the destination is how someone else's file
        gets written. Pass True to re-download to a stable path.
    :param kwargs
    :raises HttpStatusError: if the final status is not a success, carrying
        that real status rather than a synthesised one.
    :raises ResponseTooLargeError: if the response declares, or streams,
        more than ``max_response_bytes``.
    :raises PathContainmentError: if ``local_filepath`` is a symbolic
        link, or names a directory rather than a file.
    :raises ConfigurationError: if ``local_filepath`` already exists and
        ``overwrite`` is False.
    :raises UnsupportedVerbError: if ``request_type`` names no verb in
        :data:`HTTP_VERBS`. Fail closed (R21): the unbounded ``getattr``
        this replaces reached every attribute of the open session, so
        ``request_type='close'`` resolved to ``ClientSession.close`` and
        died inside the transport instead of being refused by name.
    """
    if headers is None:
        headers = {}
    target = await resolve_caller_path(local_filepath)
    client_timeout = aiohttp.ClientTimeout(
        total=HTTP_TIMEOUT if timeout is None else timeout)
    async with aiohttp.ClientSession(
            headers=headers, timeout=client_timeout) as session:
        request_obj = resolve_verb(
            session, request_type, allowed=HTTP_VERBS,
            setting='request_type')
        session_obj = request_obj(file_download_path)
        async with session_obj as response:
            if response.status >= HTTP_ERROR_STATUS:
                raise HttpStatusError(
                    f'the endpoint answered {response.status}; no file was '
                    f'written', response.status)
            guard_declared_length(response.headers, max_response_bytes)
            async with safe_writer(
                    target, overwrite=overwrite) as file_obj:
                async for chunk in iter_capped(
                    response.content,
                    chunk_size=chunk_size,
                    max_response_bytes=max_response_bytes,
                ):
                    await file_obj.write(chunk)


async def delete_local_file_path(
        local_filepath: str, **kwargs: Any) -> None:
    """Delete a downloaded file, succeeding when it is already gone.

    The unlink goes through ``aiofiles.os`` rather than ``os.remove``:
    a synchronous unlink is a blocking filesystem call, and on an async
    path it stalls the whole event loop for the duration of the syscall
    (R20/C3). ``aiofiles.os.remove`` runs it in a thread, so the loop
    stays free while the directory entry is removed.

    **Idempotent** (M19/R22-AC4). This is documented as the
    post-processor *cleanup* step, and a cleanup that raises on the
    second call is one no caller can run from a ``finally``: the
    download path's own ``try/finally`` removes a partial file, so a
    caller's cleanup routinely arrives at a path something has already
    removed. Absence is the outcome asked for, however it was reached.
    It used to raise ``FileNotFoundError``.

    :param local_filepath: file to be deleted
    :param kwargs
    """
    await safe_unlink(local_filepath)

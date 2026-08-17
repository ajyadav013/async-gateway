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

from collections.abc import AsyncIterator, Collection, Mapping
from typing import Any, Final, Optional, Text

import aioboto3
import aiofiles.os
import aiohttp

from .constants import (
    CHUNK_SIZE_CONSTANT,
    HTTP_TIMEOUT,
    MAX_RESPONSE_BYTES,
    STATUS_CODE_403,
)
from .exceptions import (
    HttpStatusError,
    ResponseTooLargeError,
    UnsupportedVerbError,
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
HTTP_VERBS: Final[frozenset[Text]] = frozenset(
    {'delete', 'get', 'head', 'options', 'patch', 'post', 'put'})


def resolve_verb(
    client: Any,
    verb: Any,
    *,
    allowed: Collection[Text],
    setting: Text,
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
    :func:`~async_gateway.helpers.internal.filters_helper.is_get` and
    :func:`~async_gateway.async_gateway.resolve_protocol` take, so
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
    name = verb.strip().lower() if isinstance(verb, str) else None
    if not name or name not in allowed:
        raise UnsupportedVerbError(
            f'protocol_info[{setting!r}] must name one of '
            f'{sorted(allowed)}, got {verb!r}')

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
    headers: Mapping[Text, Text],
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


async def download_file_from_s3(bucket_name: Text,
                                s3_filepath: Text,
                                local_filepath: Text,
                                access_key: Text = None,
                                secret_key: Text = None,
                                region: Text = None, **kwargs):
    """Download file from AWS S3.

    :param access_key: S3 access key
    :param region: S3 region
    :param secret_key: S3 secret key
    :param bucket_name: bucket name of S3.
    :param s3_filepath: S3 filepath to be downloaded.
    :param local_filepath: local filepath where the file will be saved.
    :param kwargs
    """
    client = aioboto3.client(
        's3',
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region)
    async with client as s3_client:
        await s3_client.download_file(Bucket=bucket_name,
                                      Key=s3_filepath,
                                      file_save_path=local_filepath)


async def download_file_from_url(
        file_download_path: Text,
        local_filepath: Text,
        request_type: Text = 'get',
        headers=None,
        timeout: Optional[float] = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        chunk_size: int = CHUNK_SIZE_CONSTANT, **kwargs):
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

    :param file_download_path: complete url from where to download
    :param local_filepath: machine file path to download and store it
    :param request_type: HTTP method
    :param headers: API headers if any
    :param timeout: total seconds allowed for the whole exchange,
        defaulting to ``HTTP_TIMEOUT``
    :param max_response_bytes: ceiling on the bytes read from the response
    :param chunk_size: bytes requested per read
    :param kwargs
    :raises HttpStatusError: if the endpoint answers 403.
    :raises ResponseTooLargeError: if the response declares, or streams,
        more than ``max_response_bytes``.
    :raises UnsupportedVerbError: if ``request_type`` names no verb in
        :data:`HTTP_VERBS`. Fail closed (R21): the unbounded ``getattr``
        this replaces reached every attribute of the open session, so
        ``request_type='close'`` resolved to ``ClientSession.close`` and
        died inside the transport instead of being refused by name.
    """
    if headers is None:
        headers = {}
    client_timeout = aiohttp.ClientTimeout(
        total=HTTP_TIMEOUT if timeout is None else timeout)
    async with aiohttp.ClientSession(
            headers=headers, timeout=client_timeout) as session:
        request_obj = resolve_verb(
            session, request_type, allowed=HTTP_VERBS,
            setting='request_type')
        session_obj = request_obj(file_download_path)
        async with session_obj as response:
            if response.status == STATUS_CODE_403:
                raise HttpStatusError(
                    'Access to the requested file is forbidden.',
                    STATUS_CODE_403)
            guard_declared_length(response.headers, max_response_bytes)
            async with aiofiles.open(local_filepath, 'wb') as file_obj:
                async for chunk in iter_capped(
                    response.content,
                    chunk_size=chunk_size,
                    max_response_bytes=max_response_bytes,
                ):
                    await file_obj.write(chunk)


async def delete_local_file_path(local_filepath: Text, **kwargs) -> None:
    """Deletes downloaded file.

    The unlink goes through ``aiofiles.os`` rather than ``os.remove``:
    a synchronous unlink is a blocking filesystem call, and on an async
    path it stalls the whole event loop for the duration of the syscall
    (R20/C3). ``aiofiles.os.remove`` runs it in a thread, so the loop
    stays free while the directory entry is removed.

    Note the import is ``import aiofiles.os``: importing ``aiofiles``
    alone does not bind the ``os`` submodule.

    :param local_filepath: file to be deleted
    :param kwargs
    :raises FileNotFoundError: if the file is already gone. Making the
        delete idempotent is R22's change, not this one.
    """
    await aiofiles.os.remove(local_filepath)

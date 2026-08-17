"""Http file config utils, and the read-side cap both HTTP paths share.

The two cap primitives -- :func:`guard_declared_length` and
:func:`iter_capped` -- live here rather than beside their busiest caller in
``helpers/internal/request_helper.py`` because both HTTP read paths need
them and ``helpers/`` may import ``utils/`` while the reverse direction
would be a layering inversion. One implementation means the transport
boundary and this module cannot disagree about what "too large" is, or
report it differently when they refuse (R14).
"""

from collections.abc import AsyncIterator, Mapping
from typing import Any, Optional, Text

import aioboto3
import aiofiles.os
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
    ResponseTooLargeError,
)

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


async def download_file_from_s3(
    *,
    bucket_name: Text,
    s3_filepath: Text,
    local_filepath: Text,
    access_key: Optional[Text] = None,
    secret_key: Optional[Text] = None,
    region: Optional[Text] = None,
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
            await s3_client.download_file(
                Bucket=bucket_name,
                Key=s3_filepath,
                Filename=local_filepath,
            )
    except (NoCredentialsError, PartialCredentialsError) as exc:
        raise ConfigurationError(
            f'S3 credentials could not be resolved for bucket '
            f'{bucket_name!r}: {exc}') from exc


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
    :param kwargs
    :raises HttpStatusError: if the final status is not a success, carrying
        that real status rather than a synthesised one.
    :raises ResponseTooLargeError: if the response declares, or streams,
        more than ``max_response_bytes``.
    """
    if headers is None:
        headers = {}
    client_timeout = aiohttp.ClientTimeout(
        total=HTTP_TIMEOUT if timeout is None else timeout)
    async with aiohttp.ClientSession(
            headers=headers, timeout=client_timeout) as session:
        request_obj = getattr(session, request_type.lower())
        session_obj = request_obj(file_download_path)
        async with session_obj as response:
            if response.status >= HTTP_ERROR_STATUS:
                raise HttpStatusError(
                    f'the endpoint answered {response.status}; no file was '
                    f'written', response.status)
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

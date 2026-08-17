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
from typing import Optional, Text

import aioboto3
import aiohttp

from .constants import (
    CHUNK_SIZE_CONSTANT,
    HTTP_TIMEOUT,
    MAX_RESPONSE_BYTES,
    STATUS_CODE_403,
)
from .exceptions import HttpStatusError, ResponseTooLargeError
from .paths import resolve_caller_path, safe_unlink, safe_writer

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
        chunk_size: int = CHUNK_SIZE_CONSTANT,
        overwrite: bool = False, **kwargs):
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

    The write goes through :func:`~async_gateway.utils.paths.safe_writer`
    (R22). Three things change. The path is canonicalised before it is
    opened, so ``..`` and a symlinked intermediate component resolve to
    where they actually point (M17). The open carries ``O_NOFOLLOW`` and
    mode 0600, so the symlink a hostile local process pre-created at the
    README's fixed ``/tmp/test.pdf`` is refused rather than written
    through, and the result is not world-readable (M18). And a failure
    part-way through the body removes what was written instead of
    orphaning it (M19).

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
    :raises HttpStatusError: if the endpoint answers 403.
    :raises ResponseTooLargeError: if the response declares, or streams,
        more than ``max_response_bytes``.
    :raises PathContainmentError: if ``local_filepath`` is a symbolic
        link, or names a directory rather than a file.
    :raises ConfigurationError: if ``local_filepath`` already exists and
        ``overwrite`` is False.
    """
    if headers is None:
        headers = {}
    target = await resolve_caller_path(local_filepath)
    client_timeout = aiohttp.ClientTimeout(
        total=HTTP_TIMEOUT if timeout is None else timeout)
    async with aiohttp.ClientSession(
            headers=headers, timeout=client_timeout) as session:
        request_obj = getattr(session, request_type.lower())
        session_obj = request_obj(file_download_path)
        async with session_obj as response:
            if response.status == STATUS_CODE_403:
                raise HttpStatusError(
                    'Access to the requested file is forbidden.',
                    STATUS_CODE_403)
            guard_declared_length(response.headers, max_response_bytes)
            async with safe_writer(
                    target, overwrite=overwrite) as file_obj:
                async for chunk in iter_capped(
                    response.content,
                    chunk_size=chunk_size,
                    max_response_bytes=max_response_bytes,
                ):
                    await file_obj.write(chunk)


async def delete_local_file_path(local_filepath: Text, **kwargs) -> None:
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

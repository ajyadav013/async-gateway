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
"""

from collections.abc import Collection
from typing import Any, Dict, List, Optional, Text, TypedDict

import aiofiles
import aiohttp
from async_gateway.helpers.common.file_helper import download_file_from_s3
from async_gateway.helpers.internal import (
    MULTIPART_MEDIA_PREFIX,
    filter_for_media_type,
    media_type_of,
)
from async_gateway.helpers.internal.filters_helper import get_ssl_config
from async_gateway.utils.constants import CHUNK_SIZE_CONSTANT
from async_gateway.utils.redaction import redact_url

#: Where a download lands when the caller names no path. The README
#: documents ``download_filepath`` as part of an *optional* config block, so
#: the config has to be usable without it rather than reading it with a
#: bare ``.get()`` and handing None to ``open()`` (M10).
DEFAULT_DOWNLOAD_FILEPATH = 'response.txt'


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
    :param file_config: Dict contains s3 config,
    download link, local filepath etc.
    file_config[local_filepath] is mandatory
    """
    if file_config.get('s3_config'):
        await download_file_from_s3(
            file_config['local_filepath'],
            **file_config['s3_config'],
        )
    elif file_config.get('file_download_path'):
        # Add separate aio params when required
        request_type = file_config.get('request_type', 'get')
        async with aiohttp.ClientSession(
                headers=file_config.get('headers')) as session:
            request_obj = getattr(session, request_type.lower())
            session_obj = request_obj(file_config['file_download_path'])
            async with session_obj as response:
                contents = await response.content.read()
                async with aiofiles.open(
                        file_config['local_filepath'], 'wb') as file_obj:
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

    Args:
        resp: The response to read the multipart body from.
        http_file_download_config: The caller's download config, or None.
            ``download_filepath`` defaults to
            :data:`DEFAULT_DOWNLOAD_FILEPATH`.

    Returns:
        Everything written, decoded with errors replaced. Multipart carries
        arbitrary binary, so a ``str`` rendering of it is best-effort by
        construction; the file on disk is the faithful copy.
    """
    config = http_file_download_config or {}
    response_file_name = (
        config.get('download_filepath') or DEFAULT_DOWNLOAD_FILEPATH)
    reader = aiohttp.MultipartReader.from_response(resp)
    parts: List[bytes] = []
    with open(response_file_name, 'wb') as response_file:
        while not reader.at_eof():
            part = await reader.next()
            if part is None:
                break
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                parts.append(chunk)
                response_file.write(chunk)
    return b''.join(parts).decode(errors='replace')


async def make_http_request(
        session,
        url: Text,
        filters: Dict,
        request_type: Text,
        *,
        redact_params: Collection[Text],
        **kwargs) -> HttpResult:
    """Make the API call and return what came back, as data.

    :param session - aiohttp.ClientSession session object
    :param url - url to hit the api
    :param filters - filters to include in the api
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
    :returns HttpResult - the status, headers, cookies and decoded text,
    plus a ``decode_error`` when the body was not decodable text. The
    caller copies these into the envelope it owns; this function never sees
    an envelope.
    """
    http_file_download_config = kwargs.get('http_file_download_config')
    ssl_filters: Dict = await get_ssl_config(
        certificate=kwargs.get('certificate'),
        verify_ssl=kwargs.get('verify_ssl'))
    filters.update(ssl_filters)

    request_obj = getattr(session, request_type.lower())
    session_obj = request_obj(url, **filters)

    async with session_obj as resp:
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

        if media_type_of(headers).startswith(MULTIPART_MEDIA_PREFIX):
            result['text'] = await handle_multipart_response(
                resp,
                http_file_download_config
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
            with open(filepath, 'wb') as read_file:
                async for chunk in resp.content.iter_chunked(chunk_size):
                    read_file.write(chunk)
        # resp.content is a StreamReader. After a streamed download it is
        # already exhausted, so this is `b''` rather than a second copy.
        body = await resp.content.read()
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
    """
    http_file_upload_config = kwargs.get('http_file_upload_config')
    local_filepath = http_file_upload_config['local_filepath']
    chunk_size = http_file_upload_config[
        'file_upload_chunk_size']
    filters = {
        'data': file_upload(
            file_name=local_filepath,
            file_upload_chunk_size=chunk_size)
    }
    return await circuit_breaker.failsafe.run(
        make_http_request,
        session,
        url,
        filters,
        request_type,
        **kwargs)


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
    """
    http_file_upload_config = kwargs.get('http_file_upload_config')
    with open(http_file_upload_config['local_filepath'], 'rb') as read_file:
        filters = {
            'data': {
                http_file_upload_config['file_key']: read_file}
        }
        response: HttpResult = await circuit_breaker.failsafe.run(
            make_http_request,
            session,
            url,
            filters,
            request_type,
            **kwargs)
    return response


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
    """
    headers = kwargs.get('headers')
    payload = kwargs.get('payload')
    # Bound, then called. The dispatch it replaces was
    # `header_filter_mapping.get(content_type)(...)`, which called whatever
    # the lookup returned -- including None, for every `Content-Type` that
    # carried a `charset` parameter (H14).
    request_filter = filter_for_media_type(media_type_of(headers), payload)
    filters = await request_filter(payload, request_type=request_type)
    return await circuit_breaker.failsafe.run(
        make_http_request,
        session,
        url,
        filters,
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

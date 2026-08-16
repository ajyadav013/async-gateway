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
from typing import Dict, Optional, Text, TypedDict

import aiofiles
import aiohttp
from async_gateway.helpers.common.file_helper import download_file_from_s3
from async_gateway.helpers.internal import header_filter_mapping
from async_gateway.helpers.internal.filters_helper import get_ssl_config
from async_gateway.utils.constants import CHUNK_SIZE_CONSTANT
from async_gateway.utils.exceptions import SerializationError
from async_gateway.utils.redaction import redact_url


class HttpResult(TypedDict):
    """One HTTP exchange, as data.

    Attributes:
        status_code: The status the remote side returned.
        headers: Response headers, exactly as received and unredacted --
            redaction happens where this is copied into the envelope.
        cookies: Response cookie values by name.
        text: The decoded body; ``''`` when there was none.
        body: The raw body bytes, or None when the body was streamed to
            disk rather than held in memory.
        redirect_chain: Every hop actually followed, in order.
    """

    status_code: int
    headers: Dict[Text, Text]
    cookies: Dict[Text, Text]
    text: Text
    body: Optional[bytes]
    redirect_chain: list[Text]


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
    resp: aiohttp.ClientResponse, http_file_download_config: dict
) -> str:
    """Iter multipart responses.

    this function handles multipart responses sent by server,
    by reading the chunks of data and terminates when eof is reached.
    The response is saved in the file path of http_file_download_config
    else in response file of the current path.
    :param resp - response object of aiohttp.
    :param http_file_download_config - file download config
    containing the file download location.
    """
    reader = aiohttp.MultipartReader.from_response(resp)
    response_file_name = http_file_download_config.get(
        'download_filepath', 'response.txt') if \
        http_file_download_config else 'response.txt'
    response_data = ''
    with open(response_file_name, 'w') as response_file:
        while True:
            if reader.at_eof():
                break
            part = await reader.next()
            data = await part.read_chunk()
            response_data = response_data + str(data)
            response_file.write(str(data))
    return response_data


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
    :returns HttpResult - the status, headers, cookies, decoded text and
    raw body. The caller copies these into the envelope it owns; this
    function never sees an envelope.
    :raises SerializationError - if the body is not decodable text. Its
    message carries the URL redacted with ``redact_params``, because the
    message reaches the caller through ``error['message']`` and the log
    through ``exc_info``.
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
            body=None,
            redirect_chain=[str(hop.url) for hop in resp.history],
        )

        content_type = str(headers.get('content-type'))

        # handling multipart response.
        if content_type.startswith('multipart'):
            result['text'] = await handle_multipart_response(
                resp,
                http_file_download_config
            )
            return result

        elif http_file_download_config:
            with open(
                http_file_download_config.get('download_filepath'), 'wb'
            )as read_file:
                async for chunk in resp.content.iter_chunked(
                    http_file_download_config.get('file_download_chunk_size')
                ):
                    read_file.write(chunk)
        # resp.content is a StreamReader
        body = await resp.content.read()
        result['body'] = body
        try:
            result['text'] = body.decode()  # convert to str
        except UnicodeDecodeError as err:
            raise SerializationError(
                f'Response body from '
                f'{redact_url(url, extra_params=redact_params)} is not '
                f'decodable text: {err}') from err

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
    content_type = headers.get('Content-Type', 'default').lower()
    filters = await header_filter_mapping.get(
        content_type)(
        payload,
        request_type=request_type
    )
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

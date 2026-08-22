"""R25: file-transfer utilities that work, exist once, and check status.

Three separable defects, and the tests below are grouped by them.

**H15/MG5 -- the duplicate.** ``download_file_from_s3`` existed in two
copies whose parameters were in a *different order*
(``utils/http_file_config.py`` took ``bucket_name`` first,
``helpers/common/file_helper.py`` took ``local_filepath`` first). A
caller who imported the wrong one and called positionally wrote their
bucket name to a local path. The duplicate module is deleted and
:func:`test_the_duplicate_file_helper_module_no_longer_imports` is what
stops it coming back; the surviving copy takes **keyword-only**
parameters, so there is no positional order left to get wrong either.

**H15 -- the call that could not work.** The survivor also called
``aioboto3.client(...)``, removed in aioboto3 9.0, and passed
``file_save_path=`` to ``download_file``, whose keyword is ``Filename=``.
The signature assertions below pin the corrected call exactly, against
the aioboto3 15.x this release ships (FI-11: written once, here).

**H16 -- the status check.** The URL download tested ``status == 403``
alone, so a 404 body, a 500 stack trace or an HTML login page was saved
as the requested file and any later upload shipped the error page. The
parametrised case below covers 200, a followed 301, 400, 403, 404, 500
and 502, and asserts the two halves that matter together: only a success
writes, and every failure leaves **no file on disk** -- an empty file
would be indistinguishable from a legitimate zero-length download.

The S3 tests drive a hand-written double rather than a mocking library,
matching release Ruling A: the double records the exact keyword
arguments, which is the assertion H15 needs, and it cannot drift from
aioboto3's real surface without the signature test noticing.
"""

import ast
import asyncio
import importlib
import logging
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Text

from botocore.exceptions import NoCredentialsError, PartialCredentialsError

import pytest

from asyncio_gateway.utils import http_file_config as file_config
from asyncio_gateway.utils.exceptions import (
    ConfigurationError,
    HttpStatusError,
    ResponseTooLargeError,
    SerializationError,
    TransportError,
    UnsupportedVerbError,
)
from asyncio_gateway.utils.http_file_config import (
    HTTP_VERBS,
    download_file_from_s3,
    download_file_from_url,
    resolve_verb,
)

from tests.cancellation import (
    assert_cancelled_error_survives_task_boundary,
)
from tests.fixtures.http_server import RecordingHTTPServer

#: The module under test, as a path, for the two structural scans below.
HTTP_FILE_CONFIG_SOURCE = (
    Path(__file__).resolve().parents[2]
    / 'asyncio_gateway' / 'utils' / 'http_file_config.py'
)


class ChunkedS3Body:
    """Small async S3 body double with an observable close."""

    def __init__(self, chunks: List[Any]) -> None:
        """Store chunks for one streamed read.

        Args:
            chunks: Pieces yielded to the downloader.
        """
        self.chunks = chunks
        self.closed = False
        self.iterations = 0
        self.chunk_sizes: List[int] = []

    async def iter_chunks(self, chunk_size: int) -> Any:
        """Yield the configured pieces.

        Args:
            chunk_size: Requested bound, accepted for API compatibility.

        Yields:
            Each configured piece.
        """
        self.chunk_sizes.append(chunk_size)
        for chunk in self.chunks:
            self.iterations += 1
            yield chunk

    def close(self) -> None:
        """Record body cleanup."""
        self.closed = True


class RaisingS3Body(ChunkedS3Body):
    """S3 body whose async iterator fails with provider-authored text."""

    async def iter_chunks(self, chunk_size: int) -> Any:
        """Raise after one chunk to exercise typed, redacted cleanup."""
        del chunk_size
        yield b'partial'
        raise RuntimeError('provider detail: secret=do-not-return')


class SyncIteratorS3Body(ChunkedS3Body):
    """Malformed S3 body returning an ordinary list of chunks."""

    def iter_chunks(self, chunk_size: int) -> List[bytes]:
        """Return a non-async iterator, which the seam must type safely."""
        del chunk_size
        return list(self.chunks)


async def test_pe50_shared_s3_stream_writes_and_closes_the_body(
    tmp_path: Path,
) -> None:
    """The strategy/helper primitive streams one body under a mandatory cap."""
    target = tmp_path / 'object.bin'
    client = RecordingS3Client(
        chunks=[b'abc', b'def'], content_length=6)

    result = await file_config.stream_s3_download(
        client,
        bucket_name='bucket',
        s3_filepath='key',
        local_filepath=str(target),
        max_response_bytes=6,
        overwrite=False,
        chunk_size=3,
    )

    assert result['bytes_written'] == 6
    assert result['response'] is client.responses[0]
    assert target.read_bytes() == b'abcdef'
    assert client.bodies[0].closed is True
    assert client.bodies[0].chunk_sizes == [3]


async def test_pe50_failed_overwrite_preserves_the_existing_target(
    tmp_path: Path,
) -> None:
    """An interrupted overwrite removes only its temporary file."""
    target = tmp_path / 'object.bin'
    target.write_bytes(b'original')
    client = RecordingS3Client(
        chunks=[b'replacement', b'overflow'], content_length=None)

    with pytest.raises(Exception):
        await file_config.stream_s3_download(
            client,
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(target),
            max_response_bytes=11,
            overwrite=True,
            chunk_size=11,
        )

    assert target.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [target]
    assert client.bodies[0].closed is True


@pytest.mark.parametrize(
    ('option', 'value'),
    [
        pytest.param('overwrite', 'yes', id='non-bool-overwrite'),
        pytest.param('max_response_bytes', True, id='bool-cap'),
        pytest.param('max_response_bytes', 0, id='zero-cap'),
        pytest.param('max_response_bytes', -1, id='negative-cap'),
    ],
)
async def test_pe50_invalid_stream_options_are_refused_before_get_object(
    tmp_path: Path,
    option: str,
    value: Any,
) -> None:
    """Local policy validation performs no SDK I/O."""
    client = RecordingS3Client()
    kwargs: Dict[str, Any] = {
        'bucket_name': 'bucket',
        's3_filepath': 'key',
        'local_filepath': str(tmp_path / 'object.bin'),
        'overwrite': False,
        'max_response_bytes': 1024,
    }
    kwargs[option] = value

    with pytest.raises(ConfigurationError):
        await file_config.stream_s3_download(client, **kwargs)

    assert client.get_calls == []


async def test_pe50_malformed_chunk_is_typed_without_returning_its_value(
    tmp_path: Path,
) -> None:
    """Provider values never escape via a raw aiofiles TypeError."""
    client = RecordingS3Client(chunks=[b'good', 'secret chunk'])
    target = tmp_path / 'object.bin'

    with pytest.raises(SerializationError) as caught:
        await file_config.stream_s3_download(
            client,
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(target),
            overwrite=False,
            max_response_bytes=1024,
        )

    assert 'secret chunk' not in str(caught.value)
    assert not target.exists()
    assert client.bodies[0].closed is True


async def test_pe50_malformed_body_iterator_is_typed_and_closed(
    tmp_path: Path,
) -> None:
    """A synchronous body iterator becomes a safe typed response failure."""
    body = SyncIteratorS3Body([b'body'])

    class Client:
        """Return the one malformed body."""

        async def get_object(self, **kwargs: Any) -> Dict[str, Any]:
            """Return a response carrying the malformed iterator."""
            del kwargs
            return {'Body': body, 'ContentLength': 4}

    with pytest.raises(SerializationError):
        await file_config.stream_s3_download(
            Client(),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'object.bin'),
            overwrite=False,
            max_response_bytes=1024,
        )

    assert body.closed is True


async def test_pe50_body_failure_is_typed_redacted_closed_and_cleaned(
    tmp_path: Path,
) -> None:
    """Iterator exceptions are chained behind a generic transport error."""
    body = RaisingS3Body([])

    class Client:
        """Return the one failing body."""

        async def get_object(self, **kwargs: Any) -> Dict[str, Any]:
            """Return a response carrying the failing iterator."""
            del kwargs
            return {'Body': body}

    target = tmp_path / 'object.bin'
    with pytest.raises(TransportError) as caught:
        await file_config.stream_s3_download(
            Client(),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(target),
            overwrite=False,
            max_response_bytes=1024,
        )

    assert 'secret' not in str(caught.value)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert body.closed is True
    assert not target.exists()


class RecordingS3Client:
    """An S3 client double that records the streamed object request.

    Attributes:
        get_calls: One entry per ``get_object`` call, holding the
            keyword arguments exactly as passed. Positional arguments are
            captured separately so a call that stopped using keywords is
            visible rather than silently reshaped into one that did.
        positional_calls: One entry per call, holding its positional
            arguments.
        raises: An exception to raise from ``get_object`` instead of
            recording, or None to record normally.
    """

    def __init__(
        self,
        raises: Optional[BaseException] = None,
        *,
        chunks: Optional[List[Any]] = None,
        content_length: Optional[int] = len(b'downloaded object'),
    ) -> None:
        """Build the double.

        Args:
            raises: Exception ``get_object`` should raise, or None.
            chunks: Body chunks, or the default object bytes.
            content_length: Advertised length, or None to omit it.
        """
        self.get_calls: List[Dict[str, Any]] = []
        self.positional_calls: List[tuple] = []
        self.raises = raises
        self.bodies: List[ChunkedS3Body] = []
        self.chunks = [b'downloaded object'] if chunks is None else chunks
        self.content_length = content_length
        self.responses: List[Dict[str, Any]] = []

    async def get_object(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Record a streamed object request, or raise the configured error.

        Args:
            *args: Positional arguments, recorded so their absence can be
                asserted.
            **kwargs: Keyword arguments, recorded for exact comparison.

        Returns:
            A fresh streaming body and its declared length.

        Raises:
            BaseException: Whatever ``raises`` was constructed with.
        """
        self.positional_calls.append(args)
        self.get_calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises
        body = ChunkedS3Body(list(self.chunks))
        self.bodies.append(body)
        response: Dict[str, Any] = {'Body': body}
        if self.content_length is not None:
            response['ContentLength'] = self.content_length
        self.responses.append(response)
        return response


class RawResponseS3Client:
    """S3 double returning one caller-supplied raw SDK response."""

    def __init__(self, response: Any) -> None:
        """Store the raw response and initialize request recording."""
        self.response = response
        self.get_calls: List[Dict[str, Any]] = []

    async def get_object(self, **kwargs: Any) -> Any:
        """Record keywords and return the raw response unchanged."""
        self.get_calls.append(dict(kwargs))
        return self.response


class ControlledAsyncCloseBody(ChunkedS3Body):
    """Body with an async close the test can pause or fail."""

    def __init__(
        self,
        chunks: List[Any],
        *,
        close_started: Optional[asyncio.Event] = None,
        close_release: Optional[asyncio.Event] = None,
        close_error: Optional[BaseException] = None,
    ) -> None:
        """Configure close coordination and an optional provider failure."""
        super().__init__(chunks)
        self.close_started = close_started
        self.close_release = close_release
        self.close_error = close_error

    async def close(self) -> None:
        """Wait when requested, then close or raise the configured failure."""
        if self.close_started is not None:
            self.close_started.set()
        if self.close_release is not None:
            await self.close_release.wait()
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class WaitingS3Body(ChunkedS3Body):
    """Body that yields partial bytes and then waits for cancellation."""

    def __init__(self, started: asyncio.Event) -> None:
        """Store the event raised after the first chunk."""
        super().__init__([])
        self.started = started

    async def iter_chunks(self, chunk_size: int) -> Any:
        """Yield one chunk and wait forever at the stream boundary."""
        self.chunk_sizes.append(chunk_size)
        self.iterations += 1
        yield b'partial'
        self.started.set()
        await asyncio.Event().wait()


async def test_pe50_declared_oversize_does_not_iterate_and_closes_body(
    tmp_path: Path,
) -> None:
    """An advertised cap refusal happens before body bytes or file creation."""
    target = tmp_path / 'object.bin'
    client = RecordingS3Client(chunks=[b'never'], content_length=5)

    with pytest.raises(ResponseTooLargeError):
        await file_config.stream_s3_download(
            client,
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(target),
            overwrite=False,
            max_response_bytes=4,
        )

    assert client.bodies[0].iterations == 0
    assert client.bodies[0].closed is True
    assert not target.exists()


async def test_pe50_exclusive_refusal_closes_without_consuming_s3_body(
    tmp_path: Path,
) -> None:
    """An existing non-overwrite target remains unchanged and unread."""
    target = tmp_path / 'object.bin'
    target.write_bytes(b'old')
    client = RecordingS3Client(chunks=[b'new'], content_length=3)

    with pytest.raises(ConfigurationError):
        await file_config.stream_s3_download(
            client,
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(target),
            overwrite=False,
            max_response_bytes=3,
        )

    assert target.read_bytes() == b'old'
    assert client.bodies[0].iterations == 0
    assert client.bodies[0].closed is True


@pytest.mark.parametrize('overwrite', [False, True])
async def test_pe50_s3_stream_cancellation_closes_and_cleans_before_commit(
    tmp_path: Path,
    overwrite: bool,
) -> None:
    """Cancellation during the body keeps old targets and removes partials."""
    target = tmp_path / 'object.bin'
    if overwrite:
        target.write_bytes(b'old')
    started = asyncio.Event()
    body = WaitingS3Body(started)
    client = RawResponseS3Client({'Body': body})
    downloading = asyncio.create_task(file_config.stream_s3_download(
        client,
        bucket_name='bucket',
        s3_filepath='key',
        local_filepath=str(target),
        overwrite=overwrite,
        max_response_bytes=1024,
    ))
    await started.wait()
    downloading.cancel()

    with pytest.raises(asyncio.CancelledError):
        await downloading
    assert body.closed is True
    if overwrite:
        assert target.read_bytes() == b'old'
        assert list(tmp_path.iterdir()) == [target]
    else:
        assert not target.exists()


@pytest.mark.parametrize('overwrite', [False, True])
async def test_pe50_close_cancellation_obeys_overwrite_commit_policy(
    tmp_path: Path,
    overwrite: bool,
) -> None:
    """Body-close cancellation precedes commit and preserves old bytes."""
    target = tmp_path / 'object.bin'
    if overwrite:
        target.write_bytes(b'old')
    close_started = asyncio.Event()
    close_release = asyncio.Event()
    body = ControlledAsyncCloseBody(
        [b'new'],
        close_started=close_started,
        close_release=close_release,
    )
    client = RawResponseS3Client(
        {'Body': body, 'ContentLength': 3})
    downloading = asyncio.create_task(file_config.stream_s3_download(
        client,
        bucket_name='bucket',
        s3_filepath='key',
        local_filepath=str(target),
        overwrite=overwrite,
        max_response_bytes=3,
    ))
    await close_started.wait()
    downloading.cancel()
    await asyncio.sleep(0)
    close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await downloading
    assert body.closed is True
    if overwrite:
        assert target.read_bytes() == b'old'
        assert list(tmp_path.iterdir()) == [target]
    else:
        assert not target.exists()


async def test_pe50_successful_async_body_close_precedes_commit(
    tmp_path: Path,
) -> None:
    """A successful awaitable body close completes before returning bytes."""
    body = ControlledAsyncCloseBody([b'body'])
    target = tmp_path / 'object.bin'

    result = await file_config.stream_s3_download(
        RawResponseS3Client({'Body': body}),
        bucket_name='bucket',
        s3_filepath='key',
        local_filepath=str(target),
        overwrite=False,
        max_response_bytes=4,
    )

    assert result['bytes_written'] == 4
    assert body.closed is True
    assert target.read_bytes() == b'body'


async def test_pe50_async_body_close_self_cancellation_propagates_exactly(
    tmp_path: Path,
) -> None:
    """A child-cancelled cleanup task terminates with its exact marker."""
    marker = asyncio.CancelledError('body-close-self-cancelled')

    class SelfCancellingCloseBody(ChunkedS3Body):
        """Cancel from inside the provider's awaitable close operation."""

        async def close(self) -> None:
            """Record entry and raise the child cancellation marker."""
            self.closed = True
            raise marker

    body = SelfCancellingCloseBody([b'body'])
    target = tmp_path / 'object.bin'
    with pytest.raises(asyncio.CancelledError) as caught:
        await file_config.stream_s3_download(
            RawResponseS3Client({'Body': body}),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(target),
            overwrite=False,
            max_response_bytes=4,
        )

    assert_cancelled_error_survives_task_boundary(
        caught.value,
        ('body-close-self-cancelled',),
        original=marker,
    )
    assert body.closed is True
    assert not target.exists()


async def test_pe50_async_body_close_failure_is_typed_and_redacted(
    tmp_path: Path,
) -> None:
    """Provider close text is chained behind the transport vocabulary."""
    body = ControlledAsyncCloseBody(
        [b'body'],
        close_error=RuntimeError('provider secret close detail'),
    )
    client = RawResponseS3Client({'Body': body})

    with pytest.raises(TransportError) as caught:
        await file_config.stream_s3_download(
            client,
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'object.bin'),
            overwrite=False,
            max_response_bytes=1024,
        )

    assert 'secret' not in str(caught.value)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert body.closed is True


async def test_pe50_body_close_failure_precedes_overwrite_commit(
    tmp_path: Path,
) -> None:
    """Preserve the old target when body cleanup fails before dispatch."""
    target = tmp_path / 'object.bin'
    target.write_bytes(b'old')
    body = ControlledAsyncCloseBody(
        [b'new'],
        close_error=RuntimeError('provider secret close detail'),
    )

    with pytest.raises(TransportError):
        await file_config.stream_s3_download(
            RawResponseS3Client({'Body': body}),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(target),
            overwrite=True,
            max_response_bytes=3,
        )

    assert body.closed is True
    assert target.read_bytes() == b'old'
    assert list(tmp_path.iterdir()) == [target]


async def test_pe50_stream_failure_precedes_a_secondary_body_close_failure(
    tmp_path: Path,
) -> None:
    """Cleanup completes without hiding the primary provider stream error."""
    stream_error = RuntimeError('provider secret stream detail')
    close_error = RuntimeError('provider secret close detail')

    class StreamAndCloseFailureBody(ControlledAsyncCloseBody):
        """Fail during both iteration and subsequent body cleanup."""

        async def iter_chunks(self, chunk_size: int) -> Any:
            """Raise the configured primary provider failure."""
            del chunk_size
            if False:
                yield b''
            raise stream_error

    body = StreamAndCloseFailureBody([], close_error=close_error)
    target = tmp_path / 'object.bin'
    with pytest.raises(TransportError) as caught:
        await file_config.stream_s3_download(
            RawResponseS3Client({'Body': body}),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(target),
            overwrite=False,
            max_response_bytes=1024,
        )

    assert caught.value.__cause__ is stream_error
    assert body.closed is True
    assert not target.exists()


async def test_pe50_early_fallback_body_close_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The setup fallback reports cleanup failure before any path commit."""
    async def skip_iteration(*args: Any, **kwargs: Any) -> int:
        """Simulate an early path layer that never starts the body iterable."""
        del args, kwargs
        return 0

    body = ControlledAsyncCloseBody(
        [], close_error=RuntimeError('provider secret close detail'))
    monkeypatch.setattr(file_config, 'stream_to_path', skip_iteration)

    with pytest.raises(TransportError) as caught:
        await file_config.stream_s3_download(
            RawResponseS3Client({'Body': body}),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'object.bin'),
            overwrite=True,
            max_response_bytes=1024,
        )

    assert 'secret' not in str(caught.value)
    assert body.closed is True


@pytest.mark.parametrize(
    'raw_response',
    [
        pytest.param([], id='not-a-mapping'),
        pytest.param({}, id='body-missing'),
    ],
)
async def test_pe50_malformed_s3_response_is_typed(
    tmp_path: Path,
    raw_response: Any,
) -> None:
    """Malformed SDK mappings never leak a KeyError or TypeError."""
    with pytest.raises(SerializationError):
        await file_config.stream_s3_download(
            RawResponseS3Client(raw_response),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'object.bin'),
            overwrite=False,
            max_response_bytes=1024,
        )


@pytest.mark.parametrize(
    'content_length',
    [
        pytest.param(True, id='bool'),
        pytest.param(-1, id='negative'),
        pytest.param('4', id='string'),
    ],
)
async def test_pe50_invalid_content_length_is_typed_and_body_is_closed(
    tmp_path: Path,
    content_length: Any,
) -> None:
    """SDK metadata shape failure still closes the already-owned body."""
    body = ChunkedS3Body([b'body'])
    client = RawResponseS3Client({
        'Body': body,
        'ContentLength': content_length,
    })

    with pytest.raises(SerializationError):
        await file_config.stream_s3_download(
            client,
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'object.bin'),
            overwrite=False,
            max_response_bytes=1024,
        )

    assert body.closed is True
    assert body.iterations == 0


async def test_pe50_body_without_close_or_iterator_is_typed(
    tmp_path: Path,
) -> None:
    """Both mandatory streaming-body operations are shape-checked."""
    class NoClose:
        """Body exposing only the iterator."""

        async def iter_chunks(self, chunk_size: int) -> Any:
            """Yield no chunks."""
            del chunk_size
            if False:
                yield b''

    class NoIterator:
        """Body exposing only cleanup."""

        def __init__(self) -> None:
            """Initialize cleanup recording."""
            self.closed = False

        def close(self) -> None:
            """Record cleanup."""
            self.closed = True

    with pytest.raises(SerializationError):
        await file_config.stream_s3_download(
            RawResponseS3Client({'Body': NoClose()}),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'no-close'),
            overwrite=False,
            max_response_bytes=1024,
        )
    no_iterator = NoIterator()
    with pytest.raises(SerializationError):
        await file_config.stream_s3_download(
            RawResponseS3Client({'Body': no_iterator}),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'no-iterator'),
            overwrite=False,
            max_response_bytes=1024,
        )
    assert no_iterator.closed is True


async def test_pe50_iterator_creation_failure_is_typed_redacted_and_closed(
    tmp_path: Path,
) -> None:
    """A provider failure before iteration is safe and still owns cleanup."""
    class CreationFailureBody(ChunkedS3Body):
        """Body whose iterator factory raises."""

        def iter_chunks(self, chunk_size: int) -> Any:
            """Raise provider-authored detail before returning an iterator."""
            del chunk_size
            raise RuntimeError('provider secret iterator detail')

    body = CreationFailureBody([])
    with pytest.raises(SerializationError) as caught:
        await file_config.stream_s3_download(
            RawResponseS3Client({'Body': body}),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'object.bin'),
            overwrite=False,
            max_response_bytes=1024,
        )

    assert 'secret' not in str(caught.value)
    assert body.closed is True


async def test_pe50_stream_wraps_even_typed_provider_failure(
    tmp_path: Path,
) -> None:
    """Wrap even provider-raised gateway types behind a generic message."""
    marker = SerializationError('provider secret typed marker')

    class TypedFailureBody(ChunkedS3Body):
        """Body raising one typed gateway error during iteration."""

        async def iter_chunks(self, chunk_size: int) -> Any:
            """Raise the exact marker from the async iterator."""
            del chunk_size
            if False:
                yield b''
            raise marker

    body = TypedFailureBody([])
    with pytest.raises(TransportError) as caught:
        await file_config.stream_s3_download(
            RawResponseS3Client({'Body': body}),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'object.bin'),
            overwrite=False,
            max_response_bytes=1024,
        )

    assert 'secret' not in str(caught.value)
    assert caught.value.__cause__ is marker
    assert body.closed is True


async def test_pe50_legacy_helper_new_controls_preserve_compatibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep legacy defaults while routing new controls through one seam."""
    target = tmp_path / 'object.bin'
    target.write_bytes(b'old')
    default_client = RecordingS3Client(
        chunks=[b'a' * 64], content_length=64)
    default_session = RecordingSession(default_client)
    monkeypatch.setattr(file_config.aioboto3, 'Session', default_session)

    result = await download_file_from_s3(
        bucket_name='bucket',
        s3_filepath='key',
        local_filepath=str(target),
    )

    assert result is None
    assert target.read_bytes() == b'a' * 64

    refusing_client = RecordingS3Client(chunks=[b'new'], content_length=3)
    refusing_session = RecordingSession(refusing_client)
    monkeypatch.setattr(file_config.aioboto3, 'Session', refusing_session)
    with pytest.raises(ConfigurationError):
        await download_file_from_s3(
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(target),
            overwrite=False,
            max_response_bytes=3,
        )
    assert target.read_bytes() == b'a' * 64
    assert refusing_client.bodies[0].closed is True


async def test_pe50_body_attribute_failure_is_typed_and_redacted(
    tmp_path: Path,
) -> None:
    """A hostile close property cannot leak its provider-authored detail."""
    class AttributeFailureBody:
        """Body whose close attribute cannot be inspected."""

        @property
        def close(self) -> Any:
            """Raise while the library validates or obtains cleanup."""
            raise RuntimeError('provider secret attribute detail')

        async def iter_chunks(self, chunk_size: int) -> Any:
            """Yield no chunks."""
            del chunk_size
            if False:
                yield b''

    with pytest.raises(SerializationError) as caught:
        await file_config.stream_s3_download(
            RawResponseS3Client({'Body': AttributeFailureBody()}),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'object.bin'),
            overwrite=False,
            max_response_bytes=1024,
        )

    assert 'secret' not in str(caught.value)


async def test_pe50_response_metadata_accessor_failure_closes_body(
    tmp_path: Path,
) -> None:
    """A mapping method failure is typed and cannot skip body ownership."""
    body = ChunkedS3Body([])

    class BrokenGet(dict):
        """Mapping whose optional metadata accessor fails."""

        def get(self, key: str, default: Any = None) -> Any:
            """Raise provider detail instead of returning metadata."""
            del key, default
            raise RuntimeError('provider secret mapping detail')

    response = BrokenGet(Body=body)
    with pytest.raises(SerializationError) as caught:
        await file_config.stream_s3_download(
            RawResponseS3Client(response),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'object.bin'),
            overwrite=False,
            max_response_bytes=1024,
        )

    assert 'secret' not in str(caught.value)
    assert body.closed is True


@pytest.mark.parametrize('cancelled', [False, True])
async def test_pe50_sync_body_close_failure_is_not_raw(
    tmp_path: Path,
    cancelled: bool,
) -> None:
    """Synchronous close preserves cancellation and types other failures."""
    class SyncCloseFailureBody(ChunkedS3Body):
        """Body whose synchronous close fails."""

        def close(self) -> None:
            """Raise the configured cleanup outcome."""
            if cancelled:
                raise asyncio.CancelledError
            raise RuntimeError('provider secret sync-close detail')

    body = SyncCloseFailureBody([b'body'])
    error_type = asyncio.CancelledError if cancelled else TransportError
    with pytest.raises(error_type) as caught:
        await file_config.stream_s3_download(
            RawResponseS3Client({'Body': body}),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'object.bin'),
            overwrite=False,
            max_response_bytes=1024,
        )

    assert 'secret' not in str(caught.value)


async def test_pe50_cancelled_async_close_prefers_cancellation_to_late_error(
    tmp_path: Path,
) -> None:
    """Retrieve a late provider close failure behind caller cancellation."""
    close_started = asyncio.Event()
    close_release = asyncio.Event()
    body = ControlledAsyncCloseBody(
        [b'body'],
        close_started=close_started,
        close_release=close_release,
        close_error=RuntimeError('provider secret late-close detail'),
    )
    downloading = asyncio.create_task(file_config.stream_s3_download(
        RawResponseS3Client({'Body': body}),
        bucket_name='bucket',
        s3_filepath='key',
        local_filepath=str(tmp_path / 'object.bin'),
        overwrite=False,
        max_response_bytes=1024,
    ))
    await close_started.wait()
    downloading.cancel()
    await asyncio.sleep(0)
    downloading.cancel()
    close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await downloading
    assert body.closed is True


async def test_pe50_iterator_factory_cancellation_propagates_and_closes(
    tmp_path: Path,
) -> None:
    """Cancellation raised by iterator creation is not relabelled."""
    class CancelledFactoryBody(ChunkedS3Body):
        """Body whose iterator factory reports cancellation."""

        def iter_chunks(self, chunk_size: int) -> Any:
            """Raise cancellation before returning an iterator."""
            del chunk_size
            raise asyncio.CancelledError

    body = CancelledFactoryBody([])
    with pytest.raises(asyncio.CancelledError):
        await file_config.stream_s3_download(
            RawResponseS3Client({'Body': body}),
            bucket_name='bucket',
            s3_filepath='key',
            local_filepath=str(tmp_path / 'object.bin'),
            overwrite=False,
            max_response_bytes=1024,
        )
    assert body.closed is True


class RecordingS3ClientContext:
    """The async context manager ``Session.client('s3')`` returns."""

    def __init__(self, client: RecordingS3Client) -> None:
        """Wrap a client double.

        Args:
            client: The double to yield on entry.
        """
        self.client = client

    async def __aenter__(self) -> RecordingS3Client:
        """Enter the context.

        Returns:
            The wrapped client double.
        """
        return self.client

    async def __aexit__(self, *exc_info: Any) -> bool:
        """Leave the context without suppressing anything.

        Args:
            *exc_info: The in-flight exception triple, if any.

        Returns:
            False, so an error raised inside propagates.
        """
        return False


class RecordingSession:
    """An ``aioboto3.Session`` double recording how it was constructed.

    Attributes:
        init_kwargs: The keyword arguments the session was built with --
            the credential-carrying half of the call, asserted separately
            from the download itself.
        client_calls: One entry per ``client(...)`` call, as
            ``(args, kwargs)``.
        client_double: The client every ``client(...)`` call yields.
    """

    def __init__(self, client_double: RecordingS3Client) -> None:
        """Build the session factory.

        Args:
            client_double: The client to hand out.
        """
        self.init_kwargs: Dict[str, Any] = {}
        self.client_calls: List[tuple] = []
        self.client_double = client_double

    def __call__(self, **kwargs: Any) -> 'RecordingSession':
        """Stand in for ``aioboto3.Session(...)``.

        Args:
            **kwargs: Credential and region arguments.

        Returns:
            This same object, now carrying the recorded arguments.
        """
        self.init_kwargs = dict(kwargs)
        return self

    def client(self, *args: Any, **kwargs: Any) -> RecordingS3ClientContext:
        """Record a client request and hand back the double.

        Stands in for the real ``Session.client('s3')``.

        Args:
            *args: Positional arguments, e.g. the service name.
            **kwargs: Any keyword arguments.

        Returns:
            An async context manager yielding the client double.
        """
        self.client_calls.append((args, kwargs))
        return RecordingS3ClientContext(self.client_double)


@pytest.fixture
def s3_session(monkeypatch: pytest.MonkeyPatch) -> RecordingSession:
    """Replace ``aioboto3.Session`` with a recording double.

    Args:
        monkeypatch: pytest's attribute patcher.

    Returns:
        The session double, already installed on the module under test.
    """
    session = RecordingSession(RecordingS3Client())
    monkeypatch.setattr(
        'asyncio_gateway.utils.http_file_config.aioboto3.Session', session)
    return session


# --- H15/MG5: the duplicate module is gone --------------------------------


def test_the_duplicate_file_helper_module_no_longer_imports() -> None:
    """R25-AC1: the second copy is deleted, and stays deleted.

    It held a ``download_file_from_s3`` whose parameters were in a
    different order from the survivor's, so importing the wrong one and
    calling positionally wrote the bucket name to a local path. A test
    rather than a one-time deletion because nothing else would notice it
    being restored.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module('asyncio_gateway.helpers.common.file_helper')


def test_the_dead_fetch_file_helper_is_gone() -> None:
    """R25-AC1 (MG5): the unreferenced ``fetch_file`` is deleted.

    It was the duplicate module's only importer, which is what made the
    duplicate removable at all.
    """
    request_helper = importlib.import_module(
        'asyncio_gateway.helpers.internal.request_helper')

    assert not hasattr(request_helper, 'fetch_file')


def test_the_retired_status_constant_is_gone() -> None:
    """R25-AC5 (L13): ``STATUS_CODE_403`` is removed.

    A constant named after its own value, whose only consumer was the
    single-status check H16 replaces.
    """
    constants = importlib.import_module('asyncio_gateway.utils.constants')

    assert not hasattr(constants, 'STATUS_CODE_403')


# --- H15: the S3 call, against the aioboto3 15.x API ----------------------


async def test_the_s3_download_streams_get_object_into_the_local_file(
    s3_session: RecordingSession,
    tmp_path: Path,
) -> None:
    """PE-50: the helper and strategy now share the streamed primitive."""
    target = tmp_path / 'object.png'

    result = await download_file_from_s3(
        bucket_name='my-bucket',
        s3_filepath='path/to/object.png',
        local_filepath=str(target),
    )

    assert result is None
    assert s3_session.client_double.get_calls == [{
        'Bucket': 'my-bucket',
        'Key': 'path/to/object.png',
    }]
    assert target.read_bytes() == b'downloaded object'
    assert s3_session.client_double.bodies[0].closed is True


async def test_the_s3_download_passes_no_positional_arguments(
    s3_session: RecordingSession,
    tmp_path: Path,
) -> None:
    """The three arguments are named, so none can be silently reordered."""
    await download_file_from_s3(
        bucket_name='my-bucket',
        s3_filepath='k',
        local_filepath=str(tmp_path / 'f'),
    )

    assert s3_session.client_double.positional_calls == [()]


async def test_the_s3_download_builds_a_session_not_a_module_client(
    s3_session: RecordingSession,
    tmp_path: Path,
) -> None:
    """R25-AC2: ``Session().client('s3')``, the surviving 15.x API.

    ``aioboto3.client(...)`` was removed in 9.0; this release pins
    ``>=15.5.0`` (FI-11), so the module-level factory is not merely
    deprecated but absent.
    """
    await download_file_from_s3(
        bucket_name='b',
        s3_filepath='k',
        local_filepath=str(tmp_path / 'f'),
        access_key='AKIA-not-a-real-key',
        secret_key='not-a-real-secret',
        region='eu-west-1',
    )

    assert s3_session.init_kwargs == {
        'aws_access_key_id': 'AKIA-not-a-real-key',
        'aws_secret_access_key': 'not-a-real-secret',
        'region_name': 'eu-west-1',
    }
    assert s3_session.client_calls == [(('s3',), {})]


def _module_level_aioboto3_client_calls(source: str) -> List[int]:
    """Find every ``aioboto3.client(...)`` call in a module's source.

    Matched on the parsed tree rather than by searching the text, so the
    docstrings that *explain* why the removed factory is banned -- this
    module's and the one under test -- cannot be mistaken for the call
    itself. The same technique guards ``ClientSession(timeout=)`` in
    ``tests/helpers/test_request_helper.py``, for the same reason.

    Args:
        source: The module's source text.

    Returns:
        The line number of each offending call, in file order.
    """
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'client'
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == 'aioboto3'
    )


def _get_object_keywords(source: str) -> List[str]:
    """Collect the keywords every ``get_object(...)`` call passes.

    Args:
        source: The module's source text.

    Returns:
        Every keyword name used across all such calls, in file order.
    """
    return [
        keyword.arg or '**'
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, 'attr', None) == 'get_object'
        for keyword in node.keywords
    ]


def test_the_module_level_aioboto3_client_factory_is_not_called() -> None:
    """The removed factory is absent from the code, not just unused.

    Asserted structurally because the call sat inside a coroutine no test
    could reach without credentials -- which is exactly how it survived
    to be found by review rather than by a failure.
    """
    source = HTTP_FILE_CONFIG_SOURCE.read_text(encoding='utf-8')

    assert _module_level_aioboto3_client_calls(source) == []


def test_the_streamed_get_object_call_names_only_bucket_and_key() -> None:
    """The SDK never reopens the local path through ``download_file``."""
    source = HTTP_FILE_CONFIG_SOURCE.read_text(encoding='utf-8')

    keywords = _get_object_keywords(source)

    assert keywords == ['Bucket', 'Key']


def test_the_call_scan_reports_the_forms_it_bans() -> None:
    """A scan that silently matched nothing would report clean forever."""
    source = (
        'async def one():\n'
        '    c = aioboto3.client("s3")\n'
        '    await c.get_object(Bucket=b, Key=k)\n'
        'async def two():\n'
        '    async with session.client("s3") as c:\n'
        '        await c.get_object(Bucket=b, Key=k)\n'
    )

    assert _module_level_aioboto3_client_calls(source) == [2]
    assert _get_object_keywords(source) == [
        'Bucket', 'Key', 'Bucket', 'Key']


# --- R25-AC3: keyword-only, so a positional call is a TypeError -----------


def test_the_s3_download_refuses_positional_arguments() -> None:
    """R25-AC3: the structural half of the duplicate fix.

    With two copies in different orders a positional call wrote the
    bucket name to a local path. One copy removes the ambiguity;
    keyword-only parameters remove the ability to express it at all, so
    the mistake is a ``TypeError`` at the call site.
    """
    with pytest.raises(TypeError):
        # `type: ignore[misc]` -- the positional call is the thing under
        # test, so mypy's (correct) refusal of it has to be silenced for
        # the runtime TypeError to be reachable.
        download_file_from_s3(  # type: ignore[misc]
            'my-bucket', 'key', '/tmp/f')


async def test_the_s3_download_still_accepts_every_argument_by_keyword(
    s3_session: RecordingSession,
    tmp_path: Path,
) -> None:
    """Keyword-only bites positional callers, not legitimate ones.

    Every optional parameter is passed explicitly as None, which is the
    shape a caller relying on botocore's credential chain writes.
    """
    await download_file_from_s3(
        bucket_name='b',
        s3_filepath='k',
        local_filepath=str(tmp_path / 'f'),
        access_key=None,
        secret_key=None,
        region=None,
    )

    assert len(s3_session.client_double.get_calls) == 1


async def test_the_s3_download_ignores_extra_pre_processor_params(
    s3_session: RecordingSession,
    tmp_path: Path,
) -> None:
    """The README invokes this as a ``pre_processor_config`` callable.

    That hands the function the caller's whole parameter mapping, so an
    unexpected key must not be a ``TypeError``.
    """
    await download_file_from_s3(
        bucket_name='b',
        s3_filepath='k',
        local_filepath=str(tmp_path / 'f'),
        file_download_path='https://example.invalid/ignored',
    )

    assert len(s3_session.client_double.get_calls) == 1


# --- R25 edge case: absent credentials are the caller's misconfiguration --


@pytest.mark.parametrize(
    'raised',
    [
        pytest.param(NoCredentialsError(), id='no-credentials'),
        pytest.param(
            PartialCredentialsError(
                provider='provider-canary',
                cred_var='credential-canary',
            ),
            id='partial-credentials'),
    ],
)
async def test_absent_s3_credentials_become_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    raised: BaseException,
) -> None:
    """Wrapped in this library's vocabulary, not swallowed.

    A missing credential is the caller's configuration problem, so it
    reports as ``CONFIG``/400 rather than surfacing as a bare botocore
    type the caller has to know about to catch.
    """
    session = RecordingSession(RecordingS3Client(raises=raised))
    monkeypatch.setattr(
        'asyncio_gateway.utils.http_file_config.aioboto3.Session', session)

    with pytest.raises(ConfigurationError) as caught:
        await download_file_from_s3(
            bucket_name='b', s3_filepath='k',
            local_filepath=str(tmp_path / 'f'))

    assert caught.value.code == 'CONFIG'
    assert caught.value.status_code == 400
    formatted = ''.join(traceback.format_exception(caught.value))
    caplog.set_level(logging.ERROR)
    logging.getLogger(__name__).error(
        'safe credential failure',
        exc_info=(
            type(caught.value),
            caught.value,
            caught.value.__traceback__,
        ),
    )
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert raised.args == ()
    combined = formatted + caplog.text
    assert 'provider-canary' not in combined
    assert 'credential-canary' not in combined
    assert 'partial credentials' not in combined.lower()


async def test_an_s3_failure_that_is_not_credentials_is_not_wrapped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only the credential chain's errors are reclassified.

    Wrapping everything would relabel a genuine transport failure as the
    caller's misconfiguration, which is the opposite of informative.
    """
    raised = OSError('connection reset')
    session = RecordingSession(RecordingS3Client(raises=raised))
    monkeypatch.setattr(
        'asyncio_gateway.utils.http_file_config.aioboto3.Session', session)

    with pytest.raises(OSError) as caught:
        await download_file_from_s3(
            bucket_name='b', s3_filepath='k',
            local_filepath=str(tmp_path / 'f'))

    assert caught.value is raised


# --- R21: the version-skew arm of the verb resolver -----------------------


class TransportWithoutTheVerb:
    """A transport client offering none of the operations HTTP names.

    The shape a *future* ``aiohttp`` takes if it renames or withdraws a
    request shortcut: the allowlist still carries the verb, and the
    object no longer answers to it.
    """


class TransportWhereTheVerbIsNotCallable:
    """A transport client whose ``get`` is an attribute, not a method.

    The near miss the ``callable`` test exists for rather than a plain
    ``hasattr``: ``getattr`` finds something, so a resolver checking only
    for presence would hand this back and the caller would earn
    ``TypeError: 'str' object is not callable`` from inside the request
    loop.

    Attributes:
        get: A string standing where a bound method belongs.
    """

    get = 'not an operation'


@pytest.mark.parametrize(
    'client',
    [
        pytest.param(TransportWithoutTheVerb(), id='attribute-absent'),
        pytest.param(
            TransportWhereTheVerbIsNotCallable(), id='attribute-not-callable'),
    ],
)
def test_an_allowed_verb_the_transport_lacks_is_refused_by_name(
    client: Any,
) -> None:
    """R28: version skew is refused here, not discovered further in.

    This arm is unreachable through the public path with the pinned
    ``aiohttp``, because :data:`HTTP_VERBS` is derived from the request
    shortcuts ``ClientSession`` actually exposes -- so it is driven by
    handing :func:`resolve_verb` a stand-in client directly, which is the
    only way to express a transport library that has moved on.

    Delete this and the arm is free to be dropped as dead code, and the
    day a release withdraws a shortcut the caller no longer gets a
    ``CONFIG``/400 naming the verb: they get an ``AttributeError`` -- or,
    for the non-callable case, a ``TypeError`` -- raised from somewhere
    inside the transport, against a verb *this library told them was
    allowed*. The message therefore has to name three things a caller
    can act on: which ``protocol_info`` key, which verb, and which client
    class came up short.
    """
    with pytest.raises(UnsupportedVerbError) as caught:
        resolve_verb(
            client, 'get', allowed=HTTP_VERBS, setting='request_type')

    message = str(caught.value)
    assert 'request_type' in message
    assert type(client).__name__ in message
    assert "'get'" in message
    assert caught.value.code == 'CONFIG'
    assert caught.value.status_code == 400


def test_the_verb_resolver_still_returns_a_real_transport_operation() -> None:
    """The control: the skew arm refuses, it does not refuse everything.

    A resolver that raised for every client would satisfy both rows above
    while making every HTTP call impossible, so the admitted case is
    asserted beside them -- and asserted as *identity* with the bound
    attribute, because returning some other callable would be the same
    defect wearing a different mask.
    """
    class Transport:
        """A client that does offer the operation."""

        def get(self) -> Text:
            """Stand in for a request shortcut.

            Returns:
                A marker proving this exact attribute was handed back.
            """
            return 'the real operation'

    client = Transport()

    operation = resolve_verb(
        client, ' GET ', allowed=HTTP_VERBS, setting='request_type')

    assert operation == client.get
    assert operation() == 'the real operation'


# --- H16: every non-success status refuses to write -----------------------


@pytest.mark.parametrize(
    'status',
    [
        pytest.param(400, id='400-bad-request'),
        pytest.param(403, id='403-the-only-one-checked-before'),
        pytest.param(404, id='404-the-one-that-was-saved-as-the-file'),
        pytest.param(500, id='500-server-error'),
        pytest.param(502, id='502-bad-gateway'),
    ],
)
async def test_a_failing_url_download_writes_nothing(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    status: int,
) -> None:
    """R25-AC4: the status is carried, and no file is left behind.

    Only 403 used to be checked, so each of the other statuses here wrote
    its body to the caller's path as though it were the requested file --
    and any later upload step shipped that error page to the destination.

    The absence of the file is asserted as strictly as the error: an
    empty file would be indistinguishable from a legitimate zero-length
    download to whatever runs next.
    """
    target = tmp_path / 'never-written.bin'
    http_server.respond(
        '/file', status=status, body=b'<html>an error page</html>')

    with pytest.raises(HttpStatusError) as caught:
        await download_file_from_url(
            http_server.url_for('/file'), str(target))

    assert caught.value.status_code == status
    assert caught.value.code == 'HTTP_STATUS'
    assert not target.exists()


async def test_a_successful_url_download_writes_the_body(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """R25-AC4: 200 is the case that still writes."""
    target = tmp_path / 'downloaded.bin'
    http_server.respond('/file', body=b'the real file')

    await download_file_from_url(http_server.url_for('/file'), str(target))

    assert target.read_bytes() == b'the real file'


async def test_a_redirected_url_download_is_judged_on_its_final_status(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """R25-AC4: a followed 301 is not itself a failure.

    301 is above the error threshold in numeric order but is not an
    error; the status that decides is the final hop's, after aiohttp has
    followed the chain.
    """
    target = tmp_path / 'downloaded.bin'
    http_server.respond(
        '/moved', status=301, headers={'Location': '/final'})
    http_server.respond('/final', body=b'arrived')

    await download_file_from_url(http_server.url_for('/moved'), str(target))

    assert target.read_bytes() == b'arrived'
    assert [r.path for r in http_server.requests] == ['/moved', '/final']


async def test_a_redirect_chain_ending_in_an_error_writes_nothing(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """The login-redirect shape H16 names, end to end.

    A 302 to an HTML login page used to be followed and the login page
    written out as the caller's file.
    """
    target = tmp_path / 'never-written.bin'
    http_server.respond(
        '/file', status=302, headers={'Location': '/login'})
    http_server.respond(
        '/login', status=401, body=b'<html>please sign in</html>')

    with pytest.raises(HttpStatusError) as caught:
        await download_file_from_url(
            http_server.url_for('/file'), str(target))

    assert caught.value.status_code == 401
    assert not target.exists()


async def test_a_zero_length_success_body_is_written(
    http_server: RecordingHTTPServer,
    tmp_path: Path,
) -> None:
    """R25 edge case: an empty object is a legitimate download.

    Stated as a test because the natural reading of "refuse to save an
    error page" is a non-empty-body check, and that would make this
    library -- rather than the endpoint -- decide the caller's file is
    invalid.
    """
    target = tmp_path / 'empty.bin'
    http_server.respond('/file', status=200, body=b'')

    await download_file_from_url(http_server.url_for('/file'), str(target))

    assert target.exists()
    assert target.read_bytes() == b''

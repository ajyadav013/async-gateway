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
import importlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from async_gateway.utils.exceptions import (
    ConfigurationError,
    HttpStatusError,
)
from async_gateway.utils.http_file_config import (
    download_file_from_s3,
    download_file_from_url,
)

from botocore.exceptions import NoCredentialsError, PartialCredentialsError

import pytest

from tests.fixtures.http_server import RecordingHTTPServer

#: The module under test, as a path, for the two structural scans below.
HTTP_FILE_CONFIG_SOURCE = (
    Path(__file__).resolve().parents[2]
    / 'async_gateway' / 'utils' / 'http_file_config.py'
)


class RecordingS3Client:
    """An S3 client double that records the download call it received.

    Attributes:
        download_calls: One entry per ``download_file`` call, holding the
            keyword arguments exactly as passed. Positional arguments are
            captured separately so a call that stopped using keywords is
            visible rather than silently reshaped into one that did.
        positional_calls: One entry per call, holding its positional
            arguments.
        raises: An exception to raise from ``download_file`` instead of
            recording, or None to record normally.
    """

    def __init__(self, raises: Optional[BaseException] = None) -> None:
        """Build the double.

        Args:
            raises: Exception ``download_file`` should raise, or None.
        """
        self.download_calls: List[Dict[str, Any]] = []
        self.positional_calls: List[tuple] = []
        self.raises = raises

    async def download_file(self, *args: Any, **kwargs: Any) -> None:
        """Record a download request, or raise the configured error.

        Args:
            *args: Positional arguments, recorded so their absence can be
                asserted.
            **kwargs: Keyword arguments, recorded for exact comparison.

        Returns:
            None.

        Raises:
            BaseException: Whatever ``raises`` was constructed with.
        """
        self.positional_calls.append(args)
        self.download_calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises


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
        'async_gateway.utils.http_file_config.aioboto3.Session', session)
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
        importlib.import_module('async_gateway.helpers.common.file_helper')


def test_the_dead_fetch_file_helper_is_gone() -> None:
    """R25-AC1 (MG5): the unreferenced ``fetch_file`` is deleted.

    It was the duplicate module's only importer, which is what made the
    duplicate removable at all.
    """
    request_helper = importlib.import_module(
        'async_gateway.helpers.internal.request_helper')

    assert not hasattr(request_helper, 'fetch_file')


def test_the_retired_status_constant_is_gone() -> None:
    """R25-AC5 (L13): ``STATUS_CODE_403`` is removed.

    A constant named after its own value, whose only consumer was the
    single-status check H16 replaces.
    """
    constants = importlib.import_module('async_gateway.utils.constants')

    assert not hasattr(constants, 'STATUS_CODE_403')


# --- H15: the S3 call, against the aioboto3 15.x API ----------------------


async def test_the_s3_download_calls_download_file_with_filename(
    s3_session: RecordingSession,
) -> None:
    """R25-AC2: the exact ``download_file`` signature, pinned.

    ``Filename=`` is the assertion that matters. The call passed
    ``file_save_path=``, which ``download_file`` does not accept, so the
    function could never have succeeded in either copy.
    """
    await download_file_from_s3(
        bucket_name='my-bucket',
        s3_filepath='path/to/object.png',
        local_filepath='/tmp/object.png',
    )

    assert s3_session.client_double.download_calls == [{
        'Bucket': 'my-bucket',
        'Key': 'path/to/object.png',
        'Filename': '/tmp/object.png',
    }]


async def test_the_s3_download_passes_no_positional_arguments(
    s3_session: RecordingSession,
) -> None:
    """The three arguments are named, so none can be silently reordered."""
    await download_file_from_s3(
        bucket_name='my-bucket',
        s3_filepath='k',
        local_filepath='/tmp/f',
    )

    assert s3_session.client_double.positional_calls == [()]


async def test_the_s3_download_builds_a_session_not_a_module_client(
    s3_session: RecordingSession,
) -> None:
    """R25-AC2: ``Session().client('s3')``, the surviving 15.x API.

    ``aioboto3.client(...)`` was removed in 9.0; this release pins
    ``>=15.5.0`` (FI-11), so the module-level factory is not merely
    deprecated but absent.
    """
    await download_file_from_s3(
        bucket_name='b',
        s3_filepath='k',
        local_filepath='/tmp/f',
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


def _download_file_keywords(source: str) -> List[str]:
    """Collect the keywords every ``download_file(...)`` call passes.

    Args:
        source: The module's source text.

    Returns:
        Every keyword name used across all such calls, in file order.
    """
    return [
        keyword.arg or '**'
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, 'attr', None) == 'download_file'
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


def test_the_download_call_uses_filename_and_not_file_save_path() -> None:
    """``file_save_path=`` is not a keyword ``download_file`` accepts.

    Pinned on the parsed call rather than the text so that the docstring
    naming the old keyword does not satisfy -- or trip -- the check.
    """
    source = HTTP_FILE_CONFIG_SOURCE.read_text(encoding='utf-8')

    keywords = _download_file_keywords(source)

    assert 'Filename' in keywords
    assert 'file_save_path' not in keywords


def test_the_call_scan_reports_the_forms_it_bans() -> None:
    """A scan that silently matched nothing would report clean forever."""
    source = (
        'async def one():\n'
        '    c = aioboto3.client("s3")\n'
        '    await c.download_file(Bucket=b, Key=k, file_save_path=p)\n'
        'async def two():\n'
        '    async with session.client("s3") as c:\n'
        '        await c.download_file(Bucket=b, Key=k, Filename=p)\n'
    )

    assert _module_level_aioboto3_client_calls(source) == [2]
    assert _download_file_keywords(source) == [
        'Bucket', 'Key', 'file_save_path', 'Bucket', 'Key', 'Filename']


# --- R25-AC3: keyword-only, so a positional call is a TypeError -----------


def test_the_s3_download_refuses_positional_arguments() -> None:
    """R25-AC3: the structural half of the duplicate fix.

    With two copies in different orders a positional call wrote the
    bucket name to a local path. One copy removes the ambiguity;
    keyword-only parameters remove the ability to express it at all, so
    the mistake is a ``TypeError`` at the call site.
    """
    with pytest.raises(TypeError):
        download_file_from_s3('my-bucket', 'key', '/tmp/f')  # type: ignore


async def test_the_s3_download_still_accepts_every_argument_by_keyword(
    s3_session: RecordingSession,
) -> None:
    """Keyword-only bites positional callers, not legitimate ones.

    Every optional parameter is passed explicitly as None, which is the
    shape a caller relying on botocore's credential chain writes.
    """
    await download_file_from_s3(
        bucket_name='b',
        s3_filepath='k',
        local_filepath='/tmp/f',
        access_key=None,
        secret_key=None,
        region=None,
    )

    assert len(s3_session.client_double.download_calls) == 1


async def test_the_s3_download_ignores_extra_pre_processor_params(
    s3_session: RecordingSession,
) -> None:
    """The README invokes this as a ``pre_processor_config`` callable.

    That hands the function the caller's whole parameter mapping, so an
    unexpected key must not be a ``TypeError``.
    """
    await download_file_from_s3(
        bucket_name='b',
        s3_filepath='k',
        local_filepath='/tmp/f',
        file_download_path='https://example.invalid/ignored',
    )

    assert len(s3_session.client_double.download_calls) == 1


# --- R25 edge case: absent credentials are the caller's misconfiguration --


@pytest.mark.parametrize(
    'raised',
    [
        pytest.param(NoCredentialsError(), id='no-credentials'),
        pytest.param(
            PartialCredentialsError(provider='env', cred_var='secret_key'),
            id='partial-credentials'),
    ],
)
async def test_absent_s3_credentials_become_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
) -> None:
    """Wrapped in this library's vocabulary, not swallowed.

    A missing credential is the caller's configuration problem, so it
    reports as ``CONFIG``/400 rather than surfacing as a bare botocore
    type the caller has to know about to catch.
    """
    session = RecordingSession(RecordingS3Client(raises=raised))
    monkeypatch.setattr(
        'async_gateway.utils.http_file_config.aioboto3.Session', session)

    with pytest.raises(ConfigurationError) as caught:
        await download_file_from_s3(
            bucket_name='b', s3_filepath='k', local_filepath='/tmp/f')

    assert caught.value.code == 'CONFIG'
    assert caught.value.status_code == 400
    assert caught.value.__cause__ is raised


async def test_an_s3_failure_that_is_not_credentials_is_not_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the credential chain's errors are reclassified.

    Wrapping everything would relabel a genuine transport failure as the
    caller's misconfiguration, which is the opposite of informative.
    """
    raised = OSError('connection reset')
    session = RecordingSession(RecordingS3Client(raises=raised))
    monkeypatch.setattr(
        'async_gateway.utils.http_file_config.aioboto3.Session', session)

    with pytest.raises(OSError) as caught:
        await download_file_from_s3(
            bucket_name='b', s3_filepath='k', local_filepath='/tmp/f')

    assert caught.value is raised


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

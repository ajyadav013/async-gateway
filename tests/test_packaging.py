"""Mechanical anti-drift checks on the committed CI interpreter matrix.

The matrix in ``.github/workflows/ci.yml`` is a literal list on purpose
(release spec R1, Step 3): a derived matrix shrinks silently when a runner
image changes. A literal list, though, drifts the other way -- it can fall
behind ``requires-python`` or lose a minor to a careless edit, and neither
shows up in a green CI run. These tests are the guard that makes such an
edit fail the suite instead of passing unnoticed.

Contiguity alone cannot catch the commonest drift of all -- deleting the
*highest* entry, which leaves a shorter but still perfectly contiguous run.
The ceiling is therefore pinned against ``pyproject.toml``'s
``Programming Language :: Python :: X.Y`` classifiers, which the release
spec already requires to track the matrix. Dropping ``3.14`` from one of
the two lists and not the other now fails.

The two files are read as text rather than parsed. ``pyproject.toml`` cannot
go through ``tomllib`` because this suite runs on the 3.10 leg of that very
matrix and ``tomllib`` arrived in 3.11; the workflow cannot go through
``yaml`` because PyYAML is not a dependency of this project and adding one
to satisfy a test would be a poor trade. Both patterns are anchored and
both helpers raise when they fail to match, so a formatting change breaks
the test loudly rather than silently reading nothing.
"""

import ast
import importlib.util
import re
import subprocess  # nosec B404 - builds this project's own sdist, no input
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from tests.fixtures.http_server import RecordingHTTPServer

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'ci.yml'
PYPROJECT = REPO_ROOT / 'pyproject.toml'
EXAMPLES = REPO_ROOT / 'examples'

#: The five scripts R35 requires, named rather than globbed. A glob would
#: keep passing after one of them was deleted, which is the drift this
#: list exists to catch.
EXAMPLE_NAMES: Tuple[str, ...] = (
    'http_example.py',
    'ftp_example.py',
    'sftp_example.py',
    'soap_example.py',
    'error_handling_example.py',
)

#: R35-AC5's "≤ ~40 lines", applied to the code rather than to the file:
#: the module docstring is the part a reader most wants, and counting it
#: against the budget would only buy brevity by deleting explanation.
MAX_EXAMPLE_CODE_LINES = 40

_MATRIX_PATTERN = re.compile(
    r'^\s*python-version:\s*\[(?P<entries>[^\]]+)\]\s*$',
    re.MULTILINE,
)
_FLOOR_PATTERN = re.compile(
    r'^requires-python\s*=\s*[\'"]>=\s*(?P<major>\d+)\.(?P<minor>\d+)[\'"]',
    re.MULTILINE,
)
_CLASSIFIER_PATTERN = re.compile(
    r'^\s*[\'"]Programming Language :: Python :: '
    r'(?P<major>\d+)\.(?P<minor>\d+)[\'"]',
    re.MULTILINE,
)

Version = Tuple[int, int]


def _read_requires_python_floor() -> Version:
    """Read the ``requires-python`` lower bound from ``pyproject.toml``.

    Returns:
        The floor as a ``(major, minor)`` pair, e.g. ``(3, 10)``.

    Raises:
        AssertionError: If no ``requires-python = ">=X.Y"`` line is found.
    """
    match = _FLOOR_PATTERN.search(PYPROJECT.read_text(encoding='utf-8'))
    assert match is not None, (
        f'no `requires-python = ">=X.Y"` line found in {PYPROJECT}'
    )
    return int(match['major']), int(match['minor'])


def _read_ci_matrix() -> List[Version]:
    """Read the committed interpreter matrix from the CI workflow.

    Returns:
        The matrix entries as ``(major, minor)`` pairs, in committed order.

    Raises:
        AssertionError: If the workflow does not declare exactly one
            ``python-version: [...]`` matrix, or an entry is not ``X.Y``.
    """
    found = _MATRIX_PATTERN.findall(CI_WORKFLOW.read_text(encoding='utf-8'))
    assert len(found) == 1, (
        f'expected exactly one `python-version: [...]` matrix in '
        f'{CI_WORKFLOW}, found {len(found)}'
    )
    entries: List[Version] = []
    for raw in found[0].split(','):
        parts = raw.strip().strip('\'"').split('.')
        assert len(parts) == 2, f'matrix entry {raw.strip()!r} is not `X.Y`'
        entries.append((int(parts[0]), int(parts[1])))
    return entries


def _read_version_classifiers() -> List[Version]:
    """Read the ``Programming Language :: Python :: X.Y`` classifiers.

    Returns:
        The declared interpreter versions as ``(major, minor)`` pairs, in
        declared order.

    Raises:
        AssertionError: If ``pyproject.toml`` declares no such classifier.
    """
    found = _CLASSIFIER_PATTERN.findall(PYPROJECT.read_text(encoding='utf-8'))
    assert found, (
        f'no `Programming Language :: Python :: X.Y` classifiers in '
        f'{PYPROJECT}'
    )
    return [(int(major), int(minor)) for major, minor in found]


def test_ci_matrix_is_not_empty() -> None:
    """The workflow declares a non-empty interpreter matrix."""
    assert _read_ci_matrix()


def test_every_ci_matrix_entry_is_at_or_above_the_requires_python_floor(
) -> None:
    """No matrix entry is older than the packaging floor.

    An entry below ``requires-python`` is an interpreter CI claims to test
    but pip refuses to install on -- the leg fails for the wrong reason and
    tells nothing about the code.
    """
    floor = _read_requires_python_floor()
    below = [entry for entry in _read_ci_matrix() if entry < floor]
    assert not below, (
        f'CI matrix entries {below} are below the requires-python floor '
        f'{floor[0]}.{floor[1]}'
    )


def test_ci_matrix_minors_are_contiguous_and_ascending() -> None:
    """The matrix skips no minor version and is committed in order.

    A skipped minor is the drift this test exists for: deleting ``3.14``
    from the list leaves a green CI that no longer covers the newest
    interpreter, and nothing else in the repository would notice.
    """
    entries = _read_ci_matrix()
    expected = [
        (entries[0][0], entries[0][1] + offset)
        for offset in range(len(entries))
    ]
    assert entries == expected, (
        f'CI matrix {entries} is not a contiguous ascending run of minors '
        f'starting at {entries[0][0]}.{entries[0][1]}; expected {expected}'
    )


def test_ci_matrix_matches_the_pyproject_version_classifiers() -> None:
    """The matrix and the version classifiers name the same interpreters.

    This is what pins the *ceiling*. Deleting the highest entry from the
    matrix leaves a shorter contiguous run that the test above accepts;
    only a second, independent list of the same versions catches it.
    """
    assert _read_ci_matrix() == _read_version_classifiers(), (
        f'CI matrix {_read_ci_matrix()} and pyproject classifiers '
        f'{_read_version_classifiers()} disagree; both must name the same '
        'interpreters in the same order'
    )


# --------------------------------------------------------------------------
# examples/ (release spec R35, Step 30)
#
# The README has historically shipped invalid Python, which is the whole
# reason R35 asks for runnable scripts instead. Scripts rot the same way
# unless something executes them, so these tests do three separate things
# and each catches a failure the others cannot:
#
#   * the files exist, are ≤ ~40 code lines, and parse;
#   * each one *imports*, which is what catches a renamed symbol -- a
#     compile check passes happily on `from async_gateway import gone`;
#   * the two examples that can be pointed at a local server are actually
#     *run* against the loopback recording server, so a changed envelope
#     key or error code fails here rather than in a consumer's code.
#
# No example is run against a third-party endpoint (R35-AC3): the HTTP and
# SOAP examples take their URL as a parameter and are handed one belonging
# to this suite's own server, and the FTP and SFTP examples -- which need
# a real FTP/SSH daemon -- are import-checked only.
# --------------------------------------------------------------------------


def _load_example(name: str) -> ModuleType:
    """Import one example script by path and return the live module.

    By path, not by package import: ``examples/`` is deliberately not a
    package (it ships in neither artifact and has no ``__init__.py``), so
    a consumer copying a script runs it as a file and this loads it the
    same way they will.

    Args:
        name: The script's file name, e.g. ``'http_example.py'``.

    Returns:
        The imported module.

    Raises:
        AssertionError: If a loader cannot be built for the path.
    """
    path = EXAMPLES / name
    spec = importlib.util.spec_from_file_location(f'_example_{path.stem}',
                                                  path)
    assert spec is not None and spec.loader is not None, (
        f'cannot build an import spec for {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _code_lines(path: Path) -> int:
    """Count an example's non-blank lines below its module docstring.

    Args:
        path: The example script to measure.

    Returns:
        The number of non-blank lines after the module docstring.
    """
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(path))
    after = tree.body[0].end_lineno if ast.get_docstring(tree) else 0
    assert after is not None
    return len([line for line in source.splitlines()[after:] if line.strip()])


@pytest.fixture
async def running_server() -> Iterator[RecordingHTTPServer]:
    """Yield a started loopback recording server, closed on the way out.

    Yields:
        The running server.
    """
    server = RecordingHTTPServer()
    await server.start()
    try:
        yield server
    finally:
        await server.close()


@pytest.mark.parametrize('name', EXAMPLE_NAMES)
def test_every_required_example_exists(name: str) -> None:
    """R35-AC1: one runnable script per protocol, plus error handling."""
    assert (EXAMPLES / name).is_file(), (
        f'examples/{name} is missing; R35-AC1 names all five scripts')


@pytest.mark.parametrize('name', EXAMPLE_NAMES)
def test_every_example_parses(name: str) -> None:
    """R35-AC2: every example is valid Python.

    ``compileall`` in CI asserts the same property; this makes the suite
    alone sufficient, so a developer sees a broken example before pushing.
    """
    path = EXAMPLES / name
    ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


@pytest.mark.parametrize('name', EXAMPLE_NAMES)
def test_every_example_imports_cleanly(name: str) -> None:
    """Every example's imports resolve against the real package.

    This is the test that bites when the library is refactored. Parsing
    accepts ``from async_gateway.utils.exceptions import Gone``; importing
    does not, so a renamed or deleted public symbol fails here.
    """
    assert _load_example(name) is not None


@pytest.mark.parametrize('name', EXAMPLE_NAMES)
def test_every_example_is_short_enough(name: str) -> None:
    """R35-AC5: each example is ≤ ~40 lines and demonstrates one thing."""
    measured = _code_lines(EXAMPLES / name)
    assert measured <= MAX_EXAMPLE_CODE_LINES, (
        f'examples/{name} has {measured} code lines, over the '
        f'{MAX_EXAMPLE_CODE_LINES}-line budget R35-AC5 sets')


@pytest.mark.parametrize('name', EXAMPLE_NAMES)
def test_no_example_names_a_third_party_endpoint(name: str) -> None:
    """R35-AC3: examples point at loopback, never at somebody's service.

    An example that calls a real third-party host turns a copy-paste into
    unsolicited traffic to a service this project does not own.
    """
    source = (EXAMPLES / name).read_text(encoding='utf-8')
    # `[A-Za-z0-9]` leads the host so a bare ``http://`` named in prose --
    # the docstrings discuss schemes -- is not read as a hostname.
    hosts = re.findall(r'https?://(?P<host>[A-Za-z0-9][^/\s\'"`]*)', source)
    offenders = [
        host for host in hosts
        if host.split(':')[0] not in ('127.0.0.1', 'localhost')
    ]
    assert not offenders, (
        f'examples/{name} names non-loopback host(s) {offenders}')


async def test_the_http_example_runs_against_a_real_server(
    running_server: RecordingHTTPServer,
) -> None:
    """The HTTP example dispatches and reads the envelope correctly.

    Executed, not merely imported: this is what fails if ``ok``, ``json``
    or ``status_code`` is renamed, or if the GET stops being dispatched.
    """
    running_server.respond(
        '/orders',
        body=b'{"id": 7}',
        headers={'Content-Type': 'application/json'},
    )
    example = _load_example('http_example.py')

    result: Dict[str, Any] = await example.fetch(
        running_server.url_for('/orders'))

    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['json'] == {'id': 7}
    assert [request.method for request in running_server.requests] == ['GET']


async def test_the_soap_example_runs_against_a_real_server(
    running_server: RecordingHTTPServer,
) -> None:
    """The SOAP example POSTs an envelope and reads the parsed body.

    The example reaches into ``protocol_details['soap_body']`` and treats
    it as an ``Element``; if that ever became text, the example would be
    teaching a shape that raises, and this goes red.
    """
    running_server.respond(
        '/rates',
        body=(
            b'<?xml version="1.0"?><soap:Envelope '
            b'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            b'<soap:Body><GetRateResponse xmlns="urn:rates">'
            b'<Rate>1.09</Rate></GetRateResponse>'
            b'</soap:Body></soap:Envelope>'
        ),
        headers={'Content-Type': 'text/xml; charset=utf-8'},
    )
    example = _load_example('soap_example.py')

    result: Dict[str, Any] = await example.call(
        running_server.url_for('/rates'))

    assert result['ok'] is True
    body = result['protocol_details']['soap_body']
    assert body is not None and body.tag == '{urn:rates}GetRateResponse'
    assert [request.method for request in running_server.requests] == ['POST']


async def test_the_error_handling_example_reports_a_remote_failure(
    running_server: RecordingHTTPServer,
) -> None:
    """A 404 is an ``ok=False`` envelope that still carries its body.

    Invariant E11, which is the single most useful thing the example
    teaches: ``ok=False`` never costs the caller the response.
    """
    running_server.respond(
        '/missing',
        status=404,
        body=b'{"detail": "no such order"}',
        headers={'Content-Type': 'application/json'},
    )
    example = _load_example('error_handling_example.py')

    result: Dict[str, Any] = await example.call(
        running_server.url_for('/missing'))

    assert result['ok'] is False
    assert result['status_code'] == 404
    assert result['error']['code'] == 'HTTP_STATUS'
    assert result['json'] == {'detail': 'no such order'}


async def test_the_error_handling_example_catches_an_escaping_config_error(
) -> None:
    """A ``ConfigurationError`` escapes and the example handles it.

    The example's ``except ConfigurationError`` is the half a consumer
    most often omits. Driving it needs a URL the entry point rejects
    *before* dispatch, so no server is involved and none is started.
    """
    example = _load_example('error_handling_example.py')

    # `ftp://` under protocol='HTTP' is refused by `dispatch_url_for`,
    # synchronously, before anything is sent.
    result = await example.call('ftp://127.0.0.1/orders')

    assert result == {}


def test_examples_ship_in_neither_artifact(tmp_path: Path) -> None:
    """R35-AC4: ``examples/`` is excluded from the wheel *and* the sdist.

    The decision, made explicitly here rather than inherited from a
    default: the scripts are read in the repository, where the README
    links them, and shipping them would put a second copy of the API's
    documentation inside every install to drift against the first.
    ``MANIFEST.in`` prunes nothing for them -- setuptools simply never
    collects a non-package directory -- so this test is what makes the
    exclusion a stated guarantee rather than an accident that a future
    ``[tool.setuptools]`` edit could silently reverse.

    Built into ``tmp_path``, never into the repository's own ``dist/``:
    the CI build job asserts the checkout is clean before it builds, and
    a test that left artifacts behind would break it.

    ``build`` is not in the dev extra, so this skips where it is absent.
    That is why the CI ``build-and-install`` job carries the same
    assertion against the artifact it has already built -- the guarantee
    is checked there whether or not this test could run here.
    """
    build = subprocess.run(  # nosec B603 - fixed argv, this project's build
        [sys.executable, '-m', 'build', '--outdir', str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        pytest.skip(f'`python -m build` unavailable: {build.stderr[-200:]}')

    wheels = sorted(tmp_path.glob('*.whl'))
    sdists = sorted(tmp_path.glob('*.tar.gz'))
    assert wheels and sdists, 'the build produced no artifacts to inspect'

    with zipfile.ZipFile(wheels[-1]) as wheel:
        in_wheel = [n for n in wheel.namelist() if 'example' in n.lower()]
    with tarfile.open(sdists[-1]) as sdist:  # nosec B202 - names only
        in_sdist = [n for n in sdist.getnames() if 'example' in n.lower()]

    assert not in_wheel, f'examples leaked into the wheel: {in_wheel}'
    assert not in_sdist, f'examples leaked into the sdist: {in_sdist}'

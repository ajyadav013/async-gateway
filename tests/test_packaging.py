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
from importlib.metadata import metadata
from importlib.metadata import version as metadata_version
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Final, Iterator, List, Tuple

import pytest

import asyncio_gateway

from tests.fixtures.http_server import RecordingHTTPServer

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'ci.yml'
PUBLISH_WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'publish.yml'
PYPROJECT = REPO_ROOT / 'pyproject.toml'
PACKAGE_ROOT = REPO_ROOT / 'asyncio_gateway'

#: Where this project lives on GitHub, split into the three parts that can
#: each be renamed independently of the code. Two of them already have been:
#: the repository ``async-gateway`` -> ``asyncio-gateway`` (following the
#: distribution, which PyPI forced -- see ``_TAKEN_NORMALISED_NAMES``), and
#: the default branch ``main`` -> ``master``. Neither rename touches a single
#: line of Python, so nothing failed and both left stale URLs behind in the
#: documentation, found by eye afterwards.
#:
#: These three names are therefore the *only* place any of them is spelled.
#: ``test_documented_github_urls_track_the_current_repository`` holds every
#: documented GitHub URL to them, so the next rename is one edit here and a
#: red suite until the documents follow.
REPOSITORY_OWNER: Final[str] = 'ajyadav013'
REPOSITORY_NAME: Final[str] = 'asyncio-gateway'
DEFAULT_BRANCH: Final[str] = 'master'
REPOSITORY_URL = f'https://github.com/{REPOSITORY_OWNER}/{REPOSITORY_NAME}'

_DISTRIBUTION_METADATA = metadata('asyncio-gateway')
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


# --- The floor is a syntax claim too ----------------------------------------
#
# The tests above assert the matrix *names* 3.10. They cannot assert the
# repository still *parses* on it, and that is a different failure: PEP 701
# relaxed the f-string grammar in 3.12, so an f-string written naturally on
# a modern interpreter is a hard `SyntaxError` on the oldest leg the matrix
# claims. It is a collection-time error, not a test failure -- the module
# never imports, so every test in it silently stops running.
#
# This was not hypothetical. `test_packaging.py` itself carried a
# line-spanning f-string that made this very module unparseable on 3.10 and
# 3.11: the two legs whose *existence* it asserts. Every check above was
# dead on both, and the suite was green on 3.12+ regardless.
#
# `ast.parse(..., feature_version=(3, 10))` does not help -- it gates
# semantics, not the tokenizer, and accepts all three forms below. Nothing
# short of the older interpreter, or this scan, can see them.
#
# The scan itself needs 3.12 to run, which sounds circular and is not.
# Before 3.12 every `FormattedValue` reports the position of the WHOLE
# enclosing f-string rather than of its own replacement field, so the two
# tests below -- "does the field span lines", "does it contain the
# enclosing quote" -- are both trivially true of every f-string ever
# written, and the scan reports the entire repository as offending.
#
# So the scan is skipped below 3.12, and skipping loses nothing: an older
# interpreter does not need to be *told* the file will not parse, it
# fails to parse it. The 3.12+ run is where the answer is not already
# obvious, and it is the run that reports for the older legs.

#: The opening quote of an f-string, after any prefix letters. Needed to
#: tell a *reused* quote inside a replacement field -- legal only from 3.12
#: -- from a different one, which has always been legal.
_FSTRING_QUOTE = re.compile(r'^[A-Za-z]*(?P<quote>\'\'\'|"""|\'|")')

#: The replacement-field forms PEP 701 introduced, each verified to raise
#: `SyntaxError` on a real 3.10 and 3.11 interpreter and to parse on 3.12+.
#:
#: Two entries, not three. PEP 701 also allows a `#` comment inside a
#: field, but that form cannot be detected separately and does not need
#: to be: a comment runs to end of line, so the closing brace is always
#: on a later one and the field is already caught as spanning lines.
#: Testing the source text for `#` as its own arm would be worse than
#: redundant -- it would flag `f'{"#"}'`, which 3.10 accepts.
#:
#: A nested quote that *differs* from the enclosing one is absent for the
#: same reason: it predates PEP 701 and is legal on every version here.
PEP_701_FORMS = ('spans more than one line', 'reuses the enclosing quote')

#: Applied to the scan and to its own two self-tests together, so that
#: the thing under test and the tests of it can never disagree about
#: which interpreters they run on.
needs_field_positions = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason='before 3.12 a FormattedValue reports the position of the '
           'whole f-string rather than its own, so the scan cannot tell '
           'a field from its container; the older legs prove the same '
           'property by failing to parse instead')


def pep_701_fstrings_in(source: str, module: str) -> List[str]:
    """Report replacement fields that need Python 3.12 or newer.

    Walks the f-strings rather than pattern-matching the text: a regex
    over source cannot tell an f-string's braces from a dict literal's,
    and would flag the prose in this very docstring.

    Args:
        source: The module's source text.
        module: A display name for the offence messages.

    Returns:
        One ``module:line: reason`` string per offending field, empty
        when the module parses on the ``requires-python`` floor.
    """
    offenders: List[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.JoinedStr):
            continue
        opener = _FSTRING_QUOTE.match(
            ast.get_source_segment(source, node) or '')
        quote = opener['quote'] if opener else ''
        for value in node.values:
            if not isinstance(value, ast.FormattedValue):
                continue
            field = ast.get_source_segment(source, value) or ''
            if (value.end_lineno or value.lineno) > value.lineno:
                reason = PEP_701_FORMS[0]
            elif quote and quote in field:
                reason = PEP_701_FORMS[1]
            else:
                continue
            offenders.append(f'{module}:{value.lineno}: {reason}')
    return offenders


@needs_field_positions
def test_every_committed_module_parses_on_the_requires_python_floor(
) -> None:
    """No source file needs an interpreter newer than the floor.

    Scoped to the package, the suite and the examples -- everything the
    3.10 CI leg imports. The file list is materialised and asserted
    first: an empty ``offenders`` is the pass condition and is also what
    scanning nothing produces, so a mistyped root would otherwise report
    the tree clean.
    """
    roots = (PACKAGE_ROOT, REPO_ROOT / 'tests', EXAMPLES)
    scanned = sorted(
        path for root in roots for path in root.rglob('*.py'))

    offenders = [
        offence
        for path in scanned
        for offence in pep_701_fstrings_in(
            path.read_text(encoding='utf-8'),
            path.relative_to(REPO_ROOT).as_posix())
    ]

    floor = _read_requires_python_floor()
    assert scanned, f'no modules scanned under {roots}'
    assert offenders == [], (
        f'these f-strings need Python 3.12 or newer, and '
        f'requires-python is >={floor[0]}.{floor[1]}. On the older legs '
        f'the module fails to PARSE, so its tests do not run at all -- '
        f'a green suite on a newer interpreter says nothing about them: '
        f'{offenders}'
    )


@needs_field_positions
@pytest.mark.parametrize('source, expected', [
    ("x = [1]\ny = f'a{\n    x}b'\n", PEP_701_FORMS[0]),
    ("a = {'k': 1}\nb = f'{a['k']}'\n", PEP_701_FORMS[1]),
    # The comment form, which has no arm of its own: it is reported as
    # spanning lines, because a comment always pushes the closing brace
    # onto the next one. Asserted so the reasoning stays checked.
    ("x = 1\ny = f'{x  # note\n}'\n", PEP_701_FORMS[0]),
])
def test_the_floor_scan_reports_every_form_it_claims_to(
    source: str, expected: str,
) -> None:
    """Each PEP 701 form the scan names is one the scan finds.

    A scan that reported nothing would pass the test above for the
    wrong reason, so each form is fed to it and must come back.

    Args:
        source: A module body using one 3.12-only f-string form.
        expected: The reason the scan must give for it.
    """
    offenders = pep_701_fstrings_in(source, 'probe.py')

    assert len(offenders) == 1, offenders
    assert offenders[0].endswith(expected), offenders


@needs_field_positions
@pytest.mark.parametrize('source', [
    # A nested quote that differs from the enclosing one: legal since 3.6.
    'a = {"k": 1}\nb = f"{a[\'k\']}"\n',
    # An ordinary field, and a format spec, on one line.
    "x = 1.5\ny = f'{x} and {x:.2f}'\n",
    # Adjacent implicit concatenation, each part its own f-string.
    "x = 1\ny = (f'a{x}'\n     f'b{x}')\n",
    # A `#` in the literal text rather than in a replacement field.
    "x = 1\ny = f'# {x}'\n",
    # And a `#` inside a nested string: 3.10 takes it, so a text search
    # for `#` -- the arm PEP_701_FORMS deliberately omits -- would be a
    # false positive here.
    'y = f\'{"#"}\'\n',
])
def test_the_floor_scan_permits_what_3_10_already_accepts(
    source: str,
) -> None:
    """The scan flags nothing the floor interpreter would have taken.

    The counterpart to the test above: a scan that flagged every
    f-string would also catch the three forms, and be useless.

    Args:
        source: A module body legal on every supported interpreter.
    """
    assert pep_701_fstrings_in(source, 'probe.py') == []


# --- Version: one source of truth (R5) --------------------------------------
#
# Four mutually contradictory version claims (H22) are collapsed to one:
# `pyproject.toml`'s `[project] version`. `asyncio_gateway.__version__` reads
# the installed distribution's metadata rather than restating the string, so
# the two cannot drift -- but "cannot drift" is a claim about code that has
# to be asserted, because a future edit could reintroduce a literal.

DISTRIBUTION = 'asyncio-gateway'

#: The PEP 503 normalised form of :data:`DISTRIBUTION` -- the string PyPI
#: actually compares names on, and therefore the only spelling worth
#: checking a name's availability against.
#:
#: Pinned as a literal rather than computed from ``DISTRIBUTION`` so that
#: the assertion below is a real comparison. Deriving both sides from the
#: same expression would assert that a function equals itself.
NORMALISED_DISTRIBUTION = 'asyncio-gateway'

#: The normalised names this distribution must **not** collide with, and
#: the reason each is here. `asyncgateway` is not hypothetical: it is a
#: real project ("Itential Gateway Async Client", 0.1.0, 2026-03-09) and
#: it is why the intended name `async-gateway` was rejected by PyPI.
_TAKEN_NORMALISED_NAMES: Final[Dict[str, str]] = {
    'async-gateway': (
        'the name this project was originally written for; PyPI rejects '
        'it as too similar to the existing `asyncgateway`, because all of '
        '`async-gateway`, `async_gateway` and `asyncgateway` normalise to '
        'the same string'
    ),
    'aio-gateway': (
        'blocked the same way by the existing `aiogateway`; recorded so '
        'it is not proposed as the fix next time'
    ),
}


def _pep503_normalise(name: str) -> str:
    """Normalise a distribution name the way PEP 503 defines it.

    Args:
        name: A distribution name in any spelling.

    Returns:
        The normalised form: runs of ``-``, ``_`` and ``.`` collapsed to a
        single ``-``, lowercased.
    """
    return re.sub(r'[-_.]+', '-', name).lower()


def test_the_distribution_name_normalises_as_checked() -> None:
    """The distribution's PEP 503 normalised form is the pinned one.

    **This test exists because a rename already failed for want of it.**
    The project was built as ``async-gateway``, its availability was
    "verified" with ``GET https://pypi.org/pypi/async-gateway/json`` ->
    404, and PyPI then rejected the upload: *"This project name is too
    similar to an existing project."*

    The 404 was true and worthless. PyPI does not compare the spelling
    you type -- it compares the `PEP 503 <https://peps.python.org/pep-0503/>`_
    **normalised** name, ``re.sub(r'[-_.]+', '-', name).lower()``. Under
    that rule ``async-gateway``, ``async_gateway`` and ``asyncgateway``
    are one name, and ``asyncgateway`` was already taken. A check on one
    spelling says nothing about the identity it belongs to.

    So this pins the identity rather than the spelling. A future rename
    that changes ``[project] name`` turns this test red, and the failure
    message states the exact two URLs to check -- which is the step that
    was skipped. A network call is deliberately **not** made here: a unit
    test that reaches PyPI is flaky offline, slow, and would make the
    suite's result depend on someone else's uptime. The manual check is
    documented instead, and the string it must be run against is asserted.
    """
    declared = _read_declared_distribution_name()
    assert declared == DISTRIBUTION, (
        f'pyproject declares the distribution as {declared!r} but this '
        f'suite pins {DISTRIBUTION!r}; if the rename is intended, update '
        'DISTRIBUTION and NORMALISED_DISTRIBUTION together, then verify '
        'BOTH spellings are free on PyPI before uploading'
    )
    actual = _pep503_normalise(declared)
    assert actual == NORMALISED_DISTRIBUTION, (
        f'{declared!r} normalises to {actual!r}, not the pinned '
        f'{NORMALISED_DISTRIBUTION!r}. PyPI compares normalised names, so '
        f'before publishing this name verify BOTH of:\n'
        f'  https://pypi.org/pypi/{declared}/json\n'
        f'  https://pypi.org/pypi/{actual.replace("-", "")}/json\n'
        'return 404. Checking only the hyphenated spelling is what got '
        '`async-gateway` rejected after it was declared available.'
    )


def test_the_distribution_name_avoids_the_names_already_taken() -> None:
    """The name does not normalise onto a name PyPI already holds.

    The companion to the test above: that one pins *what* the name
    normalises to, this one pins *what it must not*. Both are needed --
    a rename could satisfy the pinned-form check by updating both
    constants together and still land straight back on a taken name.

    The list is deliberately short and evidence-based: each entry is a
    name this project actually tried or considered, with the collision
    that ruled it out. It is a record of checks already paid for, not a
    speculative denylist.
    """
    actual = _pep503_normalise(_read_declared_distribution_name())
    assert actual not in _TAKEN_NORMALISED_NAMES, (
        f'the distribution normalises to {actual!r}, which is taken: '
        f'{_TAKEN_NORMALISED_NAMES[actual]}'
    )


_NAME_PATTERN = re.compile(
    r'^name\s*=\s*[\'"](?P<name>[^\'"]+)[\'"]',
    re.MULTILINE,
)


def _read_declared_distribution_name() -> str:
    """Read ``[project] name`` from ``pyproject.toml`` as text.

    Read as text rather than through ``tomllib`` for the reason
    :func:`_read_declared_version` gives: this suite runs on the 3.10 leg
    of the CI matrix and ``tomllib`` arrived in 3.11.

    Returns:
        The distribution name exactly as declared.

    Raises:
        AssertionError: If no top-level ``name = "..."`` line is found.
    """
    match = _NAME_PATTERN.search(PYPROJECT.read_text(encoding='utf-8'))
    assert match is not None, (
        f'no top-level `name = "..."` line found in {PYPROJECT}'
    )
    return match['name']


_VERSION_PATTERN = re.compile(
    r'^version\s*=\s*[\'"](?P<version>[^\'"]+)[\'"]',
    re.MULTILINE,
)


def _read_declared_version() -> str:
    """Read ``[project] version`` from ``pyproject.toml`` as text.

    Read as text, not through ``tomllib``, for the reason given in the module
    docstring: this suite runs on the 3.10 leg of the CI matrix and
    ``tomllib`` arrived in 3.11.

    Returns:
        The version string exactly as declared.

    Raises:
        AssertionError: If no top-level ``version = "..."`` line is found.
    """
    match = _VERSION_PATTERN.search(PYPROJECT.read_text(encoding='utf-8'))
    assert match is not None, (
        f'no top-level `version = "..."` line found in {PYPROJECT}'
    )
    return match['version']


def test_dunder_version_matches_the_installed_distribution_metadata() -> None:
    """``__version__`` equals ``importlib.metadata.version``.

    The acceptance criterion R5-AC1 names verbatim. It holds by construction
    today -- ``__init__`` *reads* the metadata -- and this test is what keeps
    it holding if someone later replaces that read with a literal.
    """
    assert metadata_version(DISTRIBUTION) == asyncio_gateway.__version__


def test_the_declared_version_is_the_one_that_gets_installed() -> None:
    """``pyproject.toml`` is the single source the metadata comes from.

    Closes the loop the test above leaves open: that one proves the two
    *runtime* views agree, this one proves both trace back to the single
    declaration a release edits.
    """
    assert _read_declared_version() == metadata_version(DISTRIBUTION), (
        f'pyproject declares {_read_declared_version()!r} but the installed '
        f'distribution reports {metadata_version(DISTRIBUTION)!r}; reinstall '
        'the editable install if this is a stale environment'
    )


def test_the_version_is_the_one_zero_zero_reset() -> None:
    """The version is ``1.0.0``, down from the fork-inherited 2.x version.

    The decrease is sound because nothing was ever published under **this
    distribution name** (OQ4, re-verified 404 on 2026-08-19 in both PEP 503
    spellings -- see :func:`test_the_distribution_name_normalises_as_checked`),
    so no pin or
    resolver can be broken by it. It is *not* sound on the stronger claim the
    release was originally written on -- "this code has never been published"
    -- which is false: it ships as ``asyncio-requests``, retired at ``2.7.3``.
    This release is a rename with a discontinued predecessor. Pinning the
    exact string here means a careless bump cannot quietly undo the reset the
    release is named for.
    """
    assert asyncio_gateway.__version__ == '1.0.0'


def test_the_version_is_declared_in_exactly_one_place() -> None:
    """No second version literal exists in the package (R5-AC1).

    "Exactly one place" is the part most likely to rot: reintroducing a
    ``__version__ = '1.0.0'`` literal in ``__init__.py`` would leave every
    other test in this file green, because a literal that happens to be
    *correct today* satisfies them all. Only counting the declarations
    catches it, and it catches it on the day the literal is added rather
    than on the day it first disagrees.
    """
    literals = re.compile(r'^\s*__version__\s*[:=]', re.MULTILINE)
    restated = [
        f'{path.relative_to(REPO_ROOT)}:{number}'
        for path in sorted(PACKAGE_ROOT.rglob('*.py'))
        for number, line in enumerate(
            path.read_text(encoding='utf-8').splitlines(), start=1
        )
        if literals.match(line) and '=' in line
        and re.search(r'[\'"]\d', line)
    ]
    assert not restated, (
        f'the version is restated as a literal at {restated}; '
        'pyproject.toml must be the only declaration'
    )


def test_no_fork_inherited_version_survives_in_the_shipped_tree() -> None:
    """``2.7.3`` appears nowhere outside the spec and ticket history (R5-AC3).

    The spec, the stories file and the tickets all *discuss* the old version
    and must keep doing so; the package, the packaging config and the
    workflows must not mention it at all.
    """
    haystacks = [
        PYPROJECT,
        CI_WORKFLOW,
        PUBLISH_WORKFLOW,
        *sorted(PACKAGE_ROOT.rglob('*.py')),
    ]
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in haystacks
        if '2.7.3' in path.read_text(encoding='utf-8')
    ]
    assert not offenders, (
        f'the fork-inherited version 2.7.3 still appears in {offenders}'
    )


# --- Provenance: metadata points at this repository only (R5-AC5, AC6) ------


def test_no_download_url_in_the_packaging_metadata() -> None:
    """``download_url`` appears in no metadata field (R5-AC5, H25).

    The inherited value pointed at ``gofynd/aio-requests``'s release tarball
    -- *a different project's* artifact. PEP 621 has no ``download_url``
    field, so the migration dropped it; this asserts it stays dropped rather
    than returning via a ``Download-URL`` project URL.
    """
    assert _DISTRIBUTION_METADATA.get_all('Download-URL') is None
    assert 'download_url' not in PYPROJECT.read_text(encoding='utf-8')
    labels = [
        entry.split(',', 1)[0].strip().lower()
        for entry in _DISTRIBUTION_METADATA.get_all('Project-URL') or []
    ]
    assert 'download' not in labels and 'download-url' not in labels


def test_every_project_url_resolves_to_this_repository() -> None:
    """``project.urls`` names this repository and nothing else (R5-AC6).

    A URL pointing at the upstream fork source would misdirect bug reports
    to a project that cannot act on them.
    """
    urls = [
        entry.split(',', 1)[1].strip()
        for entry in _DISTRIBUTION_METADATA.get_all('Project-URL') or []
    ]
    assert urls, 'the distribution declares no project URLs at all'
    foreign = [url for url in urls if not url.startswith(REPOSITORY_URL)]
    assert not foreign, (
        f'project URLs {foreign} do not resolve to {REPOSITORY_URL}'
    )


def test_project_description_names_only_the_supported_protocols() -> None:
    """PEP 621 metadata describes the library that is actually shipped."""
    source = PYPROJECT.read_text(encoding='utf-8')
    matched = re.search(
        r'^description\s*=\s*[\'"](?P<description>[^\'"]+)[\'"]$',
        source,
        re.MULTILINE,
    )
    assert matched is not None, 'pyproject declares no literal description'
    description = matched['description']

    for protocol in ('HTTP', 'HTTPS', 'SOAP', 'FTP', 'SFTP'):
        assert protocol in description
    assert 'redis' not in description.lower()
    assert 'XML' not in description


def test_project_urls_cover_the_consumer_support_routes() -> None:
    """Metadata exposes the six repository destinations users need."""
    source = PYPROJECT.read_text(encoding='utf-8')
    section = re.search(
        r'^\[project\.urls\]\n(?P<body>.*?)(?=^\[|\Z)',
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert section is not None, 'pyproject declares no [project.urls] table'
    entries = {
        label.strip(): url for label, url in re.findall(
            r'^(\w[\w ]*)\s*=\s*[\'"]([^\'"]+)[\'"]$',
            section['body'],
            re.MULTILINE,
        )
    }

    assert entries == {
        'Homepage': REPOSITORY_URL,
        'Documentation': f'{REPOSITORY_URL}#readme',
        'Source': REPOSITORY_URL,
        'Issues': f'{REPOSITORY_URL}/issues',
        'Changelog': f'{REPOSITORY_URL}/blob/master/CHANGELOG.md',
        'Security': f'{REPOSITORY_URL}/security/policy',
    }
    assert (REPO_ROOT / 'SECURITY.md').is_file()


#: The documents whose GitHub links a reader will actually follow. The two
#: Markdown files ship -- ``README.md`` is the distribution's long
#: description, so a stale link in it is republished in every sdist -- and
#: the workflow header is what someone debugging an `invalid-publisher`
#: upload reads. ``docs/specs/`` is deliberately absent: it quotes the
#: pre-rename metadata as a record of what the audit found, and correcting
#: a quotation would falsify it.
_LINK_BEARING_DOCUMENTS: Final[Tuple[str, ...]] = (
    'README.md',
    'CHANGELOG.md',
    '.github/workflows/publish.yml',
    '.github/workflows/ci.yml',
)

#: Any URL under this project's GitHub owner, with the repository slug and
#: the remaining path captured separately.
_OWNED_GITHUB_URL = re.compile(
    r'https://github\.com/' + re.escape(REPOSITORY_OWNER)
    + r'/(?P<slug>[\w.-]+)(?P<path>[^\s)>\]`"\']*)')

#: The GitHub path forms that address a repository at a *git ref* -- the
#: ones a default-branch rename breaks. ``compare`` and ``releases/tag``
#: are absent on purpose: they name tags, which renaming a branch does not
#: move.
_REF_BEARING_PATH = re.compile(
    r'^/(?:tree|blob|raw|blame|commits|edit)/(?P<ref>[^/\s]+)')

#: Refs that are legitimately not the default branch: an annotated release
#: tag or a pinned commit. Anything else in a ref-bearing URL is a branch
#: name, and the only branch this project documents is the default one.
_PINNED_REF = re.compile(r'^(?:v\d+\.\d+\.\d+|[0-9a-f]{7,40})$')


def test_documented_github_urls_track_the_current_repository() -> None:
    """Every documented GitHub URL names the live repo and branch.

    Two renames landed after the code was written -- the repository
    ``async-gateway`` -> ``asyncio-gateway``, and the default branch
    ``main`` -> ``master`` -- and neither touched a line of Python. The
    suite stayed green while ``README.md`` went on pointing a reader at
    ``/async-gateway/tree/main/examples``, which 404s twice over. That is
    the failure mode this test exists for: a fact about the project that
    lives *only* in prose, changed outside the tree, caught by eye or not
    at all.

    ``REPOSITORY_NAME`` and ``DEFAULT_BRANCH`` are the single place either
    name is spelled, so the next rename is one edit at the top of this
    file -- and a red suite naming every document that has not followed.

    Both constants are pinned literals rather than probed from ``git`` or
    the network: a value derived from the checkout would agree with
    whatever the checkout happens to be, which asserts nothing, and CI
    builds this project from a detached head where there is no branch to
    read.
    """
    findings: List[str] = []

    for relative_path in _LINK_BEARING_DOCUMENTS:
        document = REPO_ROOT / relative_path
        assert document.is_file(), (
            f'{relative_path} is listed as a link-bearing document but is '
            f'not in the tree; drop it from _LINK_BEARING_DOCUMENTS or '
            f'restore the file')
        text = document.read_text(encoding='utf-8')

        for match in _OWNED_GITHUB_URL.finditer(text):
            line = text.count('\n', 0, match.start()) + 1
            location = f'{relative_path}:{line}'
            slug = match.group('slug')

            if slug != REPOSITORY_NAME:
                findings.append(
                    f'{location} points at repository {slug!r}, but this '
                    f'project lives at {REPOSITORY_NAME!r}')
                continue

            ref_match = _REF_BEARING_PATH.match(match.group('path'))
            if ref_match is None:
                continue
            ref = ref_match.group('ref')
            if ref != DEFAULT_BRANCH and not _PINNED_REF.match(ref):
                findings.append(
                    f'{location} addresses branch {ref!r}, but the default '
                    f'branch is {DEFAULT_BRANCH!r}')

    assert not findings, (
        f'{len(findings)} stale GitHub reference(s):\n'
        + '\n'.join(f'  {finding}' for finding in findings))


def test_the_pending_publisher_block_names_the_current_repository() -> None:
    """The workflow's ``Repository name`` field matches the real repo.

    This one field is not decoration: PyPI matches the OIDC claim's
    repository against it, so a value left behind by a rename fails the
    upload with ``invalid-publisher`` -- an error whose text points at the
    workflow rather than at the stale registration, which is exactly why
    it is worth a mechanical check instead of a careful reading.
    """
    header = PUBLISH_WORKFLOW.read_text(encoding='utf-8')
    declared = re.search(
        r'^#\s+Repository name\s*:\s*(?P<name>\S+)\s*$',
        header, re.MULTILINE)
    assert declared is not None, (
        'the publish workflow no longer states a `Repository name` for the '
        'PyPI pending publisher; that field is what the OIDC claim is '
        'matched against, so losing it loses the only record of what must '
        'be registered')
    assert declared.group('name') == REPOSITORY_NAME, (
        f'the pending-publisher block names repository '
        f'{declared.group("name")!r}, but the repository is '
        f'{REPOSITORY_NAME!r}; PyPI will reject the upload with '
        f'invalid-publisher until the two agree')


# --- Sphinx retirement (R31-AC1, M24) ---------------------------------------


def test_the_sphinx_scaffolding_is_gone() -> None:
    """The three Sphinx files are deleted (R31-AC1).

    The build had never succeeded: no ``.rst`` root document existed, and
    ``docs/source/Makefile`` set ``SOURCEDIR = source`` from *inside*
    ``docs/source/``, resolving to ``docs/source/source``.
    """
    retired = [
        REPO_ROOT / 'docs' / 'make.bat',
        REPO_ROOT / 'docs' / 'source' / 'Makefile',
        REPO_ROOT / 'docs' / 'source' / 'conf.py',
    ]
    surviving = [str(path.relative_to(REPO_ROOT))
                 for path in retired if path.exists()]
    assert not surviving, f'retired Sphinx files still present: {surviving}'


def test_the_sphinx_dev_dependencies_are_gone() -> None:
    """``sphinx`` and ``sphinx-rtd-theme`` left the dev set (R31-AC1).

    Deleting the files while keeping the dependencies would leave two
    installs serving a build that no longer has any files to build.
    """
    dependency_pattern = re.compile(
        r'^\s*[\'"](?P<name>sphinx[^\'">=<~!\[]*)', re.MULTILINE
    )
    declared = dependency_pattern.findall(
        PYPROJECT.read_text(encoding='utf-8')
    )
    assert not declared, (
        f'Sphinx dev dependencies still declared: {declared}'
    )


def test_the_specs_and_project_docs_survived_the_retirement() -> None:
    """Only the three Sphinx files went (R31 edge case).

    Deleting ``docs/`` wholesale would have taken the release spec and the
    ticket history with it. This is the guard on the blast radius.
    """
    assert (REPO_ROOT / 'docs' / 'specs').is_dir()
    assert (REPO_ROOT / 'docs' / 'specs' / 'v1_release_spec.md').is_file()
    assert (REPO_ROOT / 'docs' / 'project' / 'tickets').is_dir()


def test_no_independent_version_claim_survives_in_docs() -> None:
    """``release = '2.1'`` is gone as a second version claim (R5-AC2/R31-AC3).

    It lived in the deleted ``docs/source/conf.py``. Asserting on the whole
    ``docs/`` tree rather than that one path keeps the criterion true if the
    claim reappears somewhere else.
    """
    claim = re.compile(r'^\s*release\s*=\s*[\'"]', re.MULTILINE)
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted((REPO_ROOT / 'docs').rglob('*'))
        if path.is_file() and path.suffix in {'.py', '.cfg', '.bat'}
        and claim.search(path.read_text(encoding='utf-8', errors='replace'))
    ]
    assert not offenders, (
        f'an independent version claim survives in {offenders}')


# --- LICENSE (R34) ----------------------------------------------------------
#
# The fork's own copyright was added *above* the retained upstream line
# rather than replacing it: MIT requires the original notice be preserved,
# and the two-line form is the conventional way a fork states both. This is
# a human decision (OQ7), recorded in the AGW-28 ticket -- the tests below
# assert the decision was carried out, they do not make it.

LICENSE_FILE = REPO_ROOT / 'LICENSE'
MIT_REFERENCE = Path(__file__).resolve().parent / 'data' / 'mit-reference.txt'

_COPYRIGHT_PATTERN = re.compile(r'^Copyright \(c\) .+$', re.MULTILINE)


def _license_body_without_copyright() -> str:
    """Return the LICENSE text with its copyright lines removed.

    Returns:
        The licence body, copyright lines stripped and the blank run they
        left collapsed, so it is directly comparable with the reference.
    """
    stripped = _COPYRIGHT_PATTERN.sub('', LICENSE_FILE.read_text(
        encoding='utf-8'))
    while '\n\n\n' in stripped:
        stripped = stripped.replace('\n\n\n', '\n\n')
    return stripped


def test_the_licence_body_is_byte_identical_to_the_mit_reference() -> None:
    """The MIT text is unaltered apart from the copyright line (R34-AC1).

    ``tests/data/mit-reference.txt`` was verified against the canonical SPDX
    ``MIT`` licence text when it was committed. A byte comparison, not a
    fuzzy one: a licence that has been reworded is a different licence, and
    a single altered word in the warranty disclaimer matters.
    """
    assert _license_body_without_copyright() == MIT_REFERENCE.read_text(
        encoding='utf-8')


def test_the_licence_retains_the_upstream_copyright_and_adds_the_fork() -> (
        None):
    """Both copyright lines are present, fork first (R34-AC2, OQ7).

    Dropping ``2022 Fynd`` would breach the MIT notice-preservation clause;
    omitting the fork's own line would leave the packaging metadata's author
    unattributed in the licence. Both must hold.
    """
    holders = _COPYRIGHT_PATTERN.findall(LICENSE_FILE.read_text(
        encoding='utf-8'))
    assert holders == [
        'Copyright (c) 2026 Arjunsingh Yadav',
        'Copyright (c) 2022 Fynd',
    ]


def test_the_licence_metadata_agrees_with_the_licence_file() -> None:
    """Metadata expression, licence file and author all agree (R34-AC3).

    Note what is *not* asserted: a ``License :: OSI Approved :: MIT
    License`` classifier. Under PEP 639 the SPDX ``License-Expression``
    supersedes it, and setuptools >= 77 -- the pinned build backend --
    raises ``InvalidConfigError`` if both are declared. The expression is
    therefore the single licence claim, and the classifier's absence is
    correct rather than an omission.
    """
    assert _DISTRIBUTION_METADATA.get_all('License-Expression') == ['MIT']
    assert _DISTRIBUTION_METADATA.get_all('License-File') == ['LICENSE']
    classifiers = _DISTRIBUTION_METADATA.get_all('Classifier') or []
    assert not [c for c in classifiers if c.startswith('License ::')], (
        'a License classifier is declared alongside the PEP 639 SPDX '
        'expression; setuptools >= 77 rejects that combination'
    )
    assert LICENSE_FILE.read_text(encoding='utf-8').startswith('MIT License')


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
#     compile check passes happily on `from asyncio_gateway import gone`;
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
    accepts ``from asyncio_gateway.utils.exceptions import Gone``; importing
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


def test_the_readme_states_where_the_examples_live() -> None:
    """The exclusion is documented, not just enforced.

    ``test_examples_ship_in_neither_artifact`` proves ``examples/``
    stays out of both artifacts. That guarantee is invisible to the one
    person it affects: a consumer who installs from the wheel, goes
    looking for the scripts the project wrote for them, and finds
    nothing on disk with no explanation anywhere in the README.

    So the decision is only half-made until the README says it. This
    test is the other half, and it is deliberately paired with the
    artifact test rather than folded into it -- one asserts what the
    build does, this asserts that the document tells the truth about
    it. Each can regress without the other: an edit could ship the
    examples and leave this prose stale, or drop the prose and leave
    the exclusion unexplained.

    The check is behavioural rather than a fixed-string match: it
    requires the README to name the directory, point at the repository
    for it, and state that it is absent from an install -- without
    prescribing the wording that carries those three facts.
    """
    readme = (REPO_ROOT / 'README.md').read_text(encoding='utf-8')

    assert 'examples/' in readme, (
        'the README never names the examples/ directory, so a consumer '
        'who cannot find the scripts in their install has nothing to '
        'read; see test_examples_ship_in_neither_artifact')

    # Derived from the three rename-sensitive names rather than spelled
    # out, because this assertion was itself a casualty of the repository
    # and default-branch renames: it went on passing against the old slug
    # and the old branch while the link it was guarding 404'd.
    linked = f'{REPOSITORY_URL}/tree/{DEFAULT_BRANCH}/examples' in readme
    assert linked, (
        'the README names examples/ but does not link it in the '
        'repository, which is the only place a consumer can now read it')

    lowered = readme.lower()
    states_exclusion = any(
        phrase in lowered for phrase in (
            'not** shipped in the wheel',
            'not shipped in the wheel',
            'ships in neither',
            'repository, not in the package',
        ))
    assert states_exclusion, (
        'the README links examples/ but never says they are absent from '
        'the wheel and the sdist -- the fact a consumer actually trips '
        'over. Keep the sentence that states it, or change '
        'test_examples_ship_in_neither_artifact instead')

    for name in EXAMPLE_NAMES:
        assert name in readme, (
            f'{name} exists in examples/ but the README does not name '
            f'it, so it is unreachable for a consumer reading only the '
            f'installed documentation')


def test_py_typed_ships_in_both_artifacts(tmp_path: Path) -> None:
    """R4-AC5: the PEP 561 marker reaches the consumer.

    ``py.typed`` is not a ``.py`` file, so setuptools does not carry it
    with the package on its own -- it ships only because
    ``[tool.setuptools.package-data]`` names it. Delete that one line and
    the wheel still installs, still imports and still runs; the *only*
    symptom is that every downstream type checker treats
    ``asyncio_gateway`` as untyped and silently ignores the annotations
    this release exists to add. A failure with no runtime signal is
    exactly the kind that needs a test rather than a review.

    Asserted on the **built artifacts**, not on the source tree: the file
    existing in ``asyncio_gateway/`` is what a ``git status`` shows, and it
    is not the property that matters. Both artifacts are checked because
    they are packed by different machinery -- ``package-data`` for the
    wheel, ``MANIFEST.in`` plus setuptools' defaults for the sdist -- and
    a change can drop it from one while leaving the other intact.

    ``build`` is not in the dev extra, so this skips where it is absent,
    for the same reason and with the same CI backstop as the sibling test
    above.

    Args:
        tmp_path: pytest's per-test directory. Built into, never into the
            repository's own ``dist/``, which the CI build job requires
            to be absent from a clean checkout.

    Returns:
        None.
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
        in_wheel = [n for n in wheel.namelist() if n.endswith('py.typed')]
    with tarfile.open(sdists[-1]) as sdist:  # nosec B202 - names only
        in_sdist = [n for n in sdist.getnames() if n.endswith('py.typed')]

    # The sdist prefixes every member with `<name>-<version>/`, so the
    # expected path is derived from the declared version rather than
    # written out -- a literal here would fail the next release for the
    # wrong reason and teach whoever fixes it to loosen the assertion.
    sdist_root = f'asyncio_gateway-{_read_declared_version()}'

    assert in_wheel == ['asyncio_gateway/py.typed'], (
        f'py.typed is missing from the wheel (found {in_wheel!r}); '
        f'downstream type checkers will ignore this package entirely')
    assert in_sdist == [f'{sdist_root}/asyncio_gateway/py.typed'], (
        f'py.typed is missing from the sdist (found {in_sdist!r}); '
        f'an install from source would ship untyped')


# --- The release workflow ---------------------------------------------------
#
# `publish.yml` is the one workflow whose failures are expensive rather than
# merely annoying: a PyPI version is immutable, so a release that goes out
# wrong cannot be re-uploaded and cannot be taken back. It is also the
# workflow that gets exercised least -- once per release, and never from a
# pull request, because `workflow_run` only fires for the copy of the file on
# the default branch. The checks below are the substitute for the review pass
# a frequently-run workflow gets for free.


def _publish_workflow_text() -> str:
    """Read ``publish.yml`` as text.

    Text rather than parsed YAML for the reason the module docstring gives
    for the CI matrix: PyYAML is not a dependency of this project, and
    adding one so a test can read a workflow would be a poor trade.

    Returns:
        The workflow source.

    Raises:
        AssertionError: If the workflow is missing.
    """
    assert PUBLISH_WORKFLOW.is_file(), (
        f'{PUBLISH_WORKFLOW} is missing; releases are automated by it and '
        'the README documents it as the release procedure'
    )
    return PUBLISH_WORKFLOW.read_text(encoding='utf-8')


def test_the_release_workflow_derives_the_distribution_name() -> None:
    """The PyPI probe interpolates the parsed name, never a literal.

    This is the defect that cannot be caught by running the workflow. The
    probe treats **404 as the publish path**, so a URL naming the wrong
    distribution -- the likeliest single error when adapting a release
    workflow from another project -- does not fail. It 404s forever and
    republishes on every merge, and every run is green while it does.

    Asserted on the URL rather than on the whole file: the header comment
    legitimately names ``asyncio-gateway`` several times, because the PyPI
    pending-publisher registration it walks through cannot be described
    without it.
    """
    source = _publish_workflow_text()
    probes = re.findall(r'https://pypi\.org/pypi/(\S+?)/json', source)
    assert probes, (
        'no `https://pypi.org/pypi/<dist>/<version>/json` probe found in '
        f'{PUBLISH_WORKFLOW}; the version check is what makes a merge '
        'without a version bump a no-op instead of a failed release'
    )
    hardcoded = [probe for probe in probes if '$' not in probe]
    assert not hardcoded, (
        f'the PyPI probe hardcodes {hardcoded}; it must interpolate the '
        'name and version parsed from pyproject.toml, or it will query '
        'the wrong distribution, 404 forever, and republish every merge'
    )


def test_the_release_workflow_refuses_an_unreleased_changelog() -> None:
    """A version still marked ``unreleased`` blocks the release.

    ``CHANGELOG.md`` heads an unshipped version ``## [1.0.0] - unreleased``.
    Nothing about publishing rewrites that word, so without this guard the
    release ships an artifact whose own changelog says it was never
    released -- and the wrong text is then permanent, because the version
    is spent and PyPI will not accept a re-upload. The guard converts a
    documentation slip into a blocked release rather than a wrong one.
    """
    source = _publish_workflow_text()
    assert 'unreleased' in source.lower(), (
        f'{PUBLISH_WORKFLOW} does not check for an `unreleased` CHANGELOG '
        'heading; it would happily publish a version documented as '
        'unreleased, and PyPI versions cannot be re-uploaded'
    )


def test_the_release_workflow_gates_on_ci_for_the_released_commit() -> None:
    """Publishing requires a green ``ci.yml`` run for that exact commit.

    ``ci.yml`` is the entire quality argument for a release -- five
    interpreters, a clean-venv install of the real wheel and the real
    sdist, shuffled orders, the coverage ratchet. Publishing without it
    would discard that guarantee at the one moment it is load-bearing.

    Both halves are asserted. The gate must consult ``ci.yml``'s own runs,
    and it must do so for the released commit rather than the branch tip:
    on a ``workflow_run`` the two differ whenever a second merge lands
    while CI is in flight, and a tip-based check would then certify a
    commit nobody built.
    """
    source = _publish_workflow_text()
    assert 'actions/workflows/ci.yml/runs' in source, (
        f'{PUBLISH_WORKFLOW} does not query ci.yml run results; the '
        'release would not be gated on CI having passed'
    )
    assert 'head_sha=' in source, (
        f'{PUBLISH_WORKFLOW} does not filter CI runs by head sha; a green '
        'run on some other commit would be accepted as proof for this one'
    )


def test_every_release_checkout_pins_the_released_commit() -> None:
    """No ``actions/checkout`` in the release workflow floats to the tip.

    A bare checkout resolves to the default branch's current tip, which on
    a ``workflow_run`` is not necessarily the commit CI approved. Left
    unpinned, the workflow can verify one commit and then build, publish
    and tag a different one -- with no error anywhere, because both
    commits are perfectly valid. Every checkout is therefore pinned to the
    resolved sha, and this counts them rather than trusting review.
    """
    source = _publish_workflow_text()
    checkouts = len(re.findall(r'uses:\s*actions/checkout@', source))
    pinned = len(re.findall(r'^\s*ref:\s*\$\{\{', source, re.MULTILINE))
    assert checkouts, f'{PUBLISH_WORKFLOW} checks out nothing'
    assert pinned == checkouts, (
        f'{PUBLISH_WORKFLOW} has {checkouts} checkout step(s) but only '
        f'{pinned} pinned `ref:`; an unpinned checkout floats to the '
        'branch tip and can publish a commit CI never verified'
    )


def test_the_release_workflow_pins_actions_the_way_ci_does() -> None:
    """Both workflows pin the same shared actions to the same majors.

    A release built by ``actions/checkout@v4`` in CI and some other major
    in ``publish.yml`` is not the artifact CI verified. Comparing the two
    files against each other rather than against a written-down version
    keeps this true through the next bump, which will touch ``ci.yml``
    first and is exactly when the two drift.
    """
    pattern = re.compile(r'uses:\s*(actions/[\w-]+)@(v\d+)')

    def majors(source: str) -> Dict[str, str]:
        found: Dict[str, str] = {}
        for action, major in pattern.findall(source):
            found.setdefault(action, major)
        return found

    ci = majors(CI_WORKFLOW.read_text(encoding='utf-8'))
    release = majors(_publish_workflow_text())
    shared = sorted(set(ci) & set(release))
    assert shared, (
        'the two workflows share no `actions/*` step, which is not '
        'credible -- one of them has stopped checking out the repository'
    )
    disagreements = {
        action: (ci[action], release[action])
        for action in shared
        if ci[action] != release[action]
    }
    assert not disagreements, (
        f'ci.yml and publish.yml pin different majors for {disagreements} '
        '(ci, publish); the released artifact must be built by the same '
        'actions that verified it'
    )


def test_the_readme_documents_the_automated_release() -> None:
    """The README's release recipe matches what the workflow does.

    The failure this prevents is a maintainer following a stale recipe:
    hand-building, ``twine upload``-ing and hand-tagging a release the
    workflow was going to cut anyway, and burning the version doing it.
    The recipe must name the workflow, and it must tell the maintainer to
    date the CHANGELOG heading -- the one step the automation cannot do
    for them and the one the guard above will otherwise block on.
    """
    readme = (REPO_ROOT / 'README.md').read_text(encoding='utf-8')
    _, _, after = readme.partition('### Cutting a release')
    section, _, _ = after.partition('\n## ')
    assert section, 'the README has no `### Cutting a release` section'
    assert 'publish.yml' in section, (
        'the README release recipe does not name the workflow that '
        'actually performs the release'
    )
    assert 'CHANGELOG' in section, (
        'the README release recipe does not tell the maintainer to date '
        'the CHANGELOG heading, which the workflow requires'
    )
    # Matched as a *command line* rather than as a substring: the prose above
    # says "no `twine upload`", and a substring check would read the sentence
    # ruling the step out as the step itself.
    upload = re.compile(r'^\s*(python -m )?twine upload\b', re.MULTILINE)
    assert not upload.search(section), (
        'the README still instructs a manual `twine upload`; publishing '
        'is automated and a manual upload would spend the version'
    )

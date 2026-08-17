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

import re
from importlib.metadata import metadata
from importlib.metadata import version as metadata_version
from pathlib import Path
from typing import List, Tuple

import async_gateway

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'ci.yml'
PYPROJECT = REPO_ROOT / 'pyproject.toml'
PACKAGE_ROOT = REPO_ROOT / 'async_gateway'
REPOSITORY_URL = 'https://github.com/ajyadav013/async-gateway'

_DISTRIBUTION_METADATA = metadata('async-gateway')

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


# --- Version: one source of truth (R5) --------------------------------------
#
# Four mutually contradictory version claims (H22) are collapsed to one:
# `pyproject.toml`'s `[project] version`. `async_gateway.__version__` reads
# the installed distribution's metadata rather than restating the string, so
# the two cannot drift -- but "cannot drift" is a claim about code that has
# to be asserted, because a future edit could reintroduce a literal.

DISTRIBUTION = 'async-gateway'

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
    assert metadata_version(DISTRIBUTION) == async_gateway.__version__


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

    The decrease is only sound because nothing was ever published under this
    name (OQ4, re-verified 404 on 2026-08-17). Pinning the exact string here
    means a careless bump cannot quietly undo the reset the release is named
    for.
    """
    assert async_gateway.__version__ == '1.0.0'


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
    and must keep doing so; the package, the packaging config and the CI
    workflow must not mention it at all.
    """
    haystacks = [PYPROJECT, CI_WORKFLOW, *sorted(PACKAGE_ROOT.rglob('*.py'))]
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
    assert not offenders, f'an independent version claim survives in {
        offenders}'


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

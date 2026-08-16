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
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'ci.yml'
PYPROJECT = REPO_ROOT / 'pyproject.toml'

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

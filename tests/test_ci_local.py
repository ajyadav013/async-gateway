"""Anti-drift check binding ``scripts/ci-local.sh`` to ``ci.yml``.

The defect this guards is the one that made a red branch look green: a
full local run of ``pytest``, ``flake8`` and ``mypy`` passed while CI
failed, because several CI gates are inline ``run:`` steps in the
workflow rather than part of any command a developer knows to type.
``scripts/ci-local.sh`` closes that gap by running the superset -- but a
script that mirrors a workflow by hand only stays true until the next
gate is added to one and not the other, which is the same class of
silent drift ``test_packaging.py`` guards the interpreter matrix against.

So the mirror is asserted rather than trusted. Every ``run:`` step in
``ci.yml`` must be accounted for in the script by exactly one annotation:

    # covers:  <job>/<step name>    -- reproduced locally
    # ci-only: <job>/<step name>    -- cannot run locally, with a reason

A step in neither list fails this test, and the failure names the step.
Adding a gate to CI therefore forces a decision about its local
counterpart at the moment it is added, rather than at the next red PR.

Both files are read as text, and the workflow is parsed with an anchored
regex rather than ``yaml``, for the reason ``test_packaging.py`` records:
PyYAML is not a dependency of this project, and adding one to satisfy a
test would be a poor trade. The parser raises when it matches nothing, so
a formatting change breaks this test loudly rather than silently reading
an empty step list.
"""

import re
from pathlib import Path
from typing import Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'ci.yml'
CI_LOCAL = REPO_ROOT / 'scripts' / 'ci-local.sh'

#: A job header: two-space-indented key at the top of a `jobs:` entry.
JOB = re.compile(r'^  (?P<name>[a-z0-9-]+):$', re.MULTILINE)

#: A step's `- name:` line, at the indentation `ci.yml` uses throughout.
STEP_NAME = re.compile(r'^      - name: (?P<name>.+)$', re.MULTILINE)

#: A step that executes something. Only these are gates; a `uses:` step
#: is checkout or setup-python, which have no local counterpart worth
#: asserting.
STEP_RUN = re.compile(r'^        run: ', re.MULTILINE)

#: The script's accounting annotations.
ANNOTATION = re.compile(
    r'^# (?P<kind>covers|ci-only): (?P<step>.+)$', re.MULTILINE)


def workflow_run_steps() -> Set[str]:
    """Return every executable step in the workflow, as ``job/step``.

    A step counts when it has both a ``name:`` and a ``run:``. The
    ``uses:``-only steps (checkout, setup-python) are excluded: they set
    an environment up rather than gating anything.

    Returns:
        The set of ``'<job>/<step name>'`` identifiers.

    Raises:
        AssertionError: If no jobs or no steps are found, which means
            the workflow's formatting changed and this parser is
            reading nothing.
    """
    source = CI_WORKFLOW.read_text(encoding='utf-8')
    jobs = list(JOB.finditer(source))
    assert jobs, f'no jobs parsed out of {CI_WORKFLOW}'

    steps: Set[str] = set()
    for index, job in enumerate(jobs):
        end = jobs[index + 1].start() if index + 1 < len(jobs) else len(source)
        body = source[job.start():end]
        names = list(STEP_NAME.finditer(body))
        for position, step in enumerate(names):
            stop = (names[position + 1].start()
                    if position + 1 < len(names) else len(body))
            if STEP_RUN.search(body[step.start():stop]):
                steps.add(f"{job.group('name')}/{step.group('name').strip()}")

    assert steps, f'no `run:` steps parsed out of {CI_WORKFLOW}'
    return steps


def annotated_steps() -> Tuple[Set[str], Set[str]]:
    """Return the steps the script claims to cover, and to skip.

    Returns:
        ``(covered, ci_only)`` -- the ``# covers:`` and ``# ci-only:``
        identifiers declared in ``scripts/ci-local.sh``.

    Raises:
        AssertionError: If the script declares nothing, which means its
            annotations were reformatted out of the parser's reach.
    """
    source = CI_LOCAL.read_text(encoding='utf-8')
    covered = {
        match.group('step').strip()
        for match in ANNOTATION.finditer(source)
        if match.group('kind') == 'covers'
    }
    ci_only = {
        match.group('step').strip()
        for match in ANNOTATION.finditer(source)
        if match.group('kind') == 'ci-only'
    }
    assert covered or ci_only, f'no annotations parsed out of {CI_LOCAL}'
    return covered, ci_only


def test_the_local_entrypoint_exists_and_is_executable() -> None:
    """The script is present and runnable.

    Every other assertion here passes vacuously against a missing file,
    so the denominator is asserted before anything divides by it.
    """
    assert CI_LOCAL.is_file(), f'{CI_LOCAL} does not exist'
    assert CI_LOCAL.stat().st_mode & 0o111, f'{CI_LOCAL} is not executable'


def test_every_ci_step_is_covered_or_declared_ci_only() -> None:
    """No workflow gate is missing from the local entrypoint.

    This is the test that closes the reported gap. A gate added to
    ``ci.yml`` and not to the script fails here, naming the step, so the
    omission surfaces in the local suite rather than as a red PR.
    """
    steps = workflow_run_steps()
    covered, ci_only = annotated_steps()

    unaccounted = steps - covered - ci_only

    assert not unaccounted, (
        'these `ci.yml` steps are in neither the `# covers:` nor the '
        f'`# ci-only:` list of {CI_LOCAL.relative_to(REPO_ROOT)}: '
        f'{sorted(unaccounted)}. Add the gate to the script, or declare '
        'it `# ci-only:` with the reason it cannot run locally.')


def test_no_annotation_names_a_step_that_no_longer_exists() -> None:
    """The script does not claim to cover a deleted step.

    The mirror drifts in both directions. A renamed or removed CI step
    leaves a stale annotation behind, which would keep asserting
    coverage of a gate that is gone -- and quietly absorb the *new*
    name into the unaccounted set only if someone reads the other test's
    output carefully.
    """
    steps = workflow_run_steps()
    covered, ci_only = annotated_steps()

    stale = (covered | ci_only) - steps

    assert not stale, (
        f'{CI_LOCAL.relative_to(REPO_ROOT)} annotates steps that no '
        f'longer exist in {CI_WORKFLOW.relative_to(REPO_ROOT)}: '
        f'{sorted(stale)}. Update or delete the annotation.')


@pytest.mark.parametrize(
    'gate',
    [
        'no unjustified suppression',
        'no global type suppression',
        'no unjustified coverage pragma',
        'coverage pragma ceiling of 10',
    ],
)
def test_the_inline_only_gates_are_reproduced(gate: str) -> None:
    """The gates that exist *only* as inline CI script are run locally.

    These four are the specific reason the gap existed: none of them is
    part of ``pytest``, ``flake8``, ``mypy`` or ``build``, so no command
    a developer would think to run could see them. Naming them
    individually means a future edit cannot quietly downgrade one to
    ``# ci-only:`` -- which would restore the gap while leaving the
    coverage test above green.
    """
    covered, _ = annotated_steps()
    matching = [
        step for step in covered
        if step.split('/', 1)[1].startswith(gate)
    ]

    assert matching, (
        f'the gate {gate!r} is not marked `# covers:` in '
        f'{CI_LOCAL.relative_to(REPO_ROOT)}. It cannot run as part of '
        'pytest, flake8, mypy or build, so if the local entrypoint does '
        'not run it, nothing local does.')

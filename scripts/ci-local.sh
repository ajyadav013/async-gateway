#!/usr/bin/env bash
#
# Run, locally, every gate `.github/workflows/ci.yml` enforces.
#
# The defect this script exists to close is the one a local suite cannot
# see. Several CI gates are inline `run:` steps in the workflow rather
# than part of `pytest`, `flake8` or `mypy` -- the unjustified-suppression
# check, the coverage-pragma pair, the global-type-suppression assertion,
# the artifact build. A full local run was green while the branch was red,
# because the commands a developer knows to type are a strict subset of
# the commands CI runs. This script is the superset, so that "green
# locally" means "green in CI".
#
# Every gate below is annotated with the workflow job and step it
# reproduces:
#
#   # covers:  <job>/<step name>   -- run here, faithfully
#   # ci-only: <job>/<step name>   -- cannot run locally; why
#
# `tests/test_ci_local.py` parses this file and `ci.yml` together and
# fails when a workflow step is in neither list. That test is what stops
# this script from silently drifting behind the workflow it mirrors --
# without it, the next gate added to CI would reintroduce exactly the gap
# the script was written to close.
#
# Usage:
#   scripts/ci-local.sh              # every locally-runnable gate
#   scripts/ci-local.sh --fast       # skip the slow ones (build, seeds)
#   PYTHON=/path/to/python scripts/ci-local.sh
#
# Exit status: 0 when every gate run passed, 1 otherwise. Gates do not
# short-circuit -- all of them run, and the summary lists each result --
# because the point is to learn everything CI would tell you in one local
# pass rather than one failure at a time.

set -uo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
FAST=0
[ "${1:-}" = '--fast' ] && FAST=1

if ! "$PYTHON" -c '' 2>/dev/null; then
  echo "error: PYTHON=$PYTHON is not runnable. Set PYTHON to your venv's" >&2
  echo "       interpreter, e.g. PYTHON=.venv/bin/python $0" >&2
  exit 1
fi

PASSED=(); FAILED=(); SKIPPED=()

# Run one gate, record its verdict, and keep going.
#
# Args:
#   $1: the gate's name, as it appears in the summary.
#   $@: the command to run.
gate() {
  local name="$1"; shift
  printf '\n\033[1m== %s\033[0m\n' "$name"
  if "$@"; then
    PASSED+=("$name")
  else
    FAILED+=("$name")
    printf '\033[31mFAILED: %s\033[0m\n' "$name"
  fi
}

# Record a gate that was deliberately not run, with the reason.
skip() {
  SKIPPED+=("$1 -- $2")
  printf '\n\033[33m== %s (skipped: %s)\033[0m\n' "$1" "$2"
}

# ---------------------------------------------------------------- lint

# ci-only: lint-type-test/Install the project and its dev extra
#   Environment setup, not a gate. Locally the venv already exists; this
#   script deliberately never installs into it.
# ci-only: shuffled-order/Install the project and its dev extra
#   The same step in the second job, for the same reason.

# covers: lint-type-test/flake8
gate 'flake8' "$PYTHON" -m flake8 .

# covers: lint-type-test/no unjustified suppression (R27-AC7)
gate 'no unjustified suppression' \
  "$PYTHON" .github/scripts/check_suppressions.py

# covers: lint-type-test/format check (R27-AC8)
gate 'format check' \
  "$PYTHON" -m flake8 --select=E1,E2,E3,E501,W2,W3,W505,Q .

# --------------------------------------------------------------- types

# covers: lint-type-test/mypy
gate 'mypy' "$PYTHON" -m mypy async_gateway

# covers: lint-type-test/no global type suppression (R27-AC5)
no_global_type_suppression() {
  if grep -qE '^\s*ignore_errors' pyproject.toml; then
    echo 'error: ignore_errors is back in pyproject.toml' >&2
    return 1
  fi
  if awk '/^\[tool\.mypy\]/{f=1;next} /^\[/{f=0} f' pyproject.toml \
     | grep -qE '^\s*ignore_missing_imports'; then
    echo 'error: ignore_missing_imports is set globally in [tool.mypy];' \
         'it belongs in a per-module override that names the package' >&2
    return 1
  fi
  echo 'no global type suppression.'
}
gate 'no global type suppression' no_global_type_suppression

# --------------------------------------------------------------- tests

# covers: lint-type-test/pytest
gate 'pytest' "$PYTHON" -m pytest

# covers: lint-type-test/no unjustified coverage pragma (R28-AC7)
no_unjustified_pragma() {
  local offenders
  offenders=$(grep -rn 'pragma:[[:space:]]*no[[:space:]]*cover' \
    async_gateway tests examples | grep -v -- '--' || true)
  if [ -n "$offenders" ]; then
    echo 'error: a `# pragma: no cover` carries no `--` justification;' \
         'the permitted categories are `if TYPE_CHECKING:`, an' \
         '`@abstractmethod` body, and a platform-guarded fallback' >&2
    echo "$offenders" >&2
    return 1
  fi
  echo 'every coverage pragma is justified.'
}
gate 'no unjustified coverage pragma' no_unjustified_pragma

# covers: lint-type-test/coverage pragma ceiling of 10 (R28-AC7)
pragma_ceiling() {
  local count
  count=$(grep -rc 'pragma:[[:space:]]*no[[:space:]]*cover' \
    async_gateway tests examples | awk -F: '{total += $2} END {print total + 0}')
  echo "pragma count: $count (ceiling 10)"
  if [ "$count" -gt 10 ]; then
    echo "error: $count coverage pragmas exceeds the ceiling of 10" >&2
    return 1
  fi
}
gate 'coverage pragma ceiling of 10' pragma_ceiling

# covers: lint-type-test/R7 fitness behaviours 1-6 (the resilience-library evidence)
# The workflow writes this `-k` across two lines under a `>-` folded
# scalar, which YAML joins into one line before pytest sees it. Written
# on one line here for the same reason: pytest's `-k` parser rejects an
# embedded newline outright.
gate 'R7 fitness behaviours 1-6' \
  "$PYTHON" -m pytest tests/helpers/test_circuit_breaker.py \
  -k 'behaviour_1 or behaviour_2 or behaviour_3 or behaviour_4 or behaviour_5 or behaviour_6' \
  --no-cov -p no:randomly -v

# ------------------------------------------------------------ security

# covers: lint-type-test/bandit
gate 'bandit' "$PYTHON" -m bandit -c pyproject.toml -r async_gateway -ll

# ------------------------------------------------------------ examples

# covers: lint-type-test/compile the examples (R35-AC2)
gate 'compile the examples' "$PYTHON" -m compileall -q examples/

# ------------------------------------------------------- shuffled order

# covers: shuffled-order/pytest in shuffled order
if [ "$FAST" = 1 ]; then
  skip 'pytest in shuffled order' '--fast'
else
  shuffled() {
    local seed
    for seed in 1 20250816 424242 99991 2147483647; do
      echo "--- seed $seed"
      "$PYTHON" -m pytest -p randomly --randomly-seed="$seed" -q \
        || return 1
    done
  }
  gate 'pytest in shuffled order (5 seeds)' shuffled
fi

# ----------------------------------------------------- coverage ratchet

# covers: coverage-ratchet/Fetch the base branch's pyproject.toml
# covers: coverage-ratchet/Compare head's fail_under against the base branch's
ratchet() {
  local base
  base="$(git rev-parse --abbrev-ref --symbolic-full-name @{upstream} \
    2>/dev/null)" || base=''
  if [ -z "$base" ]; then
    base="origin/$(git symbolic-ref --short HEAD 2>/dev/null)"
  fi
  # The workflow compares against the PR's base branch; locally the
  # nearest honest equivalent is the tracked remote branch. When neither
  # resolves the comparison is against an absent floor, which reads as 0
  # -- the same fallback the workflow takes for a base with no
  # pyproject.toml.
  git show "$base:pyproject.toml" > /tmp/base-pyproject.toml 2>/dev/null \
    || : > /tmp/base-pyproject.toml
  BASE_PYPROJECT=/tmp/base-pyproject.toml HEAD_PYPROJECT=pyproject.toml \
    "$PYTHON" - <<'PY'
import os
import sys
import tomllib


def fail_under(path: str) -> float:
    """Read `tool.coverage.report.fail_under`, 0.0 when absent."""
    try:
        with open(path, 'rb') as handle:
            data = tomllib.load(handle)
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return 0.0
    report = data.get('tool', {}).get('coverage', {}).get('report', {})
    return float(report.get('fail_under', 0))


base = fail_under(os.environ['BASE_PYPROJECT'])
head = fail_under(os.environ['HEAD_PYPROJECT'])
print(f'base fail_under = {base}')
print(f'head fail_under = {head}')
if head < base:
    sys.exit(
        f'coverage ratchet broken: head {head} < base {base}. '
        'fail_under may only ever be raised.'
    )
print('coverage ratchet holds.')
PY
}
gate 'coverage ratchet' ratchet

# --------------------------------------------------- build the artifact

# covers: build-and-install/Assert the checkout is clean before building
# covers: build-and-install/Build the wheel and the sdist
# covers: build-and-install/Install the wheel into a clean venv and import the submodule
# covers: build-and-install/Install the sdist into a clean venv and import the submodule
# covers: build-and-install/twine check
# covers: build-and-install/Assert examples/ ships in neither artifact (R35-AC4)
#
# ci-only: build-and-install/Install the build front-end
#   CI installs `build` and `twine` into a throwaway runner. Doing that
#   locally would mutate the developer's venv, and `build` cannot coexist
#   with the dev extra's `setuptools<76` pin anyway (see ci.yml). The gate
#   below therefore *uses* them when present and reports honestly when
#   they are not, rather than installing them.
build_and_install() {
  if ! "$PYTHON" -c 'import build, twine' 2>/dev/null; then
    echo 'error: `build` and `twine` are not installed in this' \
         'interpreter, so the artifact gates cannot run.' >&2
    echo '       Install them into a SEPARATE venv and re-run with' >&2
    echo '       PYTHON=that/venv/bin/python, or accept that these' >&2
    echo '       gates are verified in CI only.' >&2
    return 1
  fi
  # CI asserts `git status --porcelain` is empty, but it does so on a
  # fresh checkout, where "no untracked files" is free. A developer's
  # tree legitimately carries untracked local tooling that is no part of
  # the build, so failing on it would make this gate unrunnable for
  # everyone. Modified *tracked* files are the condition CI is actually
  # testing for -- they mean the artifact would not match the commit --
  # so those still fail; untracked files are reported and allowed.
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo 'error: tracked files are modified; the artifact would not' >&2
    echo '       match the commit. Commit or revert them first:' >&2
    git status --short --untracked-files=no >&2
    return 1
  fi
  local untracked
  untracked="$(git ls-files --others --exclude-standard | wc -l | tr -d ' ')"
  if [ "$untracked" != 0 ]; then
    echo "note: $untracked untracked file(s) present; CI's checkout has" \
         'none. Not a failure, but `prune` in MANIFEST.in is what keeps' \
         'them out of the sdist.'
  fi
  local work; work="$(mktemp -d)"
  "$PYTHON" -m build --outdir "$work/dist" || return 1
  "$PYTHON" -m twine check "$work"/dist/* || return 1

  local venv
  for kind in wheel sdist; do
    venv="$work/$kind-venv"
    "$PYTHON" -m venv "$venv" || return 1
    if [ "$kind" = wheel ]; then
      "$venv/bin/pip" install -q --no-cache-dir "$work"/dist/*.whl || return 1
    else
      "$venv/bin/pip" install -q --no-cache-dir --no-binary :all: \
        "$work"/dist/*.tar.gz || return 1
    fi
    # `cd /tmp` for the same reason CI does: from the repo root the
    # source tree shadows the installed package and the import proves
    # nothing.
    (cd /tmp && "$venv/bin/python" -c \
      'from async_gateway.async_gateway import request; print(request)') \
      || return 1
  done

  DIST="$work/dist" "$PYTHON" - <<'PY' || return 1
import glob
import os
import sys
import tarfile
import zipfile

dist = os.environ['DIST']
leaked = []
for wheel in glob.glob(f'{dist}/*.whl'):
    with zipfile.ZipFile(wheel) as archive:
        leaked += [n for n in archive.namelist() if 'example' in n.lower()]
for sdist in glob.glob(f'{dist}/*.tar.gz'):
    with tarfile.open(sdist) as archive:
        leaked += [n for n in archive.getnames() if 'example' in n.lower()]
if leaked:
    sys.exit(f'examples/ leaked into the artifacts: {leaked}')
print('examples/ ships in neither artifact.')
PY
  rm -r "$work"
}
if [ "$FAST" = 1 ]; then
  skip 'build + clean-venv install' '--fast'
else
  gate 'build + clean-venv install' build_and_install
fi

# ------------------------------------------------- inherently CI-only

# ci-only: changelog/Require a CHANGELOG.md change when async_gateway/ changes
#   Needs the pull request's base sha and its label set. On a local
#   branch there is no PR, so there is no base to diff and no
#   `skip-changelog` label to honour.
# ci-only: newer-python-minor/Compare python.org's stable releases against the matrix
#   Scheduled monthly and queries python.org. It gates the interpreter
#   matrix over time, not this commit, and running it locally would make
#   the entrypoint depend on the network.
#
# The interpreter MATRIX itself (3.10-3.14) is also CI-only: this script
# runs the gates once, on `$PYTHON`. A version-specific failure is
# genuinely only visible in CI, and that is the one honest gap left.

# ------------------------------------------------------------- summary

printf '\n\033[1m== summary\033[0m\n'
for name in "${PASSED[@]}";  do printf '\033[32m  PASS\033[0m  %s\n' "$name"; done
for name in "${SKIPPED[@]}"; do printf '\033[33m  SKIP\033[0m  %s\n' "$name"; done
for name in "${FAILED[@]}";  do printf '\033[31m  FAIL\033[0m  %s\n' "$name"; done

if [ ${#FAILED[@]} -gt 0 ]; then
  printf '\n\033[31m%d gate(s) failed.\033[0m\n' "${#FAILED[@]}"
  exit 1
fi
printf '\n\033[32mEvery gate run passed.\033[0m'
printf ' Matrix legs 3.10-3.14 remain CI-only.\n'

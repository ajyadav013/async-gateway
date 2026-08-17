#!/usr/bin/env python3
"""Fail when a lint or type suppression is unaccountable (R27-AC7).

A suppression is allowed. An *unaccountable* one is not, and the two
failure modes this catches are different:

* **No code.** A bare ``# noqa`` silences every finding on its line, so a
  suppression added for one reason quietly starts hiding the next,
  unrelated one. A bare ``# type: ignore`` does the same for mypy.
* **No reason.** A code without a justification records *what* was
  silenced and loses *why*, which is the half a later reader needs to
  decide whether the suppression is still true.

The justification does not have to sit *on* the suppressed line. At this
project's 79-column limit a real explanation does not fit beside the
code, and demanding it there buys a one-word alibi rather than a reason.
It is accepted either on the line, or in a comment within the
:data:`JUSTIFICATION_WINDOW` lines above that names **the same code** and
carries a ``--``. Requiring the code to be named is what keeps the
nearby-comment allowance honest: an unrelated comment two lines up
cannot launder a suppression, and one block can legitimately justify a
run of identical suppressions -- which is the shape they actually take
(a monkeypatch and its ``finally`` restore, two arms of one ``if``).

Run with no arguments to scan the default roots; pass paths to narrow it.

Exit status:
    0: every suppression names a code and has a justification.
    1: at least one does not; each is printed as ``path:line: reason``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterator, List, Sequence, Tuple

#: Where library, test and example code lives. `.claude` is deliberately
#: absent for the same reason `.flake8` excludes it: it is vendored agent
#: tooling checked in beside the library, not part of it.
DEFAULT_ROOTS: Tuple[str, ...] = ('async_gateway', 'tests', 'examples')

#: Any suppression comment, coded or not. Matched first so that a bare one
#: is *found* and then rejected, rather than simply not matching and
#: passing silently -- the failure mode a "match only the good shape"
#: regex has.
SUPPRESSION = re.compile(r'#\s*(noqa|type:\s*ignore)\b(?P<rest>[^\n]*)')

#: `# noqa: E501` or `# noqa: E501,W503`. flake8's own accepted spelling.
NOQA_CODES = re.compile(r'^\s*:\s*[A-Z]+[0-9]+(\s*,\s*[A-Z]+[0-9]+)*')

#: `# type: ignore[assignment]` or `[assignment, misc]`. mypy's spelling.
IGNORE_CODES = re.compile(r'^\s*\[[a-z-]+(\s*,\s*[a-z-]+)*\]')

#: The justification marker, matching `.claude/rules/linting-and-
#: formatting.md`'s `-- <justification>` convention.
JUSTIFIED = re.compile(r'--\s*\S')

#: How far above a suppression a justifying comment may sit. Wide enough
#: for one block to cover a short run of identical suppressions -- a
#: monkeypatch and its `finally` restore, the two arms of an `if` -- and
#: narrow enough that an unrelated comment further up cannot launder one.
JUSTIFICATION_WINDOW = 12


def justified_nearby(
    lines: Sequence[str],
    index: int,
    code: str,
) -> bool:
    """Report whether a justifying comment for ``code`` sits above.

    Args:
        lines: The file's lines, without terminators.
        index: Zero-based index of the suppressed line.
        code: The suppression exactly as written -- ``noqa: E501`` or
            ``type: ignore[assignment]``. A candidate comment must
            contain this text, so a nearby comment about something else
            cannot satisfy the check.

    Returns:
        True when some comment line within :data:`JUSTIFICATION_WINDOW`
        above ``index`` names ``code`` and carries a ``--``
        justification.
    """
    window: List[str] = []
    cursor = index - 1
    while cursor >= 0 and index - cursor <= JUSTIFICATION_WINDOW:
        stripped = lines[cursor].lstrip()
        if stripped.startswith('#'):
            window.append(stripped)
        cursor -= 1
    normalised = code.replace(' ', '')
    return any(
        normalised in line.replace(' ', '') and JUSTIFIED.search(line)
        for line in window)


def check_file(path: Path) -> Iterator[Tuple[int, str]]:
    """Yield every unaccountable suppression in one file.

    Args:
        path: The Python file to scan.

    Yields:
        ``(line number, reason)`` for each finding, one-based.
    """
    lines = path.read_text(encoding='utf-8').splitlines()
    for number, line in enumerate(lines, start=1):
        match = SUPPRESSION.search(line)
        if match is None:
            continue
        kind, rest = match.group(1), match.group('rest')
        coded = (NOQA_CODES if kind == 'noqa' else IGNORE_CODES).match(rest)
        if not coded:
            yield number, (
                f'bare `{match.group(0).strip()}` -- name the '
                f'{"rule code" if kind == "noqa" else "error category"} '
                f'it silences')
            continue
        code = f'{kind}{coded.group(0)}'
        if JUSTIFIED.search(rest):
            continue
        if justified_nearby(lines, number - 1, code):
            continue
        yield number, (
            f'`{code}` carries no `-- <why>` justification, on the line '
            f'or in a comment naming it within '
            f'{JUSTIFICATION_WINDOW} lines above')


def main(argv: Sequence[str]) -> int:
    """Scan the given paths (or the default roots) and report findings.

    Args:
        argv: Command-line arguments after the program name. Each is a
            file or directory to scan; empty means :data:`DEFAULT_ROOTS`.

    Returns:
        0 when every suppression is accountable, 1 otherwise.
    """
    roots = [Path(a) for a in argv] or [Path(r) for r in DEFAULT_ROOTS]
    findings = 0
    for root in roots:
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(root.rglob('*.py'))
        for path in paths:
            for number, reason in check_file(path):
                print(f'{path}:{number}: {reason}')
                findings += 1
    if findings:
        print(
            f'\n{findings} unjustified suppression(s). Every `# noqa` must '
            f'name its rule code and every `# type: ignore` its error '
            f'category, with a `-- <why>` justification on the line or in '
            f'a comment naming that code just above it.',
            file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))

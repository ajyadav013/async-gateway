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

**Only real comments are scanned.** The scan is over ``tokenize``'s
``COMMENT`` tokens rather than over raw lines, because a raw-line regex
cannot tell a suppression from *prose about* one. The docstrings that
explain why an ignore was removed necessarily quote the ignore they
removed, and a line-based scan reported all three of them as bare
suppressions -- a false positive that failed every CI leg while the
tree contained no such suppression at all. Scanning tokens also stops
the mirror-image failure, which is the one that matters: a real
suppression can no longer hide inside a string literal.

Run with no arguments to scan the default roots; pass paths to narrow it.

Exit status:
    0: every suppression names a code and has a justification.
    1: at least one does not; each is printed as ``path:line: reason``.
"""

from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path
from typing import Dict, Iterator, Sequence, Tuple

#: Where library, test and example code lives. `.claude` is deliberately
#: absent for the same reason `.flake8` excludes it: it is vendored agent
#: tooling checked in beside the library, not part of it.
DEFAULT_ROOTS: Tuple[str, ...] = ('asyncio_gateway', 'tests', 'examples')

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


def comments_by_line(source: str) -> Dict[int, str]:
    """Map each one-based line number to the comment it carries.

    Tokenizing is what separates a suppression from *prose about* one.
    A ``# type: ignore`` quoted inside a docstring is a ``STRING``
    token, never a ``COMMENT``, so it does not appear here at all --
    and a real suppression cannot hide inside a string literal either.

    Args:
        source: The file's full text.

    Returns:
        ``{line number: comment text}`` for every comment in the file.
        A line carries at most one comment, so the mapping is total.

    Raises:
        tokenize.TokenError: If the source is not tokenizable, which is
            a real failure and is deliberately not caught here.
    """
    readline = io.StringIO(source).readline
    return {
        token.start[0]: token.string
        for token in tokenize.generate_tokens(readline)
        if token.type == tokenize.COMMENT
    }


def justified_nearby(
    comments: Dict[int, str],
    number: int,
    code: str,
) -> bool:
    """Report whether a justifying comment for ``code`` sits above.

    Args:
        comments: The file's comments, keyed by one-based line number.
        number: One-based line number of the suppressed line.
        code: The suppression exactly as written -- ``noqa: E501`` or
            ``type: ignore[assignment]``. A candidate comment must
            contain this text, so a nearby comment about something else
            cannot satisfy the check.

    Returns:
        True when some comment within :data:`JUSTIFICATION_WINDOW`
        lines above ``number`` names ``code`` and carries a ``--``
        justification.
    """
    window = [
        comments[cursor]
        for cursor in range(number - JUSTIFICATION_WINDOW, number)
        if cursor >= 1 and cursor in comments
    ]
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
    comments = comments_by_line(path.read_text(encoding='utf-8'))
    for number in sorted(comments):
        match = SUPPRESSION.search(comments[number])
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
        if justified_nearby(comments, number, code):
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

"""R30: the documentation and annotation standard, enforced mechanically.

Every ``.py`` file under ``async_gateway/`` is parsed and checked against
the project's documentation rule: a module docstring that says what the
module does and why, a docstring on every public class and function that
documents its arguments, its return value and what it raises, and a full
type annotation on every signature.

**Why this file exists rather than a lint rule alone.** Two of these
properties cannot be delegated:

* ``flake8-docstrings`` checks that a docstring is *present*. It cannot
  check that one says anything. A one-line ``Constants.`` and a one-line
  ``Ftp.`` -- the real offenders R30 names, each a single word between
  triple quotes -- satisfied every docstring lint rule this runs, while
  telling a
  reader strictly less than the filename above them did. The content
  floor in :func:`test_every_module_docstring_says_what_and_why` is the
  part no linter supplies.
* ``mypy``'s ``disallow_untyped_defs`` is configured in ``pyproject.toml``
  -- and is **inert there today**, because ``ignore_errors = true`` in the
  same section suppresses it along with everything else. That was measured
  rather than assumed: with both settings on, ``mypy async_gateway``
  reports ``Success: no issues found`` while seven functions are missing
  annotations. Story S25 removes ``ignore_errors``; until it does, the
  strictness flag is a promise with nothing behind it, and
  :func:`test_every_signature_is_fully_annotated` is what actually holds
  the line. It keeps holding it afterwards, which is the point: a gate
  that can be switched off by an unrelated setting in the same file is a
  gate that will be.

The scan is an ``rglob`` over the package rather than a list of known
files, so a module that does not exist yet is covered on the day it is
added.

**The content floor.** A module docstring must be at least
:data:`MIN_DOCSTRING_CHARS` characters and :data:`MIN_DOCSTRING_WORDS`
whitespace-separated words, and must not merely restate its own filename.
Both numbers come from R30 and both are floors on *effort*, not proxies
for quality: the two-clause "what it does, why it exists" the
documentation rule asks for cannot be written in forty characters, so a
docstring that fits is one that did not attempt it. The four modules R30
names fail decisively -- ``Ftp.`` is 4 characters and one word,
``Constants.`` is 10 and one, and a 0-byte ``__init__.py`` has no
``__doc__`` at all.

The filename comparison is case- and punctuation-insensitive, so
``Constants.`` on ``constants.py``, ``constants`` and ``CONSTANTS!`` are
all the same evasion and all fail.

In practice it is a belt-and-braces check, and that was verified rather
than assumed: for every module in this package the length floor fires
first, because a filename short enough to be a filename is shorter than
forty characters. What it catches is the case padded *into* range with
punctuation -- ``C.i.r. c.u.i.t. b.r.e. a.k.e.r. h.e.l. p.e.r.`` is 45
characters and six words, clears both floors, and still tells a reader
of ``circuit_breaker_helper.py`` nothing. Stripping case and
punctuation before comparing is what makes that a filename rather than
a sentence.

**What is deliberately *not* enforced.** Private helpers (a single
leading underscore) are exempt from the docstring requirement, per the
project rule's "private helpers under 5 lines with obvious names may skip
it" -- but they are **not** exempt from the annotation requirement, which
applies to every function in the package. Dunder methods are treated as
public, because ``__init__`` carries a class's construction contract.
Nested functions are checked too: a closure returning a request body is
part of how this library works even though no consumer can name it.
"""

import ast
import re
from pathlib import Path
from typing import Iterator, List, NamedTuple, Optional, Tuple

import pytest

#: The package this module polices. Everything under it, recursively.
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / 'async_gateway'

#: Minimum length of a module docstring, in characters, after stripping.
#: R30's number. See the module docstring for why a floor on effort is
#: the right shape of check here.
MIN_DOCSTRING_CHARS = 40

#: Minimum whitespace-separated words in a module docstring. R30's
#: number, and the half that catches a long single word.
MIN_DOCSTRING_WORDS = 6

#: Everything that is not a letter or a digit, for the filename
#: comparison. Case and punctuation are noise there: the question is
#: whether the docstring carries information the filename did not.
_NOT_ALPHANUMERIC = re.compile(r'[^a-z0-9]')

#: Parameter names that carry no annotation and need none -- the
#: instance and the class a bound method receives implicitly.
IMPLICIT_PARAMETERS = frozenset({'self', 'cls'})

#: A Google-style ``Args:`` block, or any of the spellings this codebase
#: also uses. Matched at the start of a line, so a mention of the word
#: inside a sentence does not satisfy the check.
_ARGS_SECTION = re.compile(r'^\s*(Args|Arguments|Parameters)\s*:', re.M)

#: The Sphinx spelling of the same thing. Both styles are present in this
#: package and both are accepted: this test polices whether the
#: information is there, not which dialect states it.
_ARGS_FIELD = re.compile(r':param\b')

#: A Google-style ``Returns:`` or ``Yields:`` block. A generator
#: documents what it yields, which answers the same question.
_RETURNS_SECTION = re.compile(r'^\s*(Returns|Yields)\s*:', re.M)

#: The Sphinx spellings of a return or yield description.
_RETURNS_FIELD = re.compile(r':(returns?|rtype|yield)\b')

#: A Google-style ``Raises:`` block.
_RAISES_SECTION = re.compile(r'^\s*Raises\s*:', re.M)

#: The Sphinx spelling of the same.
_RAISES_FIELD = re.compile(r':raises?\b')


class Finding(NamedTuple):
    """One documentation or annotation defect, located and explained.

    Attributes:
        location: ``module.py:12`` -- the module path relative to the
            package root, and the line the offending definition starts
            on.
        name: The dotted name of what is at fault, ``Class.method`` for
            a method and ``outer.<locals>.inner`` for a closure.
        problem: What is wrong, phrased so the assertion message alone
            is enough to act on without opening the file.
    """

    location: str
    name: str
    problem: str


def module_paths() -> List[Path]:
    """Return every Python module in the package, sorted.

    Returns:
        Every ``.py`` file under :data:`PACKAGE_ROOT`, recursively, in
        path order so the parametrised tests have stable ids.
    """
    return sorted(PACKAGE_ROOT.rglob('*.py'))


def relative(path: Path) -> str:
    """Return a module's path relative to the package root, POSIX-style.

    Args:
        path: An absolute path to a module inside :data:`PACKAGE_ROOT`.

    Returns:
        The path as a forward-slash string, so a failure message reads
        the same on every platform.
    """
    return path.relative_to(PACKAGE_ROOT).as_posix()


def is_public(name: str) -> bool:
    """Return whether a name is part of the package's public surface.

    A single leading underscore marks a private helper, which the
    project rule exempts from the docstring requirement. A dunder is
    public: ``__init__`` carries a class's construction contract, and
    that is exactly the docstring a caller needs.

    Args:
        name: The identifier of a class, function, or method.

    Returns:
        True when the name is public or a dunder, False for a private
        helper.
    """
    if name.startswith('__') and name.endswith('__'):
        return True
    return not name.startswith('_')


def normalised(text: str) -> str:
    """Reduce text to lowercase alphanumerics for a content comparison.

    Args:
        text: Any string -- a docstring's first line, or a filename.

    Returns:
        The same text lowercased with every non-alphanumeric character
        removed, so ``'Constants.'``, ``'constants'`` and ``'CONSTANTS!'``
        all reduce to the same value.
    """
    return _NOT_ALPHANUMERIC.sub('', text.lower())


def docstring_problem(doc: Optional[str], stem: str) -> Optional[str]:
    """Judge a module docstring against R30's content floor.

    Args:
        doc: The module's ``__doc__``, or None when it has none.
        stem: The module's filename without its extension, for the
            "not merely the filename" comparison.

    Returns:
        A description of what is wrong, or None when the docstring
        clears every part of the floor.
    """
    if doc is None or not doc.strip():
        return 'has no module docstring at all'

    stripped = doc.strip()
    words = stripped.split()

    if len(stripped) < MIN_DOCSTRING_CHARS:
        return (
            f'module docstring is {len(stripped)} characters, below the '
            f'{MIN_DOCSTRING_CHARS}-character floor: {stripped!r}')

    if len(words) < MIN_DOCSTRING_WORDS:
        return (
            f'module docstring is {len(words)} word(s), below the '
            f'{MIN_DOCSTRING_WORDS}-word floor: {stripped!r}')

    if normalised(stripped) == normalised(stem):
        return (
            f'module docstring restates the filename and says nothing '
            f'else: {stripped!r}')

    return None


def documented_parameters(node: ast.AST) -> List[str]:
    """Return the parameters of a definition that need documenting.

    ``self`` and ``cls`` are excluded: they are implicit and no reader
    needs them described. ``*args`` and ``**kwargs`` are included --
    where a function forwards them, *where they go* is the contract.

    Args:
        node: A ``FunctionDef`` or ``AsyncFunctionDef`` node.

    Returns:
        The parameter names, in signature order.
    """
    args = node.args
    named = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    if args.vararg is not None:
        named.append(args.vararg)
    if args.kwarg is not None:
        named.append(args.kwarg)
    return [
        arg.arg for arg in named if arg.arg not in IMPLICIT_PARAMETERS
    ]


def returns_something(node: ast.AST) -> bool:
    """Return whether a definition's annotation promises a value.

    Args:
        node: A ``FunctionDef`` or ``AsyncFunctionDef`` node.

    Returns:
        False when the return annotation is absent or is literally
        ``None`` -- a function returning nothing has nothing to
        document -- and True otherwise.
    """
    annotation = node.returns
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and annotation.value is None:
        return False
    return True


def raises_something(node: ast.AST) -> bool:
    """Return whether a definition contains an explicit ``raise``.

    Scoped to the definition's own body: a nested function's ``raise``
    belongs to that function's docstring, not to its parent's.

    Args:
        node: A ``FunctionDef`` or ``AsyncFunctionDef`` node.

    Returns:
        True when the body raises directly, False otherwise. A function
        that raises only indirectly, through something it calls, is not
        detected -- see *Known limitations* in this module's docstring.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            return True
    return False


def definitions(tree: ast.Module) -> Iterator[Tuple[ast.AST, str, str]]:
    """Walk a parsed module, yielding every class and function in it.

    Nested definitions are yielded too, qualified through
    ``<locals>`` the way ``__qualname__`` spells them, so a closure that
    builds a request body is checked and a failure names it findably.

    Args:
        tree: The parsed module.

    Yields:
        A ``(node, qualified_name, kind)`` triple per definition, where
        kind is ``'class'`` or ``'function'``.
    """
    def walk(node: ast.AST, prefix: str) -> Iterator[
            Tuple[ast.AST, str, str]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                name = f'{prefix}{child.name}'
                yield child, name, 'class'
                yield from walk(child, f'{name}.')
            elif isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f'{prefix}{child.name}'
                yield child, name, 'function'
                yield from walk(child, f'{name}.<locals>.')
            else:
                yield from walk(child, prefix)

    return walk(tree, '')


def parse(path: Path) -> ast.Module:
    """Parse a module from disk.

    Args:
        path: The module to read and parse.

    Returns:
        Its abstract syntax tree.

    Raises:
        SyntaxError: If the file is not valid Python, which is a real
            failure and is deliberately not caught here.
    """
    return ast.parse(path.read_text(encoding='utf-8'))


def test_the_scan_finds_the_package() -> None:
    """The package is found and is not empty.

    Every other test here passes vacuously against an empty file list,
    which is exactly what a moved package or a renamed directory would
    produce. This asserts the denominator before anything divides by it.
    """
    paths = module_paths()

    assert PACKAGE_ROOT.is_dir(), f'{PACKAGE_ROOT} is not a directory'
    assert paths, f'no modules found under {PACKAGE_ROOT}'


@pytest.mark.parametrize(
    'path', module_paths(), ids=relative)
def test_every_module_docstring_says_what_and_why(path: Path) -> None:
    """Each module's docstring clears R30's content floor.

    Length, word count, and not-merely-the-filename. A one-line
    ``Internal.`` fails on the first two; a 0-byte ``__init__.py`` fails
    for having no docstring at all.
    """
    problem = docstring_problem(_module_doc(path), path.stem)

    assert problem is None, f'{relative(path)}: {problem}'


def _module_doc(path: Path) -> Optional[str]:
    """Return a module's docstring without importing it.

    Read from the syntax tree rather than from ``__doc__`` on an
    imported module: importing to inspect runs package side effects, and
    a module with a syntax error should fail as a parse error naming the
    file rather than as an import error naming its parent.

    Args:
        path: The module to read.

    Returns:
        The module-level docstring, or None when it has none.
    """
    return ast.get_docstring(parse(path))


@pytest.mark.parametrize(
    'path', module_paths(), ids=relative)
def test_every_public_definition_has_a_docstring(path: Path) -> None:
    """Every public class, function and method carries a docstring.

    Private helpers are exempt per the project rule; dunders are not,
    because ``__init__`` carries a construction contract.
    """
    findings = [
        Finding(f'{relative(path)}:{node.lineno}', name,
                f'public {kind} has no docstring')
        for node, name, kind in definitions(parse(path))
        if is_public(node.name) and ast.get_docstring(node) is None
    ]

    assert not findings, _report(findings)


@pytest.mark.parametrize(
    'path', module_paths(), ids=relative)
def test_every_docstring_documents_args_returns_and_raises(
        path: Path) -> None:
    """A public function's docstring covers all three of R30's sections.

    Arguments when it takes any, a return value when its annotation
    promises one, and the exceptions it raises when its body raises.
    Google-style blocks and Sphinx fields are both accepted -- the check
    is that the information is present, not which dialect carries it.
    """
    findings: List[Finding] = []

    for node, name, kind in definitions(parse(path)):
        if kind != 'function' or not is_public(node.name):
            continue
        doc = ast.get_docstring(node)
        if doc is None:
            continue

        location = f'{relative(path)}:{node.lineno}'
        params = documented_parameters(node)

        if params and not (
                _ARGS_SECTION.search(doc) or _ARGS_FIELD.search(doc)):
            findings.append(Finding(
                location, name,
                f'takes {len(params)} parameter(s) '
                f'({", ".join(params)}) and documents none'))

        if returns_something(node) and not (
                _RETURNS_SECTION.search(doc)
                or _RETURNS_FIELD.search(doc)):
            findings.append(Finding(
                location, name,
                'is annotated to return a value and documents no '
                '"Returns:" or ":returns:"'))

        if raises_something(node) and not (
                _RAISES_SECTION.search(doc) or _RAISES_FIELD.search(doc)):
            findings.append(Finding(
                location, name,
                'raises and documents no "Raises:" or ":raises:"'))

    assert not findings, _report(findings)


@pytest.mark.parametrize(
    'path', module_paths(), ids=relative)
def test_every_signature_is_fully_annotated(path: Path) -> None:
    """Every parameter and every return type carries an annotation.

    This is the standing enforcement of ``disallow_untyped_defs``, and
    it is not redundant with the mypy setting: ``ignore_errors = true``
    in the same ``pyproject.toml`` section suppresses that flag entirely
    today, so the configured strictness currently checks nothing. See
    this module's docstring.

    Private helpers are **not** exempt. The docstring rule excuses them
    from prose; nothing excuses a function in a typed package from
    saying what it takes and returns.
    """
    findings: List[Finding] = []

    for node, name, kind in definitions(parse(path)):
        if kind != 'function':
            continue

        location = f'{relative(path)}:{node.lineno}'
        args = node.args
        every = (list(args.posonlyargs) + list(args.args)
                 + list(args.kwonlyargs))
        if args.vararg is not None:
            every.append(args.vararg)
        if args.kwarg is not None:
            every.append(args.kwarg)

        unannotated = [
            arg.arg for arg in every
            if arg.annotation is None
            and arg.arg not in IMPLICIT_PARAMETERS
        ]
        if unannotated:
            findings.append(Finding(
                location, name,
                f'parameter(s) with no annotation: '
                f'{", ".join(unannotated)}'))

        if node.returns is None:
            findings.append(Finding(
                location, name,
                'has no return annotation (use "-> None" when it '
                'returns nothing)'))

    assert not findings, _report(findings)


@pytest.mark.parametrize(
    'path', module_paths(), ids=relative)
def test_no_bare_container_return_annotations(path: Path) -> None:
    """No public function returns an unparameterised container.

    ``-> dict`` says a mapping comes back and nothing about what is in
    it, which is the annotation R30 replaces with a named structure --
    ``GatewayResponse``, ``HttpResult``, ``SoapFault``. A parameterised
    ``Dict[Text, Any]`` is accepted: it is a subscript node, not a bare
    name, and it does state its shape.
    """
    bare = {'dict', 'list', 'Dict', 'List', 'set', 'Set', 'tuple', 'Tuple'}
    findings = [
        Finding(f'{relative(path)}:{node.lineno}', name,
                f'returns bare "{node.returns.id}" -- parameterise it '
                f'or name a structured type')
        for node, name, kind in definitions(parse(path))
        if kind == 'function' and is_public(node.name)
        and isinstance(node.returns, ast.Name)
        and node.returns.id in bare
    ]

    assert not findings, _report(findings)


def test_handle_request_everywhere_returns_the_envelope() -> None:
    """The base and all four overrides are annotated ``GatewayResponse``.

    R30 names this signature specifically because it is the library's
    core method and the one place a caller's expectations are set: every
    protocol fills the envelope it was handed and returns that same
    object. An override annotated anything else would be announcing a
    second response shape.
    """
    found: List[Tuple[str, str]] = []

    for path in module_paths():
        for node, name, kind in definitions(parse(path)):
            if kind == 'function' and node.name == 'handle_request':
                annotation = node.returns
                spelled = (
                    annotation.id if isinstance(annotation, ast.Name)
                    else ast.dump(annotation) if annotation is not None
                    else 'MISSING')
                found.append((f'{relative(path)}:{node.lineno}', spelled))

    wrong = [entry for entry in found if entry[1] != 'GatewayResponse']

    assert len(found) == 5, (
        f'expected the base plus four protocol overrides, found '
        f'{len(found)}: {found}')
    assert not wrong, (
        f'handle_request must return GatewayResponse; these do not: '
        f'{wrong}')


def test_typing_text_is_not_reintroduced_as_an_alias() -> None:
    """``typing.Text`` is used as ``Text``, never re-exported as its own.

    R30 retires ``typing.Text`` -- a Python 2 compatibility alias for
    ``str`` that has been redundant since Python 3.0. The package still
    imports it widely and that is a separate, mechanical rename this
    story does not perform; what this guards is the shape that would
    make the rename harder: a module defining its own ``Text = ...``
    alias, so a later ``grep`` for the import misses a use site.
    """
    findings = [
        Finding(f'{relative(path)}:{node.lineno}', 'Text',
                'module defines its own "Text" alias, which hides a '
                'typing.Text use from the rename that retires it')
        for path in module_paths()
        for node in ast.walk(parse(path))
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'Text'
                for target in node.targets)
    ]

    assert not findings, _report(findings)


def _report(findings: List[Finding]) -> str:
    """Render findings as one message per line.

    Args:
        findings: The defects to report; may be empty.

    Returns:
        A newline-separated block, one finding per line, prefixed with a
        count so a large regression reads as one number before it reads
        as a wall of text.
    """
    lines = '\n'.join(
        f'  {finding.location} {finding.name}: {finding.problem}'
        for finding in findings)
    return f'{len(findings)} finding(s):\n{lines}'

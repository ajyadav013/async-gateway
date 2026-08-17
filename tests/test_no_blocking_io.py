"""R20: the mechanical ban on blocking filesystem calls in ``async def``.

Every ``.py`` file under ``async_gateway/`` is parsed and every
``async def`` in it is searched for a synchronous filesystem call
*written in one of the forms below*. A hit fails this test, so those
forms cannot be reintroduced -- not in the four sites Step 12 converted,
and not in a module that does not exist yet, because the scan is a
``rglob`` over the package rather than a list of known files.

That is narrower than "no blocking filesystem I/O anywhere on an async
path", and the gap is not hypothetical: the package contains two
blocking calls this scan cannot see and a third it is deliberately not
failing on yet. All three are named under *Known limitations* below and
in :data:`NOT_YET_REWRITTEN`. A green run of this module means the banned
*forms* are absent, not that the property is held.

What is banned inside an ``async def``:

* a bare ``open(...)`` -- ``aiofiles.open`` is an attribute call and is
  the intended replacement, so it is not matched;
* any call whose dotted name begins with one of
  :data:`BLOCKING_MODULE_PREFIXES`, except the handful of pure
  string-manipulation helpers in :data:`PURE_PATH_HELPERS`. The ban is
  by prefix rather than by a list of known-blocking names, so an
  ``os.<something>`` nobody thought of is refused rather than missed.
  ``builtins.`` and ``io.`` are there because they are the two
  qualified spellings of the builtin the first rule bans unqualified,
  and ``tempfile.`` and ``glob.`` because both reach the disk on every
  call they offer. ``ssl.`` is there because building a TLS context is
  filesystem work wearing a cryptography name:
  ``ssl.create_default_context()`` reads the whole system CA bundle
  (194 certificates, 6-15 ms depending on the page cache, measured on
  this machine) and the ``load_*`` calls read the files they are handed.
  ``aiofiles.os.remove`` resolves to a dotted name
  beginning ``aiofiles.``, so the async form passes;
* a filesystem method called on a freshly constructed ``Path(...)`` --
  ``Path(p).read_bytes()`` and friends, in any spelling of the
  constructor: the match is on its *trailing* name, so ``Path(p)``,
  ``pathlib.Path(p)`` and ``pl.Path(p)`` are all caught. ``PurePath`` is
  deliberately absent from :data:`PATHLIB_CONSTRUCTORS`: it is the
  pure-string half of ``pathlib`` and has no filesystem methods to call,
  which is why ``request_helper`` uses it to name an upload part.

**Nested plain ``def``s are searched too, and that is deliberate.** A
synchronous helper defined inside a coroutine is, by default, called by
that coroutine -- so its ``open()`` blocks the same loop, and letting it
through would leave a one-line evasion of this whole check. The escape
hatch for the legitimate case (a blocking function handed to
``run_in_executor``) is to define it at module level. That placement is
the *intended contract* -- a module-level blocking helper is supposed to
be reached only through an executor call a reader can see -- and it is a
contract this scan cannot enforce, because any plain ``def`` not
lexically inside an ``async def`` is exactly what it does not search: a
module-level one, a method on a class, and one nested in an ``if`` at
module level are all equally unsearched. See the third limitation below
for a place where the package breaks it.

Known limitations, stated rather than hidden. The scan resolves dotted
names syntactically, so it does not detect a blocking call reached
through an alias (``from os import remove``), through reflection
(``getattr(os, 'remove')(p)``), or through a variable holding an already
constructed path object. The ``Path(...)`` rule also matches only the
methods in :data:`PATHLIB_IO_METHODS`, so ``Path(p).chmod()``,
``.samefile()``, ``.resolve()`` and ``.absolute()`` slip through.

The structural one, which no addition to the tables above can repair: a
filesystem method invoked on a **bare variable** cannot be caught here.
The scan sees ``x.load_cert_chain(a, b)`` and has no way to know what
``x`` is without type inference, so it matches only calls rooted in a
module name or in a constructor call it can read in place. That is
exactly the shape of ``async_gateway/helpers/internal/filters_helper.py``
line 59, where ``load_cert_chain`` reads two files off a local
``ssl_context`` inside ``get_ssl_config`` -- blocking filesystem I/O on
an async path, reached inside ``failsafe.run`` on HTTP and outside it on
FTP, that this scan reports clean. It is raised as ticket **AGW-36** and
owned by story **S17**, which rewrites ``get_ssl_config``. The
``ssl.create_default_context`` call one line above it *is* a form this
scan catches, and is the single entry in :data:`NOT_YET_REWRITTEN`.

The third one is the escape hatch above, used without its executor: a
**module-level plain** ``def`` that performs blocking I/O and is called
from a coroutine is never searched, so the blocking call is invisible
here no matter which form it is written in.
``async_gateway/logic/ftp_client.py`` is a live instance -- line 145,
``ssl.create_default_context()`` inside the module-level
``tls_context_for``, called from the coroutine ``_tls_value`` and
awaited on the request path. It is the same CA-bundle read the ``ssl.``
entry above describes, on an async path, and this scan reports it
clean. It is raised as ticket **AGW-37**; its owner is not settled and
is not S14. Adding it to :data:`NOT_YET_REWRITTEN` would be worse than
leaving it out -- the scan never produces an offence for that site, so
the entry would suppress nothing and would falsely suggest the call is
being tracked by this check. Closing this gap needs call-graph
following, which is real new machinery and not something a table here
can supply.

This module also holds the behavioural proof for the one converted site
outside ``request_helper`` -- ``delete_local_file_path`` -- which has no
test module of its own.
"""

import ast
import importlib
import mimetypes
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from async_gateway.helpers.internal import request_helper
from async_gateway.utils.http_file_config import delete_local_file_path

#: Builtins that open a file synchronously when called bare.
BANNED_BUILTINS = frozenset({'open'})

#: Any call whose dotted name starts with one of these is refused.
BLOCKING_MODULE_PREFIXES = (
    'builtins.',
    'glob.',
    'io.',
    'os.',
    'shutil.',
    'ssl.',
    'tempfile.',
)

#: The one blocking call in the package this ban does not fail on yet,
#: keyed as ``(package-relative module path, enclosing coroutine name,
#: call spelling)``.
#:
#: ``filters_helper.get_ssl_config`` calls
#: ``ssl.create_default_context(ssl.Purpose.SERVER_AUTH)``, which reads
#: the system CA bundle off disk, and both transports reach it. On HTTP
#: it runs inside ``make_http_request`` -- that is, inside
#: ``failsafe.run``, on every attempt of every certificate-configured
#: request. On FTP it runs at ``logic/ftp_client.py`` line 353, in
#: ``_tls_value``, which is awaited at line 291 and is *outside*
#: ``failsafe.run`` -- FTP's only ``failsafe.run`` calls are at lines 391
#: and 397 -- so it runs once per request rather than once per attempt.
#: It is ticket **AGW-36** and it belongs to story **S17**, which
#: rewrites ``get_ssl_config`` for R23 and must cover both of those
#: call sites. S14 owns this scan and not that function, and a scan its
#: own package fails is a scan that gets deleted rather than obeyed --
#: so the call is contained here instead.
#:
#: All three parts of the key are load-bearing. A ``(module, function)``
#: key would excuse *every* banned call in ``get_ssl_config``, so an
#: ``os.remove`` written into that function would be reported clean;
#: naming the call as well means exactly one spelling is excused, in
#: exactly one function of one module, and everything else in that
#: function is still an offence.
#:
#: **S17 must delete this allowance** in the change that rewrites
#: ``get_ssl_config``.
#: :func:`test_the_allowance_holds_exactly_one_entry` is the test that
#: fails if it does not, and
#: :func:`test_the_allowance_is_a_pinhole_not_a_mute_switch` is the test
#: that fails if anyone widens it to another call, another function or
#: another file.
NOT_YET_REWRITTEN = frozenset({
    (
        'helpers/internal/filters_helper.py',
        'get_ssl_config',
        'ssl.create_default_context',
    ),
})

#: The exceptions to :data:`BLOCKING_MODULE_PREFIXES`: these compute on
#: path *strings* and never touch a filesystem, so they are free to call
#: from a coroutine.
PURE_PATH_HELPERS = frozenset({
    'os.fspath',
    'os.path.basename',
    'os.path.dirname',
    'os.path.join',
    'os.path.normpath',
    'os.path.split',
    'os.path.splitext',
})

#: ``pathlib`` constructors whose instances perform real I/O.
PATHLIB_CONSTRUCTORS = frozenset({'Path', 'PosixPath', 'WindowsPath'})

#: The ``pathlib`` methods that hit the disk.
PATHLIB_IO_METHODS = frozenset({
    'exists',
    'glob',
    'iterdir',
    'mkdir',
    'open',
    'read_bytes',
    'read_text',
    'rename',
    'replace',
    'rglob',
    'rmdir',
    'stat',
    'touch',
    'unlink',
    'write_bytes',
    'write_text',
})


def _dotted_name(node: ast.expr) -> str:
    """Render an attribute chain as its dotted source spelling.

    Args:
        node: The ``func`` expression of a call.

    Returns:
        ``'os.path.exists'`` for ``os.path.exists``, or ``''`` when the
        chain is not rooted in a plain name (``f().g()``, ``a[0].b()``).
    """
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ''
    parts.append(current.id)
    return '.'.join(reversed(parts))


def _constructed_type_name(func: ast.expr) -> str:
    """Return the trailing name of whatever a call constructs.

    Matching the *trailing* name is what makes the ``Path(...)`` rule hold
    for every spelling of the constructor. Requiring a bare
    :class:`ast.Name` matched ``Path(p)`` and silently missed
    ``pathlib.Path(p)`` and ``pl.Path(p)``, which are the same call.

    Args:
        func: The ``func`` expression of the receiver call.

    Returns:
        ``'Path'`` for ``Path``, ``pathlib.Path`` and ``pl.Path``, or
        ``''`` when the callee is neither a name nor an attribute.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ''


def _banned_call_name(node: ast.Call) -> str:
    """Return the name of the blocking call ``node`` makes, if any.

    Args:
        node: A call node found inside an ``async def``.

    Returns:
        The offending call's source spelling, or ``''`` when the call is
        permitted on an async path.
    """
    if isinstance(node.func, ast.Name):
        return node.func.id if node.func.id in BANNED_BUILTINS else ''
    if not isinstance(node.func, ast.Attribute):
        return ''

    dotted = _dotted_name(node.func)
    if dotted.startswith(BLOCKING_MODULE_PREFIXES):
        return '' if dotted in PURE_PATH_HELPERS else dotted

    receiver = node.func.value
    if not (
        node.func.attr in PATHLIB_IO_METHODS
        and isinstance(receiver, ast.Call)
    ):
        return ''
    constructor = _constructed_type_name(receiver.func)
    if constructor in PATHLIB_CONSTRUCTORS:
        return f'{constructor}(...).{node.func.attr}'
    return ''


class Offence(NamedTuple):
    """One banned call, and where the scan found it.

    Attributes:
        line: The line the call is written on.
        call: Its source spelling, e.g. ``'os.remove'``.
        function: The name of the innermost ``async def`` containing it.
            Carried, together with ``call``, so that
            :data:`NOT_YET_REWRITTEN` can name one call in one function
            rather than silencing a whole function or a whole module.
    """

    line: int
    call: str
    function: str


def _offences_in(node: ast.AST, enclosing: str) -> Iterator[Offence]:
    """Walk ``node``, reporting banned calls made under an ``async def``.

    Args:
        node: The subtree to walk.
        enclosing: The name of the innermost ``async def`` this subtree
            sits inside, or ``''`` when it sits inside none. A nested
            plain ``def`` does not clear it -- a synchronous helper
            defined in a coroutine blocks that coroutine's loop -- while
            a nested ``async def`` replaces it, so every offence is
            attributed to the coroutine that actually contains it and no
            offence is reported twice.

    Yields:
        One :class:`Offence` per banned call.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.AsyncFunctionDef):
            yield from _offences_in(child, child.name)
            continue
        if enclosing and isinstance(child, ast.Call):
            name = _banned_call_name(child)
            if name:
                yield Offence(child.lineno, name, enclosing)
        yield from _offences_in(child, enclosing)


def _scan(source: str) -> list[Offence]:
    """Parse ``source`` and return its offences, sorted and de-duplicated.

    Args:
        source: The module's source text.

    Returns:
        The offences, ordered by line. Two banned calls written on one
        line with the same spelling are one defect and one finding.
    """
    return sorted(set(_offences_in(ast.parse(source), '')))


def blocking_calls_in_async_defs(source: str, module: str) -> list[str]:
    """Find every synchronous filesystem call inside an ``async def``.

    Offences whose ``(module, function, call)`` triple is in
    :data:`NOT_YET_REWRITTEN` are dropped and nothing else is: another
    banned call in the same function, the same call in another function
    of the same module, and either of those in another module entirely,
    are all still offences.

    Args:
        source: The module's source text.
        module: The module's package-relative POSIX path, which is both
            how an offence is named and the first part of an allowance
            key.

    Returns:
        ``'<module>:<line>: <call> (in <function>)'`` for each offence
        left after the allowance, sorted by line. The call and the
        function are both named because they are the other two parts of
        an allowance key.
    """
    return [
        f'{module}:{o.line}: {o.call} (in {o.function})'
        for o in _scan(source)
        if (module, o.function, o.call) not in NOT_YET_REWRITTEN
    ]


def test_no_blocking_filesystem_call_sits_inside_any_async_def() -> None:
    """R20's grep criterion over the whole package, as a CI check.

    Four sites blocked the event loop: the multipart response file, the
    per-chunk streamed download, the upload's synchronous file handle,
    and ``delete_local_file_path``'s ``os.remove``. The ban is stated
    over the package rather than over those four, because the next one
    would otherwise be written the same way.

    It passes *through* :data:`NOT_YET_REWRITTEN`, which excuses one
    named ``(module, function, call)`` triple and nothing else -- see
    that constant for the ticket, the owner, and the two tests that keep
    it from growing.

    An empty ``offenders`` is the pass condition and is also what a scan
    of *nothing* produces, so the file list is materialised and asserted
    first. Pointed at a directory that does not exist, the assertion on
    ``offenders`` alone reported the package clean.
    """
    package = Path(__file__).resolve().parents[1] / 'async_gateway'
    scanned = sorted(package.rglob('*.py'))

    offenders = [
        offence
        for path in scanned
        for offence in blocking_calls_in_async_defs(
            path.read_text(encoding='utf-8'),
            path.relative_to(package).as_posix())
    ]

    assert scanned, f'no modules scanned under {package}'
    assert offenders == []


def test_the_allowance_holds_exactly_one_entry() -> None:
    """The containment: the allowance may shrink to nothing, never grow.

    An allowance with no test on its own size is a mute switch waiting to
    be used, because the cheapest way past a failing scan is to add a
    second line to the list that made it pass the first time. Pinning the
    exact entry means the next blocking call has to be argued for in
    review -- and it means story S17 deleting ``get_ssl_config``'s
    blocking build must delete this line too, or fail here.
    """
    assert len(NOT_YET_REWRITTEN) == 1
    assert NOT_YET_REWRITTEN == frozenset({
        (
            'helpers/internal/filters_helper.py',
            'get_ssl_config',
            'ssl.create_default_context',
        ),
    })


def test_the_allowance_is_a_pinhole_not_a_mute_switch() -> None:
    """The excused call is still an offence anywhere else.

    All three parts of the key are load-bearing and all three are
    asserted: a *different* banned call inside the allowed function is
    reported, the same ``ssl.create_default_context`` in another
    function of the allowed module is reported, and so is the one in
    ``get_ssl_config`` when ``get_ssl_config`` is written in some other
    module.

    The first of those is the axis a ``(module, function)`` key loses
    silently, and it is the reason the key names the call: that key
    mutes the whole of ``get_ssl_config``, so the ``os.remove`` below
    would be reported clean while the function it sits in kept its
    allowance for one unrelated call.
    """
    source = (
        'async def get_ssl_config(certificate):\n'
        '    os.remove(certificate[0])\n'
        '    return ssl.create_default_context(ssl.Purpose.SERVER_AUTH)\n'
        'async def somewhere_else(certificate):\n'
        '    return ssl.create_default_context(ssl.Purpose.SERVER_AUTH)\n'
    )
    allowed = 'helpers/internal/filters_helper.py'

    assert blocking_calls_in_async_defs(source, allowed) == [
        f'{allowed}:2: os.remove (in get_ssl_config)',
        f'{allowed}:5: ssl.create_default_context (in somewhere_else)',
    ]
    assert blocking_calls_in_async_defs(source, 'logic/http_client.py') == [
        'logic/http_client.py:2: os.remove (in get_ssl_config)',
        'logic/http_client.py:3: ssl.create_default_context '
        '(in get_ssl_config)',
        'logic/http_client.py:5: ssl.create_default_context '
        '(in somewhere_else)',
    ]


def test_the_scan_reports_every_banned_form() -> None:
    """The check fails on each shape it exists to forbid.

    Without this, a scan that silently matched nothing would report the
    package clean and keep reporting it clean forever.
    """
    source = (
        'async def outer(p):\n'
        '    with open(p) as fh:\n'
        '        pass\n'
        '    os.remove(p)\n'
        '    os.path.exists(p)\n'
        '    shutil.copy(p, p)\n'
        '    Path(p).read_bytes()\n'
        '    pathlib.Path(p).read_bytes()\n'
        '    pl.Path(p).write_text("x")\n'
        '    io.open(p)\n'
        '    builtins.open(p)\n'
        '    tempfile.NamedTemporaryFile()\n'
        '    glob.glob(p)\n'
        '    ssl.create_default_context(ssl.Purpose.SERVER_AUTH)\n'
        '    def helper():\n'
        '        return open(p).read()\n'
        '    async def inner():\n'
        '        return open(p)\n'
        '    return helper, inner\n'
    )

    offences = blocking_calls_in_async_defs(source, 'synthetic.py')

    assert offences == [
        'synthetic.py:2: open (in outer)',
        'synthetic.py:4: os.remove (in outer)',
        'synthetic.py:5: os.path.exists (in outer)',
        'synthetic.py:6: shutil.copy (in outer)',
        'synthetic.py:7: Path(...).read_bytes (in outer)',
        'synthetic.py:8: Path(...).read_bytes (in outer)',
        'synthetic.py:9: Path(...).write_text (in outer)',
        'synthetic.py:10: io.open (in outer)',
        'synthetic.py:11: builtins.open (in outer)',
        'synthetic.py:12: tempfile.NamedTemporaryFile (in outer)',
        'synthetic.py:13: glob.glob (in outer)',
        'synthetic.py:14: ssl.create_default_context (in outer)',
        'synthetic.py:16: open (in outer)',
        'synthetic.py:18: open (in inner)',
    ]


def test_the_scan_permits_the_async_and_pure_equivalents() -> None:
    """The replacements this story installed must not be flagged.

    A check that also rejected ``aiofiles.open`` would be unsatisfiable,
    and one that rejected a module-level synchronous helper would ban the
    only correct way to hand blocking work to an executor.
    """
    source = (
        'def module_level_helper(p):\n'
        '    return open(p).read()\n'
        'async def outer(p):\n'
        '    async with aiofiles.open(p, "wb") as fh:\n'
        '        await fh.write(b"")\n'
        '    await aiofiles.os.remove(p)\n'
        '    return PurePath(p).name, os.path.basename(p)\n'
    )

    assert blocking_calls_in_async_defs(source, 'synthetic.py') == []


# --- R20: the lazy blocking read the scan above cannot see ----------------


def test_request_helper_initialises_mimetypes_at_import() -> None:
    """``request_helper`` populates the mimetypes tables at import.

    ``build_upload_form`` calls ``mimetypes.guess_type``, and
    ``guess_type`` initialises lazily: on first use it stats every path in
    ``mimetypes.knownfiles`` and reads the ones that exist. That read would
    happen inside the ``attempt()`` closure, on the event loop -- the very
    thing this module exists to forbid -- and the AST scan above cannot see
    it, because ``build_upload_form`` is a plain ``def`` and the read is
    both lazy and several frames down inside the standard library. The
    module therefore calls ``mimetypes.init()`` at import, where
    synchronous I/O is legitimate, and this pins that.

    Asserting ``mimetypes.inited`` alone would certify nothing: something
    else in a pytest session has almost certainly initialised it already,
    so the assertion would pass with the module's ``init()`` call deleted.
    The global state is reset first -- both ``inited`` and the ``_db``
    cache that ``init()`` short-circuits on -- and the module reloaded, so
    the only thing that can set the flag is the import itself.

    All six module globals ``init()`` rebinds are then restored, not the
    two it was necessary to clear. ``init()`` also rebinds ``types_map``,
    ``suffix_map``, ``encodings_map`` and ``common_types`` to tables from
    the *new* database, so restoring only ``inited`` and ``_db`` left
    ``mimetypes.types_map is mimetypes._db.types_map[True]`` false for
    the rest of the session. The tables it leaked have equal content and
    nothing in this suite has depended on the identity, but the suite
    runs in a random order and this test's job is to leave nothing
    behind, not to leave behind something that happens to be equal.
    """
    saved_inited = mimetypes.inited
    saved_db = mimetypes._db
    saved_types_map = mimetypes.types_map
    saved_suffix_map = mimetypes.suffix_map
    saved_encodings_map = mimetypes.encodings_map
    saved_common_types = mimetypes.common_types
    try:
        mimetypes.inited = False
        mimetypes._db = None

        importlib.reload(request_helper)

        assert mimetypes.inited is True
    finally:
        mimetypes.inited = saved_inited
        mimetypes._db = saved_db
        mimetypes.types_map = saved_types_map
        mimetypes.suffix_map = saved_suffix_map
        mimetypes.encodings_map = saved_encodings_map
        mimetypes.common_types = saved_common_types


# --- R20: the converted site outside request_helper -----------------------


async def test_delete_local_file_path_removes_the_file(
    tmp_path: Path,
) -> None:
    """The async unlink still deletes, not merely awaits.

    ``aiofiles.os.remove`` returning a coroutine means a caller who
    forgot to await it would leave the file in place and raise no error,
    so the observable effect is what is asserted.
    """
    target = tmp_path / 'downloaded.bin'
    target.write_bytes(b'delete me')

    await delete_local_file_path(str(target))

    assert not target.exists()


async def test_delete_local_file_path_is_idempotent(
    tmp_path: Path,
) -> None:
    """R22-AC4/M19: deleting what is already gone succeeds silently.

    The inverse of the assertion it replaces, which pinned
    ``FileNotFoundError`` and said in its own docstring that R22 owned
    changing it. This is that change.

    Asserted the way the criterion states it -- *called twice, no
    exception* -- and deliberately **not** by deleting only an absent
    path: a delete that had quietly stopped deleting anything would
    pass that weaker version. So the first call must really remove the
    file, the second must tolerate its absence, and the file must be
    gone at the end. That is what makes this callable from a
    ``finally``, which is the documented cleanup shape and the point of
    the finding: the download path's own ``try/finally`` now removes a
    partial file, so a caller's cleanup routinely arrives second.
    """
    target = tmp_path / 'downloaded.bin'
    target.write_bytes(b'delete me twice')

    await delete_local_file_path(str(target))
    await delete_local_file_path(str(target))

    assert not target.exists()

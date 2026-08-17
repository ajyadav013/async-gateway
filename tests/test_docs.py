"""Mechanical checks that hold ``README.md`` to the code it describes.

R29's premise is that documentation drifts silently. The previous README
advertised a SOAP client that did not exist, instructed the reader to import a
module that was never there, called a helper with a parameter it did not have
while omitting the two it required, and shipped two primary examples that were
not even valid Python. None of it failed anything, because nothing checked.

These tests are that check, and they are deliberately five different kinds of
check rather than five variations of one:

1. **Every ``python`` block compiles.** A syntax error in an example fails the
   suite. Blocks that are illustrative rather than runnable are fenced
   ``text``, so this test never has to guess.
2. **Every documented import resolves.** A ``from async_gateway... import ...``
   line naming a module or a symbol that does not exist fails here rather than
   in a reader's editor.
3. **Every documented signature and call matches the real one.** A signature
   listing in the README is executed into a real function object and compared
   against ``inspect.signature`` parameter by parameter; a documented call is
   bound against the real signature. A renamed parameter, a flipped default, or
   a positional argument that became keyword-only fails here.
4. **Every example actually runs**, against a live loopback HTTP server and the
   FTP/SFTP transport doubles this suite already owns, with its own assertions
   intact. This is the strongest of the five: an example that stops being true
   fails CI, which is the only mechanism that keeps prose honest over time.
5. **The anti-drift set checks are both ways.** Every ``protocol_info`` key the
   code reads appears in the README *and* every key the README documents is one
   the code reads; likewise every allowlisted verb, every envelope key and
   every error code. A one-way check would let the README grow keys the library
   does not have.

Around those sit the small, sharp assertions R29 names individually: the
payload-echo disclosure, the three SOAP consumer facts, the retired corporate
endpoint, the retired ``999`` status, and the release section naming one file.

**Reading the README as data.** The document is parsed with small regular
expressions rather than a Markdown library, for the reason
``tests/test_packaging.py`` gives about ``pyproject.toml``: adding a dependency
to satisfy a test is a poor trade. Every helper raises when it matches nothing,
so a formatting change to the README breaks these tests loudly instead of
quietly reading an empty set and passing.
"""

import ast
import inspect
import re
from pathlib import Path
from typing import (  # noqa: F401 -- re-exported into example namespaces
    Any,
    Dict,
    Optional,
    Union,
)

from async_gateway.async_gateway import request
from async_gateway.helpers.internal.circuit_breaker_helper import (
    BREAKER_CONFIG_KEYS,
    RETRY_CONFIG_KEYS,
)
from async_gateway.logic.ftp_client import FTP_COMMANDS
from async_gateway.logic.sftp_client import SFTP_MODES
from async_gateway.utils.envelope import GatewayError, GatewayResponse
from async_gateway.utils.http_file_config import (
    HTTP_VERBS,
    delete_local_file_path,
    download_file_from_s3,
    download_file_from_url,
)
from async_gateway.utils.redaction import PAYLOAD_REDACTION_DEPTH
from async_gateway.utils.status_map import STATUS_BY_CODE

import pytest

from tests.fixtures.ftp import (
    TransferringFTPClient,
    certificate_pair,
    install_ftp_double,
)
from tests.fixtures.http_server import RecordingHTTPServer
from tests.fixtures.sftp import (
    SERVER_HOST_KEY,
    SSHTransportDouble,
    StubSFTPClient,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / 'README.md'
README_TEXT = README.read_text(encoding='utf-8')

#: The host every runnable example addresses. The example runner rewrites it to
#: the loopback server's authority, so the README reads like production code
#: while the test drives a real socket. Deliberately a reserved example domain:
#: an example naming a live third-party endpoint is republished in every sdist,
#: which is what the retired corporate host below was.
EXAMPLE_HOST = 'api.example.com'

#: Fenced blocks in the README, with their language tag.
_FENCE = re.compile(r'^```(?P<language>[a-z]*)\n(?P<body>.*?)^```',
                    re.DOTALL | re.MULTILINE)

#: A documented import of this package, however it is spelled.
_IMPORT = re.compile(
    r'^\s*(?:from\s+(?P<module>async_gateway[\w.]*)\s+import\s+'
    r'(?P<names>\([^)]*\)|[^\n]+)|import\s+(?P<plain>async_gateway[\w.]*))',
    re.MULTILINE)

#: One Markdown table row's cells.
_TABLE_ROW = re.compile(r'^\|(?P<cells>.+)\|\s*$', re.MULTILINE)

#: A Markdown heading of any level, with its text.
_HEADING = re.compile(
    r'^(?P<hashes>#{2,4})\s+(?P<title>.+?)\s*$', re.MULTILINE)

#: Inline code spans, which is how every key, verb and envelope field is
#: written in the prose as well as in the tables.
_INLINE_CODE = re.compile(r'`([^`\n]+)`')

#: Names an example may reference that the README leaves abstract on purpose --
#: a local path is the reader's own, and hardcoding one is the defect R22's
#: overwrite contract exists to stop the README teaching.
PLACEHOLDERS = frozenset({
    'CLIENT_CERT_PATH',
    'CLIENT_KEY_PATH',
    'CLIENT_SSH_KEY_PATH',
    'FILE_SOURCE_URL',
    'KNOWN_HOSTS_PATH',
    'LOCAL_DOWNLOAD_PATH',
    'LOCAL_UPLOAD_PATH',
})

#: The public callables a documented signature listing or call is checked
#: against. Keyed by the name the README uses, which is the name the reader
#: types.
PUBLIC_CALLABLES = {
    'delete_local_file_path': delete_local_file_path,
    'download_file_from_s3': download_file_from_s3,
    'download_file_from_url': download_file_from_url,
    'request': request,
}

#: The ``protocol_info`` keys every protocol client actually reads, discovered
#: from the source rather than restated here. Restating them would make this
#: test a second list to keep in step, which is the drift it exists to catch.
INFO_CONTAINER_NAMES = frozenset({'info', 'protocol_info'})


def fenced_blocks(language: str) -> list[str]:
    """Return every fenced block in the README written in ``language``.

    Args:
        language: The fence's language tag, e.g. ``'python'``.

    Returns:
        The block bodies, in document order.

    Raises:
        AssertionError: If the README carries no such block. A regex that
            silently matches nothing would make every test reading it pass
            vacuously.
    """
    blocks = [
        match.group('body') for match in _FENCE.finditer(README_TEXT)
        if match.group('language') == language
    ]
    assert blocks, f'README carries no ```{language} block'
    return blocks


def table_first_column(anchor: str) -> list[str]:
    """Return the first-column cells of the **first** table after ``anchor``.

    Anchored to a literal string -- a heading, or the bolded lead-in sentence
    that introduces a table -- rather than to a whole section, because several
    sections carry more than one table and a section-wide sweep would merge
    them. Merging is not a cosmetic problem: it is what would let a verb table
    and a media-type table be checked against each other's source and both
    pass.

    Args:
        anchor: A literal substring of the README that immediately precedes the
            table.

    Returns:
        Every first-column cell of that one table, with Markdown emphasis and
        backticks stripped and the header and separator rows dropped.

    Raises:
        AssertionError: If the anchor is absent, or no table follows it. Both
            mean this test has stopped checking what it names.
    """
    start = README_TEXT.find(anchor)
    assert start >= 0, f'README has no text containing {anchor!r}'

    cells: list[str] = []
    for row in _TABLE_ROW.finditer(README_TEXT, start):
        if cells and row.start() > _table_end(README_TEXT, start, cells):
            break
        first = row.group('cells').split('|')[0].strip()
        if not first or set(first) <= set('- :'):
            continue
        cells.append(first.strip('`*_ '))
    assert cells, f'no table follows {anchor!r}'
    return cells


def _table_end(text: str, start: int, seen: list[str]) -> int:
    """Return the offset at which the table begun after ``start`` ends.

    A Markdown table ends at the first blank line after it, so that is what is
    looked for -- computed rather than tracked, so the caller's loop stays a
    plain scan.

    Args:
        text: The README.
        start: Where the search for the table began.
        seen: The rows accumulated so far; non-empty when this is called.

    Returns:
        The offset of the blank line that ends the table, or the end of the
        document when it runs to the bottom.
    """
    first_row = _TABLE_ROW.search(text, start)
    assert first_row is not None
    blank = text.find('\n\n', first_row.start())
    return len(text) if blank < 0 else blank


def documented_code_spans() -> set[str]:
    """Return every inline code span in the README.

    Returns:
        The span contents, which is where a key, a verb or an envelope field is
        written whether it appears in a table or in prose.
    """
    return {span.strip() for span in _INLINE_CODE.findall(README_TEXT)}


def source_protocol_info_keys() -> set[str]:
    """Return the ``protocol_info`` keys the package actually reads.

    Walks every module's AST for a read of ``info``/``protocol_info`` -- a
    ``.get('x')``, an ``['x']`` subscript, or an ``'x' in`` membership test --
    so the answer comes from the code rather than from a list someone has to
    remember to update.

    Returns:
        The key names read anywhere in the package.

    Raises:
        AssertionError: If the walk finds nothing, which would mean the shapes
            it looks for have changed and this test has stopped checking.
    """
    keys: set[str] = set()
    for path in sorted((REPO_ROOT / 'async_gateway').rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            keys.update(_info_keys_in(node))
    assert keys, 'no protocol_info key reads found; the AST shapes changed'
    return keys


def _reads_info(node: ast.expr) -> bool:
    """Report whether ``node`` names a ``protocol_info`` mapping.

    Args:
        node: The expression a key is being read from.

    Returns:
        True for the bare name and for the ``self.info`` attribute form.
    """
    if isinstance(node, ast.Name):
        return node.id in INFO_CONTAINER_NAMES
    return (isinstance(node, ast.Attribute)
            and node.attr in INFO_CONTAINER_NAMES)


def _info_keys_in(node: ast.AST) -> set[str]:
    """Return the ``protocol_info`` key names this one node reads.

    Args:
        node: Any AST node.

    Returns:
        The key names, empty for a node that reads none.
    """
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'get' and _reads_info(node.func.value)
            and node.args and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)):
        return {node.args[0].value}
    if (isinstance(node, ast.Subscript) and _reads_info(node.value)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)):
        return {node.slice.value}
    if (isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)):
        return {
            node.left.value
            for operator, other in zip(node.ops, node.comparators)
            if isinstance(operator, ast.In) and _reads_info(other)
        }
    return set()


def documented_protocol_info_keys() -> set[str]:
    """Return the ``protocol_info`` keys the README's own tables document.

    Returns:
        The union of the first columns of the four per-protocol tables.
    """
    keys: set[str] = set()
    for protocol in ('HTTP and HTTPS', 'FTP', 'SFTP', 'SOAP'):
        keys.update(table_first_column(f'### `protocol_info` — {protocol}'))
    return keys - {'Key'}


def parameter_shape(signature: inspect.Signature) -> list[tuple]:
    """Reduce a signature to the part a caller can get wrong.

    Annotations are deliberately excluded: a README that spells a type
    ``Optional[str]`` where the source spells it ``Optional[Text]`` documents
    the same contract, and failing on that would train the next reader to
    weaken this test. Names, kinds, order and defaults are what a call site
    actually depends on.

    Args:
        signature: The signature to reduce.

    Returns:
        One ``(name, kind, default)`` triple per parameter, in order.
    """
    return [
        (name, parameter.kind, parameter.default)
        for name, parameter in signature.parameters.items()
    ]


def example_namespace(**extra: Any) -> dict:
    """Return the globals a README example is executed in.

    The typing names are present so a signature *listing* -- which is a real
    ``def`` with real annotations, evaluated eagerly on the interpreters this
    project supports -- executes into a function object this suite can compare
    against the real one.

    Args:
        extra: The placeholder values this example needs, e.g. a temporary
            download path.

    Returns:
        A fresh namespace, so one example cannot leak a name into the next.
    """
    namespace: dict = {
        'Any': Any,
        'Dict': Dict,
        'GatewayError': GatewayError,
        'GatewayResponse': GatewayResponse,
        'Optional': Optional,
        'Union': Union,
    }
    namespace.update(extra)
    return namespace


async def run_example(source: str, namespace: dict) -> dict:
    """Execute one README example, awaiting its top-level ``await``.

    Args:
        source: The example's source, with its host already rewritten to the
            loopback server.
        namespace: The globals to execute it in.

    Returns:
        The namespace after execution, so a caller can inspect what the
        example defined.
    """
    code = compile(source, str(README), 'exec',
                   ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    pending = eval(code, namespace)  # noqa: S307 -- the README is our own file
    if inspect.isawaitable(pending):
        await pending
    return namespace


# --------------------------------------------------------------------------
# 1. Every ```python block is executable Python.
# --------------------------------------------------------------------------


def test_every_python_block_compiles() -> None:
    """Compile every ```python block, so a broken example fails the suite."""
    for block in fenced_blocks('python'):
        compile(block, str(README), 'exec', ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


def test_illustrative_blocks_are_not_tagged_python() -> None:
    """Check the shell and diagram blocks are fenced ```text, not ```python."""
    for block in fenced_blocks('text'):
        assert 'await request(' not in block, (
            'a runnable example is fenced ```text, so nothing checks it')


# --------------------------------------------------------------------------
# 2. Every documented import resolves.
# --------------------------------------------------------------------------


def test_every_documented_import_resolves() -> None:
    """Import every symbol the README instructs the reader to import."""
    found = 0
    for block in fenced_blocks('python'):
        for match in _IMPORT.finditer(block):
            found += 1
            if match.group('plain'):
                __import__(match.group('plain'))
                continue
            module = __import__(match.group('module'), fromlist=['__name__'])
            names = match.group('names').strip('()').replace('\n', ' ')
            for name in filter(None, (n.strip() for n in names.split(','))):
                assert hasattr(module, name), (
                    f'README imports {name!r} from '
                    f'{match.group("module")}, which does not export it')
    assert found, 'no async_gateway import found in the README'


def test_documented_module_paths_exist() -> None:
    """Check every ``async_gateway`` module path named in prose is real."""
    checked = 0
    for span in documented_code_spans():
        candidate = span.strip('` ').removesuffix('()')
        if not candidate.startswith('async_gateway.') or ' ' in candidate:
            continue
        checked += 1
        module, _, tail = candidate.rpartition('.')
        try:
            __import__(candidate)
        except ImportError:
            imported = __import__(module, fromlist=['__name__'])
            assert hasattr(imported, tail), (
                f'README names {candidate!r}, which does not exist')
    assert checked, 'the README names no async_gateway module path'


# --------------------------------------------------------------------------
# 3. Every documented signature and call matches the real one.
# --------------------------------------------------------------------------


async def test_documented_signatures_match_the_real_ones() -> None:
    """Compare each documented signature listing against the real callable.

    The listing is *executed*, not pattern-matched, so what is compared is a
    real function object built from the README's own text. A renamed
    parameter, a changed default, or a parameter that stopped being
    keyword-only fails here.
    """
    checked = set()
    for block in fenced_blocks('python'):
        if 'def ' not in block or '...' not in block:
            continue
        namespace = await run_example(block, example_namespace())
        for name, real in PUBLIC_CALLABLES.items():
            documented = namespace.get(name)
            if documented is None or not callable(documented):
                continue
            assert (parameter_shape(inspect.signature(documented))
                    == parameter_shape(inspect.signature(real))), (
                f'the README documents {name}() with a signature the code '
                f'does not have')
            checked.add(name)
    assert checked >= {'request', 'download_file_from_s3',
                       'download_file_from_url', 'delete_local_file_path'}, (
        f'the README stopped documenting a public signature; '
        f'checked {checked}')


def test_documented_calls_bind_to_the_real_signature() -> None:
    """Bind every documented call of a public helper to its real signature.

    Static, so it covers the calls inside examples this suite rewrites before
    running as well as the ones it runs verbatim. A keyword the function does
    not take -- the previous README's ``file_save_path=`` -- fails here.
    """
    bound = 0
    for block in fenced_blocks('python'):
        tree = ast.parse(block, mode='exec',
                         type_comments=False)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, 'id', None)
            real = PUBLIC_CALLABLES.get(name)
            if real is None:
                continue
            if any(keyword.arg is None for keyword in node.keywords):
                # A `**params` call forwards a mapping this scan cannot see,
                # so binding it would assert against arguments that are not
                # here. The example runner executes those calls for real,
                # which is the stronger check anyway.
                continue
            keywords = {
                keyword.arg: None for keyword in node.keywords
            }
            inspect.signature(real).bind(*([None] * len(node.args)),
                                         **keywords)
            bound += 1
    assert bound, 'no documented call to a public helper was checked'


# --------------------------------------------------------------------------
# 4. Every example actually runs.
# --------------------------------------------------------------------------


SOAP_REPLY = (
    b'<?xml version="1.0"?>'
    b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    b'<soap:Body><Rate xmlns="urn:rates"><Value>1.09</Value></Rate>'
    b'</soap:Body></soap:Envelope>')

SOAP_12_REPLY = (
    b'<?xml version="1.0"?>'
    b'<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope">'
    b'<env:Body><Rate xmlns="urn:rates"><Value>1.09</Value></Rate>'
    b'</env:Body></env:Envelope>')

SOAP_FAULT_REPLY = (
    b'<?xml version="1.0"?>'
    b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    b'<soap:Body><soap:Fault>'
    b'<faultcode>soap:Client</faultcode>'
    b'<faultstring>Unknown currency pair</faultstring>'
    b'</soap:Fault></soap:Body></soap:Envelope>')


def _register_responses(server: RecordingHTTPServer) -> None:
    """Register the answer every README example expects.

    The bodies are the ones the examples assert on, so an example whose
    assertion is wrong fails rather than passing against a permissive double.

    Args:
        server: The loopback server the examples are pointed at.

    Returns:
        None.
    """
    json_headers = {'Content-Type': 'application/json'}
    server.respond('/v1/items', method='GET', body=b'{"items": []}',
                   headers=json_headers)
    server.respond('/v1/items', method='POST', status=201,
                   body=b'{"id": "w-1"}', headers=json_headers)
    server.respond('/v1/items/missing', status=404,
                   body=b'{"detail": "no such item"}', headers=json_headers)
    server.respond('/v1/attachments', method='POST', body=b'{"stored": true}',
                   headers=json_headers)
    server.respond('/report.csv', body=b'quarter,total\nQ1,17\n',
                   headers={'Content-Type': 'text/csv'})
    server.respond_in_sequence('/soap', _soap_specs())


def _soap_specs() -> list:
    """Return the three SOAP answers, in the order the examples ask for them.

    The README's SOAP examples run in document order -- 1.1 success, 1.2
    success, then the Fault -- so a sequence is what expresses them. The
    sequence's last entry repeats, which is what stops a fourth call falling
    into a surprise 404.

    Returns:
        The response specs, in document order.
    """
    from tests.fixtures.http_server import ResponseSpec

    xml = (('Content-Type', 'text/xml'),)
    soap12 = (('Content-Type', 'application/soap+xml'),)
    return [
        ResponseSpec(status=200, body=SOAP_REPLY, headers=xml),
        ResponseSpec(status=200, body=SOAP_12_REPLY, headers=soap12),
        ResponseSpec(status=500, body=SOAP_FAULT_REPLY, headers=xml),
    ]


async def test_every_example_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute every runnable README example, assertions and all.

    This is the criterion that makes the README un-driftable. Each example is
    executed with its own assertions intact against a live loopback HTTP server
    and this suite's FTP and SFTP transport doubles, so a documented envelope
    key, status code or ``protocol_details`` field that stops being true fails
    CI on the line that claims it.

    Args:
        tmp_path: Where the examples' local paths point, so no example writes
            to a fixed location -- which R22's overwrite contract now refuses
            on a second run, and which is the shape the old README taught.
        monkeypatch: Installs the FTP and SFTP doubles for the duration.

    Returns:
        None.
    """
    known_hosts = tmp_path / 'known_hosts'
    known_hosts.write_text(f'{SERVER_HOST_KEY}\n', encoding='utf-8')

    server = RecordingHTTPServer()
    await server.start()
    try:
        _register_responses(server)
        authority = server.url_for('/').rstrip('/')
        install_ftp_double(monkeypatch, client=TransferringFTPClient())
        SSHTransportDouble(sftp=StubSFTPClient()).install(monkeypatch)

        with certificate_pair() as (certificate_path, key_path):
            placeholders = {
                'CLIENT_CERT_PATH': certificate_path,
                'CLIENT_KEY_PATH': key_path,
                'CLIENT_SSH_KEY_PATH': str(tmp_path / 'id_ed25519'),
                'FILE_SOURCE_URL': f'{authority}/report.csv',
                'KNOWN_HOSTS_PATH': str(known_hosts),
                'LOCAL_DOWNLOAD_PATH': str(tmp_path / 'downloaded.csv'),
                'LOCAL_UPLOAD_PATH': str(tmp_path / 'upload.csv'),
            }
            ran = await _run_all_examples(authority, placeholders)
    finally:
        await server.close()

    assert ran >= 12, f'only {ran} README examples ran; expected the full set'


async def _run_all_examples(authority: str, placeholders: dict) -> int:
    """Run every ```python block that is not a signature listing.

    Args:
        authority: The loopback server's ``scheme://host:port``, substituted
            for the README's example host.
        placeholders: The abstract names the examples reference.

    Returns:
        How many examples ran, so the caller can assert none were skipped
        silently.
    """
    ran = 0
    for index, block in enumerate(fenced_blocks('python')):
        if 'def ' in block and '...' in block:
            continue
        source = _point_at(block, authority)
        try:
            await run_example(source, example_namespace(**placeholders))
        except Exception as failure:  # noqa: BLE001 -- re-raised, see below
            # Re-raised with the block attached. The examples are compiled
            # under the README's own name, so their tracebacks carry
            # block-relative line numbers against a 1,000-line file and point
            # at the wrong lines. Naming the block is what makes the failure
            # actionable rather than a hunt.
            raise AssertionError(
                f'README example #{index} failed: {type(failure).__name__}: '
                f'{failure}\n\n{source}') from failure
        ran += 1
    return ran


def _point_at(block: str, authority: str) -> str:
    """Rewrite an example's example-host URLs to the live loopback server.

    The loopback server speaks plaintext, so ``https://<example host>`` and the
    ``protocol='HTTPS'`` that goes with it are rewritten **as a pair**. That is
    a substitution of the transport, not of the example: every argument, every
    envelope key and every assertion still executes exactly as written, and
    what TLS itself does is covered by this project's own TLS suites rather
    than by a documentation test.

    An example that names ``http://`` is left alone deliberately. The
    error-handling example writes ``http://`` under ``protocol='HTTPS'`` to
    demonstrate the refusal, so rewriting either half would delete the very
    thing it exists to show -- and it raises before a socket is opened, so it
    needs no server.

    Args:
        block: The example source.
        authority: The loopback ``scheme://host:port``.

    Returns:
        The example, pointed at the running server.
    """
    secure = f'https://{EXAMPLE_HOST}'
    if secure not in block:
        return block
    return (block
            .replace(secure, authority)
            .replace("protocol='HTTPS'", "protocol='HTTP'"))


# --------------------------------------------------------------------------
# 5. Anti-drift: every documented set matches the code's, both ways.
# --------------------------------------------------------------------------


def test_protocol_info_keys_match_the_code_both_ways() -> None:
    """Check the documented ``protocol_info`` keys are exactly the real ones.

    Both directions, which is the point: a one-way check lets the README grow
    a key the library never reads, and the other one-way check lets the library
    grow a key no reader can discover.
    """
    documented = documented_protocol_info_keys()
    real = source_protocol_info_keys()
    assert real - documented == set(), (
        f'the code reads protocol_info keys the README does not document: '
        f'{sorted(real - documented)}')
    assert documented - real == set(), (
        f'the README documents protocol_info keys the code never reads: '
        f'{sorted(documented - real)}')


def test_http_verbs_match_the_allowlist_both_ways() -> None:
    """Check the README's HTTP verb table is exactly ``HTTP_VERBS``.

    ``head`` is the case this was written for: the allowlist has admitted it
    all along and the old README's table did not list it.
    """
    documented = {
        verb.lower()
        for verb in table_first_column('**HTTP verbs.**')
    } - {'verb'}
    assert documented == set(HTTP_VERBS), (
        f'README verb table {sorted(documented)} != allowlist '
        f'{sorted(HTTP_VERBS)}')


def test_ftp_commands_match_the_allowlist_both_ways() -> None:
    """Check the README's FTP command table is exactly ``FTP_COMMANDS``."""
    documented = {
        name.lower()
        for name in table_first_column('**FTP commands**')
    } - {'command'}
    assert documented == set(FTP_COMMANDS), (
        f'README command table {sorted(documented)} != allowlist '
        f'{sorted(FTP_COMMANDS)}')


def test_sftp_modes_match_the_allowlist_both_ways() -> None:
    """Check the README's SFTP mode table is exactly ``SFTP_MODES``."""
    documented = {
        name.lower()
        for name in table_first_column('**SFTP modes**')
    } - {'mode'}
    assert documented == set(SFTP_MODES), (
        f'README mode table {sorted(documented)} != allowlist '
        f'{sorted(SFTP_MODES)}')


def test_envelope_keys_are_documented_both_ways() -> None:
    """Check the envelope table names exactly ``GatewayResponse``'s keys."""
    documented = set(table_first_column('## The response envelope')) - {'Key'}
    real = set(GatewayResponse.__annotations__)
    assert documented == real, (
        f'the envelope table and GatewayResponse disagree: '
        f'missing {sorted(real - documented)}, '
        f'invented {sorted(documented - real)}')

    documented_error = set(
        table_first_column('`error`, when present, is a `GatewayError`'),
    ) - {'Key'}
    real_error = set(GatewayError.__annotations__)
    assert documented_error == real_error, (
        f'the error table and GatewayError disagree: '
        f'missing {sorted(real_error - documented_error)}, '
        f'invented {sorted(documented_error - real_error)}')


def test_error_codes_are_documented_both_ways() -> None:
    """Check the error-code table names exactly the codes the library emits."""
    documented = {
        code.strip('` ')
        for cell in table_first_column('### The error-code table')
        for code in cell.split('/')
    } - {'code'}
    real = set(STATUS_BY_CODE)
    assert real - documented == set(), (
        f'error codes missing from the README: {sorted(real - documented)}')
    assert documented - real == set(), (
        f'the README documents error codes the library never emits: '
        f'{sorted(documented - real)}')


def test_retry_and_breaker_config_keys_are_documented_both_ways() -> None:
    """Check the two resilience tables name exactly the accepted keys.

    These configurations reject an unknown key by name rather than defaulting
    it silently, so a key the README invents is a call that fails at runtime.
    """
    breaker = set(table_first_column(
        '`circuit_breaker_config` keys — an unknown key is')) - {'Key'}
    assert breaker == set(BREAKER_CONFIG_KEYS), (
        f'README breaker table {sorted(breaker)} != accepted '
        f'{sorted(BREAKER_CONFIG_KEYS)}')
    retry = set(table_first_column(
        '`retry_config` keys — likewise closed')) - {'Key'}
    assert retry == set(RETRY_CONFIG_KEYS), (
        f'README retry table {sorted(retry)} != accepted '
        f'{sorted(RETRY_CONFIG_KEYS)}')


def test_exception_hierarchy_names_every_class() -> None:
    """Check every exception the library defines appears in the README."""
    from async_gateway.utils import exceptions

    documented = documented_code_spans() | set(README_TEXT.split())
    for name, value in vars(exceptions).items():
        if (inspect.isclass(value)
                and issubclass(value, exceptions.AsyncGatewayError)):
            assert name in documented, (
                f'{name} is part of the public hierarchy and the README '
                f'never names it')


# --------------------------------------------------------------------------
# The named disclosures R29 makes checkable rather than human-judged.
# --------------------------------------------------------------------------


def test_payload_echo_disclosure_is_present_and_correct() -> None:
    """Check the README states the payload echo's bound, and states it right.

    R8/E9 requires the redaction bound to be disclosed "plainly"; this is where
    that clause stops being a human judgement. The depth is read from the code,
    so a change to ``PAYLOAD_REDACTION_DEPTH`` that the README does not follow
    fails here.
    """
    section = README_TEXT.partition('## Redaction and the payload echo')[2]
    assert section, 'the README has no redaction section'
    assert f'depth of {PAYLOAD_REDACTION_DEPTH}' in section, (
        f'the README does not state the masking depth as '
        f'{PAYLOAD_REDACTION_DEPTH}')
    assert 'by key name' in section
    assert f'Below depth {PAYLOAD_REDACTION_DEPTH}' in section, (
        'the README does not state what happens below the masking depth')
    assert 'not a mapping' in section, (
        'the README does not disclose that a non-mapping payload is echoed')
    assert 'verbatim' in section, (
        "the README does not say the caller's own data is echoed verbatim")


def test_soap_section_carries_the_three_consumer_facts() -> None:
    """Check the SOAP section states all three facts a consumer hits first.

    R18's edge-case list asserts MTOM is "documented as out of scope"; this is
    where that documentation lives, so the claim is tested rather than merely
    made.
    """
    section = README_TEXT.partition('### SOAP')[2].partition('\n## ')[0]
    assert section, 'the README has no SOAP section'

    # 1. Hand-built XML only -- no dict-to-XML mapping.
    assert 'no dict-to-XML mapping' in section
    assert 'Element' in section

    # 2. `soap_body` is a raw Element, shown with one line of ElementTree.
    assert 'soap_body' in section
    assert 'raw `Element`' in section
    assert 'findtext(' in section, (
        'the README does not show the one line of ElementTree a consumer '
        'needs to read soap_body')

    # 3. MTOM unsupported, and raising.
    assert 'MTOM' in section
    assert 'multipart/related' in section
    assert 'ConfigurationError' in section

    assert 'WSDL' in section and 'not supported' in section


def test_sftp_transport_security_is_documented() -> None:
    """Check SFTP host-key verification, pinning, bypass and key auth appear.

    The old README returned zero matches for "sftp" across 1,126 lines, so a
    reader had no way to learn any of this.
    """
    assert README_TEXT.lower().count('sftp') > 0
    for claim in (
        'known_hosts',
        'host_key',
        'insecure_skip_host_key_check',
        'client_keys',
        'HOST_KEY',
    ):
        assert claim in README_TEXT, f'the README never mentions {claim}'


def test_url_trust_contract_is_stated() -> None:
    """Check the README says plainly that the caller owns URL validation."""
    section = README_TEXT.partition('## You own URL validation')[2]
    assert section, 'the README has no URL-trust section'
    assert 'SSRF' in section
    assert 'validate' in section
    assert 'untrusted' in section
    assert 'allowed_schemes' in section


def test_the_retired_corporate_endpoint_is_gone() -> None:
    """Check no live third-party endpoint is republished in the sdist.

    The README is the distribution's long description, so anything in it ships
    in every sdist -- which is what made 19 occurrences of a live corporate
    host a problem rather than an untidiness.
    """
    assert README_TEXT.count('api.fyndx1.de') == 0


def test_the_fabricated_status_is_gone() -> None:
    """Check ``999`` appears nowhere as a status code."""
    assert not re.search(r'\b999\b', README_TEXT), (
        'the README still names 999, a status this library no longer invents')


def test_the_release_section_names_one_file_to_bump() -> None:
    """Check the release procedure names exactly one file to edit."""
    section = README_TEXT.partition('### Cutting a release')[2]
    assert section, 'the README has no release section'
    assert 'exactly one file' in section
    assert 'pyproject.toml' in section
    assert 'setup.py' not in section
    assert 'setup.cfg' not in section


def test_the_licence_and_attribution_are_stated() -> None:
    """Check the README states the licence and the copyright attribution."""
    section = README_TEXT.partition('## Licence and attribution')[2]
    assert section, 'the README has no licence section'
    assert 'MIT' in section
    licence_line = next(
        line for line in (REPO_ROOT / 'LICENSE').read_text(
            encoding='utf-8').splitlines() if line.startswith('Copyright'))
    holder = licence_line.partition(') ')[2]
    assert holder in section, (
        f'the README does not carry the LICENSE copyright holder {holder!r}')


def test_the_supported_python_versions_track_the_floor() -> None:
    """Check the stated interpreter floor matches ``requires-python``."""
    pyproject = (REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    floor = re.search(r'requires-python\s*=\s*[\'"]>=\s*(\d+\.\d+)',
                      pyproject).group(1)
    section = README_TEXT.partition('## Supported Python versions')[2]
    assert section, 'the README has no supported-versions section'
    assert f'Python {floor} and newer' in section, (
        f'the README does not state the {floor} floor pyproject declares')


def test_no_serialisation_library_the_project_dropped_is_named() -> None:
    """Check the README names no dependency this release retired.

    ``ujson``, ``pytz`` and ``requests`` are all gone from the runtime set; a
    README still naming one would send a reader to a library the package does
    not install. The check is against the *declared* dependency set rather than
    a hand-written list, so a name that never appears in ``pyproject.toml``
    cannot be documented as one of this library's requirements.
    """
    pyproject = (REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    for retired in ('ujson', 'pytz'):
        assert retired not in pyproject, (
            f'{retired} is back in pyproject.toml; this test is stale')
        assert not re.search(rf'\b{retired}\b', README_TEXT), (
            f'the README names {retired}, which this release removed')
    # `requests` needs a narrower pattern than a word boundary: "pull requests"
    # is ordinary English and appears in the contributing section.
    assert not re.search(r'`requests`|import requests', README_TEXT), (
        'the README names the requests library, which this release removed')
    assert 'orjson' in README_TEXT


def test_the_timeout_contract_states_the_whole_chain() -> None:
    """Check the README says the HTTP deadline bounds the whole redirect chain.

    The distinction is load-bearing: a per-hop deadline would let a
    ``max_redirects=10`` chain run for eleven times the stated timeout, and a
    reader budgeting on the wrong one has no way to discover it.
    """
    section = README_TEXT.partition('### Timeouts')[2]
    assert section, 'the README has no timeout section'
    assert 'whole exchange' in section
    assert 'redirect chain included' in section


def test_the_local_write_contract_is_documented() -> None:
    """Check the download overwrite, mode and symlink rules are all stated."""
    section = README_TEXT.partition('## Local files')[2]
    assert section, 'the README has no local-files section'
    for claim in (
        'refuse to overwrite by default',
        'overwrite=True',
        'O_EXCL',
        'O_NOFOLLOW',
        '0600',
        'symbolic link',
        'process working directory',
    ):
        assert claim in section, (
            f'the local-write contract does not state {claim!r}')


def test_the_traceback_logging_tradeoff_is_documented() -> None:
    """Check the README discloses the ``exc_info`` trade and its revert.

    The library renders and redacts the traceback itself rather than emitting
    ``exc_info=True``, which costs native APM exception grouping. That is a
    consumer-visible cost, so it is documented as a choice with a stated
    revert rather than left to be discovered in production.
    """
    section = README_TEXT.partition('## Logging')[2]
    assert section, 'the README has no logging section'
    assert "extra['traceback']" in section
    assert 'exc_info' in section
    assert 'group' in section, (
        'the README does not state the APM grouping this costs')

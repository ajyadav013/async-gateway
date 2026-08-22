"""Two mechanical documentation suites: the source tree, and the README.

This file is the union of two independently-written enforcement suites that
were merged at the same path. They check different documents and neither
subsumes the other, so both are kept whole:

* **Part A (R29)** holds ``README.md`` to the code it describes -- every
  example compiles, imports, binds and runs.
* **Part B (R30)** holds the *source tree* to the project's documentation
  and annotation standard -- docstring content floors, Args/Returns/Raises
  coverage, and full type annotations.

Their helpers do not collide; each part's own preamble follows.

Part A -- README conformance (R29)
==================================

Mechanical checks that hold ``README.md`` to the code it describes.

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
2. **Every documented import resolves.** A ``from asyncio_gateway...
   import ...`` line naming a module or a symbol that does not exist fails
   here rather than in a reader's editor.
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

Part B -- source-tree documentation standard (R30)
==================================================

R30: the documentation and annotation standard, enforced mechanically.

Every ``.py`` file under ``asyncio_gateway/`` is parsed and checked against
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
  -- and was **inert there** for most of this release, because
  ``ignore_errors = true`` in the same section suppressed it along with
  everything else. That was measured rather than assumed: with both
  settings on, ``mypy asyncio_gateway`` reported ``Success: no issues
  found`` while seven functions were missing annotations. Story AGW-26
  removed ``ignore_errors``, so the flag now has something behind it.
  :func:`test_every_signature_is_fully_annotated` held the line while it
  did not, and keeps holding it now, which is the point: a gate
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
import inspect
import re
from pathlib import Path
from typing import (  # noqa: F401 -- re-exported into example namespaces
    Any,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)

import pytest

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.helpers.internal.circuit_breaker_helper import (
    BREAKER_CONFIG_KEYS,
    RETRY_CONFIG_KEYS,
)
from asyncio_gateway.logic import protocol_mapping
from asyncio_gateway.logic.ftp_client import FTP_COMMANDS
from asyncio_gateway.logic.gcs_client import (
    GCS_COMMANDS,
    GCS_OPERATION_INFO_KEYS,
    GcsRequest,
)
from asyncio_gateway.logic.graphql_client import GraphqlRequest
from asyncio_gateway.logic.grpc_client import GRPC_HTTP_STATUS, GrpcRequest
from asyncio_gateway.logic.jsonrpc_client import JsonRpcRequest
from asyncio_gateway.logic.s3_client import S3Request, S3_OPERATION_INFO_KEYS
from asyncio_gateway.logic.sftp_client import SFTP_MODES
from asyncio_gateway.utils.envelope import GatewayError, GatewayResponse
from asyncio_gateway.utils.http_file_config import (
    HTTP_VERBS,
    delete_local_file_path,
    download_file_from_s3,
    download_file_from_url,
)
from asyncio_gateway.utils.redaction import PAYLOAD_REDACTION_DEPTH
from asyncio_gateway.utils.status_map import STATUS_BY_CODE

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
    r'^\s*(?:from\s+(?P<module>asyncio_gateway[\w.]*)\s+import\s+'
    r'(?P<names>\([^)]*\)|[^\n]+)|import\s+(?P<plain>asyncio_gateway[\w.]*))',
    re.MULTILINE)

#: One Markdown table row's cells.
_TABLE_ROW = re.compile(r'^\|(?P<cells>.+)\|\s*$', re.MULTILINE)

#: A Markdown heading of any level, with its text.
_HEADING = re.compile(
    r'^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$', re.MULTILINE)

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

#: The public selector registry frozen by protocol-expansion R1.
PROTOCOL_SELECTOR_CLASSES = {
    'HTTP': 'HttpRequest',
    'HTTPS': 'HttpRequest',
    'FTP': 'FTPRequest',
    'SFTP': 'SFTPRequest',
    'SOAP': 'SoapRequest',
    'JSONRPC': 'JsonRpcRequest',
    'GRAPHQL': 'GraphqlRequest',
    'S3': 'S3Request',
    'GRPC': 'GrpcRequest',
    'GCS': 'GcsRequest',
}

#: Closed R12 inventories for the four additive selectors.
NEW_SELECTOR_REQUIRED_KEYS = {
    'JSONRPC': frozenset({'method', 'request_id'}),
    'GRAPHQL': frozenset({'query'}),
    'S3': frozenset({'command'}),
    'GRPC': frozenset({'method'}),
    'GCS': frozenset({'command'}),
}
NEW_SELECTOR_OPTION_KEYS = {
    'JSONRPC': frozenset({
        'method', 'request_id', 'headers', 'cookies', 'certificate',
        'verify_ssl', 'trace_config', 'timeout', 'max_response_bytes',
        'circuit_breaker_config', 'redact_query_params',
    }),
    'GRAPHQL': frozenset({
        'query', 'operation_name', 'headers', 'cookies', 'certificate',
        'verify_ssl', 'trace_config', 'timeout', 'max_response_bytes',
        'circuit_breaker_config', 'redact_query_params',
    }),
    'S3': frozenset({
        'command', 'local_path', 'region', 'max_response_bytes',
        'max_upload_bytes', 'max_items', 'continuation_token',
        'circuit_breaker_config', 'redact_query_params',
    }),
    'GRPC': frozenset({
        'method', 'metadata', 'request_serializer', 'response_deserializer',
        'timeout', 'max_response_bytes', 'circuit_breaker_config',
        'redact_query_params',
    }),
    'GCS': GcsRequest.ALLOWED_INFO_KEYS,
}
S3_COMMAND_OPTION_KEYS = {
    'download': frozenset({
        'command', 'local_path', 'region', 'max_response_bytes',
        'circuit_breaker_config', 'redact_query_params',
    }),
    'upload': frozenset({
        'command', 'local_path', 'region', 'max_upload_bytes',
        'circuit_breaker_config', 'redact_query_params',
    }),
    'head': frozenset({
        'command', 'region', 'circuit_breaker_config',
        'redact_query_params',
    }),
    'list': frozenset({
        'command', 'region', 'max_items', 'continuation_token',
        'circuit_breaker_config', 'redact_query_params',
    }),
}

#: Spec-frozen GCS command boundaries in public documentation order. Tuples
#: intentionally retain ordering and duplicates until the assertions below
#: reject them; a dict/set would silently hide either kind of documentation
#: drift.
GCS_COMMAND_OPTION_ROWS = (
    (
        'download',
        ('command', 'local_path'),
        (
            'max_response_bytes', 'if_generation_match', 'timeout',
            'circuit_breaker_config', 'redact_query_params',
        ),
    ),
    (
        'upload',
        ('command', 'local_path'),
        (
            'max_upload_bytes', 'if_generation_match', 'timeout',
            'circuit_breaker_config', 'redact_query_params',
        ),
    ),
    (
        'head',
        ('command',),
        (
            'if_generation_match', 'timeout', 'circuit_breaker_config',
            'redact_query_params',
        ),
    ),
    (
        'list',
        ('command',),
        (
            'max_items', 'page_token', 'timeout',
            'circuit_breaker_config', 'redact_query_params',
        ),
    ),
    (
        'signed_url GET',
        ('command', 'method'),
        (
            'expires_in_seconds', 'signing_service_account', 'timeout',
            'redact_query_params',
        ),
    ),
    (
        'signed_url PUT',
        ('command', 'method', 'content_type', 'max_upload_bytes'),
        (
            'expires_in_seconds', 'signing_service_account',
            'if_generation_match', 'timeout', 'redact_query_params',
        ),
    ),
)

#: Exact JSON-safe detail schemas consumers may rely on, in documented order.
GCS_SUCCESS_DETAIL_ROWS = (
    (
        'download',
        (
            'command', 'bucket', 'key', 'local_path', 'bytes_written', 'etag',
            'generation', 'crc32c',
        ),
    ),
    (
        'upload',
        (
            'command', 'bucket', 'key', 'local_path', 'bytes_read', 'etag',
            'generation', 'metageneration', 'crc32c',
        ),
    ),
    (
        'head',
        (
            'command', 'bucket', 'key', 'content_length', 'content_type',
            'etag', 'generation', 'metageneration', 'last_modified', 'crc32c',
            'metadata',
        ),
    ),
    (
        'list',
        (
            'command', 'bucket', 'prefix', 'items', 'item_count',
            'is_truncated', 'next_page_token',
        ),
    ),
    (
        'signed_url GET',
        (
            'command', 'method', 'bucket', 'key', 'expires_in_seconds',
            'signed_url',
        ),
    ),
    (
        'signed_url PUT',
        (
            'command', 'method', 'bucket', 'key', 'expires_in_seconds',
            'signed_url', 'content_type', 'max_upload_bytes',
            'if_generation_match', 'required_headers',
        ),
    ),
)

#: Every normalized list item has this exact closed schema.
GCS_LIST_ITEM_KEYS = (
    'key', 'size', 'content_type', 'etag', 'generation', 'last_modified',
    'crc32c',
)

#: Signed PUT's exact six inputs/public obligations. The repeated surface
#: labels are legitimate, while the full (surface, name) key must be unique.
GCS_SIGNED_PUT_HEADER_ROWS = (
    ('SDK dedicated argument', 'content_type', 'content_type'),
    (
        'SDK headers', 'x-goog-content-length-range',
        '1,<max_upload_bytes>',
    ),
    (
        'SDK headers', 'x-goog-if-generation-match',
        'str(if_generation_match)',
    ),
    ('public required_headers', 'content-type', 'content_type'),
    (
        'public required_headers', 'x-goog-content-length-range',
        '1,<max_upload_bytes>',
    ),
    (
        'public required_headers', 'x-goog-if-generation-match',
        'str(if_generation_match)',
    ),
)

#: Capabilities deliberately excluded from the bounded selector.
GCS_OUT_OF_SCOPE_ROWS = (
    ('delete', 'unsupported'),
    ('bucket administration', 'unsupported'),
    ('custom endpoint', 'unsupported'),
    ('signed POST', 'unsupported'),
    ('signed DELETE', 'unsupported'),
    ('arbitrary headers', 'unsupported'),
    ('arbitrary query parameters', 'unsupported'),
)

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


def document_table(
    document: str,
    anchor: str,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Return one complete Markdown table without collapsing its shape.

    Args:
        document: Markdown-bearing text to parse.
        anchor: Literal text immediately preceding the table.

    Returns:
        The exact header and ordered data rows, with Markdown code/emphasis
        markers removed. Duplicate rows and cells remain visible to callers.

    Raises:
        AssertionError: If the anchor or its following table is absent.
    """
    start = document.find(anchor)
    assert start >= 0, f'document has no text containing {anchor!r}'
    parsed: list[tuple[str, ...]] = []
    started = False
    for line in document[start:].splitlines():
        stripped = line.strip()
        if not stripped.startswith('|'):
            if started:
                break
            continue
        started = True
        cells = tuple(
            cell.strip().replace('`', '').replace('*', '')
            for cell in stripped.strip('|').split('|')
        )
        parsed.append(cells)
    assert len(parsed) >= 3, (
        f'table after {anchor!r} must have header, separator, and data rows')
    header, separator, *rows = parsed
    assert header and all(header), f'table after {anchor!r} has empty headers'
    width = len(header)
    assert len(separator) == width and all(
        re.fullmatch(r':?-{3,}:?', cell) for cell in separator
    ), f'table after {anchor!r} has an invalid Markdown separator'
    assert all(len(row) == width for row in rows), (
        f'table after {anchor!r} has inconsistent column counts: '
        f'{[len(row) for row in parsed]}')
    return header, tuple(rows)


def document_table_rows(document: str, anchor: str) -> list[list[str]]:
    """Return only the data rows of one complete Markdown table.

    Args:
        document: Markdown-bearing text to parse.
        anchor: Literal text immediately preceding the table.

    Returns:
        Ordered data rows as mutable lists for legacy README assertions.
    """
    _, rows = document_table(document, anchor)
    return [list(row) for row in rows]


def table_rows(anchor: str) -> list[list[str]]:
    """Return README data rows from the first table after an anchor.

    Args:
        anchor: Literal text immediately preceding the table.

    Returns:
        Cell strings with Markdown code/emphasis markers removed.
    """
    return document_table_rows(README_TEXT, anchor)


def gcs_contract_document(name: str) -> str:
    """Return one GCS contract document without importing its example.

    Args:
        name: ``README`` or ``example``.

    Returns:
        The README's bounded GCS section or the example module docstring.

    Raises:
        AssertionError: If the requested document is absent or empty.
    """
    if name == 'README':
        matches = [
            heading for heading in _HEADING.finditer(README_TEXT)
            if len(heading['hashes']) == 2
            and re.search(
                r'\b(?:GCS|Google Cloud Storage)\b',
                heading['title'],
                re.IGNORECASE,
            )
        ]
        assert len(matches) == 1, (
            'README must have exactly one dedicated H2 GCS/Google Cloud '
            f'Storage section, found {len(matches)}')
        start = matches[0].start()
        following_h2 = next(
            (
                heading for heading in _HEADING.finditer(
                    README_TEXT, matches[0].end())
                if len(heading['hashes']) <= 2
            ),
            None,
        )
        end = following_h2.start() if following_h2 is not None else len(
            README_TEXT)
        section = README_TEXT[start:end]
        assert section.strip(), 'README GCS section is empty'
        return section
    assert name == 'example', f'unknown GCS contract document {name!r}'
    path = REPO_ROOT / 'examples' / 'gcs_example.py'
    assert path.is_file(), 'examples/gcs_example.py is missing'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    docstring = ast.get_docstring(tree)
    assert docstring, 'examples/gcs_example.py has no contract docstring'
    return docstring


def _comma_separated_keys(cell: str, *, label: str) -> tuple[str, ...]:
    """Return ordered unique comma-separated keys from one table cell.

    Args:
        cell: A Markdown table cell after formatting markers are removed.
        label: Description used in assertion failures.

    Returns:
        Non-empty comma-separated values, with ``none`` mapped to empty.

    Raises:
        AssertionError: If a key is empty or repeated.
    """
    if cell.strip().lower() in {'none', '—'}:
        return ()
    keys = tuple(part.strip() for part in cell.split(','))
    assert all(keys), f'{label} has an empty comma-separated key'
    _assert_unique(keys, label=label)
    return keys


def _assert_unique(values: tuple[Any, ...], *, label: str) -> None:
    """Reject duplicate documentation values without collapsing them.

    Args:
        values: Ordered values to inspect.
        label: Description used in assertion failures.

    Raises:
        AssertionError: If one or more values occur more than once.
    """
    duplicates = tuple(
        value for index, value in enumerate(values)
        if value in values[:index]
    )
    assert not duplicates, f'{label} contains duplicates: {duplicates}'


def _assert_document_terms(
    document: str,
    concept_terms: dict[str, tuple[str, ...]],
) -> None:
    """Assert each semantic concept has at least one accepted spelling.

    Args:
        document: Documentation text to inspect.
        concept_terms: Concept labels mapped to accepted literal spellings.

    Raises:
        AssertionError: If a concept has no accepted spelling in the text.
    """
    lowered = ' '.join(document.lower().split())
    missing = [
        concept
        for concept, alternatives in concept_terms.items()
        if not any(term.lower() in lowered for term in alternatives)
    ]
    assert not missing, f'GCS documentation omits concepts: {missing}'


def _assert_document_contexts(
    document: str,
    concept_patterns: dict[str, tuple[str, ...]],
) -> None:
    """Require direction-bearing statements for security-sensitive facts.

    Args:
        document: Documentation text to inspect.
        concept_patterns: Concept labels mapped to accepted regex patterns.

    Raises:
        AssertionError: If no contextual pattern proves a concept's direction.
    """
    normalized = ' '.join(document.replace('`', '').lower().split())
    missing = [
        concept
        for concept, patterns in concept_patterns.items()
        if not any(re.search(pattern, normalized) for pattern in patterns)
    ]
    assert not missing, (
        f'GCS documentation lacks directional contract statements: {missing}')


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


def prose_after(anchor: str) -> str:
    """Return the README text after ``anchor``, whitespace-collapsed.

    Collapsed because this document is hard-wrapped at 79 columns, so a claim
    these tests look for -- "process working directory", "redirect chain
    included" -- routinely has a newline in the middle of it. Matching the raw
    text makes a pure reflow fail, which is a false positive, and a false
    positive on a documentation test teaches the next person to weaken the
    assertion rather than to keep the claim.

    Args:
        anchor: The heading or phrase the section starts at.

    Returns:
        Everything after ``anchor`` with every run of whitespace reduced to one
        space.

    Raises:
        AssertionError: If the anchor is absent, which means the section was
            renamed and this test has stopped checking it.
    """
    section = README_TEXT.partition(anchor)[2]
    assert section, f'the README has no section at {anchor!r}'
    return ' '.join(section.split())


def source_protocol_info_keys() -> set[str]:
    """Return the ``protocol_info`` keys the package actually reads.

    Walks every module's AST for a read of ``info``/``protocol_info`` -- a
    ``.get('x')``, an ``['x']`` subscript, or an ``'x' in`` membership test.
    It also reads the new selectors' production allowlists: their constructors
    intentionally copy the mapping to a private name before reading it, so a
    name-based AST scan alone would miss those frozen boundary keys.

    Returns:
        The key names read anywhere in the package.

    Raises:
        AssertionError: If the walk finds nothing, which would mean the shapes
            it looks for have changed and this test has stopped checking.
    """
    keys: set[str] = set()
    for path in sorted((REPO_ROOT / 'asyncio_gateway').rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            keys.update(_info_keys_in(node))
    for strategy in (
        JsonRpcRequest,
        GraphqlRequest,
        S3Request,
        GrpcRequest,
        GcsRequest,
    ):
        keys.update(strategy.ALLOWED_INFO_KEYS)
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
        The union of the first columns of the eight per-protocol tables.
    """
    keys: set[str] = set()
    for protocol in (
        'HTTP and HTTPS',
        'FTP',
        'SFTP',
        'SOAP',
        'JSONRPC',
        'GRAPHQL',
        'S3',
        'GRPC',
        'GCS',
    ):
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
    assert found, 'no asyncio_gateway import found in the README'


def test_documented_module_paths_exist() -> None:
    """Check every ``asyncio_gateway`` module path named in prose is real."""
    checked = 0
    for span in documented_code_spans():
        candidate = span.strip('` ').removesuffix('()')
        if not candidate.startswith('asyncio_gateway.') or ' ' in candidate:
            continue
        checked += 1
        module, _, tail = candidate.rpartition('.')
        try:
            __import__(candidate)
        except ImportError:
            imported = __import__(module, fromlist=['__name__'])
            assert hasattr(imported, tail), (
                f'README names {candidate!r}, which does not exist')
    assert checked, 'the README names no asyncio_gateway module path'


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
    server.respond('/rpc', method='POST', body=(
        b'{"jsonrpc":"2.0","id":1,"result":4}'),
        headers=json_headers)
    server.respond('/graphql', method='POST', body=(
        b'{"data":{"widget":{"id":"w-1"}}}'),
        headers={'Content-Type': 'application/graphql-response+json'})
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


def test_pe80_opening_and_registry_name_exactly_ten_selectors() -> None:
    """The README opening and public registry expose the same closed set."""
    opening = ' '.join(README_TEXT.partition('```python')[0].split())
    match = re.search(
        r'One `await` for (?P<selectors>[^.]+)\.',
        opening,
    )
    assert match is not None, (
        'the README opening must introduce all selectors in one sentence')
    documented = tuple(re.findall(r'`([A-Z0-9]+)`', match['selectors']))
    assert documented == tuple(PROTOCOL_SELECTOR_CLASSES)
    assert {
        name: strategy.__name__
        for name, strategy in protocol_mapping.items()
    } == PROTOCOL_SELECTOR_CLASSES


@pytest.mark.parametrize(
    ('selector', 'strategy'),
    (
        ('JSONRPC', JsonRpcRequest),
        ('GRAPHQL', GraphqlRequest),
        ('S3', S3Request),
        ('GRPC', GrpcRequest),
        ('GCS', GcsRequest),
    ),
)
def test_pe80_new_selector_option_tables_are_exact(
    selector: str,
    strategy: type,
) -> None:
    """Each R12 table is both exhaustive and equal to its strategy class."""
    documented = frozenset(
        table_first_column(f'### `protocol_info` — {selector}')) - {'Key'}
    assert documented == NEW_SELECTOR_OPTION_KEYS[selector]
    assert strategy.REQUIRED_INFO_KEYS == NEW_SELECTOR_REQUIRED_KEYS[selector]
    assert strategy.ALLOWED_INFO_KEYS == NEW_SELECTOR_OPTION_KEYS[selector]


@pytest.mark.parametrize('selector', ('JSONRPC', 'GRAPHQL'))
def test_pe80_http_semantic_default_cells_match_the_transport(
    selector: str,
) -> None:
    """Semantic HTTP adapters document the inherited omission defaults."""
    rows = {
        row[0]: row
        for row in table_rows(f'### `protocol_info` — {selector}')
    }
    assert rows['cookies'][2] == 'None'
    assert rows['trace_config'][2] == 'a built-in tracer'


def test_pe80_s3_command_option_inventory_is_exact() -> None:
    """S3 documents the command-scoped allowlists, not one loose union."""
    documented = {
        row[0]: frozenset(part.strip() for part in row[1].split(',') if part)
        for row in table_rows('**S3 command option allowlists.**')
    }
    assert documented == S3_COMMAND_OPTION_KEYS
    assert dict(S3_OPERATION_INFO_KEYS) == S3_COMMAND_OPTION_KEYS


@pytest.mark.parametrize('document_name', ('README', 'example'))
def test_gcs_command_option_inventory_is_exact(
    document_name: str,
) -> None:
    """README and example freeze required and optional keys per GCS command.

    Args:
        document_name: Contract document under test.
    """
    header, rows = document_table(
        gcs_contract_document(document_name),
        '**GCS command option allowlists.**',
    )
    assert header == ('Operation', 'Required keys', 'Optional keys')
    assert len(rows) == 6
    _assert_unique(tuple(row[0] for row in rows), label='GCS operation labels')
    documented = tuple(
        (
            row[0],
            _comma_separated_keys(
                row[1], label=f'{row[0]} required keys'),
            _comma_separated_keys(
                row[2], label=f'{row[0]} optional keys'),
        )
        for row in rows
    )
    for operation, required, optional in documented:
        _assert_unique(
            required + optional,
            label=f'{operation} combined option keys',
        )
    runtime = {
        'download': GCS_OPERATION_INFO_KEYS['download'],
        'upload': GCS_OPERATION_INFO_KEYS['upload'],
        'head': GCS_OPERATION_INFO_KEYS['head'],
        'list': GCS_OPERATION_INFO_KEYS['list'],
        'signed_url GET': GCS_OPERATION_INFO_KEYS['signed_get'],
        'signed_url PUT': GCS_OPERATION_INFO_KEYS['signed_put'],
    }

    assert documented == GCS_COMMAND_OPTION_ROWS
    assert tuple(
        (operation, frozenset(required + optional))
        for operation, required, optional in documented
    ) == tuple(runtime.items())
    assert GCS_COMMANDS == frozenset({
        'download', 'upload', 'head', 'list', 'signed_url',
    })


@pytest.mark.parametrize('document_name', ('README', 'example'))
def test_gcs_success_detail_schemas_are_exact(
    document_name: str,
) -> None:
    """README and example expose every closed GCS success-detail schema.

    Args:
        document_name: Contract document under test.
    """
    header, rows = document_table(
        gcs_contract_document(document_name),
        '**GCS success detail schemas.**',
    )
    assert header == ('Operation', 'Exact protocol_details keys')
    _assert_unique(tuple(row[0] for row in rows), label='GCS schema labels')
    documented = tuple(
        (
            row[0],
            _comma_separated_keys(
                row[1], label=f'{row[0]} success detail keys'),
        )
        for row in rows
    )
    assert documented == GCS_SUCCESS_DETAIL_ROWS


@pytest.mark.parametrize('document_name', ('README', 'example'))
def test_gcs_list_item_schema_is_exact(document_name: str) -> None:
    """README and example freeze every normalized one-page list item key.

    Args:
        document_name: Contract document under test.
    """
    header, rows = document_table(
        gcs_contract_document(document_name),
        '**GCS list item schema.**',
    )
    assert header == ('Schema', 'Exact keys')
    assert len(rows) == 1 and rows[0][0] == 'list item'
    keys = _comma_separated_keys(rows[0][1], label='GCS list item keys')
    assert keys == GCS_LIST_ITEM_KEYS


@pytest.mark.parametrize('document_name', ('README', 'example'))
def test_gcs_signed_put_header_surfaces_are_exact(
    document_name: str,
) -> None:
    """Docs distinguish exact SDK signing headers from client headers.

    Args:
        document_name: Contract document under test.
    """
    header, rows = document_table(
        gcs_contract_document(document_name),
        '**GCS signed PUT header contracts.**',
    )
    assert header == ('Surface', 'Name', 'Exact value')
    assert len(rows) == 6
    _assert_unique(
        tuple((row[0], row[1]) for row in rows),
        label='GCS signed PUT surface/name keys',
    )
    assert rows == GCS_SIGNED_PUT_HEADER_ROWS


@pytest.mark.parametrize('document_name', ('README', 'example'))
def test_gcs_target_and_public_invocation_are_explicit(
    document_name: str,
) -> None:
    """GCS docs state its target, ADC-only auth, and strict command surface.

    Args:
        document_name: Contract document under test.
    """
    _assert_document_terms(gcs_contract_document(document_name), {
        'gs target': ('gs://bucket',),
        'auth is None': ('auth=None', 'auth` must be `None'),
        'strict unknown-key refusal': ('unknown key', 'strict allowlist'),
        'all five commands': (
            'download, upload, head, list, and signed_url',
            'download`, `upload`, `head`, `list`, and `signed_url',
        ),
    })


@pytest.mark.parametrize('document_name', ('README', 'example'))
def test_gcs_authentication_and_signing_boundaries_are_explicit(
    document_name: str,
) -> None:
    """GCS docs state the credential and IAM trust boundary.

    Args:
        document_name: Contract document under test.
    """
    document = gcs_contract_document(document_name)
    _assert_document_terms(document, {
        'ADC': ('Application Default Credentials', 'ADC'),
        'Workload Identity': ('Workload Identity',),
        'no raw key JSON': (
            'no service-account JSON',
            'rejects service-account JSON',
            'does not accept service-account JSON',
            'no service account JSON',
        ),
        'least privilege': ('least privilege', 'least-privilege'),
        'Token Creator': ('Service Account Token Creator',),
        'signBlob': ('iam.serviceAccounts.signBlob', 'signBlob'),
        'validated impersonation': ('validated impersonation',),
        'direct signer': ('Signing credentials', 'direct Signer'),
    })
    _assert_document_contexts(document, {
        'ADC including Workload Identity is the authentication source': (
            r'authentication uses application default credentials \(adc\)'
            r'.{0,80}(?:including|through) workload identity',
        ),
    })


@pytest.mark.parametrize('document_name', ('README', 'example'))
def test_gcs_execution_and_capacity_boundaries_are_explicit(
    document_name: str,
) -> None:
    """GCS docs state timeout, retry ownership, capacity, and shutdown.

    Args:
        document_name: Contract document under test.
    """
    document = gcs_contract_document(document_name)
    _assert_document_terms(document, {
        'finite timeout': ('finite timeout', 'finite `timeout`'),
        'off-loop provider work': ('off-loop', 'off the event loop'),
        'provider retries disabled': ('retry=None',),
        'gateway breaker ownership': ('gateway circuit breaker',),
        'four workers': ('four-worker', 'four workers'),
        'four leases': ('four-lease', 'four leases'),
        'no admission queue': ('no queue', 'without queuing'),
        'safe telemetry': ('safe telemetry', 'capacity telemetry'),
    })
    _assert_document_contexts(document, {
        'provider and credential work executes off-loop': (
            r'(?:all google )?provider and credential work runs '
            r'(?:off-loop|off the event loop).{0,80}finite timeout',
        ),
        'operational calls disable provider retries for gateway ownership': (
            r'operational calls (?:pass|use) retry=none.{0,80}'
            r'(?:so|because).{0,80}gateway circuit breaker owns '
            r'(?:their )?retries',
        ),
        'signed URLs bypass the breaker': (
            r'signed urls? (?:bypass|bypasses|do not use) '
            r'(?:the )?(?:gateway )?(?:circuit )?breaker',
        ),
        'signed URLs have no gateway retry': (
            r'signed urls?.{0,120}(?:have|use|perform) no gateway '
            r'retr(?:y|ies)',
            r'no gateway retr(?:y|ies).{0,120}signed urls?',
        ),
        'shutdown is idempotent': (
            r'shutdown (?:is )?idempotent',
            r'idempotent shutdown',
        ),
        'shutdown waits for active leases': (
            r'shutdown.{0,160}(?:is deferred|defers|waits).{0,160}'
            r'active leases?.{0,80}(?:drain|finish|reach zero)',
            r'active leases?.{0,80}(?:drain|finish|reach zero).{0,160}'
            r'(?:deferred )?shutdown',
        ),
        'path helpers retain default-executor behavior under the lease': (
            r'path helpers?.{0,160}(?:retain|keep|preserve).{0,100}'
            r'(?:existing )?default[- ]executor behavior.{0,160}'
            r'(?:while|under).{0,80}(?:gcs )?lease.{0,80}'
            r'(?:is |remains )?(?:held|active)',
        ),
    })


@pytest.mark.parametrize('document_name', ('README', 'example'))
def test_gcs_download_and_list_boundaries_are_explicit(
    document_name: str,
) -> None:
    """GCS docs state generation-safe raw ranges and bounded pagination.

    Args:
        document_name: Contract document under test.
    """
    document = gcs_contract_document(document_name)
    _assert_document_terms(document, {
        'create-only upload': ('create-only',),
        'generation pin': ('generation-pinned', 'pinned generation'),
        'inclusive 64-KiB ranges': ('64 KiB', '64-KiB'),
        'raw ranges': ('raw_download=True',),
        'stored bytes': ('stored/raw', 'exact stored bytes'),
        'no CRC claim': ('no CRC', 'does not verify CRC'),
        'one page': ('one-page', 'one page'),
        'max items': ('max_items',),
        'opaque token': ('opaque',),
        'non-empty token': ('non-empty',),
        '4096-byte token limit': ('4096-byte', '4096 byte'),
        'UTF-8 byte measurement': ('UTF-8 bytes', 'UTF-8 byte'),
    })
    _assert_document_contexts(document, {
        'uploads default to a create-only generation precondition': (
            r'uploads default to (?:a )?create-only generation precondition',
            r'uploads are create-only by default',
        ),
        'downloads are generation-pinned raw requests': (
            r'downloads are generation-pinned.{0,180}'
            r'(?:use|using).{0,120}raw_download=true',
        ),
        'listing fetches exactly one bounded page': (
            r'listing (?:fetches|requests|reads) (?:exactly )?one page'
            r'.{0,100}(?:bounded by|max_items)',
        ),
        'transparent decompression is disabled': (
            r'transparent decompression (?:is )?(?:explicitly )?disabled',
            r'disables transparent decompression',
        ),
        'page_token is preserved unchanged without normalization': (
            r'page_token.{0,160}(?:is |remains )?(?:preserved|passed) '
            r'(?:exactly )?unchanged.{0,120}'
            r'(?:without|with no|and no) normaliz',
            r'page_token.{0,160}(?:is not|never) normalized.{0,120}'
            r'(?:preserved|passed).{0,40}(?:exactly|unchanged)',
        ),
    })


@pytest.mark.parametrize('document_name', ('README', 'example'))
def test_gcs_signed_url_boundary_is_explicit(document_name: str) -> None:
    """GCS docs state the exact V4 GET/PUT capability contract.

    Args:
        document_name: Contract document under test.
    """
    document = gcs_contract_document(document_name)
    _assert_document_terms(document, {
        'V4 GET and PUT only': ('V4 GET/PUT', 'V4 `GET` and `PUT`'),
        'relative timedelta': ('timedelta',),
        'expiry range': ('1..3600', '1–3600'),
        'default expiry': ('900',),
        'bearer risk': ('bearer',),
        'revocation limitation': ('revocation', 'cannot revoke'),
        'dedicated content type': ('content_type',),
        'two SDK headers': ('two-entry SDK', 'two SDK'),
        'three client headers': ('three-entry', 'three client'),
        'upload cap': ('max_upload_bytes',),
        'generation header': ('x-goog-if-generation-match',),
    })
    _assert_document_contexts(document, {
        'V4 expiry is a bounded relative duration with the documented '
        'default': (
            r'expires_in_seconds is in 1(?:\.\.|\N{EN DASH})3600'
            r'.{0,80}defaults to 900.{0,140}'
            r'(?:passed|becomes).{0,100}relative timedelta',
        ),
        'signed URLs are bearer capabilities': (
            r'(?:a )?signed urls? (?:are|is) (?:a )?bearer '
            r'capabilit(?:y|ies)',
        ),
        'the gateway cannot revoke signed URLs before expiry': (
            r'gateway cannot revoke (?:(?:it|them) )?before expiry',
        ),
        'Cloud Storage enforcement is provider-side and not live-tested': (
            r'(?:cloud storage (?:service )?enforces|'
            r'(?:cloud storage|service) enforcement is provider-side)'
            r'.{0,220}(?:not live-tested|not live tested|no-live-gcp)',
            r'(?:not live-tested|not live tested|no-live-gcp).{0,220}'
            r'(?:cloud storage (?:service )?enforces|'
            r'(?:cloud storage|service) enforcement is provider-side)',
        ),
    })


@pytest.mark.parametrize('document_name', ('README', 'example'))
def test_gcs_out_of_scope_boundary_is_explicit(document_name: str) -> None:
    """GCS docs retain the frozen excluded-capability inventory.

    Args:
        document_name: Contract document under test.
    """
    header, rows = document_table(
        gcs_contract_document(document_name),
        '**GCS out-of-scope capabilities.**',
    )
    assert header == ('Capability', 'Status')
    assert len(rows) == 7
    _assert_unique(
        tuple(row[0] for row in rows),
        label='GCS out-of-scope capability labels',
    )
    assert rows == GCS_OUT_OF_SCOPE_ROWS


def test_pe80_grpc_status_table_is_exact() -> None:
    """Every grpcio status has its frozen public HTTP-shaped status."""
    documented = {
        row[0]: int(row[1])
        for row in table_rows('**gRPC status map.**')
    }
    actual = {status.name: code for status, code in GRPC_HTTP_STATUS.items()}
    assert documented == actual


def test_pe80_central_status_table_covers_every_registered_selector() -> None:
    """The central status guide cannot silently omit a registry selector."""
    situations = ' '.join(
        row[0] for row in table_rows('### `status_code` per protocol'))
    for selector in PROTOCOL_SELECTOR_CLASSES:
        assert selector in situations


def test_pe80_error_boundary_names_every_new_selector_validation() -> None:
    """Escaping configuration guidance covers every additive boundary."""
    boundary = prose_after('The errors that escape this way are:')
    request_docs = inspect.getdoc(request) or ''
    for selector in ('JSONRPC', 'GRAPHQL', 'S3', 'GRPC', 'GCS'):
        assert selector in boundary
    for term in (
        'unknown keys', 'required keys', 'URL', 'auth', 'payload', 'command',
        'before dispatch',
    ):
        assert term in boundary
    assert '**Everything else is an `ok=False` envelope**' not in boundary
    for guidance in (boundary, request_docs):
        assert 'four deferred FTP/SFTP option checks' in guidance
        assert 'S3 credential-provider discovery' in guidance
        assert 'only during the operation' in guidance
        assert 'CONFIG' in guidance and '400' in guidance
        assert 'those four and nothing else' not in guidance
        assert re.search(r'whole\s+of that set', guidance) is None


def test_pe80_rest_is_documented_as_http_usage_not_a_selector() -> None:
    """REST remains a usage style for HTTP/HTTPS and never a registry row."""
    assert 'REST' not in protocol_mapping
    rest = prose_after('### REST')
    assert 'ordinary `HTTP`/`HTTPS` usage' in rest
    assert 'not a selector' in rest


def test_pe80_retry_warnings_name_replay_ownership_and_risks() -> None:
    """The retry prose states each new transport's exact replay contract."""
    retries = prose_after('### New selector retry rules')
    for selector in ('JSONRPC', 'GRAPHQL'):
        assert selector in retries
    assert 'duplicate delivery' in retries
    assert '`allowed_retries=N`' in retries and '`N+1`' in retries
    assert '`total_max_attempts=1`' in retries
    assert 'SDK retries are disabled' in retries
    for status in (
        'UNKNOWN', 'DEADLINE_EXCEEDED', 'INTERNAL', 'UNAVAILABLE',
    ):
        assert status in retries
    assert 'RESOURCE_EXHAUSTED' in retries
    assert 'abortable' in retries and 'uncounted' in retries


def test_pe80_s3_helper_migration_contract_is_mechanical() -> None:
    """The hardened helper migration preserves every compatibility default."""
    section = prose_after('### S3 helper migration')
    assert '`overwrite=True`' in section
    assert '`max_response_bytes=None`' in section
    assert 'overwrite by default' in section
    assert 'uncapped by default' in section
    assert 'keyword-only' in section
    assert 'shared' in section
    assert '`overwrite=False`' in section
    assert 'mandatory positive cap' in section


def test_pe80_rejected_capability_inventory_is_complete() -> None:
    """Unsupported escape hatches stay explicit and selector-scoped."""
    rejected = prose_after('### Rejected capabilities')
    for term in (
        'request_type', 'file transfer', 'session', 'serializer', 'redirect',
        'allowed-scheme', 'port override', 'cross-origin',
        'endpoint override', 'arbitrary SDK', 'channel option', 'compression',
        'credentials', 'custom roots', 'reflection', 'method-shape',
    ):
        assert f'`{term}`' in rejected or term in rejected


def test_pe80_s3_success_detail_schemas_are_exact() -> None:
    """README consumers can rely on the four closed S3 detail shapes."""
    documented = {
        row[0]: frozenset(part.strip() for part in row[1].split(',') if part)
        for row in table_rows('**S3 success detail schemas.**')
    }
    assert documented == {
        'download': frozenset({
            'command', 'bucket', 'key', 'local_path', 'bytes_written', 'etag',
        }),
        'upload': frozenset({
            'command', 'bucket', 'key', 'local_path', 'bytes_read', 'etag',
        }),
        'head': frozenset({
            'command', 'bucket', 'key', 'content_length', 'content_type',
            'etag', 'last_modified', 'metadata',
        }),
        'list': frozenset({
            'command', 'bucket', 'prefix', 'items', 'key_count',
            'is_truncated', 'next_continuation_token',
        }),
    }


def test_pe80_changelog_has_one_complete_unreleased_section() -> None:
    """Protocol expansion and P0 corrections share one unreleased record."""
    changelog = (REPO_ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    unreleased = changelog.partition('## [Unreleased]')[2]
    assert unreleased
    unreleased = unreleased.partition('\n## ')[0]
    assert re.findall(r'^### (.+)$', unreleased, re.MULTILINE) == [
        'Added', 'Changed', 'Fixed', 'Security',
    ]
    for selector in ('JSON-RPC 2.0', 'GraphQL', 'S3', 'gRPC'):
        assert selector in unreleased
    assert re.search(r'\b20\d{2}-\d{2}-\d{2}\b', unreleased) is None


def test_gcs_changelog_is_additive_without_delivery_claims() -> None:
    """The unreleased record names GCS and its dependency, but no release."""
    changelog = (REPO_ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    unreleased = changelog.partition('## [Unreleased]')[2]
    assert unreleased, 'CHANGELOG has no Unreleased section'
    unreleased = unreleased.partition('\n## ')[0]
    added = unreleased.partition('### Added')[2].partition('\n### ')[0]
    gcs_entries = [
        entry.strip()
        for entry in re.split(r'^- ', added, flags=re.MULTILINE)
        if 'GCS' in entry
    ]

    assert len(gcs_entries) == 1, (
        f'expected one additive GCS changelog entry, found {gcs_entries}')
    entry = gcs_entries[0]
    assert 'google-cloud-storage>=3,<4' in entry
    forbidden_patterns = {
        'SIT': r'\bSIT\b',
        'deployment': r'\bdeploy(?:s|ed|ing|ment|ments)?\b',
        'Kubernetes': r'\bKubernetes\b',
        'GKE': r'\bGKE\b',
        'tag': r'\b(?:tags?|tagged|tagging)\b',
        'CI': r'\bCI\b',
        'release': r'\breleas(?:e|es|ed|ing)\b',
        'shipping': r'\bship(?:s|ped|ping)?\b',
        'publication': r'\b(?:publish(?:es|ed|ing)?|publication)\b',
    }
    forbidden = {
        claim for claim, pattern in forbidden_patterns.items()
        if re.search(pattern, entry, re.IGNORECASE)
    }
    assert not forbidden, (
        f'GCS changelog entry makes delivery claims: {sorted(forbidden)}')


def test_pe80_install_and_request_arguments_cover_new_transports() -> None:
    """Install and boundary docs name the dependency and payload meanings."""
    install = prose_after('## Install')
    assert '`grpcio>=1.83.0,<2`' in install
    assert '`google-cloud-storage>=3,<4`' in install
    arguments = {
        row[0]: row[1]
        for row in table_rows('### `request()`')
    }
    assert 'JSON-RPC/GraphQL' in arguments['data']
    assert 'None' in arguments['data']
    assert 's3://bucket/key' in arguments['url']
    assert 'grpc://host:port' in arguments['url']
    assert 'grpcs://host:port' in arguments['url']
    assert 'gs://bucket' in arguments['url']
    assert 'AWS credential chain' in arguments['auth']
    assert 'gRPC' in arguments['auth'] and 'must be None' in arguments['auth']
    assert 'GCS' in arguments['auth'] and 'must be None' in arguments['auth']
    for selector in PROTOCOL_SELECTOR_CLASSES:
        assert selector in arguments['protocol']


def test_pe80_new_quickstarts_are_discoverable_and_runnable_locally() -> None:
    """Each additive selector has a quickstart with no live dependency."""
    quickstarts = prose_after('## Quickstart per protocol')
    for selector in ('JSON-RPC 2.0', 'GraphQL', 'S3', 'gRPC', 'GCS'):
        assert f'### {selector}' in README_TEXT
    for script in (
        'jsonrpc_example.py', 'graphql_example.py', 's3_example.py',
        'grpc_example.py', 'gcs_example.py',
    ):
        assert f'python examples/{script}' in quickstarts
    assert 'loopback' in quickstarts
    assert 'deterministic SDK double' in quickstarts
    assert 'local generic server' in quickstarts
    assert (
        'no live GCP' in quickstarts
        or 'does not contact GCP' in quickstarts
    )


def test_pe80_s3_and_grpc_security_boundaries_are_explicit() -> None:
    """Credential, metadata, target, and unary-only limits are documented."""
    security = prose_after('### S3 and gRPC security boundaries')
    for phrase in (
        'normal AWS credential chain', 'access key', 'secret access key',
        'credential-provider', 'continuation token', 'redacted',
        '`grpc://host:port`', '`grpcs://host:port`', 'unary-unary',
        'platform roots',
        '64', '8192', '32768', 'base64',
    ):
        assert phrase in security


def test_pe80_docker_claim_distinguishes_live_and_doubled_protocols() -> None:
    """Docker docs do not imply AWS or gRPC infrastructure that is absent."""
    docker = prose_after('### Docker:')
    assert 'HTTP/HTTPS' in docker
    assert 'FTPS' in docker and 'SFTP' in docker and 'SOAP' in docker
    assert 'JSON-RPC' in docker and 'GraphQL' in docker
    assert 'S3' in docker and 'gRPC' in docker
    assert 'GCS' in docker
    assert 'loopback' in docker
    assert 'SDK double' in docker
    assert 'local generic server' in docker
    assert 'does not contact AWS' in docker
    assert 'does not contact GCP' in docker


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
    from asyncio_gateway.utils import exceptions

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
    section = prose_after('## Redaction and the payload echo')
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
    section = prose_after('### SOAP').partition(' ## Public API')[0]
    assert section, 'the SOAP section is empty'

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
    section = prose_after('## You own URL validation')
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
    section = prose_after('### Cutting a release')
    assert 'exactly one file' in section
    assert 'pyproject.toml' in section
    assert 'setup.py' not in section
    assert 'setup.cfg' not in section


def test_the_licence_and_attribution_are_stated() -> None:
    """Check the README states the licence and the copyright attribution."""
    section = prose_after('## Licence and attribution')
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
    section = prose_after('## Supported Python versions')
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
    section = prose_after('### Timeouts')
    assert 'whole exchange' in section
    assert 'redirect chain included' in section


def test_the_local_write_contract_is_documented() -> None:
    """Check the download overwrite, mode and symlink rules are all stated."""
    flat = prose_after('## Local files')
    for claim in (
        'refuse to overwrite by default',
        'overwrite=True',
        'O_EXCL',
        'O_NOFOLLOW',
        '0600',
        'symbolic link',
        'process working directory',
    ):
        assert claim in flat, (
            f'the local-write contract does not state {claim!r}')


def test_the_traceback_logging_tradeoff_is_documented() -> None:
    """Check the README discloses the ``exc_info`` trade and its revert.

    The library renders and redacts the traceback itself rather than emitting
    ``exc_info=True``, which costs native APM exception grouping. That is a
    consumer-visible cost, so it is documented as a choice with a stated
    revert rather than left to be discovered in production.
    """
    section = prose_after('## Logging')
    assert "extra['traceback']" in section
    assert 'exc_info' in section
    assert 'group' in section, (
        'the README does not state the APM grouping this costs')


# --------------------------------------------------------------------------
# The predecessor disclosure. This release was written on the premise that
# the package had never been published -- verified against the *new* name,
# which 404s because it is new. The code ships as `asyncio-requests 2.7.3`
# with real current users, and that distribution still carries the defects
# fixed here. These tests pin the corrected claim so the docs cannot drift
# back to the comfortable one.
# --------------------------------------------------------------------------

#: The published predecessor's distribution name.
PREDECESSOR = 'asyncio-requests'

#: The defects live in the published predecessor, as `file:line` anchors the
#: advisory must name. Keyed by the shorthand used in the failure message.
ADVISORY_ANCHORS = {
    'SFTP host-key verification': 'logic/sftp.py:42',
    'the FTP UnboundLocalError': 'logic/ftp.py:50-52',
    'the zero-byte SOAP module': 'logic/soap.py',
}


def test_no_document_claims_the_package_was_never_published() -> None:
    """Check the false premise is not asserted in the README or CHANGELOG.

    The claim is false: this code is published as ``asyncio-requests``. A
    struck-through or quoted historical record is fine and expected in the
    specs, which keep their corrections visible -- but the two documents a
    consumer actually reads must not assert it in their own voice.
    """
    claim = re.compile(
        r'never (?:been )?published|zero (?:installed |published )?users',
        re.IGNORECASE)
    offenders = [
        f'{path.name}:{number}'
        for path in (README, REPO_ROOT / 'CHANGELOG.md')
        for number, line in enumerate(
            path.read_text(encoding='utf-8').splitlines(), start=1)
        if claim.search(line) and '~~' not in line
    ]
    assert not offenders, (
        f'the "never published" claim is asserted at {offenders}; this code '
        f'ships as {PREDECESSOR} and the claim was verified against the new '
        'name, which 404s only because it is new'
    )


def test_the_readme_tells_predecessor_users_how_to_migrate() -> None:
    """Check the README names the predecessor and the import-path change.

    The rename means no resolver carries an ``asyncio-requests`` user across;
    the migration is deliberate, so the instructions have to be present and
    have to name both package names.
    """
    section = prose_after('## Migrating from `asyncio-requests`')
    assert PREDECESSOR in section
    assert 'asyncio_requests' in section and 'asyncio_gateway' in section, (
        'the README does not state the import-path change')
    assert '2.7.3' in section, (
        'the README does not name the version the predecessor is retired at')


def test_the_changelog_advisory_names_every_defect_and_its_anchor() -> None:
    """Check the security advisory is present and names all three defects.

    The advisory exists so the predecessor's current users can act. An
    advisory that omits one of the three, or names it without the
    ``file:line`` that lets a reader confirm it, is not doing that job.
    """
    changelog = (REPO_ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    heading = '### Security advisory'
    assert heading in changelog, 'the CHANGELOG carries no security advisory'
    advisory = changelog.partition(heading)[2].partition('\n### ')[0]
    advisory = ' '.join(advisory.split())

    assert f'{PREDECESSOR} <= 2.7.3' in advisory, (
        'the advisory does not name the affected package and version bound')
    missing = [
        f'{name} ({anchor})'
        for name, anchor in ADVISORY_ANCHORS.items()
        if anchor.replace(' ', '') not in advisory.replace(' ', '')
    ]
    assert not missing, f'the advisory does not anchor: {missing}'
    assert 'migrate' in advisory.lower(), (
        'the advisory states no remedy')


def test_the_readme_links_the_changelog_advisory() -> None:
    """Check the README points at the advisory rather than restating it.

    One statement of the disclosure, linked from the other document, is the
    same anti-drift rule the versioning policy follows.
    """
    assert 'CHANGELOG.md#security-advisory' in README_TEXT, (
        'the README does not link the CHANGELOG security advisory')


# ==========================================================================
# Part B (R30) -- the source-tree documentation and annotation
# standard. Everything below polices ``asyncio_gateway/`` itself and is
# independent of the README checks above.
# ==========================================================================


#: The package this module polices. Everything under it, recursively.
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / 'asyncio_gateway'

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
    in the same ``pyproject.toml`` section suppressed that flag entirely
    until AGW-26 removed it, so the configured strictness checked
    nothing while this test was the only thing holding the property. It
    stays for the same reason it was written. See this module's
    docstring.

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
    """The base and all seven overrides are annotated ``GatewayResponse``.

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

    assert len(found) == 8, (
        f'expected the base plus seven protocol overrides, found '
        f'{len(found)}: {found}')
    assert not wrong, (
        f'handle_request must return GatewayResponse; these do not: '
        f'{wrong}')


def test_typing_text_appears_nowhere() -> None:
    """``typing.Text`` is gone from the package: no import, no use.

    R30's criterion is unconditional -- "``typing.Text`` appears
    nowhere". ``Text`` is a Python 2 compatibility alias that has been
    exactly ``str`` since Python 3.0, so retiring it is a pure
    mechanical substitution with no type change; ``mypy`` reporting
    clean across the substitution is the proof of that.

    This enforces the criterion as written, in the three shapes that
    could reintroduce it:

    * ``from typing import Text`` -- the import itself,
    * ``Text`` used as a name (an annotation, a subscript, a base) --
      which after the substitution can only come from a fresh import,
    * ``Text = ...`` -- a module defining its own alias, which would
      hide a use from a ``grep`` for the import.

    It deliberately walks the AST rather than grepping the text, so
    that the SOAP client's genuine ``Reason/Text`` element name and
    the English word "Text" in prose are not false positives -- they
    are string and comment content, never a ``Name`` node.
    """
    findings: List[Finding] = []

    for path in module_paths():
        tree = parse(path)
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom)
                    and node.module == 'typing'
                    and any(alias.name == 'Text' for alias in node.names)):
                findings.append(Finding(
                    f'{relative(path)}:{node.lineno}', 'Text',
                    'imports typing.Text, which R30 retires; it is an '
                    'alias for str and must be spelled str'))
            elif isinstance(node, ast.Name) and node.id == 'Text':
                findings.append(Finding(
                    f'{relative(path)}:{node.lineno}', 'Text',
                    'uses the name Text as a type; R30 requires str'))
            elif (isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name)
                            and target.id == 'Text'
                            for target in node.targets)):
                findings.append(Finding(
                    f'{relative(path)}:{node.lineno}', 'Text',
                    'module defines its own "Text" alias, which hides a '
                    'typing.Text use from the rename that retires it'))

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

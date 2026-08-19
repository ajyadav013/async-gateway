"""Tests for the response envelope, its clocks and its redaction (Step 5).

Home of Part B's invariants **E1-E11**, which are the contract every later
protocol story is written against: one key set, one success predicate, an
error message that is never empty, a body that survives a failure status,
timestamps in UTC, a latency that cannot go backwards, and no credential
anywhere in what the caller logs or stores.

Covers R8 (all but AC2, which S9 owns, and AC12, the README), R9 in full,
and R10's logger and redaction criteria. Each test names the invariant or
acceptance criterion it proves.
"""

import ast
import asyncio
import importlib
import inspect
import itertools
import json
import logging
import os
import re
import socket
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Iterator
from urllib.parse import urlsplit

from aiohttp import BasicAuth

from failsafe import RetriesExhausted

import pytest

import yarl

from asyncio_gateway import asyncio_gateway as entrypoint
from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.helpers.common import date_helper
from asyncio_gateway.helpers.common.date_helper import (
    elapsed_since,
    monotonic_now,
    utc_now_iso,
)
from asyncio_gateway.helpers.internal.request_helper import (
    DEFAULT_DOWNLOAD_FILEPATH)
from asyncio_gateway.logic import (
    ftp_client, http_client, protocol_mapping, sftp_client, soap_client)
from asyncio_gateway.logic.http_client import (
    HttpRequest, transport_error_for)
from asyncio_gateway.utils import redaction
from asyncio_gateway.utils.contained_io import contained_path_io_factory
from asyncio_gateway.utils.envelope import (
    GatewayResponse,
    finalise_error,
    finalise_ok,
    new_envelope,
)
from asyncio_gateway.utils.exceptions import (
    AsyncGatewayError,
    CircuitOpenError,
    ConnectError,
    GatewayTimeoutError,
    HttpStatusError,
    PathContainmentError,
    SerializationError,
)
from asyncio_gateway.utils.http_file_config import download_file_from_url
from asyncio_gateway.utils.redaction import (
    PAYLOAD_REDACTION_DEPTH,
    REDACTED,
    normalise_param_names,
    redact_cookies,
    redact_headers,
    redact_payload,
    redact_text,
    redact_url,
    redact_value,
)

from tests.fixtures.http_server import RecordingHTTPServer
from tests.fixtures.protocol_transports import (
    CATEGORY_EXEMPT,
    CONTRACT_CALL,
    EXPECTED_CODE,
    FAULT_CATEGORIES,
    LOCAL_IO_CATEGORIES,
    LOCAL_IO_CODE,
    LOCAL_IO_LEAF,
    OVER_CAP_BODY,
    OVER_CAP_CODE,
    OVER_CAP_LIMIT,
    PROTOCOL_FAULTS,
    REMOTE_ENTRY,
    REMOTE_PERMISSIONS,
    WRITE_FAULTS,
    WRITE_ROUTES,
    contract_call,
    install_failing_transport,
    install_transport,
    install_writing_transport,
    local_io_call,
)

#: The loopback path a ``LOCAL_IO`` row's HTTP-family call dials.
LOCAL_IO_PATH: str = '/local-io'

# The envelope's public key set. Named here so that adding a key is a
# deliberate edit to this list rather than something a test silently
# accepts. S9 parametrises the same set across all five protocols.
EXPECTED_KEYS = frozenset({
    'ok',
    'status_code',
    'protocol',
    'url',
    'request_time',
    'latency',
    'payload',
    'text',
    'json',
    'headers',
    'cookies',
    'error',
    'protocol_details',
    'request_tracer',
    'pre_processor_response',
    'post_processor_response',
})

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / 'asyncio_gateway'

JSON_HEADERS = {'Content-Type': 'application/json'}


def package_sources() -> Iterator[tuple[str, str]]:
    """Yield every module in the installed package with its source.

    Returns:
        Pairs of package-relative POSIX path and file contents, so a test
        can make the same assertion a ``grep -rn`` over the package makes.
    """
    for path in sorted(PACKAGE_ROOT.rglob('*.py')):
        yield (
            path.relative_to(PACKAGE_ROOT).as_posix(),
            path.read_text(encoding='utf-8'),
        )


def files_containing(pattern: str) -> list[str]:
    """Return the package modules whose source matches ``pattern``.

    Args:
        pattern: A regular expression applied to each module's source.

    Returns:
        The package-relative paths that match, sorted.
    """
    expression = re.compile(pattern)
    return [
        name for name, source in package_sources()
        if expression.search(source)
    ]


#: Every call by which Python can create, truncate or otherwise write a
#: local file. Matched on the *final* attribute, so ``open``,
#: ``aiofiles.open``, ``os.open`` and ``pathlib.Path(...).write_bytes``
#: are all one entry each and a new spelling of the same syscall
#: (``anyio.open_file``) is caught by the name it ends in.
#:
#: The set is deliberately wider than what the package uses today: it
#: names the seam, not the current call list, so a write introduced
#: through a helper nobody has reached for yet is still a finding rather
#: than a silent omission.
WRITE_SEAMS: frozenset[str] = frozenset({
    'copy',
    'copy2',
    'copyfile',
    'copyfileobj',
    'copytree',
    'fdopen',
    'link',
    'mknod',
    'mkstemp',
    'move',
    'mkdtemp',
    'NamedTemporaryFile',
    'open',
    'open_file',
    'symlink',
    'TemporaryFile',
    'touch',
    'write_bytes',
    'write_text',
})


def dotted_name(node: ast.expr) -> str:
    """Render an attribute chain as its dotted source spelling.

    Args:
        node: The ``func`` expression of a call.

    Returns:
        ``'aiofiles.open'`` for ``aiofiles.open``, or the bare final
        attribute for a chain not rooted in a plain name
        (``Path(p).write_bytes`` renders as ``write_bytes``), so a write
        reached through a temporary is still classified by what it does.
    """
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return '.'.join(reversed(parts)) if parts else ''


def _reads_only(call: ast.Call) -> bool:
    """Report whether ``call`` is an open whose mode cannot write.

    Only a *literal* read mode counts. A mode computed at runtime --
    ``open(path, mode)`` in :func:`~asyncio_gateway.utils.contained_io.
    _open_guarded`, where ``mode`` is whatever ``aioftp`` or ``asyncssh``
    asked for -- is treated as a write, because it can be one.

    Args:
        call: The call node to inspect.

    Returns:
        True when the second positional argument (or the ``mode``
        keyword) is a string constant containing no writing character.
    """
    mode: object = None
    if len(call.args) > 1 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for keyword in call.keywords:
        if keyword.arg == 'mode' and isinstance(keyword.value, ast.Constant):
            mode = keyword.value.value
    return isinstance(mode, str) and not set(mode) & set('wxa+')


def write_seams_in(source: str) -> list[tuple[int, str, str]]:
    """Find every call in ``source`` that can create or truncate a file.

    Reads are dropped -- an ``open(..., 'rb')`` cannot write -- and so is
    ``os.open`` with ``O_DIRECTORY`` in its flags, which opens a
    directory to walk it. Everything else that names a seam is returned,
    whether or not it looks guarded: deciding *that* is the caller's job
    and the whole point of the census.

    Args:
        source: A module's source text.

    Returns:
        One ``(line, dotted name, enclosing function)`` triple per
        writing call, ordered by line. The enclosing name is the
        innermost ``def``/``async def``/``class`` chain, so a finding
        names the function to look in rather than only a line number.
    """
    found: list[tuple[int, str, str]] = []

    def walk(node: ast.AST, enclosing: str) -> None:
        """Descend ``node``, recording seams against their scope.

        Args:
            node: The subtree to walk.
            enclosing: The dotted name of the innermost enclosing
                definition, or ``''`` at module level.

        Returns:
            None.
        """
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.AsyncFunctionDef,
                                  ast.ClassDef,
                                  ast.FunctionDef)):
                walk(child, f'{enclosing}.{child.name}'
                     if enclosing else child.name)
                continue
            if isinstance(child, ast.Call):
                name = dotted_name(child.func)
                writes = (not _reads_only(child)
                          and not _opens_a_directory(child))
                if name.rsplit('.', 1)[-1] in WRITE_SEAMS and writes:
                    found.append((child.lineno, name, enclosing))
            walk(child, enclosing)

    walk(ast.parse(source), '')
    return sorted(found)


def _opens_a_directory(call: ast.Call) -> bool:
    """Report whether ``call`` is an ``os.open`` of a directory.

    ``open_within`` walks the destination's ancestors a component at a
    time, opening each as a directory descriptor. Those opens create
    nothing -- ``O_DIRECTORY`` refuses anything that is not already a
    directory, and no ``O_CREAT`` is in the flags -- so they are not
    write seams. Recognised by the flag constant appearing anywhere in
    the flags expression rather than by line number, so the exemption
    follows the code if it moves.

    Args:
        call: The call node to inspect.

    Returns:
        True when the call names ``os.open`` and its flags mention a
        directory-open constant.
    """
    if dotted_name(call.func) != 'os.open' or len(call.args) < 2:
        return False
    return any(
        isinstance(node, ast.Name) and node.id in {'DIRECTORY_FLAGS'}
        for node in ast.walk(call.args[1])
    )


def closed_port() -> int:
    """Return a TCP port on the loopback interface with nothing behind it.

    Binding and immediately releasing a port is the cheapest way to get one
    that is free, so a connection to it is refused rather than answered.

    Returns:
        A port number no listener is bound to.
    """
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return int(probe.getsockname()[1])


def contains_self(value: Any, envelope: GatewayResponse) -> bool:
    """Report whether ``envelope`` appears anywhere inside ``value``.

    Args:
        value: The value to search, descended recursively.
        envelope: The envelope that must not be reachable from itself.

    Returns:
        True if the envelope is ``value`` or is nested inside it.
    """
    if value is envelope:
        return True
    if isinstance(value, dict):
        return any(
            contains_self(item, envelope) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(contains_self(item, envelope) for item in value)
    return False


async def http_call(
    url: str,
    *,
    data: Any = None,
    request_type: str = 'GET',
    headers: dict[str, str] | None = None,
) -> GatewayResponse:
    """Drive one HTTP request through the public entry point.

    Args:
        url: The absolute URL to call.
        data: The request payload, or None for an empty one.
        request_type: The HTTP verb to use.
        headers: Request headers, or None for a JSON content type.

    Returns:
        The envelope ``request()`` returned.
    """
    return await request(
        url,
        data={} if data is None else data,
        protocol='HTTP',
        protocol_info={
            'request_type': request_type,
            'headers': dict(headers or JSON_HEADERS),
        },
    )


def render_record(record: logging.LogRecord) -> str:
    """Render every channel one log record can carry a secret through.

    ``log_failure`` renders the traceback itself and carries it in
    ``extra``, so the chained exception's own text reaches the log
    through that field as well as through the message. A redaction
    assertion that reads one attribute proves nothing about the others,
    and the formatter is still run in case a future change puts
    ``exc_info`` back.

    Args:
        record: The captured record to flatten.

    Returns:
        The formatted message and any ``exc_info`` concatenated with the
        record's whole attribute dictionary, so a substring check over
        the result covers message, ``extra`` and traceback alike.
    """
    formatted = logging.Formatter('%(message)s').format(record)
    return f'{formatted} {record.__dict__}'


@pytest.fixture
async def ok_envelope(
    http_server: RecordingHTTPServer,
) -> GatewayResponse:
    """Return the envelope from one successful HTTP call.

    Returns:
        A finalised success envelope from the loopback server.
    """
    http_server.respond(
        '/ok', status=200, body=b'{"value": 1}', headers=JSON_HEADERS)
    return await http_call(http_server.url_for('/ok'))


@pytest.fixture
async def failed_envelope(
    http_server: RecordingHTTPServer,
) -> GatewayResponse:
    """Return the envelope from one HTTP call that failed remotely.

    Returns:
        A finalised failure envelope carrying a 404 and its JSON body.
    """
    http_server.respond(
        '/missing',
        status=404,
        body=b'{"error": "no such thing"}',
        headers=JSON_HEADERS,
    )
    return await http_call(http_server.url_for('/missing'))


# --- E1: one key set -------------------------------------------------------


def test_e1_the_skeleton_carries_every_documented_key() -> None:
    """A fresh envelope already has the whole key set (E1)."""
    envelope = new_envelope(url='http://host/p', protocol='HTTP', payload={})

    assert set(envelope) == EXPECTED_KEYS


async def test_e1_success_and_failure_return_the_same_key_set(
    ok_envelope: GatewayResponse,
    failed_envelope: GatewayResponse,
) -> None:
    """One shape for both paths, so a caller writes one handler (E1).

    S9 widens the same assertion to five protocols; HTTP is the protocol
    Step 5 rewrites, so it is the one that can be asserted here.
    """
    assert set(ok_envelope) == EXPECTED_KEYS
    assert set(failed_envelope) == EXPECTED_KEYS


async def test_e1_a_transport_failure_returns_the_same_key_set() -> None:
    """A failure with no response at all still fills every key (E1)."""
    envelope = await http_call(f'http://127.0.0.1:{closed_port()}/x')

    assert set(envelope) == EXPECTED_KEYS
    assert envelope['headers'] == {}
    assert envelope['cookies'] == {}


# --- R8-AC2: one key set, five protocols, both paths -----------------------

# The five contract rows, shared by the success and the failure test below
# so that a protocol's marker was one line to remove rather than two.
#
# **The list is now unmarked, and that is the criterion, not a tidy-up.**
# Three rows began as `xfail(strict=True)` because their clients did not
# satisfy the contract yet -- which is why these were written before the
# protocol work rather than after it. `strict` is what made a marker a
# ratchet instead of a note: the moment a protocol story fixed its client,
# both of that protocol's rows passed unexpectedly, the suite failed, and
# the marker had to come off in the story that earned it. S10 took FTP,
# S11 took SFTP, and S22 -- writing `logic/soap_client.py` from nothing --
# took the last one. All five protocols satisfy E1 and E11 here.
CONTRACT_ROWS = [
    pytest.param('HTTP', id='HTTP'),
    pytest.param('HTTPS', id='HTTPS'),
    pytest.param('FTP', id='FTP'),
    pytest.param('SFTP', id='SFTP'),
    pytest.param('SOAP', id='SOAP'),
]


@pytest.mark.parametrize('protocol', CONTRACT_ROWS)
async def test_r8_ac2_every_protocol_returns_one_key_set_on_success(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    """One shape for a success, whichever protocol produced it (R8-AC2).

    The criterion a caller actually feels: one handler, written once,
    works for every protocol this library dispatches. Without it each
    protocol is free to invent its own success shape, and the uniform
    envelope this release exists to deliver is uniform only for the
    protocol whoever last touched the code happened to run.

    No socket is opened. Each row drives ``request()`` with only its own
    transport seam doubled, so everything between that seam and the
    caller -- the protocol client's envelope mapping, the entry point's
    single conversion point -- is the real code under test.

    The key set is asserted first because it is the criterion verbatim,
    but it cannot be the only assertion. A client that swallows its own
    failure and returns the skeleton it was handed satisfies
    ``set(result.keys()) == EXPECTED_KEYS`` exactly, while having reached
    neither finaliser -- which is what both currently-broken clients do,
    and what would let their rows pass this test while claiming success
    for a call that failed. So ``ok``/``error`` are asserted too: E1's
    other half is that exactly one of ``finalise_ok`` and
    ``finalise_error`` closes every envelope, and "the success path"
    means nothing if the call never took it.
    """
    install_transport(monkeypatch, protocol, succeeds=True)

    result = await request(**contract_call(protocol))

    assert set(result.keys()) == EXPECTED_KEYS
    assert result['ok'] is True
    assert result['error'] is None


@pytest.mark.parametrize('protocol', CONTRACT_ROWS)
async def test_r8_ac2_every_protocol_returns_one_key_set_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    """The same shape for a failure, whichever protocol failed (R8-AC2).

    The half that rots first. A success path is exercised by every
    example anyone writes, so it stays honest on its own; a refused
    connection over SFTP is exercised by nobody until it happens in
    production, which is where a protocol that reports its failures in a
    shape of its own devising is discovered.

    Every row's transport refuses the connection, so each protocol
    reaches its own failure path rather than a shared pre-dispatch
    rejection -- and reaches it without a socket, because the refusal is
    the double's.

    ``error is not None`` carries the same weight here as ``ok is True``
    does in the success test above, and for the same reason: an envelope
    both broken clients return has the whole key set and a null ``error``
    on a call that failed, so a caller reading ``error`` to find out what
    went wrong learns nothing. The key set alone would accept that.
    """
    install_transport(monkeypatch, protocol, succeeds=False)

    result = await request(**contract_call(protocol))

    assert set(result.keys()) == EXPECTED_KEYS
    assert result['ok'] is False
    assert result['error'] is not None


# --- The tracing half of the same contract, on the same five rows ----------
#
# Three findings in as many review rounds have been the *same* defect: a fix
# landed on one protocol client and its siblings were left behind. M20 (the
# envelope's `request_tracer` assigned on the success path only, so every
# failure reported `[]`) was closed in `logic/http_client.py` and not in
# `logic/soap_client.py`. H17 (per-request trace state bound in `__init__`,
# where concurrent calls share whichever context built the objects) was
# closed in the same module and, again, not in the other. `redact_params`
# on the SOAP tracer was the round before that.
#
# What those have in common is not the bug, it is the *shape*: nothing in
# the suite asserted a tracing property across protocols, so each client
# was only ever held to whatever its own module's tests happened to check,
# and a property fixed in one place stayed broken in the next. The two rows
# below close that by driving the same assertion from `CONTRACT_ROWS` --
# the table the envelope contract above already uses -- so a protocol
# registered later is covered the day it is added to that list, and a fix
# ported to one client and not the others fails here rather than in a
# reviewer's spot-check.
#
# Neither row hardcodes a count. FTP and SFTP attach no tracers at all
# (`aioftp` and `asyncssh` have no equivalent of `aiohttp.TraceConfig`), so
# a row demanding a non-empty tracer would be asserting a feature those
# protocols do not have. Instead each row derives what to expect from the
# client itself -- how many tracers it attached -- which is one rule that
# reads as "none" for FTP and SFTP and as "all of them" for the HTTP
# family, and needs no edit when a protocol grows or loses tracing.
#
# Deriving it that way is not a stylistic preference; it is what makes the
# row bite. A first draft asserted only that the failure path reports as
# many collectors as the success path, and a mutant that deleted the
# envelope assignment from `logic/http_client.py` outright *survived* it:
# with the key never written, both paths report `[]` and the two sides
# agree perfectly. A relation between two paths cannot detect a defect
# that breaks both. The anchor below is external to both.


def attached_tracer_count(protocol: str) -> int:
    """Return how many tracers ``protocol``'s client attaches to a call.

    The objective baseline the trace rows below compare against, read off
    a freshly-constructed client rather than written down here, so the
    expectation tracks the code instead of a table someone has to
    remember to update. A protocol with no tracing support answers 0.

    Args:
        protocol: A protocol name from :data:`CONTRACT_CALL`.

    Returns:
        The number of ``aiohttp.TraceConfig`` objects the client attaches,
        which is the number of collector mappings a call must report.
    """
    call = CONTRACT_CALL[protocol]
    built = protocol_mapping[protocol](
        call['url'],
        BasicAuth('user', 'password'),
        new_envelope(url=call['url'], protocol=protocol, payload={}),
        info=dict(call['protocol_info']),
        redact_params=frozenset(),
    )
    return len(getattr(built, 'trace_config', []))


@pytest.mark.parametrize('protocol', CONTRACT_ROWS)
async def test_m20_a_failed_call_traces_as_richly_as_a_successful_one(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    """A failure keeps the trace it recorded, on every protocol (M20).

    The diagnostics a ``request_tracer`` carries are worth least on the
    call that worked and most on the call that did not -- a refused
    connection's ``on_request_exception``, a hung DNS lookup's
    ``on_dns_resolvehost_start`` with no matching end. A client that
    assigns the key while copying a *response* into the envelope only
    ever reaches that line when a response came back, so it throws the
    trace away in exactly the case someone is reading it.

    Both paths are asserted against :func:`attached_tracer_count` rather
    than against each other, because the two paths are not an honest
    baseline for one another: a client that never writes the key reports
    ``[]`` on both and satisfies any comparison between them. The count
    of tracers the client attached is external to both paths and is the
    number of collector mappings each is obliged to carry -- zero for a
    protocol without tracing, so the row means something for FTP and
    SFTP today and keeps meaning it the day either grows tracers.

    Args:
        protocol: The protocol under test, from the contract table.

    Returns:
        None.
    """
    expected = attached_tracer_count(protocol)

    install_transport(monkeypatch, protocol, succeeds=True)
    succeeded = await request(**contract_call(protocol))

    install_transport(monkeypatch, protocol, succeeds=False)
    failed = await request(**contract_call(protocol))

    assert succeeded['ok'] is True
    assert failed['ok'] is False
    assert len(succeeded['request_tracer']) == expected, (
        f'{protocol} attaches {expected} tracer(s) but reports '
        f'{len(succeeded["request_tracer"])} collector(s) on a successful '
        'call. The envelope is dropping a trace that was recorded.')
    assert len(failed['request_tracer']) == expected, (
        f'{protocol} attaches {expected} tracer(s) but reports '
        f'{len(failed["request_tracer"])} collector(s) on a *failed* call. '
        'A trace assigned only where a response arrived is discarded on '
        'the calls it exists for (M20).')


@pytest.mark.parametrize('protocol', CONTRACT_ROWS)
def test_h17_no_protocol_binds_trace_state_at_construction(
    protocol: str,
) -> None:
    """Per-request trace state is bound per call, not per object (H17).

    ``trace_collectors_for`` binds this call's results into the *running
    task's* context, and a task started by ``asyncio.gather`` gets its
    own copy of that context. Bind at construction and every concurrent
    call's results land in whichever context happened to build the
    objects -- one mapping, shared, reported to every caller.

    Today ``request()`` constructs and awaits inside one task, so the
    bleed is latent rather than live; this row is what keeps it latent.
    It asserts the property at the only moment it is observable without
    concurrency: a freshly-constructed protocol object has bound
    nothing, so there is no shared mapping for a later refactor -- one
    that hoists construction out of the awaiting task -- to hand out.

    Args:
        protocol: The protocol under test, from the contract table.

    Returns:
        None.
    """
    call = CONTRACT_CALL[protocol]
    built = protocol_mapping[protocol](
        call['url'],
        BasicAuth('user', 'password'),
        new_envelope(url=call['url'], protocol=protocol, payload={}),
        info=dict(call['protocol_info']),
        redact_params=frozenset(),
    )

    for attribute in ('trace_collectors', 'reported_collectors'):
        assert getattr(built, attribute, []) == [], (
            f'{protocol} bound {attribute} in __init__. Per-request trace '
            'state on a shared object is the mapping concurrent calls '
            'collide in (H17); bind it inside handle_request.')


# --- The exception half of the same contract, on the same five rows --------
#
# The trace rows above closed the *tracing* class of cross-protocol
# divergence. They did not close the class itself: AGW-R9-1 was a fourth
# instance of the same shape -- a fix applied to one client and not its
# siblings -- and it sailed past them, because what diverged was the
# **transport-exception table** and those rows assert tracer counts.
#
# The reviewer's sharpest evidence was not the SOAP bug. It was that
# deleting `ssl.SSLError` from the *HTTP* client left all 2952 tests
# green: the working side was as unpinned as the broken one, and only the
# specific value happened to be right. So a row that asserted "SOAP also
# catches ssl.SSLError" would fix one cell of a table nothing holds. The
# rows below assert the **property** -- every protocol answers for every
# fault category -- driven from `PROTOCOL_FAULTS`, so a category dropped
# from any client fails here in both directions.
#
# The category vocabulary, the per-protocol faults and the one argued
# exemption live in `tests/fixtures/protocol_transports.py`, next to the
# transport doubles they use.


def _abortable_call(protocol: str, fault: BaseException) -> dict[str, Any]:
    """Return a contract call that aborts on ``fault``'s own class.

    ``abortable_exceptions`` is what makes these rows bite, and it is a
    documented public knob rather than a test-only lever (README's retry
    table). A fault named there propagates out of
    ``CircuitBreakerHelper.run`` **unwrapped**, so it must be matched by
    the client's own catch clause; every other failure arrives inside a
    ``RetriesExhausted``, which every client catches by name and which
    therefore masks a missing family entirely. That masking is exactly
    why AGW-R9-1 survived four review rounds, and driving the rows
    through the same knob is what stops the next one surviving.

    Args:
        protocol: A protocol name from the contract table.
        fault: The exception the transport will raise.

    Returns:
        ``request()`` keyword arguments whose retry policy aborts
        immediately on ``type(fault)``.
    """
    call = contract_call(protocol)
    call['protocol_info']['circuit_breaker_config'] = {
        'retry_config': {
            'name': 'fault-category-guard',
            'allowed_retries': 0,
            'abortable_exceptions': [type(fault)],
        },
    }
    return call


@pytest.mark.parametrize('protocol', CONTRACT_ROWS)
@pytest.mark.parametrize('category', FAULT_CATEGORIES)
async def test_every_protocol_answers_for_every_fault_category(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    category: str,
) -> None:
    """Each protocol maps each wire fault to an envelope (AGW-R9-1).

    The library's central promise is that every failure becomes an
    envelope and only a library bug propagates. A client whose catch
    clause omits a family breaks that promise for one fault on one
    protocol -- and does so invisibly, because the ``RetriesExhausted``
    clause beside it catches the same failure on every path that does
    not abort.

    Asserting the *code* and not merely "some envelope" is what makes
    the row a classification test rather than a smoke test: a client
    that caught every fault and reported all of them as ``TRANSPORT``
    would satisfy "an envelope came back" while destroying the
    distinction a caller retries on.

    Args:
        monkeypatch: The pytest patcher.
        protocol: The protocol under test, from the contract table.
        category: The fault category under test.

    Returns:
        None.
    """
    exempt = CATEGORY_EXEMPT.get(category, {})
    if protocol in exempt:
        pytest.skip(f'{protocol}/{category}: {exempt[protocol]}')

    build = PROTOCOL_FAULTS[protocol][category]
    install_failing_transport(monkeypatch, protocol, build)

    result = await request(**_abortable_call(protocol, build()))

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == EXPECTED_CODE[category], (
        f'{protocol} reported {result["error"]["code"]} for a '
        f'{category} fault, which every protocol must classify as '
        f'{EXPECTED_CODE[category]}. A family missing from this '
        "client's table is the divergence AGW-R9-1 was.")


#: The ``protocol_info`` keys SOAP is documented as *not* honouring,
#: with the reason each is excluded. The README states the parity claim
#: -- "SOAP reuses the HTTP transport, so every HTTP key above applies
#: except..." -- and it was prose only, so a fourth exception could
#: appear without anything noticing. One did: ``cross_origin_headers``
#: was passed as ``validated_session``'s ``frozenset()`` default and
#: silently ignored, so a bare string, ``42`` and a list naming
#: ``Authorization`` were all refused on HTTP and **accepted** on SOAP
#: (NEW-R10-3).
SOAP_EXCEPTED_KEYS: frozenset[str] = frozenset({
    # Always POST: a SOAP call's verb is not the caller's to choose.
    'request_type',
    # The two file-transfer configs. MTOM is out of scope (R18), so a
    # multipart body is refused rather than written and SOAP hands the
    # transport a literal None -- which is also its LOCAL_IO exemption.
    'http_file_download_config',
    'http_file_upload_config',
})


#: How a client can name a ``protocol_info`` key: read it, or refuse it
#: by name. Three spellings, because a key reached through a shared
#: helper is named at the *call site* -- ``validated_serialization(
#: self.info, ...)`` -- rather than subscripted in the constructor, and
#: a pattern set that saw only ``self.info.get('key')`` was blind to
#: exactly the keys that had drifted (NEW-R10-5).
_KEY_PATTERNS: tuple[str, ...] = (
    # The ordinary read.
    r"self\.info\.get\(\s*'([a-z_]+)'",
    # A membership test -- how a key whose *presence* is the conflict is
    # detected, and how an explicit refusal is spelled.
    r"'([a-z_]+)'\s+in\s+self\.info",
    # The key a shared validator owns, named by the validator this
    # client calls. `validated_serialization` is the whole of how
    # `serialization` is reached, so calling it *is* honouring the key
    # and not calling it is ignoring it -- which is the difference the
    # first two patterns could not see.
    r'validated_(serialization|trace_config)\(',
)


def _info_keys(client: Any) -> frozenset[str]:
    """Return the ``protocol_info`` keys ``client`` reads or refuses.

    Read off the source rather than a declared list, because a declared
    list is the thing that goes stale: the point is to catch a key the
    code reads or fails to read, and only the code can say which.

    Args:
        client: The protocol class to inspect.

    Returns:
        Every ``protocol_info`` key its constructor names, whether by
        reading it, by testing for it, or by calling the validator that
        owns it.
    """
    source = inspect.getsource(client)
    return frozenset(
        name
        for pattern in _KEY_PATTERNS
        for name in re.findall(pattern, source))


def test_soap_honours_every_http_key_it_does_not_document_an_exception_for(
) -> None:
    """The README's SOAP parity claim, asserted instead of stated.

    "SOAP reuses the HTTP transport, so every HTTP key above applies
    except ``request_type`` and the two file-transfer configs" was
    prose, and prose does not fail. NEW-R10-3 is what that costs: a
    fourth exception appeared -- ``cross_origin_headers``, read by
    ``HttpRequest`` and simply not read by ``SoapRequest`` -- and the
    key it silently ignored is the one that *widens a security guard*,
    so a caller naming ``Authorization`` got a refusal on one protocol
    and silence on the other.

    Both directions are checked. A key HTTP reads and SOAP does not is
    an undocumented exception; a key in the exception list that SOAP now
    reads is a stale exception. Either way the table and the code have
    diverged, which is the only condition this row exists to catch.

    A key SOAP **refuses** by name counts as named, and that is the
    intended reading rather than a loophole. What the guard forbids is
    *silence*: a caller passing a key that does nothing and is told
    nothing. Honouring it and refusing it are both honest answers;
    ignoring it is the defect, and `serialization` is the key that
    proves the distinction matters -- it can never apply to a SOAP body,
    so refusal is the only correct answer and silence was the shipped
    one (NEW-R10-5). `SOAP_EXCEPTED_KEYS` is therefore for keys SOAP
    does not mention at all, which is why a key it refuses must *not*
    be listed there.
    """
    http_keys = _info_keys(http_client.HttpRequest)
    soap_keys = _info_keys(soap_client.SoapRequest)

    unhonoured = http_keys - soap_keys - SOAP_EXCEPTED_KEYS
    assert not unhonoured, (
        f'SOAP silently ignores {sorted(unhonoured)}, which HttpRequest '
        'reads and the README promises SOAP honours. Wire it through, '
        'or add it to SOAP_EXCEPTED_KEYS *and* to the README with the '
        'reason -- an undocumented fourth exception is NEW-R10-3.')

    stale = SOAP_EXCEPTED_KEYS & soap_keys
    assert not stale, (
        f'{sorted(stale)} is listed as a SOAP exception and SOAP now '
        'reads it, so the list and the README are stale.')


#: The four dispatch sites, each with the classification table its
#: ``except`` clause must be derived from. The round-9 fix claimed
#: divergence was "no longer representable" because the clause was
#: derived from the table -- but the derivation reached only the two
#: HTTP-family modules, and ``logic.ftp_client`` and
#: ``logic.sftp_client`` still kept hand-written tuples beside tables of
#: their own. Two of four is how the same class of gap survives a fix
#: that was supposed to end it, so the arity is asserted rather than
#: described (NEW-R10-1).
DISPATCH_SITES: tuple[tuple[str, Any, Any], ...] = (
    ('http_client', http_client.TRANSPORT_ERRORS,
     http_client.TRANSPORT_FAULTS),
    ('soap_client', http_client.TRANSPORT_ERRORS,
     soap_client.TRANSPORT_FAULTS),
    ('ftp_client', ftp_client.TRANSPORT_ERRORS,
     ftp_client.TRANSPORT_FAULTS),
    ('sftp_client', sftp_client.TRANSPORT_ERRORS,
     sftp_client.TRANSPORT_FAULTS),
)


@pytest.mark.parametrize(
    'module, table, faults',
    [pytest.param(*row, id=row[0]) for row in DISPATCH_SITES],
)
def test_every_dispatch_site_catches_exactly_what_it_classifies(
    module: str,
    table: Any,
    faults: Any,
) -> None:
    """A client's catch clause is derived from its own table, at all four.

    The property the round-9 fix named and delivered to half the code:
    a family added to a classification table is caught by the client
    that maps through it, in the same commit, with no second edit to
    remember. Where the clause is hand-written instead, the two drift --
    which is AGW-R9-1 (``ssl.SSLError`` classified and not caught) and
    its FTP sibling (``AIOFTPException`` caught and not classified), the
    same defect from opposite directions.

    Asserted structurally rather than behaviourally, and that is the
    point of this row. A behavioural test can only reach a family some
    transport actually raises on some code path; the families most
    likely to be dropped are exactly the ones no current path produces,
    so they fall out of a derived tuple in silence. Comparing the tuple
    to its table catches the drop whether or not anything raises it.

    Args:
        module: The dispatch site's module name, for the failure text.
        table: Its ordered classification table.
        faults: The tuple its ``except`` clause is built from.

    Returns:
        None.
    """
    classified = tuple(family for family, _ in table)
    missing = [f.__name__ for f in classified if f not in faults]

    assert not missing, (
        f'logic.{module} classifies {missing} in its table and does not '
        f'catch them, so each reaches the caller raw whenever it '
        f'arrives unwrapped -- through abortable_exceptions, which is a '
        f'documented public knob. Derive the clause with '
        f'exceptions.faults_of(TRANSPORT_ERRORS) instead of restating '
        f'it; that is what makes the pair impossible to desynchronise.')


def classification_table_sites() -> tuple[str, ...]:
    """Return every module that declares its own classification table.

    **Discovered, not listed**, and that is the whole of AGW-N1's
    second half. The row below used to run against a hand-written
    ``('ftp_client', 'sftp_client')`` justified by a real-sounding
    argument -- only those two end their table in a residual
    ``OSError``, so only those two can *mis-file* a timeout as a local
    disk fault. The argument was true and the scope it produced was
    wrong: it selected on how bad the consequence is, when the property
    being guarded is whether the table names both classes at all. The
    HTTP family's table does not mis-file an unmatched timeout, it
    fails to file it -- ``transport_error_for`` re-raises, and a raw
    ``TimeoutError`` leaves ``request()`` un-enveloped. That is the
    worse outcome, and the guard's own scope note is what excused it.

    So the set comes off the filesystem. A fourth table cannot opt out
    by not being thought of, and a table deleted from a module drops
    out here rather than leaving a row asserting nothing.

    Returns:
        The ``logic`` module names declaring a ``TRANSPORT_ERRORS``
        table, sorted.
    """
    return tuple(sorted(
        path.stem for path in (PACKAGE_ROOT / 'logic').glob('*.py')
        if re.search(
            r'^TRANSPORT_ERRORS', path.read_text(encoding='utf-8'),
            re.MULTILINE)
    ))


def test_the_timeout_guard_covers_every_classification_table() -> None:
    """The arity, so a table cannot be guarded by not being listed.

    The sibling of
    :func:`test_the_derivation_reaches_every_dispatch_site`, and here
    for the same reason: the row below holds each *discovered* site to
    its table, and nothing in it would notice the discovery itself
    silently returning fewer sites than there are tables. AGW-N1 was
    two-of-three; this asserts three-of-three against a count taken a
    different way.

    Returns:
        None.
    """
    tables = [
        name for name, source in package_sources()
        if name.startswith('logic/')
        and re.search(r'^TRANSPORT_ERRORS', source, re.MULTILINE)
    ]

    assert len(classification_table_sites()) == len(tables), (
        f'{len(tables)} modules declare a TRANSPORT_ERRORS table and the '
        f'timeout guard discovered {len(classification_table_sites())}. '
        f'A table the guard cannot see is AGW-N1: the fix landed on two '
        f'of the three tables because the third was not in scope.')


@pytest.mark.parametrize('module', classification_table_sites())
def test_both_timeout_classes_are_named_in_every_table(
    module: str,
) -> None:
    """``asyncio.TimeoutError`` and ``TimeoutError``, spelled out.

    They are the same object from **3.11**. On 3.10 -- which
    ``requires-python`` admits and the CI matrix claims -- they are
    unrelated classes, so a table naming only the ``asyncio`` one does
    not match the *builtin* a socket read raises. What happens next
    depends on the table's tail, and both outcomes are failures of the
    same contract:

    * where the table ends in a residual ``OSError`` row (FTP, SFTP)
      the builtin -- an ``OSError`` -- falls into it, and a slow server
      is reported ``PATH``/400: the caller is told their own disk is at
      fault and the timing-out destination stays out of its breaker.
    * where it does not (the HTTP family, deliberately) the builtin
      matches nothing, ``transport_error_for`` re-raises it, and a raw
      ``TimeoutError`` leaves ``request()`` un-enveloped -- the one
      thing this library promises cannot happen (AGW-N1).

    The second is the worse of the two, and it is the one the earlier
    version of this row excluded from its scope: it was parametrised
    over a hand-written ``('ftp_client', 'sftp_client')`` on exactly the
    argument above, that only an ``OSError``-tailed table can *mis-file*
    a timeout. True, and the wrong selector -- the property is whether
    the table names both classes, not how badly it behaves when it does
    not. :func:`classification_table_sites` now discovers the set.

    **Asserted against the source text, which is the only thing that
    can be.** Every runtime form of this question -- ``is``,
    ``issubclass``, raising one and catching the other -- is answered by
    the interpreter running the suite, and on 3.11+ every one of them
    says the table is fine whatever it names. That is how this shipped
    twice: green on 3.12, 3.13 and 3.14 both times, broken on the one
    leg nobody had run. It is also why the cross-protocol fault-category
    guard's ``TIMEOUT`` row could not catch AGW-N1 -- it raises
    ``asyncio.TimeoutError()``, which on 3.11+ *is* the builtin, so on
    every interpreter that guard passes: on 3.11+ because the two names
    are one class, and on 3.10 because the class it raises is the one
    name the table did have. Reading the table as text asks the same
    question on every interpreter.

    ``circuit_breaker_helper.RETRIABLE_FAILURES`` already named both,
    with a comment explaining why, so the knowledge was in the codebase
    and had not been applied where it also mattered.

    Args:
        module: The dispatch site's module name.

    Returns:
        None.
    """
    source = (PACKAGE_ROOT / 'logic' / f'{module}.py').read_text(
        encoding='utf-8')
    table = source.split('TRANSPORT_ERRORS')[1].split(')\n\n')[0]

    for spelling in ('(asyncio.TimeoutError, GatewayTimeoutError)',
                     '(TimeoutError, GatewayTimeoutError)'):
        assert spelling in table, (
            f'the TRANSPORT_ERRORS table in logic.{module} does not name '
            f'{spelling}. On Python 3.10 the two timeout classes are '
            f'unrelated, so a timeout arriving as the one that is '
            f'missing either falls through to a residual (OSError, '
            f'LocalWriteError) row and reports PATH/400, or matches no '
            f'row at all and escapes request() raw.')


def test_the_derivation_reaches_every_dispatch_site() -> None:
    """The count itself, so a fifth site cannot quietly opt out.

    The row above holds each *listed* site to its table; nothing in it
    notices a site that was never listed. That is precisely how the
    round-9 fix came to cover two of four -- the two that were looked
    at. The registry is the whole set of protocols, so the site list is
    checked against it rather than against itself.
    """
    covered = {module for module, _, _ in DISPATCH_SITES}
    expected = {
        f'{protocol.lower()}_client' for protocol in protocol_mapping
    } - {'https_client'}

    assert expected <= covered, (
        f'{sorted(expected - covered)} dispatch(es) over a transport '
        'and are not held to the derivation, which is the two-of-four '
        'gap NEW-R10-1 found one round after it was declared closed.')


@pytest.mark.parametrize('protocol', CONTRACT_ROWS)
@pytest.mark.parametrize('category', LOCAL_IO_CATEGORIES)
async def test_every_protocol_answers_for_a_local_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    protocol: str,
    category: str,
) -> None:
    """A non-wire failure is one code on every protocol (R10-1, R10-2).

    The second axis of the cross-protocol guard, and the one whose
    absence let four protocols answer the same question four ways. The
    rows above are all *wire* faults, raised at the transport seam;
    nothing asked what happens when the transport succeeds and the
    **local filesystem** refuses the download it produced. Measured
    before the fix, against real loopback servers: HTTP handed the
    caller a raw ``FileNotFoundError``, FTP said ``CONNECT``, SFTP said
    ``CONNECT``, and a directory destination said ``CONFIG``.

    The transport is made to **succeed** here, which is the whole
    difference from the wire-fault row above -- a refusing transport
    never reaches a disk at all. The HTTP family dials
    the real loopback server rather than a double, because its write
    lives several frames below the seam the doubles replace and a
    doubled transport would skip the code the row is about.

    Args:
        monkeypatch: The pytest patcher.
        http_server: The loopback server, for the HTTP-family rows.
        tmp_path: The test's temporary directory.
        protocol: The protocol under test, from the contract table.
        category: The non-wire category under test.

    Returns:
        None.
    """
    exempt = CATEGORY_EXEMPT.get(category, {})
    if protocol in exempt:
        pytest.skip(f'{protocol}/{category}: {exempt[protocol]}')

    over_cap = category == 'OVER_CAP'
    # An OVER_CAP row's destination must be perfectly writable: the
    # refusal it asserts is the *ceiling*, and a path the filesystem
    # would reject anyway would satisfy the row for the wrong reason.
    destination = str(
        tmp_path / 'out.bin' if over_cap else tmp_path / LOCAL_IO_LEAF)
    call = local_io_call(protocol, destination)
    if over_cap:
        call['protocol_info']['max_response_bytes'] = OVER_CAP_LIMIT
    if protocol == 'HTTPS':
        # `HTTPS` shares `HttpRequest` with `HTTP` and differs only in
        # refusing a plaintext URL (R11-AC4), so pointing it at the
        # plaintext loopback server would fail this row at the scheme
        # check -- before a byte was written -- and prove nothing. The
        # `HTTP` row above exercises the identical write path.
        pytest.skip(
            'HTTPS requires an https:// URL and shares HttpRequest with '
            'HTTP, whose live row covers the same local write path.')
    body = OVER_CAP_BODY if over_cap else b'{"value": 1}'
    if protocol == 'HTTP':
        http_server.respond(LOCAL_IO_PATH, body=body)
        call['url'] = http_server.url_for(LOCAL_IO_PATH)
    else:
        install_writing_transport(monkeypatch, protocol, body)

    result = await request(**call)

    expected = OVER_CAP_CODE if over_cap else LOCAL_IO_CODE
    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == expected, (
        f'{protocol} reported {result["error"]["code"]} for a '
        f'{category} failure, which every protocol that writes locally '
        f'must classify as {expected}. LOCAL_IO: a local disk is not '
        'evidence the remote is unhealthy, and four protocols answered '
        'four ways (NEW-R10-1). OVER_CAP: R14 states the ceiling '
        'globally and two protocols ignored it, returning ok=True '
        'having written the whole 256 KiB body (NEW-R10-2).')
    assert not Path(destination).exists(), (
        'a refused write must leave no file behind -- not even the '
        'empty or truncated one a cap crossed mid-transfer produces')


#: A ``multipart/*`` body small enough that no cap is involved. Written
#: as literal wire bytes rather than built with ``MultipartWriter`` so
#: the boundary the header names and the boundary the body carries are
#: the same string by construction.
MULTIPART_WIRE: bytes = (
    b'--BOUND\r\n'
    b'Content-Type: application/octet-stream\r\n\r\n'
    b'payload-bytes\r\n'
    b'--BOUND--\r\n'
)

#: The header that makes the loopback server's answer multipart, and so
#: sends ``read_response`` down ``handle_multipart_response``.
MULTIPART_HEADERS: dict[str, str] = {
    'Content-Type': 'multipart/form-data; boundary=BOUND'}

#: The bytes a ``symlinked-target`` row puts behind the link, so the row
#: can prove they are still there afterwards.
VICTIM_CONTENT: bytes = b'a file the caller never named'

#: The ``(route, fault)`` pairs a route genuinely cannot express, with
#: the reason. An entry here is an argued exemption, not a silent gap --
#: the same discipline
#: :data:`~tests.fixtures.protocol_transports.CATEGORY_EXEMPT` applies
#: one axis up, and for the same reason: the failure mode of a
#: data-driven guard is a row quietly dropped while the remaining rows
#: stay green.
ROUTE_FAULT_INEXPRESSIBLE: dict[tuple[str, str], str] = {
    ('http-multipart-default', 'missing-parent'): (
        'the default multipart route writes response.txt into the '
        'process working directory, and a working directory always '
        'exists -- there is no parent to remove. Naming a missing one '
        'would require download_filepath, which is the '
        'http-multipart-config route immediately below and covers the '
        'identical open. The remaining two faults are expressible here '
        'and are exercised.'
    ),
}


def _write_fault_target(fault: str, tmp_path: Path) -> tuple[Path, Path]:
    """Stage one local write fault and return where to aim at it.

    Args:
        fault: A fault id from
            :data:`~tests.fixtures.protocol_transports.WRITE_FAULTS`.
        tmp_path: The test's temporary directory.

    Returns:
        ``(destination, victim)`` -- where the route should be pointed,
        and the file that must survive it. ``victim`` is ``tmp_path``
        itself when the fault has no victim to protect, which no
        assertion reads.

    Raises:
        ValueError: If ``fault`` names no staged fault. Fail closed: a
            row silently staging nothing would pass having proven
            nothing, which is the failure mode this whole guard exists
            to prevent.
    """
    if fault == 'missing-parent':
        return tmp_path / 'no-such-dir' / 'out.bin', tmp_path
    if fault == 'unwritable-parent':
        locked = tmp_path / 'locked'
        locked.mkdir()
        os.chmod(locked, 0o500)
        return locked / 'out.bin', tmp_path
    if fault == 'symlinked-target':
        victim = tmp_path / 'victim.txt'
        victim.write_bytes(VICTIM_CONTENT)
        link = tmp_path / 'out.bin'
        link.symlink_to(victim)
        return link, victim
    raise ValueError(f'{fault!r} stages no local write fault')


async def _drive_write_route(
    driver: str,
    protocol: str,
    destination: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
) -> str:
    """Drive one write route to its refusal and return the code.

    Args:
        driver: The driver id from
            :data:`~tests.fixtures.protocol_transports.WRITE_ROUTES`.
        protocol: The protocol that route belongs to.
        destination: Where the route should try to write.
        monkeypatch: The pytest patcher, for the doubled transports.
        http_server: The loopback server, for the HTTP-family routes.

    Returns:
        The ``error['code']`` the route answered, or ``'ok=True'`` when
        it did not refuse at all -- reported rather than raised, so the
        guard's own message names what happened.

    Raises:
        ValueError: If ``driver`` names no route. Fail closed, for the
            reason :func:`_write_fault_target` gives.
    """
    if driver in {'explicit_download', 'multipart_default',
                  'multipart_configured', 'download_helper'}:
        multipart = driver.startswith('multipart')
        http_server.respond(
            LOCAL_IO_PATH,
            body=MULTIPART_WIRE if multipart else b'{"value": 1}',
            headers=MULTIPART_HEADERS if multipart else None)

    if driver == 'download_helper':
        # The standalone public helper the README documents, which
        # reaches `safe_writer` without going through `request()` at
        # all -- so no envelope is built and the typed error is what a
        # caller sees. Compared here against the routes that do build
        # one, because a caller using both must not have to branch.
        try:
            await download_file_from_url(
                file_download_path=http_server.url_for(LOCAL_IO_PATH),
                local_filepath=str(destination),
                request_type='get')
        except AsyncGatewayError as err:
            return err.code
        return 'ok=True'

    if driver == 'multipart_default':
        # The route that needs **no configuration key at all**: a
        # `multipart/*` answer on default config still writes
        # `response.txt`, relative to the process working directory. So
        # the fault is re-staged under that fixed name and the working
        # directory is moved onto its parent -- which is also why this
        # route cannot express `missing-parent`, and says so by
        # skipping rather than by quietly answering a different
        # question (see the row's own guard).
        call = contract_call(protocol)
        call['url'] = http_server.url_for(LOCAL_IO_PATH)
        monkeypatch.chdir(destination.parent)
        named = destination.parent / DEFAULT_DOWNLOAD_FILEPATH
        if destination.is_symlink():
            named.symlink_to(destination.readlink())
    elif driver in {'explicit_download', 'multipart_configured'}:
        call = local_io_call(protocol, str(destination))
        call['url'] = http_server.url_for(LOCAL_IO_PATH)
    else:
        call = local_io_call(protocol, str(destination))
        install_writing_transport(
            monkeypatch, protocol,
            recurse=driver == 'transfer_recursive')

    result = await request(**call)
    return 'ok=True' if result['ok'] else result['error']['code']


@pytest.mark.parametrize('fault, expected', WRITE_FAULTS)
@pytest.mark.parametrize(
    'route, protocol, driver', WRITE_ROUTES,
    ids=[route for route, _, _ in WRITE_ROUTES])
async def test_every_write_route_classifies_one_fault_identically(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    route: str,
    protocol: str,
    driver: str,
    fault: str,
    expected: str,
) -> None:
    """One local write fault, one code, down **every** route to a disk.

    The third axis, and the one whose absence is the nineteenth
    instance of this release's recurring class: a behaviour differing
    between two paths for no argued reason, invisible because no guard
    named that axis. The ``LOCAL_IO`` row above is indexed by
    *protocol*, so it proves four protocols agree -- while exercising,
    for each, only the **one** route its `local_io_call` happens to
    configure. It passed throughout, because the routes it does not
    take are not routes it can see.

    They were not equivalent. The single-file transfer arm hands
    ``resolve_within`` the containment base as its own candidate -- an
    empty tail no other route produces -- and that case took the
    ``..`` arm, whose whole-path ``resolve()`` canonicalises the
    **final** component. The leaf is the file about to be opened, so a
    symbolic link at it was resolved away *before* the open and
    ``O_NOFOLLOW`` was handed the link's target with nothing left to
    refuse. Measured against the real ``aioftp`` recursion: a
    ``client_path`` that was a symlink to another file wrote straight
    through it, answered ``ok=True``, and left the victim at mode 0644
    -- where the identical HTTP download answered ``PATH``/400.

    Args:
        monkeypatch: The pytest patcher.
        http_server: The loopback server, for the HTTP-family routes.
        tmp_path: The test's temporary directory.
        route: The route id, for the failure message.
        protocol: The protocol the route belongs to.
        driver: Which driver arm drives it.
        fault: The staged local write fault.
        expected: The one code every route must answer for it.

    Returns:
        None.
    """
    if (route, fault) in ROUTE_FAULT_INEXPRESSIBLE:
        pytest.skip(ROUTE_FAULT_INEXPRESSIBLE[(route, fault)])

    destination, victim = _write_fault_target(fault, tmp_path)

    answered = await _drive_write_route(
        driver, protocol, destination,
        monkeypatch=monkeypatch, http_server=http_server)

    assert answered == expected, (
        f'route {route!r} answered {answered} for a {fault} '
        f'destination, where every other route to a local disk answers '
        f'{expected}. One fault must not depend on which write path '
        f'reached it: the routes are enumerated in WRITE_ROUTES '
        f'precisely because the per-protocol LOCAL_IO row exercises '
        f'only one of them per protocol and passed while two disagreed.')
    if fault == 'symlinked-target':
        assert victim.read_bytes() == VICTIM_CONTENT, (
            f'route {route!r} wrote *through* a symbolic link at the '
            f'destination and into a file the caller never named. The '
            f'refusal alone is not the property -- a route that wrote '
            f'first and complained afterwards would satisfy a '
            f'code-only assertion, and that is exactly what the '
            f'single-file transfer arm did at mode 0644.')


@pytest.mark.parametrize(
    'route, protocol, driver', WRITE_ROUTES,
    ids=[route for route, _, _ in WRITE_ROUTES])
async def test_every_write_route_lands_on_the_canonical_location(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    tmp_path: Path,
    route: str,
    protocol: str,
    driver: str,
) -> None:
    """A symlinked *parent* resolves, on every route, to one location.

    The control this axis needs, and not a duplicate of the refusal
    rows above. Those assert that a bad destination is refused
    identically; refusing *everything* would satisfy them. This asserts
    the other half -- that a legitimate destination still succeeds, and
    lands where canonicalisation says rather than where the caller
    spelled it.

    It is a real shape rather than a contrived one: macOS ``/tmp`` is a
    symbolic link to ``/private/tmp``, and the README's own documented
    example writes there. So "the parent is canonicalised, the leaf is
    not" is a property a caller feels on every ordinary call, and the
    two halves are separable -- a mutation dropping the parent's
    ``resolve()`` leaves every refusal row green while silently
    returning the uncanonicalised path.

    Args:
        monkeypatch: The pytest patcher.
        http_server: The loopback server, for the HTTP-family routes.
        tmp_path: The test's temporary directory.
        route: The route id, for the failure message.
        protocol: The protocol the route belongs to.
        driver: Which driver arm drives it.

    Returns:
        None.
    """
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'via').symlink_to(real)

    answered = await _drive_write_route(
        driver, protocol, tmp_path / 'via' / 'out.bin',
        monkeypatch=monkeypatch, http_server=http_server)

    assert answered == 'ok=True', (
        f'route {route!r} answered {answered} for a destination whose '
        f'only unusual feature is a symlinked parent directory -- the '
        f'shape macOS /tmp has, and the README documents. A guard made '
        f'only of refusal rows is satisfied by a route that refuses '
        f'everything; this is the row that is not.')
    written = sorted(p.name for p in real.iterdir())
    assert written, (
        f'route {route!r} reported success but wrote nothing under the '
        f'canonical parent {str(real)!r}. The parent must be resolved '
        f'-- a route that writes through the *uncanonicalised* '
        f'spelling passes every refusal row above while quietly '
        f'defeating the canonicalisation half of resolve_within.')


def test_the_write_route_enumeration_reaches_every_guarded_open() -> None:
    """The enumeration itself, so a ninth route cannot opt out silently.

    The row above holds each *listed* route to the shared answer;
    nothing in it notices a route that was never listed -- which is the
    same shape as the two-of-four gap ``LOCAL_IO`` had, one level up.
    So the list is checked against the package rather than against
    itself.

    It is checked by **census, not by grep**, and that distinction is
    the whole value of the row. A search for ``safe_writer(`` or
    ``guarded_opener(`` finds the writes that are already guarded, which
    is precisely the set that needs no guarding: an unguarded ``open(p,
    'wb')`` added to a module in neither set spells neither name, so the
    grep matched nothing, the module never entered ``writing_modules``,
    and the row stayed green while the defect it names walked straight
    past it. A guard blind to the shape it exists to catch is not a
    guard.

    So the package's AST is walked instead, for every call that can
    create or truncate a local file -- ``open``, ``os.open``,
    ``aiofiles.open``, ``Path.write_bytes``, a ``shutil`` copy, a
    temporary file -- and each occurrence must sit at a site named
    below. The exemptions are the *sites*, not the modules: naming
    ``paths.py`` as a writing module would re-admit an unguarded write
    anywhere else in it, which is the failure this replaced.

    Returns:
        None.
    """
    # Every place the package may open a local file for writing, as
    # `module::enclosing function`. A site earns its place by being the
    # guard itself or by going through it; anything else is a finding.
    permitted = {
        # The two guards. `safe_writer` is the async one every HTTP-
        # family route funnels through, and its `aiofiles.open` carries
        # `guarded_opener`; `_open_guarded` is the synchronous twin the
        # two transfer protocols reach through their path-IO layer, and
        # its two `open` calls are the read arm and the guarded write
        # arm of one branch.
        'utils/paths.py::safe_writer',
        'utils/contained_io.py::_open_guarded',
        # `open_within` is what `guarded_opener` opens *through*: the
        # descriptor walk that applies O_NOFOLLOW to a leaf resolved
        # against an open parent. It is the syscall the guard is made
        # of, not a route around it.
        'utils/paths.py::open_within',
    }
    # The three upload reads in `request_helper.py` are mode-literal
    # `'rb'`, so the census drops them on mode alone and they need no
    # entry above. `_walk_to_parent`'s `os.open` is dropped for the
    # complementary reason: `O_DIRECTORY`, no `O_CREAT`, so it opens an
    # existing directory and creates nothing.
    census = {
        f'{module}::{enclosing}': (line, name)
        for module, source in package_sources()
        for line, name, enclosing in write_seams_in(source)
    }
    unclaimed = {
        site: where for site, where in census.items()
        if site not in permitted
    }

    assert not unclaimed, (
        'the package can create or truncate a local file at '
        + ', '.join(
            f'{site.split("::")[0]}:{line} ({name})'
            for site, (line, name) in sorted(unclaimed.items()))
        + ', which is not one of the guarded sites and is claimed by no '
        'route in WRITE_ROUTES. Route the write through `safe_writer` '
        '(async) or `_open_guarded` (the transfer path) rather than '
        'adding it here: a write outside those is a write with no '
        'O_NOFOLLOW, no overwrite refusal and no containment check, '
        'which is how the single-file transfer arm came to write '
        'through a symlink for the life of the release. If a new '
        'guarded route is genuinely needed, add it to WRITE_ROUTES and '
        'its driver arm first, and name the site here second.')


@pytest.mark.parametrize('protocol', ['FTP', 'SFTP'])
async def test_a_mkdir_over_an_existing_file_is_one_code_on_both(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    protocol: str,
) -> None:
    """The two transfer protocols answer one local fault one way.

    A cross-protocol row rather than two per-protocol ones, on the axis
    that exists because per-protocol tests written by hand are what
    drift. Both transfer clients ask their destination filesystem to
    create a directory before writing into it, and both can be pointed
    at a path a regular file already occupies -- one question, which
    they answered two ways: measured against real loopback servers,
    ``CONFIG``/400 on FTP and ``PATH``/400 on SFTP.

    ``PATH`` is the settled answer because it is what every
    neighbouring refusal on the same call already reports, and because
    the ``CONFIG`` message told the caller to pass ``overwrite=True``
    -- which cannot make a ``mkdir`` succeed over a file.

    The SFTP half only became reachable when the writing double learnt
    to take ``_copy``'s directory arm: it went straight to ``open``
    before, so ``ContainedLocalFS.mkdir`` was exercised by nothing.

    Args:
        monkeypatch: The pytest patcher.
        tmp_path: The test's temporary directory.
        protocol: The transfer protocol under test.

    Returns:
        None.
    """
    destination = tmp_path / 'downloads'
    destination.write_bytes(b'an ordinary file in the way')

    call = local_io_call(protocol, str(destination))
    install_writing_transport(monkeypatch, protocol, recurse=True)

    result = await request(**call)

    assert result['ok'] is False
    assert result['error']['code'] == LOCAL_IO_CODE, (
        f'{protocol} reported {result["error"]["code"]} for a mkdir over '
        f'an existing file, where the other transfer protocol reports '
        f'{LOCAL_IO_CODE}. This is the eleventh-round divergence class: '
        'one local fault, two codes, hidden because no double took the '
        "real client's directory arm.")
    assert destination.read_bytes() == b'an ordinary file in the way'


async def test_preserve_cannot_let_a_server_widen_a_local_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No protocol lets the remote side choose a local file's mode (M18).

    SFTP is the only protocol with a ``preserve`` to reconcile --
    ``aioftp`` and this library's HTTP write path apply no
    server-supplied attribute to a local file, so 0600 already held
    there unconditionally -- but the *property* is cross-protocol and
    is asserted as one: every local file this library writes is
    owner-only, whatever the server says about its own copy.

    ``asyncssh``'s ``_setstat`` chmods to the attributes ``_copy``
    hands it, and those are the remote file's, so ``preserve=True``
    silently reverted the guarded open's 0600. No double reached
    ``setstat`` at all, which is why nothing saw it.

    Args:
        monkeypatch: The pytest patcher.
        tmp_path: The test's temporary directory.

    Returns:
        None.
    """
    destination = tmp_path / 'downloads'
    call = local_io_call('SFTP', str(destination))
    install_writing_transport(
        monkeypatch, 'SFTP', recurse=True, preserve=True)

    result = await request(**call)

    landed = destination / os.fsdecode(REMOTE_ENTRY)
    assert result['ok'] is True, (
        'preserve=True is legitimate and must still complete')
    assert landed.stat().st_mode & 0o777 == 0o600, (
        f'the server asked for {oct(REMOTE_PERMISSIONS)} and got it: a '
        'remote endpoint decided the permissions of a file on this '
        'machine, undoing the 0600 every local write establishes.')


def _symlinked_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Build a small local tree reached through a symbolic link.

    The shape AGW-38 fires on, made explicit rather than borrowed from
    the platform: macOS ``/tmp`` is a link to ``/private/tmp``, which is
    why the README's own documented path reproduced it, but relying on
    that would make the row silently vacuous on Linux. A link created
    here reproduces on every platform.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        The tree's path *as a caller would name it* -- through the link
        -- and the real directory it resolves to.

    """
    real = tmp_path / 'real'
    (real / 'tree' / 'sub').mkdir(parents=True)
    (real / 'tree' / 'a.txt').write_bytes(b'AAAA')
    (real / 'tree' / 'sub' / 'b.txt').write_bytes(b'BBBB')
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    return link / 'tree', real / 'tree'


async def test_a_directory_upload_survives_a_symlinked_client_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A recursive upload works when the local path goes via a link.

    AGW-38, and the one call in the library whose *return value* a
    transport client does arithmetic on. ``ContainedPathIO.list``
    answered with canonicalised entries because that is how containment
    checks a path; ``aioftp.Client.upload`` then computed
    ``entry.relative_to(source)`` against the uncanonicalised ``source``
    it still held. Wherever any component of ``client_path`` was a
    symbolic link the two disagreed and ``ValueError`` escaped -- and
    ``ValueError`` is in no FTP transport family, so it left
    ``request()`` **un-enveloped**, part-way through the upload, with a
    half-written remote tree behind it.

    Measured against a real ``aioftp.Server`` on loopback with
    ``client_path`` under macOS ``/tmp``: ``ValueError: '/private/tmp/
    .../tree/sub' is not in the subpath of '/tmp/.../tree'``, with
    ``dest/`` created and nothing in it.

    The assertion is on the **whole tree arriving**, not on the absence
    of an exception: a wrapper that quietly listed nothing would raise
    nothing either, and would be just as broken.

    Args:
        monkeypatch: The pytest patcher.
        tmp_path: The test's temporary directory.

    Returns:
        None.
    """
    through_link, _ = _symlinked_tree(tmp_path)
    context = install_writing_transport(monkeypatch, 'FTP')

    result = await request(
        'host', protocol='FTP', auth=BasicAuth('u', 'p'),
        protocol_info={
            'command': 'upload',
            'client_path': str(through_link),
            'server_path': '/dest',
        })

    assert result['ok'] is True, (
        f'a directory upload through a symlinked path failed with '
        f'{result["error"]}; before the fix the ValueError did not even '
        'reach the envelope.')
    assert context.client.uploaded == {
        'dest/a.txt': b'AAAA',
        'dest/sub/b.txt': b'BBBB',
    }, (
        'the whole tree must arrive: a list that yielded nothing would '
        'also raise nothing, and half a tree is what the defect left.')


async def test_a_directory_upload_still_refuses_to_leave_the_base(
    tmp_path: Path,
) -> None:
    """Returning caller-form paths must not weaken containment.

    The other half of AGW-38's fix, and the risk in it: the escape the
    whole module exists to refuse is decided by canonicalising, so a
    change that hands *uncanonicalised* paths back must be shown not to
    have moved the check as well as the answer. The base here is
    reached through a symbolic link -- the very shape that broke the
    arithmetic -- so the row proves the two properties hold at once.

    Args:
        tmp_path: The test's temporary directory.

    Returns:
        None.
    """
    through_link, real = _symlinked_tree(tmp_path)
    victim = real.parent / 'victimdir'
    victim.mkdir()
    sentinel = victim / 'SENTINEL'
    sentinel.write_bytes(b'must not be reachable from the base')
    layer = contained_path_io_factory(through_link)(timeout=None)
    escaping = Path(str(through_link / '..' / 'victimdir'))

    with pytest.raises(PathContainmentError):
        layer.list(escaping)
    with pytest.raises(PathContainmentError):
        await layer.is_dir(escaping)
    with pytest.raises(PathContainmentError):
        await layer.is_file(escaping / 'SENTINEL')

    assert sentinel.read_bytes() == b'must not be reachable from the base'
    assert [p async for p in layer.list(through_link / 'sub')] == [
        through_link / 'sub' / 'b.txt'], (
        'a contained entry must come back in the form the caller named, '
        'not the canonical one aioftp cannot do arithmetic against')


@pytest.mark.parametrize('protocol', CONTRACT_ROWS)
def test_every_protocol_declares_a_local_destination_or_is_exempt(
    protocol: str,
) -> None:
    """The ``LOCAL_IO`` axis cannot silently lose a protocol.

    The data-driven guard's own failure mode, and the reason the wire
    axis carries the identical row: a protocol quietly dropped from the
    table above would reduce coverage while every remaining row stayed
    green. An omission must be **argued** in
    :data:`CATEGORY_EXEMPT`, not merely absent.

    SOAP's exemption is checked rather than believed. It claims to write
    no local file, and this asserts the claim against the code -- the
    protocol accepts no local-destination key -- so the day SOAP grows
    a download the exemption fails instead of hiding it.

    Args:
        protocol: The protocol under test, from the contract table.

    Returns:
        None.
    """
    exempt = CATEGORY_EXEMPT.get('LOCAL_IO', {})
    if protocol not in exempt:
        assert local_io_call(protocol, '/tmp/x')['protocol_info'], (
            f'{protocol} claims no LOCAL_IO exemption and names no '
            'local destination, so its row proves nothing.')
        return

    source = inspect.getsource(soap_client.SoapRequest)
    assert 'http_file_download_config=None' in source, (
        'SOAP is exempt from LOCAL_IO because it hands the transport a '
        'literal None for the download config, so no local file is '
        'ever opened. That line is the exemption; it is gone.')
    assert "info.get('http_file_download_config')" not in source, (
        'SOAP now reads a caller-supplied download config, so it can '
        'write a local file and the LOCAL_IO exemption is stale: give '
        'it a row instead of an argument.')


@pytest.mark.parametrize('protocol', CONTRACT_ROWS)
def test_every_protocol_declares_a_fault_for_every_category(
    protocol: str,
) -> None:
    """The fault table itself covers every protocol × category (AGW-R9-1).

    The row above can only test what the table declares, so a protocol
    quietly dropped from :data:`PROTOCOL_FAULTS` -- or a category left
    out of one protocol's entry -- would silently reduce the guard's
    coverage while every remaining row stayed green. That is the failure
    mode of a data-driven guard, and it is the one that would let the
    *sixth* instance of this class through.

    An omission must therefore be either present or **argued**: a
    category a protocol genuinely cannot produce is recorded in
    :data:`CATEGORY_EXEMPT` with the reason, which this row requires and
    which the row above prints when it skips. Silence is not an option
    in either direction.

    Args:
        protocol: The protocol under test, from the contract table.

    Returns:
        None.
    """
    declared = PROTOCOL_FAULTS.get(protocol)
    assert declared is not None, (
        f'{protocol} is a contract row with no entry in PROTOCOL_FAULTS, '
        'so the fault-category guard skips it entirely.')

    for category in FAULT_CATEGORIES:
        exempt = CATEGORY_EXEMPT.get(category, {})
        assert category in declared or protocol in exempt, (
            f'{protocol} declares no {category} fault and claims no '
            'exemption for it. Add the exception its transport library '
            'raises, or record in CATEGORY_EXEMPT why the category '
            'cannot arise -- an undocumented gap is how this class of '
            'divergence survived four review rounds.')
        assert not (category in declared and protocol in exempt), (
            f'{protocol} both declares a {category} fault and claims an '
            'exemption from it. One of the two is stale.')


# --- E2: ok is False exactly when error is set -----------------------------


async def test_e2_success_is_ok_with_no_error(
    ok_envelope: GatewayResponse,
) -> None:
    """``ok`` is True only when nothing failed (E2)."""
    assert ok_envelope['ok'] is True
    assert ok_envelope['error'] is None


@pytest.mark.parametrize(
    'exc',
    [
        pytest.param(HttpStatusError('not found', 404), id='HTTP_STATUS'),
        pytest.param(GatewayTimeoutError('timed out'), id='TIMEOUT'),
        pytest.param(ConnectError('refused'), id='CONNECT'),
        pytest.param(CircuitOpenError('open'), id='CIRCUIT_OPEN'),
        pytest.param(SerializationError('unparseable'), id='SERIALIZATION'),
    ],
)
def test_e2_every_failure_mode_is_not_ok_and_carries_an_error(
    exc: AsyncGatewayError,
) -> None:
    """``ok is False`` if and only if ``error`` is populated (E2)."""
    envelope = finalise_error(
        new_envelope(url='http://host/p', protocol='HTTP', payload={}),
        exc,
        started=monotonic_now(),
    )

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['code'] == exc.code
    assert envelope['error']['type'] == type(exc).__name__
    assert envelope['status_code'] == exc.status_code


# --- E3: the message is never empty ----------------------------------------


@pytest.mark.parametrize(
    'exc',
    [
        pytest.param(HttpStatusError('not found', 404), id='with-message'),
        pytest.param(ConnectError(''), id='empty-message'),
        pytest.param(GatewayTimeoutError(''), id='empty-timeout'),
    ],
)
def test_e3_the_error_message_is_never_empty(
    exc: AsyncGatewayError,
) -> None:
    """A blank message reads as success, so there is never one (E3).

    The fallback is the exception's own class name, which is the worst case
    the design permits -- and is still strictly more than ``''``.
    """
    envelope = finalise_error(
        new_envelope(url='http://host/p', protocol='HTTP', payload={}),
        exc,
        started=monotonic_now(),
    )

    assert envelope['error'] is not None
    assert envelope['error']['message']


@pytest.mark.parametrize(
    'kind',
    ['remote-status', 'connect-refused'],
)
async def test_e3_a_live_failure_reports_something_readable(
    kind: str,
    http_server: RecordingHTTPServer,
) -> None:
    """The message survives the real code path, not just a unit call (E3)."""
    if kind == 'remote-status':
        http_server.respond('/boom', status=500, body=b'{}')
        envelope = await http_call(http_server.url_for('/boom'))
    else:
        envelope = await http_call(f'http://127.0.0.1:{closed_port()}/x')

    assert envelope['ok'] is False
    assert envelope['error'] is not None
    assert envelope['error']['message'].strip()


# --- E4: never self-referential --------------------------------------------


async def test_e4_the_envelope_is_never_reachable_from_itself(
    ok_envelope: GatewayResponse,
    failed_envelope: GatewayResponse,
) -> None:
    """No key, at any depth, is the envelope itself (E4).

    The shape this replaces aliased the whole response under one of its own
    keys, so the natural success check read the container and was truthy on
    every path.
    """
    for envelope in (ok_envelope, failed_envelope):
        for key, value in envelope.items():
            assert value is not envelope, key
            assert not contains_self(value, envelope), key


# --- E5 / E6: serialisable, and `text` is always text ----------------------


async def test_e5_every_path_is_json_serialisable(
    ok_envelope: GatewayResponse,
    failed_envelope: GatewayResponse,
) -> None:
    """``json.dumps`` succeeds on success and on failure alike (E5)."""
    transport = await http_call(f'http://127.0.0.1:{closed_port()}/x')

    for envelope in (ok_envelope, failed_envelope, transport):
        assert json.loads(json.dumps(envelope)) is not None


async def test_e6_text_is_always_a_string_never_an_exception(
    ok_envelope: GatewayResponse,
    failed_envelope: GatewayResponse,
) -> None:
    """``text`` is decoded body text or ``''``, never an exception (E6)."""
    transport = await http_call(f'http://127.0.0.1:{closed_port()}/x')

    for envelope in (ok_envelope, failed_envelope, transport):
        assert isinstance(envelope['text'], str)
    assert transport['text'] == ''


# --- E7: the fabricated status is gone -------------------------------------


@pytest.mark.parametrize(
    'exc',
    [
        pytest.param(GatewayTimeoutError('t'), id='TIMEOUT'),
        pytest.param(ConnectError('c'), id='CONNECT'),
        pytest.param(CircuitOpenError('o'), id='CIRCUIT_OPEN'),
        pytest.param(HttpStatusError('h', 404), id='HTTP_STATUS'),
    ],
)
def test_e7_no_failure_reports_the_fabricated_status(
    exc: AsyncGatewayError,
) -> None:
    """Every failure carries a status a consumer already knows (E7)."""
    envelope = finalise_error(
        new_envelope(url='http://host/p', protocol='HTTP', payload={}),
        exc,
        started=monotonic_now(),
    )

    assert envelope['status_code'] != 999
    assert 100 <= envelope['status_code'] <= 599


def test_e7_the_fabricated_status_appears_nowhere_in_the_package() -> None:
    """The whole package is free of the invented status code (E7)."""
    assert files_containing(r'999') == []


#: The one place in the package that may catch ``Exception`` broadly, and
#: the function it must be inside. R10-AC2 bans the blanket catch because
#: three of them used to report *this library's own bugs* -- a ``KeyError``,
#: a ``TypeError`` -- as fabricated statuses on a failed request, which is
#: the blindness the one-conversion-point rule exists to remove.
#:
#: ``run_processor`` inverts every term of that. The code inside its ``try``
#: is not this library's: it is a callback the **caller** supplied and this
#: library merely awaits, and a bug in it is by definition not a bug in
#: here. Nor is the catch a way of *hiding* the failure -- it converts it to
#: a typed ``ProcessorError``, chains the original as ``__cause__`` so its
#: type and message reach the caller through the cause chain, and reports a
#: distinct ``PROCESSOR``/500 rather than dressing it up as a request that
#: failed. Not catching is the option that breaks the contract: an
#: arbitrary ``RuntimeError`` from a caller's own function would escape
#: ``request()`` as a bare builtin, which is NEW-2 exactly.
#:
#: Pinned to the function rather than to a line number, so the allowance
#: cannot drift: a second blanket catch anywhere -- including elsewhere in
#: ``asyncio_gateway.py`` -- still fails the ban.
BLANKET_EXCEPT_SITE: Final[tuple[str, str]] = (
    'asyncio_gateway.py', 'run_processor')


def test_r10_ac2_the_only_blanket_except_wraps_the_callers_own_callback(
) -> None:
    """The one justified blanket catch, held to being the only one.

    The ban below cannot simply exempt a file: doing so would let a
    *second* blanket catch into ``asyncio_gateway.py`` -- the entry point,
    of all places -- with the suite still green. So the file is checked
    here instead, and the check is stricter than the ban it replaces: it
    requires exactly one occurrence, inside exactly the one function
    whose ``try`` holds foreign code.
    """
    name, function = BLANKET_EXCEPT_SITE
    source = dict(package_sources())[name]

    occurrences = re.findall(r'except Exception', source)
    assert len(occurrences) == 1, (
        f'{name} may contain exactly one blanket except, the one in '
        f"{function}() that converts a caller-supplied callback's "
        f'failure into a typed ProcessorError. Found '
        f'{len(occurrences)}. Every other blanket catch in this package '
        f'wrapped code this library wrote, and reported its own bugs as '
        f'failed requests (R10-AC2).')

    body = source.split(f'def {function}(')[1].split('\ndef ')[0]
    assert 'except Exception' in body, (
        f'the blanket except in {name} has moved out of {function}(). '
        f'It is permitted only there, because only there is the code '
        f"inside the try the caller's rather than this library's.")
    assert 'raise ProcessorError' in body, (
        f'{function}() catches Exception without converting it to a '
        f'typed error, which is the suppression R10-AC2 bans rather '
        f'than the conversion it permits.')


@pytest.mark.parametrize(
    'pattern, criterion',
    [
        pytest.param(r'999', 'E7', id='fabricated-status'),
        pytest.param(r"'tat'", 'R8-AC5', id='tat'),
        pytest.param(r'time\.time\(\)', 'R9-AC4', id='wall-clock-duration'),
    ],
)
def test_the_banned_patterns_appear_nowhere_in_the_package(
    pattern: str,
    criterion: str,
) -> None:
    """No banned pattern appears in any module of the package.

    This began as a *containment* check: Step 5 could not delete the FTP
    and SFTP occurrences -- those files belonged to later stories -- so it
    guaranteed only that the residue was exactly those two files rather
    than a growing set. Both have now been rewritten (S10, S11, S12), the
    residue is empty, and the allowance goes with it. A subset assertion
    against an empty set is a ban, but an *implicit* one: it would have
    kept reading as an allowance to any reader, and it left ``'tat'``,
    ``except Exception`` and ``time.time()`` reintroducible into either
    client with the suite still green. The release-level criterion was
    always zero; this is where it is stated as zero.

    ``except Exception`` moved out of this list at NEW-2 and into
    :func:`test_r10_ac2_the_only_blanket_except_wraps_the_callers_own_callback`,
    which is a *narrower* check rather than a relaxation: the package
    still permits exactly one, in exactly one function, and only where it
    converts to a typed error. See the note on
    :data:`BLANKET_EXCEPT_SITE` for why a caller's own callback is the
    one place the ban's reasoning does not apply.
    """
    assert files_containing(pattern) == [], criterion


def test_the_transport_boundary_no_longer_invents_a_response(
) -> None:
    """FI-7: the helper's fresh response dict is gone from the package."""
    assert files_containing(r"kwargs\.get\('response'") == []


# --- E8 / R9: honest timestamps, monotonic durations -----------------------


def test_r9_request_time_is_utc_iso8601_with_an_explicit_offset() -> None:
    """The timestamp parses and says which zone it is in (R9-AC1)."""
    envelope = new_envelope(url='http://host/p', protocol='HTTP', payload={})

    parsed = datetime.fromisoformat(envelope['request_time'])

    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_r9_utc_now_iso_is_the_one_source_of_that_timestamp() -> None:
    """The helper itself produces an offset-aware UTC value (R9-AC3)."""
    parsed = datetime.fromisoformat(utc_now_iso())

    assert parsed.utcoffset().total_seconds() == 0


def test_r9_no_hardcoded_timezone_survives_anywhere() -> None:
    """The import-time regional timezone binding is gone (R9-AC2, AC3)."""
    assert files_containing(r'Asia/Kolkata|TIMEZONE|pytz') == []


def test_r9_the_dependency_set_no_longer_declares_the_timezone_library(
) -> None:
    """The declaration goes in the same change as the import (R6-AC2).

    Separating them breaks the clean-venv job in one direction or the
    other: an undeclared import fails to resolve, and a declared-but-unused
    dependency ships weight nothing needs.
    """
    manifest = (
        PACKAGE_ROOT.parent / 'pyproject.toml'
    ).read_text(encoding='utf-8')

    assert 'pytz' not in manifest


def test_e8_latency_is_never_negative_when_the_clock_steps_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clock reading that goes backwards still yields ``latency >= 0``.

    Durations are measured monotonically precisely so a wall-clock step
    cannot produce a negative value on the metric users chart. The clamp
    keeps that true even for a substituted clock (E8, R9-AC4).
    """
    readings = iter([100.0, 40.0])
    monkeypatch.setattr(
        date_helper.time, 'monotonic', lambda: next(readings))

    started = date_helper.monotonic_now()
    envelope = finalise_ok(
        new_envelope(url='http://host/p', protocol='HTTP', payload={}),
        status_code=200,
        started=started,
    )

    assert envelope['latency'] >= 0


def test_e8_elapsed_since_a_future_reference_point_is_zero() -> None:
    """The same clamp, asserted at the helper it lives in (E8)."""
    assert elapsed_since(monotonic_now() + 60) == 0.0


async def test_e8_a_real_call_reports_a_non_negative_latency(
    ok_envelope: GatewayResponse,
) -> None:
    """Latency is a measured duration, not a placeholder (R9-AC4)."""
    assert ok_envelope['latency'] >= 0
    assert isinstance(ok_envelope['latency'], float)


# --- E9: no credential in what the caller keeps ----------------------------


async def test_e9_no_credential_reaches_the_envelope(
    http_server: RecordingHTTPServer,
) -> None:
    """Header, query parameter, payload and cookie are all masked (E9).

    One request carrying all four kinds of secret at once, because the
    invariant is about the envelope as a whole and a per-field test would
    not catch a secret that leaked through a fifth field.
    """
    http_server.respond(
        '/secrets',
        status=200,
        body=b'{"value": 1}',
        headers={
            'Content-Type': 'application/json',
            'Set-Cookie': 'session=COOKIESECRET; Path=/',
        },
    )

    envelope = await http_call(
        http_server.url_for('/secrets') + '?api_key=QUERYSECRET',
        data={'password': 'PAYLOADSECRET', 'kept': 'visible'},
        request_type='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer HEADERSECRET',
        },
    )

    rendered = repr(envelope)
    for secret in (
        'QUERYSECRET', 'PAYLOADSECRET', 'HEADERSECRET', 'COOKIESECRET',
    ):
        assert secret not in rendered, secret
    assert envelope['headers']['Set-Cookie'] == REDACTED
    assert envelope['cookies']['session'] == REDACTED
    assert envelope['payload']['password'] == REDACTED
    assert envelope['payload']['kept'] == 'visible'
    assert REDACTED in envelope['url']


def test_e9_there_is_no_opt_out_of_redaction() -> None:
    """No switch exists whose only function is to unmask credentials.

    A ``redact=False`` parameter would put credentials back into the
    envelope on request, which is not a feature this release ships.
    """
    assert files_containing(r'redact\s*[=:]\s*False') == []


def test_redact_url_masks_userinfo_and_sensitive_parameters() -> None:
    """The URL redactor covers both halves E9 names (R8, redaction).

    E9 is about credential **values**, and a username is not one -- the
    same bargain :func:`redact_headers` strikes, where an
    ``Authorization`` header keeps its name and loses its value. So the
    name is asserted present rather than absent: it is diagnostic, and
    this function used to drop it only as a side effect of rebuilding
    the URL through ``urlunsplit``, which is the second userinfo rule
    F2 removed.
    """
    redacted = redact_url('https://user:pw@host/p?token=abc&page=2')

    assert 'pw' not in redacted
    assert 'abc' not in redacted
    assert redacted == (
        f'https://user:{REDACTED}@host/p?token={REDACTED}&page=2')


def test_redact_text_masks_a_url_embedded_anywhere_in_a_string() -> None:
    """Foreign text is a sentence with a URL in it, not a bare URL.

    ``redact_url`` needs the whole string to be a URL, so a whole-string
    test sees nothing to mask in
    ``'RetriesExhausted: https://...?api_key=...'`` -- which is exactly
    the shape ``unwrap_cause`` returns.
    """
    redacted = redact_text(
        'RetriesExhausted: https://host/p?api_key=SECRETVALUE happened')

    assert 'SECRETVALUE' not in redacted
    assert redacted.startswith('RetriesExhausted: ')
    assert redacted.endswith(' happened')
    assert REDACTED in redacted


def test_redact_text_leaves_a_string_with_no_url_alone() -> None:
    """It is applied to every message, so it must be a no-op on most."""
    assert redact_text('connection reset by peer') == (
        'connection reset by peer')


def test_redact_text_masks_a_sensitive_pair_that_carries_no_scheme(
) -> None:
    """The seam of S8-H1: masking may not depend on URL-ness at all.

    A caller who writes ``host/p?api_key=...`` hands this function a
    string with no ``scheme://`` anywhere in it, and an embedded-URL pass
    matches nothing there. Its three consumers therefore carried the
    secret in the clear: ``error['message']``, ``error['cause']`` and the
    log record's ``extra['traceback']``, all of which are masked by
    ``redact_text`` alone. The end-to-end guard further down proves the
    symptom is gone; this one names the seam, so reinstating a pass that
    asks whether the text is a URL fails here first.

    The insensitive pair is asserted *present* in the same breath. It is
    what separates this rule from a blanket that blanks every query
    string, and it is why the accepted cost is over-masking prose rather
    than losing the diagnostics the envelope exists for.
    """
    masked = redact_text('host/p?api_key=SECRETVALUE&page=2')

    assert masked == f'host/p?api_key={REDACTED}&page=2'


def test_redact_text_masks_a_scheme_less_url_exactly_as_a_schemed_one(
) -> None:
    """Symmetry is the contract; the absence of one secret is not.

    A predicate deciding "is this a URL?" has been narrowed three times on
    this module -- scheme *and* netloc, then scheme alone -- and each
    narrowing still left a shape it classified wrongly. Each also passed a
    test that asserted only that some particular secret was gone, which is
    a bar a fourth predicate could clear while disagreeing about the next
    shape along. Asserting that the two strings mask *identically* leaves
    no such room: the only way to satisfy it is to stop asking.
    """
    schemeless = redact_text('host/p?api_key=SECRETVALUE')
    schemed = redact_text('https://host/p?api_key=SECRETVALUE')

    assert schemeless == f'host/p?api_key={REDACTED}'
    assert schemed == f'https://{schemeless}'


def test_redact_text_applies_caller_supplied_names_without_a_scheme_too(
) -> None:
    """The extension point cannot be narrower than the built-in set.

    ``redact_query_params`` is the only way a caller names a secret this
    library could not guess, and on the ``cause`` and ``traceback``
    surfaces it is honoured by ``redact_text`` alone. A scheme-less URL
    that masked ``api_key`` but not the caller's own ``session_id`` would
    leak precisely the name the caller took the trouble to declare.
    """
    masked = redact_text(
        'host/p?session_id=SESSIONSECRET&api_key=DEFAULTSECRET',
        extra_params=('session_id',))

    assert masked == f'host/p?session_id={REDACTED}&api_key={REDACTED}'


def test_redact_text_is_idempotent_on_a_string_it_has_already_masked(
) -> None:
    """``redact_value`` composes maskers, so passes must not compound.

    A rule that treated the sentinel as a fresh value to rewrite would
    make the *number* of passes observable, and the number of passes is an
    implementation detail no caller of the envelope can see.
    """
    once = redact_text('host/p?api_key=SECRETVALUE&page=2')

    assert once == f'host/p?api_key={REDACTED}&page=2'
    assert redact_text(once) == once


def test_redact_text_runs_the_pair_pass_before_the_embedded_url_pass(
) -> None:
    """Order is load-bearing, so it is pinned rather than left to reading.

    Both passes mask the same secret here, so the secret's absence proves
    nothing about which ran first. The caller's own rendering does.
    ``redact_url`` rebuilds a query only when its own masking changed
    something, so with the pair pass first it finds nothing left to change
    and every untouched character survives. Run the other way round,
    ``urlsplit`` normalises the scheme and ``urlencode`` re-encodes the
    remainder -- ``HTTP`` becomes ``http`` and ``a%20b`` becomes ``a+b``
    -- rewriting text this library did not author and was asked only to
    mask.
    """
    masked = redact_text('HTTP://H/p?api_key=SECRETVALUE&x=a%20b')

    assert masked == f'HTTP://H/p?api_key={REDACTED}&x=a%20b'


def test_redact_text_masks_a_sensitive_pair_in_prose_that_is_not_a_url(
) -> None:
    """The ratified cost of the rule, asserted rather than assumed.

    Matching on the ``?``/``&`` structure alone cannot tell a URL from a
    sentence that merely contains one, so a sentence like this loses its
    ``hunter2`` as well. That is the accepted trade: the alternative is a
    predicate deciding where URL-ness begins, and three of those have now
    failed open here. Pinning the cost means the next reader meets it as a
    decision with a reason behind it rather than as a surprise.
    """
    masked = redact_text('try the form at /login?password=hunter2 first')

    assert masked == f'try the form at /login?password={REDACTED} first'


def test_redact_text_masks_a_percent_encoded_sensitive_name() -> None:
    """The same seam as S8-H1, one layer down: an encoded name.

    ``api%5Fkey`` is ``api_key`` to every server that will parse it, and
    a caller who wrote it that way has named exactly the secret this
    module already knows. Comparing the raw run of characters against the
    sensitive set sees two different names and masks neither, so the
    value reached ``error['message']``, ``error['cause']`` and
    ``extra['traceback']`` in the clear -- the three surfaces
    ``redact_text`` masks alone. Decoding the name before the lookup
    closes that without reinstating any predicate about the surrounding
    text: it normalises a key, and asks nothing about what the key is
    embedded in.

    The malformed ``%zz`` rides along because the docstring claims
    decoding cannot fail. ``unquote`` returns it verbatim rather than
    raising, so the pair is simply left alone -- a redactor that could
    raise on a caller's own text would turn a logging call into an
    outage, which is the one thing this module promises never to do.
    """
    assert redact_text(
        'Traceback: host/p?api%5Fkey=SECRETVALUE in prose'
    ) == f'Traceback: host/p?api%5Fkey={REDACTED} in prose'

    assert redact_text('host/p?api%zzkey=NOTASECRET') == (
        'host/p?api%zzkey=NOTASECRET')


def test_redact_text_masks_a_declared_name_spelled_with_a_percent(
) -> None:
    """Decoding may widen the sensitive set; it may not narrow it.

    ``redact_query_params`` is the only way a caller names a secret this
    library could not guess, and ``normalise_param_names`` is careful to
    never return fewer names than it was handed. A lookup that tested
    *only* the decoded name would quietly break that promise for a caller
    whose parameter genuinely contains a ``%``: the declared ``x%5Fy``
    would be decoded to ``x_y``, match nothing, and leak the one value
    the caller took the trouble to declare. Testing the name both as
    written and decoded can only ever mask more, which is the direction
    every other bound in this module errs in.
    """
    masked = redact_text(
        'host/p?x%5Fy=SECRETVALUE', extra_params=('x%5Fy',))

    assert masked == f'host/p?x%5Fy={REDACTED}'


def test_redact_url_honours_caller_supplied_parameter_names() -> None:
    """``protocol_info['redact_query_params']`` extends the name set."""
    redacted = redact_url(
        'https://host/p?tenant=acme', extra_params=('tenant',))

    assert 'acme' not in redacted


async def test_a_caller_supplied_parameter_name_is_masked_in_the_envelope(
    http_server: RecordingHTTPServer,
) -> None:
    """The extension point is reachable from ``request()``, not only a unit.

    A caller whose secret rides in a name outside the default set --
    ``?session_id=`` -- must not get it echoed back in ``url``.
    """
    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)

    envelope = await request(
        http_server.url_for('/ok') + '?session_id=SESSIONSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': ['session_id'],
        },
    )

    assert 'SESSIONSECRET' not in envelope['url']
    assert REDACTED in envelope['url']


async def test_caller_supplied_names_extend_the_defaults_they_do_not_replace(
    http_server: RecordingHTTPServer,
) -> None:
    """The set is a union: supplying one name never unmasks ``api_key``."""
    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)

    envelope = await request(
        http_server.url_for('/ok')
        + '?session_id=SESSIONSECRET&api_key=DEFAULTSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': ['session_id'],
        },
    )

    assert 'SESSIONSECRET' not in envelope['url']
    assert 'DEFAULTSECRET' not in envelope['url']


async def test_a_caller_supplied_parameter_name_is_matched_case_insensitively(
    http_server: RecordingHTTPServer,
) -> None:
    """The default set is casefolded, so a caller's addition must be too."""
    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)

    envelope = await request(
        http_server.url_for('/ok') + '?Session-ID=SESSIONSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': ['SESSION-id'],
        },
    )

    assert 'SESSIONSECRET' not in envelope['url']


@pytest.mark.parametrize(
    'malformed',
    [
        pytest.param(None, id='none'),
        pytest.param([], id='empty-list'),
        pytest.param('session_id', id='bare-string-not-a-list'),
        pytest.param(['session_id', None, 7], id='non-string-elements'),
        pytest.param(42, id='not-iterable'),
        pytest.param({'session_id': True}, id='mapping'),
    ],
)
async def test_a_malformed_redact_query_params_never_costs_the_defaults(
    http_server: RecordingHTTPServer,
    malformed: Any,
) -> None:
    """Caller input on the redaction path fails safe: mask more, never less.

    A value of the wrong shape must not raise and must not silently turn
    the default name set off -- that would be a leak caused by a typo.
    """
    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)

    envelope = await request(
        http_server.url_for('/ok') + '?api_key=DEFAULTSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': malformed,
        },
    )

    assert 'DEFAULTSECRET' not in envelope['url']
    assert envelope['ok'] is True


@pytest.mark.parametrize(
    'raw, expected',
    [
        pytest.param(None, frozenset(), id='none'),
        pytest.param([], frozenset(), id='empty-list'),
        pytest.param(
            'Session_ID', frozenset({'session_id'}), id='bare-string'),
        pytest.param(
            ['A', None, 7],
            frozenset({'a', 'none', '7'}),
            id='non-string-elements',
        ),
        pytest.param(42, frozenset({'42'}), id='not-iterable'),
        pytest.param(
            {'Session': True}, frozenset({'session'}), id='mapping-keys'),
    ],
)
def test_normalise_param_names_fails_safe_on_every_shape(
    raw: Any,
    expected: frozenset[str],
) -> None:
    """The one boundary that reads caller input never raises on it."""
    assert normalise_param_names(raw) == expected


def test_redact_url_returns_a_url_it_cannot_parse_unchanged() -> None:
    """Diagnostic data is kept: the URL is not a security boundary.

    Kept, now, is not the same as echoed -- an unparseable URL carrying a
    credential is masked by the test below. This one holds the other half
    of that bargain: one carrying *no* credential still comes back whole,
    so the fallback did not turn a diagnostic into a row of asterisks.
    """
    unparseable = 'http://[oops'

    assert redact_url(unparseable) == unparseable


def test_redact_url_masks_userinfo_in_a_url_it_cannot_parse() -> None:
    """The unparseable URL is the *most* exposed one, not an obscure one.

    ``redact_url`` echoed its input on a parse failure, and M1/AGW-34
    accepted that only on the condition that no caller-visible surface
    reached it uncomposed. The condition was false: ``validated_url``
    builds its "url is not parseable" message with this function, so the
    single input that reaches this branch is by definition the one the
    caller is about to be shown. The unclosed IPv6 bracket is what
    defeats ``urlsplit``; the fallback is regex and does not care.
    """
    unparseable = 'http://user:SUPERSECRET123@[::1/p'

    masked = redact_url(unparseable)

    assert 'SUPERSECRET123' not in masked
    assert masked == f'http://user:{REDACTED}@[::1/p'


def test_redact_url_masks_a_sensitive_pair_in_a_url_it_cannot_parse(
) -> None:
    """The fallback carries the query rule into the failure path too.

    Not only userinfo: a URL that fails to parse can carry its secret in
    a query parameter just as readily, and the branch that gave up on
    one gave up on both.
    """
    unparseable = 'http://[::1/p?api_key=QUERYSECRET'

    masked = redact_url(unparseable)

    assert 'QUERYSECRET' not in masked
    assert masked == f'http://[::1/p?api_key={REDACTED}'


@pytest.mark.parametrize(
    'text, expected',
    [
        pytest.param(
            'https://user:PASS@host/p',
            f'https://user:{REDACTED}@host/p',
            id='parseable-url-keeps-the-name-and-masks-the-password'),
        pytest.param(
            'RetriesExhausted: http://u:PASS@h/p failed, retrying',
            f'RetriesExhausted: http://u:{REDACTED}@h/p failed, retrying',
            id='inside-prose'),
        pytest.param(
            "see http://user:PA'SS@host/p here",
            f'see http://user:{REDACTED}@host/p here',
            id='password-holding-a-quote'),
        pytest.param(
            'see http://user:PA"SS@host/p here',
            f'see http://user:{REDACTED}@host/p here',
            id='password-holding-a-double-quote'),
        pytest.param(
            '//u:PASS@h/p',
            f'//u:{REDACTED}@h/p',
            id='a-protocol-relative-reference-is-an-authority'),
        pytest.param(
            'see //u:PASS@h/p here',
            'see //u:PASS@h/p here',
            id='but-only-at-the-start-of-the-string'),
        pytest.param(
            'error: http://user:UNPARSESECRET@[::1/p failed',
            f'error: http://user:{REDACTED}@[::1/p failed',
            id='unparseable-url-keeps-the-shape-and-masks'),
        pytest.param(
            'error: http://BARETOKEN@[::1/p failed',
            f'error: http://{REDACTED}@[::1/p failed',
            id='bare-userinfo-is-masked-whole'),
        pytest.param(
            'no scheme here user:PASS@host',
            'no scheme here user:PASS@host',
            id='an-email-shaped-string-is-not-userinfo'),
    ],
)
def test_redact_text_masks_url_userinfo(text: str, expected: str) -> None:
    """Userinfo is invisible to a query-pair rule, so it needs its own.

    It carries no ``?`` and no ``=``, and ``redact_text`` is the masker
    the three surfaces that get no second pass rely on:
    ``error['message']``, ``error['cause']`` and the logged traceback.

    Every row is now this pass working, and that is the change F2 made.
    The first two used to be handled by the embedded-URL pass calling
    ``redact_url``, which *dropped* the userinfo and returned
    ``https://host/p``. That drop was a second userinfo rule, it could
    never render what this scan renders -- it rebuilt the URL from
    ``urlsplit``'s parts, and ``urlsplit`` deletes tab, newline and
    carriage return -- and the two publishing different strings is the
    defect. So there is one rule and one rendering: the name survives
    where a ``:`` proves it is a name, the password does not.

    The rows that could never have been reached by the old drop are
    still here and still earn the pass: ``_EMBEDDED_URL`` stops at a
    quote and a backtick, so a password containing one truncated the
    match before the ``@`` and left the tail in the clear, and
    ``urlsplit`` gives up on the unclosed bracket entirely. Both were
    measured leaking with this pass disabled.

    The last three rows are the bounds. A rule that fired without an
    authority would mask every ``user@host`` in prose, so an authority
    is required -- either a scheme, or a separator run at the very start
    of the string, which is RFC 3986's protocol-relative reference and
    what ``urlsplit`` reads a live password out of (F2). Anywhere else
    a ``//`` is a path, and masking there would eat tracebacks.
    """
    assert redact_text(text) == expected


# --- NEW-M1b: the three userinfo bypasses, and the class of them ----------
#
# Three inputs, each defeating the *previous* userinfo rule a different
# way, and all three reproduced reaching a caller-visible surface. They
# are parametrised together because the fix is one construction rather
# than three patches: the rule stopped enumerating what a credential may
# contain and now masks to the last `@` before the authority ends.

USERINFO_BYPASSES = [
    pytest.param(
        'http://user:TAB\tSECRET@[::1/p',
        'SECRET',
        f'http://user:{REDACTED}@[::1/p',
        id='whitespace-inside-the-userinfo'),
    pytest.param(
        'http://u:PARTA@SSPARTB@[::1/p',
        'SSPARTB',
        f'http://u:{REDACTED}@[::1/p',
        id='two-at-signs-end-userinfo-at-the-last'),
    pytest.param(
        'http:/\\/user:BACKSLASHPW@host/p',
        'BACKSLASHPW',
        f'http:/\\/user:{REDACTED}@host/p',
        id='backslash-escaped-scheme-separator'),
]


@pytest.mark.parametrize('url, secret, expected', USERINFO_BYPASSES)
def test_redact_url_masks_every_userinfo_bypass(
    url: str,
    secret: str,
    expected: str,
) -> None:
    r"""Each of these was echoed verbatim by the pattern this replaced.

    The whitespace one matched nothing at all, because the character
    class excluded ``\\s`` -- and ``urlsplit`` *strips* the tab, so the
    server reads a password the pattern could not see. The two-``@`` one
    matched but stopped at the first, publishing the tail of a password
    ``urlsplit`` reads as ``PARTA@SSPARTB``. The backslash one parses,
    into an empty netloc, so neither the pattern nor the unparseable
    fallback ran.
    """
    masked = redact_url(url)

    assert secret not in masked
    assert masked == expected


@pytest.mark.parametrize('url, secret, expected', USERINFO_BYPASSES)
def test_redact_text_masks_every_userinfo_bypass(
    url: str,
    secret: str,
    expected: str,
) -> None:
    """``redact_text`` serves the three surfaces that get no second pass.

    ``error['message']``, ``error['cause']`` and the logged traceback are
    masked by this function alone, so a bypass here is a bypass on all
    three at once.
    """
    masked = redact_text(f'RetriesExhausted: {url} failed')

    assert secret not in masked
    assert masked == f'RetriesExhausted: {expected} failed'


@pytest.mark.parametrize('url, secret, expected', USERINFO_BYPASSES)
def test_redact_value_masks_every_userinfo_bypass(
    url: str,
    secret: str,
    expected: str,
) -> None:
    """``redact_value`` is what writes ``extra['url']`` on a log record.

    It composes both string maskers, so it agrees with the envelope by
    construction -- but only where the maskers themselves agree, which
    is what these rows check.
    """
    masked = redact_value(url)

    assert secret not in masked
    assert masked == expected


def test_redact_url_masks_a_credential_a_successful_parse_missed() -> None:
    r"""A parse that succeeds is not proof there is no credential.

    ``http:/\\/user:PW@host/p`` splits happily -- into an *empty* netloc
    and a path carrying the whole credential -- so the userinfo drop had
    nothing to drop, and the unparseable fallback never ran because
    nothing raised. It was the one shape that masked nothing at all
    (NEW-M1b). The rule no longer hangs off the parse verdict: an
    unhelpful parse now masks exactly as an impossible one does.
    """
    masked = redact_url('http:/\\/user:BACKSLASHPW@host/p')

    assert 'BACKSLASHPW' not in masked


def test_redact_text_masks_the_percent_encoded_spelling_of_a_separator(
) -> None:
    r"""A round trip through ``aiohttp`` rewrites ``\\`` as ``%5C``.

    That normalised spelling is the one that reaches ``error['message']``,
    ``error['cause']`` and the logged traceback, so masking only the
    spelling the caller wrote would mask the URL nobody reads and publish
    the one everybody does.
    """
    reported = 'RetriesExhausted: http:///%5C/user:ROUNDTRIPPW@host/p'

    masked = redact_text(reported)

    assert 'ROUNDTRIPPW' not in masked


@pytest.mark.parametrize(
    'url, secret',
    [
        pytest.param(
            'http://user:PA SS@host/p', 'PA SS', id='space'),
        pytest.param(
            'http://user:PA\nSS@host/p', 'PA\nSS', id='newline'),
        pytest.param(
            'http://user:PA\rSS@host/p', 'PA\rSS', id='carriage-return'),
        pytest.param(
            'http://user:PA\x0bSS@host/p', 'PA\x0bSS', id='vertical-tab'),
        pytest.param(
            'http://user:PA SS@host/p', 'PA SS',
            id='non-breaking-space'),
        pytest.param(
            'http://user:%73%65%63%72%65%74@host/p', '%73%65%63',
            id='percent-encoded'),
        pytest.param(
            'http://üser:pässwörd@host/p', 'pässwörd', id='unicode'),
        pytest.param(
            'http://user:pw@ord@x@host/p', 'ord@x', id='three-at-signs'),
        pytest.param(
            'http://user:' + 'L' * 5000 + '@host/p', 'L' * 5000,
            id='very-long-userinfo'),
        pytest.param(
            'http://' + 'u' * 5000 + ':PW@host/p', ':PW',
            id='very-long-username'),
        pytest.param(
            'http:user:NOSEPARATORPW@host/p', 'NOSEPARATORPW',
            id='no-separator-run-at-all'),
        pytest.param(
            'http:\\\\user:DOUBLEBACKPW@host/p', 'DOUBLEBACKPW',
            id='double-backslash-separator'),
        pytest.param(
            'http:////user:MANYSLASHPW@host/p', 'MANYSLASHPW',
            id='four-slash-separator'),
        pytest.param(
            'http://user:PW@host:8080/p', ':PW@', id='explicit-port'),
        pytest.param(
            'http://user:PW@[::1]:8080/p', ':PW@', id='bracketed-ipv6'),
        pytest.param(
            'HTTP://user:UPPERPW@host/p', 'UPPERPW',
            id='uppercase-scheme'),
        pytest.param(
            'x-custom.scheme+v2://user:CUSTOMPW@host/p', 'CUSTOMPW',
            id='exotic-but-legal-scheme'),
    ],
)
def test_redact_text_masks_hostile_userinfo_spellings(
    url: str,
    secret: str,
) -> None:
    """The bound is where an authority *ends*, not what it contains.

    Every row is a character or a shape that some enumerating rule would
    have to have thought of in advance. None of them is enumerated: the
    rule reads to the last ``@`` before the first ``/``, ``?`` or ``#``,
    so what lies between is masked whatever it is. This is the property
    the three shipped bypasses cost, stated as a test.
    """
    assert secret not in redact_text(url)


@pytest.mark.parametrize(
    'text',
    [
        pytest.param('mailto:bob@corp.example', id='an-email-uri'),
        pytest.param(
            'news:bob@corp.example', id='another-non-netloc-scheme'),
        pytest.param(
            'contact user@corp.example for access', id='an-address-in-prose'),
        pytest.param(
            'no scheme here user:PASS@host', id='no-scheme-at-all'),
        pytest.param('http://host/p', id='a-url-with-no-credential'),
        pytest.param(
            'http://host/p@notuserinfo',
            id='an-at-sign-after-the-path-begins'),
        pytest.param(
            'http://host/p?to=bob@corp.example',
            id='an-at-sign-inside-the-query'),
        pytest.param(
            'http://host/p#frag@ment', id='an-at-sign-inside-the-fragment'),
        # F2's bound. A separator run *is* an authority at the head of
        # the string, and is a path anywhere else -- which is what
        # `urlsplit` reads too. Without the anchor this masks the middle
        # of every traceback that mentions a path.
        pytest.param(
            'see //u:notsecret@h/p here',
            id='a-separator-run-that-is-not-at-the-start'),
        pytest.param(
            'Traceback: File "/x/y.py" line 3 in f  user@host',
            id='a-traceback-naming-a-path-and-a-user'),
    ],
)
def test_redact_text_leaves_a_non_credential_at_sign_alone(
    text: str,
) -> None:
    """Fail-closed is not fail-always: the bounds have to hold too.

    A rule that masked every ``@`` would turn each of these into
    asterisks and cost the diagnostic the string exists to provide. The
    scheme requirement stops the first three; the authority-end rule
    stops the middle three, because an ``@`` after the path begins is
    not in the authority at all; and the anchor on the scheme-less
    branch stops the last two.

    The two non-netloc-scheme rows are the bound F1's fix had to keep.
    A *bare* userinfo carries no ``:``, so nothing about its shape says
    whether it is a token or an address, and the scheme is what
    decides: ``http:`` is in ``urllib.parse``'s ``uses_netloc`` and
    ``mailto:`` and ``news:`` are not. The guard that stood here before
    asked whether a separator run was present instead, which exempted
    ``mailto:`` correctly and ``http:`` by accident (F1).
    """
    assert redact_text(text) == text


def test_redact_text_masks_an_empty_password_rather_than_skipping_it(
) -> None:
    """An empty ``user:@host`` credential is masked, not waved through.

    Nothing is lost by masking an empty value, and the alternative is a
    rule that has to decide *whether* a credential is worth hiding --
    one more question this module declines to ask, and one more branch
    for the next hostile input to aim at. The URL is unparseable so the
    embedded-URL pass cannot reach it: a parseable one has its userinfo
    dropped whole, which is stronger than masking and not what this row
    is about.
    """
    assert redact_text('http://user:@[::1/p') == (
        f'http://user:{REDACTED}@[::1/p')


def test_redact_text_masks_each_of_several_urls_independently() -> None:
    """One string can carry more than one credential, and often does."""
    text = 'ftp://u1:FIRSTPW@h1/a failed over to sftp://u2:SECONDPW@h2/b'

    masked = redact_text(text)

    assert 'FIRSTPW' not in masked
    assert 'SECONDPW' not in masked


def test_redact_text_masking_userinfo_is_idempotent() -> None:
    """A masked string masked again is unchanged.

    ``redact_value`` composes two maskers over the same string and
    ``redact_text`` runs its own passes in sequence, so a rule that
    re-masked its own output would corrupt every composed surface.
    """
    once = redact_text('http://user:IDEMPOTENTPW@host/p')

    assert redact_text(once) == once


def test_redact_text_masks_userinfo_and_a_query_secret_together() -> None:
    """The two rules are independent and both fire on one URL."""
    masked = redact_text('http://user:BOTHPW@host/p?api_key=BOTHKEY')

    assert 'BOTHPW' not in masked
    assert 'BOTHKEY' not in masked


@pytest.mark.parametrize(
    'text',
    [
        pytest.param('http://' * 200 + 'u:p@h', id='200-nested-schemes'),
        pytest.param(
            'http://h/p?' + '&'.join(f'k{i}=v{i}' for i in range(500)),
            id='500-query-pairs'),
        pytest.param('a:' * 100000, id='200kb-of-bare-schemes'),
        pytest.param('a:' * 100000 + '/@', id='200kb-of-schemes-then-an-at'),
        pytest.param('@' * 100000, id='100k-at-signs'),
    ],
)
def test_redact_text_stays_linear_on_a_hostile_string(text: str) -> None:
    """This masker runs inside ``log_failure``, on the event loop.

    The pattern this replaced backtracked for 35s on 200KB, which is an
    outage rather than a slow log line, so the replacement's cost is part
    of its contract. It is a scanner rather than a backtracking pattern:
    the ``@`` and authority-end offsets are indexed in one pass and
    answered by bisection, where a ``str.find`` per scheme would be
    quadratic on a string that is mostly schemes.

    A prior fixer also recorded that a naive fallback to ``redact_text``
    recurses forever; the first two rows are that check, and the
    non-recursive factoring through ``_mask_in_string`` is what keeps
    them terminating.
    """
    started = monotonic_now()

    redact_text(text)

    assert elapsed_since(started) < 2.0


# --- NEW-M1c: the scanner and the parsers, held to one another -------------

#: The password every generated spelling carries, and the only string
#: these rows assert the absence of. A userinfo *name* is kept by design
#: -- a name is diagnostic, a value is not -- so a distinct sentinel for
#: it is what stops that designed behaviour reading as a leak.
FUZZ_SECRET = 'FUZZSECRETPW'

#: The pieces every URL spelling is assembled from. Each list is a place
#: a previous round of findings went wrong, kept as an axis rather than
#: as one example: schemes carrying a deleted character, separator runs
#: in raw *and* percent-encoded form, userinfo with an embedded ``@`` or
#: no username, hosts that make ``urlsplit`` raise, and the three
#: characters that end an authority.
FUZZ_SCHEMES = [
    'http', 'HTTPS', 'a', 'ht\ttp', 'x+y-z.1',
    # A deleted character at the *end* of the scheme, which is a
    # different case from one in the middle: `urlsplit` deletes it
    # and reads a perfectly ordinary `a://user:PW@h/p`, while a
    # scanner reading the raw string finds no `:` adjacent to the
    # scheme run at all and masks nothing. It is the one shape that
    # `_parser_view` alone catches -- the widened separator run does
    # not reach it -- so without this row that half of the fix has no
    # failing test behind it.
    'a\t', 'https\n', 'x\r',
    # **No scheme at all** -- and therefore no colon, which is the
    # point. This generator emitted `f'{scheme}:{separators}...'`
    # unconditionally, so every spelling it produced had one, and a
    # protocol-relative `//u:PW@h/p` was structurally unreachable. That
    # is a real authority: `urlsplit` reads a full netloc out of it and
    # reports the password, `_mask_userinfo` required a scheme and
    # skipped it, and `redact_url` masked it anyway through the parsed
    # netloc -- so the two maskers published different strings and the
    # prose surfaces got the password in the clear (F2). The empty
    # scheme is what makes that shape generable.
    '',
]
FUZZ_SEPARATORS = [
    '', '/', '//', '///', '/\\', '\\\\', '%2F%2F', '%5C%5C',
    '\t//', ' //', '\n//', '/\t/', '\r\n//', '%2F\t/',
    '%20//', '%09//', '%0A//', '%0d%0a//', '/%20/', '\x0b//', '\f//',
    '%2f%5c', ' \t //', '%20%20//',
]
FUZZ_USERINFO = [
    f'user:{FUZZ_SECRET}',
    f'u:PART@SS{FUZZ_SECRET}',
    f':{FUZZ_SECRET}',
    f'us\ter:{FUZZ_SECRET}',
    # A **bare token** -- no colon, so no `password` for a parser to
    # report. This row was generable before and never *evaluated*,
    # because the oracle read `.password` alone and a bare userinfo
    # parses as a `username`: the corpus contained the shape and the
    # differential silently skipped every spelling of it. That is the
    # more dangerous half of a fuzz blind spot, since the row looks
    # present. It is the common way an API token is passed in a URL,
    # and it leaked through both maskers (F1).
    FUZZ_SECRET,
]
FUZZ_HOSTS = ['127.0.0.1', '[::1', '[::1]', 'host:1', 'ho\tst']
FUZZ_TAILS = ['/p', '?a=1', '#f', '']

#: The axis NEW-1 came in through, and the reason it is an axis rather
#: than a row: what leaked was not one character but a whole *class* the
#: corpus could not express. Every spelling above began at the URL, so a
#: leading C0-control or space -- which ``_urlsplit`` lstrips **before**
#: it deletes tab/newline/CR -- was structurally ungenerable, and the
#: 27 C0 characters that are not tab, newline or CR shifted the parser's
#: offsets without shifting the scanner's. ``\x00//user:PW@h/p`` is a
#: protocol-relative reference both readers find a password in; the
#: scanner's ``\A``-anchored separator run found ``\x00`` at offset 0,
#: matched nothing, and published it.
#:
#: Crossed with every other axis rather than sampled, because the two
#: halves of the parser's normalisation interact: ``'\t\x00'`` is the
#: row that fails if the strip and the delete are applied in the wrong
#: order, and only a product produces it next to a separator run that
#: also carries whitespace.
FUZZ_LEADS = [
    '', '\x00', '\x01', '\x1f', ' ', '\x0b', '\x0c',
    '\x00 \x1f', '\t\x00', '  ',
]


def fuzz_spellings() -> set[str]:
    """Generate every URL spelling the differential check reads.

    Each assembled spelling is yielded twice: as written, and as
    ``yarl`` renders it after a round trip. The second is not decoration
    -- it is the string that actually reaches ``error['message']`` and
    the logged traceback, and it is a *different* string: ``aiohttp``
    turns ``http: //u:PW@h/p`` into ``http:///%20//u:PW@h/p``. A fuzz
    that read only what the caller typed declared the space fixed while
    the percent-encoded form still leaked, which is exactly what
    happened while this fix was being written.

    The colon is emitted only for a scheme that exists. It used to be
    unconditional, and that one character was a blind spot with a live
    leak behind it: every spelling this generated had a scheme, so the
    protocol-relative ``//u:PW@h/p`` -- an authority ``urlsplit`` reads
    a password out of -- could not be produced at all, and the two
    maskers disagreed about it for a release (F2).

    :data:`FUZZ_LEADS` is the same lesson a third time, and the reason
    every axis here is a *product* rather than a list of remembered
    inputs. The generator began each spelling at the URL, so nothing it
    produced could carry a leading C0 character -- and that was the one
    half of ``_urlsplit``'s normalisation ``_parser_view`` did not model
    (NEW-1). A corpus is only as strong as the shapes it can express,
    and both times the gap has been a *position* nothing could be
    generated at, not a character nobody thought of.

    Returns:
        The distinct spellings to check, de-duplicated.
    """
    spellings: set[str] = set()
    for parts in itertools.product(
            FUZZ_LEADS, FUZZ_SCHEMES, FUZZ_SEPARATORS, FUZZ_USERINFO,
            FUZZ_HOSTS, FUZZ_TAILS):
        lead, scheme, separators, userinfo, host, tail = parts
        prefix = f'{scheme}:' if scheme else ''
        url = f'{lead}{prefix}{separators}{userinfo}@{host}{tail}'
        spellings.add(url)
        try:
            spellings.add(str(yarl.URL(url)))
        except (ValueError, UnicodeError):
            # A spelling neither reader accepts carries no live password
            # by either reading, so it has nothing to disagree about.
            continue
    return spellings


def a_reader_sees_a_credential(url: str) -> bool:
    """Report whether either URL reader finds a live credential here.

    Two oracles, because two readers decide what a *server* acts on and
    either can be the one that matters. ``urlsplit`` is what this package
    parses with; ``yarl`` is what ``aiohttp`` dispatches with, and the
    two genuinely disagree -- ``yarl`` finds a password in
    ``http:<TAB>//u:PW@h/p`` where ``urlsplit`` sees a path. Asking only
    one would license publishing the credential the other reads.

    **Both userinfo fields are read, not only the password**, and that
    is F1's lesson rather than a widening for its own sake. A *bare*
    userinfo -- ``http:TOKEN@h/p``, which is how a great many APIs pass
    an API token -- has no colon, so every parser reports it as the
    ``username`` and ``password`` is None. An oracle reading
    ``.password`` alone therefore called that spelling clean no matter
    what the maskers did with it: the corpus generated the shape and the
    differential skipped every instance, which is worse than not
    generating it, because the row looks covered. It leaked through both
    maskers for a release.

    So the question this asks is "does either reader find
    :data:`FUZZ_SECRET` anywhere a credential can live?" rather than
    "does either reader populate one specific attribute?" A username
    that is a username is not a secret, but the sentinel only ever
    appears here as one -- :data:`FUZZ_USERINFO` never spells a
    plausible *name* with it -- so a hit is a credential by
    construction.

    Args:
        url: The spelling to read.

    Returns:
        True when either reader reports a userinfo field containing
        :data:`FUZZ_SECRET`.
    """
    for read in (urlsplit, yarl.URL):
        try:
            parsed = read(url)
        except (ValueError, UnicodeError):
            continue
        # `urlsplit` spells it `username` and `yarl` spells it `user`;
        # asking for both by name rather than branching on the reader
        # keeps a third reader one entry away.
        fields = (parsed.password,
                  getattr(parsed, 'username', None),
                  getattr(parsed, 'user', None))
        if any(field and FUZZ_SECRET in field for field in fields):
            return True
    return False


def test_no_url_spelling_leaks_a_credential_a_reader_can_see() -> None:
    r"""The fourth bypass, and the check that a fifth cannot ship.

    Four rounds of findings against this module were one root cause: a
    hand-rolled recogniser deciding where an authority begins, and a URL
    parser deciding differently. Every round fixed the shape that had
    been reported and left the disagreement in place, so the next
    spelling was always available -- ``%2F``, then a tab, then ``/\\``,
    then whitespace.

    This is the property those four rows were each an instance of, and
    it is stated as a *differential*: for every spelling generated, if
    either real reader reports a live credential, then no published
    surface may still carry it. It is deliberately not a list of
    remembered inputs. A list grows by one after each incident; this
    fails on a shape nobody has thought of yet, which is the only kind
    that has ever gone wrong here.

    All four published surfaces are checked. The envelope's ``url``
    (``redact_url``), the log record's ``extra['url']``
    (``redact_value``), and the two prose surfaces -- ``error['message']``
    and ``extra['traceback']`` -- through ``redact_text``, one of them
    wrapped in a real ``ConfigurationError`` sentence because that is
    how the string actually arrives: ``validated_url``'s own "url is not
    parseable" message is the exact path that published a password in
    the clear (M1/AGW-34).

    **A differential is only as good as its oracle, and this one had two
    holes that each hid a live leak.** Both are closed above rather than
    here, which is the right place, but they are worth naming at the
    assertion they were silently weakening:

    * the oracle read ``.password`` alone, and a *bare* userinfo token
      parses as a ``username`` with no password. So ``http:TOKEN@h/p``
      was generated and never evaluated -- the most dangerous shape of
      fuzz gap, because the corpus looks like it covers the case (F1).
    * the generator emitted a colon unconditionally, so a
      protocol-relative ``//u:PW@h/p`` could not be produced at all,
      and the two maskers disagreed about it undetected (F2).

    Measured against the pre-fix module over the corpus these two fixes
    produce: 766 spellings published a credential on at least one
    surface, where the same corpus read by the old ``.password``-only
    oracle reported far fewer. The assertion is zero, not fewer.
    """
    leaks: list[tuple[str, list[str]]] = []
    checked = 0
    for url in sorted(fuzz_spellings()):
        if not a_reader_sees_a_credential(url):
            continue
        checked += 1
        surfaces = {
            'envelope url (redact_url)': redact_url(url),
            "extra['url'] (redact_value)": redact_value(url),
            'message (redact_text)': redact_text(url),
            "extra['traceback'] (redact_text)": redact_text(
                f'ConfigurationError: url is not parseable: {url}'),
        }
        published = sorted(
            name for name, text in surfaces.items()
            if FUZZ_SECRET in text)
        if published:
            leaks.append((url, published))

    assert checked > 1000, (
        f'only {checked} spellings carried a credential either reader '
        f'could see, so this row is close to vacuous -- the generators '
        f'above have stopped producing authorities')
    assert leaks == [], (
        f'{len(leaks)} spellings publish a credential a URL reader can '
        f'see; first five: {leaks[:5]}')


def test_the_two_maskers_agree_on_every_url_spelling() -> None:
    """The class itself, asserted where it actually keeps recurring.

    Six rounds of findings, and rounds 1, 4, 5 and 6 were all the same
    class: two rules for one job, disagreeing. The leak differential
    above cannot see that class until it has already become a leak --
    it asks "is a credential published?", and two maskers can render a
    credential-free string two different ways for a release before one
    of those renderings turns out to be the unmasked one.

    So this asserts the stronger property directly, over the same
    corpus: :func:`redact_url` and :func:`redact_text` return the
    **same string** for every spelling, credential-bearing or not. That
    is what the query corpus has asserted since round 5, and its
    absence here is why F2 shipped -- the userinfo corpus checked only
    for leaks, so ``//u:S3CRET@h/p`` being masked by one masker and
    published by the other did not fail anything.

    Measured against the pre-fix module: 3774 of these spellings were
    rendered differently by the two maskers.
    """
    disagreed: list[tuple[str, str, str]] = []
    corpus = sorted(fuzz_spellings())
    for url in corpus:
        from_url, from_text = redact_url(url), redact_text(url)
        if from_url != from_text:
            disagreed.append((url, from_url, from_text))

    assert len(corpus) > 10000, (
        f'only {len(corpus)} spellings were generated, so this row is '
        f'close to vacuous -- the generators have stopped producing '
        f'authorities')
    assert disagreed == [], (
        f'{len(disagreed)} spellings are masked differently by the two '
        f'maskers, which is how four of the six findings against this '
        f'module began; first five: {disagreed[:5]}')


def test_the_deleted_character_set_is_taken_from_the_parser() -> None:
    """The agreement is by construction, and this is what proves it.

    :func:`_parser_view` reads ``urllib.parse``'s own
    ``_UNSAFE_URL_BYTES_TO_REMOVE`` -- a private name -- with a literal
    fallback for an interpreter that has renamed it. That fallback is
    the failure mode worth guarding: if the attribute disappears, the
    module silently reverts to a *hand-written* list, which is precisely
    the arrangement that produced four bypasses, and nothing would say
    so.

    So this asserts the two agree on the interpreter under test. It
    fails on the interpreter where the constant moves, which is the
    moment to look rather than a moment to discover from a leak.
    """
    assert redaction._URL_IGNORED == frozenset(
        urllib.parse._UNSAFE_URL_BYTES_TO_REMOVE)
    assert redaction._URL_IGNORED == frozenset({'\t', '\n', '\r'})


@pytest.mark.parametrize(
    'lead',
    [
        pytest.param('', id='no-prefix'),
        pytest.param(' ', id='a-space'),
        pytest.param('\x00', id='a-null'),
        pytest.param('\x00 \x1f', id='a-mixed-run'),
        # The order-pinning row. `\t` is *both* a C0 character the
        # parser lstrips and one of the three it deletes, so this run
        # is the only shape that tells CPython's order from its
        # reverse. Strip-then-delete (CPython's, and this module's)
        # consumes `\t\x00` at the strip and reads `mailto:bob@...` --
        # which is what `urlsplit` actually reports for this string.
        # Delete-then-strip removes the `\t` first, leaves `\x00` for
        # the strip, and lands one offset further along, where the
        # scanner reads a *separator run* before `mailto:` and masks
        # the address a plain `mailto:bob@corp.example` keeps.
        #
        # Without this row a module that models the parser's two steps
        # in the wrong order passes every other assertion in the file.
        pytest.param('\t\x00', id='a-tab-then-a-null-pinning-the-order'),
        pytest.param('\t\x00\t ', id='the-same-run-with-more-of-both'),
    ],
)
def test_a_stripped_prefix_does_not_change_what_counts_as_a_credential(
    lead: str,
) -> None:
    """The strip decides *offsets*, and must decide nothing else.

    ``_parser_view``'s lstrip exists so the scanner walks the parser's
    offsets. It is not a licence to reach a different verdict about the
    same URL, and before it was added the module reached one: with no
    prefix ``mailto:bob@corp.example`` kept its address -- ``mailto``
    is not a scheme ``urllib.parse`` reads a netloc after, so a bare
    userinfo under it is an address and not a token -- while
    ``' mailto:bob@corp.example'`` masked it, because the leading space
    was read as a *separator run* and a separator run means "authority,
    mask the bare token too".

    One character of leading whitespace flipping an address into a
    credential is the same disagreement this module keeps having, just
    pointed the other way: over-masking rather than under-masking. It
    costs a diagnostic instead of a secret, which is why it survived,
    but it is still two readings of one string.

    So each row asserts the verdict is the prefix's business only for
    where the credential *is*, never for whether there is one.
    """
    # `mailto:` is outside `uses_netloc`, so a bare userinfo under it
    # is an address: kept, with or without a prefix.
    assert redact_text(
        f'{lead}mailto:bob@corp.example') == f'{lead}mailto:bob@corp.example'
    # `http:` is inside it, so the same shape is a token: masked, with
    # or without a prefix.
    assert redact_text(
        f'{lead}http:TOKENPW@h/p') == f'{lead}http:{REDACTED}@h/p'
    # And a `user:password` is a credential under any scheme at all.
    assert redact_text(
        f'{lead}a:u:ANYSCHEMEPW@h/p') == f'{lead}a:u:{REDACTED}@h/p'


def test_the_stripped_character_set_is_taken_from_the_parser() -> None:
    """The *other* constant, and the one whose absence was NEW-1.

    ``_urlsplit`` normalises in two steps -- lstrip
    ``_WHATWG_C0_CONTROL_OR_SPACE``, then delete
    ``_UNSAFE_URL_BYTES_TO_REMOVE`` -- and this module modelled only the
    second for five rounds. The row above has asserted the deletion set
    comes from the parser since NEW-M1c; nothing asserted the strip set,
    because nothing read one.

    So this is that row's twin, and it exists for the same reason: a
    hand-written copy of either half is a spelling waiting to diverge,
    and the half that was missing is exactly the half that leaked.
    """
    assert redaction._C0_OR_SPACE == (
        urllib.parse._WHATWG_C0_CONTROL_OR_SPACE)
    # The set is C0 plus the space -- 33 characters, which is 27 more
    # than the six `_AUTHORITY_SEPARATORS` used to name.
    assert set(redaction._C0_OR_SPACE) == {
        chr(point) for point in range(0x20)} | {' '}


def test_the_separator_run_consumes_everything_the_parser_may_strip(
) -> None:
    r"""The two halves are derived from one constant, not written twice.

    ``_parser_view`` strips a C0 run at offset 0 because the parser
    does. A URL quoted *inside prose* has no offset 0 of its own,
    though -- ``redact_text`` sees the sentence as the head -- so the
    run has to be consumed by :data:`_AUTHORITY_SEPARATORS` instead,
    and the set that shipped listed ``\\v`` and ``\\f`` but not
    ``\\x00``. That asymmetry was the surviving half of NEW-1: the same
    URL was masked bare and published inside a traceback.

    Deriving the separator set from the strip set is what closes it by
    construction, so this asserts the containment rather than the
    membership of any one character.
    """
    separators = set(redaction._AUTHORITY_SEPARATORS)

    assert set(redaction._C0_OR_SPACE) <= separators
    assert {'/', '\\'} <= separators


def test_an_interpreter_without_the_strip_constant_masks_more(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback must fail **closed**, and be seen to.

    Both constants are private names, so both are read with a fallback,
    and a fallback nothing exercises is how a fallback quietly becomes
    the operative value. The deletion set's fallback is the WHATWG
    three; the strip set's deliberately is **not** a hand-spelled
    WHATWG set, because hand-spelling it is the mistake this whole
    module is a record of.

    It is instead every character at or below U+0020 -- a *superset* of
    any C0-or-space definition CPython could adopt. On an interpreter
    that renamed the constant this module therefore strips more than
    the parser does, which costs a diagnostic character position and
    cannot cost a password. That is the direction the choice has to
    fall, and this row is what proves it falls that way rather than
    asserting it in a comment.
    """
    monkeypatch.delattr(
        urllib.parse, '_WHATWG_C0_CONTROL_OR_SPACE', raising=True)
    reloaded = importlib.reload(redaction)
    try:
        assert not hasattr(urllib.parse, '_WHATWG_C0_CONTROL_OR_SPACE')
        # A superset of the real set, never a subset: masking more is
        # the recoverable failure.
        assert set(reloaded._C0_OR_SPACE) >= {
            chr(point) for point in range(0x21)}
        # And it still masks, which is the only thing the fallback is
        # for -- a fail-closed constant that broke the masker would be
        # no better than a fail-open one.
        masked = reloaded.redact_text('\x00//user:FALLBACKPW@h/p')
        assert 'FALLBACKPW' not in masked
        assert masked == f'\x00//user:{reloaded.REDACTED}@h/p'
    finally:
        # Restored for every later test in the session: this module is
        # imported by name at the top of this file, and leaving the
        # reloaded copy in `sys.modules` would leave those references
        # pointing at a module built from a mutated `urllib.parse`.
        monkeypatch.undo()
        importlib.reload(redaction)


def test_masking_reports_the_string_the_caller_actually_passed() -> None:
    """Scanning the parser's view must not rewrite the diagnostic.

    The scan runs over a copy with tab, newline and carriage return
    deleted, because that is what ``urlsplit`` reads. What comes back
    must still be the caller's own string: the redacted URL is a record
    of what they passed, and quietly deleting characters from it
    misreports the call -- the same reason a masked query pair keeps its
    caller's ``;`` rather than being normalised to ``&``.

    So the tab survives here and the password does not. A fix that
    masked correctly by scanning *and returning* the view would pass
    every row above and fail this one.
    """
    masked = redact_text('http:\t//user:VIEWPW@host/p')

    assert 'VIEWPW' not in masked
    assert masked == f'http:\t//user:{REDACTED}@host/p'


@pytest.mark.parametrize(
    'url, secret',
    [
        pytest.param(
            '\x00//user:KEEPNULLPW@host/p', 'KEEPNULLPW', id='a-null'),
        pytest.param(
            '  //user:KEEPSPACEPW@host/p', 'KEEPSPACEPW', id='two-spaces'),
        pytest.param(
            '\x00 \x1f//user:KEEPMIXEDPW@host/p', 'KEEPMIXEDPW',
            id='a-mixed-c0-and-space-run'),
        pytest.param(
            '\t\x00//user:KEEPBOTHPW@host/p', 'KEEPBOTHPW',
            id='a-run-spanning-both-normalisation-steps'),
    ],
)
def test_a_stripped_prefix_survives_into_the_diagnostic(
    url: str,
    secret: str,
) -> None:
    r"""The strip decides where to mask, never what to report.

    The twin of the row above, for the half of the normalisation NEW-1
    added. ``_parser_view`` now lstrips a leading C0 run because
    ``_urlsplit`` does -- but the caller typed that run, and the
    redacted URL is a record of what they typed. A fix that scanned the
    stripped view *and returned it* would mask correctly and silently
    rewrite every diagnostic, reporting ``//u:***@h`` for a call the
    caller made as ``\\x00//u:PW@h``.

    That is not a cosmetic difference: a leading null is often the
    whole reason a request behaved oddly, and deleting it from the log
    hides the evidence of the very thing being debugged.
    """
    masked = redact_text(url)

    assert secret not in masked
    assert masked == url.replace(secret, REDACTED)
    # And the same string, character for character, on the other three
    # surfaces -- the disagreement between them is what NEW-1 was.
    assert redact_url(url) == masked
    assert redact_value(url) == masked


@pytest.mark.parametrize(
    'url, secret',
    [
        pytest.param(
            'http: //user:SPACEPW@127.0.0.1:1/p', 'SPACEPW',
            id='a-space-before-the-slashes'),
        pytest.param(
            'http:///%20//user:ROUNDTRIPPW@127.0.0.1:1/p', 'ROUNDTRIPPW',
            id='the-same-url-after-an-aiohttp-round-trip'),
        pytest.param(
            'http:\x0b//u:VTABPW@h/p', 'VTABPW',
            id='a-vertical-tab'),
        pytest.param(
            'http:\f//u:FORMFEEDPW@h/p', 'FORMFEEDPW',
            id='a-form-feed'),
        pytest.param(
            'http:%09//u:PCTTABPW@h/p', 'PCTTABPW',
            id='a-percent-encoded-tab'),
        pytest.param(
            'http:%0A//u:PCTLFPW@h/p', 'PCTLFPW',
            id='a-percent-encoded-newline'),
        # F1, and the reason it needs a row of its own rather than a
        # corpus entry. A bare token with **no separator run** is the
        # one authority shape no reader reports *at all*:
        # `urlsplit('http:TOKEN@h/p')` yields an empty netloc and
        # `yarl` yields no user, so even the widened oracle above --
        # which now reads `username` as well as `password` -- has
        # nothing to evaluate. The differential structurally cannot
        # cover this, which is exactly what this parametrisation is
        # for. Masked because `http` is a scheme `urllib.parse` reads a
        # netloc after, and a bare userinfo token is how a great many
        # APIs pass an API key.
        pytest.param(
            'http:BARETOKENPW@h.invalid/p', 'BARETOKENPW',
            id='a-bare-token-with-no-separator-run'),
        pytest.param(
            'https:BARETOKENTLS@h/p', 'BARETOKENTLS',
            id='the-same-under-https'),
        pytest.param(
            'ftp:BARETOKENFTP@h/f', 'BARETOKENFTP',
            id='the-same-under-a-non-http-netloc-scheme'),
        # A colon makes it a `user:password`, which is a credential
        # under *any* scheme -- no netloc table consulted, because
        # nothing about `a:u:PW@h/p` is diagnostic enough to keep.
        pytest.param(
            'a:u:NONNETLOCPW@h/p', 'NONNETLOCPW',
            id='a-password-under-a-scheme-outside-the-netloc-table'),
        # NEW-1. A leading C0 character is the *parser's* business --
        # `_urlsplit` lstrips it before it does anything else -- so
        # unlike the rows above, both readers DO report a password
        # here. These are on this parametrisation anyway because it
        # checks all four surfaces at once, and the surviving half of
        # NEW-1 was one surface (`extra['traceback']`) disagreeing with
        # the other three.
        pytest.param(
            '\x00//user:NULLPW@ftp.invalid/f', 'NULLPW',
            id='a-null-before-a-protocol-relative-authority'),
        pytest.param(
            '\x1f//user:UNITSEPPW@h/p', 'UNITSEPPW',
            id='a-unit-separator-before-the-same'),
        pytest.param(
            '\x01//user:SOHPW@h/p', 'SOHPW',
            id='a-start-of-heading-before-the-same'),
        # The order-dependent row: a tab *then* a null. CPython strips
        # the C0 run first and deletes tab/newline/CR second, so the
        # tab is still present when the strip runs and the strip stops
        # at it. A view built in the other order would see `//` at
        # offset 0 and mask; a view built in CPython's order sees
        # `\t\x00//` and must reach the authority through the
        # separator run instead. Both must mask, which is the point.
        pytest.param(
            '\t\x00//user:ORDERPW@h/p', 'ORDERPW',
            id='a-tab-then-a-null-the-order-dependent-shape'),
        pytest.param(
            '\x00\t//:COLONFIRSTPW@127.0.0.1', 'COLONFIRSTPW',
            id='a-null-then-a-tab-with-an-empty-username'),
        pytest.param(
            '\x00http://user:SCHEMEDPW@h/p', 'SCHEMEDPW',
            id='a-null-before-a-scheme-ful-url'),
    ],
)
def test_whitespace_in_the_separator_run_is_masked(
    url: str,
    secret: str,
) -> None:
    """The half of NEW-M1c the differential fuzz structurally cannot see.

    The fuzz above asks "does a URL reader report a live credential?"
    and masks wherever one does. These rows are the shapes where
    **neither** reader reports one and the credential is published
    anyway -- ``urlsplit`` and ``yarl`` both read ``http: //user:PW@h/p``
    as a path, so the fuzz's oracle says there is nothing to hide, while
    the reviewer's finding is that the password appears in the envelope
    ``url``, ``extra['url']`` and ``extra['traceback']`` in the clear.

    A published credential is a leak whether or not a parser agrees it
    is one: whatever the *remote server* does with the string, a caller
    reading their own logs must not find the password in them. So this
    is the fail-closed direction the module errs in everywhere else --
    mask what looks like an authority, and pay a diagnostic rather than
    a secret.

    Both spellings of each character, raw and percent-encoded, because
    the round trip through ``aiohttp`` rewrites the first into the
    second: ``http: //`` is reported back as ``http:///%20//``. Adding
    whitespace to the raw class and not to the escapes fixed the URL the
    caller wrote and left the one the library prints -- which is exactly
    what happened once while this fix was being written, and is why the
    two lists are now generated from one set rather than typed out.
    """
    assert secret not in redact_text(url)
    assert secret not in redact_value(url)
    assert secret not in redact_url(url)
    assert secret not in redact_text(
        f'ConfigurationError: url is not parseable: {url}')


def test_redact_text_masking_stays_linear_on_a_hostile_separator_run(
) -> None:
    """The widened separator run must not buy back the 35s pattern.

    The run this fix grew now accepts whitespace and eight
    percent-escapes as well as slashes, and it is still a ``*``
    quantifier over an alternation -- the shape that backtracks
    catastrophically when it is allowed to. It does not here, because
    every branch consumes at least one character and no two branches
    match the same one, so a run is capped by its own length.

    That argument is worth an assertion rather than a comment: this
    masker runs inside ``log_failure`` on the event loop, and the
    pattern it replaced took 35s on 200KB. The rows are the shapes that
    stress the new alternation specifically -- a 200KB run of escapes,
    and a 200KB mix of raw separators -- both of which the previous
    hostile-string row did not contain.
    """
    started = monotonic_now()

    redact_text('http:' + '%20' * 66000 + 'u:p@h')
    redact_text('http:' + '/\\ \t' * 50000 + 'u:p@h')

    assert elapsed_since(started) < 2.0


def test_redact_text_masks_a_pair_after_a_legacy_semicolon_separator(
) -> None:
    """``;`` separates query parameters too, and servers still read it.

    Once recommended by HTML 4.01 and still parsed by PHP and servlet
    containers, so ``?a=1;api_key=S`` is two parameters to the server
    that receives it and the second is secret. The pair rule split on
    ``?`` and ``&`` alone, so the secret stayed in the clear in
    ``error['message']`` and the logged traceback (L1).
    """
    text = 'GET failed for host/p?a=1;api_key=LEGACYSECRET now'

    masked = redact_text(text)

    assert 'LEGACYSECRET' not in masked
    assert masked == f'GET failed for host/p?a=1;api_key={REDACTED} now'


def test_redact_text_keeps_the_semicolon_it_masks_after() -> None:
    """Masking reports what was sent; it does not rewrite it to ``&``.

    A caller reading the redacted string is reading a record of their own
    request, so normalising the separator would misreport it.
    """
    assert redact_text('?token=A;api_key=B') == (
        f'?token={REDACTED};api_key={REDACTED}')


def test_redact_url_leaves_a_url_with_nothing_to_mask_alone() -> None:
    """A clean URL is not re-encoded, so it round-trips exactly."""
    clean = 'https://host/p?page=2&sort=name'

    assert redact_url(clean) == clean


# --- N1: the two query rules, held to one another --------------------------

#: The secret every generated query spelling carries, and the string the
#: rows below assert the absence of. Distinct from :data:`FUZZ_SECRET` so
#: a failure names which differential caught it.
QUERY_SECRET = 'QUERYSECRETPW'

#: The separator spellings a query can use between two pairs. ``&`` is the
#: modern one, ``;`` the legacy one HTML 4.01 recommended and PHP, servlet
#: containers and CGI code still parse -- so a server reading
#: ``?x=1;api_key=S`` sees two parameters and the second is secret.
QUERY_SEPARATOR_SPELLINGS = ('&', ';')

#: Insensitive names, to sit *before* the secret. The position is the
#: point: N1 survived a round of fixing because ``parse_qsl`` folds
#: ``;api_key=S`` into the preceding value and masks it there **when the
#: preceding name is itself sensitive** -- so a corpus whose leading name
#: was always ``token`` would have passed while the leak shipped.
QUERY_INNOCENT_NAMES = ('x', 'page', 'tenant')

#: Sensitive names, as written and percent-encoded, because the lookup
#: normalises both and a differential using one spelling would not see
#: the two maskers disagreeing about the other.
QUERY_SECRET_NAMES = ('api_key', 'token', 'api%5Fkey', 'PASSWORD')

#: Spellings of the secret value itself. The value is what a rebuild
#: re-encodes, so this is the dimension on which a masker that rewrites
#: the query -- ``urlencode`` turning ``a b`` into ``a+b`` -- diverges
#: from one that substitutes in place, while both still hide the secret.
QUERY_SECRET_VALUES = (
    QUERY_SECRET,
    f'{QUERY_SECRET}%20tail',
    f'{QUERY_SECRET}+tail',
    f'{QUERY_SECRET}=padded',
)


#: The URL component the secret is planted in, as a format string over
#: ``{pair}``. **This is the axis the round-4 generator did not have**,
#: and its absence is why this differential passed while
#: ``/p;api_key=S`` leaked: every spelling it emitted was a ``?``-
#: introduced query with no fragment, so the one thing the two maskers
#: still disagreed about -- which *component* each one scans -- was held
#: constant across the entire corpus.
#:
#: Each row is somewhere a real server reads a real parameter:
#:
#: * ``;`` in a path segment -- RFC 3986 path parameters, which PHP and
#:   Java servlet containers parse as parameters.
#: * a fragment, plain and after a query. OAuth 2.0's implicit grant
#:   returns ``#access_token=...``, so a fragment pair is a live
#:   credential by design rather than by accident.
#: * a query *after* a fragment, which is not valid grammar and is
#:   exactly why it leaked: no component-scoped rule owns it, and
#:   ``urlsplit`` files the whole thing under ``fragment``.
#: * userinfo, whose secret no pair rule can see at all -- it is here so
#:   the equality assertion covers the masker that *does* see it.
#:
#: Deliberately *not* here: a bare ``/p/api_key=S`` path segment with no
#: delimiter introducing it. Nothing parses that as a parameter -- ``;``
#: is the path-parameter spelling and it has its own rows -- so masking
#: it would mean masking any ``name=value`` anywhere in any prose, which
#: is a bound this module has never claimed. The rows below are each a
#: place a real parser reads a real parameter; that is the line.
QUERY_COMPONENTS = (
    '/p?{pair}',
    '/p;{pair}',
    '/p;{pair}/more',
    '/p#{pair}',
    '/p#frag?{pair}',
    '/p?a=1#{pair}',
    '/p;a=1?b=2#c=3&{pair}',
    '/p?a=1#frag;{pair}',
)


def query_spellings() -> Iterator[str]:
    """Generate query strings carrying a secret, spelled every way.

    A product rather than a list of remembered inputs, for the reason
    :func:`fuzz_spellings` is one: a list grows by one after each
    incident, and every finding this module has produced was a spelling
    nobody had listed yet. What is generated is the *disagreement
    surface* the two query rules share -- separator, the secret's
    position, the name before it, the value's encoding, the scheme, what
    follows, and now the **component the secret sits in**.

    That last axis is this round's addition and the reason the round-4
    corpus missed a live leak: it emitted only ``?``-introduced queries
    with no fragment, so it varied everything about a pair *except*
    where in the URL it was. :data:`QUERY_COMPONENTS` varies exactly
    that, which is what makes the equality assertion in
    :func:`test_the_two_maskers_agree_on_every_query_spelling` catch a
    component one masker scans and the other does not.

    Yields:
        A URL or scheme-less URL carrying :data:`QUERY_SECRET` under a
        sensitive name, somewhere a server would read it.
    """
    prefixes = ('https://host', 'host', 'https://user@host',
                f'https://user:{QUERY_SECRET}@host')
    tails = ('', 'page=2', 'x=a%20b', 'flag')
    for prefix, component, separator, name, value in itertools.product(
            prefixes, QUERY_COMPONENTS, QUERY_SEPARATOR_SPELLINGS,
            QUERY_SECRET_NAMES, QUERY_SECRET_VALUES):
        yield prefix + component.format(pair=f'{name}={value}')
    for prefix, separator, innocent, name, value in itertools.product(
            prefixes, QUERY_SEPARATOR_SPELLINGS, QUERY_INNOCENT_NAMES,
            QUERY_SECRET_NAMES, QUERY_SECRET_VALUES):
        pair = f'{name}={value}'
        # The secret *second*, after an insensitive name -- the shape the
        # `parse_qsl` folding argument does not cover, and the one N1 was
        # reported as.
        yield f'{prefix}/p?{innocent}=1{separator}{pair}'
        # The same, but with the insensitive name in a *different*
        # component from the secret: a rule that masks only the
        # component holding the first pair leaks the second.
        yield f'{prefix}/p;{innocent}=1?{pair}'
        yield f'{prefix}/p?{innocent}=1#{pair}'
        # And the secret first, which that argument does cover, so a
        # regression reinstating it still fails on the row above.
        for tail in tails:
            yield f'{prefix}/p?{pair}{separator}{tail}' if tail else (
                f'{prefix}/p?{pair}')


def test_the_two_maskers_agree_on_every_query_spelling() -> None:
    """The property N1 was an instance of, asserted as a differential.

    This module has two string maskers with two query rules.
    :func:`redact_url` serves the envelope ``url`` -- on the ``ok=True``
    and ``ok=False`` paths alike -- and ``validated_url``'s
    ``ConfigurationError``, both of which call it *alone*.
    :func:`redact_text` serves ``error['message']``, ``error['cause']``
    and the logged traceback. They must agree about what a query
    separator is, and twice now they have not.

    The first round taught ``;`` to :func:`redact_text`'s rule only, on
    the reasoning that ``parse_qsl`` folds the legacy pair into the
    preceding value -- true only where that preceding name is itself
    sensitive, and then written into :func:`redact_url`'s docstring as a
    claim of coverage that was false for exactly the surfaces which never
    reach the other masker. ``?x=1;api_key=SECRETPW`` was published in
    the clear.

    A row per reported spelling cannot catch the *next* divergence, which
    is the only kind that has ever shipped here. This asserts the
    invariant instead: over the generated corpus neither masker leaks,
    **and the two return the same string**. Equality is the stronger
    claim and the right one -- a difference in rendering is how a
    divergence first shows itself, one release before it becomes a
    difference in what is masked.

    The equality half is what caught this round, and only after the
    corpus grew the axis it was missing. The round-4 generator varied
    separator, position, name and encoding but emitted ``?``-introduced
    queries with no fragment throughout, so it held constant the one
    thing the maskers still disagreed about -- the *component*. With
    :data:`QUERY_COMPONENTS` varying that, ``/p;api_key=S`` and
    ``/p#frag?api_key=S`` are in the corpus: measured against the
    pre-fix module, 640 of these 1664 spellings published the secret
    through :func:`redact_url` and 320 were rendered differently by the
    two maskers. Both assertions are zero, not fewer.
    """
    leaked: list[tuple[str, list[str]]] = []
    disagreed: list[tuple[str, str, str]] = []
    checked = 0
    for spelling in sorted(set(query_spellings())):
        checked += 1
        from_url = redact_url(spelling)
        from_text = redact_text(spelling)
        published = sorted(
            name for name, text in (
                ('envelope url (redact_url)', from_url),
                ('message (redact_text)', from_text),
                ("extra['url'] (redact_value)", redact_value(spelling)),
            ) if QUERY_SECRET in text)
        if published:
            leaked.append((spelling, published))
        if from_url != from_text:
            disagreed.append((spelling, from_url, from_text))

    assert checked > 1500, (
        f'only {checked} spellings were generated, so this row is close '
        f'to vacuous -- the generator has stopped producing queries')
    assert leaked == [], (
        f'{len(leaked)} query spellings publish a secret; first five: '
        f'{leaked[:5]}')
    assert disagreed == [], (
        f'{len(disagreed)} spellings are masked differently by the two '
        f'maskers, which is how the last two leaks began; first five: '
        f'{disagreed[:5]}')


def test_the_two_maskers_run_one_scan_over_one_surface() -> None:
    """The agreement is by construction, and this is what proves it.

    The differential above says the two maskers agree *today*, over the
    shapes generated today. This says they cannot be made to disagree by
    the edit that has now produced *four* findings -- and the fourth is
    why this row no longer asserts what it used to.

    Rounds one to three were about the separator: one rule knew ``;``
    and the other did not, so the fix was a shared
    :data:`_QUERY_SEPARATORS` and this test asserted both rules derived
    from it. They did, and they diverged anyway -- because a shared
    constant says what a separator *is* and nothing about *where each
    masker looks*. ``redact_url`` scanned ``parts.query`` alone, so
    ``/p;api_key=S`` and ``/p#frag?api_key=S`` were masked by
    ``redact_text`` and published in the clear by ``redact_url``.

    So the structural claim is now the stronger one: there is no second
    query rule to keep in step. ``redact_url`` masks by calling the same
    :func:`_mask_in_string` that :func:`redact_text` calls, over the
    whole URL. That is asserted three ways -- the shared entry point
    still masks alone, no component-scoped rule has reappeared, and the
    two maskers render every component identically -- so a future edit
    reintroducing a component-scoped rule fails here rather than one
    release later from a leak.
    """
    # 1. The shared scan is what does the masking: it alone, with no
    #    parse and no component split, already masks every spelling.
    for component in ('/p;api_key=', '/p?api_key=', '/p#frag?api_key=',
                      '/p#api_key=', '/a;b?c&api_key='):
        raw = f'https://h{component}{QUERY_SECRET}'
        assert redaction._mask_in_string(
            raw, redaction._sensitive_names(())) == redact_url(raw)

    # 2. No component-scoped masking rule exists to fall out of step.
    #    `_mask_query` was that rule; its absence is the fix.
    assert not hasattr(redaction, '_mask_query')
    assert not hasattr(redaction, '_QUERY_SEPARATOR_SPLIT')

    # 3. Every introducer and separator behaves identically through both
    #    maskers, in whichever component it lands in.
    for introducer in redaction._PAIR_INTRODUCERS:
        for separator in redaction._QUERY_SEPARATORS:
            spelling = (
                f'https://h/p{introducer}x=1{separator}'
                f'api_key={QUERY_SECRET}')
            expected = (
                f'https://h/p{introducer}x=1{separator}'
                f'api_key={REDACTED}')
            assert redact_url(spelling) == expected
            assert redact_text(spelling) == expected

    assert redaction._QUERY_DELIMITERS == (
        redaction._PAIR_INTRODUCERS + redaction._QUERY_SEPARATORS)


def test_redact_url_masking_stays_linear_on_a_hostile_query() -> None:
    """``redact_url`` runs inside ``log_failure`` too, on the event loop.

    The rule this fix rewrote no longer goes through ``parse_qsl`` and
    ``urlencode``; it splits on a character class and substitutes. The
    linearity that matters is therefore this function's, not only
    :func:`redact_text`'s -- the envelope ``url`` is built on the same
    path as the log record, and a masker taking 35s on 200KB is an outage
    on either.

    A split on a one-character class has nothing to backtrack, so these
    rows are about volume rather than a catastrophic shape: 200KB of
    separators carrying no pairs at all, then 200KB of real pairs under
    each separator spelling.
    """
    started = monotonic_now()

    redact_url('https://h/p?' + ';' * 200000)
    redact_url('https://h/p?' + '&'.join(
        f'k{index}=v{index}' for index in range(20000)))
    redact_url('https://h/p?' + ';'.join(
        f'api_key=v{index}' for index in range(20000)))

    assert elapsed_since(started) < 2.0


def test_redact_headers_keeps_names_and_masks_only_the_values() -> None:
    """A caller can still see *that* a credential header was sent."""
    redacted = redact_headers({
        'Authorization': 'Bearer x',
        'X-API-KEY': 'k',
        'Accept': 'application/json',
    })

    assert redacted['Authorization'] == REDACTED
    assert redacted['X-API-KEY'] == REDACTED
    assert redacted['Accept'] == 'application/json'


def test_redact_cookies_masks_every_value() -> None:
    """A cookie name carries no signal about whether its value is secret."""
    assert redact_cookies({'session': 's', 'theme': 'dark'}) == {
        'session': REDACTED,
        'theme': REDACTED,
    }


def test_redact_payload_masks_by_key_name_down_to_the_documented_depth(
) -> None:
    """Masking is by key name, recursive, and bounded (R8, E9)."""
    payload: dict[str, Any] = {
        'password': 'p0',
        'nested': {'token': 't1', 'items': [{'secret': 's2'}]},
        'kept': 'visible',
    }

    redacted = redact_payload(payload)

    assert redacted['password'] == REDACTED
    assert redacted['nested']['token'] == REDACTED
    assert redacted['nested']['items'][0]['secret'] == REDACTED
    assert redacted['kept'] == 'visible'
    assert payload['password'] == 'p0'


def test_redact_payload_echoes_data_below_the_depth_bound_verbatim(
) -> None:
    """The bound is part of the contract, so it is asserted, not implied."""
    deep: dict[str, Any] = {'password': 'top'}
    for _ in range(PAYLOAD_REDACTION_DEPTH + 2):
        deep = {'level': deep}

    redacted = redact_payload(deep)
    probe = redacted
    for _ in range(PAYLOAD_REDACTION_DEPTH + 2):
        probe = probe['level']

    assert probe['password'] == 'top'


@pytest.mark.parametrize(
    'payload',
    [
        pytest.param('a raw string body', id='str'),
        pytest.param(b'raw bytes body', id='bytes'),
        pytest.param(42, id='int'),
        pytest.param(None, id='none'),
    ],
)
def test_redact_payload_returns_a_non_mapping_payload_unchanged(
    payload: Any,
) -> None:
    """A non-mapping payload has no key names to match, so it is echoed."""
    assert redact_payload(payload) == payload


def test_redact_value_dispatches_on_type_for_the_logger() -> None:
    """One dispatcher, so a logged value and an envelope value agree."""
    assert redact_value('https://host/p?token=x').endswith(REDACTED)
    assert redact_value('not a url') == 'not a url'
    assert redact_value({'Authorization': 'Bearer x'}) == {
        'Authorization': REDACTED,
    }
    assert redact_value(200) == 200


def test_redact_value_treats_an_unparseable_string_as_plain_text() -> None:
    """A string that will not parse as a URL is logged as it came in."""
    assert redact_value('http://[oops') == 'http://[oops'


def test_redact_value_masks_a_url_that_has_no_netloc() -> None:
    """The seam itself, not the symptom: a netloc-less URL is masked.

    ``redact_value`` is the single entry point every ``extra`` value in
    ``log_failure`` passes through, and it once gated on a whole-string
    "has a scheme *and* a netloc" test that a URL like this fails -- so
    the string was echoed into the log in the clear. The end-to-end tests
    below prove the symptom is gone; this one names the seam, so
    restoring that gate fails here rather than only somewhere downstream.
    """
    masked = redact_value(
        'http:///p?session_id=SESSIONSECRET&api_key=APIKEYSECRET',
        extra_params=('session_id',),
    )

    assert 'SESSIONSECRET' not in masked
    assert 'APIKEYSECRET' not in masked
    assert masked == f'http:///p?session_id={REDACTED}&api_key={REDACTED}'


def test_redact_value_masks_a_string_identically_to_the_envelope() -> None:
    """The by-construction property, not one more predicate outcome.

    ``redact_value`` twice guarded masking behind a predicate asking "is
    this a URL?", and the predicate twice failed open: first demanding a
    scheme *and* a netloc, then demanding a scheme, which a URL the caller
    wrote without one does not have. Both times the log got the secret in
    the clear while the envelope masked it.

    So the assertion is *equality with the envelope's own masker* rather
    than the mere absence of the secret. Absence is what a third predicate
    could pass while still disagreeing with the envelope somewhere else;
    equality is the contract, and it holds only because ``redact_value``
    composes :func:`redact_url` unconditionally.
    """
    url = 'host/p?api_key=APIKEYSECRET&session_id=SESSIONSECRET'

    assert redact_value(url, extra_params=('session_id',)) == redact_url(
        url, extra_params=('session_id',))
    assert redact_value(url, extra_params=('session_id',)) == (
        f'host/p?api_key={REDACTED}&session_id={REDACTED}')


@pytest.mark.parametrize(
    'delimiter', [' ', "'", '"', '`', '<', '>'],
    ids=['space', 'single-quote', 'double-quote', 'backtick', 'lt', 'gt'])
def test_redact_value_masks_a_secret_that_contains_a_url_delimiter(
    delimiter: str,
) -> None:
    """The embedded-URL pass stops at these; the whole-string pass does not.

    ``redact_text`` ends a match at the first character that cannot appear
    unescaped in a URL, so a secret *value* carrying one had its tail left
    outside the match and echoed -- two thirds of ``?api_key=my secret
    key``. Nothing covered a delimiter inside the value, so both the leaking
    and the fixed code passed. The second, whole-string ``redact_url`` pass
    is what closes it, and this is what fails if it is removed.
    """
    masked = redact_value(f'http://host/p?api_key=SECRET{delimiter}TAIL')

    assert 'TAIL' not in masked
    assert masked == f'http://host/p?api_key={REDACTED}'


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        pytest.param(
            'ValueError: http://host/p?api_key=SECRETVALUE',
            f'ValueError: http://host/p?api_key={REDACTED}',
            id='the-exception-name-parses-as-the-scheme'),
        pytest.param(
            'ValueError: bad /a?debug=1 at http://host/p?api_key=SECRETVALUE',
            f'ValueError: bad /a?debug=1 at http://host/p?api_key={REDACTED}',
            id='the-whole-string-query-name-is-not-sensitive'),
    ])
def test_redact_value_runs_the_text_pass_before_the_whole_string_pass(
    text: str,
    expected: str,
) -> None:
    """Order is load-bearing, so it is pinned rather than left to reading.

    A traceback line beginning ``ValueError:`` has a truthy URL scheme, so
    the whole-string pass will happily consume the lot. Run *second*, after
    the embedded URL is already masked, that is harmless. The two ways of
    getting it wrong both fail here: reaching for the whole-string pass
    *instead of* the text pass because the scheme is truthy leaks the
    second case entirely -- it parses under the insensitive name ``debug``
    and masks nothing -- and running it first loses the exception's
    capitalisation in the first case to scheme normalisation.
    """
    assert redact_value(text) == expected


@pytest.mark.filterwarnings(
    # aiohttp 3.14 deprecates the type, but `request(auth=...)` still accepts
    # it and E9 still names it, so the redactor must keep handling it. The
    # filter covers constructing one here, not any behaviour under test.
    'ignore:BasicAuth is deprecated:DeprecationWarning',
)
@pytest.mark.parametrize(
    'wrap',
    [
        pytest.param(lambda auth: auth, id='bare'),
        pytest.param(lambda auth: {'credentials': auth}, id='nested'),
    ],
)
def test_a_basic_auth_object_never_survives_redaction(
    wrap: Any,
) -> None:
    """An auth object carries a password in a field no key name reveals."""
    secret = BasicAuth('user', 'AUTHSECRET')

    assert 'AUTHSECRET' not in repr(redact_value(wrap(secret)))
    assert 'AUTHSECRET' not in repr(redact_payload(wrap(secret)))


@pytest.mark.filterwarnings(
    'ignore:BasicAuth is deprecated:DeprecationWarning',
)
def test_a_basic_auth_object_at_the_depth_bound_is_still_masked() -> None:
    """The bound stops matching *key names*, not recognising an auth object.

    At exactly the bound the recursion stops descending and echoes what it
    is holding. A ``BasicAuth`` carries its password in a field no key name
    reveals, so echoing it there was an E9 hole: the type check runs before
    the bound, not after it. (Deeper still, the documented bound applies as
    ``test_redact_payload_echoes_data_below_the_depth_bound_verbatim``
    asserts -- unbounded recursion over caller data is the thing the bound
    exists to refuse.)
    """
    deep: Any = BasicAuth('user', 'AUTHSECRET')
    for _ in range(PAYLOAD_REDACTION_DEPTH):
        deep = {'level': deep}

    assert 'AUTHSECRET' not in repr(redact_payload(deep))


# --- E10: the trap keys are gone -------------------------------------------


async def test_e10_the_truthiness_trap_key_is_absent(
    ok_envelope: GatewayResponse,
) -> None:
    """Old code reading the removed alias fails loudly (E10, R8-AC6).

    Its removal is the point: a shim would preserve the trap, because the
    natural check against it was truthy on every path including failure.
    """
    with pytest.raises(KeyError):
        ok_envelope['api_response']


async def test_e10_the_renamed_duration_key_is_absent(
    ok_envelope: GatewayResponse,
) -> None:
    """``tat`` is `latency` now, with no alias left behind (E10)."""
    assert 'tat' not in ok_envelope
    assert 'latency' in ok_envelope


async def test_the_removed_keys_are_gone_not_reshaped(
    ok_envelope: GatewayResponse,
) -> None:
    """Every key the release removed is absent from the envelope."""
    for removed in (
        'api_response', 'tat', 'external_call_request_time', 'error_message',
    ):
        assert removed not in ok_envelope


# --- E11: a failure still carries the response -----------------------------


async def test_e11_a_404_with_a_json_body_keeps_the_whole_response(
    failed_envelope: GatewayResponse,
) -> None:
    """``ok=False`` never costs the caller the response body (E11).

    The single most common way a consumer uses a failure response, and the
    case an exception-only control flow drops silently.
    """
    assert failed_envelope['ok'] is False
    assert failed_envelope['status_code'] == 404
    assert failed_envelope['text']
    assert failed_envelope['json'] == {'error': 'no such thing'}
    assert failed_envelope['headers']
    assert failed_envelope['error'] is not None
    assert failed_envelope['error']['code'] == 'HTTP_STATUS'


def test_e11_finalise_error_only_adds_the_error_and_flips_ok() -> None:
    """Nothing the protocol already wrote is cleared (E11).

    Asserted at the function rather than only end-to-end, because this is
    the one place a future change could quietly reset the envelope to a
    failure skeleton.
    """
    envelope = new_envelope(url='http://host/p', protocol='HTTP', payload={})
    envelope['status_code'] = 404
    envelope['headers'] = {'Content-Type': 'application/json'}
    envelope['cookies'] = {'session': REDACTED}
    envelope['text'] = '{"error": "gone"}'
    envelope['json'] = {'error': 'gone'}
    envelope['protocol_details'] = {'hops': 1}

    finalised = finalise_error(
        envelope, HttpStatusError('gone', 404), started=monotonic_now())

    assert finalised is envelope
    assert finalised['status_code'] == 404
    assert finalised['headers'] == {'Content-Type': 'application/json'}
    assert finalised['cookies'] == {'session': REDACTED}
    assert finalised['text'] == '{"error": "gone"}'
    assert finalised['json'] == {'error': 'gone'}
    assert finalised['protocol_details'] == {'hops': 1}
    assert finalised['ok'] is False
    assert finalised['error'] is not None


# --- the constructors are the only ones ------------------------------------


def test_the_response_shape_is_declared_exactly_once() -> None:
    """One definition, so no protocol can drift from the contract."""
    assert files_containing(r'class GatewayResponse') == ['utils/envelope.py']


def test_only_the_envelope_module_constructs_an_envelope() -> None:
    """``new_envelope`` and the two finalisers are the only builders.

    Everything else fills the envelope it was handed. That rule is what
    makes the key set invariant, and it is the rule the transport helper
    used to break by returning a dict of its own.
    """
    assert files_containing(r'GatewayResponse\(') == ['utils/envelope.py']


def test_finalise_ok_clears_any_error_and_marks_the_call_successful(
) -> None:
    """The success finaliser is the only way ``ok`` becomes True."""
    envelope = new_envelope(url='http://host/p', protocol='HTTP', payload={})

    finalised = finalise_ok(envelope, status_code=201, started=monotonic_now())

    assert finalised is envelope
    assert finalised['ok'] is True
    assert finalised['error'] is None
    assert finalised['status_code'] == 201


def test_an_unfinalised_envelope_reads_as_a_failure() -> None:
    """A skeleton that somehow escaped must not read as success."""
    assert new_envelope(
        url='http://host/p', protocol='HTTP', payload={})['ok'] is False


# --- R10: the logger tree --------------------------------------------------


def test_r10_the_library_attaches_exactly_one_null_handler() -> None:
    """A library that adds a real handler duplicates its host's output."""
    handlers = logging.getLogger('asyncio_gateway').handlers

    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.NullHandler)


async def test_r10_a_remote_failure_logs_one_warning_with_the_exception(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A status the caller may legitimately expect logs at warning.

    Exactly one record, at the one conversion point -- not one per layer.
    """
    http_server.respond('/missing', status=404, body=b'{}',
                        headers=JSON_HEADERS)
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    await http_call(http_server.url_for('/missing'))

    records = [
        record for record in caplog.records
        if record.name.startswith('asyncio_gateway')
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert 'HttpStatusError' in records[0].traceback
    assert records[0].code == 'HTTP_STATUS'
    assert records[0].status_code == 404


async def test_r10_a_transport_failure_logs_one_error_with_the_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transport or configuration failure is this library's problem."""
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    await http_call(f'http://127.0.0.1:{closed_port()}/x')

    records = [
        record for record in caplog.records
        if record.name.startswith('asyncio_gateway')
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert 'ConnectError' in records[0].traceback
    assert records[0].code == 'CONNECT'


async def test_r10_a_successful_call_logs_nothing(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing failed, so there is nothing for an operator to read."""
    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    await http_call(http_server.url_for('/ok'))

    assert [
        record for record in caplog.records
        if record.name.startswith('asyncio_gateway')
    ] == []


async def test_r10_the_logger_leaks_no_credential(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log and the envelope share one redactor, so they agree.

    A URL masked in the returned envelope but written to the log in the
    clear would defeat the point of masking it at all.
    """
    http_server.respond('/missing', status=404, body=b'{}',
                        headers=JSON_HEADERS)
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    await http_call(
        http_server.url_for('/missing') + '?api_key=LOGGEDSECRET',
        data={'password': 'PAYLOADSECRET'},
        request_type='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer HEADERSECRET',
        },
    )

    for record in caplog.records:
        rendered = f'{record.getMessage()} {record.__dict__}'
        for secret in (
            'LOGGEDSECRET', 'PAYLOADSECRET', 'HEADERSECRET',
        ):
            assert secret not in rendered, secret


async def test_r10_a_caller_supplied_parameter_name_is_masked_in_the_log(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log and the envelope share *one* set, extensions included.

    Asserted over the *whole* rendered record -- message, ``extra`` and
    the redacted ``extra['traceback']`` string -- rather than over the
    ``url`` field alone. A log that disagreed with the envelope about
    what is a secret would break the shared-redactor contract in exactly
    the place nobody looks, and an assertion narrower than that claim is
    how the exception-message channel survived the first fix.
    """
    http_server.respond('/missing', status=404, body=b'{}',
                        headers=JSON_HEADERS)
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    await request(
        http_server.url_for('/missing') + '?session_id=SESSIONSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': ['session_id'],
        },
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('asyncio_gateway')
    ]
    assert len(records) == 1
    assert 'SESSIONSECRET' not in render_record(records[0])
    assert REDACTED in records[0].url


async def test_r10_a_caller_declared_secret_reaches_no_surface_on_a_404(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A caller-declared secret survives nowhere, on the commonest failure.

    An ordinary 404 is the single most likely failure a consumer handles,
    and it reaches the caller through four channels at once: the
    envelope's ``url``, ``error['message']``, ``error['cause']`` and the
    log record including its redacted ``extra['traceback']`` string. Each
    is fed from a different call site, so this asserts the *absence of
    the raw secret across the whole surface* rather than the redaction of
    one field -- a per-field assertion is exactly what let the
    exception-message channel keep leaking after ``url`` was fixed.
    """
    http_server.respond('/missing', status=404, body=b'{}',
                        headers=JSON_HEADERS)
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    result = await request(
        http_server.url_for('/missing') + '?session_id=SESSIONSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'headers': dict(JSON_HEADERS),
            'redact_query_params': ['session_id'],
        },
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('asyncio_gateway')
    ]
    assert len(records) == 1
    error = result['error']
    assert error is not None

    surfaces = {
        'repr(result)': repr(result),
        "envelope['url']": result['url'],
        "error['message']": error['message'],
        "error['cause']": str(error['cause']),
        'log record': render_record(records[0]),
    }
    for surface_name, rendered in surfaces.items():
        assert 'SESSIONSECRET' not in rendered, surface_name

    # The secret is masked, not merely absent: a redactor that dropped the
    # parameter entirely would pass every assertion above and lose the
    # diagnostic value the envelope exists for.
    assert REDACTED in result['url']
    assert REDACTED in error['message']


# A URL with no netloc. aiohttp refuses it before it opens a connection,
# so the call needs no server and the exception that reaches
# `transport_error_for` is a real `InvalidUrlClientError` rather than a
# stand-in. Its `str()` is the whole URL, query string included -- which is
# the property under test. The shape is chosen deliberately: a netloc-less
# URL is exactly what used to defeat `redact_value`, whose whole-string
# gate demanded both a scheme and a netloc before it would mask at all and
# echoed anything else straight into the log. These two tests therefore
# also pin that fix.
NO_NETLOC_URL = 'http:///p?session_id=SESSIONSECRET&api_key=APIKEYSECRET'


async def test_r10_a_transport_failure_leaks_no_foreign_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure whose text this library did not author is redacted too.

    The 404 guard below it proves nothing about this: on a 404 the chain
    is a single ``HttpStatusError`` this library raised with an
    already-redacted message, and ``error['cause']`` is None. The
    transport path is the opposite -- ``transport_error_for`` lifts the
    message straight out of ``aiohttp``'s own exception, and the whole
    chain is then rendered into the log -- so every surface has to be
    asserted against a *foreign* string.
    """
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    result = await request(
        NO_NETLOC_URL,
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'redact_query_params': ['session_id'],
        },
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('asyncio_gateway')
    ]
    assert len(records) == 1
    error = result['error']
    assert error is not None
    # The chain really did reach aiohttp. Without this the test would pass
    # against a message this library wrote, proving nothing.
    assert 'InvalidUrlClientError' in str(error['cause'])

    surfaces = {
        'repr(result)': repr(result),
        "error['message']": error['message'],
        "error['cause']": str(error['cause']),
        'log record': render_record(records[0]),
    }
    for surface_name, rendered in surfaces.items():
        for secret in ('SESSIONSECRET', 'APIKEYSECRET'):
            assert secret not in rendered, f'{secret} in {surface_name}'

    assert REDACTED in error['message']
    assert REDACTED in str(error['cause'])


async def test_r10_a_default_secret_is_masked_with_no_redact_query_params(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The built-in names are the base contract, not a caller opt-in.

    ``redact_query_params`` is absent entirely here. A leak on this path
    would not be the extension feature failing -- it would be E9 itself
    failing for every caller who never asked for anything.
    """
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    result = await request(
        NO_NETLOC_URL,
        data={},
        protocol='HTTP',
        protocol_info={'request_type': 'GET'},
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('asyncio_gateway')
    ]
    assert len(records) == 1
    error = result['error']
    assert error is not None

    surfaces = {
        'repr(result)': repr(result),
        "error['message']": error['message'],
        "error['cause']": str(error['cause']),
        'log record': render_record(records[0]),
    }
    for surface_name, rendered in surfaces.items():
        assert 'APIKEYSECRET' not in rendered, surface_name

    assert REDACTED in error['message']
    # `session_id` is not a built-in name and was not declared, so it is
    # echoed. Asserted, not merely unasserted: it is what proves the two
    # halves of the set are distinct rather than the whole query string
    # being blanked.
    assert 'SESSIONSECRET' in error['message']


async def test_r10_a_scheme_less_url_reaches_the_log_url_field_masked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``extra['url']`` -- and only that field -- is clean end to end.

    Under ``'HTTP'`` a scheme-less URL is dispatched as the caller wrote
    it, so it is the string ``log_failure`` puts in ``extra['url']``.
    Nothing about that string carries a ``scheme://``, which is precisely
    why ``redact_value`` must not gate its ``redact_url`` pass behind a
    predicate.

    The assertion is deliberately **scoped to that one field**, because
    that field alone is masked by ``redact_value``. The surfaces masked by
    ``redact_text`` alone -- ``error['message']``, ``error['cause']`` and
    ``extra['traceback']`` -- reach the caller through a different route
    and are covered by the whole-record guard immediately below. Read this
    test as pinning the ``redact_value`` composition, not as a claim about
    the record as a whole.
    """
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    await request(
        'host/p?session_id=SESSIONSECRET&api_key=APIKEYSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'redact_query_params': ['session_id'],
        },
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('asyncio_gateway')
    ]
    assert len(records) == 1
    logged_url = records[0].url
    assert 'SESSIONSECRET' not in logged_url
    assert 'APIKEYSECRET' not in logged_url
    assert logged_url == (
        f'host/p?session_id={REDACTED}&api_key={REDACTED}')


async def test_r10_a_scheme_less_url_leaks_on_no_surface_at_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole envelope and the whole record, not one field (S8-H1).

    The test above is scoped to ``extra['url']`` because ``redact_value``
    composes ``redact_url`` over that field unconditionally. Three
    surfaces get no such second pass: ``error['message']``,
    ``error['cause']`` and ``extra['traceback']`` are masked by
    ``redact_text`` alone. While that function matched only strings
    carrying an explicit ``scheme://``, a caller who wrote
    ``host/p?api_key=...`` had the secret echoed into all three in the
    clear -- masked in the ``url`` sitting right beside them, which is
    what made it survive a green suite for so long.

    So this asserts the *whole* rendered record and the *whole* envelope
    rather than any one attribute: a per-field assertion is exactly the
    shape that let the leak hide. Both a built-in name and a
    caller-declared one ride along, because the two halves of the
    sensitive set reach ``redact_text`` by different routes and only one
    of them is this library's own.
    """
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    result = await request(
        'host/p?api_key=APIKEYSECRET&session_id=SESSIONSECRET',
        data={},
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'redact_query_params': ['session_id'],
        },
    )

    records = [
        record for record in caplog.records
        if record.name.startswith('asyncio_gateway')
    ]
    assert len(records) == 1
    error = result['error']
    assert error is not None
    # The chain really did reach aiohttp, so the strings under test are
    # foreign text rather than a message this library wrote and redacted
    # at the point it authored it.
    assert 'InvalidUrlClientError' in str(error['cause'])

    surfaces = {
        'repr(result)': repr(result),
        "envelope['url']": result['url'],
        "error['message']": error['message'],
        "error['cause']": str(error['cause']),
        "extra['traceback']": records[0].traceback,
        'log record': render_record(records[0]),
    }
    for surface_name, rendered in surfaces.items():
        for secret in ('APIKEYSECRET', 'SESSIONSECRET'):
            assert secret not in rendered, f'{secret} in {surface_name}'

    # Masked, not merely absent: a redactor that dropped the query string
    # would satisfy every assertion above and lose the diagnostic the
    # operator reading this record actually needs.
    for surface_name in (
        "error['message']", "error['cause']", "extra['traceback']",
    ):
        assert REDACTED in surfaces[surface_name], surface_name


async def test_r10_a_percent_encoded_secret_leaks_on_no_surface_either(
    http_server: RecordingHTTPServer,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recurring seam, re-checked end to end on an encoded name.

    S8-H1 was a leak into ``error['message']``, ``error['cause']`` and
    ``extra['traceback']`` -- the three surfaces no second masking pass
    ever reaches. Its first fix compared the parameter name exactly as it
    was written, so ``api%5Fkey`` was a name this module had never heard
    of, and the same three surfaces carried the secret again for the same
    reason as before.

    The failure is injected rather than driven through a real URL, and
    the reason is the point of the test. Hand ``request()`` an encoded
    name and *yarl* normalises ``%5F`` back to ``_`` long before any
    exception text is built, so aiohttp's own errors never carry the
    encoded spelling and a test written that way passes whether the name
    is decoded before the lookup or not. But ``describe_failure``
    redacts whatever the chain stringifies to, and that text is mostly
    *not* this library's: a client that echoes the request line it was
    given, or a protocol whose stack never parses a URL at all, hands
    these surfaces the caller's own encoding intact. This raises exactly
    such a foreign exception so the guard covers the case yarl happens to
    hide on the HTTP path.

    The whole record and the whole envelope are asserted, not one field
    of each, because a per-field assertion is the shape that let the
    original leak survive a green suite.
    """
    async def leaky(self: HttpRequest) -> GatewayResponse:
        cause = RuntimeError(
            'upstream rejected GET host/p?api%5Fkey=APIKEYSECRET')
        # An empty wrapper message is the shape that puts foreign text on
        # `message` as well as on `cause`: `describe_failure` falls back
        # to the deepest link when the wrapper has nothing of its own.
        raise ConnectError('') from cause

    monkeypatch.setattr(HttpRequest, 'handle_request', leaky)
    caplog.set_level(logging.DEBUG, logger='asyncio_gateway')

    result = await http_call(http_server.url_for('/anything'))

    records = [
        record for record in caplog.records
        if record.name.startswith('asyncio_gateway')
    ]
    assert len(records) == 1
    error = result['error']
    assert error is not None

    surfaces = {
        'repr(result)': repr(result),
        "error['message']": error['message'],
        "error['cause']": str(error['cause']),
        "extra['traceback']": records[0].traceback,
        'log record': render_record(records[0]),
    }
    for surface_name, rendered in surfaces.items():
        assert 'APIKEYSECRET' not in rendered, surface_name

    # Masked, not merely absent: a redactor that dropped the query string
    # would satisfy every assertion above and lose the diagnostic the
    # operator reading this record actually needs.
    for surface_name in (
        "error['message']", "error['cause']", "extra['traceback']",
    ):
        assert REDACTED in surfaces[surface_name], surface_name


async def test_r10_cancellation_propagates_rather_than_becoming_an_envelope(
    http_server: RecordingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CancelledError`` is not a failed request and must not be caught.

    It is a ``BaseException`` on every supported interpreter; swallowing it
    would make a cancelled task look like it completed.
    """
    async def cancelled(self: HttpRequest) -> GatewayResponse:
        raise asyncio.CancelledError

    monkeypatch.setattr(HttpRequest, 'handle_request', cancelled)

    with pytest.raises(asyncio.CancelledError):
        await http_call(http_server.url_for('/anything'))


def test_r10_every_module_logs_through_the_package_tree() -> None:
    """No module names its logger by hand, so the tree stays one tree."""
    for name, source in package_sources():
        for match in re.finditer(r'logging\.getLogger\(([^)]*)\)', source):
            assert match.group(1) == '__name__', name


async def test_the_processor_slots_carry_what_the_caller_returned(
    http_server: RecordingHTTPServer,
) -> None:
    """Each processor's result lands in its own key, never in the body.

    The slots exist so a caller's own hook cannot reshape the envelope: it
    is handed the envelope and its return value is filed beside it.
    """
    async def pre(response: GatewayResponse, tag: str) -> str:
        return f'pre-{tag}'

    async def post(response: GatewayResponse) -> str:
        return f'post-{response["status_code"]}'

    http_server.respond('/ok', status=200, body=b'{}', headers=JSON_HEADERS)

    envelope = await request(
        http_server.url_for('/ok'),
        data={},
        protocol='HTTP',
        protocol_info={'request_type': 'GET', 'headers': dict(JSON_HEADERS)},
        pre_processor_config={'function': pre, 'params': {'tag': 'x'}},
        post_processor_config={'function': post},
    )

    assert envelope['pre_processor_response'] == 'pre-x'
    assert envelope['post_processor_response'] == 'post-200'
    assert set(envelope) == EXPECTED_KEYS


# --- classification of what the resilience layer wraps ---------------------


def test_an_open_circuit_is_reported_as_such_not_as_a_transport_failure(
) -> None:
    """The breaker's own signal keeps its identity through conversion."""
    envelope = finalise_error(
        new_envelope(url='http://host/p', protocol='HTTP', payload={}),
        CircuitOpenError('circuit open for http://host/p'),
        started=monotonic_now(),
    )

    assert envelope['status_code'] == 503
    assert envelope['error'] is not None
    assert envelope['error']['code'] == 'CIRCUIT_OPEN'


def test_a_library_bug_wrapped_by_the_resilience_layer_still_propagates(
) -> None:
    """A wrapped ``KeyError`` is a bug, not a transport failure (R10).

    The resilience layer wraps *every* exception the call raised, so
    classifying on the wrapper would turn this library's own defects into
    ``ok=False`` envelopes -- which is exactly what hid most of the audit.
    """
    try:
        raise RetriesExhausted() from KeyError('missing_key')
    except RetriesExhausted as wrapper:
        with pytest.raises(KeyError):
            transport_error_for(wrapper)


def test_a_bare_transport_exception_is_classified_without_a_wrapper(
) -> None:
    """The classifier reads the cause when there is one, else the error."""
    classified = transport_error_for(asyncio.TimeoutError())

    assert classified.code == 'TIMEOUT'
    assert classified.status_code == 504
    assert str(classified)


def test_an_empty_resilience_wrapper_is_still_a_transport_failure() -> None:
    """A wrapper with no cause at all cannot be blamed on a library bug."""
    classified = transport_error_for(RetriesExhausted())

    assert classified.code == 'TRANSPORT'
    assert str(classified) == 'RetriesExhausted'


async def test_the_conversion_point_is_the_only_place_that_converts(
    http_server: RecordingHTTPServer,
) -> None:
    """One place turns a typed error into an envelope (R10, the one rule).

    The entry point holds the only ``except AsyncGatewayError`` in the
    package; every layer below it raises and none builds a failure shape.
    """
    converters = files_containing(r'except AsyncGatewayError')

    assert converters == ['asyncio_gateway.py']
    assert hasattr(entrypoint, 'log_failure')

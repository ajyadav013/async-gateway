"""Tests for the request tracer (spec R26 · R30-AC6, Step 19).

The one question these answer, in every shape it comes in: do the trace
results attached to a response describe **that** response? The tracer
stored its results in a single dict on the ``TraceConfig`` -- a
session-level object the README documents as caller-supplied -- so the
honest answer was no: two concurrent calls through one tracer interleaved
their timings, and a stale ``on_request_exception`` from a failed call
annotated the next successful one (H17).

The decisive test here is
:func:`test_two_concurrent_calls_through_one_tracer_do_not_interleave`:
two requests under ``asyncio.gather`` through **one** caller-supplied
``trace_config``, each asserted to carry only its own events. It is
written to fail against the shared-dict design rather than merely to pass
against this one -- with results on the ``TraceConfig`` the two envelopes
are the *same object*, which is what it asserts they are not.

Everything is driven through the public ``request()`` entry point against
the loopback recording server wherever the claim is about the envelope,
because the pairing of ``request_tracer`` with the response it describes
is a property only the entry point produces. The callbacks aiohttp will
not fire against a well-behaved loopback server -- the DNS four, and the
queueing pair -- are exercised directly against a real
``TraceConfig``-built context, which is what makes "all 15 registered
callbacks are exercised" true rather than aspirational.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import aiohttp

import pytest

from async_gateway.async_gateway import request
from async_gateway.utils.envelope import GatewayResponse
from async_gateway.utils.request_tracer import (
    REQUEST_START_KEY,
    ResultsCollector,
    begin_trace_scope,
    request_tracer,
)

from tests.fixtures.http_server import RecordingHTTPServer

#: The credential a caller sends and must never get back on the envelope.
#: A literal in a test, not a secret: the whole point is to grep the
#: returned trace for it.
SECRET_TOKEN = 'Bearer s21-tracer-token'

#: Every callback :func:`request_tracer` registers, by the results key it
#: writes. Fifteen, which is the count the spec insists on against the
#: audit's prose figure of fourteen.
ALL_CALLBACKS = (
    'on_request_start',
    'on_connection_queued_start',
    'on_connection_queued_end',
    'on_connection_create_start',
    'on_request_redirect',
    'on_connection_reuseconn',
    'on_dns_cache_hit',
    'on_dns_cache_miss',
    'on_dns_resolvehost_start',
    'on_dns_resolvehost_end',
    'on_connection_create_end',
    'on_request_chunk_sent',
    'on_response_chunk_received',
    'on_request_exception',
    'on_request_end',
)


async def _get_with(
    http_server: RecordingHTTPServer,
    path: str = '/body',
    **info: Any,
) -> GatewayResponse:
    """Fetch ``path`` through the entry point with extra protocol_info.

    Args:
        http_server: The loopback recording server fixture.
        path: The path to fetch; the caller registers its response.
        info: Extra ``protocol_info`` keys.

    Returns:
        The finalised envelope ``request()`` returned.
    """
    return await request(
        url=http_server.url_for(path),
        protocol='HTTP',
        protocol_info={'request_type': 'GET', **info},
    )


def _fire(tracer: aiohttp.TraceConfig, signal: str, params: Any) -> Any:
    """Return the coroutine one registered callback produces.

    Reaches the callback the way ``aiohttp`` does -- through the signal
    the tracer registered it on -- so a callback registered on the wrong
    signal is not silently exercised by name.

    Args:
        tracer: The tracer whose callback to invoke.
        signal: The signal name, e.g. ``'on_dns_cache_hit'``.
        params: The parameter object that signal carries.

    Returns:
        The awaitable the callback returned.
    """
    context = tracer.trace_config_ctx()
    callback = getattr(tracer, signal)[0]
    return callback(SimpleNamespace(), context, params), context


# --- R26-AC1: results belong to one request (H17) --------------------------

async def test_two_concurrent_calls_through_one_tracer_do_not_interleave(
    http_server: RecordingHTTPServer,
) -> None:
    """The decisive test: one shared tracer, two concurrent requests.

    This is the shape H17 is about, and the one the old design could not
    survive. ``trace_config`` is documented as caller-supplied and a
    ``TraceConfig`` is session-level, so a caller who builds one tracer
    and reuses it -- the obvious thing to do -- had both calls writing
    into the same dict. The two envelopes did not merely hold similar
    numbers; they held **the same object**, so whichever request finished
    last overwrote the other's timings and both reported them.

    Asserted three ways, because "each carries only its own events" needs
    all three to mean anything: the two mappings are distinct objects,
    they are not equal (the timings genuinely differ), and each records
    exactly one request's start -- not two interleaved.
    """
    http_server.respond('/slow', body=b'slow', delay=0.05)
    http_server.respond('/fast', body=b'fast')
    tracer = request_tracer()

    slow, fast = await asyncio.gather(
        _get_with(http_server, '/slow', trace_config=[tracer]),
        _get_with(http_server, '/fast', trace_config=[tracer]),
    )

    slow_trace = slow['request_tracer'][0]
    fast_trace = fast['request_tracer'][0]
    assert slow['text'] == 'slow'
    assert fast['text'] == 'fast'
    # Distinct objects: the old design handed both envelopes one dict.
    assert slow_trace is not fast_trace
    # And distinct *values*: two objects holding the last writer's
    # numbers would satisfy the identity check and still be the bug.
    assert slow_trace[REQUEST_START_KEY] != fast_trace[REQUEST_START_KEY]
    # The slow call was delayed server-side, so its total must exceed the
    # fast one's. Under the shared dict this comparison was meaningless.
    assert slow_trace['on_request_end'] > fast_trace['on_request_end']


async def test_a_failed_call_does_not_annotate_the_next_successful_one(
    http_server: RecordingHTTPServer,
) -> None:
    """The stale-key half of H17, which is the more insidious half.

    ``on_request_exception`` was written into the shared dict and never
    cleared, so the *next* call through the same tracer -- a call that
    succeeded, against a healthy server -- came back carrying the
    previous failure's exception message. A consumer branching on the
    key's presence saw a successful response reporting an exception.
    """
    tracer = request_tracer()
    unreachable = 'http://127.0.0.1:1/nothing-listens-here'

    failed = await request(
        url=unreachable,
        protocol='HTTP',
        protocol_info={'request_type': 'GET', 'trace_config': [tracer]},
    )
    http_server.respond('/body', body=b'ok')
    succeeded = await _get_with(http_server, trace_config=[tracer])

    assert failed['ok'] is False
    assert 'on_request_exception' in failed['request_tracer'][0]
    assert succeeded['ok'] is True
    assert 'on_request_exception' not in succeeded['request_tracer'][0]
    assert 'on_request_exception_message' not in succeeded['request_tracer'][0]


async def test_a_reused_tracer_reports_a_fresh_mapping_per_call(
    http_server: RecordingHTTPServer,
) -> None:
    """Sequential reuse, which is the same defect without the concurrency.

    The concurrent test above proves two *simultaneous* calls do not
    share. This proves the mapping is rebound per call rather than merely
    per task -- two calls one after the other on one tracer must still be
    two mappings, or the first envelope's numbers change under the caller
    when the second call runs.
    """
    http_server.respond('/body', body=b'ok')
    tracer = request_tracer()

    first = await _get_with(http_server, trace_config=[tracer])
    first_end = first['request_tracer'][0]['on_request_end']
    second = await _get_with(http_server, trace_config=[tracer])

    assert first['request_tracer'][0] is not second['request_tracer'][0]
    # The first envelope's value did not move when the second call ran.
    assert first['request_tracer'][0]['on_request_end'] == first_end


async def test_nothing_accumulates_on_the_shared_trace_config(
    http_server: RecordingHTTPServer,
) -> None:
    """The structural claim: the ``TraceConfig`` itself holds no results.

    ``results_collector`` stays readable -- it is public API and two
    modules depend on it -- but it is a *view*, not storage. If the
    attribute ever becomes a plain dict again this fails, which is the
    point: the identity check above could be satisfied by copying at the
    end while still accumulating on the shared object in between.
    """
    http_server.respond('/body', body=b'ok')
    tracer = request_tracer()

    await _get_with(http_server, trace_config=[tracer])

    assert isinstance(tracer.results_collector, ResultsCollector)
    assert not isinstance(tracer.results_collector, dict)


# --- R26-AC2: the reuseconn baseline (M11) --------------------------------

async def test_a_reused_connection_is_still_measured_from_the_start(
    http_server: RecordingHTTPServer,
) -> None:
    """M11: ``on_connection_reuseconn`` must not overwrite the baseline.

    It used to assign ``loop.time()`` -- an absolute timestamp -- to
    ``on_request_start``, mid-request. Every later callback then measured
    from the moment the pooled connection was picked up rather than from
    when the request began, so a keep-alive request that spent 50ms
    waiting for the server reported only the part after connection reuse.
    Latency dashboards were silently, systematically low.

    Driven on the caller's own session, because a pool that survives
    between calls is the only way to reach the reuse path at all, and
    trace configs are a session-constructor argument.
    """
    http_server.respond('/body', body=b'ok', delay=0.05)
    tracer = request_tracer()
    session = aiohttp.ClientSession(trace_configs=[tracer])
    try:
        await _get_with(http_server, session=session)
        await _get_with(http_server, session=session)
        results = dict(tracer.results_collector)
    finally:
        await session.close()

    assert 'on_connection_reuseconn' in results
    # A relative delta among relative deltas: the whole request took at
    # least the server's delay, and reuse happened inside it.
    assert 0.0 <= results['on_connection_reuseconn'] < results[
        'on_request_end']
    # The baseline survived: `on_request_end` still spans the server
    # delay. Under M11 the baseline was replaced at reuse, so this
    # measured only the post-reuse remainder and fell under the delay.
    assert results['on_request_end'] >= 0.05


def test_the_reuseconn_baseline_is_not_an_absolute_timestamp() -> None:
    """The other half of M11: an absolute clock reading among deltas.

    Asserted as a magnitude rather than by re-deriving the loop clock. A
    delta on a loopback request is well under a second; ``loop.time()``
    on any running process is orders of magnitude larger. The old code
    stored the latter under a key whose thirteen siblings all held the
    former, so a consumer charting them together got one bar the height
    of the process uptime.
    """
    tracer = request_tracer()

    async def reuse() -> dict[str, Any]:
        awaitable, context = _fire(
            tracer, 'on_connection_reuseconn', SimpleNamespace(
                connection=SimpleNamespace()))
        await awaitable
        return context.results

    results = asyncio.run(reuse())

    assert results['on_connection_reuseconn'] < 1.0
    # And it did not invent a baseline it was never given.
    assert REQUEST_START_KEY not in results


# --- R26-AC3: the exception is a string, and carries no credential (M20) ---

async def test_the_exception_report_is_a_string_not_a_live_exception(
    http_server: RecordingHTTPServer,
) -> None:
    """M20: the key named ``..._message`` must hold a message.

    It held the exception *object*. An ``aiohttp.ClientResponseError``
    carries ``.request_info.headers``, so the caller's ``Authorization``
    rode out on the envelope the README demonstrates printing -- and an
    exception object is not serialisable either, so a consumer who
    JSON-dumped the envelope got a ``TypeError`` instead of a report.
    """
    tracer = request_tracer()

    envelope = await request(
        url='http://127.0.0.1:1/nothing-listens-here',
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'trace_config': [tracer],
            'headers': {'Authorization': SECRET_TOKEN},
        },
    )

    message = envelope['request_tracer'][0]['on_request_exception_message']
    assert isinstance(message, str)
    assert not isinstance(message, BaseException)
    assert message != ''


async def test_the_exception_report_carries_no_authorization_value(
    http_server: RecordingHTTPServer,
) -> None:
    """The credential half of M20, asserted over the whole trace.

    Scanned across every value rather than only the message key: the
    claim invariant E9 makes is about the returned envelope, not about
    one field of it, and a future callback that stringifies the wrong
    object would slip past a narrower assertion.
    """
    tracer = request_tracer()

    envelope = await request(
        url='http://127.0.0.1:1/nothing-listens-here',
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'trace_config': [tracer],
            'headers': {'Authorization': SECRET_TOKEN},
        },
    )

    rendered = repr(envelope['request_tracer'])
    assert SECRET_TOKEN not in rendered
    assert 's21-tracer-token' not in rendered


async def test_the_exception_report_masks_the_callers_own_query_params(
) -> None:
    """The caller's ``redact_query_params`` must reach the tracer too.

    Found by making the failure path report a trace at all: the tracer
    called ``unwrap_cause`` with the built-in names only, so ``api_key``
    was masked and a caller's declared ``session_id`` was not. The
    exception here is ``aiohttp``'s ``InvalidUrlClientError``, whose
    ``str()`` is the whole URL including its query string -- so the
    unmasked parameter landed on the envelope verbatim.

    Invariant E9 promises no credential value on the returned envelope,
    and the caller's extension names are part of what the library was
    told is a credential. Masking most of them is the failure mode this
    asserts against.
    """
    # A URL with no netloc, because the *message* has to carry the query
    # string for this to test anything. A refused connection stringifies
    # to host:port alone and would pass whatever the redaction did;
    # aiohttp's InvalidUrlClientError stringifies to the whole URL.
    envelope = await request(
        url='http:///p?session_id=SESSIONSECRET&api_key=APISECRET',
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'redact_query_params': ['session_id'],
        },
    )

    message = envelope['request_tracer'][0]['on_request_exception_message']
    assert envelope['ok'] is False
    # The message really is the URL, so the assertions below bite. Without
    # this the test would pass against an error text that never had a
    # query string in it -- which is exactly how the first draft of this
    # test passed while the leak was live.
    assert 'session_id' in message
    assert '***redacted***' in message
    rendered = repr(envelope['request_tracer'])
    assert 'SESSIONSECRET' not in rendered
    assert 'APISECRET' not in rendered


async def test_a_failed_call_still_reports_the_trace_it_recorded(
    http_server: RecordingHTTPServer,
) -> None:
    """A failure is exactly when the trace is worth having.

    ``request_tracer`` used to be assigned only where a response came
    back, so every envelope with ``ok=False`` reported ``[]`` -- and the
    one event a failed call uniquely produces, ``on_request_exception``,
    was recorded faithfully by the tracer and then discarded by the
    envelope. This is the same shape as invariant E11 ("``ok=False``
    never costs the caller the response body") applied to the trace.
    """
    envelope = await request(
        url='http://127.0.0.1:1/nothing-listens-here',
        protocol='HTTP',
        protocol_info={'request_type': 'GET'},
    )

    assert envelope['ok'] is False
    assert envelope['request_tracer'] != []
    assert 'on_request_exception' in envelope['request_tracer'][0]


async def test_the_exception_report_is_json_serialisable() -> None:
    """The consequence a consumer actually hits, stated as its own test.

    ``json.dumps(envelope)`` on the old design raised ``TypeError:
    Object of type ClientConnectorError is not JSON serializable``. The
    envelope is the library's whole output; a caller logging or storing
    it is the documented usage.
    """
    import json

    tracer = request_tracer()
    envelope = await request(
        url='http://127.0.0.1:1/nothing-listens-here',
        protocol='HTTP',
        protocol_info={'request_type': 'GET', 'trace_config': [tracer]},
    )

    assert json.dumps(envelope['request_tracer'])


# --- R26-AC4/AC5: all fifteen callbacks, annotated and exercised -----------

def test_the_tracer_registers_exactly_fifteen_callbacks() -> None:
    """Fifteen, which is the count the spec asserts against the audit's 14.

    Counted off the ``TraceConfig``'s own signals rather than off a list
    in this file, so a callback dropped from the registration block fails
    here instead of being quietly untested.
    """
    tracer = request_tracer()

    registered = [
        name for name in ALL_CALLBACKS
        if len(getattr(tracer, name)) == 1
    ]

    assert len(ALL_CALLBACKS) == 15
    assert registered == list(ALL_CALLBACKS)


@pytest.mark.parametrize('signal, params', [
    pytest.param(
        'on_connection_queued_start', SimpleNamespace(), id='queued-start'),
    pytest.param(
        'on_connection_queued_end', SimpleNamespace(), id='queued-end'),
    pytest.param(
        'on_dns_cache_hit', SimpleNamespace(host='h'), id='dns-cache-hit'),
    pytest.param(
        'on_dns_cache_miss', SimpleNamespace(host='h'), id='dns-cache-miss'),
    pytest.param(
        'on_dns_resolvehost_start', SimpleNamespace(host='h'),
        id='dns-resolve-start'),
    pytest.param(
        'on_dns_resolvehost_end', SimpleNamespace(host='h'),
        id='dns-resolve-end'),
])
async def test_a_callback_the_loopback_server_never_triggers_still_works(
    signal: str,
    params: SimpleNamespace,
) -> None:
    """The six callbacks a healthy loopback request cannot reach.

    A connection to ``127.0.0.1`` resolves without DNS and a pool with no
    contention never queues, so these six never fire in this suite's
    integration tests. Exercising them directly is what makes R26-AC5 --
    "a test exercises every one of the 15 registered callbacks at least
    once" -- true rather than aspirational, and it is the check that
    would catch one of them raising ``AttributeError`` on a params object
    whose shape changed under an aiohttp upgrade.
    """
    tracer = request_tracer()

    awaitable, context = _fire(tracer, signal, params)
    await awaitable

    assert isinstance(context.results[signal], float)
    assert context.results[signal] >= 0.0


async def test_a_callback_firing_before_the_baseline_reports_zero_not_negative(
) -> None:
    """The edge case R26 names: a failure before ``on_request_start``.

    With no baseline recorded there is nothing to subtract from. Zero is
    the honest reading of "no time has been measured"; the alternative,
    subtracting from a missing key, is a ``TypeError`` inside a trace
    callback -- which aiohttp raises through the request the caller was
    trying to make, turning an observability detail into a failed call.
    """
    tracer = request_tracer()

    awaitable, context = _fire(
        tracer, 'on_request_end', SimpleNamespace(
            method='GET', url='http://h/p', headers={},
            response=SimpleNamespace()))
    await awaitable

    assert context.results['on_request_end'] == 0.0
    assert context.results['on_request_end'] >= 0.0


async def test_the_full_callback_set_is_exercised_by_a_real_request(
    http_server: RecordingHTTPServer,
) -> None:
    """The integration half: a real request fires the connection path.

    Complements the direct invocations above. Between the two, every one
    of the fifteen has been run -- these against a real transport, those
    against the signals a loopback server cannot provoke.
    """
    http_server.respond('/body', body=b'ok')
    tracer = request_tracer()

    envelope = await _get_with(http_server, trace_config=[tracer])

    results = envelope['request_tracer'][0]
    for key in ('on_request_start', 'on_connection_create_start',
                'on_connection_create_end', 'on_request_end'):
        assert key in results, key
    assert all(
        isinstance(results[key], float)
        for key in results
        if key != 'is_redirect')


async def test_the_chunk_received_callback_fires_when_aiohttp_fires_it(
) -> None:
    """``on_response_chunk_received`` is registered, and it works.

    Deliberately *not* asserted on an envelope, because it does not
    appear on one and that is a property of code outside this story.
    ``aiohttp`` fires this signal from ``resp.text()`` / ``resp.read()``,
    and ``read_response`` consumes the body through
    ``resp.content.iter_chunked`` instead -- measured on aiohttp 3.14.3:
    ``text()`` fires it once, ``iter_chunked`` not at all. So the
    callback is correct and unreachable on the library's own read path.

    Recorded rather than fixed: making it fire means changing how
    ``helpers/internal/request_helper.py`` reads the body, which is the
    capped-reader S15 owns and outside this story's file boundary. What
    is asserted here is what R26-AC5 actually requires -- the registered
    callback runs and records a relative delta -- so an upgrade that
    changes its params shape still fails loudly.
    """
    tracer = request_tracer()

    awaitable, context = _fire(
        tracer, 'on_response_chunk_received', SimpleNamespace(
            method='GET', url='http://h/p', chunk=b'ok'))
    await awaitable

    assert isinstance(context.results['on_response_chunk_received'], float)
    assert context.results['on_response_chunk_received'] >= 0.0


# --- R26 edge cases: foreign tracers, tracing off -------------------------

async def test_tracing_off_reports_an_empty_list_not_a_key_error(
    http_server: RecordingHTTPServer,
) -> None:
    """``trace_config=[]`` is a legitimate value meaning "no tracing".

    Named in the spec's empty-values table alongside ``data={}`` and an
    empty directory listing: each is a real value with a defined result,
    never conflated with an error.
    """
    http_server.respond('/body', body=b'ok')

    envelope = await _get_with(http_server, trace_config=[])

    assert envelope['ok'] is True
    assert envelope['request_tracer'] == []


def test_a_foreign_trace_config_is_skipped_not_assumed_to_collect() -> None:
    """The spec's other edge case: a tracer this library did not build.

    A bare ``aiohttp.TraceConfig`` has no ``results_collector`` --
    ``request_tracer`` attaches that itself -- so binding a scope must
    not assume one is there. It is skipped, because there is nothing this
    library can write into it, and skipping is what keeps a caller's own
    tracer on their own session from failing their call.
    """
    foreign = aiohttp.TraceConfig()
    ours = request_tracer()

    scopes = begin_trace_scope([foreign, ours])

    assert not hasattr(foreign, 'results_collector')
    assert len(scopes) == 1
    assert scopes[0] == {}


def test_binding_no_tracers_at_all_is_not_an_error() -> None:
    """Tracing off is the ordinary case, and it must cost nothing."""
    assert begin_trace_scope([]) == []


def test_a_tracer_carrying_a_plain_dict_collector_is_used_as_it_is() -> None:
    """The third case: a mapping this library can write to but did not bind.

    Reachable through a caller's own session, whose tracers
    ``trace_collectors_for`` scans: one may carry a ``results_collector``
    that is a plain dict rather than a :class:`ResultsCollector`, because
    the caller attached it themselves. There is no context variable to
    bind, so the mapping is used directly -- which is what keeps the
    redirect event reaching a collector the caller can actually read,
    rather than a fresh dict thrown away at the end of the call.
    """
    foreign = aiohttp.TraceConfig()
    foreign.results_collector = {'theirs': True}

    scopes = begin_trace_scope([foreign])

    assert scopes == [{'theirs': True}]
    assert scopes[0] is foreign.results_collector


async def test_the_redirect_callback_records_the_hop_and_the_flag() -> None:
    """``on_request_redirect``: unreachable in-library, still registered.

    This library owns its redirect loop and issues every hop with
    ``allow_redirects=False``, so ``aiohttp`` never fires this on the
    library's own path -- ``record_redirect`` in
    ``helpers/internal/request_helper.py`` writes the same two keys
    instead, and ``tests/logic/test_http_client.py`` covers that route
    end to end. The callback still has to be correct, because the tracer
    is public: a caller who attaches it to their own session redirects
    through ``aiohttp`` and reaches exactly this code.
    """
    tracer = request_tracer()

    awaitable, context = _fire(
        tracer, 'on_request_redirect', SimpleNamespace(
            method='GET', url='http://h/p', headers={},
            response=SimpleNamespace()))
    await awaitable

    assert context.results['is_redirect'] is True
    assert isinstance(context.results['on_request_redirect'], float)
    assert context.results['on_request_redirect'] >= 0.0


# --- ResultsCollector: the mapping contract two modules depend on ---------

def test_the_collector_behaves_as_a_mapping() -> None:
    """``results_collector`` is public API and is read as a mapping.

    ``logic/http_client.py`` type-checks it with
    ``isinstance(..., MutableMapping)`` and
    ``helpers/internal/request_helper.py`` calls ``.get()`` and
    subscript-assigns to it. Making it a view rather than a dict is only
    safe if the whole contract still holds, so the whole contract is
    asserted -- read, write, delete, iterate, size and repr.
    """
    collector = request_tracer().results_collector

    collector['a'] = 1
    collector['b'] = 2

    assert collector['a'] == 1
    assert collector.get('missing') is None
    assert sorted(collector) == ['a', 'b']
    assert len(collector) == 2
    assert dict(collector) == {'a': 1, 'b': 2}
    assert 'ResultsCollector' in repr(collector)
    assert "'a': 1" in repr(collector)

    del collector['a']

    assert 'a' not in collector
    with pytest.raises(KeyError):
        collector['a']
    with pytest.raises(KeyError):
        del collector['a']


def test_the_collector_resolves_to_whichever_scope_is_bound() -> None:
    """The view's whole purpose, at the unit level.

    Binding a new scope must redirect reads and writes to the new
    mapping without the caller's handle changing -- that is what lets
    ``results_collector`` stay the documented, stable attribute while the
    storage underneath it belongs to one request.
    """
    tracer = request_tracer()
    collector = tracer.results_collector
    collector['before'] = True

    bound = begin_trace_scope([tracer])[0]

    assert 'before' not in collector
    collector['after'] = True
    assert bound == {'after': True}
    assert dict(collector) == {'after': True}


async def test_two_tasks_binding_one_tracer_get_independent_scopes() -> None:
    """The concurrency primitive, isolated from the transport.

    The integration test above proves the property end to end; this
    proves *why* it holds, so a regression in the mechanism is
    attributable without a server in the picture. A ``ContextVar`` bound
    inside a task is invisible to its siblings, which is exactly the
    isolation H17 needed and a module-level dict could never provide.
    """
    tracer = request_tracer()
    collector = tracer.results_collector

    async def work(marker: str) -> dict[str, Any]:
        scope = begin_trace_scope([tracer])[0]
        collector['who'] = marker
        await asyncio.sleep(0)
        collector['seen_after_switch'] = collector['who']
        return dict(scope)

    first, second = await asyncio.gather(work('first'), work('second'))

    assert first == {'who': 'first', 'seen_after_switch': 'first'}
    assert second == {'who': 'second', 'seen_after_switch': 'second'}

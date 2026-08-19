"""The aiohttp request tracer, whose results belong to one request.

:func:`request_tracer` builds an ``aiohttp.TraceConfig`` whose fifteen
callbacks time one call and hand the numbers back on the envelope's
``request_tracer`` key. The interesting part is *where those numbers
live*, because that is what this module exists to fix.

They used to live in a single dict attached to the ``TraceConfig``
object. ``README.md`` documents ``trace_config`` as caller-supplied and a
``TraceConfig`` is a session-level thing, so one tracer routinely served
many requests: two concurrent calls interleaved their timings into one
mapping, and a stale ``on_request_exception`` from a failed call
annotated the next successful one (H17). Results now live on the
per-request trace **context** -- ``aiohttp`` builds one per request
through ``TraceConfig(trace_config_ctx_factory=...)`` -- and are
collected into the envelope at the end. Nothing accumulates on the
shared ``TraceConfig``.

The seam that makes both true at once is :class:`ResultsCollector`. The
``results_collector`` attribute is public, documented, and read by
``logic/http_client.py`` and ``helpers/internal/request_helper.py``, so
it could not simply be deleted -- but it is no longer *storage*. It is a
view that resolves, on every read and every write, to whichever
per-request mapping is currently in scope, held in a ``ContextVar``
private to each tracer. A ``ContextVar`` is the right primitive
precisely because ``asyncio.gather`` copies the context per task: two
concurrent calls through one tracer see two different mappings without
either knowing the other exists.

Two smaller corrections ride along. ``on_connection_reuseconn`` records
a relative delta like its thirteen siblings instead of overwriting the
request-start baseline with an absolute timestamp -- which silently
under-reported latency on every keep-alive connection (M11). And
``on_request_exception`` stores a redacted *string* built by
:func:`~async_gateway.utils.exceptions.unwrap_cause` rather than the
live exception object: an ``aiohttp.ClientResponseError`` carries
``.request_info.headers``, so the old value put the caller's
``Authorization`` on the envelope (M20).
"""

import asyncio
from collections.abc import Collection, Iterator, MutableMapping, Sequence
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any, Optional

import aiohttp

from async_gateway.utils.exceptions import unwrap_cause

#: The key every relative measurement below is taken against, and the one
#: ``helpers/internal/request_helper.py`` reads to reproduce ``aiohttp``'s
#: base instant for a redirect hop. A key written by one module and read
#: by another is the string that goes stale silently, so it is named.
REQUEST_START_KEY = 'on_request_start'

#: The per-request mapping the callbacks write into. Values are floats
#: for the timings, a bool for ``is_redirect`` and a string for the
#: exception report, so the value type is deliberately open.
TraceResults = dict[str, Any]


class ResultsCollector(MutableMapping[str, Any]):
    """A live view of whichever request's trace results are in scope.

    Not a dict, and deliberately not one. ``results_collector`` is the
    documented handle a caller keeps on a tracer, and it is read by two
    modules in this package, so it has to keep behaving like a mapping.
    What it must *stop* doing is owning the values: a dict attached here
    is shared by every request the tracer ever serves, which is H17.

    Every operation therefore resolves through :attr:`var` to the mapping
    belonging to the request currently in scope. Because ``var`` is a
    ``ContextVar`` and ``asyncio.gather`` gives each task its own copy of
    the context, concurrent calls resolve to different mappings with no
    coordination at all.

    When nothing has been bound -- a tracer driven outside this library,
    or one read after its call returned -- the mapping last bound in this
    context answers, and a virgin tracer binds an empty one on first
    touch. That is what keeps ``tracer.results_collector['on_request_end']``
    working for the caller who attaches a tracer to their own session and
    reads it afterwards, which is the documented usage.

    Attributes:
        var: The per-tracer context variable holding the mapping in
            scope. Private to the tracer that created it; two tracers
            never share one.
    """

    def __init__(self, var: 'ContextVar[Optional[TraceResults]]') -> None:
        """Bind the view to one tracer's context variable.

        Args:
            var: The context variable this view resolves through.
        """
        self.var = var

    def current(self) -> TraceResults:
        """Return the mapping in scope, binding a fresh one if there is none.

        Binding on first touch is what lets a tracer this library did not
        drive still collect something: a caller who attaches
        ``request_tracer()`` to their own ``ClientSession`` gets one
        mapping per context rather than a failure.

        Returns:
            The per-request results mapping now in scope.
        """
        results = self.var.get()
        if results is None:
            results = {}
            self.var.set(results)
        return results

    def __getitem__(self, key: str) -> Any:
        """Read ``key`` from the mapping in scope.

        Args:
            key: The trace event name.

        Returns:
            The value recorded for that event.

        Raises:
            KeyError: If the event was not recorded for this request.
        """
        return self.current()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """Write ``key`` into the mapping in scope.

        Args:
            key: The trace event name.
            value: The value to record.
        """
        self.current()[key] = value

    def __delitem__(self, key: str) -> None:
        """Remove ``key`` from the mapping in scope.

        Args:
            key: The trace event name.

        Raises:
            KeyError: If the event was not recorded for this request.
        """
        del self.current()[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate the event names recorded for the request in scope.

        Returns:
            An iterator over the recorded keys.
        """
        return iter(self.current())

    def __len__(self) -> int:
        """Count the events recorded for the request in scope.

        Returns:
            How many events the mapping in scope holds.
        """
        return len(self.current())

    def __repr__(self) -> str:
        """Render the mapping in scope, so a debugger shows real values.

        Returns:
            The representation of the results now in scope.
        """
        return f'{type(self).__name__}({self.current()!r})'


def begin_trace_scope(
    trace_config: Sequence[aiohttp.TraceConfig],
) -> list[MutableMapping[str, Any]]:
    """Bind a fresh results mapping to each tracer, for one call.

    Called by ``logic/http_client.py`` immediately before it dispatches,
    so the mappings returned here are the ones that call fills *and* the
    ones its envelope reports -- literally the same objects, not two
    reads of a shared source. Binding *fresh* mappings is what stops the
    previous call's ``on_request_exception`` annotating this one, and
    binding them through each tracer's own ``ContextVar`` is what stops
    two concurrent calls through one tracer from seeing each other.

    A redirect chain issues several ``aiohttp`` requests inside one call.
    They share the mapping bound here, last write winning, which is the
    behaviour ``aiohttp`` had when it owned the loop and the behaviour
    ``record_redirect`` measures against. The unit this scope isolates is
    the caller's request, which is the unit H17 is about.

    Args:
        trace_config: The tracers this library is attaching to the
            session it is about to build. Empty when the caller supplied
            their own session, since this library then attaches nothing.

    Returns:
        One results mapping per tracer, in the order given. A tracer this
        library did not build carries no context variable to bind, so its
        own ``results_collector`` is returned unchanged rather than a
        fresh mapping it would never write into.
    """
    scopes: list[MutableMapping[str, Any]] = []
    for tracer in trace_config:
        collector = getattr(tracer, 'results_collector', None)
        if isinstance(collector, ResultsCollector):
            results: TraceResults = {}
            collector.var.set(results)
            scopes.append(results)
        elif isinstance(collector, MutableMapping):
            scopes.append(collector)
    return scopes


def request_tracer(
    *,
    redact_params: Collection[str] = (),
) -> aiohttp.TraceConfig:
    """Build a tracer whose results belong to one request.

    Args:
        redact_params: The caller's additional sensitive query-parameter
            names, as normalised by ``request()``. They widen what the
            exception report masks beyond the built-in names, which apply
            regardless. Omitting them costs the caller's *extension*
            names only -- which is why a tracer the caller builds
            themselves still redacts, just not their custom parameters,
            since this library cannot know them.

    Returns:
        An ``aiohttp.TraceConfig`` with all fifteen callbacks registered
        and a :class:`ResultsCollector` on its ``results_collector``
        attribute. Every timing it records is seconds elapsed since that
        request's ``on_request_start``, read from the running loop's
        monotonic clock.
    """
    results_var: ContextVar[Optional[TraceResults]] = ContextVar(
        'async_gateway_trace_results', default=None)
    collector = ResultsCollector(results_var)

    def trace_context(**kwargs: Any) -> SimpleNamespace:
        """Build the per-request trace context ``aiohttp`` hands the callbacks.

        This is the hinge of the module: ``aiohttp`` calls it once per
        request, so pointing ``context.results`` at the mapping in scope
        is what makes the results per-request rather than
        per-``TraceConfig``.

        Args:
            **kwargs: What ``aiohttp`` seeds the context with, chiefly
                ``trace_request_ctx``. Passed through untouched, so a
                caller's own callbacks on the same tracer still find it.

        Returns:
            The trace context, carrying ``results``.
        """
        context = SimpleNamespace(**kwargs)
        context.results = collector.current()
        return context

    # `type: ignore[arg-type]` -- aiohttp types
    # `trace_config_ctx_factory` as `type[SimpleNamespace]`, the class
    # itself, but it *calls* the value
    # (`self._trace_config_ctx_factory(trace_request_ctx=...)`), so any
    # callable returning a `SimpleNamespace` satisfies the code and a
    # factory function is the documented way to seed the context.
    # Passing the bare class would drop the per-request `results`
    # binding, which is the whole point of this module (H17).
    trace_config = aiohttp.TraceConfig(
        trace_config_ctx_factory=trace_context)  # type: ignore[arg-type]
    # `type: ignore[attr-defined]` -- `results_collector` is this
    # library's own attribute, attached here and read by
    # `logic/http_client.py` and `helpers/internal/request_helper.py`.
    # `aiohttp.TraceConfig` naturally does not declare it, and a
    # subclass that did would change the type callers pass to
    # `trace_configs`, breaking the caller-supplied-tracer path this
    # module documents.
    trace_config.results_collector = collector  # type: ignore[attr-defined]

    def elapsed(context: SimpleNamespace) -> float:
        """Return seconds from this request's start to now.

        Read from the running loop's monotonic clock, and always against
        the baseline ``on_request_start`` recorded -- never against one a
        later callback replaced, which is the M11 defect
        ``on_connection_reuseconn`` used to inflict on every callback
        that fired after it.

        Args:
            context: The per-request trace context.

        Returns:
            Seconds since this request started, or 0.0 when the baseline
            is absent -- which means a callback fired before
            ``on_request_start``, and 0.0 is the honest reading of "no
            time has been measured" rather than a negative interval.
        """
        started = context.results.get(REQUEST_START_KEY)
        if not isinstance(started, (int, float)):
            return 0.0
        return asyncio.get_running_loop().time() - started

    async def on_request_start(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceRequestStartParams,
    ) -> None:
        """Record the baseline every other measurement is taken against.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The method, URL and headers of the request.
        """
        context.results[REQUEST_START_KEY] = (
            asyncio.get_running_loop().time())
        context.results['is_redirect'] = False

    async def on_connection_queued_start(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceConnectionQueuedStartParams,
    ) -> None:
        """Record when the request began waiting for a free connection.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The empty parameter object for this signal.
        """
        context.results['on_connection_queued_start'] = elapsed(context)

    async def on_connection_queued_end(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceConnectionQueuedEndParams,
    ) -> None:
        """Record when a queued request was handed a connection.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The empty parameter object for this signal.
        """
        context.results['on_connection_queued_end'] = elapsed(context)

    async def on_connection_create_start(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceConnectionCreateStartParams,
    ) -> None:
        """Record when a new connection began being dialled.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The empty parameter object for this signal.
        """
        context.results['on_connection_create_start'] = elapsed(context)

    async def on_request_redirect(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceRequestRedirectParams,
    ) -> None:
        """Record a redirect ``aiohttp`` itself followed.

        This library owns its redirect loop and issues every hop with
        ``allow_redirects=False``, so ``aiohttp`` no longer fires this on
        the library's own path -- ``record_redirect`` writes the same two
        keys instead. It stays registered and correct because the tracer
        is public: a caller driving it on their own session still
        redirects through ``aiohttp``.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The method, URL, headers and response of the hop.
        """
        context.results['on_request_redirect'] = elapsed(context)
        context.results['is_redirect'] = True

    async def on_connection_reuseconn(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceConnectionReuseconnParams,
    ) -> None:
        """Record that a pooled connection was reused, as a relative delta.

        This callback used to assign ``loop.time()`` to
        ``on_request_start``, overwriting the baseline mid-request with
        an absolute timestamp. Every measurement after it was then taken
        from the wrong instant, under-reporting latency on every
        keep-alive connection, and the stored value was an absolute
        timestamp sitting among thirteen relative deltas (M11). It now
        records a delta like its siblings and leaves the baseline alone,
        so a reused connection is still measured from the original start.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The connection taken from the pool.
        """
        context.results['on_connection_reuseconn'] = elapsed(context)

    async def on_dns_cache_hit(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceDnsCacheHitParams,
    ) -> None:
        """Record that the host name was answered from the DNS cache.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The host name that hit the cache.
        """
        context.results['on_dns_cache_hit'] = elapsed(context)

    async def on_dns_cache_miss(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceDnsCacheMissParams,
    ) -> None:
        """Record that the host name was not in the DNS cache.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The host name that missed the cache.
        """
        context.results['on_dns_cache_miss'] = elapsed(context)

    async def on_dns_resolvehost_start(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceDnsResolveHostStartParams,
    ) -> None:
        """Record when host-name resolution began.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The host name being resolved.
        """
        context.results['on_dns_resolvehost_start'] = elapsed(context)

    async def on_dns_resolvehost_end(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceDnsResolveHostEndParams,
    ) -> None:
        """Record when host-name resolution finished.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The host name that was resolved.
        """
        context.results['on_dns_resolvehost_end'] = elapsed(context)

    async def on_connection_create_end(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceConnectionCreateEndParams,
    ) -> None:
        """Record when a newly dialled connection became usable.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The empty parameter object for this signal.
        """
        context.results['on_connection_create_end'] = elapsed(context)

    async def on_request_chunk_sent(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceRequestChunkSentParams,
    ) -> None:
        """Record when the most recent request-body chunk went out.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The method, URL and chunk that was sent.
        """
        context.results['on_request_chunk_sent'] = elapsed(context)

    async def on_response_chunk_received(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceResponseChunkReceivedParams,
    ) -> None:
        """Record when the most recent response-body chunk arrived.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The method, URL and chunk that was received.
        """
        context.results['on_response_chunk_received'] = elapsed(context)

    async def on_request_exception(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceRequestExceptionParams,
    ) -> None:
        """Record a failed request as a timing and a redacted string.

        The message used to be the live exception object, stored under a
        key named ``..._message``. An ``aiohttp.ClientResponseError``
        carries ``.request_info.headers``, so that object put the
        caller's ``Authorization`` on the envelope the README
        demonstrates printing (M20). :func:`unwrap_cause` returns a
        string that is both redacted and non-empty -- it walks the cause
        chain rather than trusting a wrapper whose own ``str()`` is
        blank -- and there is no unredacted way out of it.

        The caller's ``redact_query_params`` are passed through, because
        the exception this reports is ``aiohttp``'s and its text is the
        URL that failed: an ``InvalidUrlClientError`` stringifies to the
        whole URL, query string included. Redacting with the built-in
        names alone would mask ``api_key`` and leave a caller's own
        ``session_id`` in the clear on the envelope -- which is invariant
        E9's promise about *all four* credential surfaces, not most of
        them.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The method, URL, headers and exception raised.
        """
        context.results['on_request_exception'] = elapsed(context)
        message, _ = unwrap_cause(
            params.exception, redact_params=redact_params)
        context.results['on_request_exception_message'] = message

    async def on_request_end(
        session: aiohttp.ClientSession,
        context: SimpleNamespace,
        params: aiohttp.TraceRequestEndParams,
    ) -> None:
        """Record the total time this request took.

        Args:
            session: The session issuing the request.
            context: The per-request trace context.
            params: The method, URL, headers and response.
        """
        context.results['on_request_end'] = elapsed(context)

    trace_config.on_request_start.append(on_request_start)
    trace_config.on_connection_queued_start.append(on_connection_queued_start)
    trace_config.on_connection_queued_end.append(on_connection_queued_end)
    trace_config.on_connection_create_start.append(on_connection_create_start)
    trace_config.on_request_redirect.append(on_request_redirect)
    trace_config.on_connection_reuseconn.append(on_connection_reuseconn)
    trace_config.on_dns_cache_hit.append(on_dns_cache_hit)
    trace_config.on_dns_cache_miss.append(on_dns_cache_miss)
    trace_config.on_dns_resolvehost_start.append(on_dns_resolvehost_start)
    trace_config.on_dns_resolvehost_end.append(on_dns_resolvehost_end)
    trace_config.on_connection_create_end.append(on_connection_create_end)
    trace_config.on_request_chunk_sent.append(on_request_chunk_sent)
    trace_config.on_response_chunk_received.append(on_response_chunk_received)
    trace_config.on_request_exception.append(on_request_exception)
    trace_config.on_request_end.append(on_request_end)

    return trace_config

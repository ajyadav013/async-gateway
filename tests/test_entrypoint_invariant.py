"""The mechanical guard on the one-conversion-point invariant (N6, N7).

The library's central promise is a single sentence: **every failure
reaches the caller as an ``ok=False`` envelope, and the only exception
that may escape ``request()`` is an ``AsyncGatewayError`` subclass.**
Everything else -- a ``ValueError`` from ``aiohttp``'s header serialiser,
an ``aioftp.InvalidCommand``, a ``TypeError`` from ``orjson`` -- is by
definition a bug in this library, because a caller cannot be asked to
catch an exception the contract does not name.

This file exists because that invariant kept being broken *one input at
a time*. Two consecutive security reviews each found a different bare
exception escaping the same entry point: N6 (a CR or an LF in a
caller-supplied header, escaping as a bare ``ValueError`` from
``aiohttp.http_writer._safe_header``) and N7
(``aioftp.errors.InvalidCommand``, a ``ValueError`` subclass omitted from
FTP's ``TRANSPORT_ERRORS``). Both were patched where they were found.
Neither patch made the *next* one impossible, and a third review would
have found a third.

So the deliverable is not another pair of regression tests -- those live
beside their protocols -- but a **matrix**: a broad, deliberately hostile
set of caller inputs driven through the public ``request()``, asserting
only the invariant and nothing about any particular input's outcome. The
matrix is the enforceable form of the contract. An input may legitimately
produce a success envelope, a failure envelope or a typed exception, and
which of the three it produces is the protocol's business; what no input
may produce is a bare builtin.

**Why the HTTP family runs against a real socket.** N6 is the reason,
and getting this wrong was measured rather than reasoned about. The
first version of this file doubled *every* protocol at
``handle_http_request`` (``tests/fixtures/protocol_transports.py``), and
a mutation that disabled the header validator outright left all 570 rows
green -- because that seam sits **above** ``aiohttp``, so a header never
reached the serialiser that refuses it and the escape had nowhere to
happen. A matrix that cannot observe the defect it was written for is
worse than no matrix, since it reports safety it never checked.

So the HTTP family and SOAP run over the loopback ``aiohttp`` server in
``tests/fixtures/http_server.py``: a real connection, a real request, a
real ``_safe_header`` call. That placement matters twice over, because
``aiohttp`` refuses a CR-bearing header only at serialisation -- *after*
the connection is open. Against an unreachable host the connect fails
first, the headers are never serialised, and the call yields a tidy
``CONNECT`` envelope that hides the defect completely. Reachability is
what makes the escape observable, and it is exactly the nondeterminism
the invariant exists to remove.

FTP and SFTP keep the doubled transport. Their equivalent refusal
(``aioftp``'s own CR/LF check, N7) happens in code the double still runs
through, and standing up a real FTP and SSH server per row would cost
seconds apiece for no additional reach. The mutation proof below is run
against both lanes.

**What this file does not do.** It does not assert that a given input is
*rejected*. Tightening a validator so that it refuses more is a
behavioural change that belongs in the protocol's own suite, where the
message and the error code are asserted. Here, a stricter library and a
laxer one both pass, provided neither leaks a bare exception -- which is
what keeps the matrix stable enough to be worth extending.

Property-based generation (``hypothesis``) would suit this well and is
deliberately not used: it is not a dependency of this project, and adding
one to the dev set to reach inputs an explicit matrix already reaches is
a poor trade. The matrix is parametrised instead, and each family below
names the *reason* it is hostile so that the next reviewer extends it
rather than replacing it.

**The blind spot this file had, and the mechanism that closes it.** The
first 570-row version varied ``headers``, ``cookies``, verbs, ``urls``,
``protocol_info``, ``data`` and ``auth`` -- and never set
``pre_processor_config``, ``post_processor_config`` or ``**kwargs`` at
all. Those three were documented public parameters of ``request()`` the
whole time, and a review that drove twenty hostile shapes through the
processor configs found **eighteen** bare builtins (NEW-2). The matrix
reported the invariant safe on a surface it had never touched.

The lesson is not "add three more families". Hand-listing the parameters
to cover is what produced a list that was silently three short, and
hand-listing them again produces a list that goes stale the day a
parameter is added. So the parameter list is **derived from
``inspect.signature(request)``** and
:func:`test_every_public_parameter_of_request_is_covered` fails if any
parameter has no hostile family bound to it. A future parameter is
therefore covered *on arrival*: adding one to ``request()`` turns this
file red until someone writes the hostile values for it, which is exactly
the moment the thinking is cheapest.

**Pairs, not just singletons.** The ninth escape found while extending
this file existed precisely because coverage was one-dimensional: a
*valid* pre-processor that mutated the envelope it was handed left the
protocol client reading a key that was no longer there. No single-
parameter row could reach it, because the hostile thing was the
interaction between a processor and the dispatch that follows it.
:data:`HOSTILE_PAIRS` therefore varies two parameters at once across the
security-relevant combinations. It is deliberately a curated list rather
than the full cross product: 7 families of ~15 values each squared is
~11,000 rows per protocol, which would trade a tractable suite for
coverage of pairs nobody has a reason to suspect.
"""

import inspect
from typing import Any, Final

import pytest

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.logic import protocol_mapping
from asyncio_gateway.utils.exceptions import AsyncGatewayError

from tests.fixtures.http_server import RecordingHTTPServer
from tests.fixtures.protocol_transports import (
    CONTRACT_CALL,
    GRAPHQL_BODY,
    JSONRPC_BODY,
    SOAP_BODY,
    contract_call,
    install_transport,
)

#: Every protocol the library registers. The invariant is a property of
#: the entry point, so it is asserted on all of them rather than on the
#: one whose defect prompted the file -- N6 landed on HTTP and SOAP, N7
#: on FTP, and the next one has no reason to respect that split.
PROTOCOLS: Final[tuple[str, ...]] = tuple(CONTRACT_CALL)

#: The protocols whose rows go over a real loopback socket, for the
#: reason the module docstring gives: their refusal lives inside
#: ``aiohttp``'s header serialiser, which a doubled transport never
#: reaches.
LIVE_PROTOCOLS: Final[tuple[str, ...]] = (
    'HTTP', 'SOAP', 'JSONRPC', 'GRAPHQL')

#: The path the loopback server answers on for every live row.
LIVE_PATH: Final[str] = '/invariant'

#: Header mappings that are hostile, malformed, or the wrong type
#: outright. The first four are N6 itself: a CR or an LF in a name or a
#: value is header injection, and the fact that ``aiohttp`` refuses it
#: correctly was never the issue -- the issue was that it refused with a
#: bare ``ValueError`` after the socket was already open. The C0/DEL rows
#: are the rest of the character class ``aiohttp`` refuses in the same
#: breath. The remaining rows are the type confusions that reached the
#: same serialiser as a bare ``TypeError`` or ``AttributeError``: three
#: exception types for three spellings of one configuration mistake.
HOSTILE_HEADERS: Final[tuple[Any, ...]] = (
    {'X-Injected': 'value\r\nX-Smuggled: 1'},
    {'X-Injected': 'value\nX-Smuggled: 1'},
    {'X-Injected': 'value\rX-Smuggled: 1'},
    {'X-Injected\r\nX-Smuggled': 'value'},
    {'X-Injected': 'value\x00null'},
    {'X-Injected': 'value\x01soh'},
    {'X-Injected': 'value\x1funit'},
    {'X-Injected': 'value\x7fdel'},
    {'X-Injected': 1},
    {'X-Injected': None},
    {'X-Injected': b'bytes'},
    {'X-Injected': ['a', 'b']},
    {'X-Injected': {'nested': 'mapping'}},
    {1: 'value'},
    {None: 'value'},
    {b'bytes': 'value'},
    {(): 'value'},
    'a bare string, which iterates as characters',
    ['a', 'list'],
    42,
    object(),
)

#: Cookie mappings, hostile for the same reasons one key over. A cookie
#: is serialised by ``http.cookies`` rather than by ``aiohttp``, so the
#: same caller mistake arrived as a *different* bare exception --
#: ``CookieError`` for a control character, ``AttributeError`` for a
#: non-str name, ``ValueError: not enough values to unpack`` for a bare
#: string. A cookie name is additionally an RFC 6265 token and cannot be
#: quoted, so the space/semicolon/comma rows have no representation at
#: all rather than an escaped one.
HOSTILE_COOKIES: Final[tuple[Any, ...]] = (
    {'session': 'value\r\nX-Smuggled: 1'},
    {'session': 'value\nnewline'},
    {'session': 'value\x00null'},
    {'session\r\nX-Smuggled': 'value'},
    {'session name': 'value'},
    {'session;name': 'value'},
    {'session,name': 'value'},
    {'': 'value'},
    {'session': 1},
    {'session': None},
    {1: 'value'},
    {None: 'value'},
    'a bare string',
    ['not', 'a', 'mapping'],
    42,
)

#: Verbs and operation names. Each protocol reads its own key -- HTTP's
#: ``request_type``, FTP's ``command``, SFTP's ``mode`` -- and all three
#: resolve it through ``getattr`` on a live client object, which is the
#: shape that made ``request_type='close'`` call
#: ``ClientSession.close(url, **filters)`` and report the resulting
#: ``TypeError`` as a fabricated status (M25).
HOSTILE_VERBS: Final[tuple[Any, ...]] = (
    'close',
    'ws_connect',
    'detach',
    '__init__',
    '__class__',
    'quit',
    '',
    '   ',
    'get\r\nNOOP',
    None,
    42,
    b'get',
    ['get'],
    {'verb': 'get'},
    object(),
)

#: URLs. The type rows are the ones that mattered: ``url`` is annotated
#: ``str``, an annotation is not enforcement, and a non-str reached
#: ``redact_url`` and ``yarl.URL`` as a bare ``TypeError`` or
#: ``AttributeError`` on *every* protocol. The malformed-but-str rows
#: cover the parser's own edges.
HOSTILE_URLS: Final[tuple[Any, ...]] = (
    None,
    42,
    b'http://host/p',
    ['http://host/p'],
    {'url': 'http://host/p'},
    object(),
    '',
    '   ',
    '///',
    'http://',
    'ht!tp://host',
    'http://[::1',
    'http://host:notaport/p',
    'http://host/p\r\nX-Smuggled: 1',
    'javascript:alert(1)',
    'file:///etc/passwd',
    'gopher://host/p',
    'http://user:pw@host/p',
    # Protocol-relative references: no scheme, but a real authority.
    #
    # **This axis was already varied and still missed a bare escape**,
    # which is the part worth recording. The family had schemeless rows
    # (`''`, `'   '`, `'///'`) and authority-bearing rows
    # (`'http://user:pw@host/p'`), but never one that was *both*: every
    # schemeless row here parsed to an empty netloc, and an empty netloc
    # is what made them safe. `//host/p` parses to a netloc of `host`
    # with no scheme, `dispatch_url_for` returned it unchanged under
    # `'HTTP'`, and `aiohttp` then failed an internal
    # `assert port is not None` -- a bare `AssertionError` out of
    # `request()` (F3).
    #
    # So the gap was not a missing dimension but an uncovered
    # *combination* of two that were each present, which is the same
    # shape as the ninth escape this file's docstring records: a
    # one-dimensional reading of a matrix that needs a corner.
    '//host/p',
    '//user:pw@host/p',
    '//host:8080/p?api_key=SECRET',
)

#: ``protocol_info`` values that are not the mapping the entry point
#: requires, plus mappings whose *values* are the wrong type. The second
#: group is the interesting one: a well-shaped ``protocol_info`` carrying
#: a nonsense value for a key the protocol reads is the shape most likely
#: to slip past a shape check and crash deeper in.
HOSTILE_PROTOCOL_INFO: Final[tuple[Any, ...]] = (
    'a bare string',
    42,
    ['a', 'list'],
    (('request_type', 'get'),),
    object(),
    {'timeout': 'ten seconds'},
    {'timeout': None},
    {'timeout': -1},
    {'max_response_bytes': 'lots'},
    {'max_response_bytes': 0},
    {'max_redirects': 'many'},
    {'max_redirects': -1},
    {'allow_redirects': 'yes'},
    {'allowed_schemes': 'http'},
    {'allowed_schemes': []},
    {'serialization': 'not callable'},
    {'session': 'not a session'},
    {'trace_config': 'not a config'},
    {'cross_origin_headers': 'x-id'},
    {'cross_origin_headers': ['authorization']},
    {'certificate': 'not a pair'},
    {'redact_query_params': 42},
    {'circuit_breaker_config': 'not a mapping'},
    {'retry_config': 'not a mapping'},
    {'port': 'not a port'},
    # NEW-2, and the reason the row above did not catch it. A string
    # port is *hashable*, so it flowed into the breaker registry's
    # `(family, host, port)` dict key without complaint and merely
    # opened a second key for a destination that already had one. An
    # **unhashable** port is the shape that actually crashed:
    # `_BREAKERS.get(key)` raised `TypeError: cannot use 'tuple' as a
    # dict key (unhashable type: 'list')` out of `request()`
    # un-enveloped, on every then-registered protocol.
    #
    # All three unhashable builtins, because it is the container-ness
    # and not the list-ness that does it, and a guard written against
    # `list` alone would leave the other two.
    {'port': ['a', 'list']},
    {'port': {'a': 'dict'}},
    {'port': {'a', 'set'}},
    # The boundary values a *range* check has to get right, and the
    # reason this validator does not stop at hashability. `True` is an
    # `int` of value 1 and would silently dispatch to port 1; `-1` is
    # `UNKNOWN_PORT`, the registry's own "no port known" sentinel, so
    # accepting it lets a caller collide with it; `65536` is one past
    # the 16-bit ceiling and reaches the transport as an
    # `OverflowError` from inside `aioftp`/`asyncssh`; a float is not a
    # port however round it looks.
    {'port': True},
    {'port': -1},
    {'port': 65536},
    {'port': 21.0},
    {'server_path': 42},
    {'client_path': 42},
    {'remote_path': 42},
    {'local_path': 42},
    {'overwrite': 'yes'},
    {'verify_ssl': 'yes'},
    # NEW-R10-1's blind spot, and the reason the escape was invisible to
    # 2981 tests: this file drives every `protocol_info` *value* it can
    # think of, and it had never named the key that makes a call write
    # to the local disk at all. So the whole download path -- the one
    # surface where a caller's configuration reaches a filesystem rather
    # than a socket -- was outside the one guard whose entire job is
    # "no shape escapes the entry point".
    #
    # Four shapes, because the key's failure modes are not one. The
    # first is the wrong type for the config itself. The next two are a
    # destination the filesystem will refuse -- a parent that does not
    # exist, and one under a path component that is a *file* -- which
    # are the shapes that actually escaped as raw `FileNotFoundError`
    # and `NotADirectoryError`. The last is the wrong type for the path
    # inside a well-formed config, which is the shape most likely to
    # slip past a check on the config and crash on the `open`.
    {'http_file_download_config': 'not a mapping'},
    {'http_file_download_config': {
        'download_filepath': '/nonexistent-dir-for-agw/out.bin'}},
    {'http_file_download_config': {
        'download_filepath': '/etc/hosts/out.bin'}},
    {'http_file_download_config': {'download_filepath': 42}},
)

#: Request payloads. ``data`` is forwarded to a JSON encoder for most
#: verbs, and ``orjson`` refuses what it cannot serialise with a bare
#: ``TypeError`` raised from deep inside ``aiohttp``'s payload
#: construction -- far from the caller's mistake and outside any handler.
HOSTILE_DATA: Final[tuple[Any, ...]] = (
    object(),
    {'key': object()},
    {1: 'non-str key'},
    {'nested': {'deeper': object()}},
    [object()],
    lambda: None,
    b'\xff\xfe raw bytes',
    float('nan'),
    float('inf'),
)

#: ``auth`` objects that carry no usable credentials. FTP and SFTP
#: interpolate a login and a password into their connect call and cannot
#: form one without, which is why the documented-default ``auth=None``
#: call was an ``AttributeError`` on ``None.login`` (H5).
HOSTILE_AUTH: Final[tuple[Any, ...]] = (
    None,
    'not an auth object',
    42,
    object(),
)


async def _good_processor(response: Any = None, **params: Any) -> str:
    """Behave exactly as a documented processor callback should.

    Args:
        response: The envelope, as ``request()`` passes it.
        params: The caller's own ``params``, unused.

    Returns:
        A sentinel, so a row can tell "it ran" from "it was skipped".
    """
    return 'processed'


async def _raising_processor(response: Any = None, **params: Any) -> str:
    """Fail the way a caller's own buggy callback does.

    Args:
        response: The envelope, unused.
        params: The caller's own ``params``, unused.

    Returns:
        Never; this always raises.

    Raises:
        RuntimeError: Always. A bare builtin, deliberately: the point is
            that this library converts it rather than letting it out.
    """
    raise RuntimeError("the caller's own callback failed")


async def _clearing_processor(response: Any = None, **params: Any) -> str:
    """Empty the live envelope the callback was handed.

    The ninth escape's shape. A processor is given the real envelope by
    design, so it can also remove from it -- and a protocol client then
    read ``self.response['payload']`` as a bare ``KeyError`` from inside
    the one conversion ``try``, where nothing catches it.

    Args:
        response: The envelope, cleared in place.
        params: The caller's own ``params``, unused.

    Returns:
        A sentinel.
    """
    response.clear()
    return 'cleared'


async def _payload_deleting_processor(
    response: Any = None,
    **params: Any,
) -> str:
    """Remove the one envelope key both HTTP and SOAP read before dispatch.

    Narrower than :func:`_clearing_processor` and kept beside it: a
    guard that checked only for a *wholly* emptied envelope would let
    this through, and this is the shape that actually crashed.

    Args:
        response: The envelope, missing ``payload`` on return.
        params: The caller's own ``params``, unused.

    Returns:
        A sentinel.
    """
    response.pop('payload', None)
    return 'deleted'


def _sync_processor(response: Any = None, **params: Any) -> str:
    """Return a plain value instead of an awaitable.

    ``await 'x'`` is ``TypeError: 'str' object can't be awaited``, raised
    at the ``await`` in ``request()`` rather than anywhere a caller can
    see. Undecidable before the call, which is why it is a runtime
    concern rather than a config one.

    Args:
        response: The envelope, unused.
        params: The caller's own ``params``, unused.

    Returns:
        A string, which is not awaitable.
    """
    return 'not awaitable'


def _no_response_kwarg() -> str:
    """Accept none of the arguments a processor is called with.

    Args:
        None.

    Returns:
        A string, never reached: binding fails first with ``TypeError:
        got an unexpected keyword argument 'response'``.
    """
    return 'never reached'


#: Processor configurations, hostile in every way the two documented
#: ``*_processor_config`` parameters can be. This family is NEW-2 itself
#: and is the blind spot the module docstring describes: these two
#: parameters were documented public surface (README's argument table,
#: ``request()``'s own docstring) and the matrix had never set them, so
#: eighteen of twenty shapes reached the caller as bare builtins --
#: ``KeyError('function')``, ``TypeError('list indices must be integers
#: or slices, not str')``, ``TypeError('... argument after ** must be a
#: mapping, not str')``.
#:
#: The rows fall into two groups on purpose, because the library answers
#: them differently and a caller acts on the difference. The malformed
#: *configurations* are refused with ``ConfigurationError`` before
#: anything runs; the well-formed configs whose *callable* misbehaves
#: raise ``ProcessorError`` after it ran. Both are typed, which is all
#: this file asserts -- the split itself is asserted in
#: ``tests/test_entrypoint.py``, where the code and message belong.
HOSTILE_PROCESSOR_CONFIGS: Final[tuple[Any, ...]] = (
    # Shapes that are not the documented mapping at all.
    'a bare string',
    42,
    ['function'],
    (('function', _good_processor),),
    object(),
    # A mapping, but not the documented one.
    {'fn': _good_processor},
    {'params': {'x': 1}},
    {'function': None},
    {'function': 'not callable'},
    {'function': 42},
    {'function': []},
    # A valid callable with malformed `params`.
    {'function': _good_processor, 'params': 'not a mapping'},
    {'function': _good_processor, 'params': ['a', 'list']},
    {'function': _good_processor, 'params': 42},
    {'function': _good_processor, 'params': {1: 'non-str key'}},
    {'function': _good_processor, 'params': {None: 'non-str key'}},
    # `response` is the keyword this library supplies; a caller naming it
    # too is `TypeError: got multiple values for keyword argument`.
    {'function': _good_processor, 'params': {'response': 'collision'}},
    # Well-formed configs whose callable is the problem.
    {'function': _raising_processor},
    {'function': _sync_processor},
    {'function': _no_response_kwarg},
    {'function': lambda response=None, **k: 42},
    # Well-formed, and the callback mutilates the envelope it was handed.
    {'function': _clearing_processor},
    {'function': _payload_deleting_processor},
    # Valid rows, kept in the same family so the hostile ones cannot pass
    # by the parameter being ignored outright.
    {'function': _good_processor},
    {'function': _good_processor, 'params': {'extra': 'value'}},
)

#: ``**kwargs`` values. The documented contract is that an unread keyword
#: is *accepted and ignored*, so that a caller passing an option this
#: version does not know gets the call they asked for rather than a
#: ``TypeError``. That promise is only worth having if it holds for
#: keywords that collide with this library's own internal names -- which
#: is what these rows are: ``response`` is the keyword processors are
#: called with, ``function`` and ``params`` are the processor config's
#: own keys, and ``info``/``started``/``self`` are local names inside
#: ``request()``.
HOSTILE_KWARGS: Final[tuple[dict[str, Any], ...]] = (
    {'unknown_option': 1},
    {'response': 'collides with the processor keyword'},
    {'function': None},
    {'params': {'x': 1}},
    {'info': 'collides with a local'},
    {'started': 'collides with a local'},
    {'self': 'looks like a method call'},
    {'timeout': 5},
    {'headers': {'X': 'y'}},
    {'kwargs': {'nested': True}},
    {'protocol_name': 'HTTP'},
    {'target_url': 'http://elsewhere/'},
)

#: Two parameters varied at once, for the security-relevant combinations.
#:
#: The reason this exists is measured, not theoretical: the ninth escape
#: found while extending this file was a *valid* pre-processor whose
#: envelope mutation only became a crash once the call was dispatched.
#: One-dimensional coverage cannot reach a defect whose two halves are
#: individually harmless, and every pair below names a mechanism by which
#: one parameter changes what another does.
#:
#: Curated rather than exhaustive. The full cross product of the seven
#: families is ~11,000 rows per protocol; these are the pairs where an
#: interaction is plausible, and the module docstring says why that
#: trade is made.
HOSTILE_PAIRS: Final[tuple[tuple[str, Any, str, Any], ...]] = tuple(
    [
        # A processor runs *before* the URL scheme guard (FI-14) and
        # *before* dispatch, so it is the parameter most able to change
        # what another one means. Crossed with every other family's
        # sharpest value.
        ('pre_processor_config', processor, second, value)
        for processor in (
            {'function': _good_processor},
            {'function': _clearing_processor},
            {'function': _payload_deleting_processor},
            {'function': _raising_processor},
            {'function': 'not callable'},
        )
        for second, value in (
            ('url', None),
            ('url', 'http://[::1'),
            ('data', object()),
            ('auth', None),
            ('headers', {'X-Injected': 'value\r\nX-Smuggled: 1'}),
            ('cookies', {'session': 'value\x00null'}),
            ('operation', 'close'),
            ('protocol_info', 'a bare string'),
        )
    ] + [
        # Both processors configured together: the post-processor runs
        # after the conversion point, on an envelope a failed call
        # finalised, so a pre-processor failure and a post-processor
        # failure interact through what the envelope holds by then.
        ('pre_processor_config', pre, 'post_processor_config', post)
        for pre in (
            {'function': _good_processor},
            {'function': _clearing_processor},
            {'function': _raising_processor},
        )
        for post in (
            {'function': _good_processor},
            {'function': _clearing_processor},
            {'function': _raising_processor},
            {'function': None},
        )
    ] + [
        # A post-processor crossed with a call that *fails*, so it runs
        # against a finalised error envelope rather than a success one.
        ('post_processor_config', post, second, value)
        for post in (
            {'function': _good_processor},
            {'function': _clearing_processor},
            {'function': _payload_deleting_processor},
        )
        for second, value in (
            ('url', None),
            ('data', object()),
            ('operation', 'close'),
            ('headers', {'X-Injected': 'value\r\nX-Smuggled: 1'}),
        )
    ] + [
        # Two non-processor parameters whose validators run in a fixed
        # order, so one rejecting first can hide the other entirely.
        ('url', url, second, value)
        for url in (None, 'http://[::1', '')
        for second, value in (
            ('headers', {'X-Injected': 'value\r\nX-Smuggled: 1'}),
            ('protocol_info', 'a bare string'),
            ('data', object()),
            ('auth', None),
        )
    ]
)

#: Which hostile family covers which ``request()`` parameter. The keys
#: are asserted against ``inspect.signature(request)`` by
#: :func:`test_every_public_parameter_of_request_is_covered`, so this
#: mapping cannot silently fall behind the signature -- adding a
#: parameter to ``request()`` fails that test until a family is bound to
#: it here.
#:
#: ``protocol`` maps to the verb family rather than to one of its own:
#: an unregistered or non-``str`` protocol is rejected by
#: ``resolve_protocol`` before any envelope exists, and
#: ``tests/test_entrypoint.py`` asserts that rejection with its message
#: and code. What belongs *here* is the surface that reaches further in,
#: which for the protocol name is the per-protocol verb each one resolves
#: -- and every family below is already parametrised over all five
#: protocols, so the protocol axis is crossed with every other family
#: rather than tested alone.
COVERING_FAMILY: Final[dict[str, str]] = {
    'url': 'HOSTILE_URLS',
    'data': 'HOSTILE_DATA',
    'auth': 'HOSTILE_AUTH',
    'protocol': 'PROTOCOLS',
    'protocol_info': 'HOSTILE_PROTOCOL_INFO',
    'pre_processor_config': 'HOSTILE_PROCESSOR_CONFIGS',
    'post_processor_config': 'HOSTILE_PROCESSOR_CONFIGS',
    'kwargs': 'HOSTILE_KWARGS',
}


#: Names in :data:`HOSTILE_PAIRS` that live inside ``protocol_info``
#: rather than being top-level ``request()`` parameters. One table can
#: then name both levels, and the pair test merges each row at the right
#: depth without a per-row branch.
_INFO_KEYS: Final[frozenset[str]] = frozenset(
    {'headers', 'cookies', 'operation'})


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    http_server: RecordingHTTPServer,
) -> dict[str, Any]:
    """Return a valid call for ``protocol`` with its transport ready.

    The one place the two lanes diverge, so no test body has to know
    which lane it is on. ``HTTP``/``HTTPS``/``SOAP`` are pointed at the
    started loopback server and left to dial it for real;
    ``FTP``/``SFTP`` get their seam doubled.

    ``HTTPS`` keeps its doubled transport rather than joining the live
    lane, and the reason is not incidental: the entry point requires a
    ``https://`` URL for that protocol (R11-AC4), so pointing it at the
    plaintext loopback server would have every row refused at the scheme
    check -- before any header was built. Those rows would still pass,
    and would still prove nothing, which is the failure mode this whole
    file was rewritten to escape. ``HTTP`` and ``HTTPS`` share one
    protocol class, so the live ``HTTP`` rows already exercise the code
    ``HTTPS`` would.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        protocol: The protocol under test.
        http_server: The started loopback server.

    Returns:
        Keyword arguments for ``request()``, valid for this protocol.
    """
    call = contract_call(protocol)
    if protocol in LIVE_PROTOCOLS:
        body, media = {
            'HTTP': (b'{"value": 1}', 'application/json'),
            'SOAP': (SOAP_BODY, 'text/xml'),
            'JSONRPC': (JSONRPC_BODY, 'application/json'),
            'GRAPHQL': (
                GRAPHQL_BODY, 'application/graphql-response+json'),
        }[protocol]
        http_server.respond(
            LIVE_PATH, body=body, headers={'Content-Type': media})
        call['url'] = http_server.url_for(LIVE_PATH)
    else:
        install_transport(monkeypatch, protocol, succeeds=True)
    return call


async def _assert_only_typed_escapes(**call: Any) -> None:
    """Drive one ``request()`` call and hold it to the invariant.

    The single assertion this whole file makes. Deliberately narrow: the
    call may succeed, may return an ``ok=False`` envelope, or may raise
    an ``AsyncGatewayError``, and all three satisfy the contract. Only a
    fourth outcome -- an exception that is not an ``AsyncGatewayError``
    -- is a failure, because that is the one a caller following the
    documented contract cannot catch.

    ``asyncio.CancelledError`` inherits from ``BaseException`` and is
    re-raised untouched: cancelling a task is the caller's own act, not
    a failure of this library, and swallowing it here would make the
    matrix hang rather than fail.

    Args:
        **call: Keyword arguments forwarded verbatim to ``request()``.

    Returns:
        None.

    Raises:
        AssertionError: If anything other than an ``AsyncGatewayError``
            escapes, naming the exception and the call that produced it.
    """
    try:
        envelope = await request(**call)
    except AsyncGatewayError:
        return
    except Exception as err:
        raise AssertionError(
            f'{type(err).__module__}.{type(err).__name__} escaped '
            f'request() un-enveloped: {err!r}. Only an AsyncGatewayError '
            f'subclass may reach the caller as an exception; every other '
            f'failure must arrive as an ok=False envelope. Call was: '
            f'{call!r}') from err
    assert 'ok' in envelope, (
        f'request() returned an object with no "ok" key for {call!r}')


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('headers', HOSTILE_HEADERS)
async def test_no_header_shape_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    headers: Any,
) -> None:
    """N6's class: no header mapping produces a bare exception.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        headers: One hostile ``protocol_info['headers']`` value.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    call['protocol_info']['headers'] = headers
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('cookies', HOSTILE_COOKIES)
async def test_no_cookie_shape_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    cookies: Any,
) -> None:
    """The same class, reached through ``http.cookies`` instead.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        cookies: One hostile ``protocol_info['cookies']`` value.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    call['protocol_info']['cookies'] = cookies
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('verb', HOSTILE_VERBS)
async def test_no_verb_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    verb: Any,
) -> None:
    """No operation name resolves to something that crashes bare.

    Each protocol's verb lives under a different key, and all three are
    set so that one row covers whichever the protocol actually reads.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        verb: One hostile verb, command or mode.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    operation_key = CONTRACT_CALL[protocol]['operation_key']
    if operation_key is not None:
        call['protocol_info'][operation_key] = verb
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('url', HOSTILE_URLS)
async def test_no_url_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    url: Any,
) -> None:
    """No URL -- of any type or spelling -- produces a bare exception.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        url: One hostile URL.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    call['url'] = url
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('info', HOSTILE_PROTOCOL_INFO)
async def test_no_protocol_info_shape_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    info: Any,
) -> None:
    """No ``protocol_info`` shape or value produces a bare exception.

    A mapping row is merged onto the protocol's valid configuration
    rather than replacing it, so the required keys stay present and the
    hostile *value* is what the call is actually judged on. A non-mapping
    row replaces it outright, which is the shape being tested there.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        info: One hostile ``protocol_info``.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    if isinstance(info, dict):
        call['protocol_info'].update(info)
    else:
        call['protocol_info'] = info
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('data', HOSTILE_DATA)
async def test_no_payload_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    data: Any,
) -> None:
    """No unserialisable payload produces a bare encoder exception.

    Sent on a POST where the protocol takes a verb, because a GET routes
    the payload to the query string and never reaches the JSON encoder
    that is the point of this row.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        data: One hostile payload.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    if 'request_type' in call['protocol_info']:
        call['protocol_info']['request_type'] = 'POST'
    call['data'] = data
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('auth', HOSTILE_AUTH)
async def test_no_auth_object_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    auth: Any,
) -> None:
    """No ``auth`` object produces a bare attribute error.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        auth: One credential-less ``auth`` value.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    call['auth'] = auth
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('which',
                         ('pre_processor_config', 'post_processor_config'))
@pytest.mark.parametrize('config', HOSTILE_PROCESSOR_CONFIGS)
async def test_no_processor_config_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    which: str,
    config: Any,
) -> None:
    """NEW-2's class: no processor configuration produces a bare builtin.

    The family the matrix never set. Both parameters are driven with the
    same values, because they are documented as the same shape and were
    read by the same unvalidated two lines.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        which: ``'pre_processor_config'`` or ``'post_processor_config'``.
        config: One hostile processor configuration.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    call[which] = config
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('extra', HOSTILE_KWARGS)
async def test_no_extra_keyword_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    extra: dict[str, Any],
) -> None:
    """``**kwargs`` is documented as accepted and ignored; hold it to that.

    An unread keyword must not crash the call, and specifically must not
    crash it by colliding with a name this library uses internally --
    which is what every row here is chosen to do.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        extra: One extra keyword argument mapping.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    call.update(extra)
    await _assert_only_typed_escapes(**call)


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize(
    'first,first_value,second,second_value',
    HOSTILE_PAIRS,
    ids=lambda value: repr(value)[:40],
)
async def test_no_pair_of_parameters_escapes_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
    first: str,
    first_value: Any,
    second: str,
    second_value: Any,
) -> None:
    """Two hostile parameters at once, for the pairs that can interact.

    The one-dimensional matrix could not have found the ninth escape,
    because neither half of it was hostile alone: a *valid* pre-processor
    that emptied the envelope only crashed once the dispatch that follows
    read a key from it.

    ``headers``, ``cookies`` and ``request_type`` are ``protocol_info``
    keys rather than top-level parameters and are merged in as such, so
    one table can name both levels without a per-row branch at the call
    site.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.
        first: The first parameter's name.
        first_value: The first parameter's hostile value.
        second: The second parameter's name.
        second_value: The second parameter's hostile value.

    Returns:
        None.
    """
    call = _prepare(monkeypatch, protocol, http_server)
    for name, value in ((first, first_value), (second, second_value)):
        if name == 'operation':
            operation_key = CONTRACT_CALL[protocol]['operation_key']
            if operation_key is not None:
                call['protocol_info'][operation_key] = value
        elif name in _INFO_KEYS:
            call['protocol_info'][name] = value
        else:
            call[name] = value
    await _assert_only_typed_escapes(**call)


def test_every_public_parameter_of_request_is_covered() -> None:
    """The mechanism that stops this file going stale (NEW-2's real half).

    The matrix's blind spot was not a missing test, it was a
    **hand-written list of parameters that was silently three short**:
    ``pre_processor_config``, ``post_processor_config`` and ``**kwargs``
    had been documented public surface all along, and a review found
    eighteen bare builtins behind them.

    Hand-listing them again would go stale the same way, so the list is
    read off ``inspect.signature(request)`` and compared with
    :data:`COVERING_FAMILY`. Adding a parameter to ``request()`` fails
    this test until a hostile family is bound to it -- which is the
    point: a future parameter is covered *on arrival* rather than
    remembered about later.

    Returns:
        None.
    """
    signature = set(inspect.signature(request).parameters)
    assert signature, (
        'inspect.signature(request) reported no parameters at all, which '
        'means this test is checking nothing')

    uncovered = sorted(signature - set(COVERING_FAMILY))
    assert not uncovered, (
        f'request() takes {uncovered} and no hostile family in this file '
        f'covers them. That is exactly how the processor configs went '
        f'unchecked for the life of the package: they were public, '
        f'documented, and nobody had added them to the matrix. Add '
        f'hostile values for each and bind them in COVERING_FAMILY.')

    stale = sorted(set(COVERING_FAMILY) - signature)
    assert not stale, (
        f'COVERING_FAMILY claims to cover {stale}, which request() no '
        f'longer takes. A stale entry hides a real gap, because the '
        f'count looks right.')

    missing = sorted(
        name for name in set(COVERING_FAMILY.values())
        if name not in globals())
    assert not missing, (
        f'COVERING_FAMILY names {missing}, which this module does not '
        f'define. The binding has to point at a family that exists or it '
        f'proves nothing.')


def test_contract_selector_inventory_matches_the_production_registry() -> None:
    """The hostile matrix cannot silently omit a registered selector."""
    assert set(PROTOCOLS) == set(protocol_mapping)


@pytest.mark.parametrize('protocol', PROTOCOLS)
async def test_the_valid_call_still_succeeds_on_every_protocol(
    monkeypatch: pytest.MonkeyPatch,
    http_server: RecordingHTTPServer,
    protocol: str,
) -> None:
    """The matrix's control row, and the reason it can be trusted.

    Every assertion above passes trivially if ``request()`` rejects
    *everything* -- a library that refuses all input leaks no bare
    exceptions either. This row is what makes the others mean something:
    the same doubled transport, the same call shape, with nothing
    hostile in it, must still reach a success envelope.

    Args:
        monkeypatch: The patcher, for the doubled lane.
        http_server: The loopback server, for the live lane.
        protocol: The protocol under test.

    Returns:
        None.
    """
    envelope = await request(**_prepare(monkeypatch, protocol, http_server))
    assert envelope['ok'] is True, (
        f'the valid {protocol} call did not succeed against a transport '
        f'double that completes the operation: {envelope!r}')

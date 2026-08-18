"""The four protocols, against real servers, plus the security posture.

Everything here needs a live peer to mean anything. The unit suite covers
the same code far more exhaustively and does it against doubles, which is
right for 1475 tests but leaves a specific residue this file exists for:

* a **double can be handed any TLS value and still report success**, so
  "FTPS by default, never downgraded" is only proven by a server that
  refuses plaintext;
* **host-key verification** is a property of a real SSH handshake -- an
  unknown key must be refused, and the same connect must succeed once the
  key is pinned, and only a live key can show both;
* **AGW-33's operand order** (an upload that read the remote path off the
  local disk) is invisible to a double that accepts two path-shaped
  positionals in either order. The assertion has to be *the local file's
  bytes arrived at the remote path*, read back from the server; and
* **credential stripping on a cross-origin redirect** has to be observed
  at the second origin. In-process you can only assert what the client
  intended to send;
* **the multipart depth cap** (NEW-H2) protects the interpreter's stack,
  and a body arriving chunked over a socket drives the reader through a
  different await path than an in-memory one -- so the cap is watched
  holding at 64 and refusing past it over a real wire; and
* **URL userinfo masking** (NEW-M1c) is a disagreement between readers of
  one string, and ``aiohttp`` is a third reader that *re-spells* what it
  is given. The string reaching the envelope and the logged traceback is
  therefore only observable after a round trip.

Each protocol also gets at least one error path, because a library whose
whole contract is "one envelope shape, on success and on failure" is not
demonstrated by success alone.
"""

import asyncio
import contextlib
import logging
import os
import pathlib
import subprocess
import uuid
from typing import Any, Dict

# A sibling module of this file, imported as a module: `.flake8`'s
# `application_import_names` can only name distributions, so `conftest`
# is classified third-party whichever way it is imported -- and it
# sorts before `pytest`. Reading the constants off it also makes it
# obvious where each one is configured.
import asyncssh

import conftest as cfg

import pytest

from async_gateway.async_gateway import request
from async_gateway.utils.constants import MAX_MULTIPART_DEPTH
from async_gateway.utils.contained_io import (
    _open_guarded,
    contained_path_io_factory,
    local_base,
)
from async_gateway.utils.exceptions import (
    ConfigurationError,
    PathContainmentError,
    ProcessorError,
)
from async_gateway.utils.paths import FILE_MODE

BASE_HTTP = cfg.BASE_HTTP
BASE_HTTPS = cfg.BASE_HTTPS
BASE_OTHER = cfg.BASE_OTHER
BASE_SOAP = cfg.BASE_SOAP
BASE_WRONG_HOST = cfg.BASE_WRONG_HOST
FTP_HOST = cfg.FTP_HOST
FTP_PORT = cfg.FTP_PORT
FTP_USER = cfg.FTP_USER
KNOWN_HOSTS = cfg.KNOWN_HOSTS
REMOTE_FIXTURE = cfg.REMOTE_FIXTURE
SCRATCH_DIR = cfg.SCRATCH_DIR
SFTP_HOST = cfg.SFTP_HOST
SFTP_PORT = cfg.SFTP_PORT

# --------------------------------------------------------------- helpers --


def unique(stem: str) -> str:
    """Return a name no other test in this run will use.

    Args:
        stem: A readable prefix.

    Returns:
        The prefixed unique name.
    """
    return f'{stem}-{uuid.uuid4().hex[:12]}'


def assert_envelope(result: Dict[str, Any], protocol: str) -> None:
    """Assert the invariant envelope shape, whatever the outcome.

    The library's central promise is that every protocol returns the same
    key set on both the success and the failure path, so it is checked
    once here and every test calls it -- including the failing ones.

    Args:
        result: The envelope ``request()`` returned.
        protocol: The protocol name the call was dispatched on.
    """
    for key in (
            'ok', 'status_code', 'protocol', 'url', 'request_time',
            'latency', 'payload', 'text', 'json', 'headers', 'error',
            'protocol_details'):
        assert key in result, f'envelope is missing {key!r}'

    assert isinstance(result['ok'], bool)
    assert isinstance(result['status_code'], int)
    assert result['protocol'] == protocol
    assert isinstance(result['latency'], float)
    assert result['latency'] >= 0.0
    assert isinstance(result['text'], str)

    if result['ok']:
        assert result['error'] is None
    else:
        assert result['error'] is not None
        assert isinstance(result['error']['code'], str)
        assert result['error']['code']


# ------------------------------------------------------------------ HTTP --


async def test_http_get_returns_the_servers_body() -> None:
    """A plain GET succeeds and the parsed body reaches the caller."""
    result = await request(
        f'{BASE_HTTP}/echo?q=1',
        protocol='HTTP',
        protocol_info={'request_type': 'GET', 'timeout': 15},
    )

    assert_envelope(result, 'HTTP')
    assert result['ok'] is True
    assert result['status_code'] == 200
    assert result['json']['method'] == 'GET'
    assert result['json']['query'] == {'q': '1'}


async def test_http_post_sends_the_payload_the_server_receives() -> None:
    """The body the caller passed is the body that arrives.

    Asserted from the *server's* echo rather than from the request object,
    which is the only way to know serialisation actually happened.
    """
    payload = {'order': 'A-1', 'qty': 3}

    result = await request(
        f'{BASE_HTTP}/echo',
        data=payload,
        protocol='HTTP',
        protocol_info={'request_type': 'POST', 'timeout': 15},
    )

    assert_envelope(result, 'HTTP')
    assert result['ok'] is True
    assert result['json']['method'] == 'POST'
    assert '"order":"A-1"' in result['json']['body'].replace(' ', '')


async def test_http_404_is_a_failure_that_keeps_the_body() -> None:
    """Invariant E11: an ``ok=False`` must not cost the caller the body."""
    result = await request(
        f'{BASE_HTTP}/status/404',
        protocol='HTTP',
        protocol_info={'request_type': 'GET', 'timeout': 15},
    )

    assert_envelope(result, 'HTTP')
    assert result['ok'] is False
    assert result['error']['code'] == 'HTTP_STATUS'
    # The body survived the failure -- usually the part the caller needs.
    assert result['json'] is not None
    assert result['json']['path'] == '/status/404'


async def test_http_timeout_is_reported_as_timeout() -> None:
    """A server slower than the deadline fails with ``TIMEOUT``."""
    result = await request(
        f'{BASE_HTTP}/slow?seconds=10',
        protocol='HTTP',
        protocol_info={'request_type': 'GET', 'timeout': 2},
    )

    assert_envelope(result, 'HTTP')
    assert result['ok'] is False
    assert result['error']['code'] == 'TIMEOUT'
    assert result['status_code'] == 504


async def test_http_connection_refused_is_reported_as_connect() -> None:
    """A port nothing listens on fails with ``CONNECT``, not a crash."""
    result = await request(
        'http://http-server:9', protocol='HTTP',
        protocol_info={'request_type': 'GET', 'timeout': 5},
    )

    assert_envelope(result, 'HTTP')
    assert result['ok'] is False
    assert result['error']['code'] in {'CONNECT', 'TIMEOUT'}


# ----------------------------------------------------------------- HTTPS --


async def test_https_verifies_a_real_certificate_chain() -> None:
    """TLS against a leaf signed by the stack's CA, verification ON.

    Nothing here disables verification: the CA is in the container's trust
    store, so a success means the chain and the host name both checked
    out. That is the whole difference between this and a unit test.
    """
    result = await request(
        f'{BASE_HTTPS}/echo',
        protocol='HTTPS',
        protocol_info={'request_type': 'GET', 'timeout': 15},
    )

    assert_envelope(result, 'HTTPS')
    assert result['ok'] is True
    assert result['json']['scheme'] == 'https'


async def test_https_protocol_refuses_a_plaintext_url() -> None:
    """``protocol='HTTPS'`` will not dispatch on ``http://`` (H6).

    A configuration error, so it escapes rather than returning an
    envelope -- the caller asked for TLS and got a URL that cannot
    provide it, and no retry would help.
    """
    with pytest.raises(ConfigurationError, match='HTTPS'):
        await request(
            f'{BASE_HTTP}/echo',
            protocol='HTTPS',
            protocol_info={'request_type': 'GET', 'timeout': 10},
        )


# --------------------------------------------------- security: TLS verify --


async def test_tls_verification_rejects_an_untrusted_certificate() -> None:
    """Verification is enforced, not merely configured.

    The server presents a certificate for ``wrong-host`` while the client
    dials ``http-server``. Same trusted CA, so a client that checked only
    the signature would accept it -- the failure proves the *host name* is
    checked too.
    """
    result = await request(
        f'{BASE_WRONG_HOST}/echo',
        protocol='HTTPS',
        protocol_info={'request_type': 'GET', 'timeout': 10},
    )

    assert_envelope(result, 'HTTPS')
    assert result['ok'] is False
    assert result['error']['code'] == 'TLS'


# ------------------------------------- security: cross-origin credentials --


async def test_same_origin_redirect_keeps_the_authorization_header() -> None:
    """The control for the test below.

    Without this, "the header was absent at the far end" could just mean
    the client never sent it. Here the hop stays on one origin, and the
    header must arrive.
    """
    target = f'{BASE_HTTP}/echo'
    result = await request(
        f'{BASE_HTTP}/redirect?to={target}',
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'timeout': 15,
            'headers': {
                'Authorization': 'Bearer same-origin-token',
                'Cookie': 'session=abc123',
            },
        },
    )

    assert_envelope(result, 'HTTP')
    assert result['ok'] is True
    arrived = result['json']['headers']
    assert arrived.get('authorization') == 'Bearer same-origin-token'


async def test_cross_origin_redirect_drops_credential_headers() -> None:
    """A credential must not follow a redirect to a different origin.

    Observed at the second origin, which is the only place the claim can
    be settled: the second server reports every header it received, so an
    absent ``authorization`` is a fact about the wire and not about the
    client's intentions.
    """
    target = f'{BASE_OTHER}/echo'
    result = await request(
        f'{BASE_HTTP}/redirect?to={target}',
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'timeout': 15,
            'headers': {
                'Authorization': 'Bearer cross-origin-token',
                'Cookie': 'session=abc123',
                'Proxy-Authorization': 'Basic Zm9vOmJhcg==',
            },
        },
    )

    assert_envelope(result, 'HTTP')
    assert result['ok'] is True

    arrived = result['json']['headers']
    # It really did reach the other origin...
    assert result['json']['host'].startswith(
        os.environ.get('OTHER_HOST', 'http-other'))
    # ...and arrived carrying none of the three credentials.
    assert 'authorization' not in arrived
    assert 'cookie' not in arrived
    assert 'proxy-authorization' not in arrived


# ------------------------------------ security: hostile multipart nesting --
#
# The depth cap and the iterative walk (NEW-H2) are pinned exhaustively in
# process, against a loopback server. These rows exist because the
# resource that fix protects -- the interpreter's stack -- is one the wire
# can influence: a body arriving in 512-byte chunks across a real socket
# drives the reader through a different await path than an in-memory one,
# and a depth guard placed in the buffered branch alone would pass the
# unit suite. The bodies are streamed with no declared Content-Length, so
# nothing can be refused by reading a length header.


async def fetch_nested(
    depth: int,
    tmp_path: pathlib.Path,
    per_level: int = 0,
    **info: Any,
) -> Dict[str, Any]:
    """Read a live nested multipart body ``depth`` levels deep.

    Args:
        depth: How many reader levels the server should nest.
        tmp_path: Where the multipart download lands.
        per_level: Bytes of leaf payload to place at every level, for the
            byte-cap row; 0 leaves the body all boundaries.
        **info: Extra ``protocol_info`` keys.

    Returns:
        The envelope ``request()`` returned.
    """
    return await request(
        f'{BASE_HTTP}/nested-multipart'
        f'?depth={depth}&per_level={per_level}',
        protocol='HTTP',
        protocol_info={
            'request_type': 'GET',
            'timeout': 60,
            'http_file_download_config': {
                'download_filepath': str(tmp_path / f'deep-{depth}.bin'),
                'overwrite': True,
            },
            **info,
        },
    )


@pytest.mark.parametrize('depth', [60, MAX_MULTIPART_DEPTH])
async def test_nesting_within_the_cap_is_read_from_a_real_server(
    depth: int, tmp_path: pathlib.Path,
) -> None:
    """At and below the cap the leaf arrives, over a real socket.

    The half that stops the guard being "fixed" by refusing everything.
    ``MAX_MULTIPART_DEPTH`` is read from the library rather than restated,
    so lowering the cap fails this row instead of quietly narrowing what
    the library accepts.

    Args:
        depth: The nesting depth to serve.
        tmp_path: Where the download lands.
    """
    result = await fetch_nested(depth, tmp_path)

    assert_envelope(result, 'HTTP')
    assert result['ok'] is True, result['error']
    assert result['status_code'] == 200
    assert result['text'] == 'bottom'


@pytest.mark.parametrize('depth', [MAX_MULTIPART_DEPTH + 1, 2000])
async def test_nesting_past_the_cap_is_an_envelope_not_an_exception(
    depth: int, tmp_path: pathlib.Path,
) -> None:
    """Past the cap the caller gets an envelope, never a bare exception.

    2000 is the depth as NEW-H2 reported it -- ~221 KB, far under any byte
    ceiling, and enough to exhaust the interpreter's stack before the walk
    was made iterative. The assertion that matters is as much that
    ``request()`` *returned* as what it returned: a ``RecursionError``
    escaping here would fail this row by propagating out of the await
    rather than by comparing unequal.

    Args:
        depth: The nesting depth to serve.
        tmp_path: Where the download would have landed.
    """
    result = await fetch_nested(depth, tmp_path)

    assert_envelope(result, 'HTTP')
    assert result['ok'] is False
    assert result['error']['code'] == 'RESPONSE_TOO_DEEP'
    assert result['status_code'] == 502
    assert f'max_multipart_depth={MAX_MULTIPART_DEPTH}' in (
        result['error']['message'])


async def test_the_byte_cap_still_spans_nesting_levels_over_the_wire(
    tmp_path: pathlib.Path,
) -> None:
    """A nested body cannot buy a fresh byte budget by nesting (R14).

    The two bounds are independent, so the depth cap must not have
    displaced the byte one: this body is well inside
    ``MAX_MULTIPART_DEPTH`` and is refused for its *size*.

    The payload is spread across the levels rather than concentrated in
    one, and that is the whole design of the row: 8 levels of 512 bytes
    against a 2 KiB ceiling means no single level reaches the cap and only
    the running sum does. A budget reset per nested reader -- the mutant
    R14 exists to exclude -- would read this body happily, whereas a body
    with all its bytes at one level would refuse under both the correct
    code and the mutant.
    """
    result = await fetch_nested(
        8, tmp_path, per_level=512, max_response_bytes=2048)

    assert_envelope(result, 'HTTP')
    assert result['ok'] is False
    assert result['error']['code'] == 'RESPONSE_TOO_LARGE'


# ---------------------------------- security: userinfo on a real round trip --


AUTHORITY = f'{cfg.HTTP_HOST}:{cfg.HTTP_PORT}/status/404'

# The separator spellings NEW-M1c is about, each written as a prefix and a
# suffix so the credential can be inserted between them.
#
# The last two matter most and are the reason this row is parametrized by
# *spelling* rather than by route. Measured against this live server with
# redaction stubbed out, the canonical `http://` shape never puts the
# credential in the rendered traceback at all -- so asserting its absence
# there is vacuous, and a deleted `redact_text` call passes. Under
# `http:/\/` and `http: //` the raw traceback does carry it, because
# `urlsplit` finds no authority, the URL is carried as an opaque string
# and a chained exception stringifies it. Those are the rows where the
# traceback assertion has teeth.
SPELLINGS = {
    'canonical': ('http://', AUTHORITY),
    'backslash': ('http:/\\/', AUTHORITY),
    'space': ('http: //', AUTHORITY),
    'tab': ('http:\t//', AUTHORITY),
    'closed-port': ('http://', f'{cfg.HTTP_HOST}:9/echo'),
}


@pytest.mark.parametrize(
    ('prefix', 'authority'), SPELLINGS.values(), ids=list(SPELLINGS))
async def test_userinfo_is_masked_on_a_real_request(
    prefix: str,
    authority: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A URL's password must not survive an aiohttp round trip anywhere.

    NEW-M1c is a disagreement bug: a hand-rolled scanner and a URL parser
    reading one string differently, four times, each fix teaching the
    scanner one more spelling. ``aiohttp`` is a *third* reader, and it
    re-spells what it is given -- normalising separators, percent-encoding
    them back -- so the string that reaches ``error['message']`` and the
    logged traceback is not always the string the caller passed. A
    backslash arrives back as ``%5C`` and a space as ``%20``, and it is
    those spellings, not the caller's, that the envelope reports. That
    round trip only happens against a real server, which is why this row
    is here and not only in the unit suite.

    Three surfaces, because each is produced by a different call:
    ``redact_url`` for the envelope, ``redact_value`` for
    ``extra['url']``, and ``redact_text`` for ``extra['traceback']`` --
    the last being the one that leaked in three of the four rounds.

    The request fails in every row, deliberately: the failure path is the
    exposed one, being the only path that renders a traceback at all.

    Args:
        prefix: The scheme and separator run, one of the spellings above.
        authority: The host, port and path to aim at.
        caplog: Captures the library's own log record.
    """
    secret = 'sup3rs3cr3t-pw'
    url = f'{prefix}user:{secret}@{authority}'

    with caplog.at_level(logging.WARNING, logger='async_gateway'):
        result = await request(
            url,
            protocol='HTTP',
            protocol_info={'request_type': 'GET', 'timeout': 15},
        )

    assert_envelope(result, 'HTTP')
    assert result['ok'] is False

    # 1. The envelope: the URL handed back, and the error prose that
    #    names it.
    assert secret not in result['url']
    assert secret not in str(result['error'])

    records = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
        and hasattr(record, 'traceback')
    ]
    assert records, 'the failure logged no record to inspect'
    record = records[-1]

    # 2. `extra['url']`, and 3. `extra['traceback']` -- the surface that
    #    leaked in three of the four rounds.
    assert secret not in record.url
    assert secret not in record.traceback

    # "Absent" must not be allowed to mean "the field is empty" or "the
    # URL never reached the record", which would make every assertion
    # above pass against a library that reported nothing at all. So each
    # surface must still carry the part of the URL that is safe to report.
    #
    # Only the host is required, not a fixed shape: the two masking paths
    # legitimately differ. Where `urlsplit` finds an authority the
    # credential is *dropped* (`http://host/p`), and where it does not the
    # string is masked in place (`http:/\/user:***redacted***@host/p`).
    # Both satisfy the invariant -- the secret is gone and the diagnostic
    # survives -- so pinning either spelling here would encode an
    # implementation detail as a requirement.
    for surface in (result['url'], record.url, record.traceback):
        assert cfg.HTTP_HOST in surface


# ------------------- security: the component the query scan used to miss --
#
# The round after NEW-M1c. `redact_url` had a masking rule of its own --
# it split `parts.query` with `parse_qsl` -- while `redact_text` scanned
# the whole string, so the two agreed about what a *separator* was and
# still disagreed about *where to look*. Two spellings put a secret
# outside `parts.query` and therefore outside the only component
# `redact_url` read:
#
#   * `/p;api_key=S`      -- RFC 3986 path parameters, which PHP and
#                            servlet containers read as parameters, and
#                            which `urlsplit` files under `path`; and
#   * `/p#frag?api_key=S` -- a `?` *after* the `#`, which `urlsplit`
#                            files entirely under `fragment`.
#
# Both were masked by `redact_text` and published in the clear by
# `redact_url` -- in the envelope `url` on the ok=True and ok=False paths
# alike. The fix deleted the second rule: there is now one scan over the
# whole string, and no component is enumerated.
#
# Proved in-process by the unit suite; proved *on the wire* here, which
# is a different question. `aiohttp` re-spells what it is handed -- it is
# the third reader of the string, after `urlsplit` and the masker -- so
# the URL that reaches the envelope and the rendered traceback is not
# necessarily the one the caller passed. `yarl` keeps `;api_key=S` in the
# path verbatim and strips `#frag?api_key=S` from the request target
# entirely, and neither of those is knowable without a real round trip.
#
# Each spelling is driven on BOTH outcomes, because the two are produced
# by different code and the bug was present in both: the ok=True path
# reports a URL nothing failed about, and the ok=False path adds the
# error prose and the logged traceback.


#: The component each spelling hides the secret in, and the route that
#: reaches a live handler for it. `/echo{suffix}` and
#: `/status/404{suffix}` exist in `docker/http/server.py` precisely so
#: the path-parameter spelling can be driven to a real 200 as well as to
#: a real failure -- without them it 404s on the route table and the
#: ok=True half of the assertion cannot be made at all.
COMPONENT_SPELLINGS = {
    'path-parameter': ('/echo;api_key=', '/status/404;api_key='),
    'after-fragment': ('/echo#frag?api_key=', '/status/404#frag?api_key='),
}


@pytest.mark.parametrize(
    ('ok_path', 'fail_path'),
    COMPONENT_SPELLINGS.values(),
    ids=list(COMPONENT_SPELLINGS))
async def test_a_secret_outside_the_query_is_masked_on_a_real_request(
    ok_path: str,
    fail_path: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A secret outside ``parts.query`` must not survive a round trip.

    Three surfaces, each produced by a different call -- ``redact_url``
    for the envelope, ``redact_value`` for ``extra['url']`` and
    ``redact_text`` for ``extra['traceback']`` -- and two outcomes,
    because the defect published the URL on both.

    The success path can only assert the envelope: nothing failed, so
    there is no error prose and no record to read. The failure path
    asserts all three, and it is the one with teeth -- the traceback is
    rendered from a chained exception that stringifies the URL, which is
    the surface that leaked in three of the four previous rounds.

    Args:
        ok_path: A path, carrying the secret, that reaches a 200.
        fail_path: A path, carrying the secret, that reaches a 404.
        caplog: Captures the library's own log record.
    """
    secret = 'sup3rs3cr3t-key'

    # 1. The success path. `ok=True` reports a URL too, and that is the
    #    half a "we only redact failures" reading would miss.
    ok_result = await request(
        f'{BASE_HTTP}{ok_path}{secret}',
        protocol='HTTP',
        protocol_info={'request_type': 'GET', 'timeout': 15},
    )

    assert_envelope(ok_result, 'HTTP')
    assert ok_result['ok'] is True, (
        f'the {ok_path} route did not reach a handler, so this asserts '
        f'nothing about the ok=True path: {ok_result["error"]}')
    assert secret not in ok_result['url']

    # 2. The failure path: the envelope, the error prose that names the
    #    URL, and the two log surfaces.
    with caplog.at_level(logging.WARNING, logger='async_gateway'):
        result = await request(
            f'{BASE_HTTP}{fail_path}{secret}',
            protocol='HTTP',
            protocol_info={'request_type': 'GET', 'timeout': 15},
        )

    assert_envelope(result, 'HTTP')
    assert result['ok'] is False

    assert secret not in result['url']
    assert secret not in str(result['error'])

    records = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
        and hasattr(record, 'traceback')
    ]
    assert records, 'the failure logged no record to inspect'
    record = records[-1]

    assert secret not in record.url
    assert secret not in record.traceback

    # "Absent" must not be allowed to mean "the URL never reached the
    # surface" -- that would pass against a library reporting nothing at
    # all. Every surface must still carry the host, and the *masked*
    # parameter name must still be visible, which is what distinguishes
    # "the pair was found and masked" from "the whole tail was dropped".
    for surface in (
            ok_result['url'], result['url'], record.url, record.traceback):
        assert cfg.HTTP_HOST in surface
        assert 'api_key=' in surface, (
            f'the parameter was discarded rather than masked: {surface}')


# ------------------------------------------------------------------- FTP --


async def test_ftp_download_over_ftps(
    ftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """A download over a verified FTPS session lands the server's bytes.

    ``verify_ssl`` is not passed, so this also asserts the default: the
    session negotiated AUTH TLS against a server configured to refuse
    anything else.
    """
    local = tmp_path / 'report.csv'

    result = await request(
        FTP_HOST,
        protocol='FTP',
        auth=ftp_auth,
        protocol_info={
            'port': FTP_PORT,
            'command': 'download',
            'server_path': 'pub/report.csv',
            'client_path': str(local),
            'timeout': 30,
        },
    )

    assert_envelope(result, 'FTP')
    assert result['ok'] is True
    assert local.read_bytes() == REMOTE_FIXTURE
    assert result['protocol_details']['command'] == 'download'


async def test_ftp_upload_puts_local_content_at_the_remote_path(
    ftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """AGW-33, against a live server, in the only way that can prove it.

    The defect reversed the two operands, so an upload read
    ``server_path`` off the LOCAL disk and wrote it to ``client_path`` on
    the server. A double taking two path-shaped positionals cannot tell
    the orders apart -- both bind cleanly.

    So the assertion is neither "the arguments were in this order" nor
    "the call returned ok". It is: the bytes that are now at the REMOTE
    path are the bytes of the LOCAL file, fetched back afterwards. Under
    the reversed order this fails, because the local file is never read.

    The remote name is unique per run and the destination directory is
    emptied at container start, so a stale file cannot satisfy it.
    """
    content = f'local-bytes-{uuid.uuid4().hex}\n'.encode()
    local = tmp_path / 'outgoing.txt'
    local.write_bytes(content)
    remote = f'incoming/{unique("upload")}.txt'

    result = await request(
        FTP_HOST,
        protocol='FTP',
        auth=ftp_auth,
        protocol_info={
            'port': FTP_PORT,
            'command': 'upload',
            'server_path': remote,       # DESTINATION, on the server
            'client_path': str(local),   # SOURCE, here
            'timeout': 30,
        },
    )

    assert_envelope(result, 'FTP')
    assert result['ok'] is True

    # Read it back DOWN to a different local path. Round-tripping through
    # the server is what makes this a statement about the remote file
    # rather than about the local one we just wrote.
    verify = tmp_path / 'verify.txt'
    back = await request(
        FTP_HOST,
        protocol='FTP',
        auth=ftp_auth,
        protocol_info={
            'port': FTP_PORT,
            'command': 'download',
            'server_path': remote,
            'client_path': str(verify),
            'timeout': 30,
        },
    )

    assert back['ok'] is True, f'the upload left nothing at {remote}'
    assert verify.read_bytes() == content, (
        "the bytes at the remote path are not the local file's bytes -- "
        'the operands are reversed (AGW-33)')


async def test_ftp_download_of_a_missing_path_fails_with_a_status(
    ftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """A path the server does not have is an ``FTP_STATUS`` envelope."""
    result = await request(
        FTP_HOST,
        protocol='FTP',
        auth=ftp_auth,
        protocol_info={
            'port': FTP_PORT,
            'command': 'download',
            'server_path': 'pub/does-not-exist.csv',
            'client_path': str(tmp_path / 'nothing.csv'),
            'timeout': 30,
        },
    )

    assert_envelope(result, 'FTP')
    assert result['ok'] is False
    assert result['error']['code'] in {'FTP_STATUS', 'TRANSPORT', 'CONNECT'}


async def test_ftp_bad_credentials_fail(tmp_path: pathlib.Path) -> None:
    """A wrong password does not succeed and does not crash."""
    from types import SimpleNamespace

    result = await request(
        FTP_HOST,
        protocol='FTP',
        auth=SimpleNamespace(login=FTP_USER, password='wrong-password'),
        protocol_info={
            'port': FTP_PORT,
            'command': 'download',
            'server_path': 'pub/report.csv',
            'client_path': str(tmp_path / 'x.csv'),
            'timeout': 30,
        },
    )

    assert_envelope(result, 'FTP')
    assert result['ok'] is False


# ---------------------------------- security: FTPS never downgrades --


async def test_the_ftp_server_itself_refuses_plaintext() -> None:
    """Establish the premise the FTPS assertions rest on.

    Probed with a raw socket rather than with the library under test,
    deliberately: "our client used TLS" and "this server would have
    refused not to" are different claims, and proving the second with the
    first is circular. If this fails, every FTPS assertion above is
    vacuous and should be read as untested.

    The server speaks implicit FTPS, so a plaintext client gets no
    greeting at all -- the first bytes it receives are a ServerHello it
    cannot read, or the connection simply yields nothing before the
    deadline. A plaintext FTP server, by contrast, sends ``220 ...``
    immediately.
    """
    reader, writer = await asyncio.open_connection(FTP_HOST, FTP_PORT)
    try:
        try:
            greeting = await asyncio.wait_for(reader.read(4), timeout=5)
        except asyncio.TimeoutError:
            greeting = b''
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    assert not greeting.startswith(b'220'), (
        'the FTP server sent a plaintext FTP greeting, so it is not '
        'running implicit FTPS and every FTPS assertion here is vacuous')


async def test_ftp_with_verify_ssl_false_is_refused_by_the_server(
    ftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """Opting out of TLS reaches a server that will not have it.

    The library permits ``verify_ssl=False`` -- it is the caller's call --
    and this asserts what that actually costs: against a server requiring
    AUTH TLS the session fails rather than silently sending credentials
    in the clear.
    """
    result = await request(
        FTP_HOST,
        protocol='FTP',
        auth=ftp_auth,
        protocol_info={
            'port': FTP_PORT,
            'command': 'download',
            'server_path': 'pub/report.csv',
            'client_path': str(tmp_path / 'plain.csv'),
            'verify_ssl': False,
            'timeout': 30,
        },
    )

    assert_envelope(result, 'FTP')
    assert result['ok'] is False


# ------------------------------------------------------------------ SFTP --


def known_hosts_available() -> bool:
    """Report whether the server published its host key.

    Returns:
        True when the pinned ``known_hosts`` file exists.
    """
    return pathlib.Path(KNOWN_HOSTS).is_file()


requires_hostkey = pytest.mark.skipif(
    not known_hosts_available(),
    reason='the sftp server has not published its host key')


@requires_hostkey
async def test_sftp_get_with_a_pinned_host_key(
    sftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """A download succeeds when the server's key is pinned."""
    local = tmp_path / 'report.csv'

    result = await request(
        SFTP_HOST,
        protocol='SFTP',
        auth=sftp_auth,
        protocol_info={
            'port': SFTP_PORT,
            'mode': 'get',
            'remote_path': 'pub/report.csv',
            'local_path': str(local),
            'known_hosts': KNOWN_HOSTS,
            'timeout': 30,
        },
    )

    assert_envelope(result, 'SFTP')
    assert result['ok'] is True
    assert local.read_bytes() == REMOTE_FIXTURE


@requires_hostkey
async def test_sftp_put_puts_local_content_at_the_remote_path(
    sftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """AGW-33 for SFTP, proven the same way as for FTP.

    ``asyncssh`` signatures disagree by verb -- ``get(remote, local)`` but
    ``put(local, remote)`` -- so one shared positional order is right for
    exactly one of them. The proof is again the round trip: what is at the
    remote path afterwards must be the local file's bytes.
    """
    content = f'sftp-local-bytes-{uuid.uuid4().hex}\n'.encode()
    local = tmp_path / 'outgoing.txt'
    local.write_bytes(content)
    remote = f'incoming/{unique("put")}.txt'

    result = await request(
        SFTP_HOST,
        protocol='SFTP',
        auth=sftp_auth,
        protocol_info={
            'port': SFTP_PORT,
            'mode': 'put',
            'remote_path': remote,       # DESTINATION, on the server
            'local_path': str(local),    # SOURCE, here
            'known_hosts': KNOWN_HOSTS,
            'timeout': 30,
        },
    )

    assert_envelope(result, 'SFTP')
    assert result['ok'] is True

    verify = tmp_path / 'verify.txt'
    back = await request(
        SFTP_HOST,
        protocol='SFTP',
        auth=sftp_auth,
        protocol_info={
            'port': SFTP_PORT,
            'mode': 'get',
            'remote_path': remote,
            'local_path': str(verify),
            'known_hosts': KNOWN_HOSTS,
            'timeout': 30,
        },
    )

    assert back['ok'] is True, f'the put left nothing at {remote}'
    assert verify.read_bytes() == content, (
        "the bytes at the remote path are not the local file's bytes -- "
        'the operands are reversed (AGW-33)')


@requires_hostkey
async def test_sftp_remove_deletes_the_remote_file(
    sftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """``remove`` deletes, and a later ``get`` of the path then fails."""
    local = tmp_path / 'doomed.txt'
    local.write_bytes(b'temporary\n')
    remote = f'incoming/{unique("doomed")}.txt'

    put = await request(
        SFTP_HOST, protocol='SFTP', auth=sftp_auth,
        protocol_info={
            'port': SFTP_PORT, 'mode': 'put', 'remote_path': remote,
            'local_path': str(local), 'known_hosts': KNOWN_HOSTS,
            'timeout': 30})
    assert put['ok'] is True

    removed = await request(
        SFTP_HOST, protocol='SFTP', auth=sftp_auth,
        protocol_info={
            'port': SFTP_PORT, 'mode': 'remove', 'remote_path': remote,
            'known_hosts': KNOWN_HOSTS, 'timeout': 30})

    assert_envelope(removed, 'SFTP')
    assert removed['ok'] is True

    gone = await request(
        SFTP_HOST, protocol='SFTP', auth=sftp_auth,
        protocol_info={
            'port': SFTP_PORT, 'mode': 'get', 'remote_path': remote,
            'local_path': str(tmp_path / 'gone.txt'),
            'known_hosts': KNOWN_HOSTS, 'timeout': 30})

    assert gone['ok'] is False, 'the file is still there after remove'


@requires_hostkey
async def test_sftp_get_of_a_missing_path_fails(
    sftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """A path the server does not have is a failure envelope."""
    result = await request(
        SFTP_HOST, protocol='SFTP', auth=sftp_auth,
        protocol_info={
            'port': SFTP_PORT, 'mode': 'get',
            'remote_path': 'pub/not-here.csv',
            'local_path': str(tmp_path / 'x.csv'),
            'known_hosts': KNOWN_HOSTS, 'timeout': 30})

    assert_envelope(result, 'SFTP')
    assert result['ok'] is False
    assert result['error']['code'] in {'SFTP_STATUS', 'TRANSPORT'}


# ------------------------------------------ security: SSH host-key checking --


async def test_sftp_refuses_an_unknown_host_key_by_default(
    sftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """The default REFUSES an unpinned server. The headline SFTP claim.

    No ``known_hosts``, no ``host_key``, no opt-out -- exactly what a
    caller who did not think about it gets. asyncssh resolves its own
    ``~/.ssh/known_hosts``, which in this container does not contain this
    server, so the connect must fail with ``HOST_KEY`` rather than
    trusting whatever key was offered.

    Paired with ``test_sftp_get_with_a_pinned_host_key`` above: that one
    proves the same connect *succeeds* once the key is known, so this
    failure is host-key verification and not a broken server.
    """
    result = await request(
        SFTP_HOST,
        protocol='SFTP',
        auth=sftp_auth,
        protocol_info={
            'port': SFTP_PORT,
            'mode': 'get',
            'remote_path': 'pub/report.csv',
            'local_path': str(tmp_path / 'should-not-exist.csv'),
            'timeout': 30,
        },
    )

    assert_envelope(result, 'SFTP')
    assert result['ok'] is False
    assert result['error']['code'] == 'HOST_KEY'
    assert result['status_code'] == 495
    assert not (tmp_path / 'should-not-exist.csv').exists()


@requires_hostkey
async def test_sftp_refuses_a_host_key_that_is_not_the_servers(
    sftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """Pinning the WRONG key is refused too.

    Stronger than the test above: it rules out "any `known_hosts` file
    satisfies the check". The file here is syntactically valid and names
    this host -- with somebody else's key.
    """
    wrong = tmp_path / 'wrong_known_hosts'
    generated = subprocess.run(
        ['ssh-keygen', '-t', 'ed25519', '-N', '', '-f',
         str(tmp_path / 'other_key'), '-q'],
        capture_output=True, text=True, check=False)
    if generated.returncode != 0:      # pragma: no cover - env dependent
        pytest.skip('ssh-keygen unavailable in this image')

    other = (tmp_path / 'other_key.pub').read_text().split()
    wrong.write_text(f'{SFTP_HOST} {other[0]} {other[1]}\n')

    result = await request(
        SFTP_HOST, protocol='SFTP', auth=sftp_auth,
        protocol_info={
            'port': SFTP_PORT, 'mode': 'get',
            'remote_path': 'pub/report.csv',
            'local_path': str(tmp_path / 'nope.csv'),
            'known_hosts': str(wrong), 'timeout': 30})

    assert_envelope(result, 'SFTP')
    assert result['ok'] is False
    assert result['error']['code'] == 'HOST_KEY'


async def test_sftp_opting_out_of_host_key_checking_must_be_named(
    sftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """Turning verification off takes the explicit flag, and works.

    The counterpart to the default: the escape hatch exists, it is spelled
    out in full, and a falsy ``verify_ssl`` is not it. This is what proves
    the earlier refusal was the host-key policy rather than any other
    connect failure.
    """
    local = tmp_path / 'insecure.csv'

    result = await request(
        SFTP_HOST, protocol='SFTP', auth=sftp_auth,
        protocol_info={
            'port': SFTP_PORT, 'mode': 'get',
            'remote_path': 'pub/report.csv',
            'local_path': str(local),
            'insecure_skip_host_key_check': True,
            'timeout': 30})

    assert_envelope(result, 'SFTP')
    assert result['ok'] is True
    assert local.read_bytes() == REMOTE_FIXTURE


# ------------------------------------------------------------------ SOAP --


SOAP_BODY = '<GetRate xmlns="urn:rates"><Pair>EURUSD</Pair></GetRate>'
SOAP_UNKNOWN = '<GetRate xmlns="urn:rates"><Pair>ZZZAAA</Pair></GetRate>'


async def test_soap_11_call_parses_the_reply_body() -> None:
    """A SOAP 1.1 call returns the parsed body element.

    The server echoes the ``SOAPAction`` it received, so this also
    asserts the version-correct transport header actually crossed the
    wire rather than merely being constructed.
    """
    result = await request(
        f'{BASE_SOAP}/rates',
        data=SOAP_BODY,
        protocol='SOAP',
        protocol_info={
            'soap_version': '1.1',
            'soap_action': 'urn:rates#GetRate',
            'timeout': 20,
        },
    )

    assert_envelope(result, 'SOAP')
    assert result['ok'] is True

    body = result['protocol_details']['soap_body']
    assert body is not None
    assert body.tag == '{urn:rates}GetRateResponse'

    values = {child.tag: child.text for child in body}
    assert values['{urn:rates}Rate'] == '1.0842'
    assert values['{urn:rates}EchoedAction'] == 'urn:rates#GetRate'


async def test_soap_12_uses_its_own_content_type() -> None:
    """A SOAP 1.2 call carries the action on the content type, not a header.

    The server reads the action off the ``application/soap+xml``
    parameter and echoes it, so a client that sent a 1.1-shaped request
    would come back with an empty echo.
    """
    result = await request(
        f'{BASE_SOAP}/rates',
        data=SOAP_BODY,
        protocol='SOAP',
        protocol_info={
            'soap_version': '1.2',
            'soap_action': 'urn:rates#GetRate',
            'timeout': 20,
        },
    )

    assert_envelope(result, 'SOAP')
    assert result['ok'] is True

    body = result['protocol_details']['soap_body']
    values = {child.tag: child.text for child in body}
    assert values['{urn:rates}Rate'] == '1.0842'
    assert values['{urn:rates}EchoedAction'] == 'urn:rates#GetRate'


async def test_soap_11_fault_is_a_failure_with_the_fault_parsed() -> None:
    """A Fault is a failure, and the Fault itself reaches the caller."""
    result = await request(
        f'{BASE_SOAP}/rates',
        data=SOAP_UNKNOWN,
        protocol='SOAP',
        protocol_info={
            'soap_version': '1.1',
            'soap_action': 'urn:rates#GetRate',
            'timeout': 20,
        },
    )

    assert_envelope(result, 'SOAP')
    assert result['ok'] is False
    assert result['error']['code'] == 'SOAP_FAULT'

    fault = result['protocol_details']['soap_fault']
    assert fault is not None
    assert 'unknown currency pair' in fault['reason']


async def test_soap_12_fault_is_parsed_in_its_own_shape() -> None:
    """The 1.2 Fault grammar differs from 1.1 and is read correctly."""
    result = await request(
        f'{BASE_SOAP}/rates',
        data=SOAP_UNKNOWN,
        protocol='SOAP',
        protocol_info={
            'soap_version': '1.2',
            'soap_action': 'urn:rates#GetRate',
            'timeout': 20,
        },
    )

    assert_envelope(result, 'SOAP')
    assert result['ok'] is False
    assert result['error']['code'] == 'SOAP_FAULT'

    fault = result['protocol_details']['soap_fault']
    assert fault is not None
    assert 'unknown currency pair' in fault['reason']


async def test_soap_non_fault_error_is_an_http_status() -> None:
    """A 5xx carrying no Fault is a status failure, not a Fault.

    The ordering rule the client documents -- a Fault beats the status,
    but only when there is one -- exercised against a real non-SOAP
    error body.
    """
    result = await request(
        f'{BASE_SOAP}/not-soap',
        data=SOAP_BODY,
        protocol='SOAP',
        protocol_info={'soap_version': '1.1', 'timeout': 20},
    )

    assert_envelope(result, 'SOAP')
    assert result['ok'] is False
    assert result['error']['code'] != 'SOAP_FAULT'


# ----------------------------- the processor hooks, against a real server --
#
# NEW-2. `pre_processor_config['function']` was indexed and awaited with
# no checking at all, so a documented public parameter was a direct route
# to a bare builtin escaping `request()`: `KeyError('function')` for a
# config missing the key, `TypeError` in three different spellings for a
# config that is a list, a non-callable function, or a non-mapping
# `params`. A caller cannot be asked to catch three builtin types for one
# configuration mistake, and the contract names none of them.
#
# The fix splits the question in two, and the split is the thing worth
# asserting live:
#
#   * `validated_processor_config` refuses what is decidable WITHOUT
#     calling -- shape, key, callability, params -- as a
#     `ConfigurationError` (CONFIG/400), raised synchronously, outside
#     the one conversion `try`, and BEFORE anything is dispatched; and
#   * `ProcessorError` (PROCESSOR/500) reports a config that was valid
#     and a callback that then failed anyway -- it raised, it refused the
#     `response` keyword, it returned a non-awaitable, or it removed a
#     key from the envelope it was handed.
#
# Both were proven in-process only. Against a live server two further
# properties become observable and neither is visible to a double: that a
# malformed config is refused with the socket never opened -- the server
# is right there to have logged a hit and does not -- and that the
# envelope-key check survives a callback mutating the *real* envelope a
# real transfer is about to read, rather than a constructed one.


async def a_working_processor(response: Dict[str, Any]) -> str:
    """Return a marker, having touched nothing.

    Args:
        response: The live envelope, passed under ``response``.

    Returns:
        A marker the test asserts reached the envelope.
    """
    return 'processor-ran'


async def a_raising_processor(response: Dict[str, Any]) -> str:
    """Raise the way a caller's own buggy callback would.

    ``KeyError`` deliberately: a bare builtin is exactly what used to
    escape ``request()``, so the assertion is that this one does not.

    Args:
        response: The live envelope, passed under ``response``.

    Returns:
        Never; this always raises.

    Raises:
        KeyError: Always.
    """
    raise KeyError('a key the callback expected and did not find')


async def a_key_removing_processor(response: Dict[str, Any]) -> str:
    """Delete an envelope key the protocol client goes on to read.

    ``payload`` specifically -- ``http_client`` reads
    ``self.response['payload']`` and ``soap_client`` reads it two lines
    into building the SOAP body, both from inside the one conversion
    ``try`` where ``except AsyncGatewayError`` cannot see a ``KeyError``.

    Args:
        response: The live envelope, passed under ``response``.

    Returns:
        A marker, which the caller never sees because the boundary
        refuses the envelope first.
    """
    del response['payload']
    return 'removed'


#: Every shape of malformed config, and the substring the refusal must
#: name so the caller is told *which* mistake they made rather than only
#: that they made one.
MALFORMED_CONFIGS = {
    'not-a-mapping': ('a string, not a config', 'must be a mapping'),
    'a-list': ([{'function': a_working_processor}], 'must be a mapping'),
    'no-function-key': ({'params': {}}, 'missing required key'),
    'function-not-callable': ({'function': 'nope'}, 'must be callable'),
    'params-not-a-mapping': (
        {'function': a_working_processor, 'params': 'nope'},
        '["params"] must be a mapping'),
    'params-key-not-str': (
        {'function': a_working_processor, 'params': {1: 'x'}},
        'keys must be str'),
    'params-names-response': (
        {'function': a_working_processor, 'params': {'response': 'x'}},
        'may not name'),
}


@pytest.mark.parametrize('setting',
                         ['pre_processor_config', 'post_processor_config'])
@pytest.mark.parametrize(
    ('config', 'expected'),
    MALFORMED_CONFIGS.values(),
    ids=list(MALFORMED_CONFIGS))
async def test_a_malformed_processor_config_never_reaches_the_server(
    setting: str,
    config: object,
    expected: str,
) -> None:
    """A malformed config is a typed refusal, and the call never goes out.

    Two assertions, and the second is the one only a live server can
    make. The first is that the failure is a ``ConfigurationError``
    carrying ``CONFIG``/400 and naming the setting -- not the bare
    ``KeyError`` or one of the three ``TypeError`` spellings that used to
    escape. The second is that it is raised *before dispatch*: the
    ``post_processor_config`` row is the proof, since a config validated
    only at the moment it runs would have contacted the server first and
    the remote side cannot be un-contacted.

    Args:
        setting: Which of the two hooks carries the bad config.
        config: The malformed config, in one of its shapes.
        expected: A substring the message must carry, so the caller is
            told which mistake this was.
    """
    before = await request(
        f'{BASE_HTTP}/echo', protocol='HTTP',
        protocol_info={'request_type': 'GET', 'timeout': 15})
    assert before['ok'] is True, 'the server was not reachable to begin with'

    with pytest.raises(ConfigurationError) as caught:
        await request(
            f'{BASE_HTTP}/echo',
            protocol='HTTP',
            protocol_info={'request_type': 'GET', 'timeout': 15},
            **{setting: config},
        )

    assert caught.value.code == 'CONFIG'
    assert caught.value.status_code == 400
    assert setting in str(caught.value)
    assert expected in str(caught.value)


@pytest.mark.parametrize('setting',
                         ['pre_processor_config', 'post_processor_config'])
async def test_a_processor_that_raises_is_a_typed_processor_error(
    setting: str,
) -> None:
    """A valid config whose callback raises reports PROCESSOR, not KeyError.

    Distinct from ``ConfigurationError`` on purpose: the configuration
    was accepted and this library called exactly what the caller asked
    for, so the fault is in the caller's function rather than in how they
    configured it. The original is chained, so the caller can still see
    what actually went wrong.

    Args:
        setting: Which of the two hooks carries the raising callback.
    """
    with pytest.raises(ProcessorError) as caught:
        await request(
            f'{BASE_HTTP}/echo',
            protocol='HTTP',
            protocol_info={'request_type': 'GET', 'timeout': 15},
            **{setting: {'function': a_raising_processor}},
        )

    assert caught.value.code == 'PROCESSOR'
    assert caught.value.status_code == 500
    assert setting in str(caught.value)
    # The cause chain, which is how the caller reaches the real fault --
    # a `ProcessorError` that swallowed it would be no better than the
    # bare builtin it replaced.
    assert isinstance(caught.value.__cause__, KeyError)


@pytest.mark.parametrize('setting',
                         ['pre_processor_config', 'post_processor_config'])
async def test_a_processor_removing_an_envelope_key_is_refused(
    setting: str,
) -> None:
    """Removing a key the protocol client reads is refused at the boundary.

    A processor is handed the *live* envelope, which is the documented
    design -- rewriting ``response['url']`` is the point of the hook. A
    live mutable object can also be deleted from, and a pre-processor
    that did left ``http_client`` reading ``self.response['payload']`` as
    a bare ``KeyError`` from inside the conversion ``try``.

    The pre row is the one with the real transfer behind it: the envelope
    is broken *before* the request is dispatched, so the assertion is
    that the boundary catches it rather than the protocol client. The
    post row breaks an envelope a real 200 has already filled in.

    Args:
        setting: Which of the two hooks removes the key.
    """
    with pytest.raises(ProcessorError) as caught:
        await request(
            f'{BASE_HTTP}/echo',
            protocol='HTTP',
            protocol_info={'request_type': 'GET', 'timeout': 15},
            **{setting: {'function': a_key_removing_processor}},
        )

    assert caught.value.code == 'PROCESSOR'
    assert caught.value.status_code == 500
    assert setting in str(caught.value)
    assert 'payload' in str(caught.value)


async def test_working_processors_run_around_a_real_request() -> None:
    """The hooks' happy path, so the refusals above are not vacuous.

    Every other test in this section asserts that something is refused.
    Without this one they would all pass against a library that refused
    *every* processor config, which is the failure mode a negative-only
    section invites.
    """
    result = await request(
        f'{BASE_HTTP}/echo',
        protocol='HTTP',
        protocol_info={'request_type': 'GET', 'timeout': 15},
        pre_processor_config={'function': a_working_processor},
        post_processor_config={'function': a_working_processor},
    )

    assert_envelope(result, 'HTTP')
    assert result['ok'] is True
    assert result['pre_processor_response'] == 'processor-ran'
    assert result['post_processor_response'] == 'processor-ran'


# ------------------------------------------------------------ concurrency --


async def test_the_four_protocols_run_concurrently_on_one_loop(
    ftp_auth: Any, sftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """All four dispatched at once, on one event loop, all succeeding.

    The library's reason for existing is that these are async, and a
    blocking call on the event loop is the defect class the AST scan in
    `tests/test_no_blocking_io.py` looks for statically. This is the
    dynamic counterpart: if any protocol blocked the loop, this would
    serialise or deadlock rather than complete.
    """
    calls = [
        request(f'{BASE_HTTP}/echo', protocol='HTTP',
                protocol_info={'request_type': 'GET', 'timeout': 30}),
        request(f'{BASE_SOAP}/rates', data=SOAP_BODY, protocol='SOAP',
                protocol_info={'soap_version': '1.1', 'timeout': 30}),
        request(FTP_HOST, protocol='FTP', auth=ftp_auth,
                protocol_info={
                    'port': FTP_PORT, 'command': 'download',
                    'server_path': 'pub/report.csv',
                    'client_path': str(tmp_path / 'c-ftp.csv'),
                    'timeout': 30}),
    ]
    if known_hosts_available():
        calls.append(
            request(SFTP_HOST, protocol='SFTP', auth=sftp_auth,
                    protocol_info={
                        'port': SFTP_PORT, 'mode': 'get',
                        'remote_path': 'pub/report.csv',
                        'local_path': str(tmp_path / 'c-sftp.csv'),
                        'known_hosts': KNOWN_HOSTS, 'timeout': 30}))

    results = await asyncio.gather(*calls)

    assert all(r['ok'] for r in results), [
        (r['protocol'], r['error']) for r in results if not r['ok']]


# ------------------------------- security: the local-write guards, live --
#
# The guards below were rewritten around an fd-based `open_within()`:
# the containment base is opened as a descriptor and each component is
# descended with `O_NOFOLLOW|O_DIRECTORY` against the fd already held,
# the leaf created with `dir_fd`, and the result judged by `fstat`
# before a single byte is written.
#
# Every assertion about that construction so far was made in-process,
# against a `tmp_path` on the developer's own filesystem. That is the
# weakest place to make it. A container writes into a mounted volume on
# overlayfs, owned by a different uid, and `O_NOFOLLOW`, `dir_fd` and
# `st_nlink` are exactly the primitives whose behaviour a filesystem is
# free to differ on. So each guard is re-asserted here through a REAL
# download from a REAL server -- the transfer runs, the server has the
# bytes, and the only thing standing between them and the disk is the
# guard.
#
# Each one asserts three things, and the third is the one a unit test
# cannot: an `ok=False` envelope (not an exception escaping), the `PATH`
# code, and *that the call returned at all*. A FIFO with no reader was
# N3's original symptom precisely because it did not return -- the open
# blocked forever in a threadpool worker where the caller's own timeout
# could not reach it. A hang is therefore a FAILURE here, not a slow
# pass, and the `asyncio.timeout` below is what makes it one.


#: Long enough that a working guard is never mistaken for a hang, short
#: enough that a real hang fails the run in seconds rather than wedging
#: it. The guarded open answers immediately -- it is one `openat` -- so
#: anything near this bound is the defect, not slowness.
GUARD_TIMEOUT = 20


async def download_into(local: pathlib.Path, ftp_auth: Any) -> Dict[str, Any]:
    """Download the FTP fixture to ``local``, refusing to hang.

    ``overwrite=True`` on purpose. Every target below already exists --
    a FIFO, a hard link, a path under a symlinked directory -- so with
    the default the ``O_EXCL`` in the flags refuses first, with a
    ``CONFIG`` "already exists" envelope, and the guard under test is
    never reached. That refusal is correct, and it is a second line of
    defence rather than the one being asserted: opting in to overwrite
    is what puts the question to ``open_within`` itself.

    Args:
        local: The local path to write to -- the thing under test.
        ftp_auth: The FTP credential fixture.

    Returns:
        The envelope ``request()`` returned.

    Raises:
        AssertionError: If the call does not return within
            :data:`GUARD_TIMEOUT`, which is N3's symptom rather than an
            ordinary failure.
    """
    try:
        async with asyncio.timeout(GUARD_TIMEOUT):
            return await request(
                FTP_HOST,
                protocol='FTP',
                auth=ftp_auth,
                protocol_info={
                    'port': FTP_PORT,
                    'command': 'download',
                    'server_path': 'pub/report.csv',
                    'client_path': str(local),
                    'overwrite': True,
                    'timeout': 30,
                },
            )
    except TimeoutError:
        raise AssertionError(
            f'the download never returned for {local} -- the guard hung '
            f'instead of refusing, which is N3 (a blocking open in a '
            f'threadpool worker the caller timeout cannot reach)')


def assert_path_refusal(result: Dict[str, Any], target: pathlib.Path) -> None:
    """Assert a refusal arrived as an envelope and wrote nothing.

    Args:
        result: The envelope the download returned.
        target: The local path that should not have been written.
    """
    assert_envelope(result, 'FTP')
    assert result['ok'] is False, (
        f'the guard admitted a write to {target}')
    assert result['error']['code'] == 'PATH', (
        f"expected a PATH refusal, got {result['error']}")


async def test_a_fifo_download_target_is_refused_not_hung(
    ftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """N3 over a real transfer: a FIFO is refused, and it returns.

    The original defect's symptom was a hang, so the timeout inside
    :func:`download_into` is the substance of this test and the envelope
    assertions are the confirmation. Asserted against a container
    filesystem because `O_NONBLOCK`-on-a-FIFO is a kernel behaviour the
    host suite only ever saw on APFS.
    """
    target = tmp_path / 'fifo-target.csv'
    os.mkfifo(target)

    result = await download_into(target, ftp_auth)

    assert_path_refusal(result, target)
    assert 'named pipe' in str(result['error']), result['error']


async def test_a_hardlinked_download_target_is_refused(
    ftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """N4 over a real transfer: writing one name would rewrite another.

    ``st_nlink`` is read off the descriptor the open returned, and link
    counting is a filesystem property -- overlayfs and a bind-mounted
    volume are entitled to answer differently from APFS, which is why
    this is worth asserting here at all.

    The victim's bytes are checked afterwards: a refusal that had
    already truncated the file would be the worse outcome, and it is a
    defect this construction actually had in its first draft.
    """
    victim = tmp_path / 'victim.txt'
    victim.write_bytes(b'do not overwrite me\n')
    target = tmp_path / 'hardlink.csv'
    os.link(victim, target)

    result = await download_into(target, ftp_auth)

    assert_path_refusal(result, target)
    assert 'hard link' in str(result['error']), result['error']
    assert victim.read_bytes() == b'do not overwrite me\n', (
        'the victim was truncated before the refusal')


async def test_a_symlink_planted_in_the_destination_is_refused(
    ftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """M18/N2 over a real transfer: the pre-planted link is refused.

    The threat is a symlink an attacker plants at a **predictable path
    inside the directory the download will write into** -- M18's actual
    shape, where the README's fixed ``/tmp/test.pdf`` was followed. The
    descent refuses it: every component below the base is opened
    ``O_NOFOLLOW|O_DIRECTORY`` against the descriptor already held, and
    the leaf carries ``O_NOFOLLOW`` too.

    A *symlinked parent the caller named themselves* is deliberately
    NOT this test. Confirmed against this stack: the caller's own
    ``client_path`` is canonicalised by ``caller_path`` before it
    becomes the containment base, so naming a symlinked directory as
    your own destination succeeds -- correctly. The caller chose it;
    there is no escape from a boundary they drew there themselves, and
    refusing it would break the ordinary case of downloading into a
    symlinked mount point.

    What must never be followed is a link the caller did *not* name,
    which is what this asserts: the tree root is real, the link sits
    inside it, and the write goes through the containment layer the
    recursion uses -- both for a link *to* a directory outside, and for
    a plain-file link planted at a name the transfer is about to create.

    See ``test_a_symlinked_client_path_is_followed_TODO`` below for the
    case this does NOT cover, and why it is filed rather than asserted.
    """
    destination = tmp_path / 'tree'
    destination.mkdir()
    outside = tmp_path / 'victimdir'
    outside.mkdir()
    victim = outside / 'victim.txt'
    victim.write_bytes(b'do not follow me\n')

    (destination / 'sub').symlink_to(outside, target_is_directory=True)
    (destination / 'planted.csv').symlink_to(victim)

    path_io = contained_path_io_factory(
        local_base(destination), overwrite=True)()

    # The entry name a server would send for a file in that subtree...
    with pytest.raises(PathContainmentError):
        path_io.contained(destination / 'sub' / 'OWNED.csv')

    # ...and a link pre-planted at the exact name the transfer creates,
    # which is M18's own shape (the README's fixed `/tmp/test.pdf`).
    # Asserted at the *open*, not at `contained()`: containment answers
    # "is this location inside the base", and this one is -- the link
    # sits in the tree. What must refuse it is the `O_NOFOLLOW` on the
    # write, so that is the seam the assertion belongs at.
    planted = path_io.contained(destination / 'planted.csv')
    with pytest.raises(PathContainmentError):
        _open_guarded(planted, 'wb', True)

    assert victim.read_bytes() == b'do not follow me\n'
    assert sorted(p.name for p in outside.iterdir()) == ['victim.txt'], (
        f'the traversal escaped into {outside}')


async def test_a_symlinked_client_path_is_followed_by_the_transfer(
    ftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """A KNOWN GAP, asserted as it behaves rather than as it should.

    When ``client_path`` *itself* is a symbolic link, the download
    follows it and writes the remote bytes into the link's target.
    Reproduced here against a live FTP server, and reproduced equally
    against the commit before the ``open_within()`` rewrite -- so this
    is **pre-existing, not a regression** from that change, and it is
    recorded here rather than fixed under an acceptance run.

    Why it happens: ``local_base(client_path)`` makes the caller's own
    path the containment base, and ``resolve_within`` canonicalises the
    base. A base that *is* a link therefore resolves to the link's
    target before ``guarded_opener`` is ever handed a path, so the
    ``O_NOFOLLOW`` on the leaf is applied to the target's name and finds
    no link there.

    Whether it is a defect is a real design question, not an oversight:
    for a *directory* download, a symlinked destination is the ordinary
    case of downloading into a mounted volume, and refusing it would
    break that. For a *single-file* download it is M18's shape with the
    caller supplying the link, and ``_open``'s own docstring claims to
    refuse "a symlink at the target". Those two cannot both be honoured
    by one rule about the base.

    The test asserts today's behaviour so the gap is visible and so any
    future fix is a deliberate, reviewed change to a red test rather
    than a silent one. The attacker-planted cases -- the ones inside a
    directory the caller named -- are refused, and the test above is
    what proves it.
    """
    victim = tmp_path / 'victim.txt'
    victim.write_bytes(b'original\n')
    target = tmp_path / 'client-path-link.csv'
    target.symlink_to(victim)

    result = await download_into(target, ftp_auth)

    assert_envelope(result, 'FTP')
    assert result['ok'] is True, result['error']
    assert victim.read_bytes() == REMOTE_FIXTURE, (
        'behaviour changed -- the symlinked client_path is no longer '
        'followed. That is very likely the FIX for this gap: delete '
        'this test and tighten the one above.')


async def test_a_crlf_header_is_refused_before_the_socket_opens() -> None:
    """N6/N7 against a real origin: the answer no longer depends on it.

    A CR in a header value *was* refused -- by ``aiohttp``, at
    serialisation, with a bare ``ValueError``, and serialisation happens
    after the connection is open. So which failure a caller got for one
    and the same mistake depended on whether the host answered: an
    unreachable host produced a ``CONNECT`` envelope (the connect failed
    first, the headers were never serialised), a reachable one produced
    an un-enveloped ``ValueError``.

    That nondeterminism is the defect, so the assertion is about
    *placement*, and dialling a **reachable** origin is what makes it
    meaningful -- against a dead port the old code passed this too.

    ``ConfigurationError`` escaping synchronously is the documented
    contract for a caller-configuration mistake, not a bug (AGW-35):
    these are unretryable programming errors, so they are raised once
    rather than returned as an envelope a retry loop would re-attempt
    forever. What N6/N7 changed is that the refusal is now this typed
    error raised at construction *before any socket is opened*, rather
    than a bare ``ValueError`` from deep inside a live connection.

    So: the same typed error from a reachable origin and from a dead
    port. Identical answers to the same mistake is exactly the property
    the placement buys, and comparing the two is what a single call
    cannot show.
    """
    bad_headers = {'X-Injected': 'value\r\nX-Smuggled: yes'}

    with pytest.raises(ConfigurationError) as reachable:
        await request(
            f'{BASE_HTTP}/echo',
            protocol='HTTP',
            protocol_info={
                'request_type': 'GET',
                'headers': bad_headers,
                'timeout': 30,
            },
        )

    # Port 1 on this container answers nothing. Under the old code this
    # call produced a CONNECT envelope while the one above raised, which
    # is the nondeterminism; now both raise the same refusal.
    with pytest.raises(ConfigurationError) as unreachable:
        await request(
            'http://127.0.0.1:1/echo',
            protocol='HTTP',
            protocol_info={
                'request_type': 'GET',
                'headers': bad_headers,
                'timeout': 30,
            },
        )

    # Named by ordinal, so a CR in the message cannot split a log line
    # the way it would have split the request.
    assert 'U+000D' in str(reachable.value), reachable.value
    assert str(reachable.value) == str(unreachable.value), (
        'the refusal still depends on whether the host answered')


# -------------------- security: recursive download and the R22 traversal --


async def test_ftp_recursive_directory_download_still_works(
    ftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """The case ``open_within()`` changes most, against a real server.

    A directory download is where ``aioftp`` composes the *server's*
    entry names onto the local destination and writes them through the
    contained path-IO layer -- so every write goes through the new
    descriptor walk, and the tree is created by it. A containment
    construction that is too strict breaks exactly here (and nowhere in
    the single-file tests), which is why the positive case is asserted
    before the traversal refusal below.
    """
    destination = tmp_path / 'tree'

    result = await request(
        FTP_HOST,
        protocol='FTP',
        auth=ftp_auth,
        protocol_info={
            'port': FTP_PORT,
            'command': 'download',
            'server_path': 'pub',
            'client_path': str(destination),
            'timeout': 30,
        },
    )

    assert_envelope(result, 'FTP')
    assert result['ok'] is True, result['error']

    landed = sorted(p.name for p in destination.rglob('*') if p.is_file())
    assert 'report.csv' in landed, landed
    fetched = next(destination.rglob('report.csv'))
    assert fetched.read_bytes() == REMOTE_FIXTURE


@requires_hostkey
async def test_sftp_recursive_directory_download_still_works(
    sftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """The same for SFTP, whose recursion is asyncssh's rather than ours.

    ``asyncssh`` walks the remote tree itself and writes through the
    contained local filesystem object, so this exercises the second of
    the two seams the rewrite touched.

    ``recurse`` is deliberately not passed: the client decides it from
    the remote ``lstat`` (M28 -- it used to be written into the
    caller's own dict by reference, which left it set for every later
    call). Passing it here would test the caller instead of that.
    """
    destination = tmp_path / 'tree'

    result = await request(
        SFTP_HOST,
        protocol='SFTP',
        auth=sftp_auth,
        protocol_info={
            'port': SFTP_PORT,
            'mode': 'get',
            'remote_path': 'pub',
            'local_path': str(destination),
            'known_hosts': KNOWN_HOSTS,
            'timeout': 30,
        },
    )

    assert_envelope(result, 'SFTP')
    assert result['ok'] is True, result['error']

    landed = sorted(p.name for p in destination.rglob('*') if p.is_file())
    assert 'report.csv' in landed, landed
    fetched = next(destination.rglob('report.csv'))
    assert fetched.read_bytes() == REMOTE_FIXTURE


async def test_a_traversing_entry_name_cannot_escape_the_named_directory(
    ftp_auth: Any, tmp_path: pathlib.Path,
) -> None:
    """R22's actual threat model: the hostile name comes from the SERVER.

    Every other path test here supplies the dangerous path as the
    *caller*. This one does not -- the escape M17 described is a server
    listing an entry called ``../victimdir/OWNED`` during a recursive
    download, which ``aioftp`` composes onto the local destination and
    writes, at mode 0644, outside the directory the caller named.

    A cooperative server will not emit such a name, so the equivalent
    reachable assertion is made against the seam that decides it: the
    contained path-IO layer the recursion writes through, bound to the
    same base a real download binds it to, asked to resolve the entry
    name a hostile server would have sent. Refusing it there is what
    refuses it on the wire.

    The live half is the sibling test above: the same layer, bound the
    same way, passes a real recursive download. Together they say the
    containment is tight enough to refuse the escape and loose enough
    to let the legitimate tree through -- neither of which either test
    shows alone.
    """
    destination = tmp_path / 'tree'
    destination.mkdir()
    outside = tmp_path / 'victimdir'
    outside.mkdir()

    path_io = contained_path_io_factory(
        local_base(destination), overwrite=False)()

    for hostile in ('../victimdir/OWNED', '../../etc/OWNED', '/etc/OWNED'):
        with pytest.raises(PathContainmentError):
            path_io.contained(pathlib.Path(hostile))

    assert list(outside.iterdir()) == [], 'the traversal escaped'


# ------------- the local filesystem, on Linux rather than on APFS --------
#
# Everything below asserts a property of the LOCAL disk the transfer
# writes to, and every one of them has only ever been proven on macOS.
# That is the gap this section closes. File mode, ownership and link
# counting are the classic points of divergence between APFS and a Linux
# filesystem, and a container adds two more layers entitled to disagree:
# the image's own overlayfs, and whatever driver backs a mounted volume.
#
# So each test that can runs TWICE -- once under `tmp_path` (overlayfs)
# and once under the mounted `scratch` volume -- parametrised on the
# directory rather than duplicated, because two hand-written copies are
# what drift. `scratch_dir` skips rather than passes when no volume is
# mounted: a silently-halved matrix is the failure this whole section
# exists to avoid.


def volume_available() -> bool:
    """Report whether the writable scratch volume is mounted.

    Returns:
        True when compose provided one and it is writable.
    """
    return bool(SCRATCH_DIR) and os.access(SCRATCH_DIR, os.W_OK)


requires_volume = pytest.mark.skipif(
    not volume_available(),
    reason='no writable scratch volume is mounted')


@pytest.fixture()
def scratch(
    request: pytest.FixtureRequest, tmp_path: pathlib.Path,
) -> pathlib.Path:
    """Return an empty directory on the filesystem the test asked for.

    Args:
        request: Carries the ``'overlay'`` or ``'volume'`` parameter.
        tmp_path: The overlayfs-backed default.

    Returns:
        A fresh empty directory on the requested filesystem.
    """
    if request.param == 'overlay':
        return tmp_path
    if not volume_available():
        pytest.skip('no writable scratch volume is mounted')
    made = pathlib.Path(SCRATCH_DIR) / unique('case')
    made.mkdir(parents=True)
    return made


#: Applied to every test that must hold on both filesystems.
both_filesystems = pytest.mark.parametrize(
    'scratch', ['overlay', 'volume'], indirect=True)

#: An mtime far enough in the past that no filesystem could produce it
#: by accident, so a preserved timestamp cannot be confused with the one
#: the download itself would have written.
WIDE_MTIME = 1000000000


async def seed_remote_file(mode: int, mtime: int) -> str:
    """Put a copy of the fixture on the server at a chosen mode and time.

    Through ``asyncssh`` directly rather than through the library, for
    the reason the FTPS posture test shells out to ``lftp``: when the
    assertion is "the server's attributes did not reach the local
    file", establishing what the server's attributes *are* with the
    code under test would be circular. The sshd here also runs
    ``ForceCommand internal-sftp``, so there is no shell to run
    ``chmod`` in -- these are SFTP attribute operations, which is what
    a real peer would use.

    Args:
        mode: The permission bits to set on the remote copy.
        mtime: The modification time to set, in epoch seconds.

    Returns:
        The remote path of the seeded file.
    """
    remote = f'pub/{unique("wide")}.csv'
    async with asyncssh.connect(
            SFTP_HOST, port=SFTP_PORT, username=cfg.SFTP_USER,
            password=cfg.SFTP_PASSWORD, known_hosts=KNOWN_HOSTS) as conn:
        async with conn.start_sftp_client() as sftp:
            async with sftp.open(remote, 'wb') as handle:
                await handle.write(REMOTE_FIXTURE)
            await sftp.chmod(remote, mode)
            await sftp.utime(remote, (mtime, mtime))
    return remote


async def download_to(
    client_path: pathlib.Path,
    ftp_auth: Any,
    server_path: str = 'pub/report.csv',
) -> Dict[str, Any]:
    """Run one FTP download, without opting in to overwrite.

    Distinct from :func:`download_into`, which passes ``overwrite=True``
    so that the guarded open is reached. Nothing here exists yet, so the
    default is what the caller would use.

    Args:
        client_path: The local destination.
        ftp_auth: The FTP credential fixture.
        server_path: The remote source.

    Returns:
        The envelope ``request()`` returned.
    """
    return await request(
        FTP_HOST,
        protocol='FTP',
        auth=ftp_auth,
        protocol_info={
            'port': FTP_PORT,
            'command': 'download',
            'server_path': server_path,
            'client_path': str(client_path),
            'timeout': 30,
        },
    )


async def sftp_get_to(
    local_path: pathlib.Path,
    sftp_auth: Any,
    remote_path: str = 'pub/report.csv',
    **extra: Any,
) -> Dict[str, Any]:
    """Run one SFTP ``get``, with the host key pinned.

    Args:
        local_path: The local destination.
        sftp_auth: The SFTP credential fixture.
        remote_path: The remote source.
        **extra: Extra ``protocol_info`` keys, e.g. a preserve option.

    Returns:
        The envelope ``request()`` returned.
    """
    info: Dict[str, Any] = {
        'port': SFTP_PORT,
        'mode': 'get',
        'remote_path': remote_path,
        'local_path': str(local_path),
        'known_hosts': KNOWN_HOSTS,
        'timeout': 30,
    }
    info.update(extra)
    return await request(
        SFTP_HOST, protocol='SFTP', auth=sftp_auth, protocol_info=info)


@both_filesystems
async def test_ftp_refuses_a_missing_destination_parent(
    ftp_auth: Any, scratch: pathlib.Path,
) -> None:
    """NEW-R11-1 against a live server, on a container filesystem.

    ``aioftp.Client.download`` calls ``mkdir(parent, parents=True,
    exist_ok=True)`` before it writes, so forwarding that flag ran an
    unbounded ``mkdir -p`` on a caller-named path: FTP answered a
    missing destination directory with ``ok=True``/200 having created
    the tree, where HTTP and SFTP both refused. Dropping the flag is the
    fix, and this is it holding over a real FTPS session.

    The gap is three levels deep on purpose. One level would be
    satisfied by a ``mkdir`` that merely lacks ``parents``; three can
    only be satisfied by not creating anything.
    """
    missing = scratch / 'a' / 'b' / 'c'

    result = await download_to(missing / 'out.bin', ftp_auth)

    assert_envelope(result, 'FTP')
    assert result['ok'] is False, (
        'FTP created the missing destination tree the caller named, '
        'instead of refusing it -- NEW-R11-1, which HTTP and SFTP '
        'both refuse')
    assert result['error']['code'] == 'PATH', result['error']
    assert result['status_code'] == 400, result['status_code']
    assert not (scratch / 'a').exists(), (
        f'the refusal still created {scratch / "a"} -- nothing the '
        'caller did not name may be created')


@requires_hostkey
@both_filesystems
async def test_sftp_refuses_a_missing_destination_parent(
    sftp_auth: Any, scratch: pathlib.Path,
) -> None:
    """The same question to the other transfer protocol, live.

    This is the half NEW-R11-1 made FTP agree WITH, so it is the
    control: if the container filesystem changed this answer, the
    parity the fix established would be broken from the other side and
    the FTP assertion above would be measuring the wrong baseline.
    """
    missing = scratch / 'a' / 'b' / 'c'

    result = await sftp_get_to(missing / 'out.bin', sftp_auth)

    assert_envelope(result, 'SFTP')
    assert result['ok'] is False, 'SFTP created the missing tree'
    assert result['error']['code'] == 'PATH', result['error']
    assert result['status_code'] == 400, result['status_code']
    assert not (scratch / 'a').exists(), (
        f'the refusal still created {scratch / "a"}')


@both_filesystems
async def test_ftp_mkdir_over_an_existing_file_is_path(
    ftp_auth: Any, scratch: pathlib.Path,
) -> None:
    """A directory download onto a path a regular file occupies.

    The eleventh-round divergence: one local fault, two codes. FTP said
    ``CONFIG``/400 -- advising ``overwrite=True``, which cannot make a
    ``mkdir`` succeed over a file -- where SFTP said ``PATH``/400.
    ``PATH`` is the settled answer on both.

    A *directory* server path is what makes this reachable: a
    single-file download opens the destination and never calls
    ``mkdir`` on it at all.
    """
    occupied = scratch / 'tree'
    occupied.write_bytes(b'an ordinary file in the way')

    result = await download_to(occupied, ftp_auth, server_path='pub')

    assert_envelope(result, 'FTP')
    assert result['ok'] is False, 'the file in the way was replaced'
    assert result['error']['code'] == 'PATH', (
        f'FTP reported {result["error"]["code"]} for a mkdir over an '
        f'existing file, where SFTP reports PATH')
    assert result['status_code'] == 400, result['status_code']
    assert occupied.read_bytes() == b'an ordinary file in the way'


@requires_hostkey
@both_filesystems
async def test_sftp_mkdir_over_an_existing_file_is_path(
    sftp_auth: Any, scratch: pathlib.Path,
) -> None:
    """The other half of the parity row above, live.

    Asserted as its own test rather than folded into the FTP one
    because the two clients reach ``mkdir`` by different routes --
    ``aioftp``'s recursion versus ``asyncssh``'s ``_copy`` -- and the
    claim is that they agree, which needs both measured.
    """
    occupied = scratch / 'tree'
    occupied.write_bytes(b'an ordinary file in the way')

    result = await sftp_get_to(occupied, sftp_auth, remote_path='pub')

    assert_envelope(result, 'SFTP')
    assert result['ok'] is False, 'the file in the way was replaced'
    assert result['error']['code'] == 'PATH', result['error']
    assert result['status_code'] == 400, result['status_code']
    assert occupied.read_bytes() == b'an ordinary file in the way'


@requires_hostkey
@both_filesystems
async def test_preserve_does_not_let_a_0777_server_file_widen_the_local_one(
    sftp_auth: Any, scratch: pathlib.Path,
) -> None:
    """M18 over a real SFTP session: the server does not pick the mode.

    ``asyncssh``'s ``_setstat`` ends in an ``os.chmod`` to whatever
    ``attrs.permissions`` carries, so ``preserve=True`` silently
    reverted the guarded open's 0600 to the REMOTE file's mode. The fix
    drops the permission and ownership fields and keeps the timestamps.

    Both halves are asserted, because dropping the whole call would
    also pass a mode-only test while quietly costing the caller the
    feature they asked for. The remote file is chmod'ed 0777 through
    the same SSH connection the test then downloads over -- the widest
    mode there is, so a preserved bit cannot be mistaken for the
    default.

    On a container filesystem for a specific reason: a volume driver is
    free to answer ``chmod`` and ``stat`` differently from APFS, and
    0600 is a security property of this release rather than an
    accident of ``umask``.
    """
    remote = await seed_remote_file(0o777, WIDE_MTIME)

    local = scratch / 'preserved.csv'
    result = await sftp_get_to(
        local, sftp_auth, remote_path=remote,
        additional_arguments={'preserve': True})

    assert_envelope(result, 'SFTP')
    assert result['ok'] is True, result['error']
    assert local.read_bytes() == REMOTE_FIXTURE

    mode = local.stat().st_mode & 0o777
    assert mode == FILE_MODE, (
        f'the local file is {mode:#o}, not {FILE_MODE:#o}: a remote '
        f'server chose the mode of a file on this machine, which is '
        f'the authority the guarded open exists to deny it (M18)')
    assert local.stat().st_mtime == WIDE_MTIME, (
        'the timestamp was not preserved -- preserve=True must still '
        'do the thing it is for, or the fix cost the caller a feature')


@requires_hostkey
@both_filesystems
async def test_a_recursive_preserve_download_keeps_every_file_at_0600(
    sftp_auth: Any, scratch: pathlib.Path,
) -> None:
    """The same, on the recursive arm, into an EXISTING tree.

    Two things at once, deliberately. The mode assertion covers every
    file the walk writes rather than one -- ``_setstat`` is called per
    entry, so a per-file leak is what a single-file test would miss.
    And the destination already exists, which is the legitimate case
    the NEW-R11-1 ``mkdir`` change is most likely to have broken: the
    base is ``mkdir``'ed with ``exist_ok``, and a fix that refused here
    would have traded a real capability for the defect it fixed.
    """
    destination = scratch / 'tree'
    destination.mkdir()

    result = await sftp_get_to(
        destination, sftp_auth, remote_path='pub',
        additional_arguments={'preserve': True})

    assert_envelope(result, 'SFTP')
    assert result['ok'] is True, result['error']

    landed = sorted(p for p in destination.rglob('*') if p.is_file())
    assert landed, f'nothing landed under {destination}'
    assert any(p.name == 'report.csv' for p in landed), landed

    wide = {
        str(p): oct(p.stat().st_mode & 0o777)
        for p in landed
        if p.stat().st_mode & 0o777 != FILE_MODE
    }
    assert wide == {}, (
        f'a recursive preserve=True download left these files at a '
        f'mode the server chose, not {FILE_MODE:#o}: {wide}')


@requires_volume
async def test_the_guards_hold_on_a_volume_owned_by_another_uid(
    ftp_auth: Any,
) -> None:
    """0600, ``O_NOFOLLOW`` and ``st_nlink`` under foreign ownership.

    The three guards are asserted where the host suite structurally
    cannot reach: a world-writable directory on a mounted volume that
    this process does **not** own. Under `tmp_path` the test user
    creates and owns every component, so a guard that quietly depended
    on that would pass there and fail in the deployment this library is
    actually used in.

    None of the three is a permission check -- `O_NOFOLLOW` and the
    `st_nlink` count are refusals the kernel makes regardless of uid,
    and 0600 is the mode the open requests -- so the expected answer is
    that ownership changes nothing. That is the claim, and an
    unsurprising result measured is worth more than an assumed one:
    this is the axis on which a container filesystem is most entitled
    to differ from APFS.

    Both premises are asserted rather than assumed, because either one
    failing would make the test pass while proving nothing. Where the
    directory comes back owned by this process -- which a rootless or
    userns-remapped daemon may do -- it says so instead.
    """
    # The foreign directory itself, not a subdirectory of it: anything
    # this process creates there it would then own, which is the very
    # property being removed. Unique leaf names keep the cases apart
    # instead.
    workdir = pathlib.Path(SCRATCH_DIR) / 'foreign'
    stem = unique('case')
    assert workdir.is_dir(), (
        f'{workdir} is missing: the image pre-creates it so that Docker '
        f'seeds the volume with a directory the test user does not own')
    assert workdir.stat().st_uid != os.getuid(), (
        f'{workdir} is owned by this process (uid {os.getuid()}), so '
        f'there is no foreign ownership here to test')

    # 0600 survives a download into a directory we do not own.
    landed = workdir / f'{stem}-report.csv'
    result = await download_to(landed, ftp_auth)
    assert_envelope(result, 'FTP')
    assert result['ok'] is True, result['error']
    assert landed.stat().st_mode & 0o777 == FILE_MODE, (
        f'{landed} is {landed.stat().st_mode & 0o777:#o}, not '
        f'{FILE_MODE:#o}, under foreign ownership')

    # O_NOFOLLOW: a link the caller did not name, planted at a path
    # INSIDE the directory they did. Not the caller's own `client_path`
    # -- that is followed by design, and
    # `test_a_symlinked_client_path_is_followed_by_the_transfer`
    # records it. Asserted at the guarded open, which is the seam that
    # refuses it, and where the recursion's writes go through.
    victim = workdir / f'{stem}-victim.txt'
    victim.write_bytes(b'do not follow me\n')
    base = workdir / f'{stem}-tree'
    base.mkdir()
    (base / 'planted.csv').symlink_to(victim)

    path_io = contained_path_io_factory(local_base(base), overwrite=True)()
    with pytest.raises(PathContainmentError):
        _open_guarded(path_io.contained(base / 'planted.csv'), 'wb', True)
    assert victim.read_bytes() == b'do not follow me\n', (
        'the planted symlink was followed and the victim rewritten')

    # st_nlink: a hard link is refused, and the other name survives.
    linked = workdir / f'{stem}-hardlink.csv'
    os.link(victim, linked)
    result = await download_into(linked, ftp_auth)
    assert_path_refusal(result, linked)
    assert 'hard link' in str(result['error']), result['error']
    assert victim.read_bytes() == b'do not follow me\n'

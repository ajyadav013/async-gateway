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
import conftest as cfg

import pytest

from async_gateway.async_gateway import request
from async_gateway.utils.constants import MAX_MULTIPART_DEPTH
from async_gateway.utils.exceptions import ConfigurationError

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

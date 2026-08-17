"""Tests for the FTP client (spec R15, Step 8).

Three defects land together here, because fixing the first exposes the
other two on a live socket (FI-1): FTP raised ``UnboundLocalError`` on
both branches and had therefore never executed (C6), it defaulted to
plain FTP where HTTP defaults to TLS (H1), and its TLS branch read a key
``get_ssl_config`` returns only for a client certificate, so
``verify_ssl=True`` without one handed ``aioftp`` a ``None`` and opened
the session in plaintext (M2). M3 -- the unconditional ``stat`` that
turned a completed deletion into a reported failure -- and the FTP half
of H10 -- a transfer nothing bounded -- ride in the same commit.

Every test names the acceptance criterion it proves and what regresses if
it is deleted. No test opens a connection to anything but loopback, and
the one that does exists because a fail-closed TLS claim cannot be proven
by a double that never handshakes.
"""

import logging
import socket
import ssl
from pathlib import Path
from typing import Any, Optional, Text

import aioftp

from aiohttp import BasicAuth

from async_gateway.async_gateway import request
from async_gateway.helpers.internal.filters_helper import get_ssl_config
from async_gateway.logic import ftp_client
from async_gateway.logic.ftp_client import FTPRequest, tls_context_for
from async_gateway.utils.envelope import GatewayResponse, new_envelope
from async_gateway.utils.exceptions import TlsError

import pytest

from tests.fixtures.ftp import (
    FILE_STATS,
    RecordingFTPClient,
    certificate_pair,
    install_ftp_double,
    plaintext_ftp_server,
)

AUTH = BasicAuth('user', 'password')

# Short, because every test that uses it either never reaches a socket or
# reaches one on loopback that answers immediately.
CALL_TIMEOUT = 5

SERVER_PATH = '/remote/f'


async def ftp_call(
    url: Text = 'host',
    **info: Any,
) -> GatewayResponse:
    """Drive one FTP request through the public entry point.

    Going through ``request()`` rather than the client object is
    deliberate for all but the identity test below: the criteria are
    about what a *caller* gets back, and the entry point's single
    conversion point is the code that turns a raised error into the
    envelope being asserted on.

    Args:
        url: The host to dispatch to.
        info: ``protocol_info`` overrides; ``command``, ``server_path``
            and ``timeout`` have defaults.

    Returns:
        The envelope ``request()`` returned.
    """
    return await request(
        url,
        protocol='FTP',
        auth=AUTH,
        protocol_info={
            'command': 'download',
            'server_path': SERVER_PATH,
            'timeout': CALL_TIMEOUT,
            **info,
        },
    )


def ftp_request(**info: Any) -> tuple[FTPRequest, GatewayResponse]:
    """Build an ``FTPRequest`` around an envelope, without dispatching.

    Args:
        info: ``protocol_info`` overrides.

    Returns:
        The constructed client and the envelope it was handed, so a test
        can assert the client returns that same object.
    """
    envelope = new_envelope(url='host', protocol='FTP', payload={})
    client = FTPRequest(
        'host',
        AUTH,
        envelope,
        info={
            'command': 'download',
            'server_path': SERVER_PATH,
            'timeout': CALL_TIMEOUT,
            **info,
        },
        redact_params=frozenset(),
    )
    return client, envelope


# --- R15-AC1: the client executes at all -----------------------------------


async def test_r15_ac1_an_ftp_download_returns_a_populated_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FTP runs for the first time: C6's read-before-assignment is gone.

    Both branches of the old client evaluated ``ssl=verify_ssl`` where no
    such local had ever been assigned, so *every* FTP call -- FTPS and
    plain alike -- died with ``UnboundLocalError`` before a socket was
    opened, and the blanket handler reported it as a ``999`` with
    ``error=None``. Remove the initialisation and this test sees an
    envelope no finaliser ever closed, on a call that reached no server.
    """
    double = install_ftp_double(monkeypatch)

    result = await ftp_call()

    assert result['ok'] is True
    assert result['error'] is None
    assert result['status_code'] == 200
    assert ('download', (SERVER_PATH,)) in double.client.calls


# --- R15-AC2: the TLS configuration follows the error contract -------------


async def test_r15_ac2_a_broken_system_ca_store_becomes_a_tls_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one TLS-configuration failure that is still a *transport* fault.

    The SSL block used to sit *above* the ``try``, so its failures were
    the one class of failure that escaped the client as a bare
    ``ssl.SSLError`` -- a caller who had written the documented
    ``result['ok']`` check got an exception instead of an envelope. Move
    the block back out of the ``try`` and this test raises rather than
    asserting.

    **What reaches this handler changed with R23**, and the failure
    driven here is chosen to match. ``get_ssl_config`` now converts every
    way the *caller's own* certificate files can fail -- missing,
    unreadable, mismatched, not a PEM, passphrase-protected -- into a
    ``ConfigurationError``, which is not an ``OSError`` and so passes
    straight through this ``except`` to the 400 envelope the test below
    asserts. The single ``OSError`` still able to arrive is the *system*
    CA bundle failing to read: it is raised by
    ``ssl.create_default_context``, which sits deliberately outside
    ``build_client_ssl_context``'s own ``try`` because a broken trust
    store is the environment's problem and not the caller's. That is a
    genuine transport-level fault, so it is ``TLS``/502 and retriable.

    Driven by breaking the real factory rather than by replacing
    ``get_ssl_config`` wholesale, so the ``OSError`` travels the actual
    path -- out of the thread, out of the helper, into this handler --
    instead of being posted directly to the seam under test.
    """
    def unreadable_trust_store(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        """Fail the way a missing system CA bundle fails.

        Args:
            args: The purpose, ignored.
            kwargs: Unused.

        Returns:
            Never; this always raises.

        Raises:
            OSError: Always.
        """
        raise OSError(2, 'No such file or directory')

    install_ftp_double(monkeypatch)
    monkeypatch.setattr(
        'async_gateway.helpers.internal.filters_helper.'
        'ssl.create_default_context',
        unreadable_trust_store)

    with certificate_pair() as pair:
        result = await ftp_call(certificate=pair)

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'TLS'
    assert result['error']['message']
    assert result['status_code'] == 502


async def test_r15_ac2_an_unloadable_certificate_becomes_a_config_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The half of the contract R23 moved, pinned so the move is visible.

    **This is a deliberate, reviewed change to FTP's live error
    contract** (ticket AGW-17). Before R23, a certificate path that did
    not exist reached ``load_cert_chain`` as a bare
    ``FileNotFoundError`` -- an ``OSError`` -- and the handler above
    reported it as ``TLS``/502. It is now ``CONFIG``/400.

    400 is the better answer and the reasoning is the same one the
    one-element-tuple case already used: nothing was attempted, no packet
    left the process, and no retry can turn a path that does not exist
    into one that does. 502 additionally puts the failure in the
    retriable transport family, so a caller with a typo in their
    configuration would have had it retried. It also matches what the
    HTTP client already reports for the same mistake, which the split
    contract did not.

    Asserted on the real filesystem rather than through a double: the
    whole point is that ``get_ssl_config`` classifies this itself now, so
    substituting it would test the assertion instead of the behaviour.
    """
    install_ftp_double(monkeypatch)
    missing = tmp_path / 'never-created.pem'

    result = await ftp_call(certificate=(str(missing), str(missing)))

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'CONFIG'
    assert result['status_code'] == 400
    assert str(missing) in result['error']['message']


# --- R15-AC3 / AC4: FTPS by default, and never a downgrade -----------------


async def test_r15_ac3_a_call_with_no_verify_ssl_key_negotiates_ftps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default is TLS, as it already was for HTTP (H1).

    FTP defaulted ``verify_ssl`` to ``False`` while the HTTP client
    defaulted it to ``True``, so the protocol whose credentials travel as
    literal ``USER``/``PASS`` lines was the one that sent them in the
    clear unless the caller knew to ask otherwise. Flip the default back
    and the context asserted here is never built.
    """
    double = install_ftp_double(monkeypatch)

    result = await ftp_call()

    context = double.kwargs['ssl']
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert result['ok'] is True


@pytest.mark.parametrize(
    'info',
    [
        pytest.param({}, id='no-verify_ssl-key'),
        pytest.param({'verify_ssl': True}, id='explicit-true'),
    ],
)
async def test_r15_ac4_the_ssl_value_is_never_none_when_verification_is_on(
    monkeypatch: pytest.MonkeyPatch,
    info: dict[Text, Any],
) -> None:
    """M2, at the exact seam: ``ssl=None`` is a silent downgrade.

    ``get_ssl_config`` returns its ``ssl_context`` key *only* when a
    certificate was supplied, so the old client's
    ``ssl_context.get('ssl_context')`` evaluated to ``None`` for every
    caller who asked for verification without one -- and ``ssl=None`` is
    how ``aioftp`` is told to speak plaintext. The assertion is on the
    value handed to the transport rather than on the call succeeding,
    because a downgraded call succeeds; that is what makes it silent.
    """
    double = install_ftp_double(monkeypatch)

    await ftp_call(**info)

    assert double.kwargs['ssl'] is not None
    assert isinstance(double.kwargs['ssl'], ssl.SSLContext)


async def test_r15_ac4_a_server_that_does_not_offer_tls_fails_closed(
) -> None:
    """The whole point, end to end and against a real handshake.

    A stub can be handed any ``ssl`` value and still report success, so
    the fail-closed claim is proven against a loopback server that
    answers with a plaintext FTP banner where a ServerHello belongs --
    the same thing a real FTP server with no TLS support does. The call
    must fail with ``TLS``; completing it would mean the credentials went
    out in the clear to a server that never proved who it was.
    """
    async with plaintext_ftp_server() as port:
        result = await ftp_call('127.0.0.1', port=port)

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'TLS'


@pytest.mark.parametrize(
    'config',
    [
        pytest.param({}, id='empty'),
        pytest.param({'ssl': True}, id='flag-true'),
        pytest.param({'ssl': False}, id='flag-false'),
        pytest.param({'ssl_context': None}, id='context-none'),
        pytest.param({'other': 'key'}, id='unrecognised-key'),
    ],
)
def test_tls_context_for_never_answers_with_none_or_a_bare_flag(
    config: dict[Text, Any],
) -> None:
    """The guarantee is the return type, not a run of lucky inputs.

    Every shape ``get_ssl_config`` can answer with -- today's and
    whatever R23 rewrites it into -- resolves to a real context here, so
    there is no value of its return for which the client can hand
    ``aioftp`` a ``None`` or a bare bool. Relax this to "pass through
    whatever was found" and M2 is reinstated for the next shape that
    turns up.
    """
    assert isinstance(tls_context_for(config), ssl.SSLContext)


async def test_tls_context_for_keeps_the_callers_own_certificate_context(
) -> None:
    """Failing closed may not mean discarding the caller's own mTLS.

    The fallback exists for the *absence* of a context; a caller who
    supplied a client certificate must get theirs, or the fix for a
    silent downgrade would become a silent loss of client authentication.

    Driven from the **real** ``get_ssl_config`` output rather than a
    hand-built ``ssl.create_default_context()``. That substitution is
    what let the certificate branch stay broken through a green suite:
    the helper never produces a default context, so a test that passed
    one in asserted pass-through against a shape the code under test
    never sees.

    The key is ``'ssl'``, and **only** ``'ssl'``. R23-AC3 moved the
    certificate branch off the deprecated ``ssl_context=`` key that this
    test used to read, so pinning the new one is the point rather than
    incidental. Accepting either would prove nothing:
    :data:`~async_gateway.logic.ftp_client.TLS_CONFIG_KEYS` still reads
    both, so a test that tolerated the old key would keep passing if the
    migration were reverted -- which is exactly the regression it is here
    to catch.
    """
    with certificate_pair() as pair:
        config = await get_ssl_config(pair, True)

    assert set(config) == {'ssl'}
    assert isinstance(config['ssl'], ssl.SSLContext)
    assert tls_context_for(config) is config['ssl']


def test_tls_context_for_refuses_a_context_that_verifies_no_peer(
) -> None:
    """The second, independent guard on the seam that failed open thrice.

    ``ssl.Purpose.CLIENT_AUTH`` is the *server* purpose, and a context
    built with it verifies nobody: ``verify_mode`` is ``CERT_NONE`` and
    ``check_hostname`` is off. Returning one is indistinguishable, at the
    socket, from the ``ssl=None`` downgrade M2 named -- the handshake
    completes against any peer that offers any certificate. Passing a
    context through on the strength of its *type* alone is therefore not
    enough; its verification settings are checked too. Delete this guard
    and the only thing standing between a caller and an unauthenticated
    FTPS session is one keyword in another module.
    """
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    assert context.verify_mode == ssl.CERT_NONE

    with pytest.raises(TlsError):
        tls_context_for({'ssl_context': context})


async def test_r15_ac4_a_client_certificate_does_not_buy_an_unverified_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2's other half, end to end: the branch a certificate takes.

    The no-certificate branch handed ``aioftp`` a ``None``; the
    certificate branch handed it a context built for
    ``ssl.Purpose.CLIENT_AUTH``, which verifies nothing. Both negotiate
    FTPS against an entirely unauthenticated peer, so both are the same
    defect -- and fixing only the first relocates it rather than closing
    it. Asserted on the value the transport was handed, because a
    machine-in-the-middled session succeeds; that is what makes it
    silent.
    """
    double = install_ftp_double(monkeypatch)

    with certificate_pair() as pair:
        result = await ftp_call(certificate=pair)

    context = double.kwargs['ssl']
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode != ssl.CERT_NONE
    assert context.check_hostname is True
    assert result['ok'] is True


async def test_r15_ac4_a_context_that_verifies_nothing_never_reaches_aioftp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth, measured at the envelope rather than the unit.

    ``get_ssl_config`` is owned by another requirement and is still being
    rewritten, so the client does not take its output on trust. This
    drives the exact regression -- the helper handing back a
    ``CLIENT_AUTH`` context again -- through the public entry point, and
    requires it to end in a ``TLS`` envelope rather than a completed
    plaintext-equivalent session. The unit test above proves the guard
    exists; this proves nothing downstream swallows it.
    """
    async def answer_with_an_unverifying_context(
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        """Return the context the pre-fix helper returned.

        Args:
            args: The certificate pair and flag, unused.
            kwargs: Unused.

        Returns:
            A config carrying a context that verifies no peer.
        """
        return {
            'ssl_context': ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        }

    double = install_ftp_double(monkeypatch)
    monkeypatch.setattr(
        ftp_client, 'get_ssl_config', answer_with_an_unverifying_context)

    result = await ftp_call(certificate=('cert.pem', 'key.pem'))

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'TLS'
    assert result['status_code'] == 502
    assert not double.calls


# --- R15-AC5: an explicit opt-out is honoured, and says so -----------------


async def test_r15_ac5_an_explicit_false_is_honoured_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plaintext stays reachable, but only by name and never quietly.

    Turning the default on is worth nothing if a caller cannot get the
    old behaviour back for a server that has no TLS at all, so
    ``verify_ssl=False`` is honoured exactly. It is also the one
    configuration that puts a password on the wire, so it is logged at
    ``warning`` naming the risk -- an operator reading the log is the
    only person who can notice it.
    """
    caplog.set_level(logging.DEBUG, logger='async_gateway')
    double = install_ftp_double(monkeypatch)

    result = await ftp_call(verify_ssl=False)

    assert double.kwargs['ssl'] is False
    assert result['ok'] is True

    warnings = [
        record for record in caplog.records
        if record.name.startswith('async_gateway')
        and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert 'verify_ssl' in warnings[0].getMessage()
    assert 'clear' in warnings[0].getMessage()


# --- R15-AC6: a completed operation is reported as one ---------------------


@pytest.mark.parametrize(
    'command, client_path, stats_read',
    [
        pytest.param('download', '/local/f', True, id='download'),
        pytest.param('upload', '/local/f', True, id='upload'),
        pytest.param('remove', None, False, id='remove'),
        pytest.param('remove_file', None, False, id='remove_file'),
        pytest.param('remove_directory', None, False, id='remove_directory'),
    ],
)
async def test_r15_ac6_every_command_reports_the_success_it_achieved(
    monkeypatch: pytest.MonkeyPatch,
    command: Text,
    client_path: Optional[Text],
    stats_read: bool,
) -> None:
    """M3: ``stat`` no longer runs after the path it would read is gone.

    The old client read back ``stat(server_path)`` whatever the command
    was, so a *successful* deletion ended in a ``stat`` on a path that no
    longer existed, the raise was swallowed as a ``999``, and a caller
    following the documented retry policy re-attempted a delete that had
    already happened. On the removing rows the double's ``stat`` is
    therefore armed with that same server answer: making the read
    unconditional again fails them, where a stub that cheerfully
    answered anything would let them pass.

    ``remove_file`` and ``remove_directory`` are rows and not an
    afterthought: ``aioftp.Client`` exposes all three, every one of them
    is reachable through the client's ``getattr`` lookup, and a
    single-name exemption list closed M3 for ``remove`` while leaving it
    live under two other names.
    """
    client = RecordingFTPClient(
        stat_error=None if stats_read else aioftp.StatusCodeError(
            '2xx', '550', ['No such file']))
    double = install_ftp_double(monkeypatch, client=client)

    result = await ftp_call(command=command, client_path=client_path)

    assert result['ok'] is True
    assert result['error'] is None
    assert result['status_code'] == 200
    assert result['protocol_details'] == {
        'command': command,
        'server_path': SERVER_PATH,
        'client_path': client_path,
        'file_stats': dict(FILE_STATS) if stats_read else None,
    }
    assert ('stat' in dict(double.client.calls)) is stats_read


async def test_r15_ac6_the_command_is_the_one_the_caller_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A success envelope means the operation ran, not merely that TLS did.

    Asserted separately from the envelope because the call log is the
    only evidence that ``remove`` reached the server at all: an
    implementation that skipped the command and reported ``ok=True``
    would satisfy every assertion in the row above except this one.
    """
    double = install_ftp_double(monkeypatch)

    await ftp_call(command='remove')

    assert double.client.calls == [('remove', (SERVER_PATH,))]


@pytest.mark.parametrize(
    'certificate',
    [
        pytest.param(('cert.pem',), id='pair-with-one-element'),
        pytest.param(5, id='not-a-sequence-at-all'),
    ],
)
async def test_a_malformed_certificate_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    certificate: Any,
) -> None:
    """A caller's typo is the caller's fault, and says so.

    The pre-fix ``get_ssl_config`` indexed ``certificate[0]`` and ``[1]``
    unguarded, so a one-element tuple raised ``IndexError`` and a
    non-sequence raised ``TypeError``. Neither is an ``OSError``, so both
    escaped the client's TLS handler raw -- a caller who wrote the
    documented ``result['ok']`` check got an exception instead of an
    envelope, and one that named no useful cause. ``CONFIG``/400 rather
    than ``TLS``/502 because nothing was attempted and no retry can help.

    The envelope is unchanged; **where** it is produced is not. This used
    to be an ``except (IndexError, TypeError)`` arm inside
    ``_tls_value``, translating the two raw exceptions after the fact.
    R23 made ``normalised_certificate`` reject these shapes itself, with
    a ``ConfigurationError`` that names the offending *type*, so the arm
    became unreachable and was deleted rather than left as a branch no
    input can enter (AGW-17). This test is what proves the deletion was
    safe: it asserts the caller-visible contract, not the mechanism, so
    it held across the move.
    """
    install_ftp_double(monkeypatch)

    result = await ftp_call(certificate=certificate)

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'CONFIG'
    assert result['status_code'] == 400
    assert 'certificate' in result['error']['message']


# --- R15-AC7: the transfer is bounded too ----------------------------------


async def test_r15_ac7_both_the_connect_and_the_transfer_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H10-ftp: only the connect was bounded, so a stalled transfer hung.

    The breaker cannot help with a hang -- a socket that never answers
    raises nothing for it to count -- so an unbounded transfer is a
    coroutine that never returns and a caller that never gets an
    envelope. Drop either keyword and this test says which one.
    """
    double = install_ftp_double(monkeypatch)

    await ftp_call(timeout=3)

    assert double.kwargs['connection_timeout'] == 3
    assert double.kwargs['socket_timeout'] == 3


# --- R15-AC9: the envelope it was handed, not a shape of its own -----------


async def test_r15_ac9_the_client_returns_the_envelope_it_was_handed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E1's other half: the client fills one object and returns it.

    ``return True`` is what the old client did, which is why the entry
    point could not rely on what came back and why FTP was free to invent
    its own success shape. Identity, not equality: a client that built an
    equal dict of its own would drop everything the entry point had
    already written into the envelope.
    """
    install_ftp_double(monkeypatch)
    client, envelope = ftp_request()

    returned = await client.handle_request()

    assert returned is envelope
    assert returned['ok'] is True


# --- failure translation ---------------------------------------------------


@pytest.mark.parametrize(
    'error, code, status',
    [
        pytest.param(
            ConnectionRefusedError('refused'), 'CONNECT', 502, id='refused'),
        pytest.param(
            ssl.SSLError('wrong version number'), 'TLS', 502, id='tls'),
        pytest.param(
            socket.gaierror('name not known'), 'DNS', 502, id='dns'),
        pytest.param(
            TimeoutError('too slow'), 'TIMEOUT', 504, id='timeout'),
        pytest.param(
            aioftp.StatusCodeError('2xx', '550', ['No such file']),
            'FTP_STATUS', 550, id='ftp-status'),
    ],
)
async def test_a_transport_failure_maps_to_its_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    code: Text,
    status: int,
) -> None:
    """Every failure carries a code and a status a consumer can read.

    The fabricated ``999`` this replaces told a caller nothing about
    whether to retry, and the swallowed exception object in ``text``
    could not even be serialised. The FTP reply code row is the reason
    the mapping reads ``received_codes`` rather than defaulting: a
    ``remove`` of a path that is not there must report the server's own
    ``550``, not a number this library invented.
    """
    install_ftp_double(monkeypatch, connect_error=error)

    result = await ftp_call()

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == code
    assert result['status_code'] == status
    assert result['error']['message']


async def test_a_library_bug_propagates_instead_of_becoming_an_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule the blanket ``except Exception`` broke for this package.

    A ``KeyError`` raised inside the transfer is this library's bug, not
    a failed request, and reporting it as one is what hid most of an
    audit for the life of the package. Reinstate any blanket handler here
    and this test stops raising.
    """
    client = RecordingFTPClient(command_error=KeyError('a library bug'))
    install_ftp_double(monkeypatch, client=client)

    with pytest.raises(KeyError):
        await ftp_call()


# --- the verb allowlist (R21-AC1,2 · R15-AC8) -------------------------------


@pytest.mark.parametrize(
    'command',
    [
        pytest.param('close', id='session-attribute'),
        pytest.param('list', id='real-but-not-admitted'),
        pytest.param('downlaod', id='typo'),
        pytest.param('', id='empty'),
        pytest.param(None, id='absent'),
        pytest.param(42, id='not-a-string'),
    ],
)
async def test_an_unadmitted_command_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
    command: Any,
) -> None:
    """R15-AC8: an unknown command is CONFIG, not a fabricated status.

    The rows are the four shapes the unbounded ``getattr`` failed open
    on, and they failed differently, which is why they are all here.
    ``close`` and ``list`` are real attributes of a connected
    ``aioftp.Client``, so the lookup *succeeded* and the caller reached a
    method this library never meant to expose; the typo raised an
    ``AttributeError``; ``None`` died on ``None.lower()`` before the
    lookup. All four are now one answer.

    The message must name the allowed set. A rejection that says only
    "bad command" leaves a caller guessing at the spelling, which is the
    difference between a fail-closed guardrail and an obstacle.
    """
    install_ftp_double(monkeypatch)

    result = await ftp_call(command=command)

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'CONFIG'
    assert result['status_code'] == 400
    assert repr(command) in result['error']['message']
    for allowed in ('download', 'upload', 'remove'):
        assert allowed in result['error']['message']


async def test_an_unadmitted_command_never_reaches_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: the refusal happens before the attribute is read.

    The property this criterion is really about. Asserting only that the
    envelope says CONFIG would pass just as well against a check placed
    *after* ``getattr`` resolved the caller's name -- and the whole
    finding (M25) is that resolving it at all is the capability surface.
    ``RecordingFTPClient`` records every command it is asked for, so an
    empty log is the proof that nothing was addressed on it.
    """
    client = RecordingFTPClient()
    install_ftp_double(monkeypatch, client=client)

    result = await ftp_call(command='close')

    assert result['ok'] is False
    assert client.calls == []


@pytest.mark.parametrize(
    'command',
    [
        pytest.param(' download ', id='surrounding-whitespace'),
        pytest.param('DOWNLOAD', id='upper'),
        pytest.param('Download', id='mixed'),
    ],
)
async def test_an_admitted_command_is_matched_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
    command: Text,
) -> None:
    """Normalisation matches the protocol name's, per R21's edge cases.

    One reading of a verb across the library, so a caller who writes
    ``'DOWNLOAD'`` -- as the README's own prose spells FTP commands --
    is not refused by the allowlist for a difference in case that every
    other lookup here already ignores.
    """
    client = RecordingFTPClient()
    install_ftp_double(monkeypatch, client=client)

    result = await ftp_call(command=command, client_path='/tmp/f')

    assert result['ok'] is True
    assert [name for name, _ in client.calls] == ['download', 'stat']

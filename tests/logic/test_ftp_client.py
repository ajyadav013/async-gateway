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

import asyncio
import inspect
import logging
import socket
import ssl
import threading
from pathlib import Path
from typing import Any, Optional, Text

import aioftp

from aiohttp import BasicAuth

from failsafe import FailsafeError, RetriesExhausted

import pytest

from async_gateway.async_gateway import request
from async_gateway.helpers.internal.filters_helper import get_ssl_config
from async_gateway.logic import ftp_client
from async_gateway.logic.ftp_client import (
    FTPRequest, reply_status, tls_context_for, transport_error_for)
from async_gateway.utils.envelope import GatewayResponse, new_envelope
from async_gateway.utils.exceptions import (
    ConfigurationError, FtpStatusError, TlsError, TransportError)

from tests.fixtures.ftp import (
    FILE_STATS,
    HARMLESS_CONTENT,
    HostileFTPServer,
    LOCAL_CONTENT,
    REMOTE_CONTENT,
    RecordingFTPClient,
    TransferringFTPClient,
    certificate_pair,
    install_ftp_double,
    plaintext_ftp_server,
)

AUTH = BasicAuth('user', 'password')

# Short, because every test that uses it either never reaches a socket or
# reaches one on loopback that answers immediately.
CALL_TIMEOUT = 5

SERVER_PATH = '/remote/f'

# The local half of a transfer, for the operand-order rows. Distinct
# from SERVER_PATH in every component, so a transposition cannot be
# mistaken for a coincidence.
CLIENT_PATH = '/local/g'


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


# --- AGW-37: the CA-bundle read is off the event loop ----------------------


async def test_agw37_the_default_context_is_built_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thread identity is the assertion, because nothing else is.

    ``tls_context_for``'s no-certificate fallback calls
    ``ssl.create_default_context()``, which reads the whole system CA
    bundle -- 194 certificates -- synchronously. It was awaited straight
    from ``_tls_value`` with no executor, so that read landed on the
    event loop once per FTPS session, blocking every other in-flight
    request for its duration (AGW-37).

    ``asyncio.to_thread`` is what moves it, and *no observable output
    changes* when it is removed: the context returned is identical
    either way. The only thing that differs is which thread the work ran
    on, so that is what is measured -- the same shape as
    ``test_agw36_the_certificate_is_loaded_off_the_event_loop``, and for
    the same reason.

    The AST scan in ``tests/test_no_blocking_io.py`` structurally cannot
    see this: ``tls_context_for`` is a *module-level plain* ``def``, so
    the scan never searches it, and the coroutine that calls it contains
    no banned call of its own. That scan used to carry a comment naming
    this very site as a known live defect; this test replaces it,
    because a guard that documents a hole reads as coverage while
    catching nothing.

    Args:
        monkeypatch: Installs the thread-recording wrapper for the
            duration of this test only.
    """
    ran_on: list[int] = []
    real_resolver = ftp_client.tls_context_for

    def record_thread(ssl_config: Any) -> ssl.SSLContext:
        """Record the running thread, then resolve the context normally.

        Args:
            ssl_config: Forwarded unchanged.

        Returns:
            The context the real resolver produced.
        """
        ran_on.append(threading.get_ident())
        return real_resolver(ssl_config)

    monkeypatch.setattr(
        'async_gateway.logic.ftp_client.tls_context_for', record_thread)
    client, _ = ftp_request()

    context = await client._tls_value()

    assert isinstance(context, ssl.SSLContext)
    assert len(ran_on) == 1
    assert ran_on[0] != threading.get_ident()


async def test_agw37_the_loop_keeps_running_while_the_context_is_built(
) -> None:
    """The property the thread identity exists to buy.

    Thread identity is a proxy; this is the thing itself. A task
    scheduled alongside the build must get its turn *while* the build is
    still running -- which is exactly what a blocking CA-bundle read on
    the loop denies it.

    The flag is read **before** the companion is awaited, and that
    ordering is the whole test. Awaiting it first would guarantee it had
    run, and the assertion would then hold no matter what ``_tls_value``
    did. Without the thread this coroutine reaches no suspension point
    at all on the no-certificate branch -- ``get_ssl_config`` returns
    ``{'ssl': True}`` without awaiting anything -- so it runs start to
    finish without yielding, and the companion is still unstarted when
    the context comes back.
    """
    progressed = asyncio.Event()

    async def other_work() -> None:
        """Set the flag as soon as the loop gives this task a turn.

        Returns:
            None.
        """
        progressed.set()

    client, _ = ftp_request()
    companion = asyncio.create_task(other_work())
    try:
        await client._tls_value()

        assert progressed.is_set()
    finally:
        await companion


def test_agw37_the_blocking_resolver_is_a_plain_def() -> None:
    """The placement the executor contract depends on, pinned.

    ``asyncio.to_thread`` on a coroutine function does not run it -- it
    returns the coroutine object from the thread, unawaited, and the
    blocking work never happens at all. Rewriting this as an ``async
    def`` would also put a banned ``ssl.`` call back inside a coroutine,
    which ``tests/test_no_blocking_io.py`` fails on; the two checks
    approach the same regression from opposite sides.
    """
    assert not inspect.iscoroutinefunction(tls_context_for)


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


@pytest.mark.parametrize(
    'received, expected',
    [
        pytest.param(('550',), 550, id='one-numeric-code'),
        pytest.param(('220-', '550'), 550, id='a-continuation-then-the-code'),
        pytest.param(('220-', 'x'), None, id='nothing-that-reads-as-a-number'),
        pytest.param((), None, id='no-code-at-all'),
    ],
)
def test_r28_a_reply_code_is_reported_only_when_the_server_gave_one(
    received: tuple[Text, ...],
    expected: Optional[int],
) -> None:
    """R28: the whole of ``reply_status``, including the answer ``None``.

    A unit test on the module function rather than an envelope, because
    the interesting rows cannot be told apart from outside: every one of
    them that answers ``None`` reaches the caller as the same 500
    ``FtpStatusError`` takes by default, so an envelope assertion cannot
    distinguish "the server said nothing numeric" from "the server said
    500". The row below drives that default end to end; these rows pin
    which received codes produce it.

    ``aioftp`` hands this a tuple of ``Code``, which is a ``str``
    subclass and is *not* guaranteed to be digits: a multi-line reply
    puts the continuation marker ``220-`` in it, and a server answering
    with anything else at all puts that in it verbatim. So the search
    keeps going past a non-numeric entry -- the second row is the one
    that proves it does -- and gives up rather than inventing a code when
    it runs out. Replace the loop with ``received_codes[0]`` and the
    second row reports ``None`` for a server that plainly said 550;
    delete the trailing ``return None`` and rows three and four raise
    ``TypeError`` from the envelope's status lookup instead.
    """
    error = aioftp.StatusCodeError(
        aioftp.Code('2xx'), tuple(aioftp.Code(code) for code in received),
        ['whatever the server said'])

    assert reply_status(error) == expected


async def test_r28_an_unreadable_reply_code_falls_back_to_the_class_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller still gets a status, and it is not a fabricated one.

    The companion to the unit rows above, and the reason answering
    ``None`` is safe: ``FtpStatusError`` takes its documented default
    from ``utils/status_map.py`` when no reply code is supplied, so a
    server whose codes this library cannot read is reported as
    ``FTP_STATUS``/500 rather than as a number invented at the call site
    -- which is the ``999`` this whole error contract replaces. Make
    ``reply_status`` return something other than None on this input and
    the caller is told the server said a code it never said.
    """
    unreadable = aioftp.StatusCodeError(
        aioftp.Code('2xx'), aioftp.Code('x'), ['not a numeric reply'])
    install_ftp_double(monkeypatch, connect_error=unreadable)

    result = await ftp_call()

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'FTP_STATUS'
    assert result['error']['type'] == FtpStatusError.__name__
    assert result['status_code'] == 500


@pytest.mark.parametrize(
    'wrapper',
    [
        pytest.param(RetriesExhausted, id='retries-exhausted'),
        pytest.param(FailsafeError, id='the-bare-wrapper'),
    ],
)
def test_r28_a_causeless_failsafe_wrapper_is_a_transport_error(
    wrapper: type[FailsafeError],
) -> None:
    """R28: the arm that keeps ``transport_error_for`` total.

    A direct call on the module function, which the rest of this module
    avoids, and it is deliberate: ``handle_request`` cannot reach this
    arm. The only ``FailsafeError`` the breaker raises with no
    ``__cause__`` is a ``CircuitOpen`` on a destination that has not
    failed in *this* call, and that class is caught one ``except`` higher
    and becomes a ``CircuitOpenError`` -- so through the public surface
    every wrapper that arrives here carries a cause.

    Worth having rather than pragma-ing away, for the same reason the
    guard exists. Without it a causeless wrapper falls through to
    ``raise cause from None`` and raises ``TypeError: exceptions must
    derive from BaseException`` on ``None``, from inside the classifier
    -- so the caller of a library whose one job is typed errors gets an
    untyped one, raised from the least legible place in this module. The
    message is asserted non-empty because ``str(RetriesExhausted())`` is
    ``''``, and an empty error message reads to a consumer as success.
    """
    error = transport_error_for(wrapper())

    assert isinstance(error, TransportError)
    assert type(error) is TransportError
    assert error.code == 'TRANSPORT'
    assert error.status_code == 502
    assert str(error)


async def test_r28_an_open_circuit_is_reported_as_an_open_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The breaker's own refusal is not a transport failure.

    ``CircuitOpen`` is a ``FailsafeError`` with nothing under it the
    generic unwrap can classify, so without its own clause it would land
    as a ``CONNECT``/502 -- retriable -- and the caller would retry into
    a circuit that is open precisely to stop them. 503 says "not now".

    The breaker is configured to open on the first failure and to retry
    twice, so the second attempt of the *same* call finds the circuit
    already open: that is what makes this reachable without a second
    ``request()``, and what makes the assertion about this library's
    dispatch rather than about test ordering.
    """
    client = RecordingFTPClient(command_error=ConnectionResetError('reset'))
    install_ftp_double(monkeypatch, client=client)

    result = await ftp_call(
        circuit_breaker_config={
            'maximum_failures': 1,
            'retry_config': {'name': 'delay', 'allowed_retries': 2,
                             'delay': 0},
        })

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'CIRCUIT_OPEN'
    assert result['status_code'] == 503


def test_r28_a_failure_of_no_family_is_re_raised_by_the_classifier() -> None:
    """The classifier's last arm: not every exception is this protocol's.

    ``transport_error_for`` is total over the families it names and
    deliberately *not* total over exceptions in general. A ``KeyError``
    or a ``TypeError`` arriving here is this library's own bug, and the
    one-conversion-point rule says a bug propagates rather than becoming
    an ``ok=False`` envelope that hides it.

    A direct call, because reaching this arm through ``handle_request``
    now requires a failure the dispatch clause catches and the
    classifier does not name -- and the two are derived from one table
    (:data:`~async_gateway.logic.ftp_client.TRANSPORT_FAULTS`), which is
    the property that makes the pair impossible to write by accident.
    Before NEW-R10-1 this arm was reached incidentally, by a
    ``PathContainmentError`` that ``aioftp`` had wrapped; that failure
    now aborts the retry loop and propagates as itself, so the arm needs
    a test of its own rather than a passer-by.
    """
    bug = KeyError('a library bug, not a transport failure')

    with pytest.raises(KeyError) as raised:
        transport_error_for(bug)

    assert raised.value is bug


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


# --- AGW-33: the operand order, per verb -----------------------------------


async def test_agw33_an_upload_sends_the_local_file_the_caller_named(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The silent-wrong-file defect, asserted on content, not arguments.

    ``_run_command`` passed ``(server_path, client_path)`` to whichever
    command was named. ``aioftp`` takes ``(source, destination)`` on
    both verbs and the verbs point in **opposite** directions, so one
    shared order is right for ``download`` and inverted for ``upload``:
    it read ``server_path`` off the *local* disk and wrote it to
    ``client_path`` on the *server*.

    This is the mirrored-tree deployment that makes it High rather than
    a clean failure: a local file exists at the remote path, so the
    transposed call finds something, succeeds, and reports ``ok=True``
    having sent the wrong file to the wrong place.

    The assertion is on the **bytes that reached the server**. A
    recording double cannot make it (the standing S11 learning) and
    neither can a signature-faithful one -- both operands are path-like
    positionals, so a transposition binds cleanly (proven in S12).
    """
    local = tmp_path / 'local' / 'report.pdf'
    local.parent.mkdir()
    local.write_bytes(LOCAL_CONTENT)
    # The mirrored tree. Without a local file at the remote path the
    # inverted call fails honestly and the dangerous case never appears.
    mirrored = tmp_path / 'mirror' / 'report.pdf'
    mirrored.parent.mkdir()
    mirrored.write_bytes(REMOTE_CONTENT)

    server = TransferringFTPClient()
    install_ftp_double(monkeypatch, client=server)

    result = await ftp_call(
        command='upload',
        server_path=str(mirrored),
        client_path=str(local))

    assert result['ok'] is True
    # Transposed, the server receives the old remote copy under a
    # success envelope, which is the whole of AGW-33.
    assert server.uploaded == LOCAL_CONTENT
    assert server.uploaded != REMOTE_CONTENT
    assert server.upload_destination == str(mirrored)


async def test_agw33_an_upload_whose_local_source_is_absent_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No mirrored tree, so the inverted call fails instead of lying.

    The common half of the defect, and the reason it went unnoticed:
    without a local file at the remote path, the transposed upload
    raises and the caller sees an honest ``ok=False``. Pinned so that
    the failure stays a *reported* failure rather than becoming a
    success envelope over an empty transfer.
    """
    local = tmp_path / 'report.pdf'
    local.write_bytes(LOCAL_CONTENT)
    server = TransferringFTPClient()
    install_ftp_double(monkeypatch, client=server)

    result = await ftp_call(
        command='upload',
        server_path=str(tmp_path / 'not-here' / 'report.pdf'),
        client_path=str(local))

    assert result['ok'] is True
    assert server.uploaded == LOCAL_CONTENT


@pytest.mark.parametrize(
    'command, expected',
    [
        pytest.param(
            'download', (SERVER_PATH, CLIENT_PATH), id='download'),
        pytest.param('upload', (CLIENT_PATH, SERVER_PATH), id='upload'),
    ],
)
async def test_agw33_each_verb_passes_its_own_operand_order(
    monkeypatch: pytest.MonkeyPatch,
    command: Text,
    expected: tuple[Text, Text],
) -> None:
    """Both directions pinned, because a table needs two rows to be one.

    The regression the fix could introduce is swapping one shared order
    for the *other* shared order: that fixes ``upload`` and silently
    breaks ``download``. Only asserting both makes the per-verb table
    demonstrably a table.
    """
    double = install_ftp_double(monkeypatch)

    await ftp_call(command=command, client_path=CLIENT_PATH)

    assert double.client.calls[0] == (command, expected)


async def test_agw33_the_readback_stat_targets_the_remote_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After an upload, ``stat`` reads the path that was written.

    The compounding half: the transposed upload never wrote
    ``server_path``, and the ``stat`` afterwards read it anyway -- so
    ``file_stats`` described a file the call had not touched. It stays
    the remote path for every verb, which is also what keeps it correct
    for a download.
    """
    double = install_ftp_double(monkeypatch)

    await ftp_call(command='upload', client_path=CLIENT_PATH)

    assert ('stat', (SERVER_PATH,)) in double.client.calls


# --- R22: containment on the recursive download ----------------------------


async def test_r22_a_hostile_listing_cannot_write_outside_client_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """M17's actual vector, against the real ``aioftp.Client.download``.

    The test whose absence would let R22 look closed while the protocol
    it was written for stayed undefended. Guarding this library's own
    write sites does nothing here, because **none of the filename
    arithmetic happens in this library**: ``aioftp`` takes the entry
    names from the listing, computes
    ``destination / name.relative_to(source)``, recurses, and writes
    through its own ``path_io``.

    So only the **wire** is faked -- the four coroutines that would talk
    to a socket. Every path decision is aioftp's real code, which is
    what makes this escape a real one: measured before the fix, a server
    listing ``/pub/data/../victimdir/OWNED`` under ``/pub/data`` wrote
    that file outside the target at mode 0644.

    Asserted on the **filesystem** and not only on the error: a refusal
    alone would also be satisfied by a client that wrote the file first
    and complained afterwards.
    """
    target = tmp_path / 'downloads'
    target.mkdir()
    victim = tmp_path / 'victimdir'
    victim.mkdir()
    install_ftp_double(
        monkeypatch,
        client=HostileFTPServer.with_escaping_entry(victim.name))

    result = await ftp_call(
        command='download',
        server_path=HostileFTPServer.ROOT,
        client_path=str(target / 'tree'))

    assert result['ok'] is False
    assert result['error']['code'] == 'PATH'
    assert result['status_code'] == 400
    assert list(victim.iterdir()) == [], (
        'a server-supplied entry name escaped the download directory')


async def test_r22_an_ordinary_listing_still_downloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The control: containment does not break a well-behaved transfer.

    Without it, refusing every recursive download would satisfy the row
    above. The bytes are asserted rather than the file's existence, so a
    client that created an empty file fails this too.
    """
    target = tmp_path / 'downloads'
    target.mkdir()
    install_ftp_double(monkeypatch, client=HostileFTPServer.harmless_only())

    result = await ftp_call(
        command='download',
        server_path=HostileFTPServer.ROOT,
        client_path=str(target / 'tree'))

    assert result['ok'] is True
    assert (target / 'tree' / 'harmless.txt').read_bytes() == (
        HARMLESS_CONTENT)


async def test_r22_a_downloaded_file_is_not_world_readable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """M18 on the FTP path: 0600, not ``open``'s umask-dependent default.

    The escape measured before the fix landed its file at 0644. Fixing
    only *where* the bytes go would leave every legitimately downloaded
    file world-readable, which on the shared host M18 describes is the
    other half of the same finding.
    """
    target = tmp_path / 'downloads'
    target.mkdir()
    install_ftp_double(monkeypatch, client=HostileFTPServer.harmless_only())

    await ftp_call(
        command='download',
        server_path=HostileFTPServer.ROOT,
        client_path=str(target / 'tree'))

    mode = (target / 'tree' / 'harmless.txt').stat().st_mode & 0o777
    assert mode == 0o600


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
    'server_path',
    [
        pytest.param(None, id='absent'),
        pytest.param('', id='empty'),
        pytest.param(42, id='not-a-string'),
    ],
)
async def test_r28_a_missing_server_path_is_refused_before_the_connect(
    monkeypatch: pytest.MonkeyPatch,
    server_path: Any,
) -> None:
    """``TypeError`` from inside ``PurePosixPath`` is not a diagnosis.

    ``server_path`` has no default, and every command acts on it. Absent,
    it used to reach ``aioftp.Client.stat`` -- whose signature is ``str |
    PurePosixPath`` -- as ``None`` and raise ``TypeError: argument should
    be a str or an os.PathLike object`` from inside ``PurePosixPath``. A
    ``TypeError`` belongs to no transport family, so it escaped
    ``request()`` un-enveloped as a library bug rather than being
    reported as the caller's configuration error it is. Surfaced by
    removing mypy's ``ignore_errors``, which is what made the honest
    ``Optional[Text]`` annotation visible.

    The empty and non-string rows are here because ``server_path`` comes
    out of a config file as often as out of a literal: ``''`` is a
    perfectly good string that names no path, and a path written as a
    number is the same mistake one keystroke away.

    The empty session log is the load-bearing half. An envelope
    assertion alone would hold just as well for a check placed after the
    connect -- and the point is that nothing is opened, no credentials
    are put on the wire, for a call that cannot run.
    """
    double = install_ftp_double(monkeypatch)

    result = await ftp_call(server_path=server_path)

    assert result['ok'] is False
    assert result['error'] is not None
    assert result['error']['code'] == 'CONFIG'
    assert result['error']['type'] == ConfigurationError.__name__
    assert result['status_code'] == 400
    assert 'server_path' in result['error']['message']
    assert repr(server_path) in result['error']['message']
    assert double.calls == []


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


def test_n7_an_invalid_command_is_configuration_not_transport() -> None:
    """N7: ``aioftp.InvalidCommand`` is the caller's config, not the wire.

    ``aioftp`` refuses a CR or an LF in a command line itself, which is
    correct and is not the defect. The defect was the *shape* of the
    refusal: ``InvalidCommand`` is an ``AIOFTPException`` **and** a
    ``ValueError``, it matched no entry in ``TRANSPORT_ERRORS``, and so
    it escaped ``request()`` as a bare third-party exception -- where
    the library's contract is that only an ``AsyncGatewayError`` may.

    Asserted as ``CONFIG``/400 rather than merely as "some typed error",
    because the classification is the whole decision. Adding
    ``ValueError`` to ``TRANSPORT_ERRORS`` would also have made this
    typed, and would have been wrong twice: it declares a caller's own
    CR/LF a transport failure -- carrying a retry recommendation, on a
    value no retry can fix -- and it swallows every genuine
    ``ValueError`` from this library's own code as a failed network
    call, which is the blindness the one-conversion-point rule exists to
    prevent.
    """
    error = transport_error_for(
        aioftp.errors.InvalidCommand('Command must not contain CR/LF'))

    assert isinstance(error, ConfigurationError)
    assert error.code == 'CONFIG'
    assert error.status_code == 400
    assert 'CR/LF' in str(error)


def test_n7_a_status_code_error_still_outranks_the_config_arm() -> None:
    """The ordering the N7 arm was inserted above must still hold.

    ``StatusCodeError`` is checked first and stays first: a real reply
    code from the server is a protocol outcome, and misreporting one as
    the caller's configuration would tell them to fix a value that is
    fine. This pins the arm's *position*, which a reader moving it for
    tidiness would otherwise silently change.
    """
    error = transport_error_for(
        aioftp.StatusCodeError('550', '550 Not found', 'info'))

    assert isinstance(error, FtpStatusError)
    assert error.code == 'FTP_STATUS'

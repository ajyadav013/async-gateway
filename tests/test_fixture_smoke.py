"""Smoke tests for the test tooling itself (spec R2, Step 1).

These prove the three things every later story in the v1.0.0 release stands
on: a plain sync test still runs under ``asyncio_mode = auto``, an
*unmarked* ``async def`` test runs, and the loopback recording server
round-trips a real request whose method, path, query, headers and raw body
bytes can be asserted from the server side.
"""

import aiohttp

from tests.fixtures.http_server import RecordingHTTPServer


def test_r2_sync_test_still_runs_under_asyncio_auto_mode() -> None:
    """A plain sync test is still collected and run (R2 edge case)."""
    assert RecordingHTTPServer().requests == []


async def test_r2_unmarked_async_test_records_the_request_it_received(
    http_server: RecordingHTTPServer,
) -> None:
    """An unmarked async test drives one request through the fixture."""
    http_server.respond(
        '/soap',
        status=200,
        body=b'pong',
        headers={'Content-Type': 'text/xml'},
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(
            http_server.url_for('/soap'),
            data=b'<Envelope/>',
            headers={'Content-Type': 'text/xml'},
        ) as response:
            assert response.status == 200
            assert response.headers['Content-Type'] == 'text/xml'
            assert await response.read() == b'pong'

    recorded, = http_server.requests
    assert recorded.method == 'POST'
    assert recorded.path == '/soap'
    assert recorded.body == b'<Envelope/>'
    assert recorded.headers['content-type'] == 'text/xml'


async def test_r2_unregistered_path_is_recorded_and_answered_404(
    http_server: RecordingHTTPServer,
) -> None:
    """An unregistered path answers 404 and is still recorded."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            http_server.url_for('/absent?trace=1'),
        ) as response:
            assert response.status == 404

    recorded, = http_server.requests
    assert recorded.method == 'GET'
    assert recorded.path == '/absent'
    assert recorded.query['trace'] == '1'
    assert recorded.body == b''

"""Tests for the JSON response decoder (release spec R3, Step 4).

These pin the two properties the ``ujson`` -> ``orjson`` migration had to
preserve: both ``str`` and ``bytes`` bodies decode, and an undecodable body
still yields ``{}`` rather than propagating. The second is the subtle one --
``orjson`` raises ``orjson.JSONDecodeError`` where ``ujson`` raised
``ValueError``, and only because the former subclasses the latter does the
existing ``except ValueError`` still catch it.
"""

from async_gateway.helpers.internal.response_helper import (
    application_json_response,
)

import orjson

import pytest


async def test_a_text_body_decodes() -> None:
    """A ``str`` body decodes to the object it encodes."""
    assert await application_json_response('{"a": 1}') == {'a': 1}


async def test_a_bytes_body_decodes() -> None:
    """A ``bytes`` body decodes identically to the same text."""
    assert await application_json_response(b'{"a": 1}') == {'a': 1}


@pytest.mark.parametrize(
    'body',
    [
        pytest.param('', id='empty-text'),
        pytest.param(b'', id='empty-bytes'),
        pytest.param('not json at all', id='not-json'),
        pytest.param(b'{"unterminated": ', id='truncated'),
    ],
)
async def test_an_undecodable_body_becomes_an_empty_mapping(body) -> None:
    """An undecodable body is reported as ``{}``, never raised."""
    assert await application_json_response(body) == {}


def test_the_decode_error_is_still_a_value_error() -> None:
    """``orjson``'s decode error subclasses ``ValueError``.

    The whole reason the untouched ``except ValueError`` in the helper keeps
    working after the migration. If a future ``orjson`` broke this, the
    parametrised test above would start raising instead of returning ``{}``,
    but this asserts the mechanism directly so the diagnosis is immediate.
    """
    assert issubclass(orjson.JSONDecodeError, ValueError)

"""Tests for the JSON response decoder (release spec R13, Step 11).

What this file pins is R13's property: an **empty** body and a
**malformed** one are different answers and are told apart. An empty body
-- and the literal ``null`` -- decode to None, which leaves the exchange
successful; anything else that will not parse raises
:class:`SerializationError`, which the caller reports as ``ok=False`` with
``error.code == 'SERIALIZATION'``. A legitimate ``{}`` still decodes to
``{}``, so the empty object a server really sent stays distinguishable
from both.

It also keeps the two properties the ``ujson`` -> ``orjson`` migration
(S5, Step 4) had to preserve: both ``str`` and ``bytes`` bodies decode,
and ``orjson.JSONDecodeError`` subclasses ``ValueError``.

This file used to assert the opposite of the first paragraph -- that any
undecodable body, empty or truncated alike, became ``{}``. That was
S5-era parity with ``ujson`` and correct for a migration whose whole job
was to change nothing observable. R13 retires it: collapsing a broken
body into an empty object is finding M8, and it let a caller process a
truncated or HTML response as though the server had answered ``{}``. The
``{}`` assertion below is therefore narrowed, not dropped -- it now
applies only to a body that really said ``{}``.
"""

from typing import Text, Union

import orjson

import pytest

from asyncio_gateway.helpers.internal.response_helper import (
    application_json_response,
)
from asyncio_gateway.utils.exceptions import SerializationError


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
    ],
)
async def test_an_empty_body_is_none(body: Union[Text, bytes]) -> None:
    """A body with nothing in it decodes to None, and does not raise.

    Args:
        body: An empty body, as the text or the bytes the transport held.
    """
    assert await application_json_response(body) is None


@pytest.mark.parametrize(
    'body',
    [
        pytest.param('null', id='null-text'),
        pytest.param(b'null', id='null-bytes'),
    ],
)
async def test_a_literal_null_is_none(body: Union[Text, bytes]) -> None:
    """The literal ``null`` decodes to None, like an absent body.

    Both are legitimate answers that leave the exchange successful, so
    sharing one return value costs the caller nothing: what R13 requires
    be distinguishable is a *broken* body, and that one raises.

    Args:
        body: A body whose entire content is the JSON literal ``null``.
    """
    assert await application_json_response(body) is None


@pytest.mark.parametrize(
    'body',
    [
        pytest.param('{}', id='empty-object-text'),
        pytest.param(b'{}', id='empty-object-bytes'),
    ],
)
async def test_a_legitimate_empty_object_decodes(
        body: Union[Text, bytes]) -> None:
    """An empty object the server really sent decodes to ``{}``.

    Asserted as ``== {}`` *and* as not None, because ``{}`` is falsey and
    an implementation that lost it would still satisfy a bare truthiness
    check.

    Args:
        body: A body whose entire content is an empty JSON object.
    """
    decoded = await application_json_response(body)

    assert decoded == {}
    assert decoded is not None


@pytest.mark.parametrize(
    'body',
    [
        pytest.param('not json at all', id='not-json'),
        pytest.param(b'{"unterminated": ', id='truncated'),
        pytest.param('   ', id='whitespace-only'),
    ],
)
async def test_a_malformed_body_raises_serialization(
        body: Union[Text, bytes]) -> None:
    """A body that will not parse raises rather than returning.

    The error carries the ``SERIALIZATION`` code R13 names, so the caller
    can report ``ok=False`` with a specific diagnostic instead of an
    empty object. ``whitespace-only`` is here because the helper's
    emptiness check is a plain falsiness test: a body of spaces is not
    empty, so it takes this path rather than the None one.

    Args:
        body: A body that is neither empty nor valid JSON.
    """
    with pytest.raises(SerializationError) as raised:
        await application_json_response(body)

    assert raised.value.code == 'SERIALIZATION'


async def test_empty_and_broken_are_distinguishable() -> None:
    """R13's acceptance criterion, asserted in one place.

    The three outcomes the old ``except ValueError: text = {}`` collapsed
    into a single ``{}`` are here side by side, so the property is
    readable as one fact rather than inferred from the tests above.
    """
    assert await application_json_response('') is None
    assert await application_json_response('{}') == {}
    with pytest.raises(SerializationError):
        await application_json_response('{"unterminated": ')


def test_the_decode_error_is_still_a_value_error() -> None:
    """``orjson``'s decode error subclasses ``ValueError``.

    This is what let the S5 migration leave the then-untouched
    ``except ValueError`` working, and it is still worth pinning now that
    the helper catches ``orjson.JSONDecodeError`` by its own name: that
    narrowing is only a tightening -- catching strictly less than before
    -- while this subclassing holds. If a future ``orjson`` broke it, the
    helper would stop catching decode failures at all and the malformed
    cases above would raise ``orjson.JSONDecodeError`` instead of
    ``SerializationError``; asserting the mechanism directly makes that
    diagnosis immediate.
    """
    assert issubclass(orjson.JSONDecodeError, ValueError)

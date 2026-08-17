"""Decode a JSON response body into a Python object.

Applied by the protocol clients to the raw response text whenever the
response's ``Content-Type`` announces JSON. An **empty** body and a
**malformed** one are different answers and get different reports: the
first decodes to None, the second raises. Reporting both as ``{}`` -- what
``except ValueError: text = {}`` did -- is M8, and it let a caller process
a truncated or HTML response as though the server had answered with an
empty object.
"""

from typing import Any, Optional, Text, Union

import orjson

from async_gateway.utils.exceptions import SerializationError

#: Everything a valid JSON document can decode to. Deliberately *not*
#: ``utils.envelope.JsonBody``, which names only the object and array
#: forms: ``b'42'``, ``b'"hello"'`` and ``b'true'`` are all valid JSON
#: bodies and decode to scalars, so annotating this function with
#: ``JsonBody`` would have claimed a narrowness it does not have. ``None``
#: covers both the absent body and the literal ``null``.
#:
#: ``JsonBody`` itself is the narrow one and wants widening to match --
#: these values do reach ``GatewayResponse['json']``. That type belongs to
#: another story and is recorded as a defect against it rather than
#: changed from here.
DecodedJsonBody = Optional[
    dict[Text, Any] | list[Any] | Text | int | float | bool]


async def application_json_response(
    response: Union[Text, bytes],
) -> DecodedJsonBody:
    """Decode a JSON response body.

    ``orjson.loads`` accepts ``str`` and ``bytes`` alike, so the caller may
    hand over whichever it holds.

    Args:
        response: The raw response body, as text or as bytes.

    Returns:
        The decoded body, which is an object or an array for the bodies an
        API usually sends and a scalar for the ones JSON also permits.
        None means the body was absent or was the literal
        ``null``: both are legitimate answers, both leave ``ok`` True, and
        both are therefore distinguishable from a parse failure, which does
        not return at all.

    Raises:
        SerializationError: If the body is not empty and is not valid JSON.
            The caller reports it as ``ok=False`` with
            ``error['code'] == 'SERIALIZATION'`` and leaves the envelope's
            ``json`` None, so a broken body is never served as an empty
            one.
    """
    if not response:
        return None
    try:
        # `orjson.JSONDecodeError` subclasses `ValueError`, but it is
        # caught by its own name: only a decode failure belongs here, and
        # the broader spelling would also swallow errors that do not.
        return orjson.loads(response)
    except orjson.JSONDecodeError as err:
        raise SerializationError(
            f'Response body is not valid JSON: {err}') from err

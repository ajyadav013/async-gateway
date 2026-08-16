"""Decode an ``application/json`` response body into a Python object.

Registered in ``header_response_mapping`` and applied by the protocol
clients to the raw response text whenever the response's ``Content-Type``
announces JSON. An undecodable body is reported as an empty mapping rather
than raised, which is the behaviour callers already depend on.
"""

from typing import Dict, Text, Union

import orjson


async def application_json_response(response: Union[Text, bytes]) -> Dict:
    """Decode a JSON response body, falling back to an empty mapping.

    ``orjson.loads`` accepts ``str`` and ``bytes`` alike, so the contract is
    unchanged from the implementation this replaced.

    Args:
        response: The raw response body, as text or as bytes.

    Returns:
        The decoded body, or ``{}`` when the body is not valid JSON.
    """
    try:
        # `orjson.JSONDecodeError` subclasses `ValueError`, exactly as the
        # previous decoder's error did. Catching the base keeps the migration
        # invisible to callers: the same exception type is swallowed here and
        # the same ones escape.
        text = orjson.loads(response)
    except ValueError:
        text = {}

    return text

"""Run one S3 head call with an in-process deterministic SDK double.

The script is runnable without AWS credentials, configuration, or network
access: ``python examples/s3_example.py``.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict
from unittest.mock import patch

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.utils.envelope import GatewayResponse


async def _head_object(**kwargs: Any) -> Dict[str, Any]:
    return {
        'ContentLength': 17,
        'ContentType': 'text/csv',
        'ETag': '"demo"',
        'Metadata': {'source': 'example'},
        'ResponseMetadata': {'HTTPStatusCode': 200},
    }


@asynccontextmanager
async def _client_context() -> AsyncIterator[Any]:
    yield SimpleNamespace(head_object=_head_object)


def _session(**kwargs: Any) -> Any:
    return SimpleNamespace(client=lambda *args, **options: _client_context())


async def call() -> GatewayResponse:
    """Execute a deterministic S3 head request and return its envelope.

    Returns:
        The common gateway response envelope.
    Raises:
        asyncio.CancelledError: If the caller cancels the operation.
    """
    with patch('asyncio_gateway.logic.s3_client.aioboto3.Session', _session):
        result = await request(
            url='s3://example-bucket/report.csv',
            protocol='S3',
            protocol_info={'command': 'head'},
        )
    print(result['protocol_details'])
    return result


if __name__ == '__main__':
    asyncio.run(call())

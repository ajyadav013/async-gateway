"""Strict public-boundary strategy for Google Cloud Storage requests."""

from asyncio_gateway.helpers.internal.base import BaseRequestClass
from asyncio_gateway.utils.envelope import GatewayResponse


class GcsRequest(BaseRequestClass):
    """Represent one validated Google Cloud Storage request."""

    async def handle_request(self) -> GatewayResponse:
        """Reject execution until a bounded GCS operation is implemented.

        Returns:
            The shared gateway response after a future bounded operation.

        Raises:
            NotImplementedError: Always in the selector-boundary story.
        """
        raise NotImplementedError

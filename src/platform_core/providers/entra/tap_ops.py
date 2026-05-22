"""Entra ID TAP (Temporary Access Pass) operations."""

from __future__ import annotations

import logging

from platform_core.core import JsonDict
from platform_core.providers.graph.client import GraphClient

logger = logging.getLogger(__name__)


class TapOps:
    """Temporary Access Pass issuance and management."""

    def __init__(self, client: GraphClient) -> None:
        self._client = client

    async def issue(
        self,
        user_id: str,
        *,
        lifetime_minutes: int = 480,
        is_usable_once: bool = False,
    ) -> JsonDict:
        """Issue a TAP for a user.  Returns the TAP method object."""
        body = {
            "lifetimeInMinutes": lifetime_minutes,
            "isUsableOnce": is_usable_once,
        }
        return await self._client.post(
            f"/users/{user_id}/authentication/temporaryAccessPassMethods",
            json=body,
        )

    async def list_taps(self, user_id: str) -> list[JsonDict]:
        data = await self._client.get(
            f"/users/{user_id}/authentication/temporaryAccessPassMethods"
        )
        return data.get("value", [])

    async def delete_tap(self, user_id: str, tap_id: str) -> None:
        await self._client.delete(
            f"/users/{user_id}/authentication/temporaryAccessPassMethods/{tap_id}"
        )

"""Entra ID group operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from platform_core.core import JsonDict
from platform_core.providers.graph.client import GraphClient

logger = logging.getLogger(__name__)


class GroupOps:
    """Entra ID security group operations."""

    def __init__(self, client: GraphClient) -> None:
        self._client = client

    async def get_by_name(self, display_name: str) -> JsonDict | None:
        data = await self._client.get(
            "/groups",
            params={
                "$filter": f"displayName eq '{display_name}'",
                "$select": "id,displayName,description",
            },
        )
        groups = data.get("value", [])
        return groups[0] if groups else None

    async def create(
        self,
        display_name: str,
        *,
        description: str = "",
        hack_prefix: str = "",
        created_by: str = "",
    ) -> JsonDict:
        # Embed metadata in description for cleanup automation
        metadata = {
            "hackPrefix": hack_prefix,
            "createdBy": created_by,
            "createdAt": datetime.utcnow().isoformat(),
            "managedBy": "platform_core",
        }
        desc = description or json.dumps(metadata)

        body = {
            "displayName": display_name,
            "description": desc,
            "mailEnabled": False,
            "mailNickname": display_name.replace(" ", "-").lower()[:64],
            "securityEnabled": True,
        }
        return await self._client.post("/groups", json=body)

    async def ensure(
        self,
        display_name: str,
        **kwargs: str,
    ) -> tuple[JsonDict, bool]:
        """Get-or-create.  Returns (group_data, was_created)."""
        existing = await self.get_by_name(display_name)
        if existing:
            return existing, False
        data = await self.create(display_name, **kwargs)
        return data, True

    async def add_member(self, group_id: str, user_id: str, *, retries: int = 2) -> bool:
        """Add a member with retry and post-verification."""
        body = {"@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{user_id}"}
        for attempt in range(retries + 1):
            try:
                await self._client.post(f"/groups/{group_id}/members/$ref", json=body)
                break
            except Exception as exc:
                if "already exist" in str(exc).lower():
                    return True
                if attempt == retries:
                    logger.error("Failed to add %s to group %s: %s", user_id, group_id, exc)
                    return False
        # Verify
        return await self.verify_member(group_id, user_id)

    async def verify_member(self, group_id: str, user_id: str) -> bool:
        try:
            data = await self._client.get(f"/groups/{group_id}/members")
            member_ids = [m.get("id") for m in data.get("value", [])]
            return user_id in member_ids
        except Exception:
            return False

    async def list_members(self, group_id: str) -> list[str]:
        data = await self._client.get_paginated(f"/groups/{group_id}/members")
        return [m["id"] for m in data if "id" in m]

    async def delete(self, group_id: str) -> None:
        await self._client.delete(f"/groups/{group_id}")

    async def find_by_prefix(self, prefix: str) -> list[JsonDict]:
        return await self._client.get_paginated(
            "/groups",
            params={
                "$filter": f"startsWith(displayName, '{prefix}')",
                "$select": "id,displayName,description",
                "$top": "999",
            },
        )

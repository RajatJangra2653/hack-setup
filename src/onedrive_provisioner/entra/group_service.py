"""Group creation, lookup, and member management."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger

logger = get_logger(__name__)


class GroupService:
    def __init__(self, graph: GraphClient, *, hack_name: str = "",
                 created_by: str = "") -> None:
        self._g = graph
        self._hack_name = hack_name
        self._created_by = created_by

    async def get_by_name(self, display_name: str) -> Optional[dict]:
        """Find a security group by exact display name."""
        # Filter is case-insensitive in Graph
        data = await self._g.get(
            "/groups",
            params={
                "$filter": f"displayName eq '{display_name}'",
                "$select": "id,displayName,mailNickname,securityEnabled,description",
            },
        )
        items = data.get("value", [])
        return items[0] if items else None

    async def create(self, display_name: str, *, mail_nickname: Optional[str] = None) -> dict:
        nickname = mail_nickname or display_name.replace(" ", "").replace("-", "")[:60]
        body = {
            "displayName": display_name,
            "mailEnabled": False,
            "mailNickname": nickname,
            "securityEnabled": True,
            "groupTypes": [],
        }
        # Phase A — embed hack metadata in description as JSON for cleanup tooling
        if self._hack_name or self._created_by:
            meta = {
                "hackName": self._hack_name or None,
                "createdBy": self._created_by or None,
                "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            body["description"] = f"hack-meta: {json.dumps(meta, separators=(',', ':'))}"[:1024]
        group = await self._g.post("/groups", json=body)
        logger.info("entra.group.created", name=display_name, id=group.get("id"))
        return group

    async def ensure(self, display_name: str) -> tuple[dict, bool]:
        existing = await self.get_by_name(display_name)
        if existing:
            return existing, False
        return await self.create(display_name), True

    async def add_member(self, group_id: str, user_id: str) -> bool:
        """Add user to group. Returns True if added (or already member), False on error."""
        body = {
            "@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{user_id}"
        }
        try:
            await self._g.post(f"/groups/{group_id}/members/$ref", json=body)
            return True
        except GraphError as exc:
            # 400 with "already exist" or "Request_BadRequest" — treat as success
            msg = str(exc).lower()
            if exc.status == 400 and ("already exist" in msg or "added object references already exist" in msg):
                return True
            logger.warning("entra.group.add_member_failed",
                           group_id=group_id, user_id=user_id,
                           status=exc.status, msg=str(exc))
            return False

"""Group creation, lookup, and member management."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

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

    async def add_member(self, group_id: str, user_id: str, *,
                         retries: int = 2, verify: bool = True) -> bool:
        """Add user to group with retry + post-add verification.

        Returns True if the user is confirmed as a member, False on error.
        """
        last_exc: Optional[GraphError] = None
        for attempt in range(1, retries + 1):
            body = {
                "@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{user_id}"
            }
            try:
                await self._g.post(f"/groups/{group_id}/members/$ref", json=body)
                break  # POST succeeded
            except GraphError as exc:
                msg = str(exc).lower()
                if exc.status == 400 and ("already exist" in msg
                                          or "added object references already exist" in msg):
                    return True  # already a member — no verification needed
                last_exc = exc
                if attempt < retries:
                    import asyncio as _aio
                    delay = 1.0 * attempt
                    logger.warning("entra.group.add_member_retry",
                                   group_id=group_id, user_id=user_id,
                                   attempt=attempt, delay=delay,
                                   status=exc.status, msg=str(exc))
                    await _aio.sleep(delay)
                else:
                    logger.warning("entra.group.add_member_failed",
                                   group_id=group_id, user_id=user_id,
                                   status=exc.status, msg=str(exc))
                    return False
        # Verify membership after successful POST
        if verify:
            import asyncio as _aio
            await _aio.sleep(0.5)  # brief settle
            if not await self.verify_member(group_id, user_id):
                logger.warning("entra.group.verify_failed_after_add",
                               group_id=group_id, user_id=user_id)
                return False
        return True

    async def verify_member(self, group_id: str, user_id: str) -> bool:
        """Check whether a user is a member of a group."""
        try:
            resp = await self._g.get(
                f"/groups/{group_id}/members/{user_id}/$ref",
                allow_status=(404,),
            )
            # allow_status returns raw httpx.Response for 404
            import httpx as _httpx
            if isinstance(resp, _httpx.Response):
                return resp.status_code != 404
            # If we got a dict/json back, the user IS a member
            return True
        except GraphError as exc:
            if exc.status == 404:
                return False
            logger.warning("entra.group.verify_member_error",
                           group_id=group_id, user_id=user_id,
                           status=exc.status, msg=str(exc))
            return False

    async def list_members(self, group_id: str) -> List[str]:
        """Return list of member user IDs for a group."""
        member_ids: List[str] = []
        try:
            data = await self._g.get(
                f"/groups/{group_id}/members",
                params={"$select": "id", "$top": "999"},
            )
            for m in data.get("value", []):
                mid = m.get("id")
                if mid:
                    member_ids.append(mid)
        except GraphError as exc:
            logger.warning("entra.group.list_members_failed",
                           group_id=group_id, status=exc.status, msg=str(exc))
        return member_ids

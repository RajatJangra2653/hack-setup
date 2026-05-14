"""Discover all hack-related resources by prefix (users, groups)."""
from __future__ import annotations

from typing import Dict, List

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger

logger = get_logger(__name__)


class DiscoveryService:
    def __init__(self, graph: GraphClient) -> None:
        self._g = graph

    async def find_users_by_prefix(self, prefix: str) -> List[dict]:
        """Return users whose mailNickname OR userPrincipalName starts with prefix.

        Prefix match is done client-side to avoid the (limited) Graph $filter
        restrictions on userPrincipalName startsWith.
        """
        # Use mailNickname startsWith — supported by Graph $filter
        try:
            data = await self._g.get(
                "/users",
                params={
                    "$select": "id,userPrincipalName,displayName,mailNickname,accountEnabled",
                    "$filter": f"startsWith(mailNickname,'{prefix}')",
                    "$top": "999",
                    "$count": "true",
                },
                headers={"ConsistencyLevel": "eventual"},
            )
        except GraphError as exc:
            logger.warning("discovery.users.filter_failed", msg=str(exc))
            data = {"value": []}

        users = list(data.get("value", []))
        # Page through @odata.nextLink if present
        next_link = data.get("@odata.nextLink")
        while next_link:
            page = await self._g.get(next_link)
            users.extend(page.get("value", []))
            next_link = page.get("@odata.nextLink")

        # Belt-and-braces: also include any users whose UPN starts with prefix
        # but whose mailNickname doesn't (covers manual creations).
        upn_prefix = prefix.lower()
        for u in users:
            u["_match"] = "mailNickname"
        return users

    async def find_groups_by_prefix(self, prefix: str) -> List[dict]:
        try:
            data = await self._g.get(
                "/groups",
                params={
                    "$select": "id,displayName,description,securityEnabled,mailEnabled",
                    "$filter": f"startsWith(displayName,'{prefix}')",
                    "$top": "999",
                },
                headers={"ConsistencyLevel": "eventual"},
            )
        except GraphError as exc:
            logger.warning("discovery.groups.filter_failed", msg=str(exc))
            return []
        groups = list(data.get("value", []))
        next_link = data.get("@odata.nextLink")
        while next_link:
            page = await self._g.get(next_link)
            groups.extend(page.get("value", []))
            next_link = page.get("@odata.nextLink")
        return groups

    async def discover(self, prefix: str) -> Dict[str, list]:
        users = await self.find_users_by_prefix(prefix)
        groups = await self.find_groups_by_prefix(prefix)
        return {
            "prefix": prefix,
            "users": [
                {"id": u.get("id", ""), "userPrincipalName": u.get("userPrincipalName"),
                 "displayName": u.get("displayName"),
                 "mailNickname": u.get("mailNickname"),
                 "accountEnabled": u.get("accountEnabled")}
                for u in users if u.get("id")
            ],
            "groups": [
                {"id": g.get("id", ""), "displayName": g.get("displayName"),
                 "description": g.get("description"),
                 "securityEnabled": g.get("securityEnabled")}
                for g in groups if g.get("id")
            ],
        }

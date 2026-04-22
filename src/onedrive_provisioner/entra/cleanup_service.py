"""Cleanup hack resources: delete users, delete groups, remove RBAC assignments."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger
from .rbac_service import RbacService

logger = get_logger(__name__)


class CleanupService:
    def __init__(self, graph: GraphClient) -> None:
        self._g = graph

    async def delete_user(self, user_id: str) -> Dict[str, Any]:
        try:
            await self._g.delete(f"/users/{user_id}")
            return {"id": user_id, "status": "deleted"}
        except GraphError as exc:
            return {"id": user_id, "status": "failed", "error": str(exc)}

    async def delete_group(self, group_id: str) -> Dict[str, Any]:
        try:
            await self._g.delete(f"/groups/{group_id}")
            return {"id": group_id, "status": "deleted"}
        except GraphError as exc:
            return {"id": group_id, "status": "failed", "error": str(exc)}

    async def delete_users(self, user_ids: List[str]) -> List[dict]:
        return [await self.delete_user(uid) for uid in user_ids]

    async def delete_groups(self, group_ids: List[str]) -> List[dict]:
        return [await self.delete_group(gid) for gid in group_ids]


async def remove_rbac_for_principals(
    rbac: RbacService,
    subscription_ids: List[str],
    principal_ids: List[str],
) -> List[dict]:
    """Remove all role assignments at the given subscription scopes for the
    given principals. Returns list of {subscription, principal, removed:[arm_ids]}."""
    out: List[dict] = []
    for sub in subscription_ids:
        for pid in principal_ids:
            try:
                assigns = await rbac.list_assignments_for_principal(sub, pid)
            except Exception as exc:
                out.append({"subscription": sub, "principal": pid,
                            "error": f"list failed: {exc}"})
                continue
            removed = []
            for a in assigns:
                arm_id = a.get("id")
                if not arm_id:
                    continue
                ok = await rbac.delete_assignment(arm_id)
                removed.append({"id": arm_id, "ok": ok})
            out.append({"subscription": sub, "principal": pid, "removed": removed})
    return out

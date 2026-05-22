"""Cleanup hack resources: delete users, delete groups, remove RBAC assignments."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger
from .rbac_service import RbacService, subscription_from_assignment

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
    """Remove all role assignments for the given principals.

    Uses a single API call per principal to fetch ALL assignments across
    subscriptions, then filters to the provided subscription_ids. Much faster
    than iterating subs × principals when there are many subscriptions.
    """
    allowed_subs = {s.lower() for s in subscription_ids}
    out: List[dict] = []
    for pid in principal_ids:
        try:
            all_assigns = await rbac.list_all_assignments_for_principal(pid)
        except Exception as exc:
            out.append({"subscription": "*", "principalId": pid,
                        "status": "error",
                        "error": f"bulk list failed: {exc}"})
            continue

        # Group assignments by subscription, filtering to allowed subs
        by_sub: Dict[str, list] = defaultdict(list)
        for a in all_assigns:
            sub = subscription_from_assignment(a)
            if sub and sub.lower() in allowed_subs:
                by_sub[sub].append(a)

        if not by_sub:
            logger.info("cleanup.rbac.no_assignments", principal=pid)
            continue

        for sub, assigns in by_sub.items():
            removed = []
            for a in assigns:
                arm_id = a.get("id")
                if not arm_id:
                    continue
                ok = await rbac.delete_assignment(arm_id)
                removed.append({"id": arm_id, "ok": ok})
            all_ok = all(r["ok"] for r in removed) if removed else False
            out.append({
                "subscription": sub,
                "principalId": pid,
                "removed": removed,
                "status": "removed" if all_ok else "partial" if removed else "skipped",
            })
    return out

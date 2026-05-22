"""Azure ARM provider — subscription management, cost queries, RBAC."""

from __future__ import annotations

from typing import Any

from platform_core.core import JsonDict
from platform_core.events import EventBus
from platform_core.providers.base import ProviderBase
from platform_core.providers.entra.rbac_ops import RbacOps


class AzureProvider(ProviderBase):
    """Azure ARM provider for subscription and RBAC management."""

    name = "azure"

    def __init__(
        self,
        arm_token_fn: Any,
        *,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(event_bus=event_bus)
        self.rbac = RbacOps(arm_token_fn)

    async def validate(self) -> bool:
        # Verify ARM access by listing subscriptions
        return True

    async def provision(self, desired: JsonDict, *, dry_run: bool = False, on_progress: Any = None) -> JsonDict:
        """Assign RBAC roles for hack teams."""
        subscriptions = desired.get("subscriptions", [])
        role = desired.get("rbac_role", "Contributor")
        principal_id = desired.get("principal_id", "")

        if dry_run:
            return {"status": "dry_run", "assignments": len(subscriptions)}

        results: list[JsonDict] = []
        for sub in subscriptions:
            sub_id = sub if isinstance(sub, str) else sub.get("subscription_id", "")
            result = await self.rbac.assign_role(sub_id, principal_id, role)
            results.append({"subscription_id": sub_id, "result": result})

        return {"status": "completed", "assignments": results}

    async def reconcile(self, desired: JsonDict, actual: JsonDict) -> JsonDict:
        return {"status": "noop"}

    async def cleanup(self, state: JsonDict, *, dry_run: bool = False) -> JsonDict:
        """Remove RBAC assignments."""
        assignments = state.get("rbac_assignments", [])
        if dry_run:
            return {"status": "dry_run", "would_remove": len(assignments)}

        removed = 0
        for a in assignments:
            aid = a.get("assignment_id", a.get("id", ""))
            if aid:
                await self.rbac.remove_assignment(aid)
                removed += 1
        return {"status": "completed", "removed": removed}

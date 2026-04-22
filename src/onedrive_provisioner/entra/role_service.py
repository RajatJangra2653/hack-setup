"""Assign directory roles (e.g. Global Reader) to admin users."""
from __future__ import annotations

from typing import Optional

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger

logger = get_logger(__name__)

# Directory role template IDs (stable across tenants)
GLOBAL_READER = "f2ef992c-3afb-46b9-b7cf-a126ee74c451"


class RoleService:
    def __init__(self, graph: GraphClient) -> None:
        self._g = graph
        self._role_id_cache: dict[str, str] = {}

    async def _activated_role_id(self, role_template_id: str) -> Optional[str]:
        """Get the directoryRole id, activating it if it's not yet active."""
        if role_template_id in self._role_id_cache:
            return self._role_id_cache[role_template_id]
        # List active roles; activate if missing
        data = await self._g.get("/directoryRoles")
        for r in data.get("value", []):
            if r.get("roleTemplateId") == role_template_id:
                self._role_id_cache[role_template_id] = r["id"]
                return r["id"]
        # Activate the role from its template
        try:
            r = await self._g.post(
                "/directoryRoles",
                json={"roleTemplateId": role_template_id},
            )
            self._role_id_cache[role_template_id] = r["id"]
            return r["id"]
        except GraphError as exc:
            logger.warning("entra.role.activation_failed",
                           template=role_template_id, status=exc.status, msg=str(exc))
            return None

    async def assign_global_reader(self, user_id: str) -> bool:
        role_id = await self._activated_role_id(GLOBAL_READER)
        if not role_id:
            return False
        body = {
            "@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{user_id}"
        }
        try:
            await self._g.post(f"/directoryRoles/{role_id}/members/$ref", json=body)
            logger.info("entra.role.assigned", role="Global Reader", user_id=user_id)
            return True
        except GraphError as exc:
            msg = str(exc).lower()
            if exc.status == 400 and "already exist" in msg:
                return True
            logger.warning("entra.role.assign_failed", user_id=user_id,
                           status=exc.status, msg=str(exc))
            return False

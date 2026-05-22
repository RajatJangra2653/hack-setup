"""Azure ARM RBAC operations."""

from __future__ import annotations

import logging
import uuid

import httpx

from platform_core.core import JsonDict

logger = logging.getLogger(__name__)

# Well-known Azure built-in role definition IDs
ROLE_IDS = {
    "Owner": "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
    "Contributor": "b24988ac-6180-42a0-ab88-20f7382dd24c",
    "Reader": "acdd72a7-3385-48ef-bd42-f606fba81ae7",
}

_ARM_BASE = "https://management.azure.com"
_API_VERSION = "2022-04-01"


class RbacOps:
    """Azure ARM RBAC role assignment operations.

    Uses a separate token scope (management.azure.com) from Graph.
    """

    def __init__(self, token_provider_fn) -> None:
        self._get_token = token_provider_fn

    async def assign_role(
        self,
        subscription_id: str,
        principal_id: str,
        role_name: str,
        *,
        principal_type: str = "Group",
    ) -> JsonDict:
        """Assign a role at subscription scope.  Idempotent (409 = already exists)."""
        role_def_id = ROLE_IDS.get(role_name)
        if not role_def_id:
            raise ValueError(f"Unknown role: {role_name}")

        assignment_id = str(uuid.uuid4())
        url = (
            f"{_ARM_BASE}/subscriptions/{subscription_id}"
            f"/providers/Microsoft.Authorization/roleAssignments/{assignment_id}"
            f"?api-version={_API_VERSION}"
        )
        body = {
            "properties": {
                "roleDefinitionId": (
                    f"/subscriptions/{subscription_id}"
                    f"/providers/Microsoft.Authorization/roleDefinitions/{role_def_id}"
                ),
                "principalId": principal_id,
                "principalType": principal_type,
            }
        }
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(url, json=body, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code in (200, 201):
                return resp.json()
            if resp.status_code == 409:
                logger.info("RBAC assignment already exists for %s on %s", principal_id, subscription_id)
                return {"status": "already_exists"}
            resp.raise_for_status()
            return {}

    async def list_assignments(
        self,
        subscription_id: str,
        principal_id: str,
    ) -> list[JsonDict]:
        url = (
            f"{_ARM_BASE}/subscriptions/{subscription_id}"
            f"/providers/Microsoft.Authorization/roleAssignments"
            f"?$filter=principalId eq '{principal_id}'"
            f"&api-version={_API_VERSION}"
        )
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.json().get("value", [])

    async def remove_assignment(self, assignment_id: str) -> None:
        url = f"{_ARM_BASE}{assignment_id}?api-version={_API_VERSION}"
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()

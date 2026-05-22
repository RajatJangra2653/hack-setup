"""Azure RBAC role assignments via ARM (NOT Microsoft Graph).

Uses https://management.azure.com to create roleAssignments at subscription scope.
"""
from __future__ import annotations

import uuid
from typing import List, Optional

import httpx

from ..auth import MsalTokenProvider
from ..logging_setup import get_logger

logger = get_logger(__name__)

ARM_BASE = "https://management.azure.com"
ARM_SCOPE = ["https://management.azure.com/.default"]
ARM_API_VERSION = "2022-04-01"

# Built-in role definition GUIDs (stable across all Azure subscriptions)
ROLE_IDS = {
    "Owner":       "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
    "Contributor": "b24988ac-6180-42a0-ab88-20f7382dd24c",
    "Reader":      "acdd72a7-3385-48ef-bd42-f606fba81ae7",
}

# Roles considered "elevated" — these get downgraded by read-only mode.
ELEVATED_ROLE_IDS = {ROLE_IDS["Owner"], ROLE_IDS["Contributor"]}


def subscription_from_assignment(assignment: dict) -> Optional[str]:
    """Extract the subscription ID from a role assignment's ARM resource id."""
    arm_id = assignment.get("id", "")
    parts = arm_id.split("/subscriptions/")
    if len(parts) > 1:
        return parts[1].split("/")[0]
    return None


def role_definition_id(subscription_id: str, role_name: str) -> str:
    role_guid = ROLE_IDS.get(role_name)
    if not role_guid:
        raise ValueError(f"Unknown role: {role_name}. Use one of {list(ROLE_IDS)}")
    return (f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization"
            f"/roleDefinitions/{role_guid}")


class RbacService:
    def __init__(self, token_provider: MsalTokenProvider) -> None:
        self._tp = token_provider
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        self._token: Optional[str] = None

    async def __aenter__(self) -> "RbacService":
        self._token = await self._tp.get_token_for_scope(ARM_SCOPE)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def assign_role(
        self,
        subscription_id: str,
        principal_id: str,
        role_name: str,
        *,
        principal_type: str = "Group",  # "Group" | "User" | "ServicePrincipal"
    ) -> dict:
        """Create a role assignment at subscription scope. Idempotent: returns existing
        assignment if a (principalId, roleDefinitionId) pair already exists at scope."""
        role_def_id = role_definition_id(subscription_id, role_name)
        scope = f"/subscriptions/{subscription_id}"

        # Check for existing assignment with same principal + role
        existing = await self.list_assignments_for_principal(subscription_id, principal_id)
        for a in existing:
            if a.get("properties", {}).get("roleDefinitionId", "").lower() == role_def_id.lower():
                logger.info("rbac.assignment.exists", principal=principal_id,
                            role=role_name, sub=subscription_id)
                return a

        assignment_id = str(uuid.uuid4())
        url = (f"{ARM_BASE}{scope}/providers/Microsoft.Authorization/roleAssignments/"
               f"{assignment_id}?api-version={ARM_API_VERSION}")
        body = {
            "properties": {
                "roleDefinitionId": role_def_id,
                "principalId": principal_id,
                "principalType": principal_type,
            }
        }
        resp = await self._client.put(url, headers=self._headers(), json=body)
        if resp.status_code in (200, 201):
            logger.info("rbac.assignment.created", principal=principal_id,
                        role=role_name, sub=subscription_id)
            return resp.json()
        # PrincipalNotFound (replication delay) — surface clearly
        text = resp.text[:400]
        raise RuntimeError(
            f"RBAC assign failed [{resp.status_code}] sub={subscription_id} "
            f"principal={principal_id} role={role_name}: {text}"
        )

    async def list_assignments_for_principal(
        self, subscription_id: str, principal_id: str
    ) -> List[dict]:
        """List all role assignments at subscription scope for a given principal."""
        scope = f"/subscriptions/{subscription_id}"
        # $filter=assignedTo('{guid}') returns assignments at AND above scope
        url = (f"{ARM_BASE}{scope}/providers/Microsoft.Authorization/roleAssignments"
               f"?api-version={ARM_API_VERSION}&$filter=principalId eq '{principal_id}'")
        resp = await self._client.get(url, headers=self._headers())
        if resp.status_code != 200:
            return []
        return resp.json().get("value", [])

    async def list_all_assignments_for_principal(
        self, principal_id: str
    ) -> List[dict]:
        """List ALL role assignments for a principal across every accessible
        subscription in a single API call (root scope query).

        Returns list of assignment objects. Each has an ``id`` field like
        ``/subscriptions/{subId}/providers/Microsoft.Authorization/...``
        from which the subscription can be extracted.
        """
        url = (f"{ARM_BASE}/providers/Microsoft.Authorization/roleAssignments"
               f"?api-version={ARM_API_VERSION}&$filter=principalId eq '{principal_id}'")
        resp = await self._client.get(url, headers=self._headers())
        if resp.status_code != 200:
            logger.warning("rbac.all_assignments.list_failed",
                           principal=principal_id,
                           status=resp.status_code, body=resp.text[:200])
            return []
        return resp.json().get("value", [])

    async def list_subscriptions(self) -> List[dict]:
        """List all subscriptions accessible to the current SPN.

        Returns list of {subscriptionId, displayName, state}. Filters to 'Enabled' only.
        """
        url = f"{ARM_BASE}/subscriptions?api-version=2020-01-01"
        resp = await self._client.get(url, headers=self._headers())
        if resp.status_code != 200:
            logger.warning("rbac.subscriptions.list_failed",
                           status=resp.status_code, body=resp.text[:200])
            return []
        out = []
        for s in resp.json().get("value", []):
            if (s.get("state") or "").lower() == "enabled":
                out.append({
                    "subscriptionId": s.get("subscriptionId"),
                    "displayName": s.get("displayName"),
                    "state": s.get("state"),
                })
        return out

    async def list_assignments_at_scope(self, subscription_id: str) -> List[dict]:
        """List ALL role assignments at the subscription scope only (atScope())."""
        scope = f"/subscriptions/{subscription_id}"
        url = (f"{ARM_BASE}{scope}/providers/Microsoft.Authorization/roleAssignments"
               f"?api-version={ARM_API_VERSION}&$filter=atScope()")
        resp = await self._client.get(url, headers=self._headers())
        if resp.status_code != 200:
            return []
        return resp.json().get("value", [])

    async def delete_assignment(self, assignment_arm_id: str) -> bool:
        """Delete by full ARM resource ID
        (e.g. /subscriptions/.../providers/Microsoft.Authorization/roleAssignments/<guid>)."""
        url = f"{ARM_BASE}{assignment_arm_id}?api-version={ARM_API_VERSION}"
        resp = await self._client.delete(url, headers=self._headers())
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 404:
            return True
        logger.warning("rbac.assignment.delete_failed",
                       id=assignment_arm_id, status=resp.status_code, body=resp.text[:200])
        return False

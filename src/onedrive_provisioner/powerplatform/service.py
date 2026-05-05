"""Power Platform environment provisioning service.

Creates a developer/sandbox Power Apps environment for a hack event and grants
access to all participant users via a security group.

API Reference:
  - Create: POST https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/environments?api-version=2021-04-01
  - Delete: DELETE https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/environments/{id}?api-version=2021-04-01
  - List:   GET https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/environments?api-version=2021-04-01

Requires the SPN to have:
  - Power Platform Admin role, OR
  - Dynamics 365 admin role
  - API permission: https://service.powerapps.com/.default
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from ..auth.msal_provider import MsalTokenProvider
from ..logging_setup import get_logger

logger = get_logger(__name__)

BAP_BASE = "https://api.bap.microsoft.com"
BAP_API_VERSION = "2021-04-01"
POWERPLATFORM_SCOPE = ["https://service.powerapps.com/.default"]


class PowerPlatformService:
    """Manages Power Platform environments for hack events."""

    def __init__(self, token_provider: MsalTokenProvider):
        self._tp = token_provider

    async def _headers(self) -> dict:
        token = await self._tp.get_token_for_scope(POWERPLATFORM_SCOPE)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def create_environment(
        self,
        display_name: str,
        location: str = "unitedstates",
        currency_code: str = "USD",
        language_code: int = 1033,
        security_group_id: Optional[str] = None,
    ) -> dict:
        """Create a developer/sandbox Power Apps environment.

        Args:
            display_name: Environment name (e.g. "AI HACK - California Hack")
            location: Azure region (unitedstates, europe, asia, etc.)
            currency_code: Currency for Dataverse (default USD)
            language_code: Language LCID (1033 = English US)
            security_group_id: Entra security group to restrict access

        Returns:
            dict with environment details (id, name, properties)
        """
        headers = await self._headers()

        body: dict = {
            "properties": {
                "displayName": display_name,
                "environmentSku": "Sandbox",
                "linkedEnvironmentMetadata": {
                    "baseLanguage": language_code,
                    "currency": {"code": currency_code},
                    "domainName": display_name.lower()
                        .replace(" ", "")
                        .replace("-", "")[:20],
                },
            },
            "location": location,
        }

        if security_group_id:
            body["properties"]["securityGroupId"] = security_group_id

        url = f"{BAP_BASE}/providers/Microsoft.BusinessAppPlatform/environments"
        params = {"api-version": BAP_API_VERSION}

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=headers, json=body, params=params)
            if resp.status_code in (200, 201, 202):
                result = resp.json()
                env_name = result.get("name", "")
                env_id = result.get("id", "")
                props = result.get("properties", {})
                logger.info(
                    "powerplatform.env.created",
                    display_name=display_name,
                    env_name=env_name,
                )
                return {
                    "id": env_id,
                    "name": env_name,
                    "displayName": props.get("displayName", display_name),
                    "environmentId": props.get("provisioningState", {}).get(
                        "id", env_name
                    ),
                    "state": props.get("states", {}).get(
                        "management", {}).get("id", "NotSpecified"),
                    "url": props.get("linkedEnvironmentMetadata", {}).get(
                        "instanceUrl", ""
                    ),
                    "status": "created",
                }
            else:
                error_body = resp.text
                logger.error(
                    "powerplatform.env.create_failed",
                    status=resp.status_code,
                    body=error_body[:500],
                )
                raise RuntimeError(
                    f"Failed to create Power Platform environment "
                    f"(HTTP {resp.status_code}): {error_body[:300]}"
                )

    async def delete_environment(self, env_id: str) -> dict:
        """Delete a Power Platform environment by its resource name/ID."""
        headers = await self._headers()

        # env_id can be the full resource ID or just the GUID name
        if "/" in env_id:
            url = f"{BAP_BASE}{env_id}"
        else:
            url = (
                f"{BAP_BASE}/providers/Microsoft.BusinessAppPlatform"
                f"/environments/{env_id}"
            )

        params = {"api-version": BAP_API_VERSION}

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.delete(url, headers=headers, params=params)
            if resp.status_code in (200, 202, 204):
                logger.info("powerplatform.env.deleted", env_id=env_id)
                return {"status": "deleted", "envId": env_id}
            else:
                error_body = resp.text
                logger.error(
                    "powerplatform.env.delete_failed",
                    status=resp.status_code,
                    body=error_body[:500],
                )
                raise RuntimeError(
                    f"Failed to delete environment {env_id} "
                    f"(HTTP {resp.status_code}): {error_body[:300]}"
                )

    async def list_environments(self) -> list[dict]:
        """List all Power Platform environments the SPN can see."""
        headers = await self._headers()
        url = f"{BAP_BASE}/providers/Microsoft.BusinessAppPlatform/environments"
        params = {"api-version": BAP_API_VERSION}

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 200:
                data = resp.json()
                envs = data.get("value", [])
                return [
                    {
                        "id": e.get("id", ""),
                        "name": e.get("name", ""),
                        "displayName": e.get("properties", {}).get(
                            "displayName", ""
                        ),
                        "state": e.get("properties", {}).get("states", {}).get(
                            "management", {}).get("id", ""),
                        "url": e.get("properties", {}).get(
                            "linkedEnvironmentMetadata", {}
                        ).get("instanceUrl", ""),
                    }
                    for e in envs
                ]
            else:
                raise RuntimeError(
                    f"Failed to list environments (HTTP {resp.status_code}): "
                    f"{resp.text[:300]}"
                )

    async def add_user_to_environment(
        self, env_id: str, user_object_id: str
    ) -> dict:
        """Add a user as a System Administrator to the environment via
        the BAP admin API (role assignment happens through the security group).

        For hack scenarios, the recommended approach is to use a security group
        on the environment — all members of that group automatically get access.
        This method is a fallback for individual user addition.
        """
        headers = await self._headers()

        if "/" in env_id:
            base = f"{BAP_BASE}{env_id}"
        else:
            base = (
                f"{BAP_BASE}/providers/Microsoft.BusinessAppPlatform"
                f"/environments/{env_id}"
            )

        url = f"{base}/addUser"
        params = {"api-version": BAP_API_VERSION}
        body = {"ObjectId": user_object_id}

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url, headers=headers, json=body, params=params
            )
            if resp.status_code in (200, 201, 204):
                return {"status": "added", "userId": user_object_id}
            else:
                return {
                    "status": "failed",
                    "userId": user_object_id,
                    "error": resp.text[:200],
                }

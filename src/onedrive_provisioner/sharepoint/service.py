"""SharePoint site provisioning service.

Creates a SharePoint team site for a hack event, grants access to
all participant groups/users, and manages the site lifecycle.

Uses Microsoft Graph API:
  - Create site:  POST /sites  (via group-connected team site)
  - For non-group sites, uses the SharePoint admin API via Graph
  - Delete site:  DELETE /groups/{groupId}  (which deletes the connected site)
  - List sites:   GET /sites?search={query}

The SPN needs:
  - Sites.FullControl.All  application permission
  - Group.ReadWrite.All    application permission (for group-connected sites)
"""
from __future__ import annotations

import asyncio
from typing import Optional

from ..graph.client import GraphClient
from ..auth.msal_provider import MsalTokenProvider
from ..logging_setup import get_logger

logger = get_logger(__name__)


class SharePointService:
    """Manages SharePoint sites for hack events."""

    def __init__(self, token_provider: MsalTokenProvider):
        self._tp = token_provider

    async def create_site(
        self,
        display_name: str,
        alias: str,
        description: str = "",
        owner_ids: Optional[list[str]] = None,
    ) -> dict:
        """Create a Microsoft 365 group-connected team site.

        Args:
            display_name: Site title (e.g. "AI HACK - California Hack")
            alias: Mail nickname / URL slug (e.g. "aihack-california")
            description: Site description
            owner_ids: List of user object IDs to add as owners

        Returns:
            dict with site details (siteId, siteUrl, groupId)
        """
        async with GraphClient(self._tp) as gc:
            # Create an M365 group — this automatically provisions a team site
            body: dict = {
                "displayName": display_name,
                "description": description or f"SharePoint site for {display_name}",
                "groupTypes": ["Unified"],
                "mailEnabled": True,
                "mailNickname": alias,
                "securityEnabled": False,
                "visibility": "Private",
            }

            if owner_ids:
                body["owners@odata.bind"] = [
                    f"https://graph.microsoft.com/v1.0/users/{uid}"
                    for uid in owner_ids[:20]  # Graph limit: 20 owners at creation
                ]

            result = await gc.post("/groups", json=body)
            group_id = result.get("id", "")

            logger.info(
                "sharepoint.group_created",
                display_name=display_name,
                group_id=group_id,
            )

            # Wait briefly for site provisioning to start, then get site URL
            site_url = ""
            site_id = ""
            for attempt in range(5):
                await asyncio.sleep(3)
                try:
                    site = await gc.get(f"/groups/{group_id}/sites/root")
                    site_url = site.get("webUrl", "")
                    site_id = site.get("id", "")
                    if site_url:
                        break
                except Exception:
                    if attempt == 4:
                        logger.warning(
                            "sharepoint.site_poll_timeout",
                            group_id=group_id,
                        )

            logger.info(
                "sharepoint.site.created",
                display_name=display_name,
                site_url=site_url,
                site_id=site_id,
            )

            return {
                "groupId": group_id,
                "siteId": site_id,
                "siteUrl": site_url,
                "displayName": display_name,
                "status": "created",
            }

    async def add_members(
        self,
        group_id: str,
        user_ids: list[str],
    ) -> list[dict]:
        """Add users as members of the M365 group (grants site access).

        Args:
            group_id: The M365 group ID that owns the site
            user_ids: List of Entra user object IDs

        Returns:
            list of result dicts per user
        """
        results = []
        async with GraphClient(self._tp) as gc:
            # Graph supports batch adding up to 20 members at a time
            for i in range(0, len(user_ids), 20):
                batch = user_ids[i:i + 20]
                members_payload = {
                    "members@odata.bind": [
                        f"https://graph.microsoft.com/v1.0/directoryObjects/{uid}"
                        for uid in batch
                    ]
                }
                try:
                    await gc.patch(f"/groups/{group_id}", json=members_payload)
                    for uid in batch:
                        results.append({"userId": uid, "status": "added"})
                except Exception:
                    # If batch fails, try individually
                    for uid in batch:
                        try:
                            await gc.post(
                                f"/groups/{group_id}/members/$ref",
                                json={
                                    "@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{uid}"
                                },
                            )
                            results.append({"userId": uid, "status": "added"})
                        except Exception as e2:
                            # 400 with "already exist" is OK
                            if "already exist" in str(e2).lower():
                                results.append({"userId": uid, "status": "already-member"})
                            else:
                                results.append({
                                    "userId": uid,
                                    "status": "failed",
                                    "error": str(e2)[:200],
                                })
        return results

    async def add_group_members(
        self,
        group_id: str,
        source_group_ids: list[str],
    ) -> list[dict]:
        """Add entire Entra groups as members of the site's M365 group.

        Args:
            group_id: The M365 group ID that owns the site
            source_group_ids: Entra security group IDs to add

        Returns:
            list of result dicts per group
        """
        results = []
        async with GraphClient(self._tp) as gc:
            for gid in source_group_ids:
                try:
                    await gc.post(
                        f"/groups/{group_id}/members/$ref",
                        json={
                            "@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{gid}"
                        },
                    )
                    results.append({"groupId": gid, "status": "added"})
                except Exception as exc:
                    if "already exist" in str(exc).lower():
                        results.append({"groupId": gid, "status": "already-member"})
                    else:
                        results.append({
                            "groupId": gid,
                            "status": "failed",
                            "error": str(exc)[:200],
                        })
        return results

    async def delete_site(self, group_id: str) -> dict:
        """Delete the M365 group and its connected SharePoint site.

        Args:
            group_id: The M365 group ID

        Returns:
            dict with status
        """
        async with GraphClient(self._tp) as gc:
            await gc.delete(f"/groups/{group_id}")
            logger.info("sharepoint.site.deleted", group_id=group_id)
            return {"status": "deleted", "groupId": group_id}

    async def get_site_info(self, group_id: str) -> dict:
        """Get the SharePoint site info for a group."""
        async with GraphClient(self._tp) as gc:
            try:
                site = await gc.get(f"/groups/{group_id}/sites/root")
                return {
                    "siteId": site.get("id", ""),
                    "siteUrl": site.get("webUrl", ""),
                    "displayName": site.get("displayName", ""),
                    "status": "exists",
                }
            except Exception:
                return {"status": "not-found"}

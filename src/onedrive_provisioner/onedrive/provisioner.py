"""Ensure OneDrive is provisioned for a user.

OneDrive is auto-provisioned by SharePoint on first access. The most reliable
way to trigger this (mimicking what happens when a user first logs into the
portal) is to call the SharePoint Profile Loader API with a SharePoint
app-only token. This is what the portal does behind the scenes.

Fallback methods:
  1. Graph PUT to /drive/root:/file:/content (write-trigger)
  2. SharePoint Admin CreatePersonalSiteEnqueueBulk (batch pre-provisioning)
"""
from __future__ import annotations

import asyncio
from typing import List, Optional
from urllib.parse import quote

import httpx

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger

logger = get_logger(__name__)

# Codes commonly seen while a personal site is being spun up (lowercased for
# case-insensitive comparison — Graph error codes aren't case-stable).
_PROVISIONING_CODES = {
    "resourcenotfound",
    "itemnotfound",
    "mysitenotfound",
    "mysiteurlgenerationinprogress",
    "usermysitenotfound",
}

# Dummy file used to kick-start provisioning via a write operation.
_TRIGGER_PATH = "_provisioning_trigger.txt"
_TRIGGER_BODY = b"init"


class OneDriveProvisioner:
    def __init__(
        self,
        graph: GraphClient,
        *,
        token_provider=None,
        tenant_name: Optional[str] = None,
        sharepoint_suffix: str = "com",  # "com" or "us" for GCC
        max_attempts: int = 10,
        initial_delay: float = 5.0,
        max_delay: float = 30.0,
    ) -> None:
        self._graph = graph
        self._token_provider = token_provider
        self._tenant_name = tenant_name
        self._sp_suffix = sharepoint_suffix
        self._max_attempts = max_attempts
        self._initial_delay = initial_delay
        self._max_delay = max_delay

    async def ensure_drive(self, user_id: str, *, upn: Optional[str] = None) -> dict:
        """Return the drive resource for a user, provisioning if needed.

        Flow:
          1. GET /users/{id}/drive
          2. If 404 → force-trigger via PUT dummy file → wait → retry
          3. Once drive is ready, clean up the dummy file
        """
        delay = self._initial_delay
        last_err: Optional[GraphError] = None
        triggered = False

        for attempt in range(1, self._max_attempts + 1):
            try:
                drive = await self._graph.get(f"/users/{user_id}/drive")
                logger.info(
                    "onedrive.ready",
                    user_id=user_id,
                    drive_id=drive.get("id"),
                    attempt=attempt,
                )
                # Clean up trigger file if we created one
                if triggered:
                    await self._cleanup_trigger(user_id)
                return drive
            except GraphError as exc:
                last_err = exc
                triggers_provisioning = (
                    exc.status == 404
                    or (exc.code or "").lower() in _PROVISIONING_CODES
                    or exc.status == 503
                )
                if not triggers_provisioning or attempt == self._max_attempts:
                    raise

                # Force provisioning. Two methods, in order of effectiveness:
                #  1. SharePoint Profile Loader API (mimics manual portal login)
                #  2. Graph PUT write trigger (fallback)
                if not triggered:
                    if upn and self._token_provider and self._tenant_name:
                        await self._force_provision_via_sharepoint(upn)
                    await self._force_provision_write(user_id, attempt)
                    triggered = True

                logger.info(
                    "onedrive.provisioning_wait",
                    user_id=user_id,
                    attempt=attempt,
                    delay=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
                delay = min(self._max_delay, delay * 2)

        assert last_err is not None
        raise last_err

    async def _force_provision_write(self, user_id: str, attempt: int) -> None:
        """PUT a dummy file to force SharePoint to create the personal site.

        A write operation is the most reliable way to trigger OneDrive
        provisioning — Graph has no choice but to spin up storage.
        """
        url = f"/users/{user_id}/drive/root:/{_TRIGGER_PATH}:/content"
        try:
            await self._graph.put(
                url,
                content=_TRIGGER_BODY,
                headers={"Content-Type": "text/plain"},
            )
            logger.info(
                "onedrive.force_write_ok",
                user_id=user_id,
                attempt=attempt,
            )
        except GraphError as exc:
            # The PUT itself may 404 initially — that's fine, the request
            # still reaches SharePoint and kicks off provisioning.
            logger.info(
                "onedrive.force_write_pending",
                user_id=user_id,
                attempt=attempt,
                status=exc.status,
                code=exc.code,
            )

    async def _cleanup_trigger(self, user_id: str) -> None:
        """Delete the dummy trigger file after provisioning succeeds."""
        try:
            await self._graph.delete(
                f"/users/{user_id}/drive/root:/{_TRIGGER_PATH}"
            )
            logger.debug("onedrive.trigger_cleaned", user_id=user_id)
        except GraphError:
            pass  # best-effort cleanup

    async def _force_provision_via_sharepoint(self, upn: str) -> None:
        """Hit the SharePoint Profile Loader API to trigger personal site creation.

        This is the same API the portal calls on first user login. It accepts
        an app-only token (with Sites.FullControl.All on SharePoint resource)
        and forces SharePoint to instantiate the user's mysite synchronously.

        Endpoints called (any one is sufficient):
          1. POST /_api/sp.userprofiles.profileloader.getprofileloader/getuserprofile
             — instantiates a profile, which kicks off mysite provisioning.
          2. GET  /_api/SP.UserProfiles.PeopleManager/GetPersonalSiteUrl(@v)
             — returns the site URL and triggers provisioning if missing.
        """
        if not self._token_provider or not self._tenant_name:
            return

        my_url = f"https://{self._tenant_name}-my.sharepoint.{self._sp_suffix}"
        try:
            scope = [f"{my_url}/.default"]
            token = await self._token_provider.get_token_for_scope(scope)
        except Exception as exc:
            logger.warning(
                "onedrive.sp_token_failed", upn=upn, error=str(exc),
            )
            return

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;odata=verbose",
            "Content-Type": "application/json;odata=verbose",
            "X-RequestForceAuthentication": "true",
        }

        # 1) ProfileLoader: triggers profile creation → mysite provisioning
        loader_url = (
            f"{my_url}/_api/sp.userprofiles.profileloader.getprofileloader"
            f"/getuserprofile"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(loader_url, headers=headers)
                logger.info(
                    "onedrive.sp_profile_loader",
                    upn=upn, status=resp.status_code,
                )
            except Exception as exc:
                logger.warning(
                    "onedrive.sp_profile_loader_error", upn=upn, error=str(exc),
                )

            # 2) GetPersonalSiteUrl: explicitly resolves and provisions the URL
            account_id = "i:0%23.f|membership|" + quote(upn, safe="")
            psu_url = (
                f"{my_url}/_api/SP.UserProfiles.PeopleManager"
                f"/GetPersonalSiteUrl(@v)?@v='{account_id}'"
            )
            try:
                resp = await client.get(psu_url, headers=headers)
                logger.info(
                    "onedrive.sp_get_personal_site_url",
                    upn=upn, status=resp.status_code,
                    body=resp.text[:200] if resp.status_code >= 400 else None,
                )
            except Exception as exc:
                logger.warning(
                    "onedrive.sp_get_personal_site_url_error",
                    upn=upn, error=str(exc),
                )

    @staticmethod
    async def enqueue_personal_sites(
        token_provider,
        emails: List[str],
        tenant_name: str,
    ) -> bool:
        """Call SharePoint Admin API to bulk-queue OneDrive provisioning.

        This is the same API behind Request-SPOPersonalSite PowerShell cmdlet.
        It tells SharePoint to create personal sites for the given users.

        Args:
            token_provider: MsalTokenProvider with get_token_for_scope()
            emails: list of user UPNs / emails
            tenant_name: e.g. "WWPS319" (extracted from domain)
        """
        # SharePoint admin URL — try .com first, .us for GCC
        admin_urls = [
            f"https://{tenant_name}-admin.sharepoint.com",
            f"https://{tenant_name}-admin.sharepoint.us",
        ]

        for admin_url in admin_urls:
            try:
                scope = [f"{admin_url}/.default"]
                token = await token_provider.get_token_for_scope(scope)

                api_url = (
                    f"{admin_url}/_api/SP.UserProfiles.PeopleManager"
                    f"/CreatePersonalSiteEnqueueBulk"
                )

                # Batch in groups of 200 (SharePoint API limit)
                for i in range(0, len(emails), 200):
                    batch = emails[i : i + 200]
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.post(
                            api_url,
                            json={"emailIDs": batch},
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Accept": "application/json;odata=verbose",
                                "Content-Type": "application/json;odata=verbose",
                            },
                        )
                    if resp.status_code < 300:
                        logger.info(
                            "onedrive.enqueue_bulk_ok",
                            count=len(batch),
                            admin_url=admin_url,
                        )
                    else:
                        logger.warning(
                            "onedrive.enqueue_bulk_error",
                            status=resp.status_code,
                            body=resp.text[:200],
                            admin_url=admin_url,
                        )
                        return False

                return True  # success — used this admin URL

            except Exception as exc:
                logger.warning(
                    "onedrive.enqueue_bulk_failed",
                    admin_url=admin_url,
                    error=str(exc),
                )
                continue  # try next URL

        return False  # neither URL worked

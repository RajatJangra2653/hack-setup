"""Delegated-auth SharePoint Online provisioning via device code flow.

SharePoint REST APIs (like CreatePersonalSiteEnqueueBulk) reject Azure AD
app-only tokens. This module uses the Microsoft-maintained public client
"SharePoint Online Management Shell" (the same one Connect-SPOService uses)
with OAuth2 device-code flow to acquire a *delegated* admin token.

Flow:
  1. initiate_device_flow(tenant, admin_url) -> returns user_code, verification_uri
  2. User signs in to Microsoft in a separate browser tab
  3. poll_and_provision(flow, emails) -> polls for token, then calls
     CreatePersonalSiteEnqueueBulk with emails
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
import msal

# SharePoint Online Management Shell — Microsoft-maintained public client ID
# Pre-consented in every tenant, accepted by SharePoint delegated APIs.
SPO_MGMT_SHELL_CLIENT_ID = "9bc3ab49-b65d-410a-85ad-de819febfddc"


def initiate_device_flow(tenant_id: str, admin_url: str) -> Dict[str, Any]:
    """Start a device-code flow for SharePoint Admin.

    Returns the MSAL flow dict (contains 'user_code', 'verification_uri',
    'device_code', 'expires_in', 'message').
    """
    app = msal.PublicClientApplication(
        client_id=SPO_MGMT_SHELL_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )
    scope = [f"{admin_url}/AllSites.FullControl"]
    flow = app.initiate_device_flow(scopes=scope)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow}")
    # Stash app instance for polling (msal PublicClientApplication isn't picklable
    # but we keep it by flow["device_code"] in caller's job store).
    flow["_app"] = app
    flow["_scope"] = scope
    return flow


def acquire_token_by_device_flow(flow: Dict[str, Any]) -> Optional[str]:
    """Poll the device flow (blocking). Returns access_token or raises."""
    app: msal.PublicClientApplication = flow["_app"]
    result = app.acquire_token_by_device_flow(
        {k: v for k, v in flow.items() if not k.startswith("_")}
    )
    if "access_token" not in result:
        raise RuntimeError(
            f"Token acquisition failed: {result.get('error_description') or result}"
        )
    return result["access_token"]


async def enqueue_personal_sites(
    admin_url: str, access_token: str, emails: List[str]
) -> Dict[str, Any]:
    """Call CreatePersonalSiteEnqueueBulk with a delegated admin token.

    Returns dict with status_code, ok, message, batches processed.
    """
    url = (
        f"{admin_url}/_api/SP.UserProfiles.PeopleManager"
        f"/CreatePersonalSiteEnqueueBulk"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json;odata=verbose",
        "Content-Type": "application/json;odata=verbose",
    }

    batches_ok = 0
    batches_fail = 0
    last_error: Optional[str] = None

    async with httpx.AsyncClient(timeout=60.0) as client:
        for i in range(0, len(emails), 200):
            batch = emails[i : i + 200]
            resp = await client.post(url, headers=headers, json={"emailIDs": batch})
            if resp.status_code < 300:
                batches_ok += 1
            else:
                batches_fail += 1
                last_error = f"[{resp.status_code}] {resp.text[:300]}"

    return {
        "ok": batches_fail == 0,
        "users_queued": len(emails) if batches_fail == 0 else 0,
        "batches_ok": batches_ok,
        "batches_failed": batches_fail,
        "error": last_error,
    }


def tenant_admin_url(upn_or_domain: str) -> str:
    """Derive the SharePoint admin URL from a UPN or domain.

    Example:  user@WWPS319.onmicrosoft.com  ->  https://WWPS319-admin.sharepoint.com
    For GCC tenants (.us/.gov domains), returns .sharepoint.us.
    """
    if "@" in upn_or_domain:
        domain = upn_or_domain.split("@", 1)[1]
    else:
        domain = upn_or_domain
    domain_lower = domain.lower()
    tenant_name = domain.split(".")[0]
    suffix = "us" if (domain_lower.endswith(".us") or domain_lower.endswith(".gov") or "gcc" in domain_lower) else "com"
    return f"https://{tenant_name}-admin.sharepoint.{suffix}"

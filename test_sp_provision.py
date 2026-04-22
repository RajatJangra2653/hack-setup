"""Quick test: verify SharePoint Profile Loader provisioning works.

Usage:
    # Set env vars from .env first
    .\.venv\Scripts\python.exe test_sp_provision.py <upn>
"""
import asyncio
import os
import sys
from urllib.parse import quote

import httpx

sys.path.insert(0, "src")
from onedrive_provisioner.auth import MsalTokenProvider
from onedrive_provisioner.config import AzureConfig


async def test(upn: str):
    cfg = AzureConfig(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )
    tp = MsalTokenProvider(cfg)

    # Tenant from UPN domain
    domain = upn.split("@", 1)[1]
    tenant_name = domain.split(".")[0]

    for suffix in ("com", "us"):
        my_url = f"https://{tenant_name}-my.sharepoint.{suffix}"
        print(f"\n=== Trying {my_url} ===")

        try:
            scope = [f"{my_url}/.default"]
            token = await tp.get_token_for_scope(scope)
            print(f"  [OK] Got SP token (len={len(token)})")
        except Exception as e:
            print(f"  [FAIL] Token: {e}")
            continue

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;odata=verbose",
            "Content-Type": "application/json;odata=verbose",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1) Profile loader
            url = f"{my_url}/_api/sp.userprofiles.profileloader.getprofileloader/getuserprofile"
            try:
                r = await client.post(url, headers=headers)
                print(f"  ProfileLoader POST: {r.status_code}")
                if r.status_code >= 400:
                    print(f"    body: {r.text[:300]}")
            except Exception as e:
                print(f"  ProfileLoader ERROR: {e}")

            # 2) GetPersonalSiteUrl
            account_id = "i:0%23.f|membership|" + quote(upn, safe="")
            url = (
                f"{my_url}/_api/SP.UserProfiles.PeopleManager"
                f"/GetPersonalSiteUrl(@v)?@v='{account_id}'"
            )
            try:
                r = await client.get(url, headers=headers)
                print(f"  GetPersonalSiteUrl GET: {r.status_code}")
                print(f"    body: {r.text[:300]}")
            except Exception as e:
                print(f"  GetPersonalSiteUrl ERROR: {e}")


if __name__ == "__main__":
    upn = sys.argv[1] if len(sys.argv) > 1 else "nyc-esri-gcc-t04-u01@WWPS319.onmicrosoft.com"
    asyncio.run(test(upn))

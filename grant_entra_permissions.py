"""Grant Microsoft Graph app-only permissions needed for Entra ID user provisioning.

Required permissions (all on Microsoft Graph - appId 00000003-0000-0000-c000-000000000000):
  - User.ReadWrite.All
  - Group.ReadWrite.All
  - UserAuthenticationMethod.ReadWrite.All  (TAP issuance)
  - Organization.Read.All                   (subscribed SKUs)
  - RoleManagement.ReadWrite.Directory      (admin role assignment)

Run with:
  $env:AZURE_TENANT_ID="..."; $env:AZURE_CLIENT_ID="..."; $env:AZURE_CLIENT_SECRET="..."
  python grant_entra_permissions.py

NOTE: The SPN used to run this must already be a Privileged Role Administrator
(or Global Admin) consented for the Graph permission `AppRoleAssignment.ReadWrite.All`.
Otherwise grant must be done via the Azure Portal -> Enterprise Applications ->
your app -> Permissions -> Grant admin consent.
"""
import os
import asyncio
import httpx
import msal


TENANT = os.environ["AZURE_TENANT_ID"]
CLIENT = os.environ["AZURE_CLIENT_ID"]
SECRET = os.environ["AZURE_CLIENT_SECRET"]

GRAPH_APPID = "00000003-0000-0000-c000-000000000000"

REQUIRED = [
    "User.ReadWrite.All",
    "Group.ReadWrite.All",
    "UserAuthenticationMethod.ReadWrite.All",
    "Organization.Read.All",
    "RoleManagement.ReadWrite.Directory",
]


async def main() -> None:
    app = msal.ConfidentialClientApplication(
        client_id=CLIENT,
        authority=f"https://login.microsoftonline.com/{TENANT}",
        client_credential=SECRET,
    )
    tok_resp = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in tok_resp:
        print(f"Token failed: {tok_resp}")
        return
    tok = tok_resp["access_token"]
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as c:
        # 1) Our SP
        sp = (await c.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{CLIENT}'",
            headers=H,
        )).json()["value"]
        if not sp:
            print(f"!! Service principal not found for client {CLIENT}")
            return
        our_sp_id = sp[0]["id"]
        print(f"Our SP id: {our_sp_id}")

        # 2) Microsoft Graph SP and its app roles
        graph_sp = (await c.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{GRAPH_APPID}'",
            headers=H,
        )).json()["value"][0]
        graph_sp_id = graph_sp["id"]
        roles_by_value = {r["value"]: r for r in graph_sp.get("appRoles", [])}
        print(f"Microsoft Graph SP id: {graph_sp_id}  ({len(roles_by_value)} app roles)")

        # 3) Existing assignments to skip duplicates
        existing = (await c.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals/{our_sp_id}/appRoleAssignments",
            headers=H,
        )).json().get("value", [])
        existing_role_ids = {a["appRoleId"] for a in existing if a.get("resourceId") == graph_sp_id}

        print()
        for perm in REQUIRED:
            role = roles_by_value.get(perm)
            if not role:
                print(f"  [skip ] {perm:42s} -> not found in Microsoft Graph appRoles")
                continue
            if role["id"] in existing_role_ids:
                print(f"  [ ok  ] {perm:42s} -> already granted")
                continue
            r = await c.post(
                f"https://graph.microsoft.com/v1.0/servicePrincipals/{our_sp_id}/appRoleAssignments",
                headers=H,
                json={
                    "principalId": our_sp_id,
                    "resourceId": graph_sp_id,
                    "appRoleId": role["id"],
                },
            )
            if r.status_code in (200, 201):
                print(f"  [grant] {perm:42s} -> granted")
            else:
                print(f"  [FAIL ] {perm:42s} -> HTTP {r.status_code}: {r.text[:200]}")

        print("\nDone. Allow ~30s for token refresh, then restart the app.")


if __name__ == "__main__":
    asyncio.run(main())

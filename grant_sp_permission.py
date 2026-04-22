.\provision_failed_users.ps1.\provision_failed_users.ps1"""Grant Sites.FullControl.All on Office 365 SharePoint Online programmatically."""
import os, asyncio, httpx, msal, json

TENANT = os.environ["AZURE_TENANT_ID"]
CLIENT = os.environ["AZURE_CLIENT_ID"]
SECRET = os.environ["AZURE_CLIENT_SECRET"]

SP_ONLINE_APPID = "00000003-0000-0ff1-ce00-000000000000"


async def main():
    app = msal.ConfidentialClientApplication(
        client_id=CLIENT,
        authority=f"https://login.microsoftonline.com/{TENANT}",
        client_credential=SECRET,
    )
    tok = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])["access_token"]
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as c:
        # Our SP
        our_sp = (await c.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{CLIENT}'",
            headers=H,
        )).json()["value"][0]
        our_sp_id = our_sp["id"]
        print(f"Our SP id: {our_sp_id}")

        # SP Online service principal
        spo = (await c.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{SP_ONLINE_APPID}'",
            headers=H,
        )).json()["value"][0]
        spo_id = spo["id"]
        print(f"SP Online SP id: {spo_id}")

        # Find Sites.FullControl.All role id inside SP Online
        roles_resp = await c.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals/{spo_id}?$select=appRoles",
            headers=H,
        )
        roles = roles_resp.json()["appRoles"]
        print(f"\nSP Online has {len(roles)} app roles. Looking for Sites.FullControl.All...")
        target = None
        for r in roles:
            if r["value"] == "Sites.FullControl.All":
                target = r
                break
        if not target:
            print("!! Sites.FullControl.All not found in SP Online appRoles")
            print("Available:", [r["value"] for r in roles[:20]])
            return
        print(f"  Role id: {target['id']}  ({target['displayName']})")

        # Grant the assignment
        print("\nAttempting POST to grant assignment...")
        resp = await c.post(
            f"https://graph.microsoft.com/v1.0/servicePrincipals/{our_sp_id}/appRoleAssignments",
            headers=H,
            json={
                "principalId": our_sp_id,
                "resourceId": spo_id,
                "appRoleId": target["id"],
            },
        )
        print(f"Status: {resp.status_code}")
        print(f"Body:   {resp.text[:500]}")


asyncio.run(main())

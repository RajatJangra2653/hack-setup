"""Check what app role assignments the service principal has."""
import os, asyncio, httpx, msal

TENANT = os.environ["AZURE_TENANT_ID"]
CLIENT = os.environ["AZURE_CLIENT_ID"]
SECRET = os.environ["AZURE_CLIENT_SECRET"]

SP_ONLINE_APPID = "00000003-0000-0ff1-ce00-000000000000"  # Office 365 SharePoint Online
GRAPH_APPID = "00000003-0000-0000-c000-000000000000"     # Microsoft Graph


async def main():
    app = msal.ConfidentialClientApplication(
        client_id=CLIENT,
        authority=f"https://login.microsoftonline.com/{TENANT}",
        client_credential=SECRET,
    )
    tok = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])["access_token"]
    H = {"Authorization": f"Bearer {tok}"}

    async with httpx.AsyncClient(timeout=30.0) as c:
        # Find our service principal
        sp = await c.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{CLIENT}'",
            headers=H,
        )
        data = sp.json()
        if not data.get("value"):
            print("!! Service principal for this app not found in tenant")
            return
        sp_id = data["value"][0]["id"]
        print(f"Our servicePrincipal id: {sp_id}")

        # List all app role assignments (what admin-consent has granted us)
        ar = await c.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp_id}/appRoleAssignments",
            headers=H,
        )
        assignments = ar.json().get("value", [])
        print(f"\nApp role assignments: {len(assignments)}")

        # Group by resource (which API)
        by_resource = {}
        for a in assignments:
            rid = a["resourceId"]
            by_resource.setdefault(rid, []).append(a)

        # Resolve resource names
        for rid, items in by_resource.items():
            r = await c.get(
                f"https://graph.microsoft.com/v1.0/servicePrincipals/{rid}?$select=appId,displayName",
                headers=H,
            )
            rd = r.json()
            print(f"\n  Resource: {rd.get('displayName')} (appId={rd.get('appId')})")
            # For each role, look up role name from the resource SP
            rsp = await c.get(
                f"https://graph.microsoft.com/v1.0/servicePrincipals/{rid}?$select=appRoles",
                headers=H,
            )
            role_map = {r["id"]: r["value"] for r in rsp.json().get("appRoles", [])}
            for a in items:
                role_id = a["appRoleId"]
                role_name = role_map.get(role_id, "?")
                print(f"    - {role_name}  ({role_id})")

        # Check specifically for SP Online
        print("\n--- Checking SP Online ---")
        sp_online = await c.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{SP_ONLINE_APPID}'",
            headers=H,
        )
        spo_data = sp_online.json().get("value", [])
        if spo_data:
            print(f"SP Online servicePrincipal exists in tenant (id={spo_data[0]['id']})")
            # Check if we have assignments against it
            has = any(a["resourceId"] == spo_data[0]["id"] for a in assignments)
            print(f"Our app has assignments on SP Online: {has}")
        else:
            print("!! SP Online service principal NOT found in tenant (unusual)")


asyncio.run(main())

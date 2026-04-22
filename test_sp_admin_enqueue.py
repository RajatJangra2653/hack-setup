"""Test the SharePoint Admin CreatePersonalSiteEnqueueBulk API directly."""
import os, asyncio, httpx, msal, json

TENANT = os.environ["AZURE_TENANT_ID"]
CLIENT = os.environ["AZURE_CLIENT_ID"]
SECRET = os.environ["AZURE_CLIENT_SECRET"]

ADMIN_URL = "https://WWPS319-admin.sharepoint.com"
USERS = [
    "nyc-esri-gcc-t04-u01@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t04-u02@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t04-u03@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t04-u04@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t04-u05@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t04-u06@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t04-u07@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t04-u08@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t04-u09@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t04-u10@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t05-u01@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t05-u02@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t05-u03@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t05-u04@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t05-u05@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t05-u06@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t05-u07@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t05-u08@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t05-u09@WWPS319.onmicrosoft.com",
    "nyc-esri-gcc-t05-u10@WWPS319.onmicrosoft.com",
]


async def main():
    app = msal.ConfidentialClientApplication(
        client_id=CLIENT,
        authority=f"https://login.microsoftonline.com/{TENANT}",
        client_credential=SECRET,
    )
    tok = app.acquire_token_for_client(scopes=[f"{ADMIN_URL}/.default"])["access_token"]

    # Decode and show roles
    import base64
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    claims = json.loads(base64.urlsafe_b64decode(p))
    print(f"Token aud:   {claims.get('aud')}")
    print(f"Token roles: {claims.get('roles')}")

    headers = {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/json;odata=verbose",
        "Content-Type": "application/json;odata=verbose",
    }

    # CreatePersonalSiteEnqueueBulk
    url = f"{ADMIN_URL}/_api/SP.UserProfiles.PeopleManager/CreatePersonalSiteEnqueueBulk"
    body = {"emailIDs": USERS}

    print(f"\nPOST {url}")
    print(f"Users: {len(USERS)}")
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(url, headers=headers, json=body)
        print(f"Status: {r.status_code}")
        print(f"Body:   {r.text[:800]}")


asyncio.run(main())

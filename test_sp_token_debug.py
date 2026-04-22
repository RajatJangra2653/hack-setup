"""Force fresh token, decode ALL claims, try every SP API variant."""
import os, sys, base64, json
import msal
import httpx
from urllib.parse import quote

TENANT = os.environ["AZURE_TENANT_ID"]
CLIENT = os.environ["AZURE_CLIENT_ID"]
SECRET = os.environ["AZURE_CLIENT_SECRET"]

def decode(tok):
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))

def get_token(resource):
    # Bypass MSAL cache by creating fresh app instance
    app = msal.ConfidentialClientApplication(
        client_id=CLIENT,
        authority=f"https://login.microsoftonline.com/{TENANT}",
        client_credential=SECRET,
    )
    # Also try to remove cached tokens
    for acct in app.get_accounts():
        app.remove_account(acct)
    r = app.acquire_token_for_client(scopes=[f"{resource}/.default"])
    if "access_token" not in r:
        print(f"  [FAIL] {r}")
        return None
    return r["access_token"]

def main():
    upn = sys.argv[1] if len(sys.argv) > 1 else "nyc-esri-gcc-t04-u01@WWPS319.onmicrosoft.com"
    tenant_name = upn.split("@")[1].split(".")[0]

    # Try BOTH admin site + -my site + root SPO
    for resource in [
        f"https://{tenant_name}.sharepoint.com",
        f"https://{tenant_name}-my.sharepoint.com",
        f"https://{tenant_name}-admin.sharepoint.com",
    ]:
        print(f"\n=== Resource: {resource} ===")
        tok = get_token(resource)
        if not tok:
            continue
        claims = decode(tok)
        print(f"  aud:   {claims.get('aud')}")
        print(f"  roles: {claims.get('roles')}")
        print(f"  app:   {claims.get('app_displayname')}")
        print(f"  appid: {claims.get('appid')}")
        print(f"  scp:   {claims.get('scp')}")
        print(f"  iss:   {claims.get('iss')}")
        print(f"  tid:   {claims.get('tid')}")
        # Print all claims for debugging
        print(f"  ALL CLAIMS: {list(claims.keys())}")

    # Also check Graph
    print(f"\n=== Resource: https://graph.microsoft.com ===")
    tok = get_token("https://graph.microsoft.com")
    if tok:
        claims = decode(tok)
        print(f"  roles: {claims.get('roles')}")

if __name__ == "__main__":
    main()

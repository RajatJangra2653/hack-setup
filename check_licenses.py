"""Quick script to check license assignments for users."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from onedrive_provisioner.auth import MsalTokenProvider
from onedrive_provisioner.graph import GraphClient
from onedrive_provisioner.config import AzureConfig


async def main():
    cfg = AzureConfig(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )
    tp = MsalTokenProvider(cfg)

    # Sample: 1 successful user, 1 from t04, 1 from t05
    users = [
        "nyc-esri-gcc-t01-u01@WWPS319.onmicrosoft.com",
        "nyc-esri-gcc-t04-u01@WWPS319.onmicrosoft.com",
        "nyc-esri-gcc-t05-u01@WWPS319.onmicrosoft.com",
    ]

    async with GraphClient(tp) as g:
        for u in users:
            try:
                lic = await g.get(f"/users/{u}/licenseDetails")
                plans = [entry["skuPartNumber"] for entry in lic.get("value", [])]
                tag = "OK" if plans else "NO LICENSES"
                print(f"  {u}:  {tag}  {plans}")
            except Exception as e:
                print(f"  {u}:  ERROR  {e}")


if __name__ == "__main__":
    asyncio.run(main())

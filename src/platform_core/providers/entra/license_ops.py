"""Entra ID license operations."""

from __future__ import annotations

import logging

from platform_core.core import JsonDict
from platform_core.models.license import LICENSE_CATALOG
from platform_core.providers.graph.client import GraphClient

logger = logging.getLogger(__name__)


class LicenseOps:
    """License resolution, assignment, and verification."""

    def __init__(self, client: GraphClient) -> None:
        self._client = client
        self._sku_cache: list[JsonDict] | None = None

    async def get_subscribed_skus(self) -> list[JsonDict]:
        if self._sku_cache is None:
            data = await self._client.get("/subscribedSkus")
            self._sku_cache = data.get("value", [])
        return self._sku_cache

    async def resolve(self, friendly_names: list[str]) -> dict[str, tuple[str, str]]:
        """Resolve friendly license names to (skuId, skuPartNumber)."""
        skus = await self.get_subscribed_skus()
        result: dict[str, tuple[str, str]] = {}
        for name in friendly_names:
            part = LICENSE_CATALOG.get(name, name)
            for sku in skus:
                if sku.get("skuPartNumber", "").upper() == part.upper():
                    result[name] = (sku["skuId"], sku["skuPartNumber"])
                    break
            else:
                logger.warning("License '%s' (part=%s) not found in tenant SKUs", name, part)
        return result

    async def check_availability(self, friendly_names: list[str], required_seats: int) -> list[JsonDict]:
        """Check if enough seats are available for requested licenses."""
        skus = await self.get_subscribed_skus()
        resolved = await self.resolve(friendly_names)
        checks: list[JsonDict] = []
        for name, (sku_id, part) in resolved.items():
            for sku in skus:
                if sku["skuId"] == sku_id:
                    total = sku.get("prepaidUnits", {}).get("enabled", 0)
                    consumed = sku.get("consumedUnits", 0)
                    available = total - consumed
                    checks.append({
                        "license": name,
                        "sku": part,
                        "available": available,
                        "required": required_seats,
                        "sufficient": available >= required_seats,
                    })
                    break
        return checks

    async def assign(self, user_id: str, sku_ids: list[str]) -> None:
        """Assign licenses to a user."""
        body = {
            "addLicenses": [{"skuId": sid} for sid in sku_ids],
            "removeLicenses": [],
        }
        await self._client.post(f"/users/{user_id}/assignLicense", json=body)

    async def remove(self, user_id: str, sku_ids: list[str]) -> None:
        body = {
            "addLicenses": [],
            "removeLicenses": sku_ids,
        }
        await self._client.post(f"/users/{user_id}/assignLicense", json=body)

    async def get_user_licenses(self, user_id: str) -> list[JsonDict]:
        data = await self._client.get(f"/users/{user_id}/licenseDetails")
        return data.get("value", [])

    async def verify_and_repair(self, user_id: str, expected_sku_ids: list[str]) -> bool:
        """Check current licenses and assign any missing ones."""
        current = await self.get_user_licenses(user_id)
        current_ids = {lic["skuId"] for lic in current}
        missing = [sid for sid in expected_sku_ids if sid not in current_ids]
        if missing:
            await self.assign(user_id, missing)
            return False  # Had to repair
        return True  # All present

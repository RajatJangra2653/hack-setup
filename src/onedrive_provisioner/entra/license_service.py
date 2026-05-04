"""Resolve friendly license names to SKU IDs and assign them."""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger
from .models import LICENSE_CATALOG

logger = get_logger(__name__)


class LicenseService:
    def __init__(self, graph: GraphClient) -> None:
        self._g = graph
        self._sku_cache: Optional[List[dict]] = None

    async def _load_skus(self) -> List[dict]:
        if self._sku_cache is None:
            data = await self._g.get("/subscribedSkus")
            self._sku_cache = data.get("value", [])
            logger.info("entra.license.skus_loaded", count=len(self._sku_cache))
        return self._sku_cache

    async def resolve(self, friendly_names: List[str]) -> Dict[str, Tuple[str, str]]:
        """Resolve friendly names → {friendly: (skuId, skuPartNumber)}.

        Skips names that don't match any subscribed SKU.
        """
        skus = await self._load_skus()
        out: Dict[str, Tuple[str, str]] = {}
        for friendly in friendly_names:
            tokens = LICENSE_CATALOG.get(friendly, [friendly])
            match = None
            for s in skus:
                part = s.get("skuPartNumber", "")
                if any(tok.lower() in part.lower() for tok in tokens):
                    # Prefer SKUs with available units
                    enabled = s.get("prepaidUnits", {}).get("enabled", 0)
                    consumed = s.get("consumedUnits", 0)
                    if enabled > consumed:
                        match = s
                        break
                    if match is None:
                        match = s
            if match:
                out[friendly] = (match["skuId"], match["skuPartNumber"])
            else:
                logger.warning("entra.license.unmatched", friendly=friendly,
                               hint="no subscribed SKU matched the catalog tokens")
        return out

    async def check_availability(
        self, friendly_names: List[str], required_seats: int,
    ) -> List[dict]:
        """Pre-flight: per friendly name, report available seats vs required.

        Returns list of {name, matched, partNumber, enabled, consumed, available, required, ok}.
        """
        if not friendly_names:
            return []
        skus = await self._load_skus()
        out: List[dict] = []
        for friendly in friendly_names:
            tokens = LICENSE_CATALOG.get(friendly, [friendly])
            best = None
            for s in skus:
                part = s.get("skuPartNumber", "")
                if any(tok.lower() in part.lower() for tok in tokens):
                    enabled = s.get("prepaidUnits", {}).get("enabled", 0)
                    consumed = s.get("consumedUnits", 0)
                    avail = enabled - consumed
                    if best is None or avail > (best["enabled"] - best["consumed"]):
                        best = {"sku": s, "enabled": enabled, "consumed": consumed}
            if not best:
                out.append({
                    "name": friendly, "matched": False,
                    "partNumber": None, "enabled": 0, "consumed": 0,
                    "available": 0, "required": required_seats, "ok": False,
                    "message": "No subscribed SKU matched",
                })
                continue
            available = best["enabled"] - best["consumed"]
            out.append({
                "name": friendly,
                "matched": True,
                "partNumber": best["sku"].get("skuPartNumber"),
                "skuId": best["sku"].get("skuId"),
                "enabled": best["enabled"],
                "consumed": best["consumed"],
                "available": available,
                "required": required_seats,
                "ok": available >= required_seats,
            })
        return out

    async def assign(self, user_id: str, sku_ids: List[str]) -> List[str]:
        """Assign SKU IDs to user. Returns list of successfully assigned SKU IDs."""
        if not sku_ids:
            return []
        body = {
            "addLicenses": [{"skuId": sid, "disabledPlans": []} for sid in sku_ids],
            "removeLicenses": [],
        }
        try:
            await self._g.post(f"/users/{user_id}/assignLicense", json=body)
            logger.info("entra.license.assigned", user_id=user_id, skus=sku_ids)
            return sku_ids
        except GraphError as exc:
            logger.warning("entra.license.failed", user_id=user_id,
                           status=exc.status, code=exc.code, msg=str(exc))
            return []

    async def get_user_licenses(self, user_id: str) -> List[str]:
        """Return the list of skuId strings currently assigned to a user."""
        try:
            data = await self._g.get(f"/users/{user_id}/licenseDetails")
            return [lic["skuId"] for lic in data.get("value", [])]
        except GraphError:
            return []

    async def verify_and_repair(
        self, user_id: str, expected_sku_ids: List[str],
    ) -> dict:
        """Check which expected SKUs a user has; reassign any missing ones.

        Returns {"assigned": [...], "missing": [...], "repaired": [...], "still_missing": [...]}.
        """
        current = set(await self.get_user_licenses(user_id))
        already = [sid for sid in expected_sku_ids if sid in current]
        missing = [sid for sid in expected_sku_ids if sid not in current]

        if not missing:
            return {"assigned": already, "missing": [], "repaired": [], "still_missing": []}

        repaired = await self.assign(user_id, missing)
        still_missing = [sid for sid in missing if sid not in repaired]
        return {
            "assigned": already,
            "missing": missing,
            "repaired": repaired,
            "still_missing": still_missing,
        }

"""Auto-detect tenant info: default UPN domain + TAP policy."""
from __future__ import annotations

from typing import Optional, Tuple

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger

logger = get_logger(__name__)


class TenantService:
    def __init__(self, graph: GraphClient) -> None:
        self._g = graph

    async def detect_default_domain(self) -> str:
        """Return tenant's verified default domain (e.g. 'WWPS319.onmicrosoft.com')."""
        data = await self._g.get("/organization?$select=verifiedDomains")
        orgs = data.get("value", [])
        if not orgs:
            raise GraphError(404, "NoOrg", "No organization found in tenant")
        domains = orgs[0].get("verifiedDomains", []) or []
        # Prefer "isDefault": True; fall back to first .onmicrosoft.com; then any
        for d in domains:
            if d.get("isDefault"):
                return d["name"]
        for d in domains:
            if d.get("name", "").endswith(".onmicrosoft.com"):
                return d["name"]
        if domains:
            return domains[0]["name"]
        raise GraphError(404, "NoDomain", "Tenant has no verified domains")

    async def list_verified_domains(self) -> list[str]:
        """Return list of all verified domain names in the tenant."""
        data = await self._g.get("/organization?$select=verifiedDomains")
        orgs = data.get("value", [])
        if not orgs:
            return []
        return [d["name"] for d in (orgs[0].get("verifiedDomains") or []) if d.get("name")]

    async def get_tap_max_lifetime(self) -> Optional[int]:
        """Return TAP max lifetime in minutes, or None if TAP policy is disabled.

        Reads /policies/authenticationMethodsPolicy/authenticationMethodConfigurations/TemporaryAccessPass.
        """
        try:
            cfg = await self._g.get(
                "/policies/authenticationMethodsPolicy/"
                "authenticationMethodConfigurations/TemporaryAccessPass"
            )
        except GraphError as exc:
            logger.warning("entra.tap.policy_unreadable", status=exc.status, msg=str(exc))
            return None
        if cfg.get("state") != "enabled":
            return None
        # maximumLifetimeInMinutes / minimumLifetimeInMinutes
        max_min = cfg.get("maximumLifetimeInMinutes")
        if isinstance(max_min, int) and max_min > 0:
            return max_min
        return None

    async def get_tenant_info(self) -> Tuple[str, Optional[int]]:
        """Convenience: return (default_domain, tap_max_lifetime_minutes)."""
        domain = await self.detect_default_domain()
        tap_max = await self.get_tap_max_lifetime()
        return domain, tap_max

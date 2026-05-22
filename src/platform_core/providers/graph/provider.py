"""Microsoft Graph provider — high-level Graph operations wrapped
as a Provider implementation."""

from __future__ import annotations

from typing import Any

from platform_core.core import JsonDict
from platform_core.events import EventBus
from platform_core.providers.base import ProviderBase
from platform_core.providers.graph.client import GraphClient


class GraphProvider(ProviderBase):
    """Provider wrapping raw Microsoft Graph API access.

    Other providers (Entra, OneDrive) depend on GraphClient
    but do NOT depend on GraphProvider.  This provider exists
    for direct Graph operations that don't fit neatly into
    a domain-specific provider.
    """

    name = "graph"

    def __init__(
        self,
        client: GraphClient,
        *,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(event_bus=event_bus)
        self.client = client

    async def validate(self) -> bool:
        """Check Graph connectivity by reading /organization."""
        try:
            data = await self.client.get("/organization")
            return bool(data.get("value"))
        except Exception as exc:
            self._log.error("Graph validation failed: %s", exc)
            return False

    async def provision(self, desired: JsonDict, *, dry_run: bool = False, on_progress: Any = None) -> JsonDict:
        # Graph provider doesn't provision — Entra/OneDrive do
        return {"status": "noop", "message": "Use domain-specific providers for provisioning"}

    async def reconcile(self, desired: JsonDict, actual: JsonDict) -> JsonDict:
        return {"status": "noop"}

    async def cleanup(self, state: JsonDict, *, dry_run: bool = False) -> JsonDict:
        return {"status": "noop"}

    # ── Graph-specific queries ───────────────────────────────────

    async def get_organization(self) -> JsonDict:
        data = await self.client.get("/organization")
        return data.get("value", [{}])[0]

    async def get_tenant_domains(self) -> list[str]:
        data = await self.client.get("/domains")
        return [d["id"] for d in data.get("value", [])]

    async def get_subscribed_skus(self) -> list[JsonDict]:
        data = await self.client.get("/subscribedSkus")
        return data.get("value", [])

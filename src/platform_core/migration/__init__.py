"""Migration bridge — compatibility layer between legacy onedrive_provisioner
and the new platform_core architecture.

This module provides adapters that let the new platform_core services
consume existing onedrive_provisioner components, enabling incremental
migration without breaking the production Flask app.

Migration strategy:
  Phase 1: Bridge — new platform calls old services through adapters
  Phase 2: Parallel — both stacks run, bridge validates parity
  Phase 3: Cutover — old code deprecated, bridge removed
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class LegacyGraphAdapter:
    """Adapt old GraphClient to new platform_core provider interface."""

    def __init__(self, legacy_client: Any) -> None:
        self._client = legacy_client

    async def get(self, url: str, **kwargs) -> dict:
        """Bridge sync GraphClient.get to async."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._client.get(url, **kwargs))

    async def post(self, url: str, body: dict, **kwargs) -> dict:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._client.post(url, body, **kwargs))

    async def patch(self, url: str, body: dict, **kwargs) -> dict:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._client.patch(url, body, **kwargs))

    async def delete(self, url: str, **kwargs) -> dict:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._client.delete(url, **kwargs))


class LegacyStateAdapter:
    """Adapt HackStateManager to new platform_core repository interface."""

    def __init__(self, state_manager: Any) -> None:
        self._state = state_manager

    async def get_hack(self, prefix: str) -> dict | None:
        """Load hack state from blob storage."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._state.load_state(prefix))

    async def save_hack(self, prefix: str, state: dict) -> None:
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._state.save_state(prefix, state))

    async def list_hacks(self) -> list[str]:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._state.list_hacks)


class LegacyAuthAdapter:
    """Adapt old MsalProvider to new TokenProvider interface."""

    def __init__(self, msal_provider: Any) -> None:
        self._provider = msal_provider

    async def get_token(self, scopes: list[str] | None = None) -> str:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._provider.get_token)

    async def get_graph_token(self) -> str:
        return await self.get_token()


def create_bridge(
    *,
    graph_client: Any = None,
    state_manager: Any = None,
    auth_provider: Any = None,
) -> dict[str, Any]:
    """Create all bridge adapters from legacy components.

    Usage:
        from onedrive_provisioner.graph.client import GraphClient
        from onedrive_provisioner.storage import HackStateManager
        from onedrive_provisioner.auth.msal_provider import MsalProvider

        bridge = create_bridge(
            graph_client=GraphClient(...),
            state_manager=HackStateManager(...),
            auth_provider=MsalProvider(...),
        )
    """
    adapters = {}
    if graph_client:
        adapters["graph"] = LegacyGraphAdapter(graph_client)
    if state_manager:
        adapters["state"] = LegacyStateAdapter(state_manager)
    if auth_provider:
        adapters["auth"] = LegacyAuthAdapter(auth_provider)
    logger.info("Migration bridge created with adapters: %s", list(adapters.keys()))
    return adapters

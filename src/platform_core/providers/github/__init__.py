"""GitHub EMU provider."""

from __future__ import annotations

import logging
from typing import Any

from platform_core.core import JsonDict
from platform_core.events import EventBus
from platform_core.providers.base import ProviderBase
from platform_core.providers.graph.client import GraphClient

logger = logging.getLogger(__name__)


class GitHubProvider(ProviderBase):
    """GitHub EMU provider — add users to EMU group and trigger sync."""

    name = "github"

    def __init__(
        self,
        graph_client: GraphClient,
        *,
        emu_group_id: str = "",
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(event_bus=event_bus)
        self._graph = graph_client
        self._emu_group_id = emu_group_id

    async def validate(self) -> bool:
        if not self._emu_group_id:
            self._log.warning("No EMU group ID configured")
            return False
        try:
            await self._graph.get(f"/groups/{self._emu_group_id}")
            return True
        except Exception:
            return False

    async def provision(self, desired: JsonDict, *, dry_run: bool = False, on_progress: Any = None) -> JsonDict:
        """Add users to GitHub EMU group."""
        users = desired.get("users", [])
        if dry_run:
            return {"status": "dry_run", "users": len(users)}

        results: list[JsonDict] = []
        for user in users:
            user_id = user.get("user_id", "")
            upn = user.get("user_principal_name", "")
            try:
                await self._add_to_emu_group(user_id)
                results.append({"upn": upn, "status": "added"})
            except Exception as exc:
                results.append({"upn": upn, "status": "failed", "error": str(exc)})

        return {"status": "completed", "results": results}

    async def reconcile(self, desired: JsonDict, actual: JsonDict) -> JsonDict:
        return {"status": "noop"}

    async def cleanup(self, state: JsonDict, *, dry_run: bool = False) -> JsonDict:
        # GitHub cleanup = remove from EMU group
        return {"status": "noop", "message": "GitHub cleanup via group removal"}

    async def _add_to_emu_group(self, user_id: str) -> None:
        body = {"@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{user_id}"}
        try:
            await self._graph.post(f"/groups/{self._emu_group_id}/members/$ref", json=body)
        except Exception as exc:
            if "already exist" not in str(exc).lower():
                raise

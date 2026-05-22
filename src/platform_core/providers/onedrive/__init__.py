"""OneDrive provider — provisioning and file upload."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from platform_core.core import JsonDict
from platform_core.events import EventBus
from platform_core.providers.base import ProviderBase
from platform_core.providers.graph.client import GraphClient

logger = logging.getLogger(__name__)

_PROVISION_CODES = frozenset({
    "resourcenotfound", "itemnotfound",
    "mysitenotfound", "mysiteurlgenerationinprogress",
})


class OneDriveProvider(ProviderBase):
    """OneDrive provider — ensure drives are provisioned and upload files."""

    name = "onedrive"

    def __init__(
        self,
        graph_client: GraphClient,
        *,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(event_bus=event_bus)
        self._graph = graph_client

    async def validate(self) -> bool:
        try:
            await self._graph.get("/me/drive")
            return True
        except Exception:
            return False

    async def provision(self, desired: JsonDict, *, dry_run: bool = False, on_progress: Any = None) -> JsonDict:
        """Ensure OneDrive is provisioned for each user."""
        users = desired.get("users", [])
        if dry_run:
            return {"status": "dry_run", "users": len(users)}

        results: list[JsonDict] = []
        for user in users:
            user_id = user.get("user_id", user.get("userId", ""))
            upn = user.get("user_principal_name", user.get("userPrincipalName", ""))
            try:
                drive = await self._ensure_drive(user_id)
                results.append({"upn": upn, "drive_id": drive.get("id", ""), "status": "ready"})
            except Exception as exc:
                results.append({"upn": upn, "status": "failed", "error": str(exc)})

        return {"status": "completed", "results": results}

    async def reconcile(self, desired: JsonDict, actual: JsonDict) -> JsonDict:
        return {"status": "noop"}

    async def cleanup(self, state: JsonDict, *, dry_run: bool = False) -> JsonDict:
        # OneDrive cleanup happens through user deletion
        return {"status": "noop", "message": "OneDrive cleaned up via user deletion"}

    # ── OneDrive-specific operations ─────────────────────────────

    async def _ensure_drive(self, user_id: str, *, max_attempts: int = 10) -> JsonDict:
        """Ensure OneDrive is provisioned for a user with retry."""
        for attempt in range(max_attempts):
            try:
                data = await self._graph.get(f"/users/{user_id}/drive")
                if data.get("id"):
                    return data
            except Exception as exc:
                err_str = str(exc).lower()
                if any(code in err_str for code in _PROVISION_CODES):
                    if attempt < max_attempts - 1:
                        delay = min(2 ** attempt, 30)
                        logger.info("OneDrive not ready for %s, retrying in %ds", user_id, delay)
                        await asyncio.sleep(delay)
                        continue
                raise
        raise RuntimeError(f"OneDrive not provisioned for {user_id} after {max_attempts} attempts")

    async def upload_file(
        self,
        user_id: str,
        dest_path: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> JsonDict:
        """Upload a small file (< 4MB) via simple PUT."""
        path = dest_path.lstrip("/")
        return await self._graph.put(
            f"/users/{user_id}/drive/root:/{path}:/content",
            content=content,
            headers={"Content-Type": content_type},
        )

    async def create_upload_session(
        self,
        user_id: str,
        dest_path: str,
    ) -> str:
        """Create an upload session for large files. Returns the upload URL."""
        path = dest_path.lstrip("/")
        data = await self._graph.post(
            f"/users/{user_id}/drive/root:/{path}:/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        return data["uploadUrl"]

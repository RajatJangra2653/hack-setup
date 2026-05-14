"""Inventory service — persistent tracking of all platform-managed
resources across hacks, providers, and resource types.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from platform_core.events import DomainEvent, EventBus
from platform_core.models.inventory import InventoryItem, InventorySnapshot
from platform_core.models.resource import ResourceType

logger = logging.getLogger(__name__)


class InventoryService:
    """Centralized inventory of all managed resources.

    The platform ALWAYS knows:
      - What exists
      - Who owns it
      - Which hack it belongs to
      - Expiration status
      - Drift state
    """

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        persist_fn: Any = None,
    ) -> None:
        self._items: dict[str, InventoryItem] = {}
        self._persist_fn = persist_fn

        if event_bus:
            event_bus.subscribe("user.created", self._on_user_created)
            event_bus.subscribe("user.deleted", self._on_user_deleted)
            event_bus.subscribe("cleanup.completed", self._on_cleanup)

    # ── CRUD ─────────────────────────────────────────────────────

    async def add(self, item: InventoryItem) -> None:
        self._items[item.resource_id] = item
        if self._persist_fn:
            await self._persist_fn("add", item)

    async def remove(self, resource_id: str) -> bool:
        removed = self._items.pop(resource_id, None)
        if removed and self._persist_fn:
            await self._persist_fn("remove", removed)
        return removed is not None

    async def update(self, resource_id: str, **kwargs: Any) -> InventoryItem | None:
        item = self._items.get(resource_id)
        if item:
            for k, v in kwargs.items():
                if hasattr(item, k):
                    setattr(item, k, v)
        return item

    async def get(self, resource_id: str) -> InventoryItem | None:
        return self._items.get(resource_id)

    # ── Queries ──────────────────────────────────────────────────

    async def list_by_hack(self, prefix: str) -> list[InventoryItem]:
        return [i for i in self._items.values() if i.hack_prefix == prefix]

    async def list_by_type(self, resource_type: ResourceType) -> list[InventoryItem]:
        return [i for i in self._items.values() if i.resource_type == resource_type]

    async def list_by_provider(self, provider: str) -> list[InventoryItem]:
        return [i for i in self._items.values() if i.provider == provider]

    async def list_expired(self) -> list[InventoryItem]:
        return [i for i in self._items.values() if i.is_expired]

    async def list_drifted(self) -> list[InventoryItem]:
        return [i for i in self._items.values() if i.drift_status == "drifted"]

    # ── Snapshot ─────────────────────────────────────────────────

    async def snapshot(self, prefix: str = "") -> InventorySnapshot:
        """Generate a point-in-time inventory snapshot."""
        items = list(self._items.values())
        if prefix:
            items = [i for i in items if i.hack_prefix == prefix]

        by_type: dict[str, int] = {}
        by_provider: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for item in items:
            by_type[item.resource_type.value] = by_type.get(item.resource_type.value, 0) + 1
            by_provider[item.provider] = by_provider.get(item.provider, 0) + 1
            by_status[item.status] = by_status.get(item.status, 0) + 1

        prefixes = {i.hack_prefix for i in items}

        return InventorySnapshot(
            hack_prefix=prefix,
            total_items=len(items),
            items_by_type=by_type,
            items_by_provider=by_provider,
            items_by_status=by_status,
            items=items,
            total_hacks=len(prefixes),
            total_users=by_type.get("user", 0),
            total_groups=by_type.get("group", 0),
            total_licenses=by_type.get("license", 0),
            total_subscriptions=by_type.get("subscription", 0),
            expired_items=sum(1 for i in items if i.is_expired),
            drifted_items=sum(1 for i in items if i.drift_status == "drifted"),
        )

    # ── Event handlers ───────────────────────────────────────────

    async def _on_user_created(self, event: DomainEvent) -> None:
        await self.add(InventoryItem(
            resource_id=event.data.get("user_id", str(event.id)),
            resource_type=ResourceType.USER,
            hack_prefix=event.hack_prefix,
            display_name=event.data.get("user_principal_name", ""),
            provider="entra",
        ))

    async def _on_user_deleted(self, event: DomainEvent) -> None:
        user_id = event.data.get("user_id", "")
        if user_id:
            await self.remove(user_id)

    async def _on_cleanup(self, event: DomainEvent) -> None:
        # Mark all items for this prefix as deleted
        for item in list(self._items.values()):
            if item.hack_prefix == event.hack_prefix:
                item.status = "deleted"

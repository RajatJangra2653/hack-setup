"""Inventory snapshot and item models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from platform_core.models.resource import ResourceType


class InventoryItem(BaseModel):
    """A single item in the platform inventory."""

    resource_id: str
    resource_type: ResourceType
    hack_prefix: str
    display_name: str = ""
    owner: str = ""
    team: str = ""
    provider: str = ""
    status: str = "active"
    created_at: datetime | None = None
    expires_at: datetime | None = None
    drift_status: str = "unknown"  # unknown | clean | drifted
    last_checked: datetime | None = None
    metadata: dict = Field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at


class InventorySnapshot(BaseModel):
    """Point-in-time snapshot of platform inventory."""

    taken_at: datetime = Field(default_factory=datetime.utcnow)
    hack_prefix: str = ""
    total_items: int = 0
    items_by_type: dict[str, int] = Field(default_factory=dict)
    items_by_provider: dict[str, int] = Field(default_factory=dict)
    items_by_status: dict[str, int] = Field(default_factory=dict)
    items: list[InventoryItem] = Field(default_factory=list)

    # ── Aggregate metrics ────────────────────────────────────────
    total_hacks: int = 0
    total_users: int = 0
    total_groups: int = 0
    total_licenses: int = 0
    total_subscriptions: int = 0
    expired_items: int = 0
    drifted_items: int = 0

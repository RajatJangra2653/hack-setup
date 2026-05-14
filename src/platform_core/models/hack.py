"""Hack environment and configuration models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class HackStatus(str, Enum):
    DRAFT = "draft"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    READONLY = "readonly"
    CLEANUP = "cleanup"
    ARCHIVED = "archived"
    FAILED = "failed"


class HackConfig(BaseModel):
    """Desired-state declaration for a hack environment."""

    name: str = Field(..., min_length=1, max_length=200)
    prefix: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]*-$")
    domain: str = ""
    created_by: str = ""

    # ── People ───────────────────────────────────────────────────
    user_count: int = Field(0, ge=0)
    admin_count: int = Field(0, ge=0)
    teams: list[TeamConfig] = Field(default_factory=list)

    # ── Entitlements ─────────────────────────────────────────────
    licenses: list[str] = Field(default_factory=list)
    azure_subscriptions: list[str] = Field(default_factory=list)
    rbac_role: str = "Contributor"

    # ── GitHub ───────────────────────────────────────────────────
    github_enabled: bool = False
    github_copilot: bool = False

    # ── Schedule ─────────────────────────────────────────────────
    hack_start_date: datetime | None = None
    hack_date: datetime | None = None
    readonly_date: datetime | None = None
    delete_date: datetime | None = None

    # ── Upload ───────────────────────────────────────────────────
    upload_source: str = ""
    upload_destination: str = "/HackFiles"

    # ── TAP ──────────────────────────────────────────────────────
    tap_lifetime_minutes: int = Field(480, ge=10, le=43200)
    tap_one_time: bool = False


class TeamConfig(BaseModel):
    """Team definition within a hack."""

    name: str
    code: str = ""
    size: int = Field(1, ge=1)
    admin_count: int = Field(0, ge=0)
    subscription_id: str = ""


class HackEnvironment(BaseModel):
    """Complete state of a hack environment — the aggregate root."""

    id: UUID = Field(default_factory=uuid4)
    config: HackConfig
    status: HackStatus = HackStatus.DRAFT
    schema_version: str = "2.0"

    # ── Timestamps ───────────────────────────────────────────────
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    provisioned_at: datetime | None = None
    archived_at: datetime | None = None

    # ── Populated during provisioning ────────────────────────────
    users: list[Any] = Field(default_factory=list)  # list[HackUser]
    groups: list[Any] = Field(default_factory=list)  # list[SecurityGroup]
    subscriptions: list[Any] = Field(default_factory=list)
    resources: list[Any] = Field(default_factory=list)  # list[TrackedResource]

    # ── Counters ─────────────────────────────────────────────────
    total_users: int = 0
    provisioned_users: int = 0
    failed_users: int = 0

    @property
    def prefix(self) -> str:
        return self.config.prefix

    @property
    def is_active(self) -> bool:
        return self.status in (HackStatus.ACTIVE, HackStatus.PROVISIONING)

    @property
    def is_readonly(self) -> bool:
        return self.status == HackStatus.READONLY

    def transition(self, new_status: HackStatus) -> None:
        """Validate and apply a status transition."""
        valid = _TRANSITIONS.get(self.status, set())
        if new_status not in valid:
            raise ValueError(
                f"Cannot transition from {self.status.value} to {new_status.value}"
            )
        self.status = new_status
        self.updated_at = datetime.utcnow()


# Valid state machine transitions
_TRANSITIONS: dict[HackStatus, set[HackStatus]] = {
    HackStatus.DRAFT: {HackStatus.PROVISIONING, HackStatus.ARCHIVED},
    HackStatus.PROVISIONING: {HackStatus.ACTIVE, HackStatus.FAILED},
    HackStatus.ACTIVE: {HackStatus.READONLY, HackStatus.CLEANUP, HackStatus.FAILED},
    HackStatus.READONLY: {HackStatus.CLEANUP, HackStatus.ACTIVE},
    HackStatus.CLEANUP: {HackStatus.ARCHIVED, HackStatus.FAILED},
    HackStatus.FAILED: {HackStatus.DRAFT, HackStatus.CLEANUP, HackStatus.ARCHIVED},
    HackStatus.ARCHIVED: set(),
}

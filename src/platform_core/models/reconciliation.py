"""Reconciliation and drift detection models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ChangeAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    ENABLE = "enable"
    DISABLE = "disable"
    ASSIGN = "assign"
    REVOKE = "revoke"
    SKIP = "skip"


class DriftCategory(str, Enum):
    USER_MISSING = "user_missing"
    USER_EXTRA = "user_extra"
    USER_MODIFIED = "user_modified"
    GROUP_MISSING = "group_missing"
    GROUP_EXTRA = "group_extra"
    LICENSE_MISSING = "license_missing"
    LICENSE_EXTRA = "license_extra"
    RBAC_MISSING = "rbac_missing"
    RBAC_EXTRA = "rbac_extra"


class DriftItem(BaseModel):
    """A single drift observation."""

    category: DriftCategory
    resource_type: str
    identifier: str
    display_name: str = ""
    expected: str = ""
    actual: str = ""
    detail: str = ""


class DriftReport(BaseModel):
    """Result of comparing desired vs actual state."""

    id: UUID = Field(default_factory=uuid4)
    hack_prefix: str
    checked_at: datetime = Field(default_factory=datetime.utcnow)
    has_drift: bool = False
    items: list[DriftItem] = Field(default_factory=list)

    # ── Summaries ────────────────────────────────────────────────
    state_user_count: int = 0
    live_user_count: int = 0
    state_group_count: int = 0
    live_group_count: int = 0
    missing_users: list[str] = Field(default_factory=list)
    extra_users: list[str] = Field(default_factory=list)
    modified_users: list[str] = Field(default_factory=list)
    missing_groups: list[str] = Field(default_factory=list)
    extra_groups: list[str] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.has_drift:
            return "No drift detected — state matches live environment."
        parts = []
        if self.missing_users:
            parts.append(f"{len(self.missing_users)} missing users")
        if self.extra_users:
            parts.append(f"{len(self.extra_users)} extra users")
        if self.modified_users:
            parts.append(f"{len(self.modified_users)} modified users")
        if self.missing_groups:
            parts.append(f"{len(self.missing_groups)} missing groups")
        if self.extra_groups:
            parts.append(f"{len(self.extra_groups)} extra groups")
        return "Drift detected: " + ", ".join(parts)


class ChangeItem(BaseModel):
    """A planned change within a reconciliation plan."""

    action: ChangeAction
    resource_type: str
    identifier: str
    display_name: str = ""
    provider: str = ""
    details: dict = Field(default_factory=dict)
    estimated_duration_seconds: float = 0.0
    risk_level: str = "low"  # low | medium | high | critical

    # ── Execution result ─────────────────────────────────────────
    executed: bool = False
    success: bool = False
    error: str = ""


class ReconciliationPlan(BaseModel):
    """A plan to reconcile desired vs actual state."""

    id: UUID = Field(default_factory=uuid4)
    hack_prefix: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    dry_run: bool = True
    drift_report_id: UUID | None = None

    changes: list[ChangeItem] = Field(default_factory=list)
    approved: bool = False
    approved_by: str = ""
    approved_at: datetime | None = None

    # ── Execution state ──────────────────────────────────────────
    executed: bool = False
    executed_at: datetime | None = None
    total_changes: int = 0
    applied_changes: int = 0
    failed_changes: int = 0
    skipped_changes: int = 0

    @property
    def summary(self) -> str:
        return (
            f"{len(self.changes)} changes: "
            f"{sum(1 for c in self.changes if c.action == ChangeAction.CREATE)} create, "
            f"{sum(1 for c in self.changes if c.action == ChangeAction.DELETE)} delete, "
            f"{sum(1 for c in self.changes if c.action == ChangeAction.UPDATE)} update"
        )

    @property
    def has_destructive_changes(self) -> bool:
        return any(c.action == ChangeAction.DELETE for c in self.changes)

    @property
    def has_high_risk(self) -> bool:
        return any(c.risk_level in ("high", "critical") for c in self.changes)

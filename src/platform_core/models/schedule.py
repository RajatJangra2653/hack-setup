"""Schedule and lifecycle policy models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ScheduleAction(str, Enum):
    PROVISION = "provision"
    CLEANUP = "cleanup"
    READONLY = "readonly"
    RECONCILE = "reconcile"
    ARCHIVE = "archive"
    LICENSE_DOWNGRADE = "license_downgrade"
    AUTO_DISABLE = "auto_disable"
    REPORT = "report"


class ScheduleStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ScheduleDefinition(BaseModel):
    """A scheduled lifecycle action."""

    id: UUID = Field(default_factory=uuid4)
    hack_prefix: str
    action: ScheduleAction
    scheduled_at: datetime
    status: ScheduleStatus = ScheduleStatus.PENDING
    created_by: str = "system"
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # ── Execution ────────────────────────────────────────────────
    executed_at: datetime | None = None
    operation_id: UUID | None = None
    result: dict | None = None
    error: str = ""

    # ── Configuration ────────────────────────────────────────────
    config: dict = Field(default_factory=dict)
    retry_on_failure: bool = True
    max_retries: int = 3
    retry_count: int = 0

    @property
    def is_due(self) -> bool:
        return self.status == ScheduleStatus.PENDING and datetime.utcnow() >= self.scheduled_at

    @property
    def is_terminal(self) -> bool:
        return self.status in (ScheduleStatus.COMPLETED, ScheduleStatus.FAILED, ScheduleStatus.CANCELLED)


class CleanupPolicy(BaseModel):
    """Defines when and how a hack should be cleaned up."""

    hack_prefix: str
    auto_cleanup: bool = True
    cleanup_after_days: int = 30
    archive_after_cleanup: bool = True
    delete_users: bool = True
    delete_groups: bool = True
    remove_rbac: bool = True
    remove_licenses: bool = True
    redact_secrets: bool = True
    notify_admins: bool = True
    notification_emails: list[str] = Field(default_factory=list)

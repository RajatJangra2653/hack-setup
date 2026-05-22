"""Audit event models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class AuditSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AuditEventType(str, Enum):
    # ── Provision lifecycle ──────────────────────────────────────
    PROVISION_STARTED = "provision.started"
    PROVISION_COMPLETED = "provision.completed"
    PROVISION_FAILED = "provision.failed"

    # ── Cleanup lifecycle ────────────────────────────────────────
    CLEANUP_STARTED = "cleanup.started"
    CLEANUP_COMPLETED = "cleanup.completed"
    CLEANUP_FAILED = "cleanup.failed"

    # ── Readonly transition ──────────────────────────────────────
    READONLY_STARTED = "readonly.started"
    READONLY_COMPLETED = "readonly.completed"

    # ── State management ─────────────────────────────────────────
    STATE_UPDATED = "state.updated"
    STATE_ARCHIVED = "state.archived"
    CONFIG_PATCHED = "config.patched"

    # ── Credentials ──────────────────────────────────────────────
    TAP_REGENERATED = "tap.regenerated"
    PASSWORD_RESET = "password.reset"

    # ── GitHub ───────────────────────────────────────────────────
    GITHUB_ENABLED = "github.enabled"
    GITHUB_DISABLED = "github.disabled"

    # ── RBAC ─────────────────────────────────────────────────────
    RBAC_ASSIGNED = "rbac.assigned"
    RBAC_REMOVED = "rbac.removed"
    RBAC_DOWNGRADED = "rbac.downgraded"

    # ── Upload ───────────────────────────────────────────────────
    UPLOAD_STARTED = "upload.started"
    UPLOAD_COMPLETED = "upload.completed"

    # ── Scheduler ────────────────────────────────────────────────
    SCHEDULE_CREATED = "scheduler.job_created"
    SCHEDULE_EXECUTED = "scheduler.executed"

    # ── Drift / Reconciliation ───────────────────────────────────
    DRIFT_DETECTED = "drift.detected"
    DRIFT_RESOLVED = "drift.resolved"
    DRIFT_CLEAN = "drift.clean"
    RECONCILE_STARTED = "reconcile.started"
    RECONCILE_COMPLETED = "reconcile.completed"
    RECONCILE_FAILED = "reconcile.failed"

    # ── License ──────────────────────────────────────────────────
    LICENSE_ASSIGNED = "license.assigned"
    LICENSE_REMOVED = "license.removed"

    # ── User lifecycle ───────────────────────────────────────────
    USER_CREATED = "user.created"
    USER_DISABLED = "user.disabled"
    USER_DELETED = "user.deleted"

    # ── Group lifecycle ──────────────────────────────────────────
    GROUP_CREATED = "group.created"
    GROUP_DELETED = "group.deleted"


class AuditEvent(BaseModel):
    """An immutable audit record."""

    id: UUID = Field(default_factory=uuid4)
    event_type: AuditEventType
    hack_prefix: str
    actor: str = "system"
    severity: AuditSeverity = AuditSeverity.INFO
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    correlation_id: str = ""
    operation_id: str = ""
    details: dict = Field(default_factory=dict)
    target_entity: str = ""
    target_id: str = ""

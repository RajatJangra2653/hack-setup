"""Operation tracking models — every mutation is an operation."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class OperationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class OperationType(str, Enum):
    PROVISION = "provision"
    CLEANUP = "cleanup"
    RECONCILE = "reconcile"
    READONLY = "readonly"
    UPLOAD = "upload"
    LICENSE_ASSIGN = "license_assign"
    RBAC_ASSIGN = "rbac_assign"
    GITHUB_ENABLE = "github_enable"
    TAP_REGENERATE = "tap_regenerate"
    SCHEDULE = "schedule"
    ARCHIVE = "archive"
    DRIFT_CHECK = "drift_check"


class OperationStep(BaseModel):
    """A discrete step within an operation."""

    name: str
    status: OperationStatus = OperationStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str = ""
    result: dict | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None


class Operation(BaseModel):
    """A tracked lifecycle operation."""

    id: UUID = Field(default_factory=uuid4)
    type: OperationType
    hack_prefix: str
    actor: str = "system"
    status: OperationStatus = OperationStatus.PENDING

    # ── Timing ───────────────────────────────────────────────────
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    deadline: datetime | None = None

    # ── Progress ─────────────────────────────────────────────────
    steps: list[OperationStep] = Field(default_factory=list)
    progress_pct: float = 0.0
    current_step: str = ""

    # ── Result ───────────────────────────────────────────────────
    result: dict | None = None
    error: str = ""
    retry_count: int = 0

    # ── Rollback metadata ────────────────────────────────────────
    rollback_data: dict | None = None
    is_rollback: bool = False
    parent_operation_id: UUID | None = None

    @property
    def duration_seconds(self) -> float | None:
        end = self.completed_at or datetime.utcnow()
        if self.started_at:
            return (end - self.started_at).total_seconds()
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
            OperationStatus.TIMED_OUT,
        )

    def start(self) -> None:
        self.status = OperationStatus.RUNNING
        self.started_at = datetime.utcnow()

    def add_step(self, name: str) -> OperationStep:
        step = OperationStep(name=name, status=OperationStatus.RUNNING, started_at=datetime.utcnow())
        self.steps.append(step)
        self.current_step = name
        return step

    def complete_step(self, name: str, *, result: dict | None = None) -> None:
        for step in self.steps:
            if step.name == name:
                step.status = OperationStatus.COMPLETED
                step.completed_at = datetime.utcnow()
                step.result = result
                break
        self._update_progress()

    def fail_step(self, name: str, error: str) -> None:
        for step in self.steps:
            if step.name == name:
                step.status = OperationStatus.FAILED
                step.completed_at = datetime.utcnow()
                step.error = error
                break

    def complete(self, *, result: dict | None = None) -> None:
        self.status = OperationStatus.COMPLETED
        self.completed_at = datetime.utcnow()
        self.result = result
        self.progress_pct = 100.0

    def fail(self, error: str) -> None:
        self.status = OperationStatus.FAILED
        self.completed_at = datetime.utcnow()
        self.error = error

    def cancel(self) -> None:
        self.status = OperationStatus.CANCELLED
        self.completed_at = datetime.utcnow()

    def _update_progress(self) -> None:
        if not self.steps:
            return
        done = sum(1 for s in self.steps if s.status == OperationStatus.COMPLETED)
        self.progress_pct = round(done / len(self.steps) * 100, 1)

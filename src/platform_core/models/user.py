"""User domain models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class UserStatus(str, Enum):
    PLANNED = "planned"
    CREATED = "created"
    EXISTING = "existing"
    FAILED = "failed"
    DISABLED = "disabled"
    DELETED = "deleted"
    DRY_RUN = "dry_run"


class HackUser(BaseModel):
    """A user provisioned as part of a hack environment."""

    user_principal_name: str
    display_name: str = ""
    mail_nickname: str = ""
    user_id: str = ""

    status: UserStatus = UserStatus.PLANNED
    is_admin: bool = False
    team: str = ""

    # ── Credentials (redacted on archive) ────────────────────────
    password: str = ""
    tap_code: str = ""
    tap_expires: datetime | None = None

    # ── Assignments ──────────────────────────────────────────────
    licenses: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    rbac_roles: list[str] = Field(default_factory=list)

    # ── GitHub ───────────────────────────────────────────────────
    github_username: str = ""
    github_status: str = ""

    # ── OneDrive ─────────────────────────────────────────────────
    drive_id: str = ""
    onedrive_provisioned: bool = False
    files_uploaded: int = 0

    # ── Metadata ─────────────────────────────────────────────────
    created_at: datetime | None = None
    error_message: str = ""

    def redact_secrets(self) -> HackUser:
        """Return a copy with secrets cleared."""
        return self.model_copy(update={
            "password": "***" if self.password else "",
            "tap_code": "***" if self.tap_code else "",
        })


class UserPlan(BaseModel):
    """Pre-provision plan for a single user."""

    user_principal_name: str
    display_name: str
    mail_nickname: str
    is_admin: bool = False
    team: str = ""
    licenses: list[str] = Field(default_factory=list)


class UserProvisionResult(BaseModel):
    """Result of provisioning a single user."""

    user_principal_name: str
    user_id: str = ""
    status: UserStatus = UserStatus.PLANNED
    password: str = ""
    tap_code: str = ""
    tap_expires: datetime | None = None
    licenses_assigned: list[str] = Field(default_factory=list)
    groups_joined: list[str] = Field(default_factory=list)
    github_username: str = ""
    error: str = ""
    duration_seconds: float = 0.0

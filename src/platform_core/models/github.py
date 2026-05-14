"""GitHub domain models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class GitHubUser(BaseModel):
    """A GitHub EMU user provisioned via the platform."""

    entra_user_id: str
    user_principal_name: str = ""
    github_username: str = ""
    github_id: int = 0
    emu_group_id: str = ""
    copilot_enabled: bool = False
    ghas_enabled: bool = False
    provisioned_at: datetime | None = None
    status: str = "pending"  # pending | provisioned | failed


class GitHubRepository(BaseModel):
    """A GitHub repository tracked by the platform."""

    repo_id: int = 0
    full_name: str = ""
    owner: str = ""
    name: str = ""
    visibility: str = "private"
    hack_prefix: str = ""
    team: str = ""
    created_at: datetime | None = None

"""Group domain models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SecurityGroup(BaseModel):
    """An Entra ID security group managed by the platform."""

    group_id: str = ""
    display_name: str
    description: str = ""
    members: list[str] = Field(default_factory=list)
    hack_prefix: str = ""
    team: str = ""
    is_admin_group: bool = False
    created_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

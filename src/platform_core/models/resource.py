"""Azure resource tracking models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ResourceType(str, Enum):
    SUBSCRIPTION = "subscription"
    RESOURCE_GROUP = "resource_group"
    ROLE_ASSIGNMENT = "role_assignment"
    ONEDRIVE = "onedrive"
    SHAREPOINT_SITE = "sharepoint_site"
    GITHUB_REPO = "github_repo"
    GITHUB_USER = "github_user"
    LICENSE = "license"
    USER = "user"
    GROUP = "group"
    TAP = "tap"


class TrackedResource(BaseModel):
    """Any cloud resource the platform manages."""

    resource_id: str
    resource_type: ResourceType
    display_name: str = ""
    hack_prefix: str = ""
    team: str = ""
    owner_id: str = ""
    provider: str = ""  # e.g. "entra", "azure", "github"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at


class AzureSubscription(BaseModel):
    """An Azure subscription assigned to a hack team."""

    subscription_id: str
    display_name: str = ""
    team: str = ""
    hack_prefix: str = ""
    rbac_role: str = "Contributor"
    principal_id: str = ""
    assignment_id: str = ""
    cost: float = 0.0
    cost_updated_at: datetime | None = None


class ResourceGroup(BaseModel):
    """An Azure resource group."""

    name: str
    subscription_id: str
    location: str = "eastus"
    hack_prefix: str = ""
    tags: dict[str, str] = Field(default_factory=dict)

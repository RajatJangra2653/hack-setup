"""GitHub EMU (Enterprise Managed Users) enablement.

Adds a user to a GitHub-EMU-backed Entra security group and triggers an
on-demand synchronization job so the user is provisioned in GitHub Enterprise
Cloud immediately.

The credentials and group object IDs used for this flow are intentionally
hardcoded here — they live in a *different* tenant than the one running the
hackathon provisioning, and never need to change per user/run.
"""
from .client import GitHubEnabler  # noqa: F401
from .config import (  # noqa: F401
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_EMU_SHORT_NAME,
    GITHUB_RULE_ID,
    GITHUB_SP_ID,
    GITHUB_SYNC_JOB_ID,
    GITHUB_TENANT_ID,
    GROUP_GHAS,
    GROUP_LEGACY_COPILOT,
    GROUP_LEGACY_DEFAULT,
    GROUP_PUBLIC_SECTOR_HACKS,
    GROUP_PUBLIC_SECTOR_HACKS_COPILOT,
    derive_github_username,
    resolve_group_id,
)
from .models import GitHubEnableReport, GitHubEnableResult  # noqa: F401

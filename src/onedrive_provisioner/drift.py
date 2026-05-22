"""Drift detection: compares saved hack state (desired) vs live Entra (actual).

Uses the existing DiscoveryService to query Graph and compares against
the user/group lists stored in blob state. Reports:
  - missing: users/groups in state but not in Entra (deleted externally)
  - extra:   users/groups in Entra but not in state (created externally)
  - modified: users whose accountEnabled differs from expectation
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from .entra.discovery_service import DiscoveryService
from .graph import GraphClient
from .logging_setup import get_logger

logger = get_logger(__name__)


class DriftResult:
    """Container for drift detection results."""

    def __init__(
        self,
        prefix: str,
        *,
        missing_users: List[dict],
        extra_users: List[dict],
        modified_users: List[dict],
        missing_groups: List[dict],
        extra_groups: List[dict],
        checked_at: str,
        state_user_count: int,
        live_user_count: int,
        state_group_count: int,
        live_group_count: int,
    ) -> None:
        self.prefix = prefix
        self.missing_users = missing_users
        self.extra_users = extra_users
        self.modified_users = modified_users
        self.missing_groups = missing_groups
        self.extra_groups = extra_groups
        self.checked_at = checked_at
        self.state_user_count = state_user_count
        self.live_user_count = live_user_count
        self.state_group_count = state_group_count
        self.live_group_count = live_group_count

    @property
    def has_drift(self) -> bool:
        return bool(
            self.missing_users or self.extra_users or self.modified_users
            or self.missing_groups or self.extra_groups
        )

    @property
    def summary(self) -> str:
        if not self.has_drift:
            return "No drift detected"
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
        return ", ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prefix": self.prefix,
            "hasDrift": self.has_drift,
            "summary": self.summary,
            "checkedAt": self.checked_at,
            "stateUserCount": self.state_user_count,
            "liveUserCount": self.live_user_count,
            "stateGroupCount": self.state_group_count,
            "liveGroupCount": self.live_group_count,
            "missingUsers": self.missing_users,
            "extraUsers": self.extra_users,
            "modifiedUsers": self.modified_users,
            "missingGroups": self.missing_groups,
            "extraGroups": self.extra_groups,
        }


async def detect_drift(
    graph: GraphClient,
    state: Dict[str, Any],
    prefix: str,
) -> DriftResult:
    """Compare saved hack state against live Entra ID.

    Args:
        graph: Authenticated GraphClient.
        state: The hack state dict from blob storage.
        prefix: The hack prefix for discovery.

    Returns:
        DriftResult with detailed comparison.
    """
    disco = DiscoveryService(graph)
    discovered = await disco.discover(prefix)
    live_users = discovered.get("users", [])
    live_groups = discovered.get("groups", [])

    # Build lookup maps
    state_users = state.get("users", []) or []
    state_groups = state.get("groups", []) or []

    state_upn_set = {
        u.get("userPrincipalName", "").lower()
        for u in state_users
        if u.get("userPrincipalName")
    }
    live_upn_map = {
        u.get("userPrincipalName", "").lower(): u
        for u in live_users
        if u.get("userPrincipalName")
    }
    live_upn_set = set(live_upn_map.keys())

    # Groups in state may be dicts with "displayName" or plain strings
    state_group_names = {}
    for g in state_groups:
        if isinstance(g, str):
            if g:
                state_group_names[g.lower()] = {"displayName": g}
        elif isinstance(g, dict) and g.get("displayName"):
            state_group_names[g["displayName"].lower()] = g

    live_group_names = {}
    for g in live_groups:
        if isinstance(g, str):
            if g:
                live_group_names[g.lower()] = {"displayName": g}
        elif isinstance(g, dict) and g.get("displayName"):
            live_group_names[g["displayName"].lower()] = g

    # Users
    missing_upns = state_upn_set - live_upn_set
    extra_upns = live_upn_set - state_upn_set
    common_upns = state_upn_set & live_upn_set

    missing_users = [
        {"userPrincipalName": upn, "issue": "exists in state but not in Entra"}
        for upn in sorted(missing_upns)
    ]
    extra_users = [
        {
            "userPrincipalName": upn,
            "id": live_upn_map[upn].get("id", ""),
            "issue": "exists in Entra but not in state",
        }
        for upn in sorted(extra_upns)
    ]

    # Check accountEnabled for common users
    modified_users = []
    for upn in sorted(common_upns):
        live = live_upn_map[upn]
        if live.get("accountEnabled") is False:
            modified_users.append({
                "userPrincipalName": upn,
                "id": live.get("id", ""),
                "issue": "account disabled in Entra but active in state",
            })

    # Groups
    state_gnames = set(state_group_names.keys())
    live_gnames = set(live_group_names.keys())

    missing_groups = [
        {"displayName": name, "issue": "exists in state but not in Entra"}
        for name in sorted(state_gnames - live_gnames)
    ]
    extra_groups = [
        {
            "displayName": name,
            "id": live_group_names[name].get("id", ""),
            "issue": "exists in Entra but not in state",
        }
        for name in sorted(live_gnames - state_gnames)
    ]

    return DriftResult(
        prefix=prefix,
        missing_users=missing_users,
        extra_users=extra_users,
        modified_users=modified_users,
        missing_groups=missing_groups,
        extra_groups=extra_groups,
        checked_at=datetime.now(timezone.utc).isoformat(),
        state_user_count=len(state_users),
        live_user_count=len(live_users),
        state_group_count=len(state_groups),
        live_group_count=len(live_groups),
    )

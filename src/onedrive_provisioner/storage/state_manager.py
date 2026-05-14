"""High-level state manager for hack provisioning data.

Handles reading, writing, versioning, and merging of hack state in blob storage.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .blob_client import BlobStateClient

logger = logging.getLogger(__name__)

# ── JSON Schema version for forward compatibility ──
STATE_SCHEMA_VERSION = "1.0"


class HackStateManager:
    """Manages the lifecycle of a hack's provisioning state in blob storage."""

    def __init__(self, blob_client: BlobStateClient) -> None:
        self._blob = blob_client

    # ─────────── Path helpers ───────────
    @staticmethod
    def _state_path(prefix: str) -> str:
        return f"{prefix.rstrip('-')}/state.json"

    @staticmethod
    def _archive_state_path(prefix: str) -> str:
        return f"archive/{prefix.rstrip('-')}/state.json"

    @staticmethod
    def _version_path(prefix: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
        return f"{prefix.rstrip('-')}/state_{ts}.json"

    @staticmethod
    def _archive_version_path(prefix: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
        return f"archive/{prefix.rstrip('-')}/state_{ts}.json"

    # ─────────── Core operations ───────────
    def get_state(self, prefix: str) -> Optional[Dict[str, Any]]:
        """Retrieve the current state for a hack prefix.

        Archived state is returned as a fallback so reports can still be
        generated after cleanup has moved the hack out of the active list.
        """
        return (
            self._blob.read_json(self._state_path(prefix))
            or self._blob.read_json(self._archive_state_path(prefix))
        )

    def save_state(
        self,
        prefix: str,
        state: Dict[str, Any],
        *,
        version: bool = True,
    ) -> None:
        """Save state, optionally creating a timestamped version backup."""
        state["schemaVersion"] = STATE_SCHEMA_VERSION
        state["lastUpdated"] = datetime.now(timezone.utc).isoformat()

        self._blob.write_json(self._state_path(prefix), state)

        if version:
            self._blob.write_json(self._version_path(prefix), state)

    def list_hacks(self) -> List[Dict[str, Any]]:
        """List all hack prefixes that have saved state."""
        blobs = self._blob.list_blobs("")
        prefixes = set()
        for b in blobs:
            if b.startswith("archive/"):
                continue
            if b.endswith("/state.json"):
                prefixes.add(b.replace("/state.json", ""))
        result = []
        for p in sorted(prefixes):
            state = self._blob.read_json(f"{p}/state.json")
            if state:
                result.append(self._summary_from_state(state, p, archived=False))
        return result

    def list_archived_hacks(self) -> List[Dict[str, Any]]:
        """List hack prefixes that were cleaned up and archived."""
        blobs = self._blob.list_blobs("archive/")
        prefixes = set()
        for b in blobs:
            if b.endswith("/state.json"):
                prefixes.add(b.replace("archive/", "", 1).replace("/state.json", ""))
        result = []
        for p in sorted(prefixes):
            state = self._blob.read_json(self._archive_state_path(p))
            if state:
                result.append(self._summary_from_state(state, p, archived=True))
        return result

    @staticmethod
    def _summary_from_state(state: Dict[str, Any], prefix: str, *, archived: bool) -> Dict[str, Any]:
        cfg = state.get("config") or {}
        summary = state.get("summary") or {}
        return {
            "prefix": state.get("prefix", prefix),
            "hackName": state.get("hackName", ""),
            "domain": state.get("domain", ""),
            "totalUsers": state.get("totalUsers", 0),
            "lastUpdated": state.get("lastUpdated", ""),
            "createdAt": state.get("createdAt", ""),
            "createdBy": state.get("createdBy", ""),
            "hackStartDate": state.get("hackStartDate", ""),
            "hackDate": state.get("hackDate", ""),
            "readonlyDate": state.get("readonlyDate", ""),
            "deleteDate": state.get("deleteDate") or state.get("endDate", ""),
            "endDate": state.get("endDate", ""),
            "archived": archived or bool(state.get("archivedAt")),
            "archivedAt": state.get("archivedAt", ""),
            "archiveReason": state.get("archiveReason", ""),
            # Enriched fields for dashboard
            "teams": int(cfg.get("teams", 0)),
            "usersPerTeam": int(cfg.get("usersPerTeam", 0)),
            "adminUsers": int(cfg.get("adminUsers", 0)),
            "licenses": cfg.get("licenses", []),
            "mode": cfg.get("mode", "team"),
            "lifecycleStatus": state.get("lifecycleStatus", ""),
            "summary": {
                "totalUsers": summary.get("totalUsers", state.get("totalUsers", 0)),
                "created": summary.get("created", 0),
                "existing": summary.get("existing", 0),
                "failed": summary.get("failed", 0),
                "admins": summary.get("admins", 0),
                "groupsCreated": summary.get("groupsCreated", 0),
            },
            "subscriptionIds": state.get("subscriptionIds", []),
            "lastReportCosts": state.get("lastReportCosts") or {},
        }

    def list_versions(self, prefix: str) -> List[str]:
        """List timestamped version blobs for a prefix."""
        clean = prefix.rstrip("-")
        blobs = self._blob.list_blobs(f"{clean}/state_")
        blobs.extend(self._blob.list_blobs(f"archive/{clean}/state_"))
        return sorted(blobs, reverse=True)

    def get_version(self, prefix: str, version_blob: str) -> Optional[Dict[str, Any]]:
        """Retrieve a specific version of state."""
        return self._blob.read_json(version_blob)

    def delete_state(self, prefix: str) -> bool:
        """Delete a hack's state (current only, keeps versions)."""
        return self._blob.delete_blob(self._state_path(prefix))

    def archive_state(
        self,
        prefix: str,
        *,
        reason: str = "cleanup",
        cleanup_result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Move active state into the archive folder and remove active state.

        Passwords and TAP values are removed from the archived copy. The archive
        keeps enough metadata for later audit/report generation without keeping
        stale sign-in secrets after cleanup.
        """
        state = self._blob.read_json(self._state_path(prefix))
        if not state:
            return bool(self._blob.read_json(self._archive_state_path(prefix)))

        archived = copy.deepcopy(state)
        now = datetime.now(timezone.utc).isoformat()
        archived["archivedAt"] = now
        archived["archiveReason"] = reason
        archived["lifecycleStatus"] = "archived"
        archived["isArchived"] = True
        if cleanup_result is not None:
            archived["cleanupResult"] = cleanup_result
        for user in archived.get("users", []) or []:
            user.pop("password", None)
            user.pop("tap", None)
            user["credentialsArchived"] = True

        self._blob.write_json(self._archive_state_path(prefix), archived)
        self._blob.write_json(self._archive_version_path(prefix), archived)
        self._blob.delete_blob(self._state_path(prefix))
        return True

    # ─────────── Build state from provisioning report ───────────
    @staticmethod
    def build_state_from_report(
        config: Dict[str, Any],
        report: Dict[str, Any],
        *,
        session_id: str = "",
    ) -> Dict[str, Any]:
        """Build a state JSON from provisioning config + report.

        This is the canonical state shape saved to blob storage.
        """
        now = datetime.now(timezone.utc).isoformat()
        users = []
        for u in report.get("users", []):
            users.append({
                "userPrincipalName": u.get("userPrincipalName", ""),
                "userId": u.get("userId", ""),
                "status": u.get("status", ""),
                "password": u.get("password", ""),
                "tap": u.get("tap", ""),
                "tapExpires": u.get("tapExpires", ""),
                "licenses": u.get("licenses", []),
                "groups": u.get("groups", []),
                "groupFailures": u.get("groupFailures", []),
                "isAdmin": u.get("isAdmin", False),
                "message": u.get("message", ""),
                "github": u.get("github"),
                "githubUsername": (u.get("github") or {}).get("githubUsername") if isinstance(u.get("github"), dict) else None,
                "provisionedAt": now,
                "lastTapRegenAt": None,
                "lastLicenseUpdateAt": None,
            })

        return {
            "schemaVersion": STATE_SCHEMA_VERSION,
            "prefix": config.get("prefix", ""),
            "hackName": config.get("hackName", ""),
            "createdBy": config.get("createdBy", ""),
            "domain": config.get("domain", ""),
            "mode": config.get("mode", "team"),
            "hackStartDate": config.get("hackStartDate", ""),
            "hackDate": config.get("hackDate", ""),
            "readonlyDate": config.get("readonlyDate", ""),
            "deleteDate": config.get("deleteDate") or config.get("endDate", ""),
            "endDate": config.get("deleteDate") or config.get("endDate", ""),
            "createdAt": now,
            "lastUpdated": now,
            "sessionId": session_id,
            "config": {
                k: v for k, v in config.items()
                if k not in ("initialPassword",)
            },
            "summary": {
                "totalUsers": report.get("totalUsers", 0),
                "created": report.get("created", 0),
                "existing": report.get("existing", 0),
                "failed": report.get("failed", 0),
                "admins": report.get("admins", 0),
                "groupsCreated": report.get("groupsCreated", 0),
            },
            "groups": report.get("groups", []),
            "users": users,
            "totalUsers": len(users),
        }

    # ─────────── Merge / update operations ───────────
    def merge_users(
        self,
        prefix: str,
        updated_users: List[Dict[str, Any]],
        *,
        field: str = "userPrincipalName",
    ) -> Dict[str, Any]:
        """Merge updated user records into existing state.

        Matches by UPN (or other field). New fields are added,
        existing fields are overwritten. Users not in updated_users
        are kept as-is.  Returns the full updated state.
        """
        state = self.get_state(prefix)
        if not state:
            raise ValueError(f"No state found for prefix '{prefix}'")

        existing = {u[field]: u for u in state.get("users", [])}
        for upd in updated_users:
            key = upd.get(field)
            if key and key in existing:
                existing[key].update(upd)
            elif key:
                existing[key] = upd

        state["users"] = list(existing.values())
        state["totalUsers"] = len(state["users"])
        self.save_state(prefix, state)
        return state

    def update_user_taps(
        self,
        prefix: str,
        tap_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Update TAP values for users after regeneration."""
        now = datetime.now(timezone.utc).isoformat()
        updates = []
        for t in tap_results:
            updates.append({
                "userPrincipalName": t["userPrincipalName"],
                "tap": t.get("tap", ""),
                "tapExpires": t.get("tapExpires", ""),
                "lastTapRegenAt": now,
            })
        return self.merge_users(prefix, updates)

    def update_user_licenses(
        self,
        prefix: str,
        license_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Update license assignments for users."""
        now = datetime.now(timezone.utc).isoformat()
        updates = []
        for lr in license_results:
            updates.append({
                "userPrincipalName": lr["userPrincipalName"],
                "licenses": lr.get("licenses", []),
                "lastLicenseUpdateAt": now,
            })
        return self.merge_users(prefix, updates)

    def update_user_passwords(
        self,
        prefix: str,
        password_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Update passwords for users after reset."""
        now = datetime.now(timezone.utc).isoformat()
        updates = []
        for pr in password_results:
            updates.append({
                "userPrincipalName": pr["userPrincipalName"],
                "password": pr.get("password", ""),
                "lastPasswordResetAt": now,
            })
        return self.merge_users(prefix, updates)

    def update_user_groups(
        self,
        prefix: str,
        group_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Update group memberships for users after repair."""
        now = datetime.now(timezone.utc).isoformat()
        updates = []
        for gr in group_results:
            upd: Dict[str, Any] = {
                "userPrincipalName": gr["userPrincipalName"],
                "groups": gr.get("groups", []),
                "groupFailures": gr.get("groupFailures", []),
                "lastGroupRepairAt": now,
            }
            updates.append(upd)
        return self.merge_users(prefix, updates)

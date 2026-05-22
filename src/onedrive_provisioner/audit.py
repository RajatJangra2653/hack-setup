"""Audit trail for hack lifecycle operations.

Records structured audit events to blob storage alongside hack state.
Events are append-only and queryable by hack prefix, actor, or event type.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Event types ──
PROVISION_STARTED = "provision.started"
PROVISION_COMPLETED = "provision.completed"
PROVISION_FAILED = "provision.failed"
CLEANUP_STARTED = "cleanup.started"
CLEANUP_COMPLETED = "cleanup.completed"
CLEANUP_FAILED = "cleanup.failed"
READONLY_STARTED = "readonly.started"
READONLY_COMPLETED = "readonly.completed"
STATE_UPDATED = "state.updated"
STATE_ARCHIVED = "state.archived"
TAP_REGENERATED = "tap.regenerated"
GITHUB_ENABLED = "github.enabled"
GITHUB_DISABLED = "github.disabled"
RBAC_ASSIGNED = "rbac.assigned"
RBAC_REMOVED = "rbac.removed"
RBAC_DOWNGRADED = "rbac.downgraded"
UPLOAD_STARTED = "upload.started"
UPLOAD_COMPLETED = "upload.completed"
SCHEDULER_JOB_CREATED = "scheduler.job_created"
SCHEDULER_JOB_EXECUTED = "scheduler.job_executed"
CONFIG_PATCHED = "config.patched"
DRIFT_DETECTED = "drift.detected"
DRIFT_RESOLVED = "drift.resolved"

ALL_EVENT_TYPES = [
    PROVISION_STARTED, PROVISION_COMPLETED, PROVISION_FAILED,
    CLEANUP_STARTED, CLEANUP_COMPLETED, CLEANUP_FAILED,
    READONLY_STARTED, READONLY_COMPLETED,
    STATE_UPDATED, STATE_ARCHIVED,
    TAP_REGENERATED, GITHUB_ENABLED, GITHUB_DISABLED,
    RBAC_ASSIGNED, RBAC_REMOVED, RBAC_DOWNGRADED,
    UPLOAD_STARTED, UPLOAD_COMPLETED,
    SCHEDULER_JOB_CREATED, SCHEDULER_JOB_EXECUTED,
    CONFIG_PATCHED, DRIFT_DETECTED, DRIFT_RESOLVED,
]


class AuditLogger:
    """Writes audit events to blob storage.

    Events are stored as `{prefix}/_audit/events.json` — a JSON array
    that gets appended to. Thread-safe via lock.

    For local dev without blob storage, events are logged to stderr only.
    """

    def __init__(self, get_state_manager) -> None:
        self._get_mgr = get_state_manager
        self._lock = threading.Lock()

    @staticmethod
    def _audit_path(prefix: str) -> str:
        return f"{prefix.rstrip('-')}/_audit/events.json"

    def log(
        self,
        event_type: str,
        prefix: str,
        *,
        actor: str = "",
        details: Optional[Dict[str, Any]] = None,
        severity: str = "info",
        correlation_id: str = "",
    ) -> Dict[str, Any]:
        """Record an audit event. Returns the event dict."""
        event = {
            "id": str(uuid.uuid4()),
            "type": event_type,
            "prefix": prefix,
            "actor": actor or "system",
            "severity": severity,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "correlationId": correlation_id or str(uuid.uuid4()),
            "details": details or {},
        }

        logger.info(
            "audit.event type=%s prefix=%s actor=%s severity=%s",
            event_type, prefix, event["actor"], severity,
        )

        mgr = self._get_mgr()
        if not mgr:
            # No blob storage — just log
            return event

        with self._lock:
            try:
                existing = mgr._blob.read_json(self._audit_path(prefix))
                events = (existing or {}).get("events", [])
            except Exception:
                events = []

            events.append(event)

            # Keep last 1000 events per hack
            if len(events) > 1000:
                events = events[-1000:]

            mgr._blob.write_json(
                self._audit_path(prefix),
                {"events": events, "lastUpdated": event["timestamp"]},
            )

        return event

    def query(
        self,
        prefix: str,
        *,
        event_type: str = "",
        actor: str = "",
        since: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Query audit events for a hack prefix."""
        mgr = self._get_mgr()
        if not mgr:
            return []

        try:
            data = mgr._blob.read_json(self._audit_path(prefix))
            events = (data or {}).get("events", [])
        except Exception:
            return []

        if event_type:
            events = [e for e in events if e.get("type") == event_type]
        if actor:
            events = [e for e in events if e.get("actor") == actor]
        if since:
            events = [e for e in events if e.get("timestamp", "") >= since]

        # Most recent first
        events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        return events[:limit]

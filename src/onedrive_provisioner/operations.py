"""Operation tracking for long-running hack lifecycle mutations.

Wraps provisioning, cleanup, readonly, and GitHub operations with a
lightweight tracking layer. Each operation gets a unique ID, timestamped
steps, and a final status. Stored alongside hack state in blob storage.

Usage:
    tracker = OperationTracker(get_state_manager)
    op = tracker.start("provision", "HACK01", actor="admin@contoso.com")
    op.step("create_users", "Creating 10 users")
    # ... do work ...
    op.step_done("create_users", result={"created": 10})
    op.complete(result={"totalUsers": 10})
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class Operation:
    """A tracked lifecycle operation with steps."""

    def __init__(
        self,
        op_id: str,
        op_type: str,
        prefix: str,
        actor: str,
    ) -> None:
        self.id = op_id
        self.type = op_type
        self.prefix = prefix
        self.actor = actor
        self.status = "running"
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.completed_at: Optional[str] = None
        self.error: Optional[str] = None
        self.result: Dict[str, Any] = {}
        self.steps: List[Dict[str, Any]] = []

    def step(self, name: str, description: str = "") -> None:
        """Record the start of a step."""
        self.steps.append({
            "name": name,
            "description": description,
            "status": "running",
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "completedAt": None,
            "result": None,
            "error": None,
        })

    def step_done(
        self,
        name: str,
        *,
        result: Any = None,
        error: str = "",
    ) -> None:
        """Mark a step as completed or failed."""
        for s in reversed(self.steps):
            if s["name"] == name and s["status"] == "running":
                s["status"] = "failed" if error else "completed"
                s["completedAt"] = datetime.now(timezone.utc).isoformat()
                s["result"] = result
                s["error"] = error or None
                break

    def complete(self, *, result: Optional[Dict[str, Any]] = None) -> None:
        self.status = "completed"
        self.completed_at = datetime.now(timezone.utc).isoformat()
        self.result = result or {}

    def fail(self, error: str) -> None:
        self.status = "failed"
        self.completed_at = datetime.now(timezone.utc).isoformat()
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "prefix": self.prefix,
            "actor": self.actor,
            "status": self.status,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "error": self.error,
            "result": self.result,
            "steps": self.steps,
        }


class OperationTracker:
    """Manages operation lifecycle and persists to blob storage."""

    def __init__(self, get_state_manager) -> None:
        self._get_mgr = get_state_manager
        self._lock = threading.Lock()
        # In-memory cache of active operations
        self._active: Dict[str, Operation] = {}

    @staticmethod
    def _ops_path(prefix: str) -> str:
        return f"{prefix.rstrip('-')}/_operations/history.json"

    def start(
        self,
        op_type: str,
        prefix: str,
        *,
        actor: str = "",
    ) -> Operation:
        """Start tracking a new operation."""
        op = Operation(
            op_id=str(uuid.uuid4()),
            op_type=op_type,
            prefix=prefix,
            actor=actor or "system",
        )
        with self._lock:
            self._active[op.id] = op

        logger.info(
            "operation.started id=%s type=%s prefix=%s actor=%s",
            op.id, op_type, prefix, op.actor,
        )
        return op

    def finish(self, op: Operation) -> None:
        """Persist a completed/failed operation and remove from active."""
        with self._lock:
            self._active.pop(op.id, None)

        logger.info(
            "operation.finished id=%s type=%s status=%s prefix=%s",
            op.id, op.type, op.status, op.prefix,
        )

        mgr = self._get_mgr()
        if not mgr:
            return

        try:
            existing = mgr._blob.read_json(self._ops_path(op.prefix))
            ops = (existing or {}).get("operations", [])
        except Exception:
            ops = []

        ops.append(op.to_dict())

        # Keep last 200 operations per hack
        if len(ops) > 200:
            ops = ops[-200:]

        mgr._blob.write_json(
            self._ops_path(op.prefix),
            {"operations": ops, "lastUpdated": datetime.now(timezone.utc).isoformat()},
        )

    def get_active(self, prefix: str = "") -> List[Dict[str, Any]]:
        """Get currently running operations, optionally filtered by prefix."""
        with self._lock:
            ops = list(self._active.values())
        if prefix:
            ops = [o for o in ops if o.prefix == prefix]
        return [o.to_dict() for o in ops]

    def get_history(self, prefix: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Get completed operations for a hack prefix."""
        mgr = self._get_mgr()
        if not mgr:
            return []

        try:
            data = mgr._blob.read_json(self._ops_path(prefix))
            ops = (data or {}).get("operations", [])
        except Exception:
            return []

        # Most recent first
        ops.sort(key=lambda o: o.get("startedAt", ""), reverse=True)
        return ops[:limit]

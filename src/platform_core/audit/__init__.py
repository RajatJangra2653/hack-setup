"""Audit service — structured, queryable audit trail.

Every mutation publishes an audit event through the event bus.
The AuditService subscribes and persists events to the storage backend.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from platform_core.events import DomainEvent, EventBus
from platform_core.models.audit import AuditEvent, AuditEventType, AuditSeverity

logger = logging.getLogger(__name__)


class AuditService:
    """Audit trail service.

    Dual mode:
      - **Passive**: subscribes to EventBus and auto-logs events
      - **Active**: called directly via ``log()`` for explicit audit entries
    """

    def __init__(
        self,
        *,
        persist_fn: Any = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._persist_fn = persist_fn  # async fn(AuditEvent) to save
        self._events: list[AuditEvent] = []
        self._max_events = 2000

        # Auto-subscribe to all events
        if event_bus:
            event_bus.subscribe("*", self._handle_event)

    async def log(
        self,
        hack_prefix: str,
        event_type: str | AuditEventType,
        *,
        actor: str = "system",
        severity: str | AuditSeverity = AuditSeverity.INFO,
        details: dict | None = None,
        correlation_id: str = "",
        operation_id: str = "",
        target_entity: str = "",
        target_id: str = "",
    ) -> AuditEvent:
        """Create and persist an audit event."""
        if isinstance(event_type, str):
            try:
                event_type = AuditEventType(event_type)
            except ValueError:
                pass  # Allow custom event types

        if isinstance(severity, str):
            try:
                severity = AuditSeverity(severity)
            except ValueError:
                severity = AuditSeverity.INFO

        event = AuditEvent(
            event_type=event_type,
            hack_prefix=hack_prefix,
            actor=actor,
            severity=severity,
            details=details or {},
            correlation_id=correlation_id,
            operation_id=operation_id,
            target_entity=target_entity,
            target_id=target_id,
        )

        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]

        if self._persist_fn:
            try:
                await self._persist_fn(event)
            except Exception as exc:
                logger.error("Failed to persist audit event: %s", exc)

        return event

    async def query(
        self,
        hack_prefix: str,
        *,
        event_type: str | None = None,
        actor: str | None = None,
        since: datetime | None = None,
        severity: str | None = None,
        limit: int = 50,
    ) -> list[AuditEvent]:
        """Query audit events with filters."""
        results = [e for e in self._events if e.hack_prefix == hack_prefix]

        if event_type:
            results = [e for e in results if e.event_type.value == event_type or str(e.event_type) == event_type]
        if actor:
            results = [e for e in results if e.actor == actor]
        if since:
            results = [e for e in results if e.timestamp >= since]
        if severity:
            results = [e for e in results if e.severity.value == severity]

        return list(reversed(results[-limit:]))

    # ── Event bus handler ────────────────────────────────────────

    async def _handle_event(self, event: DomainEvent) -> None:
        """Auto-log domain events as audit entries."""
        # Map domain event types to audit event types
        mapping = {
            "provision.started": AuditEventType.PROVISION_STARTED,
            "provision.completed": AuditEventType.PROVISION_COMPLETED,
            "provision.failed": AuditEventType.PROVISION_FAILED,
            "cleanup.started": AuditEventType.CLEANUP_STARTED,
            "cleanup.completed": AuditEventType.CLEANUP_COMPLETED,
            "drift.detected": AuditEventType.DRIFT_DETECTED,
            "drift.clean": AuditEventType.DRIFT_CLEAN,
            "reconcile.started": AuditEventType.RECONCILE_STARTED,
            "reconcile.completed": AuditEventType.RECONCILE_COMPLETED,
            "config.patched": AuditEventType.CONFIG_PATCHED,
            "user.created": AuditEventType.USER_CREATED,
            "user.deleted": AuditEventType.USER_DELETED,
        }

        audit_type = mapping.get(event.event_type)
        if audit_type and event.hack_prefix:
            await self.log(
                event.hack_prefix,
                audit_type,
                actor=event.actor,
                details=event.data,
                correlation_id=event.correlation_id,
            )

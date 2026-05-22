"""Event bus — async publish/subscribe for domain events.

This is the platform's internal nervous system.  Every mutation
publishes an event; subscribers (audit logger, operation tracker,
inventory updater, etc.) react asynchronously.

Usage:
    bus = EventBus()
    bus.subscribe("provision.*", audit_handler)
    bus.subscribe("*", metrics_handler)
    await bus.publish(ProvisionStartedEvent(...))
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Coroutine
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Type alias for event handler
EventHandler = Callable[["DomainEvent"], Coroutine[Any, Any, None]]


class DomainEvent(BaseModel):
    """Base class for all domain events."""

    id: UUID = Field(default_factory=uuid4)
    event_type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    correlation_id: str = ""
    actor: str = "system"
    hack_prefix: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════
# Concrete event types
# ═══════════════════════════════════════════════════════════════════

class ProvisionStartedEvent(DomainEvent):
    event_type: str = "provision.started"
    total_users: int = 0


class ProvisionCompletedEvent(DomainEvent):
    event_type: str = "provision.completed"
    succeeded: int = 0
    failed: int = 0


class ProvisionFailedEvent(DomainEvent):
    event_type: str = "provision.failed"
    error: str = ""


class UserCreatedEvent(DomainEvent):
    event_type: str = "user.created"
    user_id: str = ""
    user_principal_name: str = ""


class UserDeletedEvent(DomainEvent):
    event_type: str = "user.deleted"
    user_id: str = ""


class CleanupStartedEvent(DomainEvent):
    event_type: str = "cleanup.started"


class CleanupCompletedEvent(DomainEvent):
    event_type: str = "cleanup.completed"
    deleted_users: int = 0
    deleted_groups: int = 0


class DriftDetectedEvent(DomainEvent):
    event_type: str = "drift.detected"
    drift_summary: str = ""


class DriftCleanEvent(DomainEvent):
    event_type: str = "drift.clean"


class ReconcileStartedEvent(DomainEvent):
    event_type: str = "reconcile.started"
    total_changes: int = 0


class ReconcileCompletedEvent(DomainEvent):
    event_type: str = "reconcile.completed"
    applied: int = 0
    failed: int = 0


class ConfigPatchedEvent(DomainEvent):
    event_type: str = "config.patched"
    fields: list[str] = Field(default_factory=list)


class ScheduleExecutedEvent(DomainEvent):
    event_type: str = "schedule.executed"
    action: str = ""
    schedule_id: str = ""


class OperationStartedEvent(DomainEvent):
    event_type: str = "operation.started"
    operation_id: str = ""
    operation_type: str = ""


class OperationCompletedEvent(DomainEvent):
    event_type: str = "operation.completed"
    operation_id: str = ""
    duration_seconds: float = 0.0


# ═══════════════════════════════════════════════════════════════════
# Event Bus
# ═══════════════════════════════════════════════════════════════════

class EventBus:
    """In-process async event bus with glob-pattern subscriptions.

    Handlers are invoked concurrently via ``asyncio.gather`` but
    failures in one handler do not affect others.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._history: list[DomainEvent] = []
        self._max_history = 500

    def subscribe(self, pattern: str, handler: EventHandler) -> None:
        """Subscribe *handler* to events matching glob *pattern*.

        Examples: ``"provision.*"``, ``"*"``, ``"drift.detected"``
        """
        self._handlers[pattern].append(handler)

    def unsubscribe(self, pattern: str, handler: EventHandler) -> None:
        handlers = self._handlers.get(pattern, [])
        if handler in handlers:
            handlers.remove(handler)

    async def publish(self, event: DomainEvent) -> None:
        """Publish an event to all matching subscribers."""
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        matched: list[EventHandler] = []
        for pattern, handlers in self._handlers.items():
            if fnmatch.fnmatch(event.event_type, pattern):
                matched.extend(handlers)

        if not matched:
            return

        results = await asyncio.gather(
            *(self._safe_call(h, event) for h in matched),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.error("Event handler error for %s: %s", event.event_type, r)

    async def _safe_call(self, handler: EventHandler, event: DomainEvent) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception("Handler %s failed for event %s", handler.__name__, event.event_type)
            raise

    def recent(self, limit: int = 50) -> list[DomainEvent]:
        """Return most recent events."""
        return list(reversed(self._history[-limit:]))


# ── Singleton ────────────────────────────────────────────────────────

_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus

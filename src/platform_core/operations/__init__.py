"""Operation engine — tracks every mutation as a first-class entity
with steps, timing, retries, and rollback metadata.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from platform_core.events import EventBus, OperationCompletedEvent, OperationStartedEvent
from platform_core.models.operation import Operation, OperationStatus, OperationType

logger = logging.getLogger(__name__)


class OperationEngine:
    """Manages the lifecycle of tracked operations.

    Every provisioning, cleanup, reconciliation, or administrative
    action is wrapped in an Operation with step-level tracking.
    """

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        persist_fn: Any = None,
    ) -> None:
        self._event_bus = event_bus
        self._persist_fn = persist_fn  # async fn(operation) to save
        self._active: dict[UUID, Operation] = {}
        self._history: list[Operation] = []
        self._lock = asyncio.Lock()
        self._max_history = 500

    async def start(
        self,
        op_type: OperationType,
        hack_prefix: str,
        *,
        actor: str = "system",
        deadline_seconds: int | None = None,
    ) -> Operation:
        """Create and start a new operation."""
        op = Operation(
            type=op_type,
            hack_prefix=hack_prefix,
            actor=actor,
        )
        if deadline_seconds:
            op.deadline = datetime.utcnow()

        op.start()

        async with self._lock:
            self._active[op.id] = op

        if self._event_bus:
            await self._event_bus.publish(OperationStartedEvent(
                hack_prefix=hack_prefix,
                operation_id=str(op.id),
                operation_type=op_type.value,
                actor=actor,
            ))

        logger.info("Operation started: %s [%s] prefix=%s", op.type.value, op.id, hack_prefix)
        return op

    async def add_step(self, op_id: UUID, step_name: str) -> None:
        """Add and start a step within an operation."""
        async with self._lock:
            op = self._active.get(op_id)
            if op:
                op.add_step(step_name)
                logger.debug("Step started: %s → %s", op_id, step_name)

    async def complete_step(self, op_id: UUID, step_name: str, *, result: dict | None = None) -> None:
        async with self._lock:
            op = self._active.get(op_id)
            if op:
                op.complete_step(step_name, result=result)

    async def fail_step(self, op_id: UUID, step_name: str, error: str) -> None:
        async with self._lock:
            op = self._active.get(op_id)
            if op:
                op.fail_step(step_name, error)

    async def complete(self, op_id: UUID, *, result: dict | None = None) -> Operation | None:
        """Mark an operation as completed."""
        async with self._lock:
            op = self._active.pop(op_id, None)
            if not op:
                return None
            op.complete(result=result)
            self._history.append(op)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        if self._persist_fn:
            await self._persist_fn(op)

        if self._event_bus:
            await self._event_bus.publish(OperationCompletedEvent(
                hack_prefix=op.hack_prefix,
                operation_id=str(op.id),
                duration_seconds=op.duration_seconds or 0,
            ))

        logger.info("Operation completed: %s [%s] in %.1fs", op.type.value, op.id, op.duration_seconds or 0)
        return op

    async def fail(self, op_id: UUID, error: str) -> Operation | None:
        """Mark an operation as failed."""
        async with self._lock:
            op = self._active.pop(op_id, None)
            if not op:
                return None
            op.fail(error)
            self._history.append(op)

        if self._persist_fn:
            await self._persist_fn(op)

        logger.error("Operation failed: %s [%s] — %s", op.type.value, op.id, error)
        return op

    async def cancel(self, op_id: UUID) -> Operation | None:
        async with self._lock:
            op = self._active.pop(op_id, None)
            if not op:
                return None
            op.cancel()
            self._history.append(op)
        return op

    def get_active(self) -> list[Operation]:
        return list(self._active.values())

    def get_history(self, *, prefix: str = "", limit: int = 50) -> list[Operation]:
        ops = self._history
        if prefix:
            ops = [o for o in ops if o.hack_prefix == prefix]
        return list(reversed(ops[-limit:]))

    def get(self, op_id: UUID) -> Operation | None:
        return self._active.get(op_id) or next(
            (o for o in self._history if o.id == op_id), None
        )

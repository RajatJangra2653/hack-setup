"""Scheduler service — lifecycle scheduling and cleanup policies.

Handles:
  - Scheduled provisioning (activate at time T)
  - Cleanup/archival at end-of-hack
  - Read-only mode transitions
  - Recurring drift checks
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine
from uuid import uuid4

from platform_core.events import EventBus, DomainEvent
from platform_core.models.schedule import ScheduleAction, ScheduleDefinition, ScheduleStatus

logger = logging.getLogger(__name__)


class SchedulerService:
    """Async lifecycle scheduler with pluggable action handlers."""

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        check_interval: int = 60,
    ) -> None:
        self._handlers: dict[ScheduleAction, Callable[..., Coroutine]] = {}
        self._schedules: dict[str, ScheduleDefinition] = {}
        self._event_bus = event_bus
        self._check_interval = check_interval
        self._running = False
        self._task: asyncio.Task | None = None

    # ── Registration ─────────────────────────────────────────────

    def register_handler(
        self,
        action: ScheduleAction,
        handler: Callable[..., Coroutine],
    ) -> None:
        """Register a handler for a schedule action type."""
        self._handlers[action] = handler
        logger.info("Registered handler for schedule action: %s", action.value)

    # ── Schedule CRUD ────────────────────────────────────────────

    async def create_schedule(
        self,
        prefix: str,
        action: ScheduleAction,
        scheduled_at: datetime,
        *,
        config: dict[str, Any] | None = None,
        created_by: str = "system",
    ) -> ScheduleDefinition:
        schedule = ScheduleDefinition(
            id=str(uuid4()),
            hack_prefix=prefix,
            action=action,
            scheduled_at=scheduled_at,
            config=config or {},
            created_by=created_by,
        )
        self._schedules[schedule.id] = schedule
        logger.info(
            "Scheduled %s for %s at %s",
            action.value, prefix, scheduled_at.isoformat()
        )
        return schedule

    async def cancel_schedule(self, schedule_id: str) -> bool:
        schedule = self._schedules.get(schedule_id)
        if schedule and schedule.status == ScheduleStatus.PENDING:
            schedule.status = ScheduleStatus.CANCELLED
            return True
        return False

    async def list_schedules(
        self,
        prefix: str | None = None,
        *,
        status: ScheduleStatus | None = None,
    ) -> list[ScheduleDefinition]:
        items = list(self._schedules.values())
        if prefix:
            items = [s for s in items if s.hack_prefix == prefix]
        if status:
            items = [s for s in items if s.status == status]
        return sorted(items, key=lambda s: s.scheduled_at)

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Scheduler started (interval=%ds)", self._check_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler stopped")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._check_due()
            except Exception:
                logger.exception("Scheduler check failed")
            await asyncio.sleep(self._check_interval)

    async def _check_due(self) -> None:
        now = datetime.utcnow()
        due = [
            s for s in self._schedules.values()
            if s.status == ScheduleStatus.PENDING and s.scheduled_at <= now
        ]
        for schedule in due:
            await self._execute(schedule)

    async def _execute(self, schedule: ScheduleDefinition) -> None:
        handler = self._handlers.get(schedule.action)
        if not handler:
            logger.warning("No handler for action %s", schedule.action.value)
            schedule.status = ScheduleStatus.FAILED
            schedule.error = f"No handler registered for {schedule.action.value}"
            return

        schedule.status = ScheduleStatus.RUNNING
        schedule.executed_at = datetime.utcnow()
        logger.info("Executing schedule %s: %s for %s", schedule.id, schedule.action.value, schedule.hack_prefix)

        try:
            result = await handler(schedule.hack_prefix, schedule.config)
            schedule.status = ScheduleStatus.COMPLETED
            schedule.result = result or {}

            if self._event_bus:
                await self._event_bus.publish(DomainEvent(
                    event_type="schedule.executed",
                    hack_prefix=schedule.hack_prefix,
                    data={
                        "schedule_id": schedule.id,
                        "action": schedule.action.value,
                        "result": schedule.result,
                    },
                ))
        except Exception as exc:
            schedule.status = ScheduleStatus.FAILED
            schedule.error = str(exc)
            schedule.retry_count += 1
            logger.error("Schedule %s failed: %s", schedule.id, exc)

            if schedule.retry_count < schedule.max_retries:
                schedule.status = ScheduleStatus.PENDING
                schedule.scheduled_at = datetime.utcnow() + timedelta(minutes=5 * schedule.retry_count)
                logger.info("Retrying schedule %s at %s", schedule.id, schedule.scheduled_at.isoformat())

    # ── Convenience: lifecycle scheduling ─────────────────────────

    async def schedule_lifecycle(
        self,
        prefix: str,
        *,
        provision_at: datetime | None = None,
        readonly_at: datetime | None = None,
        cleanup_at: datetime | None = None,
        created_by: str = "system",
    ) -> list[ScheduleDefinition]:
        """Schedule full lifecycle events for a hack."""
        schedules = []
        if provision_at:
            schedules.append(await self.create_schedule(
                prefix, ScheduleAction.PROVISION, provision_at, created_by=created_by
            ))
        if readonly_at:
            schedules.append(await self.create_schedule(
                prefix, ScheduleAction.SET_READONLY, readonly_at, created_by=created_by
            ))
        if cleanup_at:
            schedules.append(await self.create_schedule(
                prefix, ScheduleAction.CLEANUP, cleanup_at, created_by=created_by
            ))
        return schedules

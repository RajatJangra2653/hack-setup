"""Scheduler API."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from platform_core.api.deps import SchedulerDep
from platform_core.models.schedule import ScheduleAction

router = APIRouter(prefix="/hacks/{prefix}/schedules", tags=["scheduler"])


class ScheduleCreateRequest(BaseModel):
    action: str
    scheduled_at: datetime
    config: dict = {}


@router.get("")
async def list_schedules(prefix: str, scheduler: SchedulerDep, status: str | None = None):
    """List schedules for a hack."""
    schedules = await scheduler.list_schedules(prefix)
    return {"schedules": [vars(s) for s in schedules], "total": len(schedules)}


@router.post("", status_code=201)
async def create_schedule(
    prefix: str,
    request: ScheduleCreateRequest,
    scheduler: SchedulerDep,
):
    """Create a scheduled action."""
    action = ScheduleAction(request.action)
    schedule = await scheduler.create_schedule(
        prefix, action, request.scheduled_at, config=request.config
    )
    return {"schedule_id": schedule.id, "status": "pending"}


@router.delete("/{schedule_id}")
async def cancel_schedule(prefix: str, schedule_id: str, scheduler: SchedulerDep):
    """Cancel a scheduled action."""
    success = await scheduler.cancel_schedule(schedule_id)
    return {"schedule_id": schedule_id, "cancelled": success}


class LifecycleRequest(BaseModel):
    provision_at: datetime | None = None
    readonly_at: datetime | None = None
    cleanup_at: datetime | None = None


@router.post("/lifecycle")
async def schedule_lifecycle(
    prefix: str,
    request: LifecycleRequest,
    scheduler: SchedulerDep,
):
    """Schedule full lifecycle events."""
    schedules = await scheduler.schedule_lifecycle(
        prefix,
        provision_at=request.provision_at,
        readonly_at=request.readonly_at,
        cleanup_at=request.cleanup_at,
    )
    return {"schedules": [vars(s) for s in schedules], "total": len(schedules)}

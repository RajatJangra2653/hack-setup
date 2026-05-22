"""Audit log API."""

from __future__ import annotations

from fastapi import APIRouter

from platform_core.api.deps import AuditDep

router = APIRouter(prefix="/hacks/{prefix}/audit", tags=["audit"])


@router.get("")
async def query_audit_log(
    prefix: str,
    audit: AuditDep,
    event_type: str | None = None,
    actor: str | None = None,
    severity: str | None = None,
    limit: int = 50,
):
    """Query audit events for a hack."""
    events = await audit.query(
        prefix=prefix,
        event_type=event_type,
        actor=actor,
        limit=limit,
    )
    return {
        "events": [vars(e) for e in events],
        "total": len(events),
    }


@router.get("/summary")
async def audit_summary(prefix: str, audit: AuditDep):
    """Get audit summary for a hack."""
    events = await audit.query(prefix=prefix, limit=1000)
    by_type: dict[str, int] = {}
    for e in events:
        by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
    return {
        "prefix": prefix,
        "total_events": len(events),
        "by_type": by_type,
    }

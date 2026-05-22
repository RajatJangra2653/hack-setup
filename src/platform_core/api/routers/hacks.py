"""Hack CRUD and lifecycle API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from platform_core.api.deps import OperationDep, AuditDep

router = APIRouter(prefix="/hacks", tags=["hacks"])


class HackCreateRequest(BaseModel):
    prefix: str
    name: str
    domain: str
    team_count: int = 1
    users_per_team: int = 5
    licenses: list[str] = []
    config: dict[str, Any] = {}


class HackUpdateRequest(BaseModel):
    name: str | None = None
    team_count: int | None = None
    users_per_team: int | None = None
    licenses: list[str] | None = None
    config: dict[str, Any] | None = None


@router.get("")
async def list_hacks(
    status: str | None = None,
    limit: int = 50,
):
    """List all hacks, optionally filtered by status."""
    # TODO: wire to repository
    return {"hacks": [], "total": 0}


@router.post("", status_code=201)
async def create_hack(
    request: HackCreateRequest,
    operations: OperationDep,
    audit: AuditDep,
):
    """Create a new hack environment."""
    return {"prefix": request.prefix, "status": "draft"}


@router.get("/{prefix}")
async def get_hack(prefix: str):
    """Get hack details by prefix."""
    return {"prefix": prefix, "status": "unknown"}


@router.patch("/{prefix}")
async def update_hack(prefix: str, request: HackUpdateRequest):
    """Update hack configuration."""
    return {"prefix": prefix, "updated": True}


@router.post("/{prefix}/provision")
async def provision_hack(
    prefix: str,
    operations: OperationDep,
    force: bool = False,
):
    """Trigger provisioning for a hack."""
    op = await operations.start(
        hack_prefix=prefix,
        operation_type="provision",
        actor="api",
    )
    return {"operation_id": str(op.id), "status": "started"}


@router.post("/{prefix}/cleanup")
async def cleanup_hack(
    prefix: str,
    operations: OperationDep,
    force: bool = False,
):
    """Trigger cleanup for a hack."""
    if not force:
        raise HTTPException(
            status_code=400,
            detail="Destructive action requires force=true"
        )
    op = await operations.start(
        hack_prefix=prefix,
        operation_type="cleanup",
        actor="api",
    )
    return {"operation_id": str(op.id), "status": "started"}


@router.post("/{prefix}/archive")
async def archive_hack(prefix: str, force: bool = False):
    """Archive a hack."""
    return {"prefix": prefix, "status": "archived"}


@router.get("/{prefix}/status")
async def hack_status(prefix: str):
    """Get hack provisioning status."""
    return {"prefix": prefix, "status": "unknown", "progress": 0}

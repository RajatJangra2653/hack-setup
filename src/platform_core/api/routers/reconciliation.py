"""Reconciliation & drift detection API."""

from __future__ import annotations

from fastapi import APIRouter

from platform_core.api.deps import ReconciliationDep

router = APIRouter(prefix="/hacks/{prefix}/reconciliation", tags=["reconciliation"])


@router.get("/drift")
async def detect_drift(prefix: str, reconciliation: ReconciliationDep):
    """Detect drift between desired and actual state."""
    # TODO: wire providers + desired state
    return {"prefix": prefix, "drift": [], "has_drift": False}


@router.post("/plan")
async def create_plan(prefix: str, reconciliation: ReconciliationDep):
    """Generate a reconciliation plan."""
    return {"prefix": prefix, "plan": [], "changes": 0}


@router.post("/apply")
async def apply_plan(
    prefix: str,
    reconciliation: ReconciliationDep,
    dry_run: bool = True,
    force: bool = False,
):
    """Apply a reconciliation plan."""
    return {
        "prefix": prefix,
        "applied": not dry_run,
        "dry_run": dry_run,
        "results": [],
    }


@router.get("/history")
async def reconciliation_history(prefix: str, limit: int = 20):
    """Get reconciliation history."""
    return {"prefix": prefix, "history": []}

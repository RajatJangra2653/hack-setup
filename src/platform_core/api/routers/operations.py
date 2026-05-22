"""Operations tracking API."""

from __future__ import annotations

from fastapi import APIRouter

from platform_core.api.deps import OperationDep

router = APIRouter(prefix="/operations", tags=["operations"])


@router.get("")
async def list_operations(
    operations: OperationDep,
    prefix: str | None = None,
    status: str | None = None,
    limit: int = 50,
):
    """List operations, optionally filtered."""
    active = await operations.list_active()
    return {"operations": [vars(o) for o in active], "total": len(active)}


@router.get("/{operation_id}")
async def get_operation(operation_id: str, operations: OperationDep):
    """Get operation details."""
    op = await operations.get(operation_id)
    if op:
        return vars(op)
    return {"error": "not_found"}


@router.post("/{operation_id}/cancel")
async def cancel_operation(operation_id: str, operations: OperationDep):
    """Cancel a running operation."""
    success = await operations.cancel(operation_id)
    return {"operation_id": operation_id, "cancelled": success}

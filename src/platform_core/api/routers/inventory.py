"""Inventory API."""

from __future__ import annotations

from fastapi import APIRouter

from platform_core.api.deps import InventoryDep

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.get("")
async def list_inventory(
    inventory: InventoryDep,
    prefix: str | None = None,
    resource_type: str | None = None,
    provider: str | None = None,
):
    """List inventory items."""
    if prefix:
        items = await inventory.list_by_hack(prefix)
    else:
        items = list(inventory._items.values())
    return {"items": [vars(i) for i in items], "total": len(items)}


@router.get("/snapshot")
async def inventory_snapshot(inventory: InventoryDep, prefix: str = ""):
    """Get a point-in-time inventory snapshot."""
    snap = await inventory.snapshot(prefix)
    return vars(snap)


@router.get("/expired")
async def expired_items(inventory: InventoryDep):
    """List expired inventory items."""
    items = await inventory.list_expired()
    return {"items": [vars(i) for i in items], "total": len(items)}


@router.get("/drifted")
async def drifted_items(inventory: InventoryDep):
    """List drifted inventory items."""
    items = await inventory.list_drifted()
    return {"items": [vars(i) for i in items], "total": len(items)}

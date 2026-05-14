"""User management API."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/hacks/{prefix}/users", tags=["users"])


class UserCreateRequest(BaseModel):
    display_name: str
    team: str = ""
    role: str = "participant"


@router.get("")
async def list_users(prefix: str, team: str | None = None):
    """List users in a hack."""
    return {"users": [], "total": 0}


@router.post("", status_code=201)
async def create_user(prefix: str, request: UserCreateRequest):
    """Create a user in a hack."""
    return {"prefix": prefix, "user": request.display_name, "status": "created"}


@router.get("/{user_id}")
async def get_user(prefix: str, user_id: str):
    """Get user details."""
    return {"user_id": user_id, "prefix": prefix}


@router.delete("/{user_id}")
async def delete_user(prefix: str, user_id: str, force: bool = False):
    """Delete a user."""
    return {"user_id": user_id, "deleted": True}


@router.post("/{user_id}/reset-password")
async def reset_password(prefix: str, user_id: str):
    """Reset user password."""
    return {"user_id": user_id, "password_reset": True}


@router.post("/{user_id}/tap")
async def issue_tap(prefix: str, user_id: str):
    """Issue a Temporary Access Pass for a user."""
    return {"user_id": user_id, "tap_issued": True}

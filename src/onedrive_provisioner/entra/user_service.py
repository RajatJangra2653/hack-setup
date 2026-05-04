"""User creation / lookup against Microsoft Graph."""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from typing import Optional

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger
from .models import UserPlan

logger = get_logger(__name__)


def generate_password(length: int = 20) -> str:
    """Cryptographically random password meeting Entra complexity rules."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw)
                and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in "!@#$%^&*" for c in pw)):
            return pw


class UserService:
    def __init__(self, graph: GraphClient, *, default_password: Optional[str] = None,
                 hack_name: str = "", created_by: str = "",
                 force_change_password: bool = False) -> None:
        self._g = graph
        self._default_password = default_password
        self._hack_name = hack_name
        self._created_by = created_by
        self._force_change = force_change_password

    async def get_by_upn(self, upn: str) -> Optional[dict]:
        """Return user object or None if not found."""
        try:
            return await self._g.get(
                f"/users/{upn}",
                params={"$select": "id,userPrincipalName,displayName,accountEnabled"},
            )
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise

    async def create(self, plan: UserPlan) -> tuple[dict, str]:
        """Create a user. Returns (user, password). Caller should check existence first if idempotency is needed."""
        password = self._default_password or generate_password()
        body = {
            "accountEnabled": True,
            "displayName": plan.display_name,
            "mailNickname": plan.mail_nickname,
            "userPrincipalName": plan.upn,
            "passwordProfile": {
                "forceChangePasswordNextSignIn": self._force_change,
                "password": password,
            },
            "usageLocation": "US",  # required for license assignment
        }
        # Phase A — stamp hack metadata into searchable native attributes
        if self._hack_name:
            body["companyName"] = self._hack_name[:64]
        if plan.team:
            body["department"] = plan.team
        if self._created_by:
            # jobTitle is queryable + visible in admin center
            body["jobTitle"] = f"hack:{self._created_by}"[:128]
        user = await self._g.post("/users", json=body)
        logger.info("entra.user.created", upn=plan.upn, user_id=user.get("id"))
        return user, password

    async def ensure(self, plan: UserPlan, *, skip_existing: bool = True) -> tuple[dict, bool, Optional[str]]:
        """Returns (user, created_now, password). password is None for existing users."""
        existing = await self.get_by_upn(plan.upn)
        if existing:
            if not skip_existing:
                raise GraphError(409, "Conflict", f"User {plan.upn} already exists")
            return existing, False, None
        user, password = await self.create(plan)
        return user, True, password

    async def reset_password(
        self, user_id: str, *, password: Optional[str] = None,
        force_change: bool = False,
    ) -> Optional[str]:
        """Reset password for an existing user. Returns new password or None on failure."""
        new_pw = password or generate_password()
        body = {
            "passwordProfile": {
                "forceChangePasswordNextSignIn": force_change,
                "password": new_pw,
            },
        }
        try:
            await self._g.patch(f"/users/{user_id}", json=body)
            logger.info("entra.user.password_reset", user_id=user_id)
            return new_pw
        except GraphError as exc:
            logger.warning("entra.user.password_reset_failed", user_id=user_id,
                           status=exc.status, code=exc.code, msg=str(exc))
            return None

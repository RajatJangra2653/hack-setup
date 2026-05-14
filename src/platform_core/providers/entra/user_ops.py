"""Entra ID user operations — CRUD, password, TAP."""

from __future__ import annotations

import logging
import secrets
import string

from platform_core.core import JsonDict
from platform_core.providers.graph.client import GraphClient

logger = logging.getLogger(__name__)

_PWD_ALPHABET = string.ascii_letters + string.digits + "!@#$%&*"
_PWD_LENGTH = 20


def _generate_password() -> str:
    """Generate a cryptographically random password."""
    while True:
        pwd = "".join(secrets.choice(_PWD_ALPHABET) for _ in range(_PWD_LENGTH))
        if (
            any(c.isupper() for c in pwd)
            and any(c.islower() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!@#$%&*" for c in pwd)
        ):
            return pwd


class UserOps:
    """Entra ID user CRUD operations."""

    def __init__(self, client: GraphClient) -> None:
        self._client = client

    async def get_by_upn(self, upn: str) -> JsonDict | None:
        try:
            return await self._client.get(f"/users/{upn}")
        except Exception:
            return None

    async def create(
        self,
        upn: str,
        display_name: str,
        mail_nickname: str,
        *,
        password: str = "",
        department: str = "",
        job_title: str = "",
        company_name: str = "",
    ) -> tuple[JsonDict, str]:
        """Create a user and return (user_data, password)."""
        pwd = password or _generate_password()
        body: JsonDict = {
            "accountEnabled": True,
            "displayName": display_name,
            "mailNickname": mail_nickname,
            "userPrincipalName": upn,
            "passwordProfile": {
                "password": pwd,
                "forceChangePasswordNextSignIn": False,
            },
            "usageLocation": "US",
        }
        if department:
            body["department"] = department
        if job_title:
            body["jobTitle"] = job_title
        if company_name:
            body["companyName"] = company_name

        data = await self._client.post("/users", json=body)
        return data, pwd

    async def ensure(
        self, upn: str, display_name: str, mail_nickname: str, **kwargs: str
    ) -> tuple[JsonDict, str, bool]:
        """Get-or-create.  Returns (user_data, password, was_created)."""
        existing = await self.get_by_upn(upn)
        if existing:
            return existing, "", False
        data, pwd = await self.create(upn, display_name, mail_nickname, **kwargs)
        return data, pwd, True

    async def disable(self, user_id: str) -> None:
        await self._client.patch(f"/users/{user_id}", json={"accountEnabled": False})

    async def enable(self, user_id: str) -> None:
        await self._client.patch(f"/users/{user_id}", json={"accountEnabled": True})

    async def delete(self, user_id: str) -> None:
        await self._client.delete(f"/users/{user_id}")

    async def reset_password(self, user_id: str, *, password: str = "") -> str:
        pwd = password or _generate_password()
        await self._client.patch(f"/users/{user_id}", json={
            "passwordProfile": {"password": pwd, "forceChangePasswordNextSignIn": False}
        })
        return pwd

    async def find_by_prefix(self, prefix: str) -> list[JsonDict]:
        """Find users whose mailNickname starts with prefix."""
        return await self._client.get_paginated(
            "/users",
            params={
                "$filter": f"startsWith(mailNickname, '{prefix}')",
                "$select": "id,userPrincipalName,displayName,mailNickname,accountEnabled",
                "$top": "999",
            },
        )

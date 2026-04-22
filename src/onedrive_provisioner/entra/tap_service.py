"""Issue Temporary Access Pass for a user."""
from __future__ import annotations

from typing import Optional

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger

logger = get_logger(__name__)


class TapService:
    def __init__(self, graph: GraphClient, *, lifetime_minutes: int = 120) -> None:
        self._g = graph
        self._lifetime = lifetime_minutes

    async def issue(self, user_id: str, *, usable_once: bool = True) -> Optional[dict]:
        """Create a TAP. Returns dict with temporaryAccessPass + expirationDateTime,
        or None if the tenant policy disallows TAP for this user."""
        body = {
            "lifetimeInMinutes": self._lifetime,
            "isUsableOnce": usable_once,
        }
        try:
            tap = await self._g.post(
                f"/users/{user_id}/authentication/temporaryAccessPassMethods",
                json=body,
            )
            logger.info("entra.tap.issued", user_id=user_id,
                        expires=tap.get("lifetimeInMinutes"))
            return tap
        except GraphError as exc:
            # Common: 403 if TAP policy not enabled, 400 if user excluded
            logger.warning("entra.tap.failed", user_id=user_id,
                           status=exc.status, code=exc.code, msg=str(exc))
            return None

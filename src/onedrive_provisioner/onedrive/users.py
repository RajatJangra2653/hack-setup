"""User lookup helpers."""
from __future__ import annotations

from typing import AsyncIterator, List

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger

logger = get_logger(__name__)


class UserResolver:
    """Resolves UPNs / object IDs to Graph user objects."""

    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph

    async def resolve(self, identifier: str) -> dict:
        """Return the user object or raise GraphError."""
        # Graph accepts both UPN and object id directly in /users/{id}
        return await self._graph.get(
            f"/users/{identifier}",
            params={"$select": "id,userPrincipalName,displayName,accountEnabled,userType"},
        )

    async def resolve_many(self, identifiers: List[str]) -> List[tuple[str, dict | None, str | None]]:
        out: list[tuple[str, dict | None, str | None]] = []
        for ident in identifiers:
            try:
                u = await self.resolve(ident)
                out.append((ident, u, None))
            except GraphError as exc:
                logger.warning("user.resolve_failed", user=ident, error=str(exc))
                out.append((ident, None, str(exc)))
        return out

    async def list_all_members(self) -> AsyncIterator[dict]:
        """Iterate all enabled member users in tenant."""
        params = {
            "$select": "id,userPrincipalName,displayName,accountEnabled,userType",
            "$filter": "userType eq 'Member' and accountEnabled eq true",
            "$top": "999",
        }
        async for u in self._graph.paged("/users", params=params):
            yield u

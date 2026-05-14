"""Auth module — MSAL token provider and credential management."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Sequence

import msal

logger = logging.getLogger(__name__)

_DEFAULT_SCOPES = ["https://graph.microsoft.com/.default"]
_ARM_SCOPES = ["https://management.azure.com/.default"]
_REFRESH_SKEW = 300  # Refresh 5 minutes before expiry


class TokenProvider:
    """MSAL-based async token provider with caching.

    Supports:
      - Client secret authentication
      - Certificate + thumbprint authentication
      - Managed identity (when deployed to Azure)

    Thread-safe via asyncio.Lock.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        *,
        client_secret: str = "",
        cert_path: str = "",
        cert_thumbprint: str = "",
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[str, float]] = {}

        # Build MSAL app
        kwargs: dict[str, Any] = {
            "client_id": client_id,
            "authority": f"https://login.microsoftonline.com/{tenant_id}",
        }
        if cert_path and cert_thumbprint:
            with open(cert_path, "r") as f:
                pem = f.read()
            kwargs["client_credential"] = {
                "private_key": pem,
                "thumbprint": cert_thumbprint,
            }
        elif client_secret:
            kwargs["client_credential"] = client_secret
        else:
            raise ValueError("Either client_secret or cert_path+cert_thumbprint required")

        self._app = msal.ConfidentialClientApplication(**kwargs)

    async def get_token(self, scopes: Sequence[str] | None = None) -> str:
        """Get a bearer token for the given scopes."""
        scope_key = ",".join(scopes or _DEFAULT_SCOPES)

        async with self._lock:
            # Check cache
            cached = self._cache.get(scope_key)
            if cached:
                token, expires_at = cached
                if time.time() < expires_at - _REFRESH_SKEW:
                    return token

            # Acquire new token
            result = self._app.acquire_token_for_client(
                scopes=list(scopes or _DEFAULT_SCOPES)
            )
            if "access_token" not in result:
                error = result.get("error_description", result.get("error", "Unknown"))
                raise RuntimeError(f"Token acquisition failed: {error}")

            token = result["access_token"]
            expires_in = result.get("expires_in", 3600)
            self._cache[scope_key] = (token, time.time() + expires_in)
            return token

    async def get_graph_token(self) -> str:
        return await self.get_token(_DEFAULT_SCOPES)

    async def get_arm_token(self) -> str:
        return await self.get_token(_ARM_SCOPES)

    async def get_token_for_scope(self, scope: str) -> str:
        return await self.get_token([scope])

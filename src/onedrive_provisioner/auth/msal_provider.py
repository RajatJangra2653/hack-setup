"""MSAL-based token provider for Microsoft Graph (client credentials flow).

Supports:
  * Client secret
  * Certificate (PEM file containing private key + cert) with thumbprint

Tokens are cached in-memory and refreshed ~5 minutes before expiry.
Thread/async-safe via an asyncio lock.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Protocol

import msal

from ..config import AzureConfig
from ..logging_setup import get_logger

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
_AUTHORITY_TPL = "https://login.microsoftonline.com/{tenant}"
_REFRESH_SKEW_SEC = 300

logger = get_logger(__name__)


class TokenProvider(Protocol):
    async def get_token(self) -> str: ...


class MsalTokenProvider:
    def __init__(self, cfg: AzureConfig) -> None:
        self._cfg = cfg
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._app: msal.ConfidentialClientApplication | None = None

    def _build_app(self) -> msal.ConfidentialClientApplication:
        if self._app is not None:
            return self._app
        authority = _AUTHORITY_TPL.format(tenant=self._cfg.tenant_id)
        if self._cfg.cert_path and self._cfg.cert_thumbprint:
            pem = Path(self._cfg.cert_path).read_text(encoding="utf-8")
            credential = {
                "private_key": pem,
                "thumbprint": self._cfg.cert_thumbprint,
            }
            logger.info("auth.cert_mode", thumbprint=self._cfg.cert_thumbprint[-6:])
        else:
            credential = self._cfg.client_secret
            logger.info("auth.secret_mode")
        self._app = msal.ConfidentialClientApplication(
            client_id=self._cfg.client_id,
            authority=authority,
            client_credential=credential,
        )
        return self._app

    async def get_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - _REFRESH_SKEW_SEC:
            return self._token
        async with self._lock:
            now = time.time()
            if self._token and now < self._expires_at - _REFRESH_SKEW_SEC:
                return self._token
            app = self._build_app()
            # MSAL is synchronous; offload to thread.
            result = await asyncio.to_thread(
                app.acquire_token_for_client, scopes=GRAPH_SCOPE
            )
            if not result or "access_token" not in result:
                err = (result or {}).get("error_description") or "unknown error"
                raise RuntimeError(f"Failed to acquire Graph token: {err}")
            self._token = result["access_token"]
            self._expires_at = now + int(result.get("expires_in", 3600))
            logger.info("auth.token_acquired", expires_in=result.get("expires_in"))
            return self._token

    async def get_token_for_scope(self, scopes: list[str]) -> str:
        """Acquire a token for an arbitrary scope (e.g. SharePoint Admin)."""
        app = self._build_app()
        result = await asyncio.to_thread(
            app.acquire_token_for_client, scopes=scopes
        )
        if not result or "access_token" not in result:
            err = (result or {}).get("error_description") or "unknown error"
            raise RuntimeError(f"Failed to acquire token for {scopes}: {err}")
        return result["access_token"]

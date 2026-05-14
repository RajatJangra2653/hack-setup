"""Async Microsoft Graph HTTP client with automatic retry, throttling,
and token refresh.

This is the low-level transport layer.  Higher-level providers
(Entra, OneDrive, etc.) use this client for all Graph API calls.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from platform_core.core.errors import GraphApiError, ThrottledError

logger = logging.getLogger(__name__)

_RETRIABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphClient:
    """Async Microsoft Graph client with retry and throttle handling.

    Args:
        token_provider: An async callable returning a bearer token string.
        base_url: Graph API base URL (default v1.0).
        max_retries: Maximum retry attempts for retriable errors.
        timeout: HTTP read timeout in seconds.
        concurrency: Max concurrent requests (semaphore limit).
    """

    def __init__(
        self,
        token_provider: Any,
        *,
        base_url: str = _GRAPH_BASE,
        max_retries: int = 6,
        timeout: int = 120,
        concurrency: int = 8,
    ) -> None:
        self._token_provider = token_provider
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(concurrency)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> GraphClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=30),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Public API ───────────────────────────────────────────────

    async def get(self, path: str, *, params: dict | None = None, headers: dict | None = None) -> Any:
        return await self.request("GET", path, params=params, headers=headers)

    async def post(self, path: str, *, json: Any = None, headers: dict | None = None) -> Any:
        return await self.request("POST", path, json=json, headers=headers)

    async def patch(self, path: str, *, json: Any = None) -> Any:
        return await self.request("PATCH", path, json=json)

    async def put(self, path: str, *, content: bytes | None = None, headers: dict | None = None) -> Any:
        return await self.request("PUT", path, content=content, headers=headers)

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path, expect_json=False)

    async def get_paginated(self, path: str, *, params: dict | None = None, max_pages: int = 50) -> list[dict]:
        """Follow @odata.nextLink to collect all pages."""
        results: list[dict] = []
        url = path
        p = params
        for _ in range(max_pages):
            data = await self.get(url, params=p)
            results.extend(data.get("value", []))
            next_link = data.get("@odata.nextLink")
            if not next_link:
                break
            url = next_link
            p = None  # nextLink includes params
        return results

    # ── Core request method ──────────────────────────────────────

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: dict | None = None,
        expect_json: bool = True,
        allow_status: set[int] | None = None,
    ) -> Any:
        """Execute an HTTP request with automatic retry."""
        if not url.startswith("http"):
            url = f"{self._base_url}{url}"

        async with self._semaphore:
            return await self._request_with_retry(
                method, url,
                params=params, json=json, content=content,
                headers=headers, expect_json=expect_json,
                allow_status=allow_status or set(),
            )

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict | None,
        json: Any,
        content: bytes | None,
        headers: dict | None,
        expect_json: bool,
        allow_status: set[int],
    ) -> Any:
        client = self._ensure_client()
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                token = await self._get_token()
                req_headers = {"Authorization": f"Bearer {token}"}
                if headers:
                    req_headers.update(headers)

                resp = await client.request(
                    method, url,
                    params=params,
                    json=json,
                    content=content,
                    headers=req_headers,
                )

                if resp.status_code in allow_status:
                    return resp.json() if expect_json and resp.content else resp.text

                if resp.status_code in _RETRIABLE_STATUSES:
                    delay = self._backoff_delay(attempt, resp)
                    logger.warning(
                        "Graph %s %s → %d, retrying in %.1fs (attempt %d/%d)",
                        method, url, resp.status_code, delay, attempt + 1, self._max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code >= 400:
                    body = resp.json() if resp.content else {}
                    error = body.get("error", {})
                    raise GraphApiError(
                        error.get("message", f"HTTP {resp.status_code}"),
                        status=resp.status_code,
                        graph_code=error.get("code"),
                        details=body,
                    )

                if not expect_json or not resp.content:
                    return resp.text
                return resp.json()

            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                last_error = exc
                if attempt < self._max_retries:
                    delay = self._backoff_delay(attempt)
                    logger.warning("Network error on %s %s: %s — retrying in %.1fs", method, url, exc, delay)
                    await asyncio.sleep(delay)
                    continue
                raise GraphApiError(f"Network error after {self._max_retries} retries: {exc}") from exc

        raise GraphApiError(f"Max retries exceeded for {method} {url}") from last_error

    # ── Helpers ──────────────────────────────────────────────────

    def _backoff_delay(self, attempt: int, resp: httpx.Response | None = None) -> float:
        """Compute backoff delay, respecting Retry-After header."""
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after)
                except ValueError:
                    try:
                        dt = parsedate_to_datetime(retry_after)
                        return max(0, (dt - datetime.utcnow()).total_seconds())
                    except Exception:
                        pass
        base = min(2 ** attempt, 64)
        return base + random.uniform(0, base * 0.5)

    async def _get_token(self) -> str:
        if asyncio.iscoroutinefunction(self._token_provider):
            return await self._token_provider()
        if callable(self._token_provider):
            result = self._token_provider()
            if asyncio.iscoroutine(result):
                return await result
            return result
        raise TypeError("token_provider must be callable")

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=30),
                follow_redirects=True,
            )
        return self._client

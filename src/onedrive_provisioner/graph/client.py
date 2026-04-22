"""Async Microsoft Graph client.

Features:
  * Bearer-token injection via TokenProvider
  * Automatic retry with exponential backoff on 429 / 5xx / network errors
  * Honours Retry-After (seconds or HTTP-date) header
  * Streaming uploads (PUT bytes/ranges)
  * Pagination helper for @odata.nextLink
"""
from __future__ import annotations

import asyncio
import random
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping, Optional

import httpx

from ..auth import TokenProvider
from ..logging_setup import get_logger

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_RETRY_STATUS = {408, 429, 500, 502, 503, 504}
_DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=15.0)

logger = get_logger(__name__)


class GraphError(Exception):
    def __init__(self, status: int, code: str | None, message: str, *, body: Any = None):
        super().__init__(f"[{status}] {code or ''} {message}".strip())
        self.status = status
        self.code = code
        self.message = message
        self.body = body


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class GraphClient:
    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        max_retries: int = 6,
        base_url: str = GRAPH_BASE,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._tp = token_provider
        self._max_retries = max_retries
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, http2=False)
        self._owned_client = client is None

    async def __aenter__(self) -> "GraphClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ core
    def _full_url(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if not url.startswith("/"):
            url = "/" + url
        return self._base_url + url

    async def _auth_headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        token = await self._tp.get_token()
        h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        expect_json: bool = True,
        allow_status: tuple[int, ...] = (),
    ) -> Any:
        full_url = self._full_url(url)
        attempt = 0
        while True:
            attempt += 1
            try:
                hdrs = await self._auth_headers(headers)
                resp = await self._client.request(
                    method,
                    full_url,
                    params=params,
                    json=json,
                    content=content,
                    headers=hdrs,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt > self._max_retries:
                    raise GraphError(0, "network", str(exc)) from exc
                delay = self._backoff(attempt, None)
                logger.warning(
                    "graph.network_retry",
                    method=method,
                    url=full_url,
                    attempt=attempt,
                    delay=round(delay, 2),
                    error=str(exc),
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code in allow_status:
                return resp

            if resp.status_code in _RETRY_STATUS and attempt <= self._max_retries:
                ra = _parse_retry_after(resp.headers.get("Retry-After"))
                delay = self._backoff(attempt, ra)
                logger.warning(
                    "graph.http_retry",
                    method=method,
                    url=full_url,
                    status=resp.status_code,
                    attempt=attempt,
                    delay=round(delay, 2),
                    retry_after=ra,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 400:
                self._raise_for_error(resp)

            if not expect_json or resp.status_code == 204 or not resp.content:
                return resp
            try:
                return resp.json()
            except ValueError:
                return resp

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, 60.0)
        # exponential backoff with jitter, capped at 60s
        base = min(60.0, (2 ** (attempt - 1)))
        return base + random.uniform(0, base * 0.25)

    @staticmethod
    def _raise_for_error(resp: httpx.Response) -> None:
        code = None
        message = resp.text
        body: Any = None
        try:
            body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                code = err.get("code")
                message = err.get("message", message)
        except ValueError:
            pass
        raise GraphError(resp.status_code, code, message, body=body)

    # ------------------------------------------------------------- helpers
    async def get(self, url: str, **kw) -> Any:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw) -> Any:
        return await self.request("POST", url, **kw)

    async def put(self, url: str, **kw) -> Any:
        return await self.request("PUT", url, **kw)

    async def patch(self, url: str, **kw) -> Any:
        return await self.request("PATCH", url, **kw)

    async def delete(self, url: str, **kw) -> Any:
        return await self.request("DELETE", url, expect_json=False, **kw)

    async def paged(self, url: str, **kw) -> AsyncIterator[dict]:
        next_url: str | None = url
        params = kw.pop("params", None)
        while next_url:
            page = await self.get(next_url, params=params, **kw)
            params = None  # only first call uses params
            for item in page.get("value", []):
                yield item
            next_url = page.get("@odata.nextLink")

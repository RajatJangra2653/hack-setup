"""Azure Cost Management helpers for subscription-level spend."""
from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from ..auth import MsalTokenProvider
from ..logging_setup import get_logger
from .rbac_service import ARM_BASE, ARM_SCOPE

logger = get_logger(__name__)

COST_API_VERSION = "2023-11-01"
# Cost Management is rate-limited per scope; cap concurrent requests to stay
# under throttle ceiling.  3 is conservative — avoids 429s that cause
# inconsistent totals when some subs silently fail.
_COST_QUERY_CONCURRENCY = 3
_MAX_RETRIES = 5

# ── In-memory cost cache ────────────────────────────────────────────────
# Cost data from Azure is delayed 4-8 hours anyway, so caching for a few
# hours is perfectly safe and eliminates redundant API calls.
_CACHE_TTL_SECS = 4 * 3600  # 4 hours
_cost_cache: Dict[str, Tuple[float, dict]] = {}  # key → (expire_ts, result)


def _normalise_date(value: str, *, end: bool = False) -> str:
    value = (value or "").strip()
    if not value:
        return datetime.now(timezone.utc).isoformat()
    if "T" not in value:
        return f"{value}T{'23:59:59' if end else '00:00:00'}Z"
    if value.endswith("Z") or "+" in value:
        return value
    return f"{value}Z"


# Delay (seconds) between successive cost queries to stay well under
# Azure Cost Management's per-tenant throttle (~10-15 req / 10 s).
_INTER_QUERY_DELAY = 1.5


class CostManagementService:
    """Query Azure Cost Management for actual subscription costs.

    The service principal must have access to read cost data on each target
    subscription (for example, Cost Management Reader or equivalent billing
    permissions).
    """

    def __init__(self, token_provider: MsalTokenProvider) -> None:
        self._tp = token_provider
        # Per-request timeout: connect=10s, read=60s.  Cost Management can
        # be slow for large date ranges; a generous read timeout avoids
        # silently dropping subs as $0 and producing inconsistent totals.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
        )
        self._token: Optional[str] = None

    async def __aenter__(self) -> "CostManagementService":
        self._token = await self._tp.get_token_for_scope(ARM_SCOPE)
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def query_subscription_cost(
        self,
        subscription_id: str,
        *,
        start_date: str,
        end_date: str,
    ) -> dict:
        """Return total actual cost for one subscription and date range."""
        # ── Check cache first ───────────────────────────────────────────
        cache_key = f"{subscription_id}|{start_date}|{end_date}"
        now = time.time()
        cached = _cost_cache.get(cache_key)
        if cached and cached[0] > now:
            logger.debug("cost.cache.hit", subscription=subscription_id)
            return cached[1]

        url = (
            f"{ARM_BASE}/subscriptions/{subscription_id}/providers/"
            f"Microsoft.CostManagement/query?api-version={COST_API_VERSION}"
        )
        body = {
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {
                "from": _normalise_date(start_date),
                "to": _normalise_date(end_date, end=True),
            },
            "dataset": {
                "granularity": "None",
                "aggregation": {
                    "totalCost": {
                        "name": "PreTaxCost",
                        "function": "Sum",
                    }
                },
            },
        }
        resp = None
        last_err = ""
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(url, headers=self._headers(), json=body)
            except httpx.TimeoutException as exc:
                logger.warning("cost.query.timeout", subscription=subscription_id, error=str(exc), attempt=attempt)
                last_err = f"Cost query timed out for subscription {subscription_id}"
                # Retry timeouts with exponential backoff + jitter
                if attempt < _MAX_RETRIES - 1:
                    delay = min(5 * (2 ** attempt) + random.uniform(0, 2), 120)
                    await asyncio.sleep(delay)
                    continue
                return {
                    "subscriptionId": subscription_id, "cost": None, "currency": "",
                    "source": "azure_cost_management", "periodStart": start_date,
                    "periodEnd": end_date, "error": last_err,
                }
            except httpx.HTTPError as exc:
                logger.warning("cost.query.network_error", subscription=subscription_id, error=str(exc))
                return {
                    "subscriptionId": subscription_id, "cost": None, "currency": "",
                    "source": "azure_cost_management", "periodStart": start_date,
                    "periodEnd": end_date, "error": f"Cost query network error: {exc}",
                }
            # 429 / 503 → respect Retry-After with exponential backoff + jitter
            if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                retry_after = resp.headers.get("Retry-After") or resp.headers.get("x-ms-ratelimit-microsoft.costmanagement-entity-retry-after")
                try:
                    base_delay = float(retry_after) if retry_after else 0
                except ValueError:
                    base_delay = 0
                # Exponential backoff: 5, 15, 45, 120 + jitter
                exp_delay = min(5 * (3 ** attempt) + random.uniform(0, 3), 120)
                delay = max(base_delay, exp_delay)
                logger.warning("cost.query.throttled", subscription=subscription_id, attempt=attempt, delay=round(delay, 1), status=resp.status_code)
                await asyncio.sleep(delay)
                continue
            break
        if resp is None:
            return {
                "subscriptionId": subscription_id, "cost": None, "currency": "",
                "source": "azure_cost_management", "periodStart": start_date,
                "periodEnd": end_date, "error": last_err or "Cost query failed without response",
            }
        if resp.status_code != 200:
            logger.warning(
                "cost.query.failed",
                subscription=subscription_id,
                status=resp.status_code,
                body=resp.text[:300],
            )
            err_msg = f"Cost query failed [{resp.status_code}]: {resp.text[:300]}"
            if resp.status_code == 429:
                err_msg = f"Cost query throttled (429) after {_MAX_RETRIES} retries — Azure Cost Management is rate-limiting this subscription. Retry the report in a minute."
            return {
                "subscriptionId": subscription_id,
                "cost": None,
                "currency": "",
                "source": "azure_cost_management",
                "periodStart": start_date,
                "periodEnd": end_date,
                "error": err_msg,
            }

        payload = resp.json().get("properties", {})
        columns = payload.get("columns", [])
        rows = payload.get("rows", [])
        names = [c.get("name", "") for c in columns]
        cost_index = next(
            (i for i, name in enumerate(names) if name in {"PreTaxCost", "Cost", "CostUSD"}),
            0,
        )
        currency_index = next((i for i, name in enumerate(names) if name == "Currency"), None)
        cost = 0.0
        currency = ""
        if rows:
            try:
                cost = float(rows[0][cost_index] or 0)
            except (TypeError, ValueError, IndexError):
                cost = 0.0
            if currency_index is not None:
                try:
                    currency = str(rows[0][currency_index] or "")
                except IndexError:
                    currency = ""

        result = {
            "subscriptionId": subscription_id,
            "cost": cost,
            "currency": currency,
            "source": "azure_cost_management",
            "periodStart": start_date,
            "periodEnd": end_date,
            "error": "",
        }
        # Cache successful results
        _cost_cache[cache_key] = (time.time() + _CACHE_TTL_SECS, result)
        return result

    # ── Management Group bulk query ─────────────────────────────────────
    async def _query_via_management_group(
        self,
        tenant_id: str,
        subscription_ids: List[str],
        *,
        start_date: str,
        end_date: str,
    ) -> Optional[List[dict]]:
        """Try a single Cost Management query at the tenant root management
        group scope, grouped by SubscriptionId.  Returns None if the SPN
        lacks permissions at that scope (so the caller can fall back to
        per-subscription queries).
        """
        # Tenant root management group ID == tenant ID
        url = (
            f"{ARM_BASE}/providers/Microsoft.Management/managementGroups/{tenant_id}"
            f"/providers/Microsoft.CostManagement/query"
            f"?api-version={COST_API_VERSION}"
        )
        body = {
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {
                "from": _normalise_date(start_date),
                "to": _normalise_date(end_date, end=True),
            },
            "dataset": {
                "granularity": "None",
                "aggregation": {
                    "totalCost": {"name": "PreTaxCost", "function": "Sum"},
                },
                "grouping": [
                    {"type": "Dimension", "name": "SubscriptionId"},
                ],
                "filter": {
                    "dimensions": {
                        "name": "SubscriptionId",
                        "operator": "In",
                        "values": subscription_ids,
                    }
                },
            },
        }
        try:
            resp = await self._client.post(url, headers=self._headers(), json=body)
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            logger.info("cost.mg_query.error", error=str(exc))
            return None

        if resp.status_code in (401, 403):
            logger.info("cost.mg_query.no_access", status=resp.status_code)
            return None
        if resp.status_code == 429:
            logger.info("cost.mg_query.throttled")
            return None
        if resp.status_code != 200:
            logger.warning("cost.mg_query.failed", status=resp.status_code, body=resp.text[:300])
            return None

        # Parse grouped response
        payload = resp.json().get("properties", {})
        columns = payload.get("columns", [])
        rows = payload.get("rows", [])
        names = [c.get("name", "") for c in columns]
        cost_idx = next((i for i, n in enumerate(names) if n in {"PreTaxCost", "Cost", "CostUSD"}), 0)
        sub_idx = next((i for i, n in enumerate(names) if n == "SubscriptionId"), None)
        currency_idx = next((i for i, n in enumerate(names) if n == "Currency"), None)

        if sub_idx is None:
            logger.warning("cost.mg_query.no_sub_column", columns=names)
            return None

        # Build lookup of sub → cost from the response
        sub_costs: Dict[str, Tuple[float, str]] = {}
        for row in rows:
            try:
                sid = str(row[sub_idx]).strip().lower()
                c = float(row[cost_idx] or 0)
                cur = str(row[currency_idx] or "USD") if currency_idx is not None else "USD"
                sub_costs[sid] = (c, cur)
            except (IndexError, TypeError, ValueError):
                continue

        # Build result list in the same format as per-sub queries
        results = []
        now_ts = time.time()
        for sid in subscription_ids:
            cost_val, cur = sub_costs.get(sid.lower(), (0.0, "USD"))
            r = {
                "subscriptionId": sid,
                "cost": cost_val,
                "currency": cur,
                "source": "azure_cost_management",
                "periodStart": start_date,
                "periodEnd": end_date,
                "error": "",
            }
            # Populate cache so subsequent per-sub calls are free
            cache_key = f"{sid}|{start_date}|{end_date}"
            _cost_cache[cache_key] = (now_ts + _CACHE_TTL_SECS, r)
            results.append(r)

        logger.info("cost.mg_query.ok", subs=len(subscription_ids), rows=len(rows))
        return results

    async def query_subscription_costs(
        self,
        subscription_ids: List[str],
        *,
        start_date: str,
        end_date: str,
        tenant_id: str = "",
    ) -> List[dict]:
        """Return total actual costs for multiple subscriptions.

        Strategy (in order):
        1. Return cached results for any subs already in cache.
        2. Try a single Management Group query for all uncached subs (1 API call).
        3. Fall back to serial per-sub queries with delay + retry pass.
        """
        if not subscription_ids:
            return []

        # ── Step 1: serve from cache ────────────────────────────────────
        results: dict[str, dict] = {}
        uncached: List[str] = []
        now = time.time()
        for sid in subscription_ids:
            cache_key = f"{sid}|{start_date}|{end_date}"
            cached = _cost_cache.get(cache_key)
            if cached and cached[0] > now:
                results[sid] = cached[1]
            else:
                uncached.append(sid)

        if not uncached:
            logger.info("cost.all_cached", count=len(subscription_ids))
            return [results[sid] for sid in subscription_ids]

        # ── Step 2: try Management Group bulk query ─────────────────────
        if tenant_id and len(uncached) > 1:
            mg_results = await self._query_via_management_group(
                tenant_id, uncached, start_date=start_date, end_date=end_date,
            )
            if mg_results is not None:
                for r in mg_results:
                    results[r["subscriptionId"]] = r
                return [results[sid] for sid in subscription_ids]

        # ── Step 3: serial per-sub queries with delay ───────────────────
        for i, sid in enumerate(uncached):
            if i > 0:
                await asyncio.sleep(_INTER_QUERY_DELAY)
            results[sid] = await self.query_subscription_cost(
                sid, start_date=start_date, end_date=end_date,
            )

        # ── Step 4: retry any that failed ───────────────────────────────
        failed = [sid for sid, r in results.items() if r.get("error")]
        if failed:
            logger.info("cost.retry_pass", failed_count=len(failed), total=len(subscription_ids))
            await asyncio.sleep(5.0)
            for i, sid in enumerate(failed):
                if i > 0:
                    await asyncio.sleep(_INTER_QUERY_DELAY * 2)
                retry_result = await self.query_subscription_cost(
                    sid, start_date=start_date, end_date=end_date,
                )
                if not retry_result.get("error"):
                    results[sid] = retry_result

        return [results[sid] for sid in subscription_ids]

    async def list_accessible_subscriptions(self) -> List[dict]:
        """Return subscriptions the current SPN can read.

        Uses the ARM list endpoint, which returns every subscription where the
        SPN has at least Reader-equivalent access. Caller can then attempt cost
        queries on each (cost queries may still fail if Cost Management Reader
        is missing — that's reported per-sub in the cost response).
        """
        url = f"{ARM_BASE}/subscriptions?api-version=2022-12-01"
        try:
            resp = await self._client.get(url, headers=self._headers())
        except Exception as exc:
            logger.warning("subscriptions.list.error", error=str(exc))
            return []
        if resp.status_code != 200:
            logger.warning(
                "subscriptions.list.failed",
                status=resp.status_code,
                body=resp.text[:300],
            )
            return []
        items = (resp.json() or {}).get("value", []) or []
        out = []
        for item in items:
            sid = item.get("subscriptionId") or ""
            if not sid:
                continue
            out.append({
                "subscriptionId": sid,
                "displayName": item.get("displayName") or sid,
                "state": item.get("state") or "",
                "tenantId": item.get("tenantId") or "",
            })
        return out

"""Azure Cost Management helpers for subscription-level spend."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

import httpx

from ..auth import MsalTokenProvider
from ..logging_setup import get_logger
from .rbac_service import ARM_BASE, ARM_SCOPE

logger = get_logger(__name__)

COST_API_VERSION = "2023-11-01"


def _normalise_date(value: str, *, end: bool = False) -> str:
    value = (value or "").strip()
    if not value:
        return datetime.now(timezone.utc).isoformat()
    if "T" not in value:
        return f"{value}T{'23:59:59' if end else '00:00:00'}Z"
    if value.endswith("Z") or "+" in value:
        return value
    return f"{value}Z"


class CostManagementService:
    """Query Azure Cost Management for actual subscription costs.

    The service principal must have access to read cost data on each target
    subscription (for example, Cost Management Reader or equivalent billing
    permissions).
    """

    def __init__(self, token_provider: MsalTokenProvider) -> None:
        self._tp = token_provider
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
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
        resp = await self._client.post(url, headers=self._headers(), json=body)
        if resp.status_code != 200:
            logger.warning(
                "cost.query.failed",
                subscription=subscription_id,
                status=resp.status_code,
                body=resp.text[:300],
            )
            return {
                "subscriptionId": subscription_id,
                "cost": None,
                "currency": "",
                "source": "azure_cost_management",
                "periodStart": start_date,
                "periodEnd": end_date,
                "error": f"Cost query failed [{resp.status_code}]: {resp.text[:300]}",
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

        return {
            "subscriptionId": subscription_id,
            "cost": cost,
            "currency": currency,
            "source": "azure_cost_management",
            "periodStart": start_date,
            "periodEnd": end_date,
            "error": "",
        }

    async def query_subscription_costs(
        self,
        subscription_ids: List[str],
        *,
        start_date: str,
        end_date: str,
    ) -> List[dict]:
        """Return total actual costs for multiple subscriptions."""
        results = []
        for subscription_id in subscription_ids:
            results.append(await self.query_subscription_cost(
                subscription_id,
                start_date=start_date,
                end_date=end_date,
            ))
        return results

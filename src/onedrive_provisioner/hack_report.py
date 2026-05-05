"""Build hack provisioning summary and cost reports.

The report intentionally avoids password/TAP secrets. Cost inputs are either
user-entered estimates or values fetched from Azure Cost Management by callers.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, date, timezone
import re
from typing import Any, Dict, Iterable, List, Optional


_TEAM_RE = re.compile(r"(?:^|-)(t\d{2,})(?:-|$)", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_prefix(prefix: str) -> str:
    return (prefix or "").rstrip("-")


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip().replace("$", "").replace(",", "")
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _round_money(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 2)


def _normalise_license_costs(raw: Optional[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for name, value in (raw or {}).items():
        amount = _to_float(value)
        if name and amount is not None:
            out[str(name).strip()] = amount
    return out


def _normalise_subscription_costs(raw: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        raw_cost = item.get("cost") if "cost" in item else item.get("amount")
        sub_id = (
            item.get("subscriptionId")
            or item.get("subscription")
            or item.get("id")
            or ""
        )
        sub_id = str(sub_id).strip()
        if not sub_id:
            continue
        out.append({
            "subscriptionId": sub_id,
            "displayName": item.get("displayName") or item.get("name") or "",
            "team": (item.get("team") or item.get("teamId") or "").strip(),
            "cost": _to_float(raw_cost),
            "currency": item.get("currency") or "",
            "source": item.get("source") or "manual",
            "periodStart": item.get("periodStart") or item.get("startDate") or "",
            "periodEnd": item.get("periodEnd") or item.get("endDate") or "",
            "error": item.get("error") or "",
        })
    return out


def _infer_team(user: Dict[str, Any], prefix: str) -> str:
    groups = user.get("groups") or []
    for group in groups:
        match = _TEAM_RE.search(str(group))
        if match:
            return match.group(1).lower()

    upn = str(user.get("userPrincipalName") or "")
    local = upn.split("@", 1)[0]
    prefix = _clean_prefix(prefix)
    if prefix and local.startswith(prefix):
        local = local[len(prefix):].lstrip("-")
    match = _TEAM_RE.search(local)
    if match:
        return match.group(1).lower()
    return ""


def _summarise_users(users: List[Dict[str, Any]], prefix: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for user in users:
        out.append({
            "userPrincipalName": user.get("userPrincipalName", ""),
            "userId": user.get("userId", ""),
            "status": user.get("status", ""),
            "isAdmin": bool(user.get("isAdmin")),
            "team": _infer_team(user, prefix),
            "licenses": list(user.get("licenses") or []),
            "groups": list(user.get("groups") or []),
            "message": user.get("message", ""),
        })
    return out


def _compute_billing_months(start_date: str, end_date: str) -> int:
    """Return the number of billing months for a date range.

    If the period spans more than 30 days, each additional 30-day block
    adds another billing month.  Minimum is 1.
    """
    try:
        s = datetime.fromisoformat(start_date[:10]).date() if start_date else None
        e = datetime.fromisoformat(end_date[:10]).date() if end_date else None
    except (ValueError, TypeError):
        return 1
    if not s or not e or e <= s:
        return 1
    days = (e - s).days
    return max(1, math.ceil(days / 30))


def build_hack_report(
    state: Dict[str, Any],
    *,
    subscription_costs: Optional[Iterable[Dict[str, Any]]] = None,
    license_unit_costs: Optional[Dict[str, Any]] = None,
    currency: str = "USD",
    start_date: str = "",
    end_date: str = "",
    github_enabled: bool = False,
    github_copilot: bool = False,
    github_users: Optional[int] = None,
) -> Dict[str, Any]:
    """Return a report for a persisted hack state.

    ``subscription_costs`` entries may include ``subscriptionId``, ``cost``,
    optional ``team`` (for team-specific allocation), and optional metadata.
    ``license_unit_costs`` maps license/SKU name to unit monthly cost.
    ``github_enabled`` / ``github_copilot`` control GitHub seat cost inclusion.
    """
    prefix = state.get("prefix", "")
    currency = (currency or "USD").upper()
    users = _summarise_users(list(state.get("users") or []), prefix)
    participant_users = [u for u in users if not u["isAdmin"]]
    admin_users = [u for u in users if u["isAdmin"]]
    teams = sorted({u["team"] for u in participant_users if u["team"]})

    summary = {
        "totalUsers": len(users),
        "participantUsers": len(participant_users),
        "adminUsers": len(admin_users),
        "createdUsers": sum(1 for u in participant_users if u["status"] == "created"),
        "createdAdmins": sum(1 for u in admin_users if u["status"] == "created"),
        "created": sum(1 for u in users if u["status"] == "created"),
        "existing": sum(1 for u in users if u["status"] == "existing"),
        "failed": sum(1 for u in users if u["status"] == "failed"),
        "teams": len(teams),
        "groups": len(state.get("groups") or []),
        "groupsCreated": (state.get("summary") or {}).get("groupsCreated", 0),
    }

    license_cost_map = _normalise_license_costs(license_unit_costs)
    license_counts: Dict[str, int] = defaultdict(int)
    license_users: Dict[str, List[str]] = defaultdict(list)
    user_license_costs: Dict[str, float] = defaultdict(float)

    for user in users:
        upn = user["userPrincipalName"]
        for license_name in user["licenses"]:
            license_counts[license_name] += 1
            license_users[license_name].append(upn)
            if license_name in license_cost_map:
                user_license_costs[upn] += license_cost_map[license_name]

    license_entries = []
    unknown_license_costs = []
    total_license_cost = 0.0
    for license_name in sorted(license_counts):
        count = license_counts[license_name]
        unit_cost = license_cost_map.get(license_name)
        estimated = unit_cost * count if unit_cost is not None else None
        if estimated is None:
            unknown_license_costs.append(license_name)
        else:
            total_license_cost += estimated
        license_entries.append({
            "name": license_name,
            "assignedUsers": count,
            "unitCost": _round_money(unit_cost),
            "estimatedMonthlyCost": _round_money(estimated),
            "users": license_users[license_name],
        })

    user_subscription_costs: Dict[str, float] = defaultdict(float)
    sub_entries = []
    normalised_subs = _normalise_subscription_costs(subscription_costs)
    total_subscription_cost = 0.0
    known_subscription_costs = 0
    unknown_subscription_costs = 0

    for index, sub in enumerate(normalised_subs):
        team = (sub.get("team") or "").lower()
        if not team and teams and len(normalised_subs) == len(teams):
            team = teams[index]

        if team:
            target_users = [u for u in participant_users if u["team"] == team]
            allocation = "team"
        else:
            target_users = participant_users
            allocation = "all_participants"

        cost = sub.get("cost")
        known = cost is not None and not sub.get("error")
        cost_per_user = (cost / len(target_users)) if known and target_users else None
        if known:
            known_subscription_costs += 1
            total_subscription_cost += cost
            for user in target_users:
                user_subscription_costs[user["userPrincipalName"]] += cost_per_user or 0.0
        else:
            unknown_subscription_costs += 1

        sub_entries.append({
            "subscriptionId": sub["subscriptionId"],
            "displayName": sub.get("displayName") or "",
            "team": team,
            "allocation": allocation,
            "targetUsers": len(target_users),
            "cost": _round_money(cost),
            "costPerUser": _round_money(cost_per_user),
            "currency": (sub.get("currency") or currency).upper(),
            "source": sub.get("source") or "manual",
            "periodStart": sub.get("periodStart") or "",
            "periodEnd": sub.get("periodEnd") or "",
            "error": sub.get("error") or "",
        })

    user_rows = []
    for user in users:
        upn = user["userPrincipalName"]
        license_cost = user_license_costs.get(upn, 0.0)
        subscription_cost = user_subscription_costs.get(upn, 0.0)
        user_rows.append({
            **user,
            "licenseCost": _round_money(license_cost),
            "subscriptionCost": _round_money(subscription_cost),
            "totalEstimatedCost": _round_money(license_cost + subscription_cost),
        })

    team_rows = []
    for team in teams:
        team_users = [u for u in user_rows if u["team"] == team and not u["isAdmin"]]
        license_cost = sum(u["licenseCost"] or 0 for u in team_users)
        subscription_cost = sum(u["subscriptionCost"] or 0 for u in team_users)
        team_rows.append({
            "team": team,
            "users": len(team_users),
            "licenseCost": _round_money(license_cost),
            "subscriptionCost": _round_money(subscription_cost),
            "totalEstimatedCost": _round_money(license_cost + subscription_cost),
            "subscriptions": [s["subscriptionId"] for s in sub_entries if s.get("team") == team],
        })

    # ── GitHub seat cost ──────────────────────────────────────────────
    config = state.get("config") or {}
    gh_enabled = github_enabled or bool(config.get("enableGithub"))
    gh_copilot = github_copilot or bool(config.get("enableGithubCopilot"))
    gh_user_count = github_users if github_users is not None else (
        sum(1 for u in users if u.get("isAdmin") is False or not u.get("isAdmin"))
        if gh_enabled else 0
    )
    from onedrive_provisioner.license_prices import github_seat_cost
    gh_unit_cost = github_seat_cost(gh_copilot) if gh_enabled else 0.0
    gh_monthly_cost = gh_unit_cost * gh_user_count if gh_enabled else 0.0

    # ── Billing months ────────────────────────────────────────────────
    billing_months = _compute_billing_months(start_date, end_date)
    license_period_cost = total_license_cost * billing_months
    gh_period_cost = gh_monthly_cost * billing_months

    notes = []
    if unknown_license_costs:
        notes.append(
            "License cost is missing for: " + ", ".join(sorted(unknown_license_costs))
        )
    if unknown_subscription_costs:
        notes.append(
            f"{unknown_subscription_costs} subscription cost entr{'y is' if unknown_subscription_costs == 1 else 'ies are'} missing or failed."
        )
    if not sub_entries:
        notes.append("No subscription costs were supplied or fetched; subscription cost allocation is zero.")
    if billing_months > 1:
        notes.append(f"Hack spans {billing_months} billing months — license and GitHub costs are multiplied accordingly.")

    total_estimated = license_period_cost + total_subscription_cost + gh_period_cost

    return {
        "generatedAt": _now_iso(),
        "prefix": state.get("prefix", ""),
        "hackName": state.get("hackName", ""),
        "domain": state.get("domain", ""),
        "mode": state.get("mode", ""),
        "createdAt": state.get("createdAt", ""),
        "lastUpdated": state.get("lastUpdated", ""),
        "currency": currency,
        "summary": summary,
        "groups": list(state.get("groups") or []),
        "admins": admin_users,
        "licenses": {
            "items": license_entries,
            "uniqueCount": len(license_entries),
            "totalAssignments": sum(license_counts.values()),
            "estimatedMonthlyCost": _round_money(total_license_cost),
            "unknownCostLicenses": sorted(unknown_license_costs),
        },
        "subscriptions": {
            "items": sub_entries,
            "knownCostCount": known_subscription_costs,
            "unknownCostCount": unknown_subscription_costs,
            "estimatedPeriodCost": _round_money(total_subscription_cost),
        },
        "github": {
            "enabled": gh_enabled,
            "withCopilot": gh_copilot,
            "users": gh_user_count,
            "unitCostMonthly": _round_money(gh_unit_cost),
            "monthlyCost": _round_money(gh_monthly_cost),
            "periodCost": _round_money(gh_period_cost),
        },
        "billingMonths": billing_months,
        "teams": team_rows,
        "users": user_rows,
        "costs": {
            "licenseMonthly": _round_money(total_license_cost),
            "licensePeriod": _round_money(license_period_cost),
            "githubMonthly": _round_money(gh_monthly_cost),
            "githubPeriod": _round_money(gh_period_cost),
            "subscriptionPeriod": _round_money(total_subscription_cost),
            "totalEstimated": _round_money(total_estimated),
            "billingMonths": billing_months,
            "currency": currency,
        },
        "notes": notes,
    }

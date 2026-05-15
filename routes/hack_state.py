"""Hack state management routes (blob storage CRUD + user operations)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from flask import Blueprint, request, jsonify
import structlog

from onedrive_provisioner.graph import GraphClient

logger = structlog.get_logger(__name__)

from ._state import (
    extract_creds, get_state_manager, make_token_provider,
    require_confirmation, is_archived_state,
    audit_logger,
)

bp = Blueprint("hack_state", __name__)


# ────────────────────── State CRUD ──────────────────────

@bp.route("/api/hack-state", methods=["GET"])
def list_hacks():
    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured (set AZURE_STORAGE_CONNECTION_STRING)"}), 503
    try:
        return jsonify(mgr.list_hacks())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/hack-state/archive", methods=["GET"])
def list_archived_hacks():
    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured (set AZURE_STORAGE_CONNECTION_STRING)"}), 503
    try:
        return jsonify(mgr.list_archived_hacks())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/hack-state/<prefix>", methods=["GET"])
def get_hack_state(prefix):
    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404
    return jsonify(state)


@bp.route("/api/hack-state/<prefix>/versions", methods=["GET"])
def get_hack_versions(prefix):
    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    versions = mgr.list_versions(prefix)
    return jsonify(versions)


@bp.route("/api/hack-state/<prefix>/config", methods=["PATCH"])
def patch_hack_config(prefix):
    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    ALLOWED_KEYS = {"enableGithub", "enableGithubCopilot", "enableGithubGhas", "enableGithubForAdmins", "enablePowerApps", "enableSharePoint"}
    data = request.get_json(silent=True) or {}
    updates = {k: v for k, v in data.items() if k in ALLOWED_KEYS}
    if not updates:
        return jsonify({"error": f"No valid config keys. Allowed: {sorted(ALLOWED_KEYS)}"}), 400

    cfg = state.get("config") or {}
    cfg.update(updates)
    state["config"] = cfg
    mgr.save_state(prefix, state)
    audit_logger.log("config.patched", prefix, details={"updatedKeys": list(updates.keys())})
    return jsonify({"ok": True, "updatedKeys": list(updates.keys()), "config": cfg})


# ────────────────────── TAP regeneration ──────────────────────

async def _async_regenerate_tap(t, c, s, *, state, target_upns, tap_lifetime):
    from onedrive_provisioner.entra.tap_service import TapService
    tp = make_token_provider(t, c, s)
    results = []
    async with GraphClient(tp) as g:
        tap_svc = TapService(g, lifetime_minutes=tap_lifetime)
        for u in state.get("users", []):
            upn = u.get("userPrincipalName", "")
            uid = u.get("userId", "")
            if not uid:
                continue
            if target_upns and upn not in target_upns:
                continue
            tap = await tap_svc.issue(uid)
            results.append({
                "userPrincipalName": upn,
                "tap": tap.get("temporaryAccessPass", "") if tap else "",
                "tapExpires": tap.get("startDateTime", "") if tap else "",
                "status": "ok" if tap else "failed",
            })
    return results


@bp.route("/api/hack-state/<prefix>/regenerate-tap", methods=["POST"])
def regenerate_tap(prefix):
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404
    if is_archived_state(state):
        return jsonify({"error": "Archived hacks are report-only. Use the Report tab to generate historical reports."}), 409

    target_upns = data.get("users")
    tap_lifetime = int(data.get("tapLifetime", 120))
    target_count = sum(
        1 for user in state.get("users", [])
        if user.get("userId") and (not target_upns or user.get("userPrincipalName") in target_upns)
    )
    confirmation_needed = require_confirmation("regenerate_tap", {
        "prefix": state.get("prefix") or prefix,
        "resourceCount": target_count,
        "targetUserCount": target_count,
        "subscriptionCount": 0,
    }, data)
    if confirmation_needed:
        return confirmation_needed

    try:
        results = asyncio.run(_async_regenerate_tap(
            *creds, state=state, target_upns=target_upns,
            tap_lifetime=tap_lifetime))
        mgr.update_user_taps(prefix, results)
        return jsonify({"results": results, "updatedUsers": len(results)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── Password reset ──────────────────────

async def _async_reset_password(t, c, s, *, state, target_upns, custom_password):
    from onedrive_provisioner.entra.user_service import UserService
    tp = make_token_provider(t, c, s)
    results = []
    async with GraphClient(tp) as g:
        user_svc = UserService(g)
        for u in state.get("users", []):
            upn = u.get("userPrincipalName", "")
            uid = u.get("userId", "")
            if not uid:
                continue
            if target_upns and upn not in target_upns:
                continue
            new_pw = await user_svc.reset_password(uid, password=custom_password)
            results.append({
                "userPrincipalName": upn,
                "password": new_pw or "",
                "status": "ok" if new_pw else "failed",
            })
    return results


@bp.route("/api/hack-state/<prefix>/reset-password", methods=["POST"])
def reset_password(prefix):
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404
    if is_archived_state(state):
        return jsonify({"error": "Archived hacks are report-only."}), 409

    target_upns = data.get("users")
    custom_password = data.get("password")
    target_count = sum(
        1 for user in state.get("users", [])
        if user.get("userId") and (not target_upns or user.get("userPrincipalName") in target_upns)
    )
    confirmation_needed = require_confirmation("reset_password", {
        "prefix": state.get("prefix") or prefix,
        "resourceCount": target_count,
        "targetUserCount": target_count,
        "subscriptionCount": 0,
    }, data)
    if confirmation_needed:
        return confirmation_needed

    try:
        results = asyncio.run(_async_reset_password(
            *creds, state=state, target_upns=target_upns,
            custom_password=custom_password))
        mgr.update_user_passwords(prefix, results)
        return jsonify({"results": results, "updatedUsers": len(results)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── Group repair ──────────────────────

async def _async_repair_groups(t, c, s, *, state):
    from onedrive_provisioner.entra.group_service import GroupService
    tp = make_token_provider(t, c, s)
    results = []
    group_map = {}
    config = state.get("config", {})
    hack_prefix = config.get("prefix") or state.get("prefix", "")
    async with GraphClient(tp) as g:
        group_svc = GroupService(g, hack_name=state.get("hackName", ""))
        all_group_names = set()
        for u in state.get("users", []):
            all_group_names.update(u.get("groups", []))
            all_group_names.update(u.get("groupFailures", []))
        mode = config.get("mode", "team")
        teams = int(config.get("teams", 0))
        if mode == "team" and teams > 0:
            for t_idx in range(1, teams + 1):
                gn = f"{hack_prefix.rstrip('-')}-t{t_idx:02d}-group"
                all_group_names.add(gn)
        if int(config.get("adminUsers", 0)) > 0:
            all_group_names.add(f"{hack_prefix.rstrip('-')}-admins")

        for name in all_group_names:
            grp = await group_svc.get_by_name(name)
            if grp:
                group_map[name] = grp["id"]

        for u in state.get("users", []):
            uid = u.get("userId", "")
            upn = u.get("userPrincipalName", "")
            if not uid:
                continue
            is_admin = u.get("isAdmin", False)
            expected_groups = set(u.get("groups", [])) | set(u.get("groupFailures", []))

            if not is_admin and mode == "team":
                local = upn.split("@")[0] if "@" in upn else upn
                parts = local.replace(hack_prefix.rstrip("-") + "-", "").split("-")
                if parts and parts[0].startswith("t"):
                    team_grp = f"{hack_prefix.rstrip('-')}-{parts[0]}-group"
                    expected_groups.add(team_grp)
            elif is_admin:
                admin_grp = f"{hack_prefix.rstrip('-')}-admins"
                expected_groups.add(admin_grp)

            current_groups = []
            still_failed = []
            any_repaired = False

            for grp_name in sorted(expected_groups):
                gid = group_map.get(grp_name)
                if not gid:
                    still_failed.append(grp_name)
                    continue
                is_member = await group_svc.verify_member(gid, uid)
                if is_member:
                    current_groups.append(grp_name)
                else:
                    added = await group_svc.add_member(gid, uid)
                    if added:
                        current_groups.append(grp_name)
                        any_repaired = True
                    else:
                        still_failed.append(grp_name)

            results.append({
                "userPrincipalName": upn,
                "groups": current_groups,
                "groupFailures": still_failed,
                "repaired": any_repaired,
                "status": "repaired" if any_repaired else ("ok" if not still_failed else "failed"),
            })
    return results


@bp.route("/api/hack-state/<prefix>/repair-groups", methods=["POST"])
def repair_groups(prefix):
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404
    if is_archived_state(state):
        return jsonify({"error": "Archived hacks are report-only."}), 409

    try:
        results = asyncio.run(_async_repair_groups(*creds, state=state))
        mgr.update_user_groups(prefix, results)
        repaired = sum(1 for r in results if r.get("repaired"))
        return jsonify({"results": results, "repairedUsers": repaired, "totalChecked": len(results)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── License repair ──────────────────────

async def _async_repair_licenses(t, c, s, *, state):
    from onedrive_provisioner.entra.license_service import LicenseService
    tp = make_token_provider(t, c, s)
    results = []
    config = state.get("config", {})
    expected_licenses = config.get("licenses", [])
    assign_to_admins = config.get("assignLicensesToAdmins", False)

    async with GraphClient(tp) as g:
        lic_svc = LicenseService(g)
        sku_map = await lic_svc.resolve(expected_licenses)
        expected_sku_ids = [sid for (sid, _) in sku_map.values()]
        sku_to_friendly = {}
        for friendly, (sid, _) in sku_map.items():
            sku_to_friendly[sid] = friendly

        if not expected_sku_ids:
            return results

        for u in state.get("users", []):
            uid = u.get("userId", "")
            upn = u.get("userPrincipalName", "")
            is_admin = u.get("isAdmin", False)
            if not uid:
                continue
            if is_admin and not assign_to_admins:
                continue

            vr = await lic_svc.verify_and_repair(uid, expected_sku_ids)
            if vr["repaired"]:
                status = "repaired"
            elif vr["still_missing"]:
                status = "failed"
            else:
                status = "ok"

            all_assigned_sids = set(vr["assigned"] + vr["repaired"])
            license_names = [sku_to_friendly[sid] for sid in all_assigned_sids if sid in sku_to_friendly]

            results.append({
                "userPrincipalName": upn,
                "licenses": license_names,
                "assigned": len(vr["assigned"]),
                "repaired": len(vr["repaired"]),
                "stillMissing": len(vr["still_missing"]),
                "status": status,
            })
    return results


@bp.route("/api/hack-state/<prefix>/repair-licenses", methods=["POST"])
def repair_licenses(prefix):
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404
    if is_archived_state(state):
        return jsonify({"error": "Archived hacks are report-only."}), 409

    try:
        results = asyncio.run(_async_repair_licenses(*creds, state=state))
        mgr.update_user_licenses(prefix, results)
        repaired = sum(1 for r in results if r.get("status") == "repaired")
        return jsonify({"results": results, "repairedUsers": repaired, "totalChecked": len(results)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── License assignment ──────────────────────

async def _async_assign_licenses(t, c, s, *, state, licenses, target_upns, include_admins: bool = False):
    from onedrive_provisioner.entra.license_service import LicenseService
    tp = make_token_provider(t, c, s)
    results = []
    async with GraphClient(tp) as g:
        lic_svc = LicenseService(g)
        sku_map = await lic_svc.resolve(licenses)
        sku_ids = [sid for (sid, _) in sku_map.values()]
        if not sku_ids:
            return results
        for u in state.get("users", []):
            upn = u.get("userPrincipalName", "")
            uid = u.get("userId", "")
            if not uid or (u.get("isAdmin") and not include_admins):
                continue
            if target_upns and upn not in target_upns:
                continue
            assigned = await lic_svc.assign(uid, sku_ids)
            existing = u.get("licenses", [])
            merged = list(set(existing + licenses))
            results.append({
                "userPrincipalName": upn,
                "licenses": merged,
                "status": "ok" if assigned else "failed",
            })
    return results


@bp.route("/api/hack-state/<prefix>/assign-licenses", methods=["POST"])
def assign_licenses_to_hack(prefix):
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404
    if is_archived_state(state):
        return jsonify({"error": "Archived hacks are report-only. Use the Report tab to generate historical reports."}), 409

    licenses = data.get("licenses", [])
    if not licenses:
        return jsonify({"error": "licenses[] required"}), 400
    target_upns = data.get("users")
    include_admins = bool(data.get("includeAdmins", False))
    target_count = sum(
        1 for user in state.get("users", [])
        if user.get("userId")
        and (include_admins or not user.get("isAdmin"))
        and (not target_upns or user.get("userPrincipalName") in target_upns)
    )
    confirmation_needed = require_confirmation("assign_licenses", {
        "prefix": state.get("prefix") or prefix,
        "resourceCount": target_count,
        "targetUserCount": target_count,
        "licenseCount": len(licenses),
        "subscriptionCount": 0,
    }, data)
    if confirmation_needed:
        return confirmation_needed

    try:
        results = asyncio.run(_async_assign_licenses(
            *creds, state=state, licenses=licenses, target_upns=target_upns,
            include_admins=include_admins))
        mgr.update_user_licenses(prefix, results)
        return jsonify({"results": results, "updatedUsers": len(results)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── Hack report ──────────────────────

async def _async_fetch_subscription_costs(t, c, s, *, subscription_ids, start_date, end_date):
    from onedrive_provisioner.entra.cost_service import CostManagementService
    tp = make_token_provider(t, c, s)
    async with CostManagementService(tp) as cost_svc:
        return await cost_svc.query_subscription_costs(
            subscription_ids,
            start_date=start_date,
            end_date=end_date,
        )


async def _async_list_accessible_subscriptions(t, c, s):
    """List subscriptions the SPN can read (one ARM call)."""
    from onedrive_provisioner.entra.cost_service import CostManagementService
    tp = make_token_provider(t, c, s)
    async with CostManagementService(tp) as cost_svc:
        return await cost_svc.list_accessible_subscriptions()


async def _async_resolve_group_ids_by_prefix(t, c, s, *, prefix):
    """Look up Entra group IDs for a hack prefix via Graph.

    Used when state.groups contains only display-names (no IDs) — typical for
    hacks created before group ID persistence or absorbed via drift cleanup.
    Returns ``[{id, displayName}, ...]``.
    """
    from onedrive_provisioner.entra.discovery_service import DiscoveryService
    tp = make_token_provider(t, c, s)
    async with GraphClient(tp) as g:
        disco = DiscoveryService(g)
        groups = await disco.find_groups_by_prefix(prefix)
        return [{"id": gr.get("id"), "displayName": gr.get("displayName")}
                for gr in groups if gr.get("id")]


async def _async_resolve_subs_from_groups(t, c, s, *, group_ids):
    """Resolve subscriptions a list of Entra groups have ANY role on.

    Uses ARM root-scope role assignment listing (one call per group, parallel).
    Returns ``(sorted_unique_sub_ids, group_to_subs_map)`` where
    ``group_to_subs_map`` is ``{groupId: [subId, ...]}``.
    """
    import asyncio as _asyncio
    from onedrive_provisioner.entra.rbac_service import RbacService, subscription_from_assignment
    if not group_ids:
        return [], {}
    tp = make_token_provider(t, c, s)
    async with RbacService(tp) as rbac:
        sem = _asyncio.Semaphore(8)

        async def _one(gid: str):
            async with sem:
                return gid, await rbac.list_all_assignments_for_principal(gid)

        results = await _asyncio.gather(*[_one(g) for g in group_ids])
    group_to_subs: dict[str, list[str]] = {}
    union: set[str] = set()
    for gid, assignments in results:
        sids = []
        for a in assignments or []:
            sid = subscription_from_assignment(a)
            if sid:
                sids.append(sid)
                union.add(sid)
        # de-dupe per group
        group_to_subs[gid] = sorted(set(sids))
    return sorted(union), group_to_subs


async def _async_resolve_subs_from_users(t, c, s, *, user_ids, max_users=200):
    """Fallback: resolve subscriptions a list of users have ANY role on.

    Used when group-RBAC discovery returns 0 subs (groups assigned at
    management-group scope, or no Azure RBAC at all). Same root-scope ARM
    query per user, parallel with semaphore=8. Capped at ``max_users`` to
    avoid pounding ARM for huge hacks; first N users are typically enough
    since hack users share the same subs.
    Returns ``(sorted_unique_sub_ids, user_to_subs_map)``.
    """
    import asyncio as _asyncio
    from onedrive_provisioner.entra.rbac_service import RbacService, subscription_from_assignment
    if not user_ids:
        return [], {}
    user_ids = list(user_ids)[:max_users]
    tp = make_token_provider(t, c, s)
    async with RbacService(tp) as rbac:
        sem = _asyncio.Semaphore(8)

        async def _one(uid: str):
            async with sem:
                return uid, await rbac.list_all_assignments_for_principal(uid)

        results = await _asyncio.gather(*[_one(u) for u in user_ids])
    user_to_subs: dict[str, list[str]] = {}
    union: set[str] = set()
    for uid, assignments in results:
        sids = []
        for a in assignments or []:
            sid = subscription_from_assignment(a)
            if sid:
                sids.append(sid)
                union.add(sid)
        user_to_subs[uid] = sorted(set(sids))
    return sorted(union), user_to_subs


async def _async_discover_and_fetch_subscription_costs(t, c, s, *, start_date, end_date, only_ids=None):
    """List SPN-accessible subs, then fetch cost for each.

    If ``only_ids`` is provided, restrict the discovery to that allow-list
    (still using discovery to enrich displayName / state).
    Returns a tuple of (cost_results, accessible_subs_metadata).
    """
    from onedrive_provisioner.entra.cost_service import CostManagementService
    tp = make_token_provider(t, c, s)
    async with CostManagementService(tp) as cost_svc:
        accessible = await cost_svc.list_accessible_subscriptions()
        if only_ids:
            allow = {str(x).strip() for x in only_ids if str(x or "").strip()}
            accessible = [s_ for s_ in accessible if s_["subscriptionId"] in allow]
        ids = [s_["subscriptionId"] for s_ in accessible]
        results = await cost_svc.query_subscription_costs(
            ids, start_date=start_date, end_date=end_date,
        )
        # Enrich each result with the displayName from discovery.
        meta_by_id = {s_["subscriptionId"]: s_ for s_ in accessible}
        for r in results:
            meta = meta_by_id.get(r.get("subscriptionId") or "")
            if meta:
                r["displayName"] = meta.get("displayName") or r.get("subscriptionId")
        return results, accessible


def _merge_subscription_cost_inputs(manual_costs, fetched_costs):
    merged: Dict[str, dict] = {}
    for item in manual_costs or []:
        if not isinstance(item, dict):
            continue
        sub_id = (item.get("subscriptionId") or item.get("subscription") or item.get("id") or "").strip()
        if sub_id:
            merged[sub_id] = dict(item)
            merged[sub_id]["subscriptionId"] = sub_id
    for item in fetched_costs or []:
        sub_id = item.get("subscriptionId", "")
        if not sub_id:
            continue
        existing = merged.get(sub_id, {"subscriptionId": sub_id})
        existing.update({
            "cost": item.get("cost"),
            "currency": item.get("currency") or existing.get("currency", ""),
            "source": item.get("source", "azure_cost_management"),
            "periodStart": item.get("periodStart", ""),
            "periodEnd": item.get("periodEnd", ""),
            "error": item.get("error", ""),
        })
        if item.get("displayName"):
            existing["displayName"] = item["displayName"]
        if item.get("team") and not existing.get("team"):
            existing["team"] = item["team"]
        merged[sub_id] = existing
    return list(merged.values())


def _state_report_date_range(state: dict) -> tuple[str, str]:
    start_date = (
        state.get("hackStartDate")
        or state.get("hackDate")
        or state.get("createdAt")
        or ""
    )[:10]
    end_date = (
        state.get("deleteDate")
        or state.get("endDate")
        or state.get("readonlyDate")
        or state.get("archivedAt")
        or datetime.now(timezone.utc).date().isoformat()
    )[:10]
    if not start_date:
        start_date = datetime.now(timezone.utc).date().isoformat()
    return start_date, end_date


@bp.route("/api/hack-state/<prefix>/report", methods=["POST"])
def build_hack_report_api(prefix):
    try:
        return _build_hack_report_api_impl(prefix)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Report generation failed: {exc}"}), 500


def _build_hack_report_api_impl(prefix):
    from onedrive_provisioner.hack_report import build_hack_report

    data = request.get_json(silent=True) or {}
    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    subscription_costs = data.get("subscriptionCosts") or []
    fetched_costs = []
    discovered_subs = []
    auto_discovered = False
    sub_source = "explicit"  # explicit | hack_state | group_rbac | user_rbac | auto_discover
    cache_stats = {"hits": 0, "fetched": 0, "ttlSeconds": 0}
    group_sub_map: dict[str, list[str]] = {}
    user_sub_map: dict[str, list[str]] = {}
    if data.get("fetchSubscriptionCosts"):
        creds = extract_creds(data)
        if not creds:
            return jsonify({"error": "SPN credentials required to fetch Azure costs"}), 400
        # Resolve subscription IDs in priority order:
        #   1. Explicit list from request (frontend textarea / API caller)
        #   2. subscriptionCosts entries from the request
        #   3. Hack state's recorded subs (state.subscriptionIds / config.subscriptionIds)
        #      → these are the subs THIS hack targets, never unrelated tenant subs
        #   4. Auto-discover everything the SPN can read (last resort, with note)
        subscription_ids = data.get("subscriptionIds") or [
            sc.get("subscriptionId") or sc.get("subscription") or sc.get("id")
            for sc in subscription_costs if isinstance(sc, dict)
        ]
        subscription_ids = [str(s).strip() for s in subscription_ids if str(s or "").strip()]
        if not subscription_ids:
            hack_subs = (
                state.get("subscriptionIds")
                or (state.get("config") or {}).get("subscriptionIds")
                or []
            )
            subscription_ids = [str(s).strip() for s in hack_subs if str(s or "").strip()]
            if subscription_ids:
                sub_source = "hack_state"

        # ── Group-RBAC discovery: if still no subs, look up which subscriptions
        # the hack's groups have role assignments on. This is much narrower
        # than auto-discovering all SPN-accessible subs.
        logger.warning(
            "report.sub_resolution.checkpoint",
            prefix=prefix,
            sub_source=sub_source,
            sub_count_before_group_rbac=len(subscription_ids),
            state_groups_type=type(state.get("groups")).__name__,
            state_groups_len=len(state.get("groups") or []),
        )
        if not subscription_ids:
            hack_groups = state.get("groups") or []
            # state.groups can be a mixed list of {"id":..., ...} dicts (from
            # discovery) or bare display-name strings (from drift absorption).
            # Pull IDs from dicts; if any string entries exist OR no IDs were
            # found, do a Graph prefix lookup to enrich.
            group_ids = [
                g.get("id") for g in hack_groups
                if isinstance(g, dict) and g.get("id")
            ]
            has_string_entries = any(isinstance(g, str) for g in hack_groups)
            if (not group_ids) or has_string_entries:
                try:
                    enriched = asyncio.run(
                        _async_resolve_group_ids_by_prefix(*creds, prefix=prefix)
                    )
                    found = [g["id"] for g in enriched if g.get("id")]
                    # union with whatever we already had
                    group_ids = sorted(set(group_ids) | set(found))
                    logger.warning(
                        "report.group_rbac.id_lookup",
                        prefix=prefix,
                        from_state=len([g for g in hack_groups if isinstance(g, dict) and g.get("id")]),
                        from_graph=len(found),
                    )
                except Exception as exc:
                    logger.warning(
                        "report.group_rbac.id_lookup_failed",
                        prefix=prefix, error=str(exc),
                    )
            group_ids = [str(g).strip() for g in group_ids if g and str(g).strip()]
            if group_ids:
                resolved_ids, group_sub_map = asyncio.run(
                    _async_resolve_subs_from_groups(*creds, group_ids=group_ids)
                )
                logger.warning(
                    "report.group_rbac.resolved",
                    prefix=prefix,
                    group_count=len(group_ids),
                    sub_count=len(resolved_ids),
                    per_group={gid: len(v) for gid, v in (group_sub_map or {}).items()},
                )
                if resolved_ids:
                    subscription_ids = resolved_ids
                    sub_source = "group_rbac"

        # ── User-RBAC fallback: if groups had no subscription-scope role
        # assignments (common when roles are assigned at MG scope, or only at
        # user level), look up each hack user's role assignments and union the
        # subs they have access to.
        if not subscription_ids:
            hack_users = state.get("users") or []
            user_ids = [
                u.get("userId") or u.get("id")
                for u in hack_users if isinstance(u, dict)
            ]
            user_ids = [str(u).strip() for u in user_ids if u and str(u).strip()]
            if user_ids:
                resolved_ids, user_sub_map = asyncio.run(
                    _async_resolve_subs_from_users(*creds, user_ids=user_ids)
                )
                logger.warning(
                    "report.user_rbac.resolved",
                    prefix=prefix,
                    user_count=len(user_ids),
                    queried=min(len(user_ids), 200),
                    sub_count=len(resolved_ids),
                    users_with_subs=sum(1 for v in (user_sub_map or {}).values() if v),
                )
                if resolved_ids:
                    subscription_ids = resolved_ids
                    sub_source = "user_rbac"
        default_start_date, default_end_date = _state_report_date_range(state)
        start_date = data.get("startDate") or default_start_date
        end_date = data.get("endDate") or default_end_date

        # ── Cost cache: {subId|start|end → row, fetchedAt} in state["costCache"]
        force_refresh = bool(data.get("forceRefresh"))
        ttl_seconds = int(data.get("cacheTtlSeconds") or 900)  # 15 min default
        cache_stats["ttlSeconds"] = ttl_seconds
        cost_cache = state.get("costCache") or {}
        now_ts = datetime.now(timezone.utc).timestamp()

        def _cache_key(sid: str) -> str:
            return f"{sid}|{start_date}|{end_date}"

        def _cache_get(sid: str):
            if force_refresh:
                return None
            entry = cost_cache.get(_cache_key(sid))
            if not entry:
                return None
            try:
                fetched_at = datetime.fromisoformat(entry.get("fetchedAt", "").replace("Z", "+00:00")).timestamp()
            except Exception:
                return None
            if (now_ts - fetched_at) > ttl_seconds:
                return None
            return entry.get("row")

        def _cache_put(rows: list[dict]) -> None:
            for row in rows:
                sid = row.get("subscriptionId") or ""
                if not sid or row.get("error"):  # don't cache errors
                    continue
                cost_cache[_cache_key(sid)] = {
                    "row": row,
                    "fetchedAt": datetime.now(timezone.utc).isoformat(),
                }

        if subscription_ids:
            # Explicit IDs path: skip ARM discovery entirely (saves a round-trip).
            # Use cache for hits, fetch only the misses.
            cached_rows = []
            misses = []
            for sid in subscription_ids:
                hit = _cache_get(sid)
                if hit is not None:
                    cached_rows.append(hit)
                else:
                    misses.append(sid)
            cache_stats["hits"] = len(cached_rows)
            cache_stats["fetched"] = len(misses)
            new_rows = []
            if misses:
                new_rows = asyncio.run(_async_fetch_subscription_costs(
                    *creds,
                    subscription_ids=misses,
                    start_date=start_date,
                    end_date=end_date,
                ))
                _cache_put(new_rows)
            fetched_costs = cached_rows + new_rows
        else:
            # No subs in request, no subs in hack state — fall back to auto-discover.
            # Caller can opt-out (e.g. for "audit all subs" view) by setting
            # discoverAllAccessible=true (used as the explicit gate).
            sub_source = "auto_discover"
            discovered_subs = asyncio.run(_async_list_accessible_subscriptions(*creds))
            auto_discovered = True
            disco_ids = [s_["subscriptionId"] for s_ in discovered_subs]
            meta_by_id = {s_["subscriptionId"]: s_ for s_ in discovered_subs}
            cached_rows = []
            misses = []
            for sid in disco_ids:
                hit = _cache_get(sid)
                if hit is not None:
                    cached_rows.append(hit)
                else:
                    misses.append(sid)
            cache_stats["hits"] = len(cached_rows)
            cache_stats["fetched"] = len(misses)
            new_rows = []
            if misses:
                new_rows = asyncio.run(_async_fetch_subscription_costs(
                    *creds,
                    subscription_ids=misses,
                    start_date=start_date,
                    end_date=end_date,
                ))
                # enrich displayName
                for r in new_rows:
                    meta = meta_by_id.get(r.get("subscriptionId") or "")
                    if meta:
                        r["displayName"] = meta.get("displayName") or r.get("subscriptionId")
                _cache_put(new_rows)
            fetched_costs = cached_rows + new_rows

        # Persist updated cache to state.
        state["costCache"] = cost_cache

        # ── Enrich subscription displayNames. Cost API returns only the GUID;
        # to get the human-friendly name we need ARM `/subscriptions`. We
        # cache the result on state.subscriptionNameMap so subsequent reports
        # don't re-call ARM unless a new sub appears.
        try:
            sub_name_map: dict[str, str] = dict(state.get("subscriptionNameMap") or {})
            unnamed = [
                r.get("subscriptionId") for r in fetched_costs
                if r.get("subscriptionId")
                and (not r.get("displayName") or r.get("displayName") == r.get("subscriptionId"))
                and r.get("subscriptionId") not in sub_name_map
            ]
            if unnamed:
                try:
                    accessible = asyncio.run(_async_list_accessible_subscriptions(*creds))
                    for s_ in accessible:
                        sid = s_.get("subscriptionId")
                        dn = s_.get("displayName")
                        if sid and dn:
                            sub_name_map[sid] = dn
                except Exception as _e:
                    logger.warning("report.sub_name_enrich.failed", error=str(_e))
            # Apply to fetched_costs
            for r in fetched_costs:
                sid = r.get("subscriptionId")
                if sid and sub_name_map.get(sid):
                    if not r.get("displayName") or r.get("displayName") == sid:
                        r["displayName"] = sub_name_map[sid]
            if sub_name_map:
                state["subscriptionNameMap"] = sub_name_map
        except Exception as _e:
            logger.warning("report.sub_name_enrich.outer_failed", error=str(_e))

        # If we resolved subs via group-RBAC or user-RBAC, save them on the
        # hack state so subsequent reports skip the discovery round-trip and
        # go straight to the "hack_state" path.
        if sub_source in ("group_rbac", "user_rbac") and subscription_ids:
            state["subscriptionIds"] = subscription_ids
            state["subscriptionsDiscoveredAt"] = datetime.now(timezone.utc).isoformat()
            state["subscriptionsDiscoveredVia"] = sub_source
            if group_sub_map:
                state["subscriptionsByGroup"] = group_sub_map
            if user_sub_map:
                state["subscriptionsByUser"] = user_sub_map

        # ── Compute / update the sub→team map. Runs every report so the
        # explicit/hack_state paths (subsequent runs after the maps were
        # persisted) still get team allocation correctly. Sources of truth:
        #   1. Live group_sub_map (this run, if group_rbac fired)
        #   2. Live user_sub_map  (this run, if user_rbac fired)
        #   3. Persisted state.subscriptionsByGroup / subscriptionsByUser
        #   4. Existing state.subscriptionTeamMap (cumulative cache)
        #   5. On-demand discovery: if any fetched sub has no team yet, run
        #      group+user RBAC lookup to fill in the gaps (one-time cost,
        #      result is cached in state for next time).
        try:
            from onedrive_provisioner.hack_report import _infer_team, _TEAM_RE
            sub_to_team: dict[str, str] = dict(state.get("subscriptionTeamMap") or {})
            persisted_group_map = state.get("subscriptionsByGroup") or {}
            persisted_user_map = state.get("subscriptionsByUser") or {}
            effective_group_map = group_sub_map or persisted_group_map
            effective_user_map = user_sub_map or persisted_user_map

            # If any fetched sub still has no team mapping AND we have no
            # persisted maps, do an on-demand RBAC discovery so the team
            # column is populated correctly even for the explicit/hack_state
            # paths.
            untagged_subs = [
                r.get("subscriptionId") for r in fetched_costs
                if r.get("subscriptionId") and r["subscriptionId"] not in sub_to_team
            ]
            if untagged_subs and not effective_group_map and not effective_user_map:
                logger.warning(
                    "report.team_map.on_demand_discovery",
                    prefix=prefix, untagged_count=len(untagged_subs),
                )
                # Resolve group IDs by prefix (cheap: 1 Graph call)
                try:
                    enriched_groups = asyncio.run(
                        _async_resolve_group_ids_by_prefix(*creds, prefix=prefix)
                    )
                    discovered_group_ids = [g["id"] for g in enriched_groups if g.get("id")]
                except Exception:
                    discovered_group_ids = []
                if discovered_group_ids:
                    try:
                        _, effective_group_map = asyncio.run(
                            _async_resolve_subs_from_groups(*creds, group_ids=discovered_group_ids)
                        )
                    except Exception:
                        effective_group_map = {}
                # Always also try user-RBAC for subs not covered by groups
                user_ids_for_disco = [
                    (u.get("userId") or u.get("id"))
                    for u in (state.get("users") or []) if isinstance(u, dict)
                ]
                user_ids_for_disco = [str(u).strip() for u in user_ids_for_disco if u]
                if user_ids_for_disco:
                    try:
                        _, effective_user_map = asyncio.run(
                            _async_resolve_subs_from_users(*creds, user_ids=user_ids_for_disco)
                        )
                    except Exception:
                        effective_user_map = {}
                # Persist for future runs
                if effective_group_map:
                    state["subscriptionsByGroup"] = effective_group_map
                if effective_user_map:
                    state["subscriptionsByUser"] = effective_user_map

            # Group-derived team mapping (need group display names)
            if effective_group_map:
                gid_to_name: dict[str, str] = {}
                for g in (state.get("groups") or []):
                    if isinstance(g, dict) and g.get("id") and g.get("displayName"):
                        gid_to_name[g["id"]] = g["displayName"]
                missing = [gid for gid in effective_group_map if gid not in gid_to_name]
                if missing:
                    try:
                        enriched = asyncio.run(
                            _async_resolve_group_ids_by_prefix(*creds, prefix=prefix)
                        )
                        for ge in enriched:
                            gid_to_name.setdefault(ge["id"], ge.get("displayName") or "")
                    except Exception:
                        pass
                for gid, sids in effective_group_map.items():
                    name = gid_to_name.get(gid, "")
                    m = _TEAM_RE.search(name) if name else None
                    if not m:
                        continue
                    team = m.group(1).lower()
                    for sid in sids:
                        sub_to_team.setdefault(sid, team)

            # User-derived team mapping (fallback for subs not covered above)
            if effective_user_map:
                uid_to_user = {
                    (u.get("userId") or u.get("id")): u
                    for u in (state.get("users") or []) if isinstance(u, dict)
                }
                for uid, sids in effective_user_map.items():
                    u = uid_to_user.get(uid)
                    if not u:
                        continue
                    team = _infer_team(u, prefix)
                    if not team:
                        continue
                    for sid in sids:
                        sub_to_team.setdefault(sid, team)

            if sub_to_team:
                # Persist for next time so we don't recompute names on every run
                state["subscriptionTeamMap"] = sub_to_team
                for r in fetched_costs:
                    sid = r.get("subscriptionId") or ""
                    if sid in sub_to_team and not r.get("team"):
                        r["team"] = sub_to_team[sid]
                logger.warning(
                    "report.sub_team_mapping",
                    prefix=prefix,
                    sub_source=sub_source,
                    map_size=len(sub_to_team),
                    teams_assigned=sum(1 for r in fetched_costs if r.get("team")),
                    total_subs=len(fetched_costs),
                )
        except Exception as exc:
            logger.warning("report.sub_team_mapping.failed",
                           prefix=prefix, error=str(exc))

        subscription_costs = _merge_subscription_cost_inputs(subscription_costs, fetched_costs)

    # Auto-fill missing license costs from NCE price list.
    from onedrive_provisioner.license_prices import resolve_license_prices
    user_costs = data.get("licenseUnitCosts") or {}
    all_skus = list({lic for u in (state.get("users") or []) for lic in (u.get("licenses") or [])})
    nce_prices = resolve_license_prices(all_skus)
    # NCE prices as defaults; user-supplied values take precedence.
    merged_costs = {**nce_prices, **{k: v for k, v in user_costs.items() if v is not None and v != ""}}

    default_start_date, default_end_date = _state_report_date_range(state)
    start_date = data.get("startDate") or default_start_date
    end_date = data.get("endDate") or default_end_date

    config = state.get("config") or {}
    budget = data.get("budget")
    try:
        budget = float(budget) if budget not in (None, "", 0) else None
    except (TypeError, ValueError):
        budget = None

    report = build_hack_report(
        state,
        subscription_costs=subscription_costs,
        license_unit_costs=merged_costs,
        currency=data.get("currency") or "USD",
        start_date=start_date,
        end_date=end_date,
        github_enabled=data.get("githubEnabled", config.get("enableGithub", False)),
        github_copilot=data.get("githubCopilot", config.get("enableGithubCopilot", False)),
        budget=budget,
    )
    if fetched_costs:
        report["costFetch"] = {
            "subscriptionsQueried": len(fetched_costs),
            "autoDiscovered": auto_discovered,
            "subSource": sub_source,
            "accessibleSubscriptions": discovered_subs,
            "groupSubMap": group_sub_map if sub_source == "group_rbac" else {},
            "userSubMap": user_sub_map if sub_source == "user_rbac" else {},
            "cacheHits": cache_stats["hits"],
            "cacheFetched": cache_stats["fetched"],
            "cacheTtlSeconds": cache_stats["ttlSeconds"],
        }
    elif auto_discovered:
        report["costFetch"] = {
            "subscriptionsQueried": 0,
            "autoDiscovered": True,
            "subSource": sub_source,
            "accessibleSubscriptions": [],
            "cacheHits": cache_stats["hits"],
            "cacheFetched": cache_stats["fetched"],
            "cacheTtlSeconds": cache_stats["ttlSeconds"],
        }
        report.setdefault("notes", []).append(
            "Auto-discovery found no subscriptions accessible to this SPN. "
            "Grant Reader (or Cost Management Reader) on the target subscriptions, "
            "or paste subscription IDs manually under Advanced cost inputs."
        )

    # Persist cost inputs to state so they survive across report generations
    if data.get("persistInputs", True):
        try:
            state["reportInputs"] = {
                "currency": data.get("currency") or "USD",
                "startDate": start_date,
                "endDate": end_date,
                "subscriptionCosts": data.get("subscriptionCosts") or [],
                "licenseUnitCosts": user_costs,
                "budget": budget,
                "fetchSubscriptionCosts": bool(data.get("fetchSubscriptionCosts")),
                "savedAt": datetime.now(timezone.utc).isoformat(),
            }
            # Save a lightweight cost summary for the dashboard
            rc = report.get("costs") or {}
            rb = report.get("budget") or {}
            state["lastReportCosts"] = {
                "totalEstimated": rc.get("totalEstimated", 0),
                "costPerUser": rc.get("costPerUser", 0),
                "costPerDay": rc.get("costPerDay", 0),
                "currency": rc.get("currency") or data.get("currency") or "USD",
                "subscriptionPeriod": rc.get("subscriptionPeriod", 0),
                "licensePeriod": rc.get("licensePeriod", 0),
                "githubPeriod": rc.get("githubPeriod", 0),
                "budgetUsedPercent": rb.get("usedPercent"),
                "budgetStatus": rb.get("status"),
                "generatedAt": report.get("generatedAt", ""),
            }
            mgr.save_state(prefix, state, version=False)
        except Exception as exc:
            report.setdefault("notes", []).append(f"Could not persist cost inputs: {exc}")

    return jsonify(report)


@bp.route("/api/license-prices", methods=["GET"])
def get_license_prices():
    """Return NCE license prices, GitHub seat costs, and resolve prices for specific SKU codes."""
    from onedrive_provisioner.license_prices import (
        NCE_LICENSE_PRICES, CPC_LICENSE_PRICES, resolve_license_prices,
        GITHUB_SEAT_COST_WITH_COPILOT, GITHUB_SEAT_COST_WITHOUT_COPILOT,
    )
    skus_param = request.args.get("skus", "")
    if skus_param:
        sku_list = [s.strip() for s in skus_param.split(",") if s.strip()]
        prices = resolve_license_prices(sku_list)
    else:
        prices = dict(NCE_LICENSE_PRICES)
    return jsonify({
        "licenses": prices,
        "cpc": dict(CPC_LICENSE_PRICES),
        "github": {
            "withCopilot": GITHUB_SEAT_COST_WITH_COPILOT,
            "withoutCopilot": GITHUB_SEAT_COST_WITHOUT_COPILOT,
        },
    })


# ────────────────────── Power Platform ──────────────────────

@bp.route("/api/hack-state/<prefix>/powerplatform", methods=["POST"])
def create_powerplatform_env(prefix):
    """Create a Power Apps environment for a hack and update state."""
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    hack_name = state.get("hackName") or state.get("prefix") or prefix
    env_display_name = f"AI HACK - {hack_name}"
    location = (data.get("location") or "unitedstates").strip()

    # Use the admin group as security group if available
    security_group_id = None
    groups = state.get("groups") or []
    # Prefer the all-users group or admin group for env access
    for g in groups:
        gname = g if isinstance(g, str) else (g.get("displayName") or "")
        if "all" in gname.lower() or "admin" in gname.lower():
            security_group_id = g.get("id") if isinstance(g, dict) else None
            break

    import asyncio
    from onedrive_provisioner.powerplatform.service import PowerPlatformService

    tp = make_token_provider(*creds)
    svc = PowerPlatformService(tp)

    try:
        result = asyncio.run(svc.create_environment(
            display_name=env_display_name,
            location=location,
            security_group_id=security_group_id,
        ))

        # Persist in state
        cfg = state.get("config") or {}
        cfg["enablePowerApps"] = True
        cfg["powerAppsEnvName"] = result.get("name", "")
        cfg["powerAppsEnvDisplayName"] = env_display_name
        cfg["powerAppsEnvId"] = result.get("id", "")
        cfg["powerAppsEnvUrl"] = result.get("url", "")
        state["config"] = cfg
        mgr.save_state(prefix, state)

        # Add users to environment if no security group was used
        user_results = []
        if not security_group_id:
            users = state.get("users") or []
            user_ids = [u.get("userId") for u in users if u.get("userId")]
            if user_ids:
                async def _add_users():
                    results = []
                    for uid in user_ids:
                        try:
                            r = await svc.add_user_to_environment(
                                result["name"], uid
                            )
                            results.append(r)
                        except Exception as exc:
                            results.append({
                                "status": "failed",
                                "userId": uid,
                                "error": str(exc),
                            })
                    return results
                user_results = asyncio.run(_add_users())

        result["usersAdded"] = len([r for r in user_results if r.get("status") == "added"])
        result["usersFailed"] = len([r for r in user_results if r.get("status") == "failed"])
        result["userResults"] = user_results
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/hack-state/<prefix>/powerplatform", methods=["DELETE"])
def delete_powerplatform_env(prefix):
    """Delete the Power Apps environment for a hack."""
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    cfg = state.get("config") or {}
    env_name = cfg.get("powerAppsEnvName") or ""
    if not env_name:
        return jsonify({"error": "No Power Apps environment found in hack state"}), 404

    import asyncio
    from onedrive_provisioner.powerplatform.service import PowerPlatformService

    tp = make_token_provider(*creds)
    svc = PowerPlatformService(tp)

    try:
        result = asyncio.run(svc.delete_environment(env_name))
        # Update state
        cfg["enablePowerApps"] = False
        cfg.pop("powerAppsEnvName", None)
        cfg.pop("powerAppsEnvId", None)
        cfg.pop("powerAppsEnvUrl", None)
        state["config"] = cfg
        mgr.save_state(prefix, state)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/powerplatform/environments", methods=["POST"])
def list_powerplatform_envs():
    """List Power Platform environments visible to the SPN."""
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    import asyncio
    from onedrive_provisioner.powerplatform.service import PowerPlatformService

    tp = make_token_provider(*creds)
    svc = PowerPlatformService(tp)
    try:
        envs = asyncio.run(svc.list_environments())
        return jsonify({"environments": envs})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── SharePoint Site ──────────────────────

@bp.route("/api/hack-state/<prefix>/sharepoint", methods=["POST"])
def create_sharepoint_site(prefix):
    """Create a SharePoint site for a hack and grant all groups/users access."""
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    site_name = (data.get("siteName") or "").strip()
    hack_name = state.get("hackName") or state.get("prefix") or prefix
    display_name = site_name or f"AI HACK - {hack_name}"

    # Generate a URL-safe alias from the display name
    import re
    alias = re.sub(r'[^a-zA-Z0-9]', '', display_name.lower())[:40] or "aihacksite"

    # Collect owner IDs (admin users)
    owner_ids = []
    for u in (state.get("users") or []):
        if u.get("isAdmin") and u.get("userId"):
            owner_ids.append(u["userId"])

    import asyncio
    from onedrive_provisioner.sharepoint.service import SharePointService

    tp = make_token_provider(*creds)
    svc = SharePointService(tp)

    try:
        result = asyncio.run(svc.create_site(
            display_name=display_name,
            alias=alias,
            description=f"SharePoint site for {hack_name}",
            owner_ids=owner_ids[:20] or None,
        ))

        group_id = result.get("groupId", "")

        # Add all hack users as members
        user_results = []
        if group_id:
            user_ids = [u.get("userId") for u in (state.get("users") or []) if u.get("userId")]
            group_ids = [g.get("id") for g in (state.get("groups") or []) if isinstance(g, dict) and g.get("id")]

            async def _grant_access():
                member_results = []
                # Add individual users
                if user_ids:
                    member_results.extend(await svc.add_members(group_id, user_ids))
                # Add groups
                if group_ids:
                    member_results.extend(await svc.add_group_members(group_id, group_ids))
                return member_results

            user_results = asyncio.run(_grant_access())

        # Persist in state
        cfg = state.get("config") or {}
        cfg["enableSharePoint"] = True
        cfg["sharePointSiteName"] = display_name
        cfg["sharePointSiteUrl"] = result.get("siteUrl", "")
        cfg["sharePointSiteId"] = result.get("siteId", "")
        cfg["sharePointGroupId"] = group_id
        state["config"] = cfg
        mgr.save_state(prefix, state)

        added = len([r for r in user_results if r.get("status") in ("added", "already-member")])
        failed = len([r for r in user_results if r.get("status") == "failed"])
        result["membersAdded"] = added
        result["membersFailed"] = failed
        result["memberResults"] = user_results
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/hack-state/<prefix>/sharepoint", methods=["DELETE"])
def delete_sharepoint_site(prefix):
    """Delete the SharePoint site (M365 group) for a hack."""
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    cfg = state.get("config") or {}
    group_id = cfg.get("sharePointGroupId") or ""
    if not group_id:
        return jsonify({"error": "No SharePoint site found in hack state"}), 404

    import asyncio
    from onedrive_provisioner.sharepoint.service import SharePointService

    tp = make_token_provider(*creds)
    svc = SharePointService(tp)

    try:
        result = asyncio.run(svc.delete_site(group_id))
        # Update state
        cfg["enableSharePoint"] = False
        cfg.pop("sharePointSiteName", None)
        cfg.pop("sharePointSiteUrl", None)
        cfg.pop("sharePointSiteId", None)
        cfg.pop("sharePointGroupId", None)
        state["config"] = cfg
        mgr.save_state(prefix, state)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── Template Library (Blob Storage) ──────────────────────

_TEMPLATE_BLOB = "_templates/library.json"


def _load_templates(mgr) -> list:
    data = mgr._blob.read_json(_TEMPLATE_BLOB)
    return data if isinstance(data, list) else []


def _save_templates(mgr, templates: list):
    mgr._blob.write_json(_TEMPLATE_BLOB, templates)


@bp.route("/api/templates", methods=["GET"])
def list_templates():
    mgr = get_state_manager()
    if not mgr:
        return jsonify([])
    return jsonify(_load_templates(mgr))


@bp.route("/api/templates", methods=["POST"])
def save_template():
    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    config = data.get("config")
    if not name or not config:
        return jsonify({"error": "name and config are required"}), 400

    import uuid
    templates = _load_templates(mgr)
    template = {
        "id": str(uuid.uuid4()),
        "name": name,
        "config": config,
        "created": datetime.now(timezone.utc).isoformat(),
        "createdBy": data.get("createdBy", ""),
    }
    templates.append(template)
    _save_templates(mgr, templates)
    return jsonify(template), 201


@bp.route("/api/templates/<template_id>", methods=["DELETE"])
def delete_template(template_id):
    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    templates = _load_templates(mgr)
    before = len(templates)
    templates = [t for t in templates if t.get("id") != template_id]
    if len(templates) == before:
        return jsonify({"error": "Template not found"}), 404
    _save_templates(mgr, templates)
    return jsonify({"ok": True})

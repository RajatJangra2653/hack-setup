"""Hack state management routes (blob storage CRUD + user operations)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List

from flask import Blueprint, request, jsonify

from onedrive_provisioner.graph import GraphClient

from ._state import (
    extract_creds, get_state_manager, make_token_provider,
    require_confirmation, is_archived_state,
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
    if data.get("fetchSubscriptionCosts"):
        creds = extract_creds(data)
        if not creds:
            return jsonify({"error": "SPN credentials required to fetch Azure costs"}), 400
        subscription_ids = data.get("subscriptionIds") or [
            sc.get("subscriptionId") or sc.get("subscription") or sc.get("id")
            for sc in subscription_costs if isinstance(sc, dict)
        ]
        subscription_ids = [str(s).strip() for s in subscription_ids if str(s or "").strip()]
        if not subscription_ids:
            return jsonify({"error": "subscriptionIds[] required to fetch Azure costs"}), 400
        default_start_date, default_end_date = _state_report_date_range(state)
        start_date = data.get("startDate") or default_start_date
        end_date = data.get("endDate") or default_end_date
        fetched_costs = asyncio.run(_async_fetch_subscription_costs(
            *creds,
            subscription_ids=subscription_ids,
            start_date=start_date,
            end_date=end_date,
        ))
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
    report = build_hack_report(
        state,
        subscription_costs=subscription_costs,
        license_unit_costs=merged_costs,
        currency=data.get("currency") or "USD",
        start_date=start_date,
        end_date=end_date,
        github_enabled=data.get("githubEnabled", config.get("enableGithub", False)),
        github_copilot=data.get("githubCopilot", config.get("enableGithubCopilot", False)),
    )
    if fetched_costs:
        report["costFetch"] = {"subscriptionsQueried": len(fetched_costs)}
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

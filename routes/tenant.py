"""Tenant info, permissions, discovery, cleanup, and read-only mode routes."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from flask import Blueprint, request, jsonify

from onedrive_provisioner.auth import MsalTokenProvider
from onedrive_provisioner.config import AzureConfig
from onedrive_provisioner.graph import GraphClient
from onedrive_provisioner.entra import (
    TenantService, RbacService, DiscoveryService, CleanupService,
    remove_rbac_for_principals, downgrade_principals_to_reader, ROLE_IDS,
)
from onedrive_provisioner.entra.rbac_service import subscription_from_assignment

from ._state import (
    extract_creds, get_state_manager, make_token_provider,
    require_confirmation,
)

bp = Blueprint("tenant", __name__)


# ────────────────────── Async helpers ──────────────────────

async def _async_tenant_info(t, c, s):
    tp = make_token_provider(t, c, s)
    async with GraphClient(tp) as g:
        ts = TenantService(g)
        domain, tap_max = await ts.get_tenant_info()
        try:
            sku_data = await g.get("/subscribedSkus")
            skus = sku_data.get("value", [])
        except Exception:
            skus = []
        return domain, tap_max, skus


async def _async_list_subscriptions(t, c, s):
    tp = make_token_provider(t, c, s)
    async with RbacService(tp) as rbac:
        return await rbac.list_subscriptions()


async def _async_assign_permissions(t, c, s, *, subscriptions, principals, role):
    tp = make_token_provider(t, c, s)
    out = []
    async with RbacService(tp) as rbac:
        for sub in subscriptions:
            for p in principals:
                try:
                    a = await rbac.assign_role(
                        sub, p["id"], role,
                        principal_type=p.get("type", "Group"),
                    )
                    out.append({
                        "subscription": sub, "principalId": p["id"],
                        "principalType": p.get("type"),
                        "displayName": p.get("displayName"),
                        "role": role, "status": "assigned",
                        "assignmentId": a.get("id"),
                    })
                except Exception as exc:
                    out.append({
                        "subscription": sub, "principalId": p["id"],
                        "principalType": p.get("type"),
                        "displayName": p.get("displayName"),
                        "role": role, "status": "failed", "error": str(exc),
                    })
    return out


async def _async_discover(t, c, s, prefix):
    tp = make_token_provider(t, c, s)
    async with GraphClient(tp) as g:
        return await DiscoveryService(g).discover(prefix)


def _readonly_principals(discovered: dict, mode: str) -> list[dict]:
    if mode == "flat":
        return [
            {
                "id": u.get("id"),
                "type": "User",
                "displayName": u.get("userPrincipalName") or u.get("displayName"),
                "userPrincipalName": u.get("userPrincipalName"),
            }
            for u in discovered.get("users", [])
            if u.get("id")
        ]
    return [
        {"id": g.get("id"), "type": "Group", "displayName": g.get("displayName")}
        for g in discovered.get("groups", [])
        if g.get("id")
    ]


def _role_name_from_assignment(assignment: dict) -> str:
    role_def_id = (assignment.get("properties", {}).get("roleDefinitionId") or "").lower()
    role_guid = role_def_id.rsplit("/", 1)[-1]
    role_names = {role_id.lower(): name for name, role_id in ROLE_IDS.items()}
    return role_names.get(role_guid, role_guid or "unknown")


async def _async_readonly_preview(t, c, s, *, prefix: str, mode: str, subscriptions: list[str]):
    tp = make_token_provider(t, c, s)
    async with GraphClient(tp) as g:
        discovered = await DiscoveryService(g).discover(prefix)

    principals = _readonly_principals(discovered, mode)
    requested_subs = []
    seen_subs = set()
    for sub in subscriptions or []:
        sub_id = str(sub or "").strip()
        if sub_id and sub_id.lower() not in seen_subs:
            requested_subs.append(sub_id)
            seen_subs.add(sub_id.lower())
    requested_filter = {s.lower() for s in requested_subs}

    async with RbacService(tp) as rbac:
        visible_subs = await rbac.list_subscriptions()
        sub_lookup = {
            (s.get("subscriptionId") or "").lower(): s
            for s in visible_subs
            if s.get("subscriptionId")
        }
        sub_stats: dict[str, dict] = {}
        principal_subscriptions = []
        for principal in principals:
            entry = {**principal, "subscriptions": [], "errors": []}
            try:
                assignments = await rbac.list_all_assignments_for_principal(principal["id"])
            except Exception as exc:
                entry["errors"].append(str(exc))
                principal_subscriptions.append(entry)
                continue

            by_sub: dict[str, list] = {}
            for assignment in assignments:
                sub_id = subscription_from_assignment(assignment)
                if not sub_id:
                    continue
                if requested_filter and sub_id.lower() not in requested_filter:
                    continue
                role_name = _role_name_from_assignment(assignment)
                by_sub.setdefault(sub_id, []).append({
                    "assignmentId": assignment.get("id", ""),
                    "role": role_name,
                })

            for sub_id, sub_assignments in sorted(by_sub.items()):
                meta = sub_lookup.get(sub_id.lower(), {})
                roles = sorted({a.get("role", "") for a in sub_assignments if a.get("role")})
                entry["subscriptions"].append({
                    "subscriptionId": sub_id,
                    "displayName": meta.get("displayName") or "",
                    "state": meta.get("state") or "",
                    "assignmentCount": len(sub_assignments),
                    "roles": roles,
                })
                stats = sub_stats.setdefault(sub_id, {
                    "subscriptionId": sub_id,
                    "displayName": meta.get("displayName") or "",
                    "state": meta.get("state") or "",
                    "matchedPrincipalCount": 0,
                    "assignmentCount": 0,
                    "roles": set(),
                })
                stats["matchedPrincipalCount"] += 1
                stats["assignmentCount"] += len(sub_assignments)
                stats["roles"].update(roles)

            if entry["subscriptions"] or entry["errors"]:
                principal_subscriptions.append(entry)

        target_sub_ids = requested_subs if requested_subs else sorted(
            sub_stats,
            key=lambda sid: ((sub_stats[sid].get("displayName") or "").lower(), sid.lower()),
        )
        subscription_details = []
        for sub_id in target_sub_ids:
            meta = sub_lookup.get(sub_id.lower(), {})
            stats = sub_stats.get(sub_id, {})
            roles = stats.get("roles") or set()
            subscription_details.append({
                "subscriptionId": sub_id,
                "displayName": meta.get("displayName") or stats.get("displayName") or "",
                "state": meta.get("state") or stats.get("state") or "",
                "matchedPrincipalCount": stats.get("matchedPrincipalCount", 0),
                "assignmentCount": stats.get("assignmentCount", 0),
                "roles": sorted(roles) if not isinstance(roles, list) else sorted(roles),
                "source": "specified" if requested_subs else "auto-detected",
                "visibleToSpn": bool(meta),
            })

    return {
        "prefix": prefix,
        "mode": mode,
        "autoDetectedSubscriptions": not bool(requested_subs),
        "users": discovered.get("users", []),
        "groups": discovered.get("groups", []),
        "principals": principals,
        "subscriptions": subscription_details,
        "principalSubscriptions": principal_subscriptions,
    }


async def _async_cleanup(t, c, s, *, user_ids, group_ids, sub_ids, principal_ids):
    tp = make_token_provider(t, c, s)
    out = {"users": [], "groups": [], "rbac": []}
    if sub_ids and principal_ids:
        async with RbacService(tp) as rbac:
            out["rbac"] = await remove_rbac_for_principals(rbac, sub_ids, principal_ids)
    async with GraphClient(tp) as g:
        cleaner = CleanupService(g)
        if user_ids:
            out["users"] = await cleaner.delete_users(user_ids)
        if group_ids:
            out["groups"] = await cleaner.delete_groups(group_ids)
    return out


async def _async_readonly(t, c, s, *, subscriptions, principals):
    tp = make_token_provider(t, c, s)
    async with RbacService(tp) as rbac:
        return await downgrade_principals_to_reader(rbac, subscriptions, principals)


# ────────────────────── Check / Grant permissions ──────────────────────

GRAPH_APPID = "00000003-0000-0000-c000-000000000000"

REQUIRED_GRAPH_PERMISSIONS = [
    {"value": "User.ReadWrite.All",                      "reason": "Create, update, delete users"},
    {"value": "Group.ReadWrite.All",                     "reason": "Create team/admin groups"},
    {"value": "GroupMember.ReadWrite.All",                "reason": "Add users to groups"},
    {"value": "Organization.Read.All",                   "reason": "Read tenant info & subscribed SKUs"},
    {"value": "RoleManagement.ReadWrite.Directory",      "reason": "Assign Global Reader to admin users"},
    {"value": "UserAuthenticationMethod.ReadWrite.All",  "reason": "Create Temporary Access Passes (TAP)"},
    {"value": "Policy.Read.All",                         "reason": "Read TAP policy configuration"},
]

OPTIONAL_GRAPH_PERMISSIONS = [
    {"value": "Files.ReadWrite.All",  "reason": "Upload files to users' OneDrive (optional)"},
]

SELF_GRANT_PERMISSION = "AppRoleAssignment.ReadWrite.All"


async def _async_check_permissions(t, c, s):
    import httpx
    tp = make_token_provider(t, c, s)
    tok = await tp.get_token()
    H = {"Authorization": f"Bearer {tok}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        sp_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{c}'",
            headers=H,
        )
        sp_data = sp_resp.json().get("value", [])
        if not sp_data:
            raise ValueError(f"Service principal not found for client_id {c}")
        sp_id = sp_data[0]["id"]
        sp_display = sp_data[0].get("displayName", c)

        graph_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{GRAPH_APPID}'",
            headers=H,
        )
        graph_data = graph_resp.json().get("value", [])
        if not graph_data:
            raise ValueError("Microsoft Graph service principal not found in tenant")
        graph_sp_id = graph_data[0]["id"]
        roles_by_value = {r["value"]: r for r in graph_data[0].get("appRoles", [])}

        assignments_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp_id}/appRoleAssignments",
            headers=H,
        )
        existing = assignments_resp.json().get("value", [])
        existing_role_ids = {
            a["appRoleId"] for a in existing
            if a.get("resourceId") == graph_sp_id
        }

        results = []
        for perm in REQUIRED_GRAPH_PERMISSIONS + OPTIONAL_GRAPH_PERMISSIONS:
            role = roles_by_value.get(perm["value"])
            granted = role["id"] in existing_role_ids if role else False
            is_optional = perm in OPTIONAL_GRAPH_PERMISSIONS
            results.append({
                "permission": perm["value"],
                "reason": perm["reason"],
                "granted": granted,
                "optional": is_optional,
            })

        self_grant_role = roles_by_value.get(SELF_GRANT_PERMISSION)
        can_self_grant = (
            self_grant_role["id"] in existing_role_ids
            if self_grant_role else False
        )

        return {
            "spnId": sp_id,
            "spnDisplayName": sp_display,
            "permissions": results,
            "canSelfGrant": can_self_grant,
        }


async def _async_grant_permissions(t, c, s, permissions_to_grant):
    import httpx
    tp = make_token_provider(t, c, s)
    tok = await tp.get_token()
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        sp_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{c}'",
            headers=H,
        )
        sp_data = sp_resp.json().get("value", [])
        if not sp_data:
            raise ValueError(f"Service principal not found for client_id {c}")
        sp_id = sp_data[0]["id"]

        graph_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{GRAPH_APPID}'",
            headers=H,
        )
        graph_data = graph_resp.json().get("value", [])
        if not graph_data:
            raise ValueError("Microsoft Graph service principal not found")
        graph_sp_id = graph_data[0]["id"]
        roles_by_value = {r["value"]: r for r in graph_data[0].get("appRoles", [])}

        assignments_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp_id}/appRoleAssignments",
            headers=H,
        )
        existing = assignments_resp.json().get("value", [])
        existing_role_ids = {
            a["appRoleId"] for a in existing
            if a.get("resourceId") == graph_sp_id
        }

        results = []
        for perm_value in permissions_to_grant:
            role = roles_by_value.get(perm_value)
            if not role:
                results.append({"permission": perm_value, "status": "not_found",
                                "error": "Permission not found in Graph appRoles"})
                continue
            if role["id"] in existing_role_ids:
                results.append({"permission": perm_value, "status": "already_granted"})
                continue
            r = await client.post(
                f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp_id}/appRoleAssignments",
                headers=H,
                json={
                    "principalId": sp_id,
                    "resourceId": graph_sp_id,
                    "appRoleId": role["id"],
                },
            )
            if r.status_code in (200, 201):
                results.append({"permission": perm_value, "status": "granted"})
            else:
                err_body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                err_msg = err_body.get("error", {}).get("message", r.text[:200])
                results.append({"permission": perm_value, "status": "failed", "error": err_msg})

        return results


# ────────────────────── Route handlers ──────────────────────

@bp.route("/api/tenant-info", methods=["POST"])
def tenant_info():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    try:
        domain, tap_max, skus = asyncio.run(_async_tenant_info(*creds))
        sku_summary = [
            {
                "skuPartNumber": s.get("skuPartNumber", ""),
                "skuId": s.get("skuId", ""),
                "consumedUnits": s.get("consumedUnits", 0),
                "prepaidUnits": s.get("prepaidUnits", {}),
            }
            for s in skus
        ]
        return jsonify({
            "domain": domain,
            "tapMaxLifetimeMinutes": tap_max,
            "subscribedSkus": sku_summary,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/subscriptions", methods=["POST"])
def list_subscriptions():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    try:
        query = (data.get("query") or "").strip().lower()
        subscriptions = asyncio.run(_async_list_subscriptions(*creds))
        if query:
            subscriptions = [
                s for s in subscriptions
                if query in (s.get("subscriptionId") or "").lower()
                or query in (s.get("displayName") or "").lower()
            ]
        subscriptions = sorted(
            subscriptions,
            key=lambda s: ((s.get("displayName") or "").lower(), s.get("subscriptionId") or ""),
        )
        return jsonify({"subscriptions": subscriptions, "count": len(subscriptions)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/assign-permissions", methods=["POST"])
def assign_permissions():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    subs = data.get("subscriptions") or []
    principals = data.get("principals") or []
    role = data.get("role")
    if not subs or not principals or role not in ROLE_IDS:
        return jsonify({"error": "subscriptions[], principals[], role required"}), 400
    hack_prefix = (data.get("hackPrefix") or data.get("prefix") or "").strip()
    if not hack_prefix:
        return jsonify({"error": "hackPrefix is required for privileged RBAC assignment confirmation"}), 400
    confirmation_needed = require_confirmation("assign_permissions", {
        "prefix": hack_prefix,
        "role": role,
        "resourceCount": len(principals),
        "principalCount": len(principals),
        "subscriptionCount": len(subs),
    }, data)
    if confirmation_needed:
        return confirmation_needed
    try:
        results = asyncio.run(_async_assign_permissions(
            *creds, subscriptions=subs, principals=principals, role=role,
        ))
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/discover-hack", methods=["POST"])
def discover_hack():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    prefix = (data.get("prefix") or "").strip()
    if not prefix:
        return jsonify({"error": "prefix required"}), 400
    try:
        return jsonify(asyncio.run(_async_discover(*creds, prefix)))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/readonly-preview", methods=["POST"])
def readonly_preview():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    prefix = (data.get("prefix") or data.get("hackPrefix") or "").strip()
    if not prefix:
        return jsonify({"error": "prefix required"}), 400
    mode = (data.get("mode") or "team").strip().lower()
    if mode not in {"team", "flat"}:
        return jsonify({"error": "mode must be team or flat"}), 400
    try:
        return jsonify(asyncio.run(_async_readonly_preview(
            *creds,
            prefix=prefix,
            mode=mode,
            subscriptions=data.get("subscriptions") or [],
        )))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/cleanup-hack", methods=["POST"])
def cleanup_hack():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    try:
        prefix = (data.get("hackPrefix") or "").strip()
        if not prefix:
            return jsonify({"error": "hackPrefix is required for cleanup confirmation"}), 400
        user_ids = data.get("userIds") or []
        group_ids = data.get("groupIds") or []
        sub_ids = data.get("subscriptionIds") or []
        principal_ids = data.get("principalIds") or []
        confirmation_needed = require_confirmation("cleanup_hack", {
            "prefix": prefix,
            "resourceCount": len(user_ids) + len(group_ids),
            "userCount": len(user_ids),
            "groupCount": len(group_ids),
            "principalCount": len(principal_ids),
            "subscriptionCount": len(sub_ids),
        }, data)
        if confirmation_needed:
            return confirmation_needed
        result = asyncio.run(_async_cleanup(
            *creds,
            user_ids=user_ids,
            group_ids=group_ids,
            sub_ids=sub_ids,
            principal_ids=principal_ids,
        ))
        if prefix:
            try:
                mgr = get_state_manager()
                if mgr:
                    archived = mgr.archive_state(prefix, cleanup_result=result)
                    result["blob_state_archived"] = archived
                    result["blob_state_deleted"] = False
                else:
                    result["blob_state_archived"] = False
                    result["blob_state_deleted"] = False
                    result["blob_state_note"] = "Storage not configured"
            except Exception as exc:
                result["blob_state_error"] = str(exc)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/readonly-mode", methods=["POST"])
def readonly_mode():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    subs = data.get("subscriptions") or []
    principals = data.get("principals") or []
    if not principals:
        return jsonify({"error": "principals[] required"}), 400
    hack_prefix = (data.get("hackPrefix") or data.get("prefix") or "").strip()
    if not hack_prefix:
        return jsonify({"error": "hackPrefix is required for read-only confirmation"}), 400
    confirmation_needed = require_confirmation("readonly_mode", {
        "prefix": hack_prefix,
        "resourceCount": len(principals),
        "principalCount": len(principals),
        "subscriptionCount": len(subs),
    }, data)
    if confirmation_needed:
        return confirmation_needed
    try:
        results = asyncio.run(_async_readonly(*creds,
                                              subscriptions=subs, principals=principals))
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/check-permissions", methods=["POST"])
def check_permissions():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    try:
        result = asyncio.run(_async_check_permissions(*creds))
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/grant-permissions", methods=["POST"])
def grant_permissions():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    perms = data.get("permissions") or []
    if not perms:
        return jsonify({"error": "permissions[] required"}), 400
    try:
        results = asyncio.run(_async_grant_permissions(*creds, perms))
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

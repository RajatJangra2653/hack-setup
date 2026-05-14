"""Entra ID user provisioning and preflight routes."""
from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from flask import Blueprint, request, jsonify

from onedrive_provisioner.config import AzureConfig
from onedrive_provisioner.auth import MsalTokenProvider
from onedrive_provisioner.graph import GraphClient
from onedrive_provisioner.logging_setup import configure_logging
from onedrive_provisioner.entra import (
    EntraOrchestrator, EntraConfig, RbacService, run_preflight,
)
from onedrive_provisioner.onedrive import sp_delegated
from onedrive_provisioner.storage import HackStateManager

from ._state import (
    extract_creds, get_state_manager,
    entra_sessions, entra_lock, MAX_ENTRA_SESSIONS,
    prov_sessions, prov_lock,
    audit_logger, operation_tracker,
)

bp = Blueprint("provision", __name__)


# ────────────────────── Background workers ──────────────────────

def _run_entra_provision(session_id: str, cfg_dict: dict,
                         tenant_id: str, client_id: str, client_secret: str):
    def _set(**kw):
        with entra_lock:
            s = entra_sessions.get(session_id)
            if s:
                s.update(kw)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()

    partial: List[dict] = []

    def _on_user_done(result, done, total):
        partial.append(result.to_dict())
        with entra_lock:
            s = entra_sessions.get(session_id)
            if s:
                s["processed"] = done
                s["total"] = total
                s["partial_users"] = list(partial)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        configure_logging("INFO")
        cfg = EntraConfig.from_dict(cfg_dict)
        if not cfg.domain:
            raise ValueError("'domain' is required (e.g. WWPS319.onmicrosoft.com)")

        prefix = cfg_dict.get("prefix", "unknown")
        op = operation_tracker.start("provision", prefix, actor=cfg_dict.get("createdBy", ""))
        audit_logger.log("provision.started", prefix, actor=cfg_dict.get("createdBy", ""),
                         details={"sessionId": session_id, "userCount": cfg_dict.get("userCount")})
        op.step("create_users", f"Provisioning users for {prefix}")

        azure = AzureConfig(tenant_id=tenant_id, client_id=client_id,
                            client_secret=client_secret)
        orch = EntraOrchestrator(azure, concurrency=int(cfg_dict.get("concurrency", 6)))
        report = asyncio.run(orch.provision(cfg, on_user_done=_on_user_done))

        op.step_done("create_users", result={"total": report.total_users, "created": report.created})

        _set(status="completed", result=report.to_dict(),
             processed=report.total_users, total=report.total_users)

        try:
            mgr = get_state_manager()
            if mgr:
                state = HackStateManager.build_state_from_report(
                    cfg_dict, report.to_dict(), session_id=session_id)
                mgr.save_state(cfg_dict.get("prefix", "unknown"), state)
        except Exception as blob_exc:
            print(f"[WARN] Failed to save state to blob: {blob_exc}")

        op.complete(result={"totalUsers": report.total_users, "created": report.created})
        audit_logger.log("provision.completed", prefix, actor=cfg_dict.get("createdBy", ""),
                         details={"totalUsers": report.total_users, "created": report.created})
        operation_tracker.finish(op)
    except Exception as exc:
        _set(status="failed", error=str(exc))
        prefix = cfg_dict.get("prefix", "unknown")
        audit_logger.log("provision.failed", prefix, actor=cfg_dict.get("createdBy", ""),
                         details={"error": str(exc)}, severity="error")
        if "op" in locals():
            op.fail(str(exc))
            operation_tracker.finish(op)


async def _async_preflight(t, c, s, *, cfg_dict, subscriptions):
    cfg = EntraConfig.from_dict(cfg_dict)
    azure = AzureConfig(tenant_id=t, client_id=c, client_secret=s)
    tp = MsalTokenProvider(azure)
    async with GraphClient(tp) as g:
        if subscriptions:
            async with RbacService(tp) as rbac:
                return await run_preflight(g, rbac, cfg, subscription_ids=subscriptions)
        return await run_preflight(g, None, cfg, subscription_ids=None)


# ────────────────────── Device-code provisioning ──────────────────────

@bp.route("/api/provision/start", methods=["POST"])
def provision_start():
    body = request.get_json(silent=True) or {}
    tenant_id = (body.get("tenant_id") or "").strip()
    emails = body.get("users") or []
    emails = [e.strip() for e in emails if e and e.strip()]

    if not tenant_id:
        return jsonify({"error": "Missing tenant_id"}), 400
    if not emails:
        return jsonify({"error": "No users provided"}), 400

    admin_url = sp_delegated.tenant_admin_url(emails[0])

    try:
        flow = sp_delegated.initiate_device_flow(tenant_id, admin_url)
    except Exception as exc:
        return jsonify({"error": f"Device flow init failed: {exc}"}), 500

    session_id = str(uuid.uuid4())
    with prov_lock:
        prov_sessions[session_id] = {
            "id": session_id,
            "status": "awaiting_login",
            "tenant_id": tenant_id,
            "admin_url": admin_url,
            "emails": emails,
            "flow": flow,
            "result": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _run():
        try:
            token = sp_delegated.acquire_token_by_device_flow(flow)
            with prov_lock:
                s = prov_sessions.get(session_id)
                if s:
                    s["status"] = "provisioning"

            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    sp_delegated.enqueue_personal_sites(admin_url, token, emails)
                )
            finally:
                loop.close()

            with prov_lock:
                s = prov_sessions.get(session_id)
                if s:
                    s["status"] = "completed" if result["ok"] else "failed"
                    s["result"] = result
                    s.pop("flow", None)
        except Exception as exc:
            with prov_lock:
                s = prov_sessions.get(session_id)
                if s:
                    s["status"] = "failed"
                    s["error"] = str(exc)
                    s.pop("flow", None)

    threading.Thread(target=_run, daemon=True).start()

    return jsonify({
        "session_id": session_id,
        "user_code": flow["user_code"],
        "verification_uri": flow["verification_uri"],
        "expires_in": flow.get("expires_in", 900),
        "message": flow.get("message"),
        "user_count": len(emails),
    })


@bp.route("/api/provision/<session_id>", methods=["GET"])
def provision_status(session_id):
    with prov_lock:
        s = prov_sessions.get(session_id)
        if not s:
            return jsonify({"error": "Session not found"}), 404
        return jsonify({k: v for k, v in s.items() if k != "flow"})


# ────────────────────── Entra ID provisioning ──────────────────────

@bp.route("/api/preflight", methods=["POST"])
def preflight():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    cfg_dict = data.get("config") or {}
    if not isinstance(cfg_dict, dict):
        return jsonify({"error": "'config' must be an object"}), 400
    subs = data.get("subscriptions") or []
    try:
        report = asyncio.run(_async_preflight(*creds, cfg_dict=cfg_dict,
                                              subscriptions=subs))
        return jsonify(report)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/provision-users", methods=["POST"])
def provision_users_start():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials (tenant_id/client_id/client_secret)"}), 400
    cfg_dict = data.get("config") or {}
    if not isinstance(cfg_dict, dict):
        return jsonify({"error": "'config' must be an object"}), 400
    if not cfg_dict.get("domain"):
        return jsonify({"error": "config.domain is required"}), 400

    tenant_id, client_id, client_secret = creds
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    session = {
        "id": session_id,
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "config": {k: v for k, v in cfg_dict.items() if k != "initialPassword"},
        "processed": 0,
        "total": 0,
        "partial_users": [],
        "result": None,
        "error": None,
    }
    with entra_lock:
        if len(entra_sessions) >= MAX_ENTRA_SESSIONS:
            oldest = min(entra_sessions, key=lambda k: entra_sessions[k]["created_at"])
            del entra_sessions[oldest]
        entra_sessions[session_id] = session

    threading.Thread(
        target=_run_entra_provision,
        args=(session_id, cfg_dict, tenant_id, client_id, client_secret),
        daemon=True,
    ).start()

    return jsonify({"session_id": session_id, "status": "running"}), 202


@bp.route("/api/provision-users/<session_id>", methods=["GET"])
def provision_users_status(session_id):
    with entra_lock:
        s = entra_sessions.get(session_id)
        if not s:
            return jsonify({"error": "Session not found"}), 404
        return jsonify(s)


@bp.route("/api/provision-users", methods=["GET"])
def provision_users_list():
    with entra_lock:
        out = [
            {k: v for k, v in s.items() if k not in ("result", "partial_users")}
            for s in sorted(entra_sessions.values(),
                            key=lambda s: s["created_at"], reverse=True)
        ]
    return jsonify(out)


# ────────────────────── Hack expansion (add teams / users / admins) ──────────────────────

def _run_entra_expand(session_id: str, prefix: str, merged_cfg: dict,
                      delta: dict, tenant_id: str, client_id: str, client_secret: str):
    """Run orchestrator with `skip_existing=True` so only the *new* users/teams
    are created, then MERGE the new users into the existing state (instead of
    overwriting it like full provisioning does). Updates state.config counts so
    subsequent expansions resume from the new max indices.
    """
    def _set(**kw):
        with entra_lock:
            s = entra_sessions.get(session_id)
            if s:
                s.update(kw)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()

    partial: List[dict] = []

    def _on_user_done(result, done, total):
        partial.append(result.to_dict())
        with entra_lock:
            s = entra_sessions.get(session_id)
            if s:
                s["processed"] = done
                s["total"] = total
                s["partial_users"] = list(partial)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        configure_logging("INFO")
        cfg = EntraConfig.from_dict(merged_cfg)
        cfg.skip_existing = True  # critical: never recreate existing users
        if not cfg.domain:
            raise ValueError("'domain' is required to expand a hack")

        op = operation_tracker.start("expand", prefix, actor=merged_cfg.get("createdBy", ""))
        audit_logger.log("expand.started", prefix, actor=merged_cfg.get("createdBy", ""),
                         details={"sessionId": session_id, "delta": delta})
        op.step("expand_users", f"Expanding {prefix} (+{delta})")

        azure = AzureConfig(tenant_id=tenant_id, client_id=client_id,
                            client_secret=client_secret)
        orch = EntraOrchestrator(azure, concurrency=int(merged_cfg.get("concurrency", 6)))
        report = asyncio.run(orch.provision(cfg, on_user_done=_on_user_done))

        op.step_done("expand_users", result={"total": report.total_users, "created": report.created})

        _set(status="completed", result=report.to_dict(),
             processed=report.total_users, total=report.total_users)

        # Merge new users into existing state; update config counts.
        try:
            mgr = get_state_manager()
            if mgr:
                report_dict = report.to_dict()
                # Only newly created (or freshly discovered as "existing") users
                # need a state-row; merge_users will upsert by UPN.
                new_user_rows = []
                now = datetime.now(timezone.utc).isoformat()
                for u in report_dict.get("users", []):
                    new_user_rows.append({
                        "userPrincipalName": u.get("userPrincipalName", ""),
                        "userId": u.get("userId", ""),
                        "status": u.get("status", ""),
                        "password": u.get("password", ""),
                        "tap": u.get("tap", ""),
                        "tapExpires": u.get("tapExpires", ""),
                        "licenses": u.get("licenses", []),
                        "groups": u.get("groups", []),
                        "groupFailures": u.get("groupFailures", []),
                        "isAdmin": u.get("isAdmin", False),
                        "message": u.get("message", ""),
                        "github": u.get("github"),
                        "provisionedAt": now,
                    })
                state = mgr.merge_users(prefix, new_user_rows)
                # Persist new counts so future reports/expansions know the size
                state_cfg = state.get("config") or {}
                state_cfg["teams"] = merged_cfg.get("teams", state_cfg.get("teams"))
                state_cfg["usersPerTeam"] = merged_cfg.get("usersPerTeam", state_cfg.get("usersPerTeam"))
                state_cfg["adminUsers"] = merged_cfg.get("adminUsers", state_cfg.get("adminUsers"))
                state["config"] = state_cfg
                state["lastUpdated"] = now
                # Update summary roll-up
                summary = state.get("summary") or {}
                summary["totalUsers"] = len(state.get("users") or [])
                summary["admins"] = sum(1 for u in (state.get("users") or []) if u.get("isAdmin"))
                state["summary"] = summary
                state["totalUsers"] = summary["totalUsers"]
                # Merge any newly created groups (avoid dupes by id/displayName)
                existing_groups = state.get("groups") or []
                seen = {(g.get("id") if isinstance(g, dict) else g) for g in existing_groups}
                for g in (report_dict.get("groups") or []):
                    key = g.get("id") if isinstance(g, dict) else g
                    if key and key not in seen:
                        existing_groups.append(g)
                        seen.add(key)
                state["groups"] = existing_groups
                mgr.save_state(prefix, state)
        except Exception as blob_exc:
            print(f"[WARN] Failed to merge expansion into state: {blob_exc}")

        op.complete(result={"totalCreated": report.created, "delta": delta})
        audit_logger.log("expand.completed", prefix, actor=merged_cfg.get("createdBy", ""),
                         details={"created": report.created, "delta": delta})
        operation_tracker.finish(op)
    except Exception as exc:
        _set(status="failed", error=str(exc))
        audit_logger.log("expand.failed", prefix, actor=merged_cfg.get("createdBy", ""),
                         details={"error": str(exc), "delta": delta}, severity="error")
        if "op" in locals():
            op.fail(str(exc))
            operation_tracker.finish(op)


@bp.route("/api/hack-state/<prefix>/expand", methods=["POST"])
def expand_hack(prefix):
    """Add more teams / participants / admins to an EXISTING hack.

    Body:
      {
        "tenant_id", "client_id", "client_secret": SPN credentials (required),
        "addTeams": int (optional, default 0),
        "addParticipantsPerTeam": int (optional, default 0),
        "addAdmins": int (optional, default 0),
        "dryRun": bool (optional, default false)
      }

    Indices continue from the existing max — orchestrator's skip_existing
    guarantees existing users are never touched.
    """
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    add_teams = int(data.get("addTeams") or 0)
    add_users_per_team = int(data.get("addParticipantsPerTeam") or 0)
    add_admins = int(data.get("addAdmins") or 0)
    if add_teams < 0 or add_users_per_team < 0 or add_admins < 0:
        return jsonify({"error": "Delta values must be >= 0"}), 400
    if add_teams == 0 and add_users_per_team == 0 and add_admins == 0:
        return jsonify({"error": "Specify at least one of addTeams, addParticipantsPerTeam, or addAdmins (>0)"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    state_cfg = dict(state.get("config") or {})
    if not state_cfg.get("domain"):
        return jsonify({"error": "Existing hack state has no config.domain — cannot expand"}), 400

    cur_teams = int(state_cfg.get("teams") or 0)
    cur_upt = int(state_cfg.get("usersPerTeam") or 0)
    cur_admins = int(state_cfg.get("adminUsers") or 0)
    new_teams = cur_teams + add_teams
    new_upt = cur_upt + add_users_per_team
    new_admins = cur_admins + add_admins

    merged_cfg = dict(state_cfg)
    merged_cfg["prefix"] = state_cfg.get("prefix") or prefix
    merged_cfg["teams"] = new_teams
    merged_cfg["usersPerTeam"] = new_upt
    merged_cfg["adminUsers"] = new_admins
    merged_cfg["skipExisting"] = True
    merged_cfg["dryRun"] = bool(data.get("dryRun"))
    # By default keep the same initial password from the original hack;
    # caller can override with 'password' to set a new one, or 'randomPasswords'
    # to force random generation for the new batch.
    if data.get("password"):
        merged_cfg["initialPassword"] = data["password"]
    elif data.get("randomPasswords"):
        merged_cfg.pop("initialPassword", None)

    delta = {
        "addTeams": add_teams,
        "addParticipantsPerTeam": add_users_per_team,
        "addAdmins": add_admins,
        "newTotals": {"teams": new_teams, "usersPerTeam": new_upt, "adminUsers": new_admins},
        "expectedNewUsers": (
            (add_teams * new_upt) +                  # new teams × new per-team count
            (add_users_per_team * cur_teams) +       # extra users in existing teams
            add_admins
        ),
    }

    tenant_id, client_id, client_secret = creds
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    session = {
        "id": session_id,
        "status": "running",
        "kind": "expand",
        "prefix": prefix,
        "delta": delta,
        "created_at": now,
        "updated_at": now,
        "config": {k: v for k, v in merged_cfg.items() if k != "initialPassword"},
        "processed": 0,
        "total": 0,
        "partial_users": [],
        "result": None,
        "error": None,
    }
    with entra_lock:
        if len(entra_sessions) >= MAX_ENTRA_SESSIONS:
            oldest = min(entra_sessions, key=lambda k: entra_sessions[k]["created_at"])
            del entra_sessions[oldest]
        entra_sessions[session_id] = session

    threading.Thread(
        target=_run_entra_expand,
        args=(session_id, prefix, merged_cfg, delta, tenant_id, client_id, client_secret),
        daemon=True,
    ).start()

    return jsonify({
        "session_id": session_id,
        "status": "running",
        "delta": delta,
    }), 202

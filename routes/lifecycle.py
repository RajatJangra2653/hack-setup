"""Routes for audit trail, drift detection, and operation history.

Provides:
  GET  /api/hack-state/<prefix>/audit     — query audit events
  POST /api/hack-state/<prefix>/drift     — run drift detection
  GET  /api/hack-state/<prefix>/operations — operation history
  GET  /api/operations/active              — currently running operations
"""
from __future__ import annotations

import asyncio

from flask import Blueprint, request, jsonify

from onedrive_provisioner.graph import GraphClient
from onedrive_provisioner.drift import detect_drift

from ._state import (
    extract_creds, get_state_manager, make_token_provider,
    audit_logger, operation_tracker,
)

bp = Blueprint("lifecycle", __name__)


# ────────────────────── Audit ──────────────────────

@bp.route("/api/hack-state/<prefix>/audit", methods=["GET"])
def get_audit(prefix):
    """Query audit events for a hack prefix."""
    event_type = request.args.get("type", "")
    actor = request.args.get("actor", "")
    since = request.args.get("since", "")
    limit = min(int(request.args.get("limit", "100")), 500)

    events = audit_logger.query(
        prefix,
        event_type=event_type,
        actor=actor,
        since=since,
        limit=limit,
    )
    return jsonify({"prefix": prefix, "events": events, "count": len(events)})


# ────────────────────── Drift Detection ──────────────────────

@bp.route("/api/hack-state/<prefix>/drift", methods=["POST"])
def check_drift(prefix):
    """Run drift detection: compare saved state vs live Entra."""
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials (tenant_id, client_id, client_secret)"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503

    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    t, c, s = creds
    tp = make_token_provider(t, c, s)

    async def _run():
        async with GraphClient(tp) as g:
            return await detect_drift(g, state, prefix)

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return jsonify({"error": f"Drift check failed: {exc}"}), 500

    # Audit the drift check
    audit_logger.log(
        "drift.detected" if result.has_drift else "drift.clean",
        prefix,
        actor=data.get("operator", "unknown"),
        details={"summary": result.summary, "hasDrift": result.has_drift},
        severity="warning" if result.has_drift else "info",
    )

    return jsonify(result.to_dict())


@bp.route("/api/hack-state/<prefix>/drift/sync", methods=["POST"])
def sync_drift_extras(prefix):
    """Absorb extra users/groups from Entra into hack state."""
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

    t, c, s = creds
    tp = make_token_provider(t, c, s)

    async def _run():
        async with GraphClient(tp) as g:
            return await detect_drift(g, state, prefix)

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        return jsonify({"error": f"Drift check failed: {exc}"}), 500

    if not result.extra_users and not result.extra_groups:
        return jsonify({"message": "No extras to absorb", "absorbed": 0})

    users = state.get("users", []) or []
    existing_upns = {
        (u.get("userPrincipalName", "") if isinstance(u, dict) else "").lower()
        for u in users
    }

    absorbed_users = []
    for eu in result.extra_users:
        upn = eu.get("userPrincipalName", "")
        if upn.lower() not in existing_upns:
            users.append({
                "userPrincipalName": upn,
                "userId": eu.get("id", ""),
                "status": "absorbed",
                "provisionedAt": None,
                "absorbedFromDrift": True,
                "password": None,
                "tap": None,
                "tapExpires": None,
                "licenses": [],
                "groups": [],
                "isAdmin": False,
            })
            absorbed_users.append(upn)
    state["users"] = users

    groups = state.get("groups", []) or []
    existing_groups = set()
    for g in groups:
        if isinstance(g, str):
            existing_groups.add(g.lower())
        elif isinstance(g, dict):
            existing_groups.add(g.get("displayName", "").lower())

    absorbed_groups = []
    for eg in result.extra_groups:
        name = eg.get("displayName", "")
        if name.lower() not in existing_groups:
            groups.append(name)
            absorbed_groups.append(name)
    state["groups"] = groups

    state["totalUsers"] = len(state["users"])
    mgr.save_state(prefix, state)

    audit_logger.log(
        "drift.absorbed",
        prefix,
        actor=data.get("operator", "unknown"),
        details={
            "absorbedUsers": absorbed_users,
            "absorbedGroups": absorbed_groups,
        },
        severity="info",
    )

    return jsonify({
        "message": f"Absorbed {len(absorbed_users)} users, {len(absorbed_groups)} groups into state",
        "absorbedUsers": absorbed_users,
        "absorbedGroups": absorbed_groups,
        "totalUsers": state["totalUsers"],
    })


# ────────────────────── Operation History ──────────────────────

@bp.route("/api/hack-state/<prefix>/operations", methods=["GET"])
def get_operations(prefix):
    """Get operation history for a hack prefix."""
    limit = min(int(request.args.get("limit", "50")), 200)
    history = operation_tracker.get_history(prefix, limit=limit)
    active = operation_tracker.get_active(prefix)
    return jsonify({
        "prefix": prefix,
        "active": active,
        "history": history,
        "totalActive": len(active),
        "totalHistory": len(history),
    })


@bp.route("/api/operations/active", methods=["GET"])
def get_active_operations():
    """Get all currently running operations across all hacks."""
    active = operation_tracker.get_active()
    return jsonify({"operations": active, "count": len(active)})

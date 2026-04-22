"""Production Flask app for Azure App Service deployment.

Serves the frontend + API endpoints for OneDrive provisioning and file uploads.
Designed for Azure App Service (Linux, B1+ plan) with gunicorn.

Start locally:  python app.py
Production:     gunicorn --bind 0.0.0.0:8000 --timeout 600 --workers 2 app:app
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_default_policy
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, request, jsonify, send_from_directory

# ── Add src to path ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from onedrive_provisioner.config import AppConfig, AzureConfig, UploadConfig, ExecutionConfig
from onedrive_provisioner.logging_setup import configure_logging
from onedrive_provisioner.orchestrator import Orchestrator
from onedrive_provisioner.auth import MsalTokenProvider
from onedrive_provisioner.graph import GraphClient
from onedrive_provisioner.onedrive import UserResolver
from onedrive_provisioner.onedrive import sp_delegated
from onedrive_provisioner.entra import EntraOrchestrator, EntraConfig
from onedrive_provisioner.entra import (
    TenantService, RbacService, DiscoveryService, CleanupService,
    remove_rbac_for_principals, downgrade_principals_to_reader, ROLE_IDS,
    run_preflight,
)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB upload limit

# ── In-memory job store ──
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 200

# ── In-memory device-code provisioning sessions ──
_prov_sessions: Dict[str, Dict[str, Any]] = {}
_prov_lock = threading.Lock()

# ── In-memory Entra provisioning sessions ──
_entra_sessions: Dict[str, Dict[str, Any]] = {}
_entra_lock = threading.Lock()
_MAX_ENTRA_SESSIONS = 100


# ────────────────────── Helpers ──────────────────────

def _build_config(tenant_id, client_id, client_secret, concurrency=8, dry_run=False):
    return AppConfig(
        azure=AzureConfig(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret),
        upload=UploadConfig(),
        execution=ExecutionConfig(concurrency=min(max(1, concurrency), 64), dry_run=dry_run),
    )


def _extract_creds(data: dict):
    t = (data.get("tenant_id") or "").strip()
    c = (data.get("client_id") or "").strip()
    s = (data.get("client_secret") or "").strip()
    if not t or not c or not s:
        return None
    return t, c, s


def _run_job(job_id, users, source_dir, destination, dry_run, concurrency,
             tenant_id, client_id, client_secret):
    try:
        cfg = _build_config(tenant_id, client_id, client_secret, concurrency, dry_run)
        configure_logging(cfg.log_level)
        orch = Orchestrator(cfg)

        partial_results: List = []

        def _on_user_done(user_result, done_count, total):
            partial_results.append(user_result)
            ok = sum(1 for r in partial_results if r.status.value == "success")
            fail = sum(1 for r in partial_results if r.status.value == "failed")
            with _jobs_lock:
                j = _jobs.get(job_id)
                if j:
                    j["completed_users"] = ok
                    j["failed_users"] = fail
                    j["processed"] = done_count
                    j["updated_at"] = datetime.now(timezone.utc).isoformat()
                    j["result"] = {
                        "total": total,
                        "succeeded": ok,
                        "failed": fail,
                        "skipped": done_count - ok - fail,
                        "results": [r.to_dict() for r in partial_results],
                    }

        loop = asyncio.new_event_loop()
        try:
            report = loop.run_until_complete(
                orch.bulk_setup(users, source_dir, destination or None,
                                on_user_done=_on_user_done)
            )
        finally:
            loop.close()

        with _jobs_lock:
            j = _jobs[job_id]
            j["status"] = "completed"
            j["completed_users"] = report.succeeded
            j["failed_users"] = report.failed
            j["result"] = report.to_dict()
            j["updated_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        with _jobs_lock:
            j = _jobs.get(job_id)
            if j:
                j["status"] = "failed"
                j["error"] = str(exc)
                j["updated_at"] = datetime.now(timezone.utc).isoformat()
    finally:
        shutil.rmtree(source_dir, ignore_errors=True)


# ────────────────────── Frontend (static files) ──────────────────────

@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(FRONTEND_DIR, path)


# ────────────────────── POST /api/jobs ──────────────────────

@app.route("/api/jobs", methods=["POST"])
def start_job():
    # Extract form fields
    tenant_id = (request.form.get("tenant_id") or "").strip()
    client_id = (request.form.get("client_id") or "").strip()
    client_secret = (request.form.get("client_secret") or "").strip()

    if not tenant_id or not client_id or not client_secret:
        return jsonify({"error": "Missing SPN credentials"}), 400

    raw_users = request.form.get("users", "")
    users = [u.strip() for u in raw_users.splitlines() if u.strip()]
    if not users:
        return jsonify({"error": "No users provided"}), 400
    if len(users) > 5000:
        return jsonify({"error": "Max 5000 users per job"}), 400

    destination = request.form.get("destination", "")
    dry_run = request.form.get("dry_run", "").lower() == "true"
    concurrency = int(request.form.get("concurrency", "8") or "8")

    # Save uploaded files to temp dir
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded"}), 400

    tmp_dir = tempfile.mkdtemp(prefix="onedrive_upload_")
    file_count = 0
    for f in files:
        rel_path = f.filename or ""
        if not rel_path:
            continue
        rel_path = rel_path.replace("\\", "/")
        parts = [p for p in rel_path.split("/") if p and p != ".."]
        if not parts:
            continue
        dest_path = Path(tmp_dir).joinpath(*parts)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        f.save(dest_path)
        file_count += 1

    if file_count == 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": "No valid files in upload"}), 400

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    job = {
        "id": job_id, "status": "running", "created_at": now,
        "updated_at": now, "total_users": len(users),
        "completed_users": 0, "failed_users": 0, "processed": 0,
        "file_count": file_count, "dry_run": dry_run,
        "destination": destination, "result": None, "error": None,
    }
    with _jobs_lock:
        if len(_jobs) >= _MAX_JOBS:
            oldest_key = min(_jobs, key=lambda k: _jobs[k]["created_at"])
            del _jobs[oldest_key]
        _jobs[job_id] = job

    t = threading.Thread(
        target=_run_job,
        args=(job_id, users, tmp_dir, destination, dry_run, concurrency,
              tenant_id, client_id, client_secret),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id, "status": "running",
                    "users": len(users), "files": file_count}), 202


# ────────────────────── GET /api/jobs ──────────────────────

@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    with _jobs_lock:
        summaries = [
            {k: v for k, v in j.items() if k != "result"}
            for j in sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
        ]
    return jsonify(summaries)


# ────────────────────── GET /api/jobs/<id> ──────────────────────

@app.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ────────────────────── POST /api/users ──────────────────────

@app.route("/api/users", methods=["POST"])
def list_users_api():
    body = request.get_json(silent=True) or {}
    creds = _extract_creds(body)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    tenant_id, client_id, client_secret = creds
    limit = min(max(1, int(body.get("limit", 200))), 999)

    try:
        azure_cfg = AzureConfig(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret,
        )
        tp = MsalTokenProvider(azure_cfg)

        async def _fetch():
            out = []
            async with GraphClient(tp, max_retries=4) as g:
                async for u in UserResolver(g).list_all_members():
                    out.append({
                        "upn": u.get("userPrincipalName"),
                        "id": u.get("id"),
                        "displayName": u.get("displayName"),
                    })
                    if len(out) >= limit:
                        break
            return out

        loop = asyncio.new_event_loop()
        try:
            users = loop.run_until_complete(_fetch())
        finally:
            loop.close()

        return jsonify(users)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── POST /api/licenses ──────────────────────

@app.route("/api/licenses", methods=["POST"])
def check_licenses():
    body = request.get_json(silent=True) or {}
    creds = _extract_creds(body)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    tenant_id, client_id, client_secret = creds

    user_list = body.get("users", [])
    if not user_list:
        return jsonify({"error": "No users provided"}), 400

    try:
        azure_cfg = AzureConfig(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret,
        )
        tp = MsalTokenProvider(azure_cfg)

        async def _check():
            results = []
            async with GraphClient(tp, max_retries=4) as g:
                for u in user_list[:200]:
                    u = u.strip()
                    if not u:
                        continue
                    try:
                        lic = await g.get(f"/users/{u}/licenseDetails")
                        plans = [entry.get("skuPartNumber", "") for entry in lic.get("value", [])]
                        has_onedrive = any(
                            "SHAREPOINTONLINE" in p or "M365" in p or "O365" in p
                            or "OFFICE365" in p or "SPE" in p or "ENTERPRISEPACK" in p
                            for p in plans
                        )
                        results.append({
                            "user": u, "licenses": plans,
                            "has_onedrive": has_onedrive, "error": None,
                        })
                    except Exception as e:
                        results.append({
                            "user": u, "licenses": [],
                            "has_onedrive": False, "error": str(e),
                        })
            return results

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(_check())
        finally:
            loop.close()

        return jsonify(results)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── POST /api/provision/start ──────────────────────
# Delegated-auth OneDrive bulk provisioning via device code flow.
# Used when uploads fail because users' OneDrives were never created.

@app.route("/api/provision/start", methods=["POST"])
def provision_start():
    """Initiate device-code flow. Returns user_code + verification_uri."""
    body = request.get_json(silent=True) or {}
    tenant_id = (body.get("tenant_id") or "").strip()
    emails = body.get("users") or []
    emails = [e.strip() for e in emails if e and e.strip()]

    if not tenant_id:
        return jsonify({"error": "Missing tenant_id"}), 400
    if not emails:
        return jsonify({"error": "No users provided"}), 400

    # Derive admin URL from first email's domain
    admin_url = sp_delegated.tenant_admin_url(emails[0])

    try:
        flow = sp_delegated.initiate_device_flow(tenant_id, admin_url)
    except Exception as exc:
        return jsonify({"error": f"Device flow init failed: {exc}"}), 500

    session_id = str(uuid.uuid4())
    with _prov_lock:
        _prov_sessions[session_id] = {
            "id": session_id,
            "status": "awaiting_login",
            "tenant_id": tenant_id,
            "admin_url": admin_url,
            "emails": emails,
            "flow": flow,  # contains _app reference
            "result": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    # Start polling thread (blocks on acquire_token_by_device_flow)
    def _run():
        try:
            token = sp_delegated.acquire_token_by_device_flow(flow)
            with _prov_lock:
                s = _prov_sessions.get(session_id)
                if s:
                    s["status"] = "provisioning"

            # Call the admin API with delegated token
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    sp_delegated.enqueue_personal_sites(admin_url, token, emails)
                )
            finally:
                loop.close()

            with _prov_lock:
                s = _prov_sessions.get(session_id)
                if s:
                    s["status"] = "completed" if result["ok"] else "failed"
                    s["result"] = result
                    # Clean up non-serializable flow handle
                    s.pop("flow", None)
        except Exception as exc:
            with _prov_lock:
                s = _prov_sessions.get(session_id)
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


# ────────────────────── GET /api/provision/<id> ──────────────────────

@app.route("/api/provision/<session_id>", methods=["GET"])
def provision_status(session_id):
    with _prov_lock:
        s = _prov_sessions.get(session_id)
        if not s:
            return jsonify({"error": "Session not found"}), 404
        # Return a copy without the non-serializable flow object
        return jsonify({k: v for k, v in s.items() if k != "flow"})


# ────────────────────── Entra ID user provisioning ──────────────────────

def _run_entra_provision(session_id: str, cfg_dict: dict,
                         tenant_id: str, client_id: str, client_secret: str):
    """Background worker that runs EntraOrchestrator and updates the session state."""
    def _set(**kw):
        with _entra_lock:
            s = _entra_sessions.get(session_id)
            if s:
                s.update(kw)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()

    partial: List[dict] = []

    def _on_user_done(result, done, total):
        partial.append(result.to_dict())
        with _entra_lock:
            s = _entra_sessions.get(session_id)
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

        azure = AzureConfig(tenant_id=tenant_id, client_id=client_id,
                            client_secret=client_secret)
        orch = EntraOrchestrator(azure, concurrency=int(cfg_dict.get("concurrency", 6)))
        report = asyncio.run(orch.provision(cfg, on_user_done=_on_user_done))

        _set(status="completed", result=report.to_dict(),
             processed=report.total_users, total=report.total_users)
    except Exception as exc:
        _set(status="failed", error=str(exc))


async def _async_preflight(t, c, s, *, cfg_dict, subscriptions):
    cfg = EntraConfig.from_dict(cfg_dict)
    azure = AzureConfig(tenant_id=t, client_id=c, client_secret=s)
    tp = MsalTokenProvider(azure)
    async with GraphClient(tp) as g:
        if subscriptions:
            async with RbacService(tp) as rbac:
                return await run_preflight(g, rbac, cfg, subscription_ids=subscriptions)
        return await run_preflight(g, None, cfg, subscription_ids=None)


@app.route("/api/preflight", methods=["POST"])
def preflight():
    """Run pre-flight validation before provisioning.

    Body: { tenant_id, client_id, client_secret, config: {...},
            subscriptions?: [string] }
    Returns: { overall: 'ok'|'warnings'|'blocked', checks: [...], totals: {...} }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
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


@app.route("/api/provision-users", methods=["POST"])
def provision_users_start():
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
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
    with _entra_lock:
        if len(_entra_sessions) >= _MAX_ENTRA_SESSIONS:
            oldest = min(_entra_sessions, key=lambda k: _entra_sessions[k]["created_at"])
            del _entra_sessions[oldest]
        _entra_sessions[session_id] = session

    threading.Thread(
        target=_run_entra_provision,
        args=(session_id, cfg_dict, tenant_id, client_id, client_secret),
        daemon=True,
    ).start()

    return jsonify({"session_id": session_id, "status": "running"}), 202


@app.route("/api/provision-users/<session_id>", methods=["GET"])
def provision_users_status(session_id):
    with _entra_lock:
        s = _entra_sessions.get(session_id)
        if not s:
            return jsonify({"error": "Session not found"}), 404
        return jsonify(s)


@app.route("/api/provision-users", methods=["GET"])
def provision_users_list():
    with _entra_lock:
        out = [
            {k: v for k, v in s.items() if k not in ("result", "partial_users")}
            for s in sorted(_entra_sessions.values(),
                            key=lambda s: s["created_at"], reverse=True)
        ]
    return jsonify(out)


# ────────────────────── Tenant info / Permissions / Discovery / Cleanup / Read-only ──────────────────────

def _make_token_provider(t, c, s):
    return MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))


async def _async_tenant_info(t, c, s):
    tp = _make_token_provider(t, c, s)
    async with GraphClient(tp) as g:
        ts = TenantService(g)
        domain, tap_max = await ts.get_tenant_info()
        # Also fetch subscribedSkus for license availability
        try:
            sku_data = await g.get("/subscribedSkus")
            skus = sku_data.get("value", [])
        except Exception:
            skus = []
        return domain, tap_max, skus


@app.route("/api/tenant-info", methods=["POST"])
def tenant_info():
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
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


async def _async_assign_permissions(t, c, s, *, subscriptions, principals, role):
    tp = _make_token_provider(t, c, s)
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


@app.route("/api/assign-permissions", methods=["POST"])
def assign_permissions():
    """Assign a role across one or more subscriptions to one or more principals.

    Body:
      { tenant_id, client_id, client_secret,
        subscriptions: ["sub1", "sub2", ...],
        principals: [{id, type:"Group"|"User", displayName?}, ...],
        role: "Owner" | "Contributor" | "Reader" }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    subs = data.get("subscriptions") or []
    principals = data.get("principals") or []
    role = data.get("role")
    if not subs or not principals or role not in ROLE_IDS:
        return jsonify({"error": "subscriptions[], principals[], role required"}), 400
    try:
        results = asyncio.run(_async_assign_permissions(
            *creds, subscriptions=subs, principals=principals, role=role,
        ))
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


async def _async_discover(t, c, s, prefix):
    tp = _make_token_provider(t, c, s)
    async with GraphClient(tp) as g:
        return await DiscoveryService(g).discover(prefix)


@app.route("/api/discover-hack", methods=["POST"])
def discover_hack():
    """Discover users + groups whose name starts with the prefix.

    Body: { tenant_id, client_id, client_secret, prefix }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    prefix = (data.get("prefix") or "").strip()
    if not prefix:
        return jsonify({"error": "prefix required"}), 400
    try:
        return jsonify(asyncio.run(_async_discover(*creds, prefix)))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


async def _async_cleanup(t, c, s, *, user_ids, group_ids, sub_ids, principal_ids):
    tp = _make_token_provider(t, c, s)
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


@app.route("/api/cleanup-hack", methods=["POST"])
def cleanup_hack():
    """Delete selected users + groups + RBAC assignments.

    Body: { tenant_id, client_id, client_secret,
            userIds: [], groupIds: [],
            subscriptionIds: [], principalIds: [] }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    try:
        return jsonify(asyncio.run(_async_cleanup(
            *creds,
            user_ids=data.get("userIds") or [],
            group_ids=data.get("groupIds") or [],
            sub_ids=data.get("subscriptionIds") or [],
            principal_ids=data.get("principalIds") or [],
        )))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


async def _async_readonly(t, c, s, *, subscriptions, principals):
    tp = _make_token_provider(t, c, s)
    async with RbacService(tp) as rbac:
        return await downgrade_principals_to_reader(rbac, subscriptions, principals)


@app.route("/api/readonly-mode", methods=["POST"])
def readonly_mode():
    """Strip Owner/Contributor and ensure Reader for given principals across subs.

    Body: { tenant_id, client_id, client_secret,
            subscriptions: [], principals: [{id, type, displayName?}] }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    subs = data.get("subscriptions") or []
    principals = data.get("principals") or []
    if not principals:
        return jsonify({"error": "principals[] required"}), 400
    try:
        results = asyncio.run(_async_readonly(*creds,
                                              subscriptions=subs, principals=principals))
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── Local dev ──────────────────────

if __name__ == "__main__":
    print("Starting local dev server at http://localhost:4280")
    app.run(host="0.0.0.0", port=4280, debug=True)

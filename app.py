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
from onedrive_provisioner.storage import HackStateManager
from onedrive_provisioner.storage.blob_client import BlobStateClient
from onedrive_provisioner.chatbot import ChatbotAgent
from onedrive_provisioner.chatbot.tool_executor import ToolExecutor
from onedrive_provisioner.docgen import DocGenerator
from onedrive_provisioner.scheduler import HackScheduler, ScheduledJob

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")

# ── Blob Storage state persistence ──
_state_mgr: HackStateManager | None = None

def _get_state_manager() -> HackStateManager | None:
    global _state_mgr
    if _state_mgr is not None:
        return _state_mgr
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conn_str:
        return None
    try:
        client = BlobStateClient("", connection_string=conn_str)
        _state_mgr = HackStateManager(client)
        return _state_mgr
    except Exception as exc:
        print(f"[WARN] Could not init blob state manager: {exc}")
        return None

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB upload limit

# ── In-memory job store ──
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 200

# ── In-memory device-code provisioning sessions ──
_prov_sessions: Dict[str, Dict[str, Any]] = {}
_prov_lock = threading.Lock()

# ── Scheduler singleton ──
_hack_scheduler: HackScheduler | None = None

def _get_scheduler() -> HackScheduler | None:
    global _hack_scheduler
    if _hack_scheduler is not None:
        return _hack_scheduler
    mgr = _get_state_manager()
    if not mgr:
        return None
    _hack_scheduler = HackScheduler(
        get_state_manager=_get_state_manager,
        run_provision=_scheduler_provision,
        run_cleanup=_scheduler_cleanup,
        run_readonly=_scheduler_readonly,
    )
    _hack_scheduler.start()
    return _hack_scheduler


def _scheduler_provision(cfg_dict: dict, tenant_id: str, client_id: str, client_secret: str):
    """Called by scheduler to provision a hack (runs in scheduler thread)."""
    configure_logging("INFO")
    cfg = EntraConfig.from_dict(cfg_dict)
    azure = AzureConfig(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    orch = EntraOrchestrator(azure, concurrency=int(cfg_dict.get("concurrency", 6)))
    report = asyncio.run(orch.provision(cfg))
    # Persist state
    mgr = _get_state_manager()
    if mgr:
        state = HackStateManager.build_state_from_report(cfg_dict, report.to_dict())
        mgr.save_state(cfg_dict.get("prefix", "unknown"), state)


def _scheduler_readonly(prefix: str, tenant_id: str, client_id: str, client_secret: str,
                        subscription_ids: list = None, mode: str = "team"):
    """Called by scheduler to switch a hack to read-only mode (runs in scheduler thread).

    Discovers principals by prefix and downgrades them to Reader role.
    """
    sub_ids = subscription_ids or []
    async def _do():
        tp = _make_token_provider(tenant_id, client_id, client_secret)
        async with GraphClient(tp) as g:
            discovered = await DiscoveryService(g).discover(prefix)
        users = discovered.get("users", [])
        groups = discovered.get("groups", [])
        principals = [{"id": u["id"], "type": "user", "displayName": u.get("displayName", "")} for u in users]
        principals += [{"id": gr["id"], "type": "group", "displayName": gr.get("displayName", "")} for gr in groups]
        if sub_ids and principals:
            async with RbacService(tp) as rbac:
                await downgrade_principals_to_reader(rbac, sub_ids, principals)
    asyncio.run(_do())


def _scheduler_cleanup(prefix: str, tenant_id: str, client_id: str, client_secret: str,
                       subscription_ids: list = None):
    """Called by scheduler to cleanup an expired hack (runs in scheduler thread).

    Deletes Entra ID users, groups, removes RBAC assignments from subscriptions,
    and deletes blob state.
    """
    sub_ids = subscription_ids or []
    async def _do():
        tp = _make_token_provider(tenant_id, client_id, client_secret)
        async with GraphClient(tp) as g:
            discovered = await DiscoveryService(g).discover(prefix)
        user_ids = [u["id"] for u in discovered.get("users", [])]
        group_ids = [gr["id"] for gr in discovered.get("groups", [])]
        principal_ids = user_ids + group_ids
        # Remove RBAC role assignments from Azure subscriptions
        if sub_ids and principal_ids:
            async with RbacService(tp) as rbac:
                await remove_rbac_for_principals(rbac, sub_ids, principal_ids)
        # Delete users and groups from Entra ID
        if user_ids or group_ids:
            async with GraphClient(tp) as g:
                cleaner = CleanupService(g)
                if user_ids:
                    await cleaner.delete_users(user_ids)
                if group_ids:
                    await cleaner.delete_groups(group_ids)
    asyncio.run(_do())
    # Delete blob state
    mgr = _get_state_manager()
    if mgr:
        mgr.delete_state(prefix)

# ── In-memory Entra provisioning sessions ──
_entra_sessions: Dict[str, Dict[str, Any]] = {}
_entra_lock = threading.Lock()
_MAX_ENTRA_SESSIONS = 100

# ── In-memory generated docs store ──
_generated_docs: Dict[str, Dict[str, Any]] = {}
_docs_lock = threading.Lock()
_MAX_DOCS = 50


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

        # Persist state to blob storage
        try:
            mgr = _get_state_manager()
            if mgr:
                state = HackStateManager.build_state_from_report(
                    cfg_dict, report.to_dict(), session_id=session_id)
                mgr.save_state(cfg_dict.get("prefix", "unknown"), state)
        except Exception as blob_exc:
            print(f"[WARN] Failed to save state to blob: {blob_exc}")
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
    """Delete selected users + groups + RBAC assignments, then remove blob state.

    Body: { tenant_id, client_id, client_secret,
            userIds: [], groupIds: [],
            subscriptionIds: [], principalIds: [],
            hackPrefix: "" }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    try:
        result = asyncio.run(_async_cleanup(
            *creds,
            user_ids=data.get("userIds") or [],
            group_ids=data.get("groupIds") or [],
            sub_ids=data.get("subscriptionIds") or [],
            principal_ids=data.get("principalIds") or [],
        ))
        # Delete hack state from blob storage if prefix provided
        prefix = (data.get("hackPrefix") or "").strip()
        if prefix:
            try:
                mgr = _get_state_manager()
                if mgr:
                    deleted = mgr.delete_state(prefix)
                    result["blob_state_deleted"] = deleted
                else:
                    result["blob_state_deleted"] = False
                    result["blob_state_note"] = "Storage not configured"
            except Exception as exc:
                result["blob_state_error"] = str(exc)
        return jsonify(result)
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


# ────────────────────── Hack State Management (Blob Storage) ──────────────────────

@app.route("/api/hack-state", methods=["GET"])
def list_hacks():
    """List all hacks with saved state."""
    mgr = _get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured (set AZURE_STORAGE_CONNECTION_STRING)"}), 503
    try:
        return jsonify(mgr.list_hacks())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/hack-state/<prefix>", methods=["GET"])
def get_hack_state(prefix):
    """Retrieve full state for a hack prefix."""
    mgr = _get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404
    return jsonify(state)


@app.route("/api/hack-state/<prefix>/versions", methods=["GET"])
def get_hack_versions(prefix):
    """List version history for a hack prefix."""
    mgr = _get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    versions = mgr.list_versions(prefix)
    return jsonify(versions)


@app.route("/api/hack-state/<prefix>/regenerate-tap", methods=["POST"])
def regenerate_tap(prefix):
    """Regenerate TAP for selected users in a hack.

    Body: { tenant_id, client_id, client_secret,
            users?: [upn1, upn2, ...],  // omit for all non-admin users
            tapLifetime?: 120 }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    mgr = _get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    target_upns = data.get("users")  # None = all
    tap_lifetime = int(data.get("tapLifetime", 120))

    try:
        results = asyncio.run(_async_regenerate_tap(
            *creds, state=state, target_upns=target_upns,
            tap_lifetime=tap_lifetime))
        updated_state = mgr.update_user_taps(prefix, results)
        return jsonify({"results": results, "updatedUsers": len(results)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


async def _async_regenerate_tap(t, c, s, *, state, target_upns, tap_lifetime):
    from onedrive_provisioner.entra.tap_service import TapService
    tp = _make_token_provider(t, c, s)
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


@app.route("/api/hack-state/<prefix>/assign-licenses", methods=["POST"])
def assign_licenses_to_hack(prefix):
    """Assign additional licenses to users in a hack.

    Body: { tenant_id, client_id, client_secret,
            licenses: ["SKU_PART_1", ...],
            users?: [upn1, ...] }  // omit for all non-admin users
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400

    mgr = _get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    licenses = data.get("licenses", [])
    if not licenses:
        return jsonify({"error": "licenses[] required"}), 400
    target_upns = data.get("users")

    try:
        results = asyncio.run(_async_assign_licenses(
            *creds, state=state, licenses=licenses, target_upns=target_upns))
        updated_state = mgr.update_user_licenses(prefix, results)
        return jsonify({"results": results, "updatedUsers": len(results)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


async def _async_assign_licenses(t, c, s, *, state, licenses, target_upns):
    from onedrive_provisioner.entra.license_service import LicenseService
    tp = _make_token_provider(t, c, s)
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
            if not uid or u.get("isAdmin"):
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


# ────────────────────── Scheduler ──────────────────────

@app.route("/api/hack-state/<prefix>/set-end-date", methods=["POST"])
def set_hack_end_date(prefix):
    """Set end date (and optional read-only date) for auto-lifecycle of a hack.

    Body: { tenant_id, client_id, client_secret, endDate: "2025-02-01T00:00:00Z",
            readonlyDate?: "2025-01-31T00:00:00Z", mode?: "team",
            subscriptionIds?: ["sub-id-1", ...] }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    end_date = (data.get("endDate") or "").strip()
    if not end_date:
        return jsonify({"error": "endDate is required (ISO datetime)"}), 400
    readonly_date = (data.get("readonlyDate") or "").strip() or None
    mode = data.get("mode") or "team"
    sub_ids = data.get("subscriptionIds") or []

    scheduler = _get_scheduler()
    if not scheduler:
        return jsonify({"error": "Storage not configured"}), 503

    try:
        t, c, s = creds
        jobs = scheduler.set_hack_end_date(prefix, end_date, {
            "tenant_id": t, "client_id": c, "client_secret": s,
        }, subscription_ids=sub_ids, readonly_date=readonly_date, mode=mode)
        return jsonify({"message": f"End date set for '{prefix}'",
                        "endDate": end_date,
                        "readonlyDate": readonly_date,
                        "subscriptionIds": sub_ids,
                        "jobs": [j.to_dict() for j in jobs]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/create-job", methods=["POST"])
def create_job():
    """Create a scheduled job (provision, readonly, or cleanup).

    Body: { tenant_id, client_id, client_secret,
            jobType: "provision"|"readonly"|"cleanup",
            scheduledAt: "2025-02-01T09:00:00Z",
            hackPrefix: "nyc-esri-gcc",
            subscriptionIds?: ["sub-id-1", ...],
            mode?: "team",
            config?: { ... }  // for provision jobs
          }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    job_type = (data.get("jobType") or "").strip()
    if job_type not in ("provision", "readonly", "cleanup"):
        return jsonify({"error": "jobType must be provision, readonly, or cleanup"}), 400
    scheduled_at = (data.get("scheduledAt") or "").strip()
    if not scheduled_at:
        return jsonify({"error": "scheduledAt is required (ISO datetime)"}), 400
    hack_prefix = (data.get("hackPrefix") or "").strip()
    if not hack_prefix:
        return jsonify({"error": "hackPrefix is required"}), 400

    scheduler = _get_scheduler()
    if not scheduler:
        return jsonify({"error": "Storage not configured"}), 503

    t, c, s = creds
    sub_ids = data.get("subscriptionIds") or []

    try:
        if job_type == "provision":
            config = data.get("config") or {}
            config["prefix"] = hack_prefix
            job = scheduler.schedule_provision(scheduled_at, config, {
                "tenant_id": t, "client_id": c, "client_secret": s,
            })
        else:
            cfg = {
                "tenant_id": t, "client_id": c, "client_secret": s,
                "subscription_ids": sub_ids,
            }
            if job_type == "readonly":
                cfg["mode"] = data.get("mode") or "team"
            job = ScheduledJob(
                id="",
                job_type=job_type,
                hack_prefix=hack_prefix,
                scheduled_at=scheduled_at,
                config=cfg,
            )
            job = scheduler.add_job(job)
        return jsonify({"message": f"{job_type} job scheduled", "job": job.to_dict()}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/scheduled-hacks", methods=["GET"])
def list_scheduled_hacks():
    """List all scheduled jobs (provision + cleanup)."""
    scheduler = _get_scheduler()
    if not scheduler:
        return jsonify({"error": "Storage not configured"}), 503
    status = request.args.get("status")
    jobs = scheduler.list_jobs(status=status)
    return jsonify([j.to_dict() for j in jobs])


@app.route("/api/schedule-hack", methods=["POST"])
def schedule_hack():
    """Schedule a hack to be provisioned at a future date.

    Body: { tenant_id, client_id, client_secret,
            scheduledAt: "2025-02-01T09:00:00Z",
            config: { prefix, hackName, domain, totalUsers, ... } }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    scheduled_at = (data.get("scheduledAt") or "").strip()
    if not scheduled_at:
        return jsonify({"error": "scheduledAt is required (ISO datetime)"}), 400
    config = data.get("config") or {}
    if not config.get("domain"):
        return jsonify({"error": "config.domain is required"}), 400

    scheduler = _get_scheduler()
    if not scheduler:
        return jsonify({"error": "Storage not configured"}), 503

    try:
        t, c, s = creds
        job = scheduler.schedule_provision(scheduled_at, config, {
            "tenant_id": t, "client_id": c, "client_secret": s,
        })
        return jsonify({"message": "Hack scheduled", "job": job.to_dict()}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/scheduled-hacks/<job_id>", methods=["DELETE"])
def cancel_scheduled_hack(job_id):
    """Cancel a pending scheduled job."""
    scheduler = _get_scheduler()
    if not scheduler:
        return jsonify({"error": "Storage not configured"}), 503
    if scheduler.cancel_job(job_id):
        return jsonify({"message": "Job cancelled", "id": job_id})
    return jsonify({"error": "Job not found or not pending"}), 404


@app.route("/api/scheduled-hacks/<job_id>/run", methods=["POST"])
def run_scheduled_hack_now(job_id):
    """Immediately execute a pending scheduled job."""
    scheduler = _get_scheduler()
    if not scheduler:
        return jsonify({"error": "Storage not configured"}), 503
    try:
        job = scheduler.run_job_now(job_id)
        return jsonify({"message": f"Job {job.status}", "job": job.to_dict()})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── Document Generation ──────────────────────

@app.route("/api/generate-doc", methods=["POST"])
def generate_doc():
    """Generate Admin/Trainer Guide for a hack.

    Body: { hackPrefix: "nyc-esri-gcc-" }
    Returns: binary .docx file download
    """
    data = request.get_json(silent=True) or {}
    prefix = (data.get("hackPrefix") or data.get("prefix") or "").strip()
    if not prefix:
        return jsonify({"error": "hackPrefix is required"}), 400

    mgr = _get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503

    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    try:
        gen = DocGenerator()
        doc_bytes = gen.generate(state)
        filename = gen.get_filename(state)

        from flask import Response
        return Response(
            doc_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/generated-docs/<doc_id>", methods=["GET"])
def download_generated_doc(doc_id):
    """Download a previously generated document by ID."""
    with _docs_lock:
        entry = _generated_docs.get(doc_id)
    if not entry:
        return jsonify({"error": "Document not found or expired"}), 404

    from flask import Response
    return Response(
        entry["data"],
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{entry["filename"]}"'},
    )


# ────────────────────── Chatbot ──────────────────────

def _get_chatbot_agent() -> ChatbotAgent | None:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    key = os.environ.get("AZURE_OPENAI_KEY", "")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    if not endpoint or not key:
        return None
    return ChatbotAgent(endpoint=endpoint, api_key=key, deployment=deployment)


# ────────────────────── Tenant setup: check & grant permissions ──────────────────────

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

# Extra permissions needed for file upload features
OPTIONAL_GRAPH_PERMISSIONS = [
    {"value": "Files.ReadWrite.All",  "reason": "Upload files to users' OneDrive (optional)"},
]

SELF_GRANT_PERMISSION = "AppRoleAssignment.ReadWrite.All"


async def _async_check_permissions(t, c, s):
    """Check which Graph app permissions the SPN currently has."""
    import httpx
    tp = _make_token_provider(t, c, s)
    tok = await tp.get_token()
    H = {"Authorization": f"Bearer {tok}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Find our service principal
        sp_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{c}'",
            headers=H,
        )
        sp_data = sp_resp.json().get("value", [])
        if not sp_data:
            raise ValueError(f"Service principal not found for client_id {c}")
        sp_id = sp_data[0]["id"]
        sp_display = sp_data[0].get("displayName", c)

        # Find Microsoft Graph service principal
        graph_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{GRAPH_APPID}'",
            headers=H,
        )
        graph_data = graph_resp.json().get("value", [])
        if not graph_data:
            raise ValueError("Microsoft Graph service principal not found in tenant")
        graph_sp_id = graph_data[0]["id"]
        roles_by_value = {r["value"]: r for r in graph_data[0].get("appRoles", [])}

        # Get existing app role assignments
        assignments_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp_id}/appRoleAssignments",
            headers=H,
        )
        existing = assignments_resp.json().get("value", [])
        existing_role_ids = {
            a["appRoleId"] for a in existing
            if a.get("resourceId") == graph_sp_id
        }

        # Check required permissions
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

        # Check if SPN can self-grant (has AppRoleAssignment.ReadWrite.All)
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
    """Grant specified Graph app permissions to the SPN."""
    import httpx
    tp = _make_token_provider(t, c, s)
    tok = await tp.get_token()
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Find our service principal
        sp_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{c}'",
            headers=H,
        )
        sp_data = sp_resp.json().get("value", [])
        if not sp_data:
            raise ValueError(f"Service principal not found for client_id {c}")
        sp_id = sp_data[0]["id"]

        # Find Microsoft Graph service principal and its roles
        graph_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{GRAPH_APPID}'",
            headers=H,
        )
        graph_data = graph_resp.json().get("value", [])
        if not graph_data:
            raise ValueError("Microsoft Graph service principal not found")
        graph_sp_id = graph_data[0]["id"]
        roles_by_value = {r["value"]: r for r in graph_data[0].get("appRoles", [])}

        # Existing assignments
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


@app.route("/api/check-permissions", methods=["POST"])
def check_permissions():
    """Check which Graph API permissions the SPN currently has.

    Body: { tenant_id, client_id, client_secret }
    Returns: { spnId, spnDisplayName, permissions: [...], canSelfGrant }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    try:
        result = asyncio.run(_async_check_permissions(*creds))
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/grant-permissions", methods=["POST"])
def grant_permissions():
    """Grant missing Graph API permissions to the SPN (requires AppRoleAssignment.ReadWrite.All).

    Body: { tenant_id, client_id, client_secret, permissions: ["User.ReadWrite.All", ...] }
    Returns: { results: [...] }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
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


@app.route("/api/chat", methods=["POST"])
def chat():
    """Chat with the AI assistant.

    Body: { tenant_id, client_id, client_secret, messages: [{role, content}, ...] }
    Returns: { reply, tools_called }
    """
    data = request.get_json(silent=True) or {}
    creds = _extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    messages = data.get("messages") or []
    if not messages:
        return jsonify({"error": "messages[] required"}), 400

    agent = _get_chatbot_agent()
    if not agent:
        return jsonify({"error": "Azure OpenAI not configured (set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY)"}), 503

    executor = ToolExecutor(
        creds=creds,
        get_state_manager=_get_state_manager,
        entra_sessions=_entra_sessions,
        entra_lock=_entra_lock,
        upload_jobs=_jobs,
        jobs_lock=_jobs_lock,
        docs_store=_generated_docs,
    )

    try:
        result = agent.chat(messages, tool_executor=executor)
        resp = {
            "reply": result["reply"],
            "tools_called": result["tools_called"],
        }
        if result.get("provision_data"):
            resp["provision_data"] = result["provision_data"]
        return jsonify(resp)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ────────────────────── Local dev ──────────────────────

if __name__ == "__main__":
    print("Starting local dev server at http://localhost:4280")
    app.run(host="0.0.0.0", port=4280, debug=True)

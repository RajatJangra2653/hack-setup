"""Azure Functions API for OneDrive Provisioner.

Endpoints:
  POST /api/jobs      — start a bulk provisioning + upload job (multipart form)
  GET  /api/jobs      — list recent jobs
  GET  /api/jobs/{id} — get job status / results
  POST /api/users     — list tenant users using provided SPN creds

Credentials are provided **per-request** from the browser (never stored).
Files are uploaded via multipart form data (folder picker in browser).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import azure.functions as func

# ── Add parent dir to path so we can import our package ──
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onedrive_provisioner.config import AppConfig, AzureConfig, UploadConfig, ExecutionConfig  # noqa: E402
from onedrive_provisioner.logging_setup import configure_logging  # noqa: E402
from onedrive_provisioner.orchestrator import Orchestrator  # noqa: E402
from onedrive_provisioner.auth import MsalTokenProvider  # noqa: E402
from onedrive_provisioner.graph import GraphClient  # noqa: E402
from onedrive_provisioner.onedrive import UserResolver  # noqa: E402

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ── In-memory job store ──
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 200


def _json_response(body: Any, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(body, indent=2, default=str),
        status_code=status_code,
        mimetype="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _build_config(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    concurrency: int = 8,
    dry_run: bool = False,
) -> AppConfig:
    """Build an AppConfig from per-request SPN credentials."""
    return AppConfig(
        azure=AzureConfig(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
        ),
        upload=UploadConfig(),
        execution=ExecutionConfig(
            concurrency=min(max(1, concurrency), 64),
            dry_run=dry_run,
        ),
    )


def _extract_creds(params: dict) -> tuple[str, str, str]:
    """Extract and validate SPN creds from request params."""
    t = (params.get("tenant_id") or "").strip()
    c = (params.get("client_id") or "").strip()
    s = (params.get("client_secret") or "").strip()
    if not t or not c or not s:
        raise ValueError("Missing required SPN credentials (tenant_id, client_id, client_secret)")
    return t, c, s


# ────────────────────── POST /api/jobs (multipart form) ──────────────────────
@app.route(route="jobs", methods=["POST"])
def start_job(req: func.HttpRequest) -> func.HttpResponse:
    """Start a bulk provisioning + upload job.

    Multipart form fields:
      tenant_id, client_id, client_secret  — SPN credentials
      users           — newline-separated UPNs / object IDs
      destination     — OneDrive destination folder (optional)
      dry_run         — "true" for plan-only (optional)
      concurrency     — parallel workers (optional, default 8)
      files           — one or more file parts (webkitRelativePath preserved)
    """
    try:
        # ── Parse form fields ──
        params: dict = {}
        for key in ("tenant_id", "client_id", "client_secret", "users",
                     "destination", "dry_run", "concurrency"):
            val = req.form.get(key)
            if val:
                params[key] = val

        try:
            tenant_id, client_id, client_secret = _extract_creds(params)
        except ValueError as e:
            return _json_response({"error": str(e)}, 400)

        raw_users = params.get("users", "")
        users = [u.strip() for u in raw_users.splitlines() if u.strip()]
        if not users:
            return _json_response({"error": "No users provided"}, 400)
        if len(users) > 5000:
            return _json_response({"error": "Max 5000 users per job"}, 400)

        destination = params.get("destination", "")
        dry_run = params.get("dry_run", "").lower() == "true"
        concurrency = int(params.get("concurrency", "8") or "8")

        # ── Save uploaded files to temp dir ──
        files = req.files.getlist("files")
        if not files:
            return _json_response({"error": "No files uploaded"}, 400)

        tmp_dir = tempfile.mkdtemp(prefix="onedrive_upload_")
        file_count = 0
        for f in files:
            # webkitRelativePath comes from the browser (folder/sub/file.txt)
            rel_path = f.filename or ""
            if not rel_path:
                continue
            # Security: prevent path traversal
            rel_path = rel_path.replace("\\", "/")
            parts = [p for p in rel_path.split("/") if p and p != ".."]
            if not parts:
                continue
            dest_path = Path(tmp_dir).joinpath(*parts)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(f.read())
            file_count += 1

        if file_count == 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return _json_response({"error": "No valid files in upload"}, 400)

        # ── Create job record ──
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        job: Dict[str, Any] = {
            "id": job_id,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "total_users": len(users),
            "completed_users": 0,
            "failed_users": 0,
            "processed": 0,
            "file_count": file_count,
            "dry_run": dry_run,
            "destination": destination,
            "result": None,
            "error": None,
        }
        with _jobs_lock:
            if len(_jobs) >= _MAX_JOBS:
                oldest_key = min(_jobs, key=lambda k: _jobs[k].get("created_at", ""))
                del _jobs[oldest_key]
            _jobs[job_id] = job

        # ── Run in background thread ──
        t = threading.Thread(
            target=_run_job_sync,
            args=(job_id, users, tmp_dir, destination, dry_run, concurrency,
                  tenant_id, client_id, client_secret),
            daemon=True,
        )
        t.start()

        return _json_response({
            "job_id": job_id, "status": "running",
            "users": len(users), "files": file_count,
        }, 202)

    except Exception as exc:
        return _json_response({"error": f"Unexpected error: {exc}"}, 500)


def _run_job_sync(
    job_id: str,
    users: List[str],
    source_dir: str,
    destination: str,
    dry_run: bool,
    concurrency: int,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> None:
    """Background worker — runs the async orchestrator, then cleans up temp files."""
    try:
        cfg = _build_config(tenant_id, client_id, client_secret, concurrency, dry_run)
        configure_logging(cfg.log_level)
        orch = Orchestrator(cfg)

        # Partial results collected as each user completes
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
        # Always clean up temp files
        shutil.rmtree(source_dir, ignore_errors=True)


# ─────────────────────────────── GET /api/jobs ───────────────────────────────
@app.route(route="jobs", methods=["GET"])
def list_jobs(req: func.HttpRequest) -> func.HttpResponse:
    with _jobs_lock:
        summaries = [
            {k: v for k, v in j.items() if k != "result"}
            for j in sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
        ]
    return _json_response(summaries)


# ─────────────────────────────── GET /api/jobs/{id} ──────────────────────────
@app.route(route="jobs/{job_id}", methods=["GET"])
def get_job(req: func.HttpRequest) -> func.HttpResponse:
    job_id = req.route_params.get("job_id", "")
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return _json_response({"error": "Job not found"}, 404)
    return _json_response(job)


# ────────────────────── POST /api/users (with SPN creds) ─────────────────────
@app.route(route="users", methods=["POST"])
def list_users_api(req: func.HttpRequest) -> func.HttpResponse:
    """List enabled member users, authenticated with provided SPN creds."""
    try:
        body = req.get_json()
    except ValueError:
        return _json_response({"error": "Invalid JSON"}, 400)

    try:
        tenant_id, client_id, client_secret = _extract_creds(body)
    except ValueError as e:
        return _json_response({"error": str(e)}, 400)

    limit = int(body.get("limit", 200))
    limit = min(max(1, limit), 999)

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

        return _json_response(users)

    except Exception as exc:
        return _json_response({"error": str(exc)}, 500)

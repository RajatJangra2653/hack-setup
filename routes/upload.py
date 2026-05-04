"""OneDrive file upload job routes."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from flask import Blueprint, request, jsonify

from onedrive_provisioner.logging_setup import configure_logging
from onedrive_provisioner.orchestrator import Orchestrator

from ._state import (
    build_config, extract_creds,
    jobs, jobs_lock, MAX_JOBS,
)

bp = Blueprint("upload", __name__)


def _run_job(job_id, users, source_dir, destination, dry_run, concurrency,
             tenant_id, client_id, client_secret):
    try:
        cfg = build_config(tenant_id, client_id, client_secret, concurrency, dry_run)
        configure_logging(cfg.log_level)
        orch = Orchestrator(cfg)

        partial_results: List = []

        def _on_user_done(user_result, done_count, total):
            partial_results.append(user_result)
            ok = sum(1 for r in partial_results if r.status.value == "success")
            fail = sum(1 for r in partial_results if r.status.value == "failed")
            skip = sum(1 for r in partial_results if r.status.value in ("skipped", "dry_run"))
            with jobs_lock:
                j = jobs.get(job_id)
                if j:
                    j["completed_users"] = ok
                    j["failed_users"] = fail
                    j["processed"] = done_count
                    j["updated_at"] = datetime.now(timezone.utc).isoformat()
                    j["result"] = {
                        "total": total,
                        "succeeded": ok,
                        "failed": fail,
                        "skipped": skip,
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

        with jobs_lock:
            j = jobs[job_id]
            j["status"] = "completed"
            j["completed_users"] = report.succeeded
            j["failed_users"] = report.failed
            j["result"] = report.to_dict()
            j["updated_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        with jobs_lock:
            j = jobs.get(job_id)
            if j:
                j["status"] = "failed"
                j["error"] = str(exc)
                j["updated_at"] = datetime.now(timezone.utc).isoformat()
    finally:
        shutil.rmtree(source_dir, ignore_errors=True)


@bp.route("/api/jobs", methods=["POST"])
def start_job():
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
    with jobs_lock:
        if len(jobs) >= MAX_JOBS:
            oldest_key = min(jobs, key=lambda k: jobs[k]["created_at"])
            del jobs[oldest_key]
        jobs[job_id] = job

    t = threading.Thread(
        target=_run_job,
        args=(job_id, users, tmp_dir, destination, dry_run, concurrency,
              tenant_id, client_id, client_secret),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id, "status": "running",
                    "users": len(users), "files": file_count}), 202


@bp.route("/api/jobs", methods=["GET"])
def list_jobs():
    with jobs_lock:
        summaries = [
            {k: v for k, v in j.items() if k != "result"}
            for j in sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)
        ]
    return jsonify(summaries)


@bp.route("/api/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@bp.route("/api/users", methods=["POST"])
def list_users_api():
    body = request.get_json(silent=True) or {}
    creds = extract_creds(body)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    tenant_id, client_id, client_secret = creds
    limit = min(max(1, int(body.get("limit", 200))), 999)

    try:
        from onedrive_provisioner.config import AzureConfig
        from onedrive_provisioner.auth import MsalTokenProvider
        from onedrive_provisioner.graph import GraphClient
        from onedrive_provisioner.onedrive import UserResolver

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


@bp.route("/api/licenses", methods=["POST"])
def check_licenses():
    body = request.get_json(silent=True) or {}
    creds = extract_creds(body)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    tenant_id, client_id, client_secret = creds

    user_list = body.get("users", [])
    if not user_list:
        return jsonify({"error": "No users provided"}), 400

    try:
        from onedrive_provisioner.config import AzureConfig
        from onedrive_provisioner.auth import MsalTokenProvider
        from onedrive_provisioner.graph import GraphClient

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

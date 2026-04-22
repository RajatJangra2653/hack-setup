"""Local dev server that serves frontend + proxies /api to Azure Functions logic.

Usage:  python dev_server.py
Opens at: http://localhost:4280
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from http.server import HTTPServer, SimpleHTTPRequestHandler
import io

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

# ── In-memory job store ──
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 200

# ── In-memory provisioning sessions ──
_prov_sessions: Dict[str, Dict[str, Any]] = {}
_prov_lock = threading.Lock()

# ── In-memory Entra provisioning sessions ──
_entra_sessions: Dict[str, Dict[str, Any]] = {}
_entra_lock = threading.Lock()
_MAX_ENTRA_SESSIONS = 100


def _run_entra_provision(session_id, cfg_dict, tenant_id, client_id, client_secret):
    def _set(**kw):
        with _entra_lock:
            s = _entra_sessions.get(session_id)
            if s:
                s.update(kw)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()
    partial = []
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
            raise ValueError("'domain' is required")
        azure = AzureConfig(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
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

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")


def _build_config(tenant_id, client_id, client_secret, concurrency=8, dry_run=False):
    return AppConfig(
        azure=AzureConfig(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret),
        upload=UploadConfig(),
        execution=ExecutionConfig(concurrency=min(max(1, concurrency), 64), dry_run=dry_run),
    )


def _run_job(job_id, users, source_dir, destination, dry_run, concurrency,
             tenant_id, client_id, client_secret):
    try:
        cfg = _build_config(tenant_id, client_id, client_secret, concurrency, dry_run)
        configure_logging(cfg.log_level)
        orch = Orchestrator(cfg)

        # Partial results collected as each user completes
        partial_results = []

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
                    # Store partial results so UI can show per-user status live
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


class DevHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND_DIR, **kwargs)

    def do_GET(self):
        if self.path == "/api/jobs":
            self._handle_list_jobs()
        elif self.path.startswith("/api/jobs/"):
            self._handle_get_job(self.path.split("/api/jobs/")[1])
        elif self.path.startswith("/api/provision/"):
            self._handle_provision_status(self.path.split("/api/provision/")[1])
        elif self.path == "/api/provision-users":
            self._handle_entra_list()
        elif self.path.startswith("/api/provision-users/"):
            self._handle_entra_status(self.path.split("/api/provision-users/")[1])
        elif self.path == "/api/hack-state":
            self._handle_hack_list()
        elif self.path.startswith("/api/hack-state/") and self.path.endswith("/versions"):
            prefix = self.path.replace("/api/hack-state/", "").replace("/versions", "")
            self._handle_hack_versions(prefix)
        elif self.path.startswith("/api/hack-state/"):
            prefix = self.path.replace("/api/hack-state/", "")
            self._handle_hack_get(prefix)
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/jobs":
            self._handle_start_job()
        elif self.path == "/api/users":
            self._handle_list_users()
        elif self.path == "/api/licenses":
            self._handle_check_licenses()
        elif self.path == "/api/provision/start":
            self._handle_provision_start()
        elif self.path == "/api/provision-users":
            self._handle_entra_start()
        elif self.path == "/api/tenant-info":
            self._handle_tenant_info()
        elif self.path == "/api/assign-permissions":
            self._handle_assign_permissions()
        elif self.path == "/api/discover-hack":
            self._handle_discover_hack()
        elif self.path == "/api/cleanup-hack":
            self._handle_cleanup_hack()
        elif self.path == "/api/readonly-mode":
            self._handle_readonly_mode()
        elif self.path == "/api/preflight":
            self._handle_preflight()
        elif self.path.startswith("/api/hack-state/") and self.path.endswith("/regenerate-tap"):
            prefix = self.path.replace("/api/hack-state/", "").replace("/regenerate-tap", "")
            self._handle_hack_regenerate_tap(prefix)
        elif self.path.startswith("/api/hack-state/") and self.path.endswith("/assign-licenses"):
            prefix = self.path.replace("/api/hack-state/", "").replace("/assign-licenses", "")
            self._handle_hack_assign_licenses(prefix)
        else:
            self._send_json({"error": "Not found"}, 404)

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _handle_start_job(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart/form-data"}, 400)
            return

        # Parse multipart using email.parser (cgi.FieldStorage is broken in 3.12+)
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Build an RFC 2822 message that email.parser can handle
        from email.parser import BytesParser
        from email.policy import default as email_default_policy

        header_bytes = f"Content-Type: {content_type}\r\n\r\n".encode()
        msg = BytesParser(policy=email_default_policy).parsebytes(header_bytes + body)

        fields = {}   # name -> str value
        files = []    # list of (filename, bytes)

        if msg.is_multipart():
            for part in msg.iter_parts():
                cd = part.get("Content-Disposition", "")
                # Extract name and filename from Content-Disposition
                import re as _re
                name_match = _re.search(r'name="([^"]*)"', cd)
                fname_match = _re.search(r'filename="([^"]*)"', cd)
                if not name_match:
                    continue
                name = name_match.group(1)
                payload = part.get_payload(decode=True) or b""
                if fname_match:
                    fname = fname_match.group(1)
                    if fname:
                        files.append((fname, payload))
                else:
                    fields[name] = payload.decode("utf-8", errors="replace")

        tenant_id = fields.get("tenant_id", "").strip()
        client_id = fields.get("client_id", "").strip()
        client_secret = fields.get("client_secret", "").strip()

        if not tenant_id or not client_id or not client_secret:
            self._send_json({"error": "Missing SPN credentials"}, 400)
            return

        raw_users = fields.get("users", "")
        users = [u.strip() for u in raw_users.splitlines() if u.strip()]
        if not users:
            self._send_json({"error": "No users provided"}, 400)
            return

        destination = fields.get("destination", "")
        dry_run = fields.get("dry_run", "").lower() == "true"
        concurrency = int(fields.get("concurrency", "8") or "8")

        # Save uploaded files to temp dir
        tmp_dir = tempfile.mkdtemp(prefix="onedrive_upload_")
        file_count = 0

        for fname, fdata in files:
            rel_path = fname.replace("\\", "/")
            parts = [p for p in rel_path.split("/") if p and p != ".."]
            if not parts:
                continue
            dest_path = Path(tmp_dir).joinpath(*parts)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(fdata)
            file_count += 1

        if file_count == 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            self._send_json({"error": "No valid files"}, 400)
            return

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

        self._send_json({"job_id": job_id, "status": "running",
                         "users": len(users), "files": file_count}, 202)

    def _handle_list_jobs(self):
        with _jobs_lock:
            summaries = [
                {k: v for k, v in j.items() if k != "result"}
                for j in sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
            ]
        self._send_json(summaries)

    def _handle_get_job(self, job_id):
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job:
            self._send_json({"error": "Job not found"}, 404)
            return
        self._send_json(job)

    def _handle_list_users(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        tenant_id = (body.get("tenant_id") or "").strip()
        client_id = (body.get("client_id") or "").strip()
        client_secret = (body.get("client_secret") or "").strip()
        if not tenant_id or not client_id or not client_secret:
            self._send_json({"error": "Missing SPN credentials"}, 400)
            return

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

            self._send_json(users)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_check_licenses(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        tenant_id = (body.get("tenant_id") or "").strip()
        client_id = (body.get("client_id") or "").strip()
        client_secret = (body.get("client_secret") or "").strip()
        if not tenant_id or not client_id or not client_secret:
            self._send_json({"error": "Missing SPN credentials"}, 400)
            return

        user_list = body.get("users", [])
        if not user_list:
            self._send_json({"error": "No users provided"}, 400)
            return

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
                                "user": u,
                                "licenses": plans,
                                "has_onedrive": has_onedrive,
                                "error": None,
                            })
                        except Exception as e:
                            results.append({
                                "user": u,
                                "licenses": [],
                                "has_onedrive": False,
                                "error": str(e),
                            })
                return results

            loop = asyncio.new_event_loop()
            try:
                results = loop.run_until_complete(_check())
            finally:
                loop.close()

            self._send_json(results)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_provision_start(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        tenant_id = (body.get("tenant_id") or "").strip()
        emails = [e.strip() for e in (body.get("users") or []) if e and e.strip()]
        if not tenant_id:
            self._send_json({"error": "Missing tenant_id"}, 400)
            return
        if not emails:
            self._send_json({"error": "No users provided"}, 400)
            return

        admin_url = sp_delegated.tenant_admin_url(emails[0])

        try:
            flow = sp_delegated.initiate_device_flow(tenant_id, admin_url)
        except Exception as exc:
            self._send_json({"error": f"Device flow init failed: {exc}"}, 500)
            return

        session_id = str(uuid.uuid4())
        with _prov_lock:
            _prov_sessions[session_id] = {
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
                with _prov_lock:
                    s = _prov_sessions.get(session_id)
                    if s:
                        s["status"] = "provisioning"
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
                        s.pop("flow", None)
            except Exception as exc:
                with _prov_lock:
                    s = _prov_sessions.get(session_id)
                    if s:
                        s["status"] = "failed"
                        s["error"] = str(exc)
                        s.pop("flow", None)

        threading.Thread(target=_run, daemon=True).start()

        self._send_json({
            "session_id": session_id,
            "user_code": flow["user_code"],
            "verification_uri": flow["verification_uri"],
            "expires_in": flow.get("expires_in", 900),
            "message": flow.get("message"),
            "user_count": len(emails),
        })

    def _handle_provision_status(self, session_id):
        with _prov_lock:
            s = _prov_sessions.get(session_id)
            if not s:
                self._send_json({"error": "Session not found"}, 404)
                return
            self._send_json({k: v for k, v in s.items() if k != "flow"})

    # ── Entra provisioning ──
    def _handle_entra_start(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            self._send_json({"error": "Invalid JSON body"}, 400)
            return
        t = (data.get("tenant_id") or "").strip()
        c = (data.get("client_id") or "").strip()
        s = (data.get("client_secret") or "").strip()
        if not t or not c or not s:
            self._send_json({"error": "Missing SPN credentials"}, 400)
            return
        cfg_dict = data.get("config") or {}
        if not isinstance(cfg_dict, dict) or not cfg_dict.get("domain"):
            self._send_json({"error": "config.domain is required"}, 400)
            return

        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with _entra_lock:
            if len(_entra_sessions) >= _MAX_ENTRA_SESSIONS:
                oldest = min(_entra_sessions, key=lambda k: _entra_sessions[k]["created_at"])
                del _entra_sessions[oldest]
            _entra_sessions[session_id] = {
                "id": session_id, "status": "running",
                "created_at": now, "updated_at": now,
                "config": {k: v for k, v in cfg_dict.items() if k != "initialPassword"},
                "processed": 0, "total": 0, "partial_users": [],
                "result": None, "error": None,
            }
        threading.Thread(
            target=_run_entra_provision,
            args=(session_id, cfg_dict, t, c, s),
            daemon=True,
        ).start()
        self._send_json({"session_id": session_id, "status": "running"}, 202)

    def _handle_entra_status(self, session_id):
        with _entra_lock:
            s = _entra_sessions.get(session_id)
            if not s:
                self._send_json({"error": "Session not found"}, 404)
                return
            self._send_json(s)

    def _handle_entra_list(self):
        with _entra_lock:
            out = [
                {k: v for k, v in s.items() if k not in ("result", "partial_users")}
                for s in sorted(_entra_sessions.values(),
                                key=lambda s: s["created_at"], reverse=True)
            ]
        self._send_json(out)

    # ── Tenant info / Permissions / Discovery / Cleanup / Read-only ──

    # ── Hack State Management (Blob Storage) ──
    def _handle_hack_list(self):
        mgr = _get_state_manager()
        if not mgr:
            self._send_json({"error": "Storage not configured (set AZURE_STORAGE_CONNECTION_STRING)"}, 503); return
        try:
            self._send_json(mgr.list_hacks())
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_hack_get(self, prefix):
        mgr = _get_state_manager()
        if not mgr:
            self._send_json({"error": "Storage not configured"}, 503); return
        state = mgr.get_state(prefix)
        if not state:
            self._send_json({"error": f"No state found for prefix '{prefix}'"}, 404); return
        self._send_json(state)

    def _handle_hack_versions(self, prefix):
        mgr = _get_state_manager()
        if not mgr:
            self._send_json({"error": "Storage not configured"}, 503); return
        self._send_json(mgr.list_versions(prefix))

    def _handle_hack_regenerate_tap(self, prefix):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        mgr = _get_state_manager()
        if not mgr:
            self._send_json({"error": "Storage not configured"}, 503); return
        state = mgr.get_state(prefix)
        if not state:
            self._send_json({"error": f"No state found for prefix '{prefix}'"}, 404); return
        target_upns = data.get("users")
        tap_lifetime = int(data.get("tapLifetime", 120))
        t, c, s = creds
        try:
            from onedrive_provisioner.entra.tap_service import TapService
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            async def _run():
                results = []
                async with GraphClient(tp) as g:
                    tap_svc = TapService(g, lifetime_minutes=tap_lifetime)
                    for u in state.get("users", []):
                        upn = u.get("userPrincipalName", "")
                        uid = u.get("userId", "")
                        if not uid: continue
                        if target_upns and upn not in target_upns: continue
                        tap = await tap_svc.issue(uid)
                        results.append({
                            "userPrincipalName": upn,
                            "tap": tap.get("temporaryAccessPass", "") if tap else "",
                            "tapExpires": tap.get("startDateTime", "") if tap else "",
                            "status": "ok" if tap else "failed",
                        })
                return results
            results = asyncio.run(_run())
            mgr.update_user_taps(prefix, results)
            self._send_json({"results": results, "updatedUsers": len(results)})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_hack_assign_licenses(self, prefix):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        mgr = _get_state_manager()
        if not mgr:
            self._send_json({"error": "Storage not configured"}, 503); return
        state = mgr.get_state(prefix)
        if not state:
            self._send_json({"error": f"No state found for prefix '{prefix}'"}, 404); return
        licenses = data.get("licenses", [])
        if not licenses:
            self._send_json({"error": "licenses[] required"}, 400); return
        target_upns = data.get("users")
        t, c, s = creds
        try:
            from onedrive_provisioner.entra.license_service import LicenseService
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            async def _run():
                results = []
                async with GraphClient(tp) as g:
                    lic_svc = LicenseService(g)
                    sku_map = await lic_svc.resolve(licenses)
                    sku_ids = [sid for (sid, _) in sku_map.values()]
                    if not sku_ids: return results
                    for u in state.get("users", []):
                        upn = u.get("userPrincipalName", "")
                        uid = u.get("userId", "")
                        if not uid or u.get("isAdmin"): continue
                        if target_upns and upn not in target_upns: continue
                        assigned = await lic_svc.assign(uid, sku_ids)
                        existing = u.get("licenses", [])
                        merged = list(set(existing + licenses))
                        results.append({
                            "userPrincipalName": upn, "licenses": merged,
                            "status": "ok" if assigned else "failed",
                        })
                return results
            results = asyncio.run(_run())
            mgr.update_user_licenses(prefix, results)
            self._send_json({"results": results, "updatedUsers": len(results)})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)
    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            return None

    def _creds(self, data):
        if not isinstance(data, dict):
            return None
        t = (data.get("tenant_id") or "").strip()
        c = (data.get("client_id") or "").strip()
        s = (data.get("client_secret") or "").strip()
        return (t, c, s) if t and c and s else None

    def _handle_tenant_info(self):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        t, c, s = creds
        try:
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            async def _run():
                async with GraphClient(tp) as g:
                    ts = TenantService(g)
                    domain, tap_max = await ts.get_tenant_info()
                    try:
                        sku_data = await g.get("/subscribedSkus")
                        skus = sku_data.get("value", [])
                    except Exception:
                        skus = []
                    return domain, tap_max, skus
            domain, tap_max, skus = asyncio.run(_run())
            sku_summary = [
                {
                    "skuPartNumber": sk.get("skuPartNumber", ""),
                    "skuId": sk.get("skuId", ""),
                    "consumedUnits": sk.get("consumedUnits", 0),
                    "prepaidUnits": sk.get("prepaidUnits", {}),
                }
                for sk in skus
            ]
            self._send_json({
                "domain": domain,
                "tapMaxLifetimeMinutes": tap_max,
                "subscribedSkus": sku_summary,
            })
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_assign_permissions(self):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        subs = data.get("subscriptions") or []
        principals = data.get("principals") or []
        role = data.get("role")
        if not subs or not principals or role not in ROLE_IDS:
            self._send_json({"error": "subscriptions[], principals[], role required"}, 400); return
        t, c, s = creds
        try:
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            async def _run():
                out = []
                async with RbacService(tp) as rbac:
                    for sub in subs:
                        for p in principals:
                            try:
                                a = await rbac.assign_role(
                                    sub, p["id"], role,
                                    principal_type=p.get("type", "Group"))
                                out.append({"subscription": sub, "principalId": p["id"],
                                            "displayName": p.get("displayName"),
                                            "role": role, "status": "assigned",
                                            "assignmentId": a.get("id")})
                            except Exception as exc:
                                out.append({"subscription": sub, "principalId": p["id"],
                                            "displayName": p.get("displayName"),
                                            "role": role, "status": "failed",
                                            "error": str(exc)})
                return out
            self._send_json({"results": asyncio.run(_run())})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_discover_hack(self):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        prefix = (data.get("prefix") or "").strip()
        if not prefix:
            self._send_json({"error": "prefix required"}, 400); return
        t, c, s = creds
        try:
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            async def _run():
                async with GraphClient(tp) as g:
                    return await DiscoveryService(g).discover(prefix)
            self._send_json(asyncio.run(_run()))
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_cleanup_hack(self):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        t, c, s = creds
        user_ids = data.get("userIds") or []
        group_ids = data.get("groupIds") or []
        sub_ids = data.get("subscriptionIds") or []
        principal_ids = data.get("principalIds") or []
        try:
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            async def _run():
                out = {"users": [], "groups": [], "rbac": []}
                if sub_ids and principal_ids:
                    async with RbacService(tp) as rbac:
                        out["rbac"] = await remove_rbac_for_principals(
                            rbac, sub_ids, principal_ids)
                async with GraphClient(tp) as g:
                    cleaner = CleanupService(g)
                    if user_ids:
                        out["users"] = await cleaner.delete_users(user_ids)
                    if group_ids:
                        out["groups"] = await cleaner.delete_groups(group_ids)
                return out
            self._send_json(asyncio.run(_run()))
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_readonly_mode(self):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        subs = data.get("subscriptions") or []
        principals = data.get("principals") or []
        if not principals:
            self._send_json({"error": "principals[] required"}, 400); return
        t, c, s = creds
        try:
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            async def _run():
                async with RbacService(tp) as rbac:
                    return await downgrade_principals_to_reader(rbac, subs, principals)
            self._send_json({"results": asyncio.run(_run())})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_preflight(self):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        cfg_dict = data.get("config") or {}
        if not isinstance(cfg_dict, dict):
            self._send_json({"error": "'config' must be an object"}, 400); return
        subs = data.get("subscriptions") or []
        t, c, s = creds
        try:
            cfg = EntraConfig.from_dict(cfg_dict)
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            async def _run():
                async with GraphClient(tp) as g:
                    if subs:
                        async with RbacService(tp) as rbac:
                            return await run_preflight(g, rbac, cfg, subscription_ids=subs)
                    return await run_preflight(g, None, cfg, subscription_ids=None)
            self._send_json(asyncio.run(_run()))
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def log_message(self, format, *args):
        msg = format % args
        if "/api/" in msg:
            print(f"[API] {msg}")


if __name__ == "__main__":
    port = 4280
    server = HTTPServer(("0.0.0.0", port), DevHandler)
    print(f"Dev server running at http://localhost:{port}")
    print(f"Frontend: {FRONTEND_DIR}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()

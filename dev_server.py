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

from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import io

# ── Load .env (best-effort — only if python-dotenv is installed) ──
try:
    from dotenv import load_dotenv  # type: ignore
    _env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.isfile(_env_path):
        load_dotenv(_env_path, override=False)
        print(f"[ENV] Loaded {_env_path}")
except ImportError:
    pass

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
from onedrive_provisioner.entra.rbac_service import subscription_from_assignment
from onedrive_provisioner.storage import HackStateManager
from onedrive_provisioner.storage.blob_client import BlobStateClient
from onedrive_provisioner.chatbot import ChatbotAgent
from onedrive_provisioner.chatbot.tool_executor import ToolExecutor
from onedrive_provisioner.docgen import DocGenerator
from onedrive_provisioner.scheduler import HackScheduler, ScheduledJob
from onedrive_provisioner.security import DEFAULT_CONFIRMATION_STORE, OperationConfirmationError
from onedrive_provisioner.security.scheduler_credentials import make_scheduler_credential_config

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


def _is_archived_state(state: dict) -> bool:
    return bool(state.get("isArchived") or state.get("archivedAt") or state.get("lifecycleStatus") == "archived")

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

# ── In-memory GitHub-enable sessions ──
_github_sessions: Dict[str, Dict[str, Any]] = {}
_github_lock = threading.Lock()
_MAX_GITHUB_SESSIONS = 100

# ── In-memory generated docs store ──
_generated_docs: Dict[str, Dict[str, Any]] = {}
_docs_lock = threading.Lock()
_MAX_DOCS = 50

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
    tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
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
            (sub.get("subscriptionId") or "").lower(): sub
            for sub in visible_subs
            if sub.get("subscriptionId")
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


def _scheduler_provision(cfg_dict, tenant_id, client_id, client_secret):
    configure_logging("INFO")
    cfg = EntraConfig.from_dict(cfg_dict)
    azure = AzureConfig(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    orch = EntraOrchestrator(azure, concurrency=int(cfg_dict.get("concurrency", 6)))
    report = asyncio.run(orch.provision(cfg))
    mgr = _get_state_manager()
    if mgr:
        state = HackStateManager.build_state_from_report(cfg_dict, report.to_dict())
        mgr.save_state(cfg_dict.get("prefix", "unknown"), state)


def _scheduler_readonly(prefix, tenant_id, client_id, client_secret, subscription_ids=None, mode="team"):
    sub_ids = subscription_ids or []
    async def _do():
        tp = MsalTokenProvider(AzureConfig(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret))
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


def _scheduler_cleanup(prefix, tenant_id, client_id, client_secret, subscription_ids=None):
    sub_ids = subscription_ids or []
    async def _do():
        tp = MsalTokenProvider(AzureConfig(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret))
        async with GraphClient(tp) as g:
            discovered = await DiscoveryService(g).discover(prefix)
        users = discovered.get("users", [])
        user_ids = [u["id"] for u in users]
        group_ids = [gr["id"] for gr in discovered.get("groups", [])]
        principal_ids = user_ids + group_ids
        # Remove from GitHub EMU groups BEFORE deleting Entra users
        user_emails = [u.get("userPrincipalName", "") for u in users if u.get("userPrincipalName")]
        if user_emails:
            try:
                from onedrive_provisioner.github_emu import GitHubEnabler
                async with GitHubEnabler() as gh:
                    await gh.disable_users(user_emails, with_copilot=True, trigger_sync=True)
            except Exception:
                pass  # GitHub cleanup is best-effort during scheduled cleanup
        # Remove RBAC role assignments from Azure subscriptions
        if sub_ids and principal_ids:
            async with RbacService(tp) as rbac:
                await remove_rbac_for_principals(rbac, sub_ids, principal_ids)
        # Delete users and groups
        if user_ids or group_ids:
            async with GraphClient(tp) as g:
                cleaner = CleanupService(g)
                if user_ids:
                    await cleaner.delete_users(user_ids)
                if group_ids:
                    await cleaner.delete_groups(group_ids)
    asyncio.run(_do())
    mgr = _get_state_manager()
    if mgr:
        mgr.archive_state(prefix, reason="scheduled_cleanup")


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


def _run_github_enable(session_id, emails, with_copilot, with_ghas,
                       use_legacy, group_override, trigger_sync):
    """Background worker for GitHub EMU enablement."""
    from onedrive_provisioner.github_emu import GitHubEnabler

    def _set(**kw):
        with _github_lock:
            s = _github_sessions.get(session_id)
            if s:
                s.update(kw)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()

    partial = []

    def _on_done(result, done, total):
        partial.append(result.to_dict())
        with _github_lock:
            s = _github_sessions.get(session_id)
            if s:
                s["processed"] = done
                s["total"] = total
                s["partial_results"] = list(partial)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()

    async def _go():
        async with GitHubEnabler() as gh:
            return await gh.enable_users(
                emails,
                with_copilot=with_copilot,
                with_ghas=with_ghas,
                use_legacy=use_legacy,
                group_id_override=group_override,
                trigger_sync=trigger_sync,
                progress_cb=_on_done,
            )

    try:
        configure_logging("INFO")
        report = asyncio.run(_go())
        _set(status="completed", result=report.to_dict(),
             processed=report.total, total=report.total)
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
        elif self.path.startswith("/api/github-enable/"):
            self._handle_github_status(self.path.split("/api/github-enable/")[1])
        elif self.path == "/api/hack-state":
            self._handle_hack_list()
        elif self.path == "/api/hack-state/archive":
            self._handle_hack_archive_list()
        elif self.path.startswith("/api/hack-state/") and self.path.endswith("/versions"):
            prefix = self.path.replace("/api/hack-state/", "").replace("/versions", "")
            self._handle_hack_versions(prefix)
        elif self.path.startswith("/api/hack-state/"):
            prefix = self.path.replace("/api/hack-state/", "")
            self._handle_hack_get(prefix)
        elif self.path == "/api/scheduled-hacks" or self.path.startswith("/api/scheduled-hacks?"):
            self._handle_scheduled_hacks_list()
        elif self.path.startswith("/api/generated-docs/"):
            doc_id = self.path.split("/api/generated-docs/")[1]
            self._handle_download_doc(doc_id)
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
        elif self.path == "/api/github-enable":
            self._handle_github_start()
        elif self.path == "/api/github-disable":
            self._handle_github_disable()
        elif self.path == "/api/tenant-info":
            self._handle_tenant_info()
        elif self.path == "/api/subscriptions":
            self._handle_list_subscriptions()
        elif self.path == "/api/assign-permissions":
            self._handle_assign_permissions()
        elif self.path == "/api/discover-hack":
            self._handle_discover_hack()
        elif self.path == "/api/readonly-preview":
            self._handle_readonly_preview()
        elif self.path == "/api/cleanup-hack":
            self._handle_cleanup_hack()
        elif self.path == "/api/readonly-mode":
            self._handle_readonly_mode()
        elif self.path == "/api/preflight":
            self._handle_preflight()
        elif self.path.startswith("/api/hack-state/") and self.path.endswith("/regenerate-tap"):
            prefix = self.path.replace("/api/hack-state/", "").replace("/regenerate-tap", "")
            self._handle_hack_regenerate_tap(prefix)
        elif self.path.startswith("/api/hack-state/") and self.path.endswith("/reset-password"):
            prefix = self.path.replace("/api/hack-state/", "").replace("/reset-password", "")
            self._handle_hack_reset_password(prefix)
        elif self.path.startswith("/api/hack-state/") and self.path.endswith("/assign-licenses"):
            prefix = self.path.replace("/api/hack-state/", "").replace("/assign-licenses", "")
            self._handle_hack_assign_licenses(prefix)
        elif self.path.startswith("/api/hack-state/") and self.path.endswith("/repair-groups"):
            prefix = self.path.replace("/api/hack-state/", "").replace("/repair-groups", "")
            self._handle_hack_repair_groups(prefix)
        elif self.path.startswith("/api/hack-state/") and self.path.endswith("/repair-licenses"):
            prefix = self.path.replace("/api/hack-state/", "").replace("/repair-licenses", "")
            self._handle_hack_repair_licenses(prefix)
        elif self.path.startswith("/api/hack-state/") and self.path.endswith("/report"):
            prefix = self.path.replace("/api/hack-state/", "").replace("/report", "")
            self._handle_hack_report(prefix)
        elif self.path.startswith("/api/hack-state/") and self.path.endswith("/set-end-date"):
            prefix = self.path.replace("/api/hack-state/", "").replace("/set-end-date", "")
            self._handle_set_end_date(prefix)
        elif self.path == "/api/schedule-hack":
            self._handle_schedule_hack()
        elif self.path == "/api/create-job":
            self._handle_create_job()
        elif self.path == "/api/chat":
            self._handle_chat()
        elif self.path == "/api/generate-doc":
            self._handle_generate_doc()
        elif self.path == "/api/check-permissions":
            self._handle_check_permissions()
        elif self.path == "/api/grant-permissions":
            self._handle_grant_permissions()
        elif self.path.startswith("/api/scheduled-hacks/") and self.path.endswith("/run"):
            job_id = self.path.replace("/api/scheduled-hacks/", "").replace("/run", "")
            self._handle_run_job_now(job_id)
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        if self.path.startswith("/api/scheduled-hacks/"):
            job_id = self.path.split("/api/scheduled-hacks/")[1]
            self._handle_cancel_scheduled_hack(job_id)
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

    # ── GitHub EMU enablement ──
    def _handle_github_start(self):
        import re as _re
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            self._send_json({"error": "Invalid JSON body"}, 400)
            return
        raw_emails = data.get("emails") or []
        if isinstance(raw_emails, str):
            raw_emails = [e for e in _re.split(r"[\s,;]+", raw_emails) if e]
        emails = [str(e).strip() for e in raw_emails if str(e).strip()]
        if not emails:
            self._send_json({"error": "Provide at least one email in 'emails'"}, 400)
            return
        with_copilot = bool(data.get("withCopilot", False))
        with_ghas = bool(data.get("withGhas", False))
        use_legacy = bool(data.get("useLegacyGroups", False))
        group_override = (data.get("groupIdOverride") or "").strip() or None
        trigger_sync = bool(data.get("triggerSync", True))

        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with _github_lock:
            if len(_github_sessions) >= _MAX_GITHUB_SESSIONS:
                oldest = min(_github_sessions, key=lambda k: _github_sessions[k]["created_at"])
                del _github_sessions[oldest]
            _github_sessions[session_id] = {
                "id": session_id, "status": "running",
                "created_at": now, "updated_at": now,
                "total": len(emails), "processed": 0,
                "withCopilot": with_copilot, "withGhas": with_ghas,
                "useLegacyGroups": use_legacy, "groupIdOverride": group_override,
                "partial_results": [], "result": None, "error": None,
            }
        threading.Thread(
            target=_run_github_enable,
            args=(session_id, emails, with_copilot, with_ghas,
                  use_legacy, group_override, trigger_sync),
            daemon=True,
        ).start()
        self._send_json({"session_id": session_id, "status": "running"}, 202)

    def _handle_github_status(self, session_id):
        with _github_lock:
            s = _github_sessions.get(session_id)
            if not s:
                self._send_json({"error": "Session not found"}, 404)
                return
            self._send_json(s)

    def _handle_github_disable(self):
        import re as _re
        data = self._read_json()
        raw_emails = data.get("emails") or []
        if isinstance(raw_emails, str):
            raw_emails = [e for e in _re.split(r"[\s,;]+", raw_emails) if e]
        emails = [str(e).strip() for e in raw_emails if str(e).strip()]
        if not emails:
            self._send_json({"error": "Provide at least one email in 'emails'"}, 400)
            return
        with_copilot = bool(data.get("withCopilot", False))
        with_ghas = bool(data.get("withGhas", False))
        use_legacy = bool(data.get("useLegacyGroups", False))
        trigger_sync = bool(data.get("triggerSync", True))
        try:
            from onedrive_provisioner.github_emu import GitHubEnabler
            async def _go():
                async with GitHubEnabler() as gh:
                    return await gh.disable_users(
                        emails,
                        with_copilot=with_copilot,
                        with_ghas=with_ghas,
                        use_legacy=use_legacy,
                        trigger_sync=trigger_sync,
                    )
            result = asyncio.run(_go())
            self._send_json(result)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

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

    def _handle_hack_archive_list(self):
        mgr = _get_state_manager()
        if not mgr:
            self._send_json({"error": "Storage not configured (set AZURE_STORAGE_CONNECTION_STRING)"}, 503); return
        try:
            self._send_json(mgr.list_archived_hacks())
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
        if _is_archived_state(state):
            self._send_json({"error": "Archived hacks are report-only. Use the Report tab to generate historical reports."}, 409); return
        target_upns = data.get("users")
        tap_lifetime = int(data.get("tapLifetime", 120))
        target_count = sum(
            1 for user in state.get("users", [])
            if user.get("userId") and (not target_upns or user.get("userPrincipalName") in target_upns)
        )
        if self._confirmation_required("regenerate_tap", {
            "prefix": state.get("prefix") or prefix,
            "resourceCount": target_count,
            "targetUserCount": target_count,
            "subscriptionCount": 0,
        }, data):
            return
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

    def _handle_hack_reset_password(self, prefix):
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
        if _is_archived_state(state):
            self._send_json({"error": "Archived hacks are report-only."}, 409); return
        target_upns = data.get("users")
        custom_password = data.get("password")
        target_count = sum(
            1 for user in state.get("users", [])
            if user.get("userId") and (not target_upns or user.get("userPrincipalName") in target_upns)
        )
        if self._confirmation_required("reset_password", {
            "prefix": state.get("prefix") or prefix,
            "resourceCount": target_count,
            "targetUserCount": target_count,
            "subscriptionCount": 0,
        }, data):
            return
        t, c, s = creds
        try:
            from onedrive_provisioner.entra.user_service import UserService
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            async def _run():
                results = []
                async with GraphClient(tp) as g:
                    user_svc = UserService(g)
                    for u in state.get("users", []):
                        upn = u.get("userPrincipalName", "")
                        uid = u.get("userId", "")
                        if not uid: continue
                        if target_upns and upn not in target_upns: continue
                        new_pw = await user_svc.reset_password(uid, password=custom_password)
                        results.append({
                            "userPrincipalName": upn,
                            "password": new_pw or "",
                            "status": "ok" if new_pw else "failed",
                        })
                return results
            results = asyncio.run(_run())
            mgr.update_user_passwords(prefix, results)
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
        if _is_archived_state(state):
            self._send_json({"error": "Archived hacks are report-only. Use the Report tab to generate historical reports."}, 409); return
        licenses = data.get("licenses", [])
        if not licenses:
            self._send_json({"error": "licenses[] required"}, 400); return
        target_upns = data.get("users")
        include_admins = bool(data.get("includeAdmins", False))
        target_count = sum(
            1 for user in state.get("users", [])
            if user.get("userId")
            and (include_admins or not user.get("isAdmin"))
            and (not target_upns or user.get("userPrincipalName") in target_upns)
        )
        if self._confirmation_required("assign_licenses", {
            "prefix": state.get("prefix") or prefix,
            "resourceCount": target_count,
            "targetUserCount": target_count,
            "licenseCount": len(licenses),
            "subscriptionCount": 0,
        }, data):
            return
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
                        if not uid or (u.get("isAdmin") and not include_admins): continue
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

    def _handle_hack_repair_groups(self, prefix):
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
        if _is_archived_state(state):
            self._send_json({"error": "Archived hacks are report-only."}, 409); return
        t, c, s = creds
        try:
            from onedrive_provisioner.entra.group_service import GroupService
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            async def _run():
                results = []
                config = state.get("config", {})
                hack_prefix = config.get("prefix") or state.get("prefix", "")
                async with GraphClient(tp) as g:
                    group_svc = GroupService(g, hack_name=state.get("hackName", ""))
                    # Resolve group names to IDs
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
                    group_map = {}
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
                            expected_groups.add(f"{hack_prefix.rstrip('-')}-admins")
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
            results = asyncio.run(_run())
            mgr.update_user_groups(prefix, results)
            repaired = sum(1 for r in results if r.get("repaired"))
            self._send_json({"results": results, "repairedUsers": repaired, "totalChecked": len(results)})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_hack_repair_licenses(self, prefix):
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
        if _is_archived_state(state):
            self._send_json({"error": "Archived hacks are report-only."}, 409); return
        t, c, s = creds
        try:
            from onedrive_provisioner.entra.license_service import LicenseService
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            async def _run():
                results = []
                config = state.get("config", {})
                expected_licenses = config.get("licenses", [])
                assign_to_admins = config.get("assignLicensesToAdmins", False)
                async with GraphClient(tp) as g:
                    lic_svc = LicenseService(g)
                    sku_map = await lic_svc.resolve(expected_licenses)
                    expected_sku_ids = [sid for (sid, _) in sku_map.values()]
                    sku_to_friendly = {sid: friendly for friendly, (sid, _) in sku_map.items()}
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
            results = asyncio.run(_run())
            mgr.update_user_licenses(prefix, results)
            repaired = sum(1 for r in results if r.get("status") == "repaired")
            self._send_json({"results": results, "repairedUsers": repaired, "totalChecked": len(results)})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_hack_report(self, prefix):
        from onedrive_provisioner.hack_report import build_hack_report

        data = self._read_json() or {}
        mgr = _get_state_manager()
        if not mgr:
            self._send_json({"error": "Storage not configured"}, 503); return
        state = mgr.get_state(prefix)
        if not state:
            self._send_json({"error": f"No state found for prefix '{prefix}'"}, 404); return

        subscription_costs = data.get("subscriptionCosts") or []
        fetched_costs = []
        if data.get("fetchSubscriptionCosts"):
            creds = self._creds(data)
            if not creds:
                self._send_json({"error": "SPN credentials required to fetch Azure costs"}, 400); return
            subscription_ids = data.get("subscriptionIds") or [
                sc.get("subscriptionId") or sc.get("subscription") or sc.get("id")
                for sc in subscription_costs if isinstance(sc, dict)
            ]
            subscription_ids = [str(s).strip() for s in subscription_ids if str(s or "").strip()]
            if not subscription_ids:
                self._send_json({"error": "subscriptionIds[] required to fetch Azure costs"}, 400); return
            default_start_date, default_end_date = _state_report_date_range(state)
            start_date = data.get("startDate") or default_start_date
            end_date = data.get("endDate") or default_end_date
            t, c, s = creds
            try:
                from onedrive_provisioner.entra.cost_service import CostManagementService
                tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))

                async def _run():
                    async with CostManagementService(tp) as cost_svc:
                        return await cost_svc.query_subscription_costs(
                            subscription_ids,
                            start_date=start_date,
                            end_date=end_date,
                        )

                fetched_costs = asyncio.run(_run())
                merged = {}
                for item in subscription_costs:
                    if not isinstance(item, dict):
                        continue
                    sub_id = (item.get("subscriptionId") or item.get("subscription") or item.get("id") or "").strip()
                    if sub_id:
                        merged[sub_id] = dict(item)
                        merged[sub_id]["subscriptionId"] = sub_id
                for item in fetched_costs:
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
                subscription_costs = list(merged.values())
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500); return

        report = build_hack_report(
            state,
            subscription_costs=subscription_costs,
            license_unit_costs=data.get("licenseUnitCosts") or {},
            currency=data.get("currency") or "USD",
        )
        if fetched_costs:
            report["costFetch"] = {"subscriptionsQueried": len(fetched_costs)}
        self._send_json(report)

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

    def _operator(self, data):
        return (
            self.headers.get("X-Operator")
            or self.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
            or (data or {}).get("operator")
            or (data or {}).get("createdBy")
            or "unknown"
        )

    def _confirmation_required(self, operation, expected, data):
        try:
            DEFAULT_CONFIRMATION_STORE.validate(
                operation,
                expected,
                (data or {}).get("confirmation"),
                operator=self._operator(data),
            )
            return False
        except OperationConfirmationError:
            challenge = DEFAULT_CONFIRMATION_STORE.create(
                operation,
                expected,
                operator=self._operator(data),
            )
            self._send_json(challenge, 409)
            return True

    def _scheduler_creds_dict(self, creds, data):
        t, c, s = creds
        return {
            "tenant_id": t,
            "client_id": c,
            "client_secret": s,
            "client_secret_ref": (
                (data or {}).get("client_secret_ref")
                or (data or {}).get("credentialRef")
                or (data or {}).get("schedulerClientSecretRef")
            ),
        }

    def _handle_set_end_date(self, prefix):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        end_date = (data.get("endDate") or "").strip()
        if not end_date:
            self._send_json({"error": "endDate is required (ISO datetime)"}, 400); return
        readonly_date = (data.get("readonlyDate") or "").strip() or None
        mode = data.get("mode") or "team"
        sub_ids = data.get("subscriptionIds") or []
        lifecycle_metadata = {
            "hackStartDate": (data.get("hackStartDate") or "").strip(),
            "hackDate": (data.get("hackDate") or "").strip(),
            "deleteDate": (data.get("deleteDate") or end_date).strip(),
        }
        scheduler = _get_scheduler()
        if not scheduler:
            self._send_json({"error": "Storage not configured"}, 503); return
        mgr = _get_state_manager()
        state = mgr.get_state(prefix) if mgr else None
        if state and _is_archived_state(state):
            self._send_json({"error": "Archived hacks are report-only. Schedule changes are disabled."}, 409); return
        try:
            jobs = scheduler.set_hack_end_date(
                prefix,
                end_date,
                self._scheduler_creds_dict(creds, data),
                subscription_ids=sub_ids,
                readonly_date=readonly_date,
                mode=mode,
                metadata=lifecycle_metadata,
            )
            self._send_json({"message": f"End date set for '{prefix}'",
                             "endDate": end_date,
                             "deleteDate": lifecycle_metadata["deleteDate"],
                             "hackStartDate": lifecycle_metadata["hackStartDate"],
                             "hackDate": lifecycle_metadata["hackDate"],
                             "readonlyDate": readonly_date,
                             "subscriptionIds": sub_ids,
                             "jobs": [j.to_dict() for j in jobs]})
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_scheduled_hacks_list(self):
        scheduler = _get_scheduler()
        if not scheduler:
            self._send_json({"error": "Storage not configured"}, 503); return
        # Parse query string for status filter
        status = None
        if "?" in self.path:
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            status = qs.get("status", [None])[0]
        jobs = scheduler.list_jobs(status=status)
        self._send_json([j.to_dict() for j in jobs])

    def _handle_schedule_hack(self):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        scheduled_at = (data.get("scheduledAt") or "").strip()
        if not scheduled_at:
            self._send_json({"error": "scheduledAt is required (ISO datetime)"}, 400); return
        config = data.get("config") or {}
        if not config.get("domain"):
            self._send_json({"error": "config.domain is required"}, 400); return
        scheduler = _get_scheduler()
        if not scheduler:
            self._send_json({"error": "Storage not configured"}, 503); return
        try:
            job = scheduler.schedule_provision(scheduled_at, config, self._scheduler_creds_dict(creds, data))
            self._send_json({"message": "Hack scheduled", "job": job.to_dict()}, 201)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_cancel_scheduled_hack(self, job_id):
        scheduler = _get_scheduler()
        if not scheduler:
            self._send_json({"error": "Storage not configured"}, 503); return
        if scheduler.cancel_job(job_id):
            self._send_json({"message": "Job cancelled", "id": job_id})
        else:
            self._send_json({"error": "Job not found or not pending"}, 404)

    def _handle_run_job_now(self, job_id):
        scheduler = _get_scheduler()
        if not scheduler:
            self._send_json({"error": "Storage not configured"}, 503); return
        try:
            job = scheduler.run_job_now(job_id)
            self._send_json({"message": f"Job {job.status}", "job": job.to_dict()})
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_create_job(self):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        job_type = (data.get("jobType") or "").strip()
        if job_type not in ("provision", "readonly", "cleanup"):
            self._send_json({"error": "jobType must be provision, readonly, or cleanup"}, 400); return
        scheduled_at = (data.get("scheduledAt") or "").strip()
        if not scheduled_at:
            self._send_json({"error": "scheduledAt is required (ISO datetime)"}, 400); return
        hack_prefix = (data.get("hackPrefix") or "").strip()
        if not hack_prefix:
            self._send_json({"error": "hackPrefix is required"}, 400); return
        scheduler = _get_scheduler()
        if not scheduler:
            self._send_json({"error": "Storage not configured"}, 503); return
        sub_ids = data.get("subscriptionIds") or []
        try:
            if job_type == "provision":
                config = data.get("config") or {}
                config["prefix"] = hack_prefix
                job = scheduler.schedule_provision(scheduled_at, config, self._scheduler_creds_dict(creds, data))
            else:
                cfg = {
                    **make_scheduler_credential_config(self._scheduler_creds_dict(creds, data)),
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
            self._send_json({"message": f"{job_type} job scheduled", "job": job.to_dict()}, 201)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

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
        hack_prefix = (data.get("hackPrefix") or data.get("prefix") or "").strip()
        if not hack_prefix:
            self._send_json({"error": "hackPrefix is required for privileged RBAC assignment confirmation"}, 400); return
        if self._confirmation_required("assign_permissions", {
            "prefix": hack_prefix,
            "role": role,
            "resourceCount": len(principals),
            "principalCount": len(principals),
            "subscriptionCount": len(subs),
        }, data):
            return
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

    def _handle_list_subscriptions(self):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        t, c, s = creds
        try:
            tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
            query = (data.get("query") or "").strip().lower()

            async def _run():
                async with RbacService(tp) as rbac:
                    return await rbac.list_subscriptions()

            subscriptions = asyncio.run(_run())
            if query:
                subscriptions = [
                    sub for sub in subscriptions
                    if query in (sub.get("subscriptionId") or "").lower()
                    or query in (sub.get("displayName") or "").lower()
                ]
            subscriptions = sorted(
                subscriptions,
                key=lambda sub: ((sub.get("displayName") or "").lower(), sub.get("subscriptionId") or ""),
            )
            self._send_json({"subscriptions": subscriptions, "count": len(subscriptions)})
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

    def _handle_readonly_preview(self):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        prefix = (data.get("prefix") or data.get("hackPrefix") or "").strip()
        if not prefix:
            self._send_json({"error": "prefix required"}, 400); return
        mode = (data.get("mode") or "team").strip().lower()
        if mode not in {"team", "flat"}:
            self._send_json({"error": "mode must be team or flat"}, 400); return
        try:
            self._send_json(asyncio.run(_async_readonly_preview(
                *creds,
                prefix=prefix,
                mode=mode,
                subscriptions=data.get("subscriptions") or [],
            )))
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
        prefix = (data.get("hackPrefix") or "").strip()
        if not prefix:
            self._send_json({"error": "hackPrefix is required for cleanup confirmation"}, 400); return
        if self._confirmation_required("cleanup_hack", {
            "prefix": prefix,
            "resourceCount": len(user_ids) + len(group_ids),
            "userCount": len(user_ids),
            "groupCount": len(group_ids),
            "principalCount": len(principal_ids),
            "subscriptionCount": len(sub_ids),
        }, data):
            return
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
            result = asyncio.run(_run())
            # Archive hack state from blob storage if prefix provided. Archived
            # state remains available for later reporting.
            if prefix:
                try:
                    mgr = _get_state_manager()
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
            self._send_json(result)
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
        hack_prefix = (data.get("hackPrefix") or data.get("prefix") or "").strip()
        if not hack_prefix:
            self._send_json({"error": "hackPrefix is required for read-only confirmation"}, 400); return
        if self._confirmation_required("readonly_mode", {
            "prefix": hack_prefix,
            "resourceCount": len(principals),
            "principalCount": len(principals),
            "subscriptionCount": len(subs),
        }, data):
            return
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

    def _handle_chat(self):
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        messages = (data or {}).get("messages") or []
        if not messages:
            self._send_json({"error": "messages[] required"}, 400); return

        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        key = os.environ.get("AZURE_OPENAI_KEY", "")
        deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        if not endpoint or not key:
            self._send_json({"error": "Azure OpenAI not configured (set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY)"}, 503); return

        agent = ChatbotAgent(endpoint=endpoint, api_key=key, deployment=deployment)
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
            self._send_json(resp)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_generate_doc(self):
        data = self._read_json()
        prefix = ((data or {}).get("hackPrefix") or (data or {}).get("prefix") or "").strip()
        if not prefix:
            self._send_json({"error": "hackPrefix is required"}, 400); return
        mgr = _get_state_manager()
        if not mgr:
            self._send_json({"error": "Storage not configured"}, 503); return
        state = mgr.get_state(prefix)
        if not state:
            self._send_json({"error": f"No state found for prefix '{prefix}'"}, 404); return
        try:
            gen = DocGenerator()
            doc_bytes = gen.generate(state)
            filename = gen.get_filename(state)
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(doc_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(doc_bytes)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_download_doc(self, doc_id):
        with _docs_lock:
            entry = _generated_docs.get(doc_id)
        if not entry:
            self._send_json({"error": "Document not found or expired"}, 404); return
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.send_header("Content-Disposition", f'attachment; filename="{entry["filename"]}"')
        self.send_header("Content-Length", str(len(entry["data"])))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(entry["data"])

    # ── Tenant setup: check & grant permissions ──

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

    def _handle_check_permissions(self):
        import httpx
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        t, c, s = creds
        try:
            async def _run():
                tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
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
                        f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{self.GRAPH_APPID}'",
                        headers=H,
                    )
                    graph_data = graph_resp.json().get("value", [])
                    if not graph_data:
                        raise ValueError("Microsoft Graph SP not found in tenant")
                    graph_sp_id = graph_data[0]["id"]
                    roles_by_value = {r["value"]: r for r in graph_data[0].get("appRoles", [])}

                    assignments_resp = await client.get(
                        f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp_id}/appRoleAssignments",
                        headers=H,
                    )
                    existing = assignments_resp.json().get("value", [])
                    existing_role_ids = {a["appRoleId"] for a in existing if a.get("resourceId") == graph_sp_id}

                    results = []
                    all_perms = self.REQUIRED_GRAPH_PERMISSIONS + self.OPTIONAL_GRAPH_PERMISSIONS
                    for perm in all_perms:
                        role = roles_by_value.get(perm["value"])
                        granted = role["id"] in existing_role_ids if role else False
                        is_optional = perm in self.OPTIONAL_GRAPH_PERMISSIONS
                        results.append({
                            "permission": perm["value"],
                            "reason": perm["reason"],
                            "granted": granted,
                            "optional": is_optional,
                        })

                    self_grant_role = roles_by_value.get(self.SELF_GRANT_PERMISSION)
                    can_self_grant = self_grant_role["id"] in existing_role_ids if self_grant_role else False

                    return {
                        "spnId": sp_id,
                        "spnDisplayName": sp_display,
                        "permissions": results,
                        "canSelfGrant": can_self_grant,
                    }

            self._send_json(asyncio.run(_run()))
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_grant_permissions(self):
        import httpx
        data = self._read_json()
        creds = self._creds(data)
        if not creds:
            self._send_json({"error": "Missing SPN credentials"}, 400); return
        perms = data.get("permissions") or []
        if not perms:
            self._send_json({"error": "permissions[] required"}, 400); return
        t, c, s = creds
        try:
            async def _run():
                tp = MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))
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
                        f"https://graph.microsoft.com/v1.0/servicePrincipals?$filter=appId eq '{self.GRAPH_APPID}'",
                        headers=H,
                    )
                    graph_data = graph_resp.json().get("value", [])
                    if not graph_data:
                        raise ValueError("Microsoft Graph SP not found")
                    graph_sp_id = graph_data[0]["id"]
                    roles_by_value = {r["value"]: r for r in graph_data[0].get("appRoles", [])}

                    assignments_resp = await client.get(
                        f"https://graph.microsoft.com/v1.0/servicePrincipals/{sp_id}/appRoleAssignments",
                        headers=H,
                    )
                    existing = assignments_resp.json().get("value", [])
                    existing_role_ids = {a["appRoleId"] for a in existing if a.get("resourceId") == graph_sp_id}

                    results = []
                    for perm_value in perms:
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

            self._send_json({"results": asyncio.run(_run())})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def log_message(self, format, *args):
        msg = format % args
        if "/api/" in msg:
            print(f"[API] {msg}")


if __name__ == "__main__":
    port = 4280
    server = ThreadingHTTPServer(("0.0.0.0", port), DevHandler)
    server.daemon_threads = True
    print(f"Dev server running at http://localhost:{port}")
    print(f"Frontend: {FRONTEND_DIR}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()

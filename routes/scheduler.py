"""Scheduler routes – set-end-date, create-job, schedule/cancel/run jobs."""
from __future__ import annotations

import asyncio
import threading

from flask import Blueprint, request, jsonify

from onedrive_provisioner.config import AzureConfig
from onedrive_provisioner.logging_setup import configure_logging
from onedrive_provisioner.entra import EntraOrchestrator, EntraConfig
from onedrive_provisioner.entra import (
    DiscoveryService, CleanupService, RbacService,
    remove_rbac_for_principals, downgrade_principals_to_reader,
)
from onedrive_provisioner.graph import GraphClient
from onedrive_provisioner.scheduler import HackScheduler, ScheduledJob
from onedrive_provisioner.storage import HackStateManager
from onedrive_provisioner.security.scheduler_credentials import make_scheduler_credential_config

from ._state import (
    extract_creds, get_state_manager, make_token_provider,
    scheduler_creds_dict, is_archived_state,
    audit_logger, operation_tracker,
)

bp = Blueprint("scheduler", __name__)

# ── Scheduler singleton ──
_hack_scheduler: HackScheduler | None = None
_scheduler_lock = threading.Lock()


def _scheduler_provision(cfg_dict: dict, tenant_id: str, client_id: str, client_secret: str):
    """Called by scheduler to provision a hack (runs in scheduler thread)."""
    configure_logging("INFO")
    cfg = EntraConfig.from_dict(cfg_dict)
    azure = AzureConfig(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    orch = EntraOrchestrator(azure, concurrency=int(cfg_dict.get("concurrency", 6)))
    report = asyncio.run(orch.provision(cfg))
    mgr = get_state_manager()
    if mgr:
        state = HackStateManager.build_state_from_report(cfg_dict, report.to_dict())
        mgr.save_state(cfg_dict.get("prefix", "unknown"), state)


def _scheduler_readonly(prefix: str, tenant_id: str, client_id: str, client_secret: str,
                        subscription_ids: list = None, mode: str = "team"):
    sub_ids = subscription_ids or []

    async def _do():
        tp = make_token_provider(tenant_id, client_id, client_secret)
        async with GraphClient(tp) as g:
            discovered = await DiscoveryService(g).discover(prefix)
        users = discovered.get("users", [])
        groups = discovered.get("groups", [])
        principals = [{"id": u["id"], "type": "user", "displayName": u.get("displayName", "")} for u in users]
        principals += [{"id": gr["id"], "type": "group", "displayName": gr.get("displayName", "")} for gr in groups]
        rbac_results = []
        if sub_ids and principals:
            async with RbacService(tp) as rbac:
                rbac_results = await downgrade_principals_to_reader(rbac, sub_ids, principals)
        return {
            "users_discovered": len(users),
            "groups_discovered": len(groups),
            "principals": [{"id": p["id"], "type": p["type"], "displayName": p.get("displayName", "")} for p in principals],
            "rbac_changes": rbac_results,
        }

    return asyncio.run(_do())


def _scheduler_cleanup(prefix: str, tenant_id: str, client_id: str, client_secret: str,
                       subscription_ids: list = None):
    sub_ids = subscription_ids or []
    audit_logger.log("cleanup.started", prefix, actor="scheduler",
                     details={"subscriptions": len(sub_ids)})
    op = operation_tracker.start("cleanup", prefix, actor="scheduler")

    async def _do():
        tp = make_token_provider(tenant_id, client_id, client_secret)
        async with GraphClient(tp) as g:
            discovered = await DiscoveryService(g).discover(prefix)
        users = discovered.get("users", [])
        groups = discovered.get("groups", [])
        user_ids = [u["id"] for u in users]
        group_ids = [gr["id"] for gr in groups]
        principal_ids = user_ids + group_ids
        github_results = None
        user_emails = [u.get("userPrincipalName", "") for u in users if u.get("userPrincipalName")]
        if user_emails:
            try:
                from onedrive_provisioner.github_emu import GitHubEnabler
                async with GitHubEnabler() as gh:
                    github_results = await gh.disable_users(user_emails, with_copilot=True, trigger_sync=True)
            except Exception as exc:
                github_results = {"error": str(exc)}
        rbac_results = []
        if sub_ids and principal_ids:
            async with RbacService(tp) as rbac:
                rbac_results = await remove_rbac_for_principals(rbac, sub_ids, principal_ids)
        deleted_users = []
        deleted_groups = []
        if user_ids or group_ids:
            async with GraphClient(tp) as g:
                cleaner = CleanupService(g)
                if user_ids:
                    await cleaner.delete_users(user_ids)
                    deleted_users = [{"id": u["id"], "displayName": u.get("displayName", ""), "upn": u.get("userPrincipalName", "")} for u in users]
                if group_ids:
                    await cleaner.delete_groups(group_ids)
                    deleted_groups = [{"id": gr["id"], "displayName": gr.get("displayName", "")} for gr in groups]
        return {
            "users_deleted": deleted_users,
            "groups_deleted": deleted_groups,
            "rbac_removed": rbac_results,
            "github_cleanup": github_results,
        }

    result = asyncio.run(_do())
    mgr = get_state_manager()
    if mgr:
        result["state_archived"] = mgr.archive_state(prefix, reason="scheduled_cleanup", cleanup_result=result)
    else:
        result["state_archived"] = False
    op.complete(result={"users": len(result.get("users_deleted", [])),
                        "groups": len(result.get("groups_deleted", []))})
    operation_tracker.finish(op)
    audit_logger.log("cleanup.completed", prefix, actor="scheduler",
                     details={"users_deleted": len(result.get("users_deleted", [])),
                              "groups_deleted": len(result.get("groups_deleted", []))})
    return result


def _get_scheduler() -> HackScheduler | None:
    global _hack_scheduler
    if _hack_scheduler is not None:
        return _hack_scheduler
    with _scheduler_lock:
        if _hack_scheduler is not None:
            return _hack_scheduler
        mgr = get_state_manager()
        if not mgr:
            return None
        _hack_scheduler = HackScheduler(
            get_state_manager=get_state_manager,
            run_provision=_scheduler_provision,
            run_cleanup=_scheduler_cleanup,
            run_readonly=_scheduler_readonly,
        )
        _hack_scheduler.start()
        return _hack_scheduler


# ────────────────────── Routes ──────────────────────

@bp.route("/api/hack-state/<prefix>/set-end-date", methods=["POST"])
def set_hack_end_date(prefix):
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    end_date = (data.get("endDate") or "").strip()
    if not end_date:
        return jsonify({"error": "endDate is required (ISO datetime)"}), 400
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
        return jsonify({"error": "Storage not configured"}), 503
    mgr = get_state_manager()
    state = mgr.get_state(prefix) if mgr else None
    if state and is_archived_state(state):
        return jsonify({"error": "Archived hacks are report-only. Schedule changes are disabled."}), 409

    try:
        jobs = scheduler.set_hack_end_date(
            prefix, end_date, scheduler_creds_dict(creds, data),
            subscription_ids=sub_ids, readonly_date=readonly_date,
            mode=mode, metadata=lifecycle_metadata,
        )
        return jsonify({
            "message": f"End date set for '{prefix}'",
            "endDate": end_date,
            "deleteDate": lifecycle_metadata["deleteDate"],
            "hackStartDate": lifecycle_metadata["hackStartDate"],
            "hackDate": lifecycle_metadata["hackDate"],
            "readonlyDate": readonly_date,
            "subscriptionIds": sub_ids,
            "jobs": [j.to_dict() for j in jobs],
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/create-job", methods=["POST"])
def create_job():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
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

    sub_ids = data.get("subscriptionIds") or []
    try:
        if job_type == "provision":
            config = data.get("config") or {}
            config["prefix"] = hack_prefix
            job = scheduler.schedule_provision(scheduled_at, config, scheduler_creds_dict(creds, data))
        else:
            cfg = {
                **make_scheduler_credential_config(scheduler_creds_dict(creds, data)),
                "subscription_ids": sub_ids,
            }
            if job_type == "readonly":
                cfg["mode"] = data.get("mode") or "team"
            job = ScheduledJob(
                id="", job_type=job_type, hack_prefix=hack_prefix,
                scheduled_at=scheduled_at, config=cfg,
            )
            job = scheduler.add_job(job)
        return jsonify({"message": f"{job_type} job scheduled", "job": job.to_dict()}), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/scheduled-hacks", methods=["GET"])
def list_scheduled_hacks():
    scheduler = _get_scheduler()
    if not scheduler:
        return jsonify({"error": "Storage not configured"}), 503
    status = request.args.get("status")
    jobs = scheduler.list_jobs(status=status)
    return jsonify([j.to_dict() for j in jobs])


@bp.route("/api/schedule-hack", methods=["POST"])
def schedule_hack():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
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
        job = scheduler.schedule_provision(scheduled_at, config, scheduler_creds_dict(creds, data))
        return jsonify({"message": "Hack scheduled", "job": job.to_dict()}), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/scheduled-hacks/<job_id>", methods=["DELETE"])
def cancel_scheduled_hack(job_id):
    scheduler = _get_scheduler()
    if not scheduler:
        return jsonify({"error": "Storage not configured"}), 503
    if scheduler.cancel_job(job_id):
        return jsonify({"message": "Job cancelled", "id": job_id})
    return jsonify({"error": "Job not found or not pending"}), 404


@bp.route("/api/scheduled-hacks/<job_id>/run", methods=["POST"])
def run_scheduled_hack_now(job_id):
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


@bp.route("/api/notifications/test-webhook", methods=["POST"])
def test_webhook():
    """Send a test message to a Teams Incoming Webhook URL."""
    data = request.get_json(silent=True) or {}
    url = data.get("webhook_url", "").strip()
    if not url:
        return jsonify({"error": "webhook_url is required"}), 400

    from onedrive_provisioner.notifications import NotificationService
    ns = NotificationService(teams_webhook_url=url)
    ok = ns.send_teams_message(
        "🧪 Test Notification",
        "This is a test message from **Spektra HackOps**. If you see this, your webhook is configured correctly!",
        facts=[{"name": "Status", "value": "✅ Working"}],
    )
    if ok:
        return jsonify({"message": "Test message sent successfully"})
    return jsonify({"error": "Failed to send — check your webhook URL"}), 502

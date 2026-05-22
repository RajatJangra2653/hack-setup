"""Scheduler engine: background thread that checks for due jobs every hour.

Jobs are persisted in Azure Blob Storage under `_scheduler/jobs.json`.
Two job types:
  - "provision": create a hack at a scheduled time
  - "cleanup":   delete users/groups/state when endDate passes
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from onedrive_provisioner.security.scheduler_credentials import (
    make_scheduler_credential_config,
    redact_scheduler_config,
    resolve_scheduler_credentials,
    delete_secret_blob,
)
from onedrive_provisioner.notifications import (
    NotificationService, check_upcoming_reminders,
)

logger = logging.getLogger(__name__)

JOBS_BLOB = "_scheduler/jobs.json"
CHECK_INTERVAL = 3600  # seconds (1 hour)


@dataclass
class ScheduledJob:
    id: str
    job_type: str          # "provision" | "cleanup"
    hack_prefix: str
    scheduled_at: str      # ISO datetime UTC
    status: str = "pending"  # pending | running | completed | failed
    created_at: str = ""
    completed_at: str = ""
    error: str = ""
    config: Dict[str, Any] = field(default_factory=dict)  # provision config or cleanup details
    result: Dict[str, Any] = field(default_factory=dict)  # execution results / details

    def to_dict(self) -> dict:
        data = asdict(self)
        data["config"] = redact_scheduler_config(data.get("config") or {})
        data["result"] = redact_scheduler_config(data.get("result") or {})
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduledJob":
        config = d.get("config", {}) or {}
        if "client_secret" in config:
            config = dict(config)
            config.pop("client_secret", None)
            config["legacy_secret_removed"] = True
            config["requiresCredentialReference"] = True
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            job_type=d.get("job_type", ""),
            hack_prefix=d.get("hack_prefix", ""),
            scheduled_at=d.get("scheduled_at", ""),
            status=d.get("status", "pending"),
            created_at=d.get("created_at", ""),
            completed_at=d.get("completed_at", ""),
            error=d.get("error", ""),
            config=config,
            result=redact_scheduler_config(d.get("result", {})),
        )


class HackScheduler:
    """Background scheduler that processes due provision/cleanup/readonly jobs."""

    def __init__(
        self,
        get_state_manager: Callable,
        run_provision: Callable,  # (cfg_dict, tenant_id, client_id, client_secret) -> None
        run_cleanup: Callable,    # (prefix, tenant_id, client_id, client_secret, subscription_ids) -> None
        run_readonly: Callable = None,  # (prefix, tenant_id, client_id, client_secret, subscription_ids, mode) -> None
        notifier: Optional[NotificationService] = None,
    ) -> None:
        self._get_mgr = get_state_manager
        self._run_provision = run_provision
        self._run_cleanup = run_cleanup
        self._run_readonly = run_readonly
        self._notifier = notifier or NotificationService()
        self._sent_reminders: set = set()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ─── Job persistence ───
    def _load_jobs(self) -> List[ScheduledJob]:
        mgr = self._get_mgr()
        if not mgr:
            return []
        data = mgr._blob.read_json(JOBS_BLOB)
        if not data or "jobs" not in data:
            return []
        return [ScheduledJob.from_dict(j) for j in data["jobs"]]

    def _save_jobs(self, jobs: List[ScheduledJob]) -> None:
        mgr = self._get_mgr()
        if not mgr:
            return
        mgr._blob.write_json(JOBS_BLOB, {"jobs": [j.to_dict() for j in jobs]})

    # ─── Public API ───
    def add_job(self, job: ScheduledJob) -> ScheduledJob:
        """Add a scheduled job."""
        if not job.id:
            job.id = str(uuid.uuid4())
        job.created_at = datetime.now(timezone.utc).isoformat()
        job.status = "pending"
        with self._lock:
            jobs = self._load_jobs()
            jobs.append(job)
            self._save_jobs(jobs)
        logger.info("Scheduled job %s (%s) for prefix '%s' at %s",
                     job.id, job.job_type, job.hack_prefix, job.scheduled_at)
        return job

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a pending job. Returns True if found and cancelled."""
        with self._lock:
            jobs = self._load_jobs()
            for j in jobs:
                if j.id == job_id and j.status == "pending":
                    j.status = "cancelled"
                    j.completed_at = datetime.now(timezone.utc).isoformat()
                    self._save_jobs(jobs)
                    return True
        return False

    def list_jobs(self, status: Optional[str] = None) -> List[ScheduledJob]:
        """List jobs, optionally filtered by status."""
        jobs = self._load_jobs()
        if status:
            jobs = [j for j in jobs if j.status == status]
        return jobs

    def get_job(self, job_id: str) -> Optional[ScheduledJob]:
        """Get a specific job by ID."""
        for j in self._load_jobs():
            if j.id == job_id:
                return j
        return None

    def run_job_now(self, job_id: str) -> ScheduledJob:
        """Immediately execute a pending or failed job. Returns the updated job."""
        with self._lock:
            jobs = self._load_jobs()
            job = None
            for j in jobs:
                if j.id == job_id:
                    job = j
                    break
            if not job:
                raise ValueError(f"Job {job_id} not found")
            if job.status not in ("pending", "failed"):
                raise ValueError(f"Job {job_id} is {job.status}, only pending/failed jobs can be run")

            job.status = "running"
            self._save_jobs(jobs)

        try:
            if job.job_type == "cleanup":
                self._execute_cleanup(job)
            elif job.job_type == "provision":
                self._execute_provision(job)
            elif job.job_type == "readonly":
                self._execute_readonly(job)
            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc).isoformat()
            logger.info("Manual run: job %s (%s) completed for '%s'",
                        job.id, job.job_type, job.hack_prefix)
            self._cleanup_secret_blob(job)
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            job.completed_at = datetime.now(timezone.utc).isoformat()
            logger.error("Manual run: job %s failed: %s", job.id, exc)

        with self._lock:
            jobs = self._load_jobs()
            for i, j in enumerate(jobs):
                if j.id == job_id:
                    jobs[i] = job
                    break
            self._save_jobs(jobs)

        return job

    def set_hack_end_date(self, prefix: str, end_date: str, creds: Dict[str, str],
                          subscription_ids: Optional[List[str]] = None,
                          readonly_date: Optional[str] = None,
                          mode: str = "team",
                          metadata: Optional[Dict[str, Any]] = None) -> List[ScheduledJob]:
        """Set end date (and optional read-only date) for a hack.

        Creates up to two scheduler jobs:
        - readonly job at readonly_date (if provided)
        - cleanup job at end_date

        Also stores endDate / readonlyDate on the hack state itself.
        subscription_ids: Azure subscription IDs for RBAC changes.
        Returns list of created jobs.
        """
        credential_cfg = make_scheduler_credential_config(creds, blob_client=self._get_blob_client())

        # Update hack state
        mgr = self._get_mgr()
        metadata = metadata or {}
        if mgr:
            state = mgr.get_state(prefix)
            if state:
                state["endDate"] = end_date
                state["deleteDate"] = metadata.get("deleteDate") or end_date
                if metadata.get("hackStartDate"):
                    state["hackStartDate"] = metadata["hackStartDate"]
                if metadata.get("hackDate"):
                    state["hackDate"] = metadata["hackDate"]
                if readonly_date:
                    state["readonlyDate"] = readonly_date
                if subscription_ids:
                    state["subscriptionIds"] = subscription_ids
                mgr.save_state(prefix, state, version=False)

        # Cancel any existing pending cleanup/readonly for this prefix
        with self._lock:
            jobs = self._load_jobs()
            for j in jobs:
                if (j.hack_prefix == prefix
                        and j.job_type in ("cleanup", "readonly")
                        and j.status == "pending"):
                    j.status = "cancelled"
                    j.completed_at = datetime.now(timezone.utc).isoformat()
            self._save_jobs(jobs)

        created_jobs = []
        base_cfg = {
            **credential_cfg,
            "subscription_ids": subscription_ids or [],
            "hackStartDate": metadata.get("hackStartDate", ""),
            "hackDate": metadata.get("hackDate", ""),
            "deleteDate": metadata.get("deleteDate") or end_date,
        }

        # Read-only job (if date provided)
        if readonly_date:
            ro_job = ScheduledJob(
                id=str(uuid.uuid4()),
                job_type="readonly",
                hack_prefix=prefix,
                scheduled_at=readonly_date,
                config={**base_cfg, "mode": mode},
            )
            created_jobs.append(self.add_job(ro_job))

        # Cleanup job
        cleanup_job = ScheduledJob(
            id=str(uuid.uuid4()),
            job_type="cleanup",
            hack_prefix=prefix,
            scheduled_at=end_date,
            config=base_cfg,
        )
        created_jobs.append(self.add_job(cleanup_job))

        return created_jobs

    def schedule_provision(self, scheduled_at: str, config: Dict[str, Any],
                           creds: Dict[str, str]) -> ScheduledJob:
        """Schedule a hack to be provisioned at a future time."""
        prefix = config.get("prefix", "unknown")
        job = ScheduledJob(
            id=str(uuid.uuid4()),
            job_type="provision",
            hack_prefix=prefix,
            scheduled_at=scheduled_at,
            config={**config, **make_scheduler_credential_config(creds, blob_client=self._get_blob_client())},
        )
        return self.add_job(job)

    # ─── Background loop ───
    def start(self) -> None:
        """Start the background scheduler thread."""
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hack-scheduler")
        self._thread.start()
        logger.info("Hack scheduler started (checking every %ds)", CHECK_INTERVAL)

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("Scheduler tick error")
            time.sleep(CHECK_INTERVAL)

    def _tick(self) -> None:
        """Check for due jobs and execute them. Also send upcoming reminders."""
        now = datetime.now(timezone.utc)

        # ── Send 48-hour reminders ──
        try:
            jobs_snapshot = self._load_jobs()
            mgr = self._get_mgr()
            state_getter = mgr.get_state if mgr else None
            self._sent_reminders = check_upcoming_reminders(
                jobs_snapshot, self._notifier, state_getter, self._sent_reminders,
            )
        except Exception:
            logger.exception("Reminder check error")

        with self._lock:
            jobs = self._load_jobs()
            changed = False
            for job in jobs:
                if job.status != "pending":
                    continue
                try:
                    due_at = datetime.fromisoformat(job.scheduled_at.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                if due_at > now:
                    continue

                # Job is due — execute it
                job.status = "running"
                changed = True
                self._save_jobs(jobs)

                try:
                    if job.job_type == "cleanup":
                        self._execute_cleanup(job)
                    elif job.job_type == "provision":
                        self._execute_provision(job)
                    elif job.job_type == "readonly":
                        self._execute_readonly(job)
                    job.status = "completed"
                    job.completed_at = datetime.now(timezone.utc).isoformat()
                    logger.info("Scheduler job %s (%s) completed for '%s'",
                                job.id, job.job_type, job.hack_prefix)
                    # Clean up secret blob now that the job succeeded
                    self._cleanup_secret_blob(job)
                    # Send completion notification
                    self._notify_job_complete(job)
                except Exception as exc:
                    job.status = "failed"
                    job.error = str(exc)
                    job.completed_at = datetime.now(timezone.utc).isoformat()
                    logger.error("Scheduler job %s failed: %s", job.id, exc)
                    self._notify_job_failed(job, str(exc))

            if changed:
                self._save_jobs(jobs)

    def _get_blob_client(self):
        """Get blob client from the state manager for secret blob operations."""
        mgr = self._get_mgr()
        return mgr._blob if mgr else None

    def _cleanup_secret_blob(self, job: ScheduledJob) -> None:
        """Delete the secret blob after a job completes successfully."""
        ref = (job.config or {}).get("client_secret_ref") or {}
        if ref.get("type") == "blob_secret" and ref.get("id"):
            blob = self._get_blob_client()
            if blob:
                delete_secret_blob(blob, ref["id"])

    def _execute_cleanup(self, job: ScheduledJob) -> None:
        """Run cleanup for an expired hack."""
        cfg = job.config
        t, c, s = resolve_scheduler_credentials(cfg, blob_client=self._get_blob_client())
        sub_ids = cfg.get("subscription_ids", [])
        result = self._run_cleanup(job.hack_prefix, t, c, s, subscription_ids=sub_ids)
        if isinstance(result, dict):
            job.result = result

    def _execute_readonly(self, job: ScheduledJob) -> None:
        """Switch a hack to read-only mode."""
        cfg = job.config
        t, c, s = resolve_scheduler_credentials(cfg, blob_client=self._get_blob_client())
        if not self._run_readonly:
            raise ValueError("No readonly handler configured")
        sub_ids = cfg.get("subscription_ids", [])
        mode = cfg.get("mode", "team")
        result = self._run_readonly(job.hack_prefix, t, c, s,
                           subscription_ids=sub_ids, mode=mode)
        if isinstance(result, dict):
            job.result = result

    def _execute_provision(self, job: ScheduledJob) -> None:
        """Run provisioning for a scheduled hack."""
        cfg = dict(job.config)
        t, c, s = resolve_scheduler_credentials(cfg, blob_client=self._get_blob_client())
        cfg.pop("tenant_id", None)
        cfg.pop("client_id", None)
        cfg.pop("client_secret_ref", None)
        cfg.pop("credentialRef", None)
        self._run_provision(cfg, t, c, s)

    def _get_hack_name(self, prefix: str) -> str:
        """Resolve hack name from state, fallback to prefix."""
        mgr = self._get_mgr()
        if mgr:
            state = mgr.get_state(prefix)
            if state:
                return state.get("hackName") or prefix
        return prefix

    def _notify_job_complete(self, job: ScheduledJob) -> None:
        hack_name = self._get_hack_name(job.hack_prefix)
        if job.job_type == "cleanup":
            self._notifier.notify_cleanup_complete(job.hack_prefix, hack_name)
        elif job.job_type == "readonly":
            self._notifier.notify_readonly_applied(job.hack_prefix, hack_name)
        elif job.job_type == "provision":
            result = job.result or {}
            self._notifier.notify_batch_complete(
                job.hack_prefix, hack_name,
                result.get("created", 0), result.get("failed", 0),
            )

    def _notify_job_failed(self, job: ScheduledJob, error: str) -> None:
        hack_name = self._get_hack_name(job.hack_prefix)
        action_map = {"cleanup": "Cleanup", "readonly": "Read-Only", "provision": "Provisioning"}
        self._notifier.notify_failure(job.hack_prefix, hack_name,
                                      action_map.get(job.job_type, job.job_type), error)

"""Scheduler engine: background thread that checks for due jobs every 60 seconds.

Jobs are persisted in Azure Blob Storage under `_scheduler/jobs.json`.
Two job types:
  - "provision": create a hack at a scheduled time
  - "cleanup":   delete users/groups/state when endDate passes
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

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
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduledJob":
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            job_type=d.get("job_type", ""),
            hack_prefix=d.get("hack_prefix", ""),
            scheduled_at=d.get("scheduled_at", ""),
            status=d.get("status", "pending"),
            created_at=d.get("created_at", ""),
            completed_at=d.get("completed_at", ""),
            error=d.get("error", ""),
            config=d.get("config", {}),
            result=d.get("result", {}),
        )


class HackScheduler:
    """Background scheduler that processes due provision/cleanup/readonly jobs."""

    def __init__(
        self,
        get_state_manager: Callable,
        run_provision: Callable,  # (cfg_dict, tenant_id, client_id, client_secret) -> None
        run_cleanup: Callable,    # (prefix, tenant_id, client_id, client_secret, subscription_ids) -> None
        run_readonly: Callable = None,  # (prefix, tenant_id, client_id, client_secret, subscription_ids, mode) -> None
    ) -> None:
        self._get_mgr = get_state_manager
        self._run_provision = run_provision
        self._run_cleanup = run_cleanup
        self._run_readonly = run_readonly
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
        """Immediately execute a pending job. Returns the updated job."""
        with self._lock:
            jobs = self._load_jobs()
            job = None
            for j in jobs:
                if j.id == job_id:
                    job = j
                    break
            if not job:
                raise ValueError(f"Job {job_id} not found")
            if job.status != "pending":
                raise ValueError(f"Job {job_id} is {job.status}, not pending")

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
                          mode: str = "team") -> List[ScheduledJob]:
        """Set end date (and optional read-only date) for a hack.

        Creates up to two scheduler jobs:
        - readonly job at readonly_date (if provided)
        - cleanup job at end_date

        Also stores endDate / readonlyDate on the hack state itself.
        subscription_ids: Azure subscription IDs for RBAC changes.
        Returns list of created jobs.
        """
        # Update hack state
        mgr = self._get_mgr()
        if mgr:
            state = mgr.get_state(prefix)
            if state:
                state["endDate"] = end_date
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
            "tenant_id": creds["tenant_id"],
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "subscription_ids": subscription_ids or [],
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
            config={**config,
                    "tenant_id": creds["tenant_id"],
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"]},
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
        """Check for due jobs and execute them."""
        now = datetime.now(timezone.utc)
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
                except Exception as exc:
                    job.status = "failed"
                    job.error = str(exc)
                    job.completed_at = datetime.now(timezone.utc).isoformat()
                    logger.error("Scheduler job %s failed: %s", job.id, exc)

            if changed:
                self._save_jobs(jobs)

    def _execute_cleanup(self, job: ScheduledJob) -> None:
        """Run cleanup for an expired hack."""
        cfg = job.config
        t = cfg.get("tenant_id", "")
        c = cfg.get("client_id", "")
        s = cfg.get("client_secret", "")
        if not all([t, c, s]):
            raise ValueError("Missing SPN credentials in scheduled cleanup job")
        sub_ids = cfg.get("subscription_ids", [])
        result = self._run_cleanup(job.hack_prefix, t, c, s, subscription_ids=sub_ids)
        if isinstance(result, dict):
            job.result = result

    def _execute_readonly(self, job: ScheduledJob) -> None:
        """Switch a hack to read-only mode."""
        cfg = job.config
        t = cfg.get("tenant_id", "")
        c = cfg.get("client_id", "")
        s = cfg.get("client_secret", "")
        if not all([t, c, s]):
            raise ValueError("Missing SPN credentials in scheduled readonly job")
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
        t = cfg.pop("tenant_id", "")
        c = cfg.pop("client_id", "")
        s = cfg.pop("client_secret", "")
        if not all([t, c, s]):
            raise ValueError("Missing SPN credentials in scheduled provision job")
        self._run_provision(cfg, t, c, s)

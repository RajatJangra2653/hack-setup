"""Top-level orchestration: per-user pipeline + bulk parallel runner."""
from __future__ import annotations

import asyncio
from typing import Callable, List, Optional, Sequence

from .auth import MsalTokenProvider
from .config import AppConfig
from .graph import GraphClient, GraphError
from .logging_setup import get_logger
from .models import BulkReport, Status, UserResult
from .onedrive import OneDriveProvisioner, UserResolver
from .uploader import OneDriveUploader, build_source

# Type alias for progress callbacks: receives (user_result, completed_count, total)
ProgressCallback = Callable[[UserResult, int, int], None]

logger = get_logger(__name__)


class Orchestrator:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._token_provider = MsalTokenProvider(cfg.azure)

    # ---------------------------------------------------------- single-user
    async def provision_user(self, user: str) -> UserResult:
        async with GraphClient(self._token_provider, max_retries=self.cfg.execution.max_retries) as g:
            return await self._provision_user_inner(g, user)

    async def upload_for_user(
        self, user: str, source_spec: Optional[str] = None, destination: Optional[str] = None
    ) -> UserResult:
        async with GraphClient(self._token_provider, max_retries=self.cfg.execution.max_retries) as g:
            return await self._pipeline(g, user, source_spec, destination)

    async def bulk_setup(
        self,
        users: Optional[Sequence[str]] = None,
        source_spec: Optional[str] = None,
        destination: Optional[str] = None,
        on_user_done: Optional[ProgressCallback] = None,
    ) -> BulkReport:
        async with GraphClient(self._token_provider, max_retries=self.cfg.execution.max_retries) as g:
            target_users = await self._collect_users(g, users)

            # Pre-provision: call SharePoint Admin API to queue personal sites
            await self._enqueue_sites(target_users)

            sem = asyncio.Semaphore(self.cfg.execution.concurrency)
            completed_count = 0
            lock = asyncio.Lock()

            async def _worker(u: str) -> UserResult:
                nonlocal completed_count
                async with sem:
                    result = await self._pipeline(g, u, source_spec, destination)
                async with lock:
                    completed_count += 1
                    if on_user_done:
                        try:
                            on_user_done(result, completed_count, len(target_users))
                        except Exception:
                            pass  # never let callback errors break the pipeline
                return result

            raw_results = await asyncio.gather(
                *(_worker(u) for u in target_users), return_exceptions=True
            )
            results: list[UserResult] = []
            for i, r in enumerate(raw_results):
                if isinstance(r, Exception):
                    results.append(UserResult(
                        user=target_users[i],
                        status=Status.FAILED,
                        message=str(r),
                    ))
                else:
                    results.append(r)

        succeeded = sum(1 for r in results if r.status == Status.SUCCESS)
        failed = sum(1 for r in results if r.status == Status.FAILED)
        skipped = sum(1 for r in results if r.status in (Status.SKIPPED, Status.DRY_RUN))
        return BulkReport(
            total=len(results),
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            results=list(results),
        )

    # ------------------------------------------------------------- internal
    async def _enqueue_sites(self, users: List[str]) -> None:
        """Pre-provision: call Request-SPOPersonalSite equivalent API.

        Groups users by domain so multi-domain tenants get per-domain
        enqueue calls with the correct SharePoint admin URL.
        """
        emails_with_domain = [u for u in users if "@" in u]
        if not emails_with_domain:
            return

        # Bucket users by tenant name (first label of their domain)
        buckets: dict[str, list[str]] = {}
        for email in emails_with_domain:
            domain = email.split("@", 1)[1]
            tenant_name = domain.split(".")[0]
            buckets.setdefault(tenant_name, []).append(email)

        any_ok = False
        for tenant_name, bucket_emails in buckets.items():
            logger.info(
                "orchestrator.enqueue_sites",
                count=len(bucket_emails),
                tenant_name=tenant_name,
            )
            ok = await OneDriveProvisioner.enqueue_personal_sites(
                self._token_provider, bucket_emails, tenant_name,
            )
            if ok:
                any_ok = True

        if any_ok:
            logger.info("orchestrator.enqueue_sites_done", success=True)
            # Give SharePoint a moment to start processing the queue
            await asyncio.sleep(self.cfg.execution.enqueue_delay
                                if hasattr(self.cfg.execution, 'enqueue_delay')
                                else 5)
        else:
            logger.warning(
                "orchestrator.enqueue_sites_skipped",
                reason="SharePoint Admin API unavailable — will rely on per-user write trigger",
            )

    async def _collect_users(
        self, graph: GraphClient, explicit: Optional[Sequence[str]]
    ) -> List[str]:
        if explicit:
            return list(explicit)
        if self.cfg.users.list:
            return list(self.cfg.users.list)
        if self.cfg.users.all_users:
            resolver = UserResolver(graph)
            ids: list[str] = []
            async for u in resolver.list_all_members():
                upn = u.get("userPrincipalName") or u.get("id")
                if upn:
                    ids.append(upn)
            logger.info("orchestrator.users_loaded", count=len(ids), source="tenant")
            return ids
        raise ValueError(
            "No users supplied: pass `users`, set users.list in config, or enable users.all_users."
        )

    async def _provision_user_inner(self, graph: GraphClient, user: str) -> UserResult:
        resolver = UserResolver(graph)
        # Extract tenant name from UPN for SharePoint URL construction
        tenant_name, sp_suffix = self._sp_tenant_for(user)
        provisioner = OneDriveProvisioner(
            graph,
            token_provider=self._token_provider,
            tenant_name=tenant_name,
            sharepoint_suffix=sp_suffix,
        )
        try:
            u = await resolver.resolve(user)
        except GraphError as exc:
            return UserResult(user=user, status=Status.FAILED, message=f"resolve: {exc}")
        try:
            drive = await provisioner.ensure_drive(u["id"], upn=u.get("userPrincipalName") or user)
        except GraphError as exc:
            return UserResult(
                user=user, user_id=u["id"], status=Status.FAILED, message=f"provision: {exc}"
            )
        return UserResult(
            user=user, user_id=u["id"], drive_id=drive.get("id"), status=Status.SUCCESS
        )

    @staticmethod
    def _sp_tenant_for(user: str) -> tuple[str | None, str]:
        """Extract SharePoint tenant name and suffix from UPN.

        Returns (tenant_name, suffix) where suffix is 'com' or 'us'.
        For GCC tenants, the SharePoint URL ends in .sharepoint.us.
        """
        if "@" not in user:
            return None, "com"
        domain = user.split("@", 1)[1].lower()
        tenant_name = domain.split(".")[0]
        # GCC tenants use .sharepoint.us — detect via domain suffix or keywords
        if domain.endswith(".us") or domain.endswith(".gov") or "gcc" in domain:
            return tenant_name, "us"
        return tenant_name, "com"

    async def _pipeline(
        self,
        graph: GraphClient,
        user: str,
        source_spec: Optional[str],
        destination: Optional[str],
    ) -> UserResult:
        spec = source_spec or self.cfg.upload.source
        dest = destination if destination is not None else self.cfg.upload.destination
        result = await self._provision_user_inner(graph, user)
        if result.status == Status.FAILED:
            return result

        try:
            source = build_source(spec)
        except (FileNotFoundError, ValueError, PermissionError, IsADirectoryError, OSError) as exc:
            result.status = Status.FAILED
            result.message = f"source: {exc}"
            return result

        uploader = OneDriveUploader(
            graph,
            chunk_size_mb=self.cfg.upload.chunk_size_mb,
            large_file_threshold_mb=self.cfg.upload.large_file_threshold_mb,
            dry_run=self.cfg.execution.dry_run,
        )
        files = await uploader.upload_tree(result.user_id, source, dest)
        result.files = files
        if any(f.status == Status.FAILED for f in files):
            result.status = Status.FAILED
            result.message = f"{sum(1 for f in files if f.status == Status.FAILED)} file(s) failed"
        elif self.cfg.execution.dry_run:
            result.status = Status.DRY_RUN
        return result

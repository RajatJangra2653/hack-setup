"""Top-level orchestrator: ensures groups, then provisions users in parallel."""
from __future__ import annotations

import asyncio
from typing import Callable, Dict, List, Optional, Tuple

from ..auth import MsalTokenProvider
from ..config import AzureConfig
from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger
from .group_service import GroupService
from .license_service import LicenseService
from .models import (
    EntraConfig,
    ProvisioningReport,
    Status,
    UserPlan,
    UserProvisionResult,
)
from .naming import admin_group_name, generate_user_plans, team_group_name
from .role_service import RoleService
from .tap_service import TapService
from .user_service import UserService

logger = get_logger(__name__)

ProgressCallback = Callable[[UserProvisionResult, int, int], None]


class EntraOrchestrator:
    def __init__(self, azure_cfg: AzureConfig, *, concurrency: int = 6) -> None:
        self._token_provider = MsalTokenProvider(azure_cfg)
        self._concurrency = max(1, min(concurrency, 16))

    async def provision(
        self,
        cfg: EntraConfig,
        on_user_done: Optional[ProgressCallback] = None,
    ) -> ProvisioningReport:
        # Allow per-job concurrency override from EntraConfig
        if cfg.concurrency and cfg.concurrency > 0:
            self._concurrency = max(1, min(int(cfg.concurrency), 32))

        # Auto-detect tenant defaults if caller left them blank
        if not cfg.domain or cfg.tap_lifetime is None or cfg.tap_lifetime <= 0:
            async with GraphClient(self._token_provider) as g:
                from .tenant_service import TenantService
                ts = TenantService(g)
                if not cfg.domain:
                    cfg.domain = await ts.detect_default_domain()
                    logger.info("entra.orchestrator.domain_detected", domain=cfg.domain)
                if not cfg.tap_lifetime or cfg.tap_lifetime <= 0:
                    tap_max = await ts.get_tap_max_lifetime()
                    if tap_max:
                        cfg.tap_lifetime = tap_max
                        logger.info("entra.orchestrator.tap_lifetime_detected",
                                    minutes=tap_max)
                    else:
                        cfg.tap_lifetime = 60  # safe fallback

        plans = generate_user_plans(cfg)
        logger.info("entra.orchestrator.start",
                    total=len(plans), mode=cfg.mode, dry_run=cfg.dry_run,
                    domain=cfg.domain, tap_lifetime=cfg.tap_lifetime)

        if cfg.dry_run:
            results = [
                UserProvisionResult(
                    user_principal_name=p.upn,
                    status=Status.DRY_RUN,
                    is_admin=p.is_admin,
                    groups=self._planned_groups(cfg, p),
                    licenses=list(cfg.licenses) if not p.is_admin else [],
                ) for p in plans
            ]
            return self._build_report(plans, results, groups_created=0, groups=[])

        async with GraphClient(self._token_provider) as g:
            group_svc = GroupService(g, hack_name=cfg.hack_name, created_by=cfg.created_by)
            license_svc = LicenseService(g)
            user_svc = UserService(g, default_password=cfg.initial_password,
                                   hack_name=cfg.hack_name, created_by=cfg.created_by,
                                   force_change_password=cfg.force_change_password)
            tap_svc = TapService(g, lifetime_minutes=cfg.tap_lifetime)
            role_svc = RoleService(g)

            # 1) Pre-create / lookup groups
            group_map, groups_created = await self._ensure_groups(cfg, plans, group_svc)

            # 2) Pre-resolve licenses (one Graph call shared across all users)
            license_map = await license_svc.resolve(cfg.licenses) if cfg.licenses else {}
            sku_ids = [sid for (sid, _part) in license_map.values()]

            # 3) Provision users in parallel
            sem = asyncio.Semaphore(self._concurrency)
            done = 0
            done_lock = asyncio.Lock()

            async def _worker(plan: UserPlan) -> UserProvisionResult:
                nonlocal done
                async with sem:
                    res = await self._provision_one(
                        plan, cfg, user_svc, tap_svc, license_svc,
                        group_svc, role_svc, group_map, sku_ids,
                        list(license_map.keys()),
                    )
                async with done_lock:
                    done += 1
                    if on_user_done:
                        try:
                            on_user_done(res, done, len(plans))
                        except Exception:
                            pass
                return res

            results = await asyncio.gather(
                *(_worker(p) for p in plans), return_exceptions=False
            )

        return self._build_report(
            plans, results,
            groups_created=groups_created,
            groups=sorted(group_map.keys()),
        )

    # ------------------------------------------------------------------
    async def _ensure_groups(
        self,
        cfg: EntraConfig,
        plans: List[UserPlan],
        group_svc: GroupService,
    ) -> Tuple[Dict[str, dict], int]:
        names: set[str] = set()
        if cfg.create_team_groups and cfg.mode == "team":
            for p in plans:
                if p.team:
                    names.add(team_group_name(cfg, p.team))
        if cfg.create_admin_group and any(p.is_admin for p in plans):
            names.add(admin_group_name(cfg))

        created = 0
        out: Dict[str, dict] = {}
        for name in sorted(names):
            try:
                grp, was_new = await group_svc.ensure(name)
                out[name] = grp
                if was_new:
                    created += 1
            except GraphError as exc:
                logger.error("entra.group.ensure_failed", name=name, msg=str(exc))
        return out, created

    # ------------------------------------------------------------------
    async def _provision_one(
        self,
        plan: UserPlan,
        cfg: EntraConfig,
        user_svc: UserService,
        tap_svc: TapService,
        license_svc: LicenseService,
        group_svc: GroupService,
        role_svc: RoleService,
        group_map: Dict[str, dict],
        sku_ids: List[str],
        license_names: List[str],
    ) -> UserProvisionResult:
        # 1) Create or fetch user
        try:
            user, created_now, password = await user_svc.ensure(plan, skip_existing=cfg.skip_existing)
        except GraphError as exc:
            return UserProvisionResult(
                user_principal_name=plan.upn,
                status=Status.FAILED,
                is_admin=plan.is_admin,
                message=f"user: {exc}",
            )

        user_id = user["id"]
        result = UserProvisionResult(
            user_principal_name=plan.upn,
            user_id=user_id,
            password=password,
            status=Status.CREATED if created_now else Status.EXISTING,
            is_admin=plan.is_admin,
        )

        # 2) Issue TAP (only for fresh users; existing users already have credentials)
        if created_now:
            tap = await tap_svc.issue(user_id)
            if tap:
                result.tap = tap.get("temporaryAccessPass")
                result.tap_expires = tap.get("startDateTime")  # plus lifetimeInMinutes

        # 3) Licenses
        if sku_ids and not plan.is_admin:
            assigned = await license_svc.assign(user_id, sku_ids)
            # Map back assigned SKU IDs to friendly names for output
            result.licenses = [
                name for name, (sid, _) in zip(
                    license_names,
                    [(sid, "") for sid in sku_ids]
                ) if sid in assigned
            ]

        # 4) Group membership
        if plan.team and cfg.create_team_groups:
            grp_name = team_group_name(cfg, plan.team)
            grp = group_map.get(grp_name)
            if grp and await group_svc.add_member(grp["id"], user_id):
                result.groups.append(grp_name)

        if plan.is_admin and cfg.create_admin_group:
            grp_name = admin_group_name(cfg)
            grp = group_map.get(grp_name)
            if grp and await group_svc.add_member(grp["id"], user_id):
                result.groups.append(grp_name)

        # 5) Admin role
        if plan.is_admin and cfg.assign_admin_role:
            await role_svc.assign_global_reader(user_id)

        return result

    # ------------------------------------------------------------------
    def _planned_groups(self, cfg: EntraConfig, plan: UserPlan) -> List[str]:
        out: List[str] = []
        if plan.team and cfg.create_team_groups:
            out.append(team_group_name(cfg, plan.team))
        if plan.is_admin and cfg.create_admin_group:
            out.append(admin_group_name(cfg))
        return out

    def _build_report(
        self,
        plans: List[UserPlan],
        results: List[UserProvisionResult],
        *,
        groups_created: int,
        groups: List[str],
    ) -> ProvisioningReport:
        return ProvisioningReport(
            total_users=len(plans),
            created=sum(1 for r in results if r.status == Status.CREATED),
            existing=sum(1 for r in results if r.status == Status.EXISTING),
            failed=sum(1 for r in results if r.status == Status.FAILED),
            admins=sum(1 for r in results if r.is_admin),
            groups_created=groups_created,
            groups=groups,
            users=results,
        )

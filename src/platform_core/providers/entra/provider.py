"""Entra ID provider — orchestrates user/group/license/TAP/RBAC
operations as a single Provider implementation.

This is the highest-level Entra abstraction.  It composes the
lower-level *Ops classes and exposes the standard Provider lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from platform_core.core import HackPrefix, JsonDict
from platform_core.events import (
    EventBus,
    ProvisionCompletedEvent,
    ProvisionStartedEvent,
    UserCreatedEvent,
    CleanupCompletedEvent,
    CleanupStartedEvent,
)
from platform_core.models.user import UserPlan, UserProvisionResult, UserStatus
from platform_core.providers.base import ProviderBase
from platform_core.providers.graph.client import GraphClient
from platform_core.providers.entra.user_ops import UserOps
from platform_core.providers.entra.group_ops import GroupOps
from platform_core.providers.entra.license_ops import LicenseOps
from platform_core.providers.entra.tap_ops import TapOps
from platform_core.providers.entra.rbac_ops import RbacOps

logger = logging.getLogger(__name__)


class EntraProvider(ProviderBase):
    """Entra ID provider — manages users, groups, licenses, TAPs, RBAC."""

    name = "entra"

    def __init__(
        self,
        graph_client: GraphClient,
        *,
        arm_token_fn: Any = None,
        event_bus: EventBus | None = None,
    ) -> None:
        super().__init__(event_bus=event_bus)
        self._graph = graph_client
        self.users = UserOps(graph_client)
        self.groups = GroupOps(graph_client)
        self.licenses = LicenseOps(graph_client)
        self.taps = TapOps(graph_client)
        self.rbac = RbacOps(arm_token_fn) if arm_token_fn else None

    async def validate(self) -> bool:
        try:
            data = await self._graph.get("/organization")
            return bool(data.get("value"))
        except Exception as exc:
            self._log.error("Entra validation failed: %s", exc)
            return False

    async def provision(
        self,
        desired: JsonDict,
        *,
        dry_run: bool = False,
        on_progress: Any = None,
    ) -> JsonDict:
        """Provision users, groups, licenses, TAPs for a hack.

        Expected *desired* shape::

            {
                "prefix": "nyc-hack-",
                "domain": "contoso.onmicrosoft.com",
                "users": [UserPlan, ...],
                "licenses": ["M365_E3"],
                "groups": [{"name": "admin-group", "is_admin": true}, ...],
                "tap_lifetime_minutes": 480,
                "rbac": {"subscriptions": [...], "role": "Contributor"},
                "concurrency": 8,
            }
        """
        prefix = desired["prefix"]
        user_plans = [
            UserPlan.model_validate(u) if isinstance(u, dict) else u
            for u in desired.get("users", [])
        ]
        concurrency = desired.get("concurrency", 8)
        sem = asyncio.Semaphore(concurrency)

        await self._emit(ProvisionStartedEvent(
            hack_prefix=prefix, total_users=len(user_plans),
        ))

        if dry_run:
            return {
                "status": "dry_run",
                "prefix": prefix,
                "planned_users": len(user_plans),
            }

        # 1. Resolve licenses
        license_names = desired.get("licenses", [])
        resolved_licenses = await self.licenses.resolve(license_names) if license_names else {}
        sku_ids = [sid for sid, _ in resolved_licenses.values()]

        # 2. Create groups
        groups_created: list[JsonDict] = []
        for g in desired.get("groups", []):
            grp, created = await self.groups.ensure(
                g["name"], hack_prefix=prefix, created_by=desired.get("created_by", ""),
            )
            groups_created.append({**grp, "was_created": created})

        # 3. Provision users in parallel
        results: list[UserProvisionResult] = []

        async def _provision_one(plan: UserPlan) -> UserProvisionResult:
            async with sem:
                return await self._provision_user(
                    plan, prefix=prefix, sku_ids=sku_ids,
                    group_ids=[g["id"] for g in groups_created],
                    tap_lifetime=desired.get("tap_lifetime_minutes", 480),
                )

        results = await asyncio.gather(*[_provision_one(p) for p in user_plans])

        succeeded = sum(1 for r in results if r.status == UserStatus.CREATED)
        failed = sum(1 for r in results if r.status == UserStatus.FAILED)

        await self._emit(ProvisionCompletedEvent(
            hack_prefix=prefix, succeeded=succeeded, failed=failed,
        ))

        return {
            "status": "completed",
            "prefix": prefix,
            "users": [r.model_dump() for r in results],
            "groups": groups_created,
            "licenses_resolved": {k: v[1] for k, v in resolved_licenses.items()},
            "succeeded": succeeded,
            "failed": failed,
        }

    async def _provision_user(
        self,
        plan: UserPlan,
        *,
        prefix: str,
        sku_ids: list[str],
        group_ids: list[str],
        tap_lifetime: int,
    ) -> UserProvisionResult:
        """Provision a single user end-to-end."""
        result = UserProvisionResult(user_principal_name=plan.user_principal_name)
        try:
            # Create user
            user_data, password, was_created = await self.users.ensure(
                plan.user_principal_name,
                plan.display_name,
                plan.mail_nickname,
                department=plan.team,
                company_name=prefix,
            )
            result.user_id = user_data.get("id", "")
            result.password = password

            if was_created:
                result.status = UserStatus.CREATED
                await self._emit(UserCreatedEvent(
                    hack_prefix=prefix,
                    user_id=result.user_id,
                    user_principal_name=plan.user_principal_name,
                ))
            else:
                result.status = UserStatus.EXISTING

            # Assign licenses
            if sku_ids:
                await self.licenses.assign(result.user_id, sku_ids)
                result.licenses_assigned = sku_ids

            # Add to groups
            for gid in group_ids:
                await self.groups.add_member(gid, result.user_id)
            result.groups_joined = group_ids

            # Issue TAP
            try:
                tap_data = await self.taps.issue(result.user_id, lifetime_minutes=tap_lifetime)
                result.tap_code = tap_data.get("temporaryAccessPass", "")
            except Exception as exc:
                logger.warning("TAP failed for %s: %s", plan.user_principal_name, exc)

        except Exception as exc:
            result.status = UserStatus.FAILED
            result.error = str(exc)
            logger.error("Failed to provision %s: %s", plan.user_principal_name, exc)

        return result

    async def reconcile(self, desired: JsonDict, actual: JsonDict) -> JsonDict:
        """Compare desired vs actual Entra state."""
        prefix = desired.get("prefix", "")
        desired_upns = {u.get("user_principal_name", u.get("userPrincipalName", "")) for u in desired.get("users", [])}
        actual_upns = {u.get("user_principal_name", u.get("userPrincipalName", "")) for u in actual.get("users", [])}

        missing = desired_upns - actual_upns
        extra = actual_upns - desired_upns

        return {
            "prefix": prefix,
            "has_drift": bool(missing or extra),
            "missing_users": sorted(missing),
            "extra_users": sorted(extra),
            "desired_count": len(desired_upns),
            "actual_count": len(actual_upns),
        }

    async def cleanup(self, state: JsonDict, *, dry_run: bool = False) -> JsonDict:
        """Delete users and groups for a hack."""
        prefix = state.get("prefix", "")
        await self._emit(CleanupStartedEvent(hack_prefix=prefix))

        users = state.get("users", [])
        groups = state.get("groups", [])

        if dry_run:
            return {
                "status": "dry_run",
                "would_delete_users": len(users),
                "would_delete_groups": len(groups),
            }

        deleted_users = 0
        for u in users:
            uid = u.get("user_id", u.get("userId", ""))
            if uid:
                try:
                    await self.users.delete(uid)
                    deleted_users += 1
                except Exception as exc:
                    logger.error("Failed to delete user %s: %s", uid, exc)

        deleted_groups = 0
        for g in groups:
            gid = g.get("group_id", g.get("id", ""))
            if gid:
                try:
                    await self.groups.delete(gid)
                    deleted_groups += 1
                except Exception as exc:
                    logger.error("Failed to delete group %s: %s", gid, exc)

        await self._emit(CleanupCompletedEvent(
            hack_prefix=prefix,
            deleted_users=deleted_users,
            deleted_groups=deleted_groups,
        ))

        return {
            "status": "completed",
            "deleted_users": deleted_users,
            "deleted_groups": deleted_groups,
        }

    async def discover(self, prefix: HackPrefix) -> JsonDict:
        """Discover existing Entra resources for a prefix."""
        users = await self.users.find_by_prefix(prefix)
        groups = await self.groups.find_by_prefix(prefix)
        return {
            "prefix": prefix,
            "users": users,
            "groups": groups,
        }

    async def preflight(self, desired: JsonDict) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        # License availability
        licenses = desired.get("licenses", [])
        if licenses:
            user_count = len(desired.get("users", []))
            avail = await self.licenses.check_availability(licenses, user_count)
            for check in avail:
                checks.append({
                    "check": f"license_{check['license']}",
                    "passed": check["sufficient"],
                    "detail": f"{check['available']} available, {check['required']} needed",
                })
        return checks

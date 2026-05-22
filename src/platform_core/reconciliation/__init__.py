"""Reconciliation engine — desired-state reconciliation
following the Kubernetes operator / Terraform plan-apply pattern.

Flow:
  1. detect_drift() — compare desired vs actual
  2. plan()        — generate a change plan from drift
  3. apply()       — execute the plan (with dry-run support)
"""

from __future__ import annotations

import logging
from datetime import datetime

from platform_core.core import HackPrefix, JsonDict
from platform_core.events import (
    DriftCleanEvent,
    DriftDetectedEvent,
    EventBus,
    ReconcileCompletedEvent,
    ReconcileStartedEvent,
)
from platform_core.models.reconciliation import (
    ChangeAction,
    ChangeItem,
    DriftCategory,
    DriftItem,
    DriftReport,
    ReconciliationPlan,
)
from platform_core.providers.base import ProviderRegistry

logger = logging.getLogger(__name__)


class ReconciliationEngine:
    """Desired-state reconciliation engine.

    Operates like:
      - ``terraform plan`` → ``plan()``
      - ``terraform apply`` → ``apply()``
      - Kubernetes controller loop → ``reconcile()`` (detect + plan + apply)
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        event_bus: EventBus | None = None,
    ) -> None:
        self._registry = registry
        self._event_bus = event_bus

    # ── Step 1: Detect Drift ─────────────────────────────────────

    async def detect_drift(
        self,
        prefix: HackPrefix,
        desired_state: JsonDict,
        actual_state: JsonDict,
    ) -> DriftReport:
        """Compare desired state with actual live state.

        *desired_state*: What the hack config says should exist.
        *actual_state*:  What actually exists in Entra/Azure/GitHub.
        """
        report = DriftReport(hack_prefix=prefix)

        # ── Users ────────────────────────────────────────────────
        desired_upns = {
            u.get("user_principal_name", u.get("userPrincipalName", ""))
            for u in desired_state.get("users", [])
        }
        actual_users = {
            u.get("userPrincipalName", u.get("user_principal_name", "")): u
            for u in actual_state.get("users", [])
        }
        actual_upns = set(actual_users.keys())

        report.state_user_count = len(desired_upns)
        report.live_user_count = len(actual_upns)

        # Missing users (desired but not in live)
        for upn in sorted(desired_upns - actual_upns):
            report.missing_users.append(upn)
            report.items.append(DriftItem(
                category=DriftCategory.USER_MISSING,
                resource_type="user",
                identifier=upn,
                expected="exists",
                actual="missing",
            ))

        # Extra users (in live but not desired)
        for upn in sorted(actual_upns - desired_upns):
            report.extra_users.append(upn)
            report.items.append(DriftItem(
                category=DriftCategory.USER_EXTRA,
                resource_type="user",
                identifier=upn,
                expected="absent",
                actual="exists",
            ))

        # Modified users (exist in both but properties differ)
        for upn in desired_upns & actual_upns:
            live = actual_users.get(upn, {})
            if not live.get("accountEnabled", True):
                report.modified_users.append(upn)
                report.items.append(DriftItem(
                    category=DriftCategory.USER_MODIFIED,
                    resource_type="user",
                    identifier=upn,
                    expected="enabled",
                    actual="disabled",
                    detail="accountEnabled=false",
                ))

        # ── Groups ───────────────────────────────────────────────
        desired_groups = {
            g.get("displayName", g.get("display_name", ""))
            for g in desired_state.get("groups", [])
        }
        actual_groups = {
            g.get("displayName", g.get("display_name", ""))
            for g in actual_state.get("groups", [])
        }

        report.state_group_count = len(desired_groups)
        report.live_group_count = len(actual_groups)

        for name in sorted(desired_groups - actual_groups):
            report.missing_groups.append(name)
            report.items.append(DriftItem(
                category=DriftCategory.GROUP_MISSING,
                resource_type="group",
                identifier=name,
                expected="exists",
                actual="missing",
            ))

        for name in sorted(actual_groups - desired_groups):
            report.extra_groups.append(name)
            report.items.append(DriftItem(
                category=DriftCategory.GROUP_EXTRA,
                resource_type="group",
                identifier=name,
                expected="absent",
                actual="exists",
            ))

        report.has_drift = bool(report.items)

        # Emit event
        if self._event_bus:
            if report.has_drift:
                await self._event_bus.publish(DriftDetectedEvent(
                    hack_prefix=prefix, drift_summary=report.summary,
                ))
            else:
                await self._event_bus.publish(DriftCleanEvent(hack_prefix=prefix))

        return report

    # ── Step 2: Generate Plan ────────────────────────────────────

    async def plan(
        self,
        prefix: HackPrefix,
        drift_report: DriftReport,
        *,
        auto_fix_missing: bool = True,
        auto_remove_extra: bool = False,
        auto_fix_modified: bool = True,
    ) -> ReconciliationPlan:
        """Generate a change plan from a drift report.

        By default:
          - Missing resources → CREATE (re-provision)
          - Extra resources → SKIP (manual review)
          - Modified resources → UPDATE (re-enable)
        """
        plan = ReconciliationPlan(
            hack_prefix=prefix,
            drift_report_id=drift_report.id,
        )

        for item in drift_report.items:
            if item.category == DriftCategory.USER_MISSING and auto_fix_missing:
                plan.changes.append(ChangeItem(
                    action=ChangeAction.CREATE,
                    resource_type="user",
                    identifier=item.identifier,
                    provider="entra",
                    risk_level="medium",
                    details={"reason": "User exists in state but missing from Entra"},
                ))
            elif item.category == DriftCategory.USER_EXTRA and auto_remove_extra:
                plan.changes.append(ChangeItem(
                    action=ChangeAction.DELETE,
                    resource_type="user",
                    identifier=item.identifier,
                    provider="entra",
                    risk_level="high",
                    details={"reason": "User exists in Entra but not in state"},
                ))
            elif item.category == DriftCategory.USER_MODIFIED and auto_fix_modified:
                plan.changes.append(ChangeItem(
                    action=ChangeAction.ENABLE,
                    resource_type="user",
                    identifier=item.identifier,
                    provider="entra",
                    risk_level="low",
                    details={"reason": item.detail},
                ))
            elif item.category == DriftCategory.GROUP_MISSING and auto_fix_missing:
                plan.changes.append(ChangeItem(
                    action=ChangeAction.CREATE,
                    resource_type="group",
                    identifier=item.identifier,
                    provider="entra",
                    risk_level="medium",
                ))
            elif item.category == DriftCategory.GROUP_EXTRA and auto_remove_extra:
                plan.changes.append(ChangeItem(
                    action=ChangeAction.DELETE,
                    resource_type="group",
                    identifier=item.identifier,
                    provider="entra",
                    risk_level="high",
                ))
            else:
                plan.changes.append(ChangeItem(
                    action=ChangeAction.SKIP,
                    resource_type=item.resource_type,
                    identifier=item.identifier,
                    details={"reason": f"Auto-fix disabled for {item.category.value}"},
                ))

        plan.total_changes = len(plan.changes)
        return plan

    # ── Step 3: Apply Plan ───────────────────────────────────────

    async def apply(
        self,
        plan: ReconciliationPlan,
        *,
        dry_run: bool = False,
    ) -> ReconciliationPlan:
        """Execute a reconciliation plan.

        If *dry_run* is True, marks the plan as executed without
        actually calling providers.
        """
        if self._event_bus:
            await self._event_bus.publish(ReconcileStartedEvent(
                hack_prefix=plan.hack_prefix,
                total_changes=plan.total_changes,
            ))

        applied = 0
        failed = 0
        skipped = 0

        for change in plan.changes:
            if change.action == ChangeAction.SKIP:
                skipped += 1
                continue

            if dry_run:
                change.executed = True
                change.success = True
                applied += 1
                continue

            try:
                await self._execute_change(change)
                change.executed = True
                change.success = True
                applied += 1
            except Exception as exc:
                change.executed = True
                change.success = False
                change.error = str(exc)
                failed += 1
                logger.error("Failed to apply change %s %s: %s", change.action.value, change.identifier, exc)

        plan.executed = True
        plan.executed_at = datetime.utcnow()
        plan.applied_changes = applied
        plan.failed_changes = failed
        plan.skipped_changes = skipped
        plan.dry_run = dry_run

        if self._event_bus:
            await self._event_bus.publish(ReconcileCompletedEvent(
                hack_prefix=plan.hack_prefix,
                applied=applied,
                failed=failed,
            ))

        return plan

    async def _execute_change(self, change: ChangeItem) -> None:
        """Execute a single change via the appropriate provider."""
        if not self._registry.has(change.provider):
            raise ValueError(f"Provider '{change.provider}' not registered")

        provider = self._registry.get(change.provider)

        if change.action == ChangeAction.CREATE:
            await provider.provision(
                {"type": change.resource_type, "identifier": change.identifier, **change.details},
            )
        elif change.action == ChangeAction.DELETE:
            await provider.cleanup(
                {"type": change.resource_type, "identifier": change.identifier, **change.details},
            )
        elif change.action == ChangeAction.ENABLE:
            # Provider-specific enable
            if hasattr(provider, "users"):
                await provider.users.enable(change.identifier)
        elif change.action == ChangeAction.DISABLE:
            if hasattr(provider, "users"):
                await provider.users.disable(change.identifier)

    # ── Convenience: Full reconcile cycle ────────────────────────

    async def reconcile(
        self,
        prefix: HackPrefix,
        desired_state: JsonDict,
        actual_state: JsonDict,
        *,
        dry_run: bool = True,
        auto_fix_missing: bool = True,
        auto_remove_extra: bool = False,
    ) -> tuple[DriftReport, ReconciliationPlan]:
        """Full reconciliation: detect → plan → apply."""
        drift = await self.detect_drift(prefix, desired_state, actual_state)
        if not drift.has_drift:
            return drift, ReconciliationPlan(hack_prefix=prefix)

        change_plan = await self.plan(
            prefix, drift,
            auto_fix_missing=auto_fix_missing,
            auto_remove_extra=auto_remove_extra,
        )
        executed = await self.apply(change_plan, dry_run=dry_run)
        return drift, executed

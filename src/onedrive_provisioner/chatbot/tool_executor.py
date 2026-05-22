"""Tool executor that maps chatbot tool calls to real backend operations."""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Dict, Optional, Tuple

from onedrive_provisioner.auth import MsalTokenProvider
from onedrive_provisioner.config import AzureConfig
from onedrive_provisioner.graph import GraphClient
from onedrive_provisioner.entra import (
    TenantService, DiscoveryService,
    run_preflight, EntraConfig,
)
from onedrive_provisioner.entra.models import license_display_name


def _make_tp(creds: Tuple[str, str, str]) -> MsalTokenProvider:
    t, c, s = creds
    return MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))


MUTATION_TOOLS = {
    "provision_users",
    "regenerate_tap",
    "assign_licenses",
    "cleanup_hack",
    "delete_hack_state",
    "set_hack_end_date",
    "schedule_hack_provision",
    "cancel_scheduled_job",
    "enable_github_access",
    "disable_github_access",
    "assign_rbac_permissions",
    "apply_readonly",
    "reset_user_password",
    "modify_hack_dates",
    "repair_groups",
    "repair_licenses",
    "expand_hack",
}

SECRET_KEYS = {
    "password",
    "tap",
    "temporaryaccesspass",
    "client_secret",
    "secret",
    "token",
    "access_token",
    "refresh_token",
}


def _mutations_enabled() -> bool:
    return os.environ.get("CHATBOT_ENABLE_MUTATION_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SECRET_KEYS:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class ToolExecutor:
    """Executes chatbot tool calls using SPN credentials and in-memory state."""

    def __init__(
        self,
        creds: Tuple[str, str, str],
        get_state_manager: Callable,
        entra_sessions: dict,
        entra_lock: Any,
        upload_jobs: dict,
        jobs_lock: Any,
        docs_store: Optional[dict] = None,
    ):
        self._creds = creds
        self._get_mgr = get_state_manager
        self._entra_sessions = entra_sessions
        self._entra_lock = entra_lock
        self._upload_jobs = upload_jobs
        self._jobs_lock = jobs_lock
        self._docs_store = docs_store

    def __call__(self, tool_name: str, args: Dict[str, Any]) -> Any:
        if tool_name in MUTATION_TOOLS and not _mutations_enabled():
            return {
                "error": (
                    "AI assistant is read-only by default. Use the UI/API workflow with server-side "
                    "confirmation for privileged operations, or explicitly enable CHATBOT_ENABLE_MUTATION_TOOLS."
                )
            }
        handler = getattr(self, f"_tool_{tool_name}", None)
        if not handler:
            return {"error": f"Unknown tool: {tool_name}"}
        return _redact(handler(**args))

    # ── Tools ──

    def _tool_list_saved_hacks(self) -> Any:
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured (AZURE_STORAGE_CONNECTION_STRING not set)"}
        return mgr.list_hacks()

    def _tool_get_hack_state(self, prefix: str) -> Any:
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}
        return _redact(state)

    def _tool_get_hack_users_summary(self, prefix: str) -> Any:
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}
        users = state.get("users", [])
        return {
            "prefix": prefix,
            "totalUsers": len(users),
            "created": sum(1 for u in users if u.get("status") == "created"),
            "failed": sum(1 for u in users if u.get("status") == "failed"),
            "existing": sum(1 for u in users if u.get("status") == "existing"),
            "admins": sum(1 for u in users if u.get("isAdmin")),
            "upns": [u.get("userPrincipalName", "") for u in users],
        }

    def _tool_expand_hack(
        self,
        prefix: str,
        addTeams: int = 0,
        addParticipantsPerTeam: int = 0,
        addAdmins: int = 0,
        dryRun: bool = False,
        password: str = "",
        randomPasswords: bool = False,
    ) -> Any:
        """Expand an existing hack by adding more teams, more participants per
        team, and/or more admins. Indices continue from the current max — no
        existing user is touched (orchestrator runs with skip_existing=True).
        Returns a session_id; caller polls get_session_status to monitor.
        """
        from flask import current_app
        try:
            app = current_app._get_current_object()  # noqa: SLF001
        except Exception:
            return {"error": "No Flask app context — cannot invoke expand endpoint."}

        payload = {
            "tenant_id": self._creds[0],
            "client_id": self._creds[1],
            "client_secret": self._creds[2],
            "addTeams": int(addTeams or 0),
            "addParticipantsPerTeam": int(addParticipantsPerTeam or 0),
            "addAdmins": int(addAdmins or 0),
            "dryRun": bool(dryRun),
        }
        if password:
            payload["password"] = password
        elif randomPasswords:
            payload["randomPasswords"] = True
        with app.test_client() as client:
            resp = client.post(f"/api/hack-state/{prefix}/expand", json=payload)
            try:
                body = resp.get_json()
            except Exception:
                body = {"error": "Invalid JSON from expand endpoint", "status": resp.status_code}
            if resp.status_code >= 400:
                if not isinstance(body, dict):
                    body = {"error": str(body), "status": resp.status_code}
                body.setdefault("status", resp.status_code)
                return body
            return body

    def _tool_get_subscription_cost(
        self,
        subscription: str,
        startDate: Optional[str] = None,
        endDate: Optional[str] = None,
    ) -> Any:
        """Fetch actual Azure cost for a single subscription, identified by GUID
        OR display name (case-insensitive contains match).

        - ``subscription`` may be a subscription GUID or a (partial) display name.
        - ``startDate`` / ``endDate`` are ISO ``YYYY-MM-DD``. Defaults: last 30 days.
        Requires the SPN to have Reader + Cost Management Reader on the sub.
        """
        from datetime import datetime, timedelta, timezone
        from onedrive_provisioner.entra.cost_service import CostManagementService

        if not subscription or not isinstance(subscription, str):
            return {"error": "subscription is required (GUID or display name)"}
        if not endDate:
            endDate = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not startDate:
            startDate = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

        tp = _make_tp(self._creds)

        async def _run() -> dict:
            async with CostManagementService(tp) as cost_svc:
                # Resolve the subscription: if it looks like a GUID, use it
                # directly; otherwise list accessible subs and substring-match
                # the display name.
                sub_id = subscription.strip()
                display_name = None
                is_guid = (
                    len(sub_id) == 36 and sub_id.count("-") == 4
                )
                if not is_guid:
                    accessible = await cost_svc.list_accessible_subscriptions()
                    needle = sub_id.lower()
                    matches = [
                        s for s in accessible
                        if needle in (s.get("displayName") or "").lower()
                    ]
                    if not matches:
                        return {
                            "error": f"No accessible subscription matched '{subscription}'.",
                            "accessibleCount": len(accessible),
                        }
                    if len(matches) > 1:
                        return {
                            "error": f"Ambiguous: '{subscription}' matched {len(matches)} subscriptions.",
                            "candidates": [
                                {"subscriptionId": m.get("subscriptionId"), "displayName": m.get("displayName")}
                                for m in matches[:10]
                            ],
                        }
                    sub_id = matches[0].get("subscriptionId")
                    display_name = matches[0].get("displayName")
                else:
                    # Look up display name (best-effort; ignore failures)
                    try:
                        accessible = await cost_svc.list_accessible_subscriptions()
                        for s in accessible:
                            if s.get("subscriptionId") == sub_id:
                                display_name = s.get("displayName")
                                break
                    except Exception:
                        pass
                row = await cost_svc.query_subscription_cost(
                    sub_id, start_date=startDate, end_date=endDate,
                )
                if display_name and not row.get("displayName"):
                    row["displayName"] = display_name
                return row

        try:
            return asyncio.run(_run())
        except Exception as exc:
            return {"error": f"Cost query failed: {exc}"}

    def _tool_generate_hack_report(
        self,
        prefix: str,
        currency: str = "USD",
        licenseUnitCosts: Optional[dict] = None,
        subscriptionCosts: Optional[list] = None,
        fetchSubscriptionCosts: bool = True,
        startDate: Optional[str] = None,
        endDate: Optional[str] = None,
        budget: Optional[float] = None,
        forceRefresh: bool = False,
    ) -> Any:
        """Delegate to the production /api/hack-state/<prefix>/report endpoint
        so the chatbot uses the SAME pipeline as the UI: group-RBAC + user-RBAC
        sub discovery, displayName + team-map enrichment, cost cache, state
        persistence, and budget tracking. Any feature added to that route is
        automatically available here.
        """
        from datetime import datetime, timezone
        from flask import current_app

        try:
            app = current_app._get_current_object()  # noqa: SLF001
        except Exception:
            return {"error": "No Flask app context — cannot invoke report endpoint."}

        if not endDate:
            endDate = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        payload = {
            "tenant_id": self._creds[0],
            "client_id": self._creds[1],
            "client_secret": self._creds[2],
            "currency": (currency or "USD"),
            "fetchSubscriptionCosts": bool(fetchSubscriptionCosts),
            "forceRefresh": bool(forceRefresh),
            "endDate": endDate,
        }
        if startDate:
            payload["startDate"] = startDate
        if budget is not None:
            payload["budget"] = budget
        if subscriptionCosts:
            payload["subscriptionCosts"] = subscriptionCosts
            payload["subscriptionIds"] = [
                c.get("subscriptionId") for c in subscriptionCosts
                if c.get("subscriptionId")
            ]
        if licenseUnitCosts:
            payload["licenseUnitCosts"] = licenseUnitCosts

        with app.test_client() as client:
            resp = client.post(
                f"/api/hack-state/{prefix}/report",
                json=payload,
            )
            try:
                body = resp.get_json()
            except Exception:
                body = {"error": "Invalid JSON from report endpoint", "status": resp.status_code}
            if resp.status_code >= 400:
                if not isinstance(body, dict):
                    body = {"error": str(body), "status": resp.status_code}
                body.setdefault("status", resp.status_code)
                return body
            return body

    def _tool_get_provisioning_sessions(self) -> Any:
        with self._entra_lock:
            return [
                {k: v for k, v in s.items() if k not in ("result", "partial_users")}
                for s in sorted(
                    self._entra_sessions.values(),
                    key=lambda s: s["created_at"],
                    reverse=True,
                )
            ]

    def _tool_get_session_status(self, session_id: str) -> Any:
        with self._entra_lock:
            s = self._entra_sessions.get(session_id)
            if not s:
                return {"error": "Session not found"}
            return _redact(s)

    def _tool_detect_tenant_info(self) -> Any:
        tp = _make_tp(self._creds)

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
        return {
            "domain": domain,
            "tapMaxLifetimeMinutes": tap_max,
            "subscribedSkus": [
                {
                    "skuPartNumber": s.get("skuPartNumber", ""),
                    "displayName": license_display_name(s.get("skuPartNumber", "")),
                    "consumedUnits": s.get("consumedUnits", 0),
                    "prepaidUnits": s.get("prepaidUnits", {}),
                }
                for s in skus
            ],
        }

    def _tool_run_preflight_check(self, prefix: str, domain: str, **kwargs) -> Any:
        tp = _make_tp(self._creds)
        cfg_dict = {
            "prefix": prefix,
            "domain": domain,
            "teams": kwargs.get("teams", 2),
            "usersPerTeam": kwargs.get("usersPerTeam", 5),
            "adminUsers": kwargs.get("adminUsers", 1),
            "mode": kwargs.get("mode", "team"),
            "licenses": kwargs.get("licenses", []),
            "assignLicensesToAdmins": kwargs.get("assignLicensesToAdmins", False),
        }
        cfg = EntraConfig.from_dict(cfg_dict)

        async def _run():
            async with GraphClient(tp) as g:
                return await run_preflight(g, None, cfg, subscription_ids=None)

        return asyncio.run(_run())

    def _tool_provision_users(self, prefix: str, domain: str, **kwargs) -> Any:
        from onedrive_provisioner.config import AzureConfig as _AzCfg
        from onedrive_provisioner.entra import EntraOrchestrator, EntraConfig
        from onedrive_provisioner.storage import HackStateManager

        cfg_dict = {
            "prefix": prefix,
            "domain": domain,
            "teams": kwargs.get("teams", 0),
            "usersPerTeam": kwargs.get("usersPerTeam", 2),
            "adminUsers": kwargs.get("adminUsers", 1),
            "mode": kwargs.get("mode", "flat"),
            "licenses": kwargs.get("licenses", []),
            "assignLicensesToAdmins": kwargs.get("assignLicensesToAdmins", False),
            "hackName": kwargs.get("hackName", prefix),
            "dryRun": kwargs.get("dryRun", False),
            "initialPassword": kwargs.get("initialPassword", ""),
        }
        t, c, s = self._creds
        cfg = EntraConfig.from_dict(cfg_dict)
        azure = _AzCfg(tenant_id=t, client_id=c, client_secret=s)
        orch = EntraOrchestrator(azure, concurrency=6)

        report = asyncio.run(orch.provision(cfg))
        report_dict = report.to_dict()

        # Persist to blob storage
        try:
            mgr = self._get_mgr()
            if mgr:
                state = HackStateManager.build_state_from_report(cfg_dict, report_dict)
                mgr.save_state(prefix, state)
        except Exception as exc:
            report_dict["_blobWarning"] = f"State saved failed: {exc}"

        return report_dict

    def _tool_regenerate_tap(self, prefix: str, **kwargs) -> Any:
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}

        target_upns = kwargs.get("users")
        tap_lifetime = int(kwargs.get("tapLifetime", 120))
        tp = _make_tp(self._creds)

        async def _run():
            from onedrive_provisioner.entra.tap_service import TapService
            results = []
            async with GraphClient(tp) as g:
                tap_svc = TapService(g, lifetime_minutes=tap_lifetime)
                for u in state.get("users", []):
                    upn = u.get("userPrincipalName", "")
                    uid = u.get("userId", "")
                    if not uid:
                        continue
                    if target_upns and upn not in target_upns:
                        continue
                    tap = await tap_svc.issue(uid)
                    results.append({
                        "userPrincipalName": upn,
                        "tap": tap.get("temporaryAccessPass", "") if tap else "",
                        "status": "ok" if tap else "failed",
                    })
            return results

        results = asyncio.run(_run())
        mgr.update_user_taps(prefix, results)
        return {"results": results, "updatedUsers": len(results)}

    def _tool_assign_licenses(self, prefix: str, licenses: list, **kwargs) -> Any:
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}

        target_upns = kwargs.get("users")
        include_admins = bool(kwargs.get("includeAdmins", False))
        tp = _make_tp(self._creds)

        async def _run():
            from onedrive_provisioner.entra.license_service import LicenseService
            results = []
            async with GraphClient(tp) as g:
                lic_svc = LicenseService(g)
                sku_map = await lic_svc.resolve(licenses)
                sku_ids = [sid for (sid, _) in sku_map.values()]
                if not sku_ids:
                    return results
                for u in state.get("users", []):
                    upn = u.get("userPrincipalName", "")
                    uid = u.get("userId", "")
                    if not uid or (u.get("isAdmin") and not include_admins):
                        continue
                    if target_upns and upn not in target_upns:
                        continue
                    assigned = await lic_svc.assign(uid, sku_ids)
                    existing = u.get("licenses", [])
                    merged = list(set(existing + licenses))
                    results.append({
                        "userPrincipalName": upn,
                        "licenses": merged,
                        "status": "ok" if assigned else "failed",
                    })
            return results

        results = asyncio.run(_run())
        mgr.update_user_licenses(prefix, results)
        return {"results": results, "updatedUsers": len(results)}

    def _tool_discover_hack_resources(self, prefix: str) -> Any:
        tp = _make_tp(self._creds)

        async def _run():
            async with GraphClient(tp) as g:
                return await DiscoveryService(g).discover(prefix)

        return asyncio.run(_run())

    def _tool_list_upload_jobs(self) -> Any:
        with self._jobs_lock:
            return [
                {k: v for k, v in j.items() if k != "result"}
                for j in sorted(
                    self._upload_jobs.values(),
                    key=lambda j: j["created_at"],
                    reverse=True,
                )
            ]

    def _tool_cleanup_hack(self, prefix: str) -> Any:
        """Discover and delete all users + groups for a hack, then archive blob state."""
        tp = _make_tp(self._creds)

        async def _run():
            from onedrive_provisioner.entra.cleanup_service import CleanupService
            # Discover resources first
            async with GraphClient(tp) as g:
                discovered = await DiscoveryService(g).discover(prefix)

            user_ids = [u["id"] for u in discovered.get("users", []) if u.get("id")]
            group_ids = [g["id"] for g in discovered.get("groups", []) if g.get("id")]

            results = {"users": [], "groups": [], "discovered_users": len(user_ids), "discovered_groups": len(group_ids)}

            async with GraphClient(tp) as g:
                cleaner = CleanupService(g)
                if user_ids:
                    results["users"] = await cleaner.delete_users(user_ids)
                if group_ids:
                    results["groups"] = await cleaner.delete_groups(group_ids)

            return results

        results = asyncio.run(_run())

        # Archive blob state for historical reporting.
        try:
            mgr = self._get_mgr()
            if mgr:
                archived = mgr.archive_state(prefix, cleanup_result=results)
                results["blob_state_archived"] = archived
                results["blob_state_deleted"] = False
        except Exception as exc:
            results["blob_state_error"] = str(exc)

        return results

    def _tool_set_hack_end_date(self, prefix: str, end_date: str,
                                 subscription_ids: list = None,
                                 readonly_date: str = None,
                                 mode: str = "team") -> Any:
        """Set an auto-cleanup (and optional read-only) end date for a hack."""
        from onedrive_provisioner.scheduler import HackScheduler
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}
        # We need a scheduler instance — build one inline
        scheduler = HackScheduler(
            get_state_manager=self._get_mgr,
            run_provision=lambda *a: None,
            run_cleanup=lambda *a: None,
            run_readonly=lambda *a, **kw: None,
        )
        t, c, s = self._creds
        jobs = scheduler.set_hack_end_date(prefix, end_date, {
            "tenant_id": t, "client_id": c, "client_secret": s,
        }, subscription_ids=subscription_ids or [],
           readonly_date=readonly_date, mode=mode)
        return {"message": f"End date set to {end_date} for '{prefix}'",
                "endDate": end_date,
                "readonlyDate": readonly_date,
                "subscriptionIds": subscription_ids or [],
                "jobs": [j.to_dict() for j in jobs]}

    def _tool_schedule_hack_provision(self, scheduled_at: str, config: dict) -> Any:
        """Schedule a hack to be provisioned at a future date."""
        from onedrive_provisioner.scheduler import HackScheduler
        if not config.get("domain"):
            return {"error": "config.domain is required"}
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        scheduler = HackScheduler(
            get_state_manager=self._get_mgr,
            run_provision=lambda *a: None,
            run_cleanup=lambda *a: None,
        )
        t, c, s = self._creds
        job = scheduler.schedule_provision(scheduled_at, config, {
            "tenant_id": t, "client_id": c, "client_secret": s,
        })
        return {"message": f"Hack '{config.get('prefix', '?')}' scheduled for {scheduled_at}",
                "job": job.to_dict()}

    def _tool_list_scheduled_jobs(self, status: str = None) -> Any:
        """List all scheduled jobs."""
        from onedrive_provisioner.scheduler import HackScheduler
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        scheduler = HackScheduler(
            get_state_manager=self._get_mgr,
            run_provision=lambda *a: None,
            run_cleanup=lambda *a: None,
        )
        jobs = scheduler.list_jobs(status=status)
        return [j.to_dict() for j in jobs]

    def _tool_cancel_scheduled_job(self, job_id: str) -> Any:
        """Cancel a pending scheduled job."""
        from onedrive_provisioner.scheduler import HackScheduler
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        scheduler = HackScheduler(
            get_state_manager=self._get_mgr,
            run_provision=lambda *a: None,
            run_cleanup=lambda *a: None,
        )
        if scheduler.cancel_job(job_id):
            return {"message": f"Job {job_id} cancelled"}
        return {"error": "Job not found or not in pending status"}

    def _tool_delete_hack_state(self, prefix: str) -> Any:
        """Delete only the blob state for a hack."""
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        deleted = mgr.delete_state(prefix)
        if deleted:
            return {"message": f"State for '{prefix}' deleted from blob storage."}
        return {"error": f"No state found for prefix '{prefix}'"}

    def _tool_generate_admin_guide(self, prefix: str) -> Any:
        """Generate Admin/Trainer Guide .docx for a hack."""
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}

        from onedrive_provisioner.docgen import DocGenerator
        gen = DocGenerator()
        doc_bytes = gen.generate(state)
        filename = gen.get_filename(state)

        # Store the generated doc in the shared docs store
        if hasattr(self, '_docs_store') and self._docs_store is not None:
            import uuid as _uuid
            doc_id = str(_uuid.uuid4())
            self._docs_store[doc_id] = {
                "filename": filename,
                "data": doc_bytes,
                "prefix": prefix,
            }
            return {
                "message": f"Admin Guide generated: {filename} ({len(doc_bytes)} bytes)",
                "filename": filename,
                "download_url": f"/api/generated-docs/{doc_id}",
                "size_bytes": len(doc_bytes),
            }

        return {
            "message": f"Admin Guide generated: {filename} ({len(doc_bytes)} bytes). "
                       "Use the /api/generate-doc endpoint to download it.",
            "filename": filename,
            "size_bytes": len(doc_bytes),
        }

    def _tool_enable_github_access(self, prefix: str, **kwargs) -> Any:
        """Add hack users to GitHub-EMU Entra groups and trigger sync."""
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}

        include_admins = bool(kwargs.get("includeAdmins", False))
        with_copilot = bool(kwargs.get("withCopilot", False))
        with_ghas = bool(kwargs.get("withGhas", False))

        emails = []
        for u in state.get("users", []):
            if u.get("isAdmin") and not include_admins:
                continue
            upn = u.get("userPrincipalName", "")
            if upn:
                emails.append(upn)

        if not emails:
            return {"error": "No users found in hack state"}

        async def _run():
            from onedrive_provisioner.github_emu import GitHubEnabler
            async with GitHubEnabler() as gh:
                return await gh.enable_users(
                    emails,
                    with_copilot=with_copilot,
                    with_ghas=with_ghas,
                    trigger_sync=True,
                )

        result = asyncio.run(_run())
        return {
            "message": f"GitHub access enabled for {len(emails)} users from '{prefix}'",
            "emailCount": len(emails),
            "withCopilot": with_copilot,
            "withGhas": with_ghas,
            "result": result,
        }

    def _tool_disable_github_access(self, prefix: str, **kwargs) -> Any:
        """Remove hack users from GitHub-EMU groups and trigger sync."""
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}

        with_copilot = bool(kwargs.get("withCopilot", False))
        with_ghas = bool(kwargs.get("withGhas", False))

        emails = [u.get("userPrincipalName", "") for u in state.get("users", []) if u.get("userPrincipalName")]
        if not emails:
            return {"error": "No users found in hack state"}

        async def _run():
            from onedrive_provisioner.github_emu import GitHubEnabler
            async with GitHubEnabler() as gh:
                return await gh.disable_users(
                    emails,
                    with_copilot=with_copilot,
                    with_ghas=with_ghas,
                    trigger_sync=True,
                )

        result = asyncio.run(_run())
        return {
            "message": f"GitHub access disabled for {len(emails)} users from '{prefix}'",
            "emailCount": len(emails),
            "result": result,
        }

    def _tool_assign_rbac_permissions(self, prefix: str, subscriptionIds: list, **kwargs) -> Any:
        """Assign RBAC role on subscriptions to hack groups/users."""
        role = kwargs.get("role", "Reader")
        target_scope = kwargs.get("targetScope", "teams")
        tp = _make_tp(self._creds)

        async def _run():
            from onedrive_provisioner.entra.rbac_service import RbacService
            # Discover principals
            async with GraphClient(tp) as g:
                discovered = await DiscoveryService(g).discover(prefix)

            # Select principals based on target scope
            principals = []
            groups = discovered.get("groups", [])
            users = discovered.get("users", [])

            if target_scope in ("teams", "teams-admins"):
                principals.extend(
                    {"id": g["id"], "type": "Group", "displayName": g.get("displayName", "")}
                    for g in groups if g.get("id")
                )
            elif target_scope == "admins":
                principals.extend(
                    {"id": g["id"], "type": "Group", "displayName": g.get("displayName", "")}
                    for g in groups if g.get("id") and "admin" in g.get("displayName", "").lower()
                )
            elif target_scope == "users":
                principals.extend(
                    {"id": u["id"], "type": "User", "displayName": u.get("userPrincipalName", "")}
                    for u in users if u.get("id")
                )

            if not principals:
                return {"error": f"No principals found for scope '{target_scope}' with prefix '{prefix}'"}

            async with RbacService(tp) as rbac:
                results = []
                for sub_id in subscriptionIds:
                    for p in principals:
                        try:
                            ok = await rbac.assign_role(sub_id, p["id"], role)
                            results.append({
                                "subscription": sub_id,
                                "principal": p["displayName"],
                                "role": role,
                                "status": "assigned" if ok else "already_exists",
                            })
                        except Exception as exc:
                            results.append({
                                "subscription": sub_id,
                                "principal": p["displayName"],
                                "role": role,
                                "status": "failed",
                                "error": str(exc),
                            })
                return results

        results = asyncio.run(_run())
        return {
            "message": f"RBAC '{role}' assignment completed for '{prefix}' on {len(subscriptionIds)} subscription(s)",
            "results": results,
        }

    def _tool_apply_readonly(self, prefix: str, subscriptionIds: list, **kwargs) -> Any:
        """Apply read-only mode: remove Owner/Contributor, grant Reader."""
        mode = kwargs.get("mode", "team")
        tp = _make_tp(self._creds)

        async def _run():
            from onedrive_provisioner.entra.rbac_service import RbacService
            async with GraphClient(tp) as g:
                discovered = await DiscoveryService(g).discover(prefix)

            if mode == "flat":
                principals = [
                    {"id": u["id"], "type": "User", "displayName": u.get("userPrincipalName", "")}
                    for u in discovered.get("users", []) if u.get("id")
                ]
            else:
                principals = [
                    {"id": g["id"], "type": "Group", "displayName": g.get("displayName", "")}
                    for g in discovered.get("groups", []) if g.get("id")
                ]

            results = []
            async with RbacService(tp) as rbac:
                for sub_id in subscriptionIds:
                    for p in principals:
                        try:
                            removed = await rbac.remove_write_roles(sub_id, p["id"])
                            reader_ok = await rbac.assign_role(sub_id, p["id"], "Reader")
                            results.append({
                                "subscription": sub_id,
                                "principal": p["displayName"],
                                "removedRoles": removed,
                                "readerAssigned": reader_ok,
                                "status": "ok",
                            })
                        except Exception as exc:
                            results.append({
                                "subscription": sub_id,
                                "principal": p["displayName"],
                                "status": "failed",
                                "error": str(exc),
                            })
            return results

        results = asyncio.run(_run())
        return {
            "message": f"Read-only mode applied for '{prefix}' on {len(subscriptionIds)} subscription(s)",
            "results": results,
        }

    def _tool_reset_user_password(self, prefix: str, **kwargs) -> Any:
        """Reset passwords for users in a saved hack."""
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}

        target_upns = kwargs.get("users")
        custom_password = kwargs.get("password")
        tp = _make_tp(self._creds)

        async def _run():
            from onedrive_provisioner.entra.user_service import UserService
            results = []
            async with GraphClient(tp) as g:
                user_svc = UserService(g)
                for u in state.get("users", []):
                    upn = u.get("userPrincipalName", "")
                    uid = u.get("userId", "")
                    if not uid:
                        continue
                    if target_upns and upn not in target_upns:
                        continue
                    new_pw = await user_svc.reset_password(uid, password=custom_password)
                    results.append({
                        "userPrincipalName": upn,
                        "password": new_pw or "",
                        "status": "ok" if new_pw else "failed",
                    })
            return results

        results = asyncio.run(_run())
        mgr.update_user_passwords(prefix, results)
        return {"results": results, "updatedUsers": len(results)}

    def _tool_modify_hack_dates(self, prefix: str, deleteDate: str, **kwargs) -> Any:
        """Update lifecycle dates and reschedule automation for a hack."""
        from onedrive_provisioner.scheduler import HackScheduler
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}

        hack_start = kwargs.get("hackStartDate", "")
        hack_date = kwargs.get("hackDate", "")
        readonly_date = kwargs.get("readonlyDate", "")
        sub_ids = kwargs.get("subscriptionIds") or []

        scheduler = HackScheduler(
            get_state_manager=self._get_mgr,
            run_provision=lambda *a: None,
            run_cleanup=lambda *a: None,
            run_readonly=lambda *a, **kw: None,
        )
        t, c, s = self._creds
        jobs = scheduler.set_hack_end_date(
            prefix, deleteDate,
            {"tenant_id": t, "client_id": c, "client_secret": s},
            subscription_ids=sub_ids,
            readonly_date=readonly_date or None,
            mode=kwargs.get("mode", "team"),
            metadata={
                "hackStartDate": hack_start,
                "hackDate": hack_date,
                "deleteDate": deleteDate,
            },
        )
        return {
            "message": f"Dates updated for '{prefix}'",
            "hackStartDate": hack_start,
            "hackDate": hack_date,
            "readonlyDate": readonly_date,
            "deleteDate": deleteDate,
            "jobs": [j.to_dict() for j in jobs],
        }

    def _tool_repair_groups(self, prefix: str) -> Any:
        """Verify and repair group memberships for users in a hack."""
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}

        tp = _make_tp(self._creds)

        async def _run():
            from onedrive_provisioner.entra.group_service import GroupService
            async with GraphClient(tp) as g:
                gs = GroupService(g)
                return await gs.repair_memberships(state)

        result = asyncio.run(_run())
        return {
            "message": f"Group repair completed for '{prefix}'",
            "result": result,
        }

    def _tool_repair_licenses(self, prefix: str) -> Any:
        """Re-assign expected licenses to users missing them."""
        mgr = self._get_mgr()
        if not mgr:
            return {"error": "Storage not configured"}
        state = mgr.get_state(prefix)
        if not state:
            return {"error": f"No state found for prefix '{prefix}'"}

        tp = _make_tp(self._creds)
        expected_licenses = state.get("config", {}).get("licenses") or []
        if not expected_licenses:
            return {"message": "No licenses configured for this hack — nothing to repair."}

        async def _run():
            from onedrive_provisioner.entra.license_service import LicenseService
            results = []
            async with GraphClient(tp) as g:
                lic_svc = LicenseService(g)
                sku_map = await lic_svc.resolve(expected_licenses)
                sku_ids = [sid for (sid, _) in sku_map.values()]
                if not sku_ids:
                    return [{"error": "Could not resolve any license SKUs"}]
                for u in state.get("users", []):
                    uid = u.get("userId", "")
                    upn = u.get("userPrincipalName", "")
                    if not uid:
                        continue
                    try:
                        assigned = await lic_svc.assign(uid, sku_ids)
                        results.append({
                            "userPrincipalName": upn,
                            "status": "repaired" if assigned else "already_ok",
                        })
                    except Exception as exc:
                        results.append({
                            "userPrincipalName": upn,
                            "status": "failed",
                            "error": str(exc),
                        })
            return results

        results = asyncio.run(_run())
        repaired = sum(1 for r in results if r.get("status") == "repaired")
        return {
            "message": f"License repair completed for '{prefix}': {repaired} user(s) repaired",
            "results": results,
        }

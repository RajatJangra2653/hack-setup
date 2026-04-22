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


def _make_tp(creds: Tuple[str, str, str]) -> MsalTokenProvider:
    t, c, s = creds
    return MsalTokenProvider(AzureConfig(tenant_id=t, client_id=c, client_secret=s))


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
        handler = getattr(self, f"_tool_{tool_name}", None)
        if not handler:
            return {"error": f"Unknown tool: {tool_name}"}
        return handler(**args)

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
        return state

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
            return s

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
                    if not uid or u.get("isAdmin"):
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
        """Discover and delete all users + groups for a hack, then remove blob state."""
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

        # Also delete blob state
        try:
            mgr = self._get_mgr()
            if mgr:
                deleted = mgr.delete_state(prefix)
                results["blob_state_deleted"] = deleted
        except Exception as exc:
            results["blob_state_error"] = str(exc)

        return results

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

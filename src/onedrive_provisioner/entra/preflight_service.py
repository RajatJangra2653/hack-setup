"""Pre-flight validation: fail before execution rather than during.

Checks performed:
  * Tenant reachable (token works, default domain resolves)
  * Domain valid (one of the verified domains in /organization or /domains)
  * License seats available for each requested SKU
  * Naming conflicts (any users / groups already exist with this prefix?)
  * RBAC: optional list of subscription IDs the SPN must be able to read

Returns a structured report with overall status: ok | warnings | blocked.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..graph import GraphClient
from ..logging_setup import get_logger
from .discovery_service import DiscoveryService
from .license_service import LicenseService
from .models import EntraConfig
from .naming import generate_user_plans
from .rbac_service import RbacService
from .tenant_service import TenantService

logger = get_logger(__name__)


async def run_preflight(
    graph: GraphClient,
    rbac: Optional[RbacService],
    cfg: EntraConfig,
    *,
    subscription_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    blocked = False
    warned = False

    # 1) Tenant reachable + domain
    ts = TenantService(graph)
    detected_domain = ""
    try:
        detected_domain = await ts.detect_default_domain()
        checks.append({
            "name": "Tenant reachable",
            "status": "ok",
            "detail": f"Default domain: {detected_domain}",
        })
    except Exception as exc:
        blocked = True
        checks.append({
            "name": "Tenant reachable",
            "status": "blocked",
            "detail": f"Could not query tenant: {exc}",
        })
        return _summarize(checks, blocked=True, warned=warned)

    domain = cfg.domain or detected_domain
    if not domain:
        blocked = True
        checks.append({
            "name": "Domain configured",
            "status": "blocked",
            "detail": "No domain provided and tenant default could not be detected",
        })
    else:
        # Verify domain is one of the tenant's verified domains
        try:
            verified = await ts.list_verified_domains()
            if domain.lower() in {d.lower() for d in verified}:
                checks.append({
                    "name": "Domain configured",
                    "status": "ok",
                    "detail": f"{domain} is a verified domain",
                })
            else:
                warned = True
                checks.append({
                    "name": "Domain configured",
                    "status": "warning",
                    "detail": f"{domain} is not in verified list: {verified}",
                })
        except Exception as exc:
            warned = True
            checks.append({
                "name": "Domain configured",
                "status": "warning",
                "detail": f"Could not verify domain list: {exc}",
            })

    # 2) Build the plan (count seats, naming conflicts) — works even on dry-run cfg
    plans = generate_user_plans(cfg)
    total_users = len(plans)
    if total_users == 0:
        blocked = True
        checks.append({
            "name": "User plan",
            "status": "blocked",
            "detail": "Plan generated 0 users — check teams/usersPerTeam/mode",
        })
        return _summarize(checks, blocked=blocked, warned=warned)
    checks.append({
        "name": "User plan",
        "status": "ok",
        "detail": f"{total_users} user(s) will be provisioned",
    })

    # 3) License availability
    if cfg.licenses:
        non_admin_seats = sum(1 for p in plans if not p.is_admin)
        try:
            lic_report = await LicenseService(graph).check_availability(
                cfg.licenses, non_admin_seats,
            )
            for row in lic_report:
                if not row["matched"]:
                    blocked = True
                    checks.append({
                        "name": f"License: {row['name']}",
                        "status": "blocked",
                        "detail": row.get("message", "Not found in /subscribedSkus"),
                    })
                elif not row["ok"]:
                    blocked = True
                    checks.append({
                        "name": f"License: {row['name']}",
                        "status": "blocked",
                        "detail": (f"Need {row['required']} seats, "
                                   f"only {row['available']} available "
                                   f"({row['consumed']}/{row['enabled']} consumed)"),
                    })
                else:
                    checks.append({
                        "name": f"License: {row['name']}",
                        "status": "ok",
                        "detail": (f"{row['available']} seat(s) available, "
                                   f"need {row['required']}"),
                    })
        except Exception as exc:
            warned = True
            checks.append({
                "name": "License check",
                "status": "warning",
                "detail": f"Could not query /subscribedSkus: {exc}",
            })

    # 4) Naming conflicts (only inspect by prefix; detailed list saved for UI)
    try:
        disc = await DiscoveryService(graph).discover(cfg.prefix)
        existing_upns = {
            (u.get("userPrincipalName") or "").lower()
            for u in (disc.get("users") or [])
        }
        planned_upns = {p.upn.lower() for p in plans}
        conflicts = sorted(planned_upns & existing_upns)
        if conflicts:
            if cfg.skip_existing:
                checks.append({
                    "name": "Naming conflicts (users)",
                    "status": "ok",
                    "detail": (f"{len(conflicts)} planned UPN(s) already exist; "
                               f"skipExisting=true so they will be reused"),
                    "items": conflicts[:20],
                })
            else:
                blocked = True
                checks.append({
                    "name": "Naming conflicts (users)",
                    "status": "blocked",
                    "detail": f"{len(conflicts)} UPN(s) already exist and skipExisting=false",
                    "items": conflicts[:20],
                })
        else:
            checks.append({
                "name": "Naming conflicts (users)",
                "status": "ok",
                "detail": "No collisions with existing UPNs at this prefix",
            })
    except Exception as exc:
        warned = True
        checks.append({
            "name": "Naming conflicts (users)",
            "status": "warning",
            "detail": f"Discovery failed: {exc}",
        })

    # 5) RBAC reach (optional — only if caller provided subs)
    if rbac is not None and subscription_ids:
        try:
            visible = await rbac.list_subscriptions()
            visible_ids = {s["subscriptionId"] for s in visible}
            missing = [s for s in subscription_ids if s not in visible_ids]
            if missing:
                blocked = True
                checks.append({
                    "name": "Subscription access",
                    "status": "blocked",
                    "detail": f"SPN cannot see {len(missing)} subscription(s)",
                    "items": missing,
                })
            else:
                checks.append({
                    "name": "Subscription access",
                    "status": "ok",
                    "detail": f"All {len(subscription_ids)} subscription(s) visible to SPN",
                })
        except Exception as exc:
            warned = True
            checks.append({
                "name": "Subscription access",
                "status": "warning",
                "detail": f"Could not list subscriptions: {exc}",
            })

    return _summarize(checks, blocked=blocked, warned=warned, totals={
        "totalUsers": total_users,
        "admins": sum(1 for p in plans if p.is_admin),
        "teams": cfg.teams if cfg.mode == "team" else 0,
        "domain": domain,
    })


def _summarize(checks, *, blocked: bool, warned: bool,
               totals: Optional[dict] = None) -> Dict[str, Any]:
    if blocked:
        overall = "blocked"
    elif warned:
        overall = "warnings"
    else:
        overall = "ok"
    return {
        "overall": overall,
        "checks": checks,
        "totals": totals or {},
    }

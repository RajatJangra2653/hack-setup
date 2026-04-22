"""Read-only mode: discover all subscriptions where hack principals have
Owner/Contributor → strip those and grant Reader instead."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

from ..logging_setup import get_logger
from .rbac_service import (
    ELEVATED_ROLE_IDS,
    ROLE_IDS,
    RbacService,
    role_definition_id,
    subscription_from_assignment,
)

logger = get_logger(__name__)


async def downgrade_principals_to_reader(
    rbac: RbacService,
    subscription_ids: Optional[List[str]],
    principals: List[dict],   # [{id, type: "Group"|"User", displayName?}]
) -> List[dict]:
    """For each principal: fetch ALL role assignments across subscriptions in
    ONE API call, then only process the subscriptions where that principal has
    assignments.  This is O(principals) instead of O(subs × principals).

    If ``subscription_ids`` is provided, results are filtered to only those subs.
    """
    reader_role_id_short = ROLE_IDS["Reader"].lower()
    allowed_subs: Optional[set] = None
    if subscription_ids:
        allowed_subs = {s.lower() for s in subscription_ids}

    out: List[dict] = []
    for p in principals:
        pid = p["id"]
        ptype = p.get("type", "Group")

        # ── 1. Single API call: get ALL assignments for this principal ──
        try:
            all_assigns = await rbac.list_all_assignments_for_principal(pid)
        except Exception as exc:
            out.append({
                "subscription": "*",
                "principalId": pid,
                "principalType": ptype,
                "displayName": p.get("displayName"),
                "removed": [],
                "readerEnsured": False,
                "errors": [f"bulk list failed: {exc}"],
            })
            continue

        # ── 2. Group assignments by subscription ──
        by_sub: Dict[str, list] = defaultdict(list)
        for a in all_assigns:
            sub = subscription_from_assignment(a)
            if sub is None:
                continue
            # If caller specified subs, skip assignments outside that set
            if allowed_subs and sub.lower() not in allowed_subs:
                continue
            by_sub[sub].append(a)

        if not by_sub:
            logger.info("readonly.principal.no_assignments",
                        principal=pid, display=p.get("displayName"))
            continue

        logger.info("readonly.principal.subs_found",
                    principal=pid,
                    display=p.get("displayName"),
                    subs=len(by_sub))

        # ── 3. Process only the subs with assignments ──
        for sub, assigns in by_sub.items():
            entry = {
                "subscription": sub,
                "principalId": pid,
                "principalType": ptype,
                "displayName": p.get("displayName"),
                "removed": [],
                "readerEnsured": False,
                "errors": [],
            }
            has_reader = False
            for a in assigns:
                role_def_id = (a.get("properties", {}).get("roleDefinitionId") or "").lower()
                role_guid = role_def_id.rsplit("/", 1)[-1]
                if role_guid == reader_role_id_short:
                    has_reader = True
                if role_guid in {rid.lower() for rid in ELEVATED_ROLE_IDS}:
                    arm_id = a.get("id")
                    ok = await rbac.delete_assignment(arm_id) if arm_id else False
                    entry["removed"].append({
                        "id": arm_id, "role": role_guid, "ok": ok,
                    })

            if not has_reader:
                try:
                    await rbac.assign_role(sub, pid, "Reader", principal_type=ptype)
                    entry["readerEnsured"] = True
                except Exception as exc:
                    entry["errors"].append(f"reader assign failed: {exc}")
            else:
                entry["readerEnsured"] = True

            out.append(entry)
    return out

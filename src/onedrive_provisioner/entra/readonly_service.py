"""Read-only mode: discover all subscriptions where hack principals have
Owner/Contributor → strip those and grant Reader instead."""
from __future__ import annotations

from typing import Dict, List, Optional

from ..logging_setup import get_logger
from .rbac_service import ELEVATED_ROLE_IDS, ROLE_IDS, RbacService, role_definition_id

logger = get_logger(__name__)


async def downgrade_principals_to_reader(
    rbac: RbacService,
    subscription_ids: Optional[List[str]],
    principals: List[dict],   # [{id, type: "Group"|"User", displayName?}]
) -> List[dict]:
    """For each (sub, principal): list assignments → remove Owner/Contributor →
    ensure Reader. Returns per-principal report.

    If ``subscription_ids`` is empty / None, the SPN's accessible subscriptions are
    auto-discovered and used as the candidate set. Only (sub, principal) pairs where
    the principal actually has any role assignment are processed — this avoids
    spamming Reader assignments on subs the principal never touched.
    """
    reader_role_id_short = ROLE_IDS["Reader"].lower()
    auto_discovered = False
    if not subscription_ids:
        subs_meta = await rbac.list_subscriptions()
        subscription_ids = [s["subscriptionId"] for s in subs_meta if s.get("subscriptionId")]
        auto_discovered = True
        logger.info("readonly.subs.auto_discovered", count=len(subscription_ids))

    out: List[dict] = []
    for sub in subscription_ids:
        for p in principals:
            pid = p["id"]
            ptype = p.get("type", "Group")
            entry = {
                "subscription": sub,
                "principalId": pid,
                "principalType": ptype,
                "displayName": p.get("displayName"),
                "removed": [],
                "readerEnsured": False,
                "errors": [],
            }
            try:
                assigns = await rbac.list_assignments_for_principal(sub, pid)
            except Exception as exc:
                entry["errors"].append(f"list failed: {exc}")
                out.append(entry)
                continue

            # When subs were auto-discovered, skip subs where this principal has
            # no assignment at all — avoids granting Reader on irrelevant subs.
            if auto_discovered and not assigns:
                continue

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

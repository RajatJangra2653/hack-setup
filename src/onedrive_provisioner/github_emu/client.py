"""GitHubEnabler — async context manager for GitHub EMU user management."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from ..auth import MsalTokenProvider
from ..config import AzureConfig
from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger

from .config import (
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_EMU_SHORT_NAME,
    GITHUB_RULE_ID,
    GITHUB_SP_ID,
    GITHUB_SYNC_JOB_ID,
    GITHUB_TENANT_ID,
    derive_github_username,
    resolve_group_id,
)
from .models import GitHubEnableReport, GitHubEnableResult

logger = get_logger(__name__)


class GitHubEnabler:
    """Add hack users to a GitHub-EMU-backed Entra group + trigger sync.

    Use as an async context manager:

        async with GitHubEnabler() as gh:
            await gh.enable_user("alice@contoso.com", with_copilot=True)
    """

    def __init__(
        self,
        *,
        tenant_id: str = GITHUB_TENANT_ID,
        client_id: str = GITHUB_CLIENT_ID,
        client_secret: str = GITHUB_CLIENT_SECRET,
        invite_redirect_url: Optional[str] = None,
        emu_short_name: str = GITHUB_EMU_SHORT_NAME,
    ) -> None:
        self._azure = AzureConfig(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )
        self._tp = MsalTokenProvider(self._azure)
        self._graph: Optional[GraphClient] = None
        self._tenant_default_domain: Optional[str] = None
        self._invite_redirect_url = invite_redirect_url or (
            f"https://myapplications.microsoft.com/?tenantid={tenant_id}"
        )
        self._emu_short_name = emu_short_name

    async def __aenter__(self) -> "GitHubEnabler":
        self._graph = GraphClient(self._tp)
        await self._graph.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._graph is not None:
            await self._graph.__aexit__(*exc)
            self._graph = None

    # ------------------------------------------------------------------
    async def _get_default_domain(self) -> str:
        if self._tenant_default_domain:
            return self._tenant_default_domain
        assert self._graph is not None
        org = await self._graph.get("/organization")
        for o in org.get("value", []):
            for d in o.get("verifiedDomains", []) or []:
                if d.get("isDefault"):
                    self._tenant_default_domain = d.get("name", "")
                    return self._tenant_default_domain
        self._tenant_default_domain = ""
        return ""

    async def _resolve_or_invite_user(self, email: str) -> tuple[Optional[str], bool]:
        """Return (userId, invited?). If user not in tenant, send a guest invite."""
        assert self._graph is not None
        email = (email or "").strip()
        if "@" not in email:
            raise ValueError(f"Invalid email: {email!r}")

        user_domain = email.split("@", 1)[1].lower()
        tenant_default = (await self._get_default_domain()).lower()

        if tenant_default and user_domain == tenant_default:
            try:
                u = await self._graph.get(
                    "/users",
                    params={"$filter": f"userPrincipalName eq '{email}'"},
                )
                vals = u.get("value", []) if isinstance(u, dict) else []
                if vals:
                    return vals[0].get("id"), False
            except GraphError as exc:
                logger.warning("github.user.lookup_failed", email=email, err=str(exc))
                return None, False
            return None, False

        # External user → invite as guest
        body = {
            "invitedUserEmailAddress": email,
            "inviteRedirectUrl": self._invite_redirect_url,
            "sendInvitationMessage": True,
        }
        inv = await self._graph.post("/invitations", json=body)
        invited_user = (inv or {}).get("invitedUser") or {}
        return invited_user.get("id"), True

    async def _add_to_group(self, group_id: str, user_id: str) -> tuple[bool, Optional[str]]:
        """Return (added?, message). False if already a member."""
        assert self._graph is not None
        body = {
            "@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{user_id}"
        }
        try:
            await self._graph.post(f"/groups/{group_id}/members/$ref", json=body)
            return True, None
        except GraphError as exc:
            # "One or more added object references already exist for the following modified properties"
            if exc.status in (400, 409) and (
                "already exist" in (exc.message or "").lower()
                or "already a member" in (exc.message or "").lower()
            ):
                return False, "already a member"
            raise

    async def _remove_from_group(self, group_id: str, user_id: str) -> tuple[bool, Optional[str]]:
        """Return (removed?, message). False if not a member."""
        assert self._graph is not None
        try:
            await self._graph.delete(f"/groups/{group_id}/members/{user_id}/$ref")
            return True, None
        except GraphError as exc:
            if exc.status == 404:
                return False, "not a member"
            raise

    async def _trigger_sync(
        self,
        group_id: str,
        user_id: str,
        *,
        max_retries: int = 5,
        initial_delay: float = 2.0,
    ) -> bool:
        assert self._graph is not None
        body = {
            "parameters": [
                {
                    "ruleId": GITHUB_RULE_ID,
                    "subjects": [
                        {
                            "objectId": group_id,
                            "objectTypeName": "Group",
                            "links": {
                                "members": [
                                    {"objectId": user_id, "objectTypeName": "User"}
                                ]
                            },
                        }
                    ],
                }
            ]
        }
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        url = (
            f"/servicePrincipals/{GITHUB_SP_ID}/synchronization/jobs/"
            f"{GITHUB_SYNC_JOB_ID}/provisionOnDemand"
        )
        for attempt in range(1, max_retries + 1):
            try:
                await self._graph.post(url, json=body, expect_json=False)
                logger.info("github.sync.triggered", user_id=user_id, attempt=attempt)
                return True
            except GraphError as exc:
                logger.warning(
                    "github.sync.failed",
                    attempt=attempt,
                    user_id=user_id,
                    err=str(exc),
                )
                if attempt >= max_retries:
                    return False
                await asyncio.sleep(min(60.0, 2 ** attempt))
        return False

    async def _trigger_sync_batch(
        self,
        group_id: str,
        user_ids: List[str],
        *,
        max_retries: int = 5,
        initial_delay: float = 5.0,
        batch_size: int = 5,
    ) -> Dict[str, bool]:
        """Trigger on-demand sync for many users in batches.

        The Graph provisionOnDemand API accepts one group with multiple member
        entries per call. We chunk into ``batch_size`` users per call and run
        those chunks concurrently (with a small semaphore) to stay under
        throttling limits while being much faster than sequential single-user
        calls.

        Returns {user_id: synced_bool}.
        """
        assert self._graph is not None
        if not user_ids:
            return {}
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)

        url = (
            f"/servicePrincipals/{GITHUB_SP_ID}/synchronization/jobs/"
            f"{GITHUB_SYNC_JOB_ID}/provisionOnDemand"
        )

        # Split into chunks
        chunks: List[List[str]] = []
        for i in range(0, len(user_ids), batch_size):
            chunks.append(user_ids[i : i + batch_size])

        results: Dict[str, bool] = {}
        sem = asyncio.Semaphore(3)  # max 3 concurrent sync calls

        async def _sync_chunk(chunk: List[str]) -> None:
            body = {
                "parameters": [
                    {
                        "ruleId": GITHUB_RULE_ID,
                        "subjects": [
                            {
                                "objectId": group_id,
                                "objectTypeName": "Group",
                                "links": {
                                    "members": [
                                        {"objectId": uid, "objectTypeName": "User"}
                                        for uid in chunk
                                    ]
                                },
                            }
                        ],
                    }
                ]
            }
            async with sem:
                for attempt in range(1, max_retries + 1):
                    try:
                        await self._graph.post(url, json=body, expect_json=False)
                        for uid in chunk:
                            results[uid] = True
                        logger.info("github.sync.batch_triggered",
                                    count=len(chunk), attempt=attempt)
                        return
                    except GraphError as exc:
                        logger.warning("github.sync.batch_failed",
                                       attempt=attempt, count=len(chunk),
                                       err=str(exc))
                        if attempt >= max_retries:
                            for uid in chunk:
                                results[uid] = False
                            return
                        await asyncio.sleep(min(60.0, 2 ** attempt))

        await asyncio.gather(*(_sync_chunk(c) for c in chunks))
        return results

    # ------------------------------------------------------------------
    async def enable_user(
        self,
        email: str,
        *,
        with_copilot: bool = False,
        with_ghas: bool = False,
        use_legacy: bool = False,
        group_id_override: Optional[str] = None,
        trigger_sync: bool = True,
    ) -> GitHubEnableResult:
        group_id = resolve_group_id(
            with_copilot=with_copilot,
            with_ghas=with_ghas,
            use_legacy=use_legacy,
            override=group_id_override,
        )
        gh_username = derive_github_username(email, short_name=self._emu_short_name)
        try:
            user_id, invited = await self._resolve_or_invite_user(email)
            if not user_id:
                return GitHubEnableResult(
                    email=email,
                    status="failed",
                    group_id=group_id,
                    github_username=gh_username,
                    message="User not found in tenant and could not be invited",
                )
            added, msg = await self._add_to_group(group_id, user_id)
            sync_ok = False
            if trigger_sync:
                sync_ok = await self._trigger_sync(group_id, user_id)
            status = (
                "invited" if invited and added
                else "added" if added
                else "already-member"
            )
            return GitHubEnableResult(
                email=email,
                status=status,
                user_id=user_id,
                group_id=group_id,
                invited=invited,
                sync_triggered=sync_ok,
                github_username=gh_username,
                message=msg,
            )
        except Exception as exc:
            logger.error("github.enable.failed", email=email, err=str(exc))
            return GitHubEnableResult(
                email=email,
                status="failed",
                group_id=group_id,
                github_username=gh_username,
                message=str(exc),
            )

    async def enable_users(
        self,
        emails: List[str],
        *,
        with_copilot: bool = False,
        with_ghas: bool = False,
        use_legacy: bool = False,
        group_id_override: Optional[str] = None,
        trigger_sync: bool = True,
        progress_cb: Optional[Any] = None,
        concurrency: int = 8,
    ) -> GitHubEnableReport:
        """Enable GitHub for multiple users — fast, batched approach.

        Phase 1: Resolve / invite all users and add to group concurrently.
        Phase 2: Trigger a single batched on-demand sync for everyone at once.

        This avoids the per-user 30-second sync delay, turning an O(n * 2min)
        process into roughly O(1) for the sync + O(n/concurrency) for group adds.
        """
        group_id = resolve_group_id(
            with_copilot=with_copilot,
            with_ghas=with_ghas,
            use_legacy=use_legacy,
            override=group_id_override,
        )

        clean_emails = [e.strip() for e in emails if e and e.strip()]
        results: List[GitHubEnableResult] = []
        sem = asyncio.Semaphore(concurrency)
        done_count = 0
        done_lock = asyncio.Lock()

        async def _process_one(email: str) -> GitHubEnableResult:
            nonlocal done_count
            gh_username = derive_github_username(email, short_name=self._emu_short_name)
            try:
                user_id, invited = await self._resolve_or_invite_user(email)
                if not user_id:
                    res = GitHubEnableResult(
                        email=email, status="failed", group_id=group_id,
                        github_username=gh_username,
                        message="User not found and could not be invited",
                    )
                else:
                    added, msg = await self._add_to_group(group_id, user_id)
                    status = (
                        "invited" if invited and added
                        else "added" if added
                        else "already-member"
                    )
                    res = GitHubEnableResult(
                        email=email, status=status, user_id=user_id,
                        group_id=group_id, invited=invited,
                        github_username=gh_username, message=msg,
                    )
            except Exception as exc:
                logger.error("github.enable.failed", email=email, err=str(exc))
                res = GitHubEnableResult(
                    email=email, status="failed", group_id=group_id,
                    github_username=gh_username, message=str(exc),
                )
            async with done_lock:
                done_count += 1
                if progress_cb:
                    try:
                        progress_cb(res, done_count, len(clean_emails))
                    except Exception:
                        pass
            return res

        async def _throttled(email: str) -> GitHubEnableResult:
            async with sem:
                return await _process_one(email)

        # Phase 1 — resolve + group-add, all concurrently
        results = list(await asyncio.gather(
            *(_throttled(e) for e in clean_emails)
        ))

        # Phase 2 — batch sync for all users that were successfully added/are members
        if trigger_sync:
            sync_user_ids = [
                r.user_id for r in results
                if r.user_id and r.status in ("added", "already-member", "invited")
            ]
            if sync_user_ids:
                sync_map = await self._trigger_sync_batch(group_id, sync_user_ids)
                for r in results:
                    if r.user_id and r.user_id in sync_map:
                        r.sync_triggered = sync_map[r.user_id]

        return GitHubEnableReport(
            total=len(results),
            added=sum(1 for r in results if r.status == "added"),
            already=sum(1 for r in results if r.status == "already-member"),
            invited=sum(1 for r in results if r.status == "invited"),
            failed=sum(1 for r in results if r.status == "failed"),
            sync_triggered=sum(1 for r in results if r.sync_triggered),
            results=results,
        )

    async def disable_users(
        self,
        emails: List[str],
        *,
        with_copilot: bool = False,
        with_ghas: bool = False,
        use_legacy: bool = False,
        group_id_override: Optional[str] = None,
        trigger_sync: bool = True,
        progress_cb: Optional[Any] = None,
        concurrency: int = 8,
    ) -> Dict[str, Any]:
        """Remove users from GitHub EMU group + trigger sync.

        Looks up each email in the broker tenant, removes from the target group,
        then triggers a batch sync so GitHub deprovisions them promptly.
        Does NOT delete any groups — only removes memberships.
        """
        group_id = resolve_group_id(
            with_copilot=with_copilot,
            with_ghas=with_ghas,
            use_legacy=use_legacy,
            override=group_id_override,
        )
        clean_emails = [e.strip() for e in emails if e and e.strip()]
        results: List[Dict[str, Any]] = []
        sem = asyncio.Semaphore(concurrency)
        removed_user_ids: List[str] = []

        async def _process_one(email: str) -> Dict[str, Any]:
            try:
                user_id, _ = await self._resolve_or_invite_user(email)
                if not user_id:
                    return {"email": email, "status": "skipped", "message": "User not found in broker tenant"}
                removed, msg = await self._remove_from_group(group_id, user_id)
                if removed:
                    removed_user_ids.append(user_id)
                    return {"email": email, "status": "removed", "userId": user_id, "groupId": group_id}
                else:
                    return {"email": email, "status": "not-member", "userId": user_id, "message": msg}
            except Exception as exc:
                logger.error("github.disable.failed", email=email, err=str(exc))
                return {"email": email, "status": "failed", "message": str(exc)}

        async def _throttled(email: str) -> Dict[str, Any]:
            async with sem:
                return await _process_one(email)

        results = list(await asyncio.gather(*(_throttled(e) for e in clean_emails)))

        # Trigger sync so GitHub deprovisions removed users
        sync_ok = False
        if trigger_sync and removed_user_ids:
            sync_map = await self._trigger_sync_batch(group_id, removed_user_ids)
            sync_ok = any(sync_map.values())
            for r in results:
                if r.get("userId") in sync_map:
                    r["syncTriggered"] = sync_map[r["userId"]]

        removed_count = sum(1 for r in results if r["status"] == "removed")
        return {
            "total": len(results),
            "removed": removed_count,
            "notMember": sum(1 for r in results if r["status"] == "not-member"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "syncTriggered": sync_ok,
            "results": results,
        }

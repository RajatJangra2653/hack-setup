"""GitHub EMU enablement routes."""
from __future__ import annotations

import asyncio
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from flask import Blueprint, request, jsonify

from onedrive_provisioner.logging_setup import configure_logging

from ._state import (
    github_sessions, github_lock, MAX_GITHUB_SESSIONS,
)

bp = Blueprint("github", __name__)


def _run_github_enable(session_id: str, emails: List[str], with_copilot: bool,
                       with_ghas: bool, use_legacy: bool, group_override: str | None,
                       trigger_sync: bool):
    from onedrive_provisioner.github_emu import GitHubEnabler

    def _set(**kw):
        with github_lock:
            s = github_sessions.get(session_id)
            if s:
                s.update(kw)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()

    partial: List[dict] = []

    def _on_done(result, done, total):
        partial.append(result.to_dict())
        with github_lock:
            s = github_sessions.get(session_id)
            if s:
                s["processed"] = done
                s["total"] = total
                s["partial_results"] = list(partial)
                s["updated_at"] = datetime.now(timezone.utc).isoformat()

    async def _go():
        async with GitHubEnabler() as gh:
            return await gh.enable_users(
                emails,
                with_copilot=with_copilot,
                with_ghas=with_ghas,
                use_legacy=use_legacy,
                group_id_override=group_override,
                trigger_sync=trigger_sync,
                progress_cb=_on_done,
            )

    try:
        configure_logging("INFO")
        report = asyncio.run(_go())
        _set(status="completed", result=report.to_dict(),
             processed=report.total, total=report.total)
    except Exception as exc:
        _set(status="failed", error=str(exc))


@bp.route("/api/github-enable", methods=["POST"])
def github_enable_start():
    data = request.get_json(silent=True) or {}
    raw_emails = data.get("emails") or []
    if isinstance(raw_emails, str):
        raw_emails = [e for e in re.split(r"[\s,;]+", raw_emails) if e]
    emails = [str(e).strip() for e in raw_emails if str(e).strip()]
    if not emails:
        return jsonify({"error": "Provide at least one email in 'emails'"}), 400

    with_copilot = bool(data.get("withCopilot", False))
    with_ghas = bool(data.get("withGhas", False))
    use_legacy = bool(data.get("useLegacyGroups", False))
    group_override = (data.get("groupIdOverride") or "").strip() or None
    trigger_sync = bool(data.get("triggerSync", True))

    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    session = {
        "id": session_id,
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "total": len(emails),
        "processed": 0,
        "withCopilot": with_copilot,
        "withGhas": with_ghas,
        "useLegacyGroups": use_legacy,
        "groupIdOverride": group_override,
        "partial_results": [],
        "result": None,
        "error": None,
    }
    with github_lock:
        if len(github_sessions) >= MAX_GITHUB_SESSIONS:
            oldest = min(github_sessions, key=lambda k: github_sessions[k]["created_at"])
            del github_sessions[oldest]
        github_sessions[session_id] = session

    threading.Thread(
        target=_run_github_enable,
        args=(session_id, emails, with_copilot, with_ghas, use_legacy,
              group_override, trigger_sync),
        daemon=True,
    ).start()

    return jsonify({"session_id": session_id, "status": "running"}), 202


@bp.route("/api/github-enable/<session_id>", methods=["GET"])
def github_enable_status(session_id):
    with github_lock:
        s = github_sessions.get(session_id)
        if not s:
            return jsonify({"error": "Session not found"}), 404
        return jsonify(s)


@bp.route("/api/github-disable", methods=["POST"])
def github_disable():
    from onedrive_provisioner.github_emu import GitHubEnabler

    data = request.get_json(silent=True) or {}
    raw_emails = data.get("emails") or []
    if isinstance(raw_emails, str):
        raw_emails = [e for e in re.split(r"[\s,;]+", raw_emails) if e]
    emails = [str(e).strip() for e in raw_emails if str(e).strip()]
    if not emails:
        return jsonify({"error": "Provide at least one email in 'emails'"}), 400

    with_copilot = bool(data.get("withCopilot", False))
    with_ghas = bool(data.get("withGhas", False))
    use_legacy = bool(data.get("useLegacyGroups", False))
    trigger_sync = bool(data.get("triggerSync", True))

    try:
        async def _go():
            async with GitHubEnabler() as gh:
                return await gh.disable_users(
                    emails,
                    with_copilot=with_copilot,
                    with_ghas=with_ghas,
                    use_legacy=use_legacy,
                    trigger_sync=trigger_sync,
                )
        result = asyncio.run(_go())
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

"""MCP server exposing OneDrive provisioning tools over stdio.

Tools:
  * provision_onedrive(user)
  * upload_folder(user, source?, destination?)
  * bulk_setup(users[], source?, destination?, dry_run?, concurrency?)
  * list_users(limit?)

Each tool returns a JSON text content block with structured results.

Run:
    onedrive-provisioner-mcp
or  python -m onedrive_provisioner.mcp.server
"""
from __future__ import annotations

import json
import os
from typing import Any, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ..config import load_config
from ..logging_setup import configure_logging, get_logger
from ..orchestrator import Orchestrator
from ..reporting import write_reports

logger = get_logger(__name__)

CONFIG_ENV = "ONEDRIVE_PROVISIONER_CONFIG"

server = Server("onedrive-provisioner")


def _orchestrator() -> Orchestrator:
    cfg_path = os.environ.get(CONFIG_ENV)
    cfg = load_config(cfg_path)
    configure_logging(cfg.log_level)
    return Orchestrator(cfg)


def _text(payload: Any) -> List[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


@server.list_tools()
async def _list_tools() -> List[Tool]:
    return [
        Tool(
            name="provision_onedrive",
            description=(
                "Ensure OneDrive is provisioned for a single user. "
                "Returns the user's drive ID on success."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user": {
                        "type": "string",
                        "description": "User Principal Name (UPN) or Azure AD object ID.",
                    }
                },
                "required": ["user"],
            },
        ),
        Tool(
            name="upload_folder",
            description=(
                "Provision (if needed) and upload a folder/file tree to a single "
                "user's OneDrive. Source can be a local folder path or an Azure "
                "Blob URL (https://<acct>.blob.core.windows.net/<container>/<prefix>)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user": {"type": "string"},
                    "source": {"type": "string", "description": "Local folder OR Azure Blob URL."},
                    "destination": {
                        "type": "string",
                        "description": "Root-relative destination folder in OneDrive.",
                    },
                },
                "required": ["user"],
            },
        ),
        Tool(
            name="bulk_setup",
            description=(
                "Run provisioning + folder upload for many users in parallel. "
                "Returns a structured report with per-user and per-file status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "users": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of UPNs/object IDs (omit to use config or all_users).",
                    },
                    "source": {"type": "string"},
                    "destination": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": False},
                    "concurrency": {"type": "integer", "minimum": 1, "maximum": 64},
                    "all_users": {"type": "boolean", "default": False},
                    "write_report": {"type": "boolean", "default": True},
                },
            },
        ),
        Tool(
            name="list_users",
            description="List enabled member users in the tenant (sanity check for credentials).",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 999, "default": 25}
                },
            },
        ),
    ]


@server.call_tool()
async def _call_tool(name: str, arguments: dict | None) -> List[TextContent]:
    args = arguments or {}
    try:
        if name == "provision_onedrive":
            orch = _orchestrator()
            res = await orch.provision_user(args["user"])
            return _text(res.to_dict())

        if name == "upload_folder":
            orch = _orchestrator()
            res = await orch.upload_for_user(
                args["user"], args.get("source"), args.get("destination")
            )
            return _text(res.to_dict())

        if name == "bulk_setup":
            orch = _orchestrator()
            if args.get("dry_run"):
                orch.cfg.execution.dry_run = True
            if args.get("concurrency"):
                orch.cfg.execution.concurrency = int(args["concurrency"])
            if args.get("all_users"):
                orch.cfg.users.all_users = True
            users: Optional[List[str]] = args.get("users") or None
            rep = await orch.bulk_setup(users, args.get("source"), args.get("destination"))
            payload = rep.to_dict()
            if args.get("write_report", True):
                paths = write_reports(rep, orch.cfg.reporting.output_dir, orch.cfg.reporting.formats)
                payload["report_files"] = [str(p) for p in paths]
            return _text(payload)

        if name == "list_users":
            from ..auth import MsalTokenProvider
            from ..graph import GraphClient
            from ..onedrive import UserResolver

            orch = _orchestrator()
            tp = MsalTokenProvider(orch.cfg.azure)
            limit = int(args.get("limit", 25))
            out = []
            async with GraphClient(tp, max_retries=orch.cfg.execution.max_retries) as g:
                async for u in UserResolver(g).list_all_members():
                    out.append({"upn": u.get("userPrincipalName"), "id": u.get("id")})
                    if len(out) >= limit:
                        break
            return _text(out)

        return _text({"error": f"unknown tool: {name}"})

    except Exception as exc:
        logger.exception("mcp.tool_failed", tool=name)
        return _text({"error": str(exc), "tool": name})


async def _amain() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def run() -> None:
    """Entry point for `onedrive-provisioner-mcp`."""
    import asyncio

    asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover
    run()

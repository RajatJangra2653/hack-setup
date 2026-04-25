"""AI Chatbot agent backed by Azure OpenAI GPT-4o with tool-calling.

The agent understands the Spektra hack setup system and can call internal
APIs to answer questions & perform operations on behalf of the user.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from openai import AzureOpenAI

logger = logging.getLogger(__name__)

# ── Tool definitions (maps to internal API calls) ──
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_saved_hacks",
            "description": "List all hacks that have been provisioned and saved to blob storage. Returns prefix, hackName, domain, totalUsers, lastUpdated.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hack_state",
            "description": "Get full state (users, groups, config, summary) for a specific hack by its prefix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix (e.g. 'nyc-esri-gcc')"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hack_users_summary",
            "description": "Get a summary of users in a hack: total count, created/failed/existing breakdown, admin count, list of UPNs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_hack_report",
            "description": "Generate a saved hack report with user/admin counts, licenses assigned, and optional cost allocation by subscription/team/user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                    "currency": {"type": "string", "description": "Currency code for manual cost inputs, default USD"},
                    "licenseUnitCosts": {
                        "type": "object",
                        "description": "Optional map of license/SKU name to monthly unit cost",
                        "additionalProperties": {"type": "number"},
                    },
                    "subscriptionCosts": {
                        "type": "array",
                        "description": "Optional subscription costs to allocate. Each item may include subscriptionId, cost, and team.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "subscriptionId": {"type": "string"},
                                "cost": {"type": "number"},
                                "team": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_provisioning_sessions",
            "description": "List recent provisioning sessions (in-memory). Shows session IDs, status, config, timing.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session_status",
            "description": "Get detailed status of a specific provisioning session by session ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "The provisioning session UUID"},
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_tenant_info",
            "description": "Detect tenant domain, TAP max lifetime, and available license SKUs. Requires SPN credentials.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_preflight_check",
            "description": "Run pre-flight validation to check if provisioning can proceed. Validates domain, licenses, permissions, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Hack prefix"},
                    "domain": {"type": "string", "description": "UPN domain"},
                    "teams": {"type": "integer", "description": "Number of teams"},
                    "usersPerTeam": {"type": "integer", "description": "Users per team"},
                    "adminUsers": {"type": "integer", "description": "Number of admin users"},
                    "mode": {"type": "string", "enum": ["team", "flat"], "description": "Provisioning mode"},
                },
                "required": ["prefix", "domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "provision_users",
            "description": "Provision users for a hack. Creates Entra ID users, assigns licenses, generates TAPs, and creates groups. Runs synchronously and returns the full report with user details (UPN, password, TAP, licenses). Always present the results in a clear table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Hack prefix (e.g. 'nyc-esri-gcc-')"},
                    "domain": {"type": "string", "description": "UPN domain (e.g. 'WWPS319.onmicrosoft.com')"},
                    "teams": {"type": "integer", "description": "Number of teams (default 2)"},
                    "usersPerTeam": {"type": "integer", "description": "Users per team (default 5)"},
                    "adminUsers": {"type": "integer", "description": "Admin users (default 1)"},
                    "mode": {"type": "string", "enum": ["team", "flat"], "description": "Provisioning mode"},
                    "licenses": {"type": "array", "items": {"type": "string"}, "description": "License SKU names to assign"},
                    "assignLicensesToAdmins": {"type": "boolean", "description": "Set true to assign selected licenses to admin users also"},
                    "hackName": {"type": "string", "description": "Human-friendly hack name"},
                    "dryRun": {"type": "boolean", "description": "If true, simulate without creating anything"},
                },
                "required": ["prefix", "domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "regenerate_tap",
            "description": "Regenerate Temporary Access Pass (TAP) for users in an existing hack stored in blob storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                    "tapLifetime": {"type": "integer", "description": "TAP lifetime in minutes (default 120)"},
                    "users": {"type": "array", "items": {"type": "string"}, "description": "Specific UPNs to regenerate TAP for (omit for all)"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_licenses",
            "description": "Assign additional licenses to users in an existing hack stored in blob storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                    "licenses": {"type": "array", "items": {"type": "string"}, "description": "License SKU part numbers to assign"},
                    "users": {"type": "array", "items": {"type": "string"}, "description": "Specific UPNs (omit for all non-admin users, or all users when includeAdmins is true)"},
                    "includeAdmins": {"type": "boolean", "description": "Set true to include admin users in license assignment"},
                },
                "required": ["prefix", "licenses"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_hack_resources",
            "description": "Discover existing Entra ID users and groups that match a hack prefix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix to search for"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_upload_jobs",
            "description": "List all OneDrive file upload jobs with their status.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cleanup_hack",
            "description": "Privileged mutation: delete all Entra ID users and groups for a hack, then archive saved state. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix to clean up"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_hack_state",
            "description": "Privileged mutation: delete only saved state from blob storage for a hack prefix. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_admin_guide",
            "description": "Generate a professional Admin/Trainer Guide Word document (.docx) for a saved hack. This is a read-only artifact generation action. Returns a download URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix to generate the guide for"},
                },
                "required": ["prefix"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_hack_end_date",
            "description": "Privileged mutation: set an auto-cleanup end date for a hack. Scheduled cleanup deletes users/groups/RBAC and archives blob state. Disabled unless CHATBOT_ENABLE_MUTATION_TOOLS=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix"},
                    "end_date": {"type": "string", "description": "ISO datetime for auto-cleanup (e.g. '2025-02-15T18:00:00Z')"},
                    "subscription_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Azure subscription IDs to remove RBAC role assignments from during cleanup (optional)",
                    },
                },
                "required": ["prefix", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_hack_provision",
            "description": "Schedule a hack to be provisioned at a future date/time. The scheduler will automatically create users, groups, and licenses at the specified time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scheduled_at": {"type": "string", "description": "ISO datetime to start provisioning (e.g. '2025-02-01T09:00:00Z')"},
                    "config": {
                        "type": "object",
                        "description": "Provisioning config (same as provision-users): prefix, hackName, domain, totalUsers, teamsCount, mode, licenses, tapLifetimeMinutes, etc.",
                    },
                },
                "required": ["scheduled_at", "config"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scheduled_jobs",
            "description": "List all scheduled jobs (both provisioning and cleanup). Shows job type, prefix, scheduled time, and status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status: pending, running, completed, failed, cancelled. Omit for all.", "enum": ["pending", "running", "completed", "failed", "cancelled"]},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_scheduled_job",
            "description": "Cancel a pending scheduled job by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "The job ID to cancel"},
                },
                "required": ["job_id"],
            },
        },
    },
]

READ_ONLY_TOOL_NAMES = {
    "list_saved_hacks",
    "get_hack_state",
    "get_hack_users_summary",
    "generate_hack_report",
    "get_provisioning_sessions",
    "get_session_status",
    "detect_tenant_info",
    "run_preflight_check",
    "discover_hack_resources",
    "list_upload_jobs",
    "list_scheduled_jobs",
    "generate_admin_guide",
}

if os.environ.get("CHATBOT_ENABLE_MUTATION_TOOLS", "").strip().lower() not in {"1", "true", "yes", "on"}:
    TOOLS = [tool for tool in TOOLS if tool["function"]["name"] in READ_ONLY_TOOL_NAMES]

SYSTEM_PROMPT = """You are the Spektra hack setup Assistant — an AI helper for managing hackathon user provisioning on Microsoft Entra ID (Azure AD).

IMPORTANT: You MUST ONLY respond to questions related to hackathon setup, user provisioning, license management, and the tools available to you. If a user asks about anything unrelated (general knowledge, current events, trivia, math, weather, politics, coding help, etc.), politely decline and redirect them to hack-related tasks. Example response for off-topic questions: "I'm only able to help with hackathon setup and management tasks. Would you like to provision users, check hack status, assign licenses, or generate a trainer guide?"

You help users with:
1. **Provisioning** — Creating bulk Entra ID users with teams, licenses, TAPs, and groups
2. **Managing** — Viewing existing hacks, regenerating TAPs, assigning licenses
3. **Uploading** — OneDrive file upload jobs
4. **Permissions** — Azure RBAC permission management
5. **Cleanup** — Removing hack resources
6. **Documentation** — Generating Admin/Trainer Guide documents for hacks
7. **Scheduling** — Setting auto-cleanup end dates for hacks, scheduling future hack provisioning, managing scheduled jobs

Key concepts:
- A "hack" is a hackathon event identified by a prefix (e.g. "nyc-esri-gcc-")
- Users are provisioned with UPNs like {prefix}t01-u01@{domain}
- TAP = Temporary Access Pass (one-time login credential)
- SPN = Service Principal credentials (tenant_id, client_id, client_secret) needed for Graph API calls
- State is persisted in Azure Blob Storage for cross-session management

When calling tools:
- You are read-only by default. Do not perform provisioning, cleanup, TAP regeneration, license assignment, state deletion, scheduling, or other mutations unless the server explicitly exposes those tools and the user completes confirmation outside the LLM.
- The SPN credentials are automatically injected from the user's session — don't ask for them
- For provisioning, always confirm the plan with the user before starting (unless they say "go ahead")
- Never reveal raw passwords, TAPs, tokens, or client secrets. Tool results are sanitized; if a user asks for secrets, direct them to the non-AI Manage screen.
- After provisioning completes, show results as a markdown table with columns: UPN, Status, Password, TAP, Licenses
- Show tabular data as well-formed markdown tables with a header row and separator row. Do not use padded ASCII tables or unstructured pipe text.
- Use friendly license product names when they are available; do not show only raw SKU part numbers.
- When generating docs, the guide dynamically includes access instructions based on assigned licenses
- After generating a doc, provide the exact download URL returned by the tool. Never invent placeholder links like "#".
- If a tool returns an error about Storage not configured, explain that AZURE_STORAGE_CONNECTION_STRING needs to be set

Be concise, helpful, and proactive. If the user asks something vague, suggest what they might want to do."""


def _json_len(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except Exception:
        return len(str(value))


def _hack_summary_for_client(hack: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "prefix": hack.get("prefix", ""),
        "hackName": hack.get("hackName", ""),
        "domain": hack.get("domain", ""),
        "totalUsers": hack.get("totalUsers", 0),
        "lastUpdated": hack.get("lastUpdated", ""),
        "createdAt": hack.get("createdAt", ""),
        "hackStartDate": hack.get("hackStartDate", ""),
        "hackDate": hack.get("hackDate", ""),
        "readonlyDate": hack.get("readonlyDate", ""),
        "deleteDate": hack.get("deleteDate", ""),
        "archived": bool(hack.get("archived")),
    }


def _tool_result_for_client(tool_name: str, result: Any) -> Any:
    """Return a compact, already-redacted tool payload for UI rendering.

    The LLM still receives the full tool result (truncated for context size), but
    the browser needs small structured fields such as generated-document URLs and
    saved-hack prefixes. Keeping this separate avoids scraping links from prose.
    """
    if isinstance(result, dict) and result.get("error"):
        return {"error": result.get("error")}

    if tool_name == "generate_admin_guide" and isinstance(result, dict):
        return {
            "message": result.get("message", ""),
            "filename": result.get("filename", ""),
            "download_url": result.get("download_url", ""),
            "size_bytes": result.get("size_bytes", 0),
        }

    if tool_name == "list_saved_hacks":
        hacks = result if isinstance(result, list) else result.get("hacks", []) if isinstance(result, dict) else []
        return [_hack_summary_for_client(h) for h in hacks[:100] if isinstance(h, dict)]

    if tool_name == "detect_tenant_info" and isinstance(result, dict):
        return {
            "domain": result.get("domain", ""),
            "tapMaxLifetimeMinutes": result.get("tapMaxLifetimeMinutes"),
            "subscribedSkus": (result.get("subscribedSkus") or [])[:200],
        }

    if tool_name == "generate_hack_report" and isinstance(result, dict):
        if _json_len(result) <= 24000:
            return result
        return {
            "prefix": result.get("prefix", ""),
            "summary": result.get("summary", {}),
            "licenses": result.get("licenses", {}),
            "costs": result.get("costs", {}),
            "truncated": True,
        }

    if _json_len(result) <= 16000:
        return result
    return {"truncated": True, "message": "Tool result was too large for inline UI rendering."}


def _looks_like_guide_request(text: str) -> bool:
    value = (text or "").lower()
    return "guide" in value and any(token in value for token in ("admin", "trainer", "document", "doc"))


def _clean_prefix(value: str) -> str:
    return (value or "").strip().strip("`'\"“”‘’.,;:!?()[]{}")


def _extract_admin_guide_prefix(messages: List[Dict[str, str]]) -> str:
    """Find the requested hack prefix for Admin Guide generation.

    Handles both direct requests ("generate guide for hack demo-") and the
    two-step chat flow where the assistant asks for a prefix and the user replies
    with only the prefix.
    """
    user_messages = [m for m in messages if m.get("role") == "user"]
    if not user_messages:
        return ""
    latest = str(user_messages[-1].get("content") or "")
    patterns = [
        r"\bhack\s+[\"'“”‘’`]?([A-Za-z0-9][A-Za-z0-9_.-]{1,127})",
        r"\bprefix\s+[\"'“”‘’`]?([A-Za-z0-9][A-Za-z0-9_.-]{1,127})",
        r"\bfor\s+[\"'“”‘’`]?([A-Za-z0-9][A-Za-z0-9_.-]{1,127})",
    ]
    if _looks_like_guide_request(latest):
        for pattern in patterns:
            match = re.search(pattern, latest, flags=re.IGNORECASE)
            if match:
                prefix = _clean_prefix(match.group(1))
                if prefix.lower() not in {"the", "a", "an", "hack", "guide", "document"}:
                    return prefix

    latest_prefix = _clean_prefix(latest)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,127}", latest_prefix):
        recent = "\n".join(str(m.get("content") or "") for m in messages[-6:-1])
        if _looks_like_guide_request(recent):
            return latest_prefix
    return ""


def _has_admin_guide_download(tools_called: List[Dict[str, Any]]) -> bool:
    for tool in tools_called:
        if tool.get("name") == "generate_admin_guide":
            result = tool.get("result") or {}
            if isinstance(result, dict) and result.get("download_url"):
                return True
    return False


def _ensure_admin_guide_result(
    reply: str,
    messages: List[Dict[str, str]],
    tool_executor: Any,
    tools_called: List[Dict[str, Any]],
) -> str:
    """Deterministically generate a guide if the model claims/needs one without using the tool."""
    if _has_admin_guide_download(tools_called):
        return reply
    prefix = _extract_admin_guide_prefix(messages)
    if not prefix:
        return reply

    try:
        result = tool_executor("generate_admin_guide", {"prefix": prefix})
    except Exception as exc:
        result = {"error": str(exc)}
    client_result = _tool_result_for_client("generate_admin_guide", result)
    tools_called.append({
        "name": "generate_admin_guide",
        "args": {"prefix": prefix},
        "result": client_result,
        "source": "deterministic_fallback",
    })
    if isinstance(client_result, dict) and client_result.get("download_url"):
        filename = client_result.get("filename") or "Admin Guide.docx"
        return (
            f"The Admin Guide for hack \"{prefix}\" has been generated.\n\n"
            f"Download: {client_result['download_url']}\n\n"
            f"File: {filename}"
        )
    if isinstance(client_result, dict) and client_result.get("error"):
        return f"I couldn't generate the Admin Guide for \"{prefix}\": {client_result['error']}"
    return reply


class ChatbotAgent:
    """Stateful chatbot agent with tool-calling capabilities."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str = "gpt-4o",
        api_version: str = "2024-10-21",
    ) -> None:
        self._client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )
        self._deployment = deployment

    def chat(
        self,
        messages: List[Dict[str, str]],
        tool_executor: Any,
        max_tool_rounds: int = 5,
    ) -> Dict[str, Any]:
        """Run a chat turn with tool calling.

        Args:
            messages: Full conversation history
            tool_executor: Callable(tool_name, args) -> result_dict
            max_tool_rounds: Max consecutive tool call rounds

        Returns:
            {"reply": str, "messages": [...updated history...], "tools_called": [...]}
        """
        full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
        tools_called = []
        provision_data = None

        for _ in range(max_tool_rounds):
            response = self._client.chat.completions.create(
                model=self._deployment,
                messages=full_messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.3,
                max_tokens=2000,
            )

            choice = response.choices[0]

            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                # Append assistant message with tool calls
                full_messages.append(choice.message.model_dump())

                for tc in choice.message.tool_calls:
                    fn_name = tc.function.name
                    fn_args = json.loads(tc.function.arguments or "{}")
                    logger.info("chatbot.tool_call name=%s args=%s", fn_name, fn_args)

                    result_for_client: Any = None
                    try:
                        result = tool_executor(fn_name, fn_args)
                        result_for_client = _tool_result_for_client(fn_name, result)
                        result_str = json.dumps(result, default=str, indent=2)
                        # Capture provision data for frontend rendering
                        if fn_name == "provision_users" and isinstance(result, dict) and "users" in result:
                            provision_data = result
                        # Truncate very large results
                        if len(result_str) > 8000:
                            result_str = result_str[:8000] + "\n... (truncated)"
                    except Exception as exc:
                        result_for_client = {"error": str(exc)}
                        result_str = json.dumps(result_for_client)

                    tools_called.append({"name": fn_name, "args": fn_args, "result": result_for_client})
                    full_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    })
                # Continue loop to process tool results
                continue

            # No more tool calls — return final response
            reply = choice.message.content or ""
            reply = _ensure_admin_guide_result(reply, messages, tool_executor, tools_called)
            # Update messages (without system prompt)
            out_messages = messages + [{"role": "assistant", "content": reply}]
            return {
                "reply": reply,
                "messages": out_messages,
                "tools_called": tools_called,
                "provision_data": provision_data,
            }

        # Exceeded max rounds — return what we have
        return {
            "reply": "I've processed multiple steps. Let me know if you need anything else.",
            "messages": messages,
            "tools_called": tools_called,
            "provision_data": provision_data,
        }

"""AI Chatbot agent backed by Azure OpenAI GPT-4o with tool-calling.

The agent understands the Spektra hack setup system and can call internal
APIs to answer questions & perform operations on behalf of the user.
"""
from __future__ import annotations

import json
import logging
import os
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
                    "users": {"type": "array", "items": {"type": "string"}, "description": "Specific UPNs (omit for all non-admin)"},
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
            "description": "Delete all Entra ID users and groups for a hack. First discovers resources by prefix, then deletes them. Also removes the saved state from blob storage. Use this when the user wants to delete/remove/cleanup a hack.",
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
            "description": "Delete only the saved state from blob storage for a hack prefix (does not delete Entra ID resources).",
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
            "description": "Generate a professional Admin/Trainer Guide Word document (.docx) for a hack. The document includes environment summary, user structure, license allocation, dynamic access instructions based on assigned licenses (M365, Copilot Studio, Power BI, etc.), login steps with screenshots, and a user credentials appendix. Returns a download URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "The hack prefix to generate the guide for"},
                },
                "required": ["prefix"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are the Spektra hack setup Assistant — an AI helper for managing hackathon user provisioning on Microsoft Entra ID (Azure AD).

IMPORTANT: You MUST ONLY respond to questions related to hackathon setup, user provisioning, license management, and the tools available to you. If a user asks about anything unrelated (general knowledge, current events, trivia, math, weather, politics, coding help, etc.), politely decline and redirect them to hack-related tasks. Example response for off-topic questions: "I'm only able to help with hackathon setup and management tasks. Would you like to provision users, check hack status, assign licenses, or generate a trainer guide?"

You help users with:
1. **Provisioning** — Creating bulk Entra ID users with teams, licenses, TAPs, and groups
2. **Managing** — Viewing existing hacks, regenerating TAPs, assigning licenses
3. **Uploading** — OneDrive file upload jobs
4. **Permissions** — Azure RBAC permission management
5. **Cleanup** — Removing hack resources
6. **Documentation** — Generating Admin/Trainer Guide documents for hacks

Key concepts:
- A "hack" is a hackathon event identified by a prefix (e.g. "nyc-esri-gcc-")
- Users are provisioned with UPNs like {prefix}t01-u01@{domain}
- TAP = Temporary Access Pass (one-time login credential)
- SPN = Service Principal credentials (tenant_id, client_id, client_secret) needed for Graph API calls
- State is persisted in Azure Blob Storage for cross-session management

When calling tools:
- The SPN credentials are automatically injected from the user's session — don't ask for them
- For provisioning, always confirm the plan with the user before starting (unless they say "go ahead")
- After provisioning completes, show results as a markdown table with columns: UPN, Status, Password, TAP, Licenses
- Show results in a clear, organized way
- When generating docs, the guide dynamically includes access instructions based on assigned licenses
- After generating a doc, provide the download link to the user
- If a tool returns an error about Storage not configured, explain that AZURE_STORAGE_CONNECTION_STRING needs to be set

Be concise, helpful, and proactive. If the user asks something vague, suggest what they might want to do."""


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

                    try:
                        result = tool_executor(fn_name, fn_args)
                        result_str = json.dumps(result, default=str, indent=2)
                        # Capture provision data for frontend rendering
                        if fn_name == "provision_users" and isinstance(result, dict) and "users" in result:
                            provision_data = result
                        # Truncate very large results
                        if len(result_str) > 8000:
                            result_str = result_str[:8000] + "\n... (truncated)"
                    except Exception as exc:
                        result_str = json.dumps({"error": str(exc)})

                    tools_called.append({"name": fn_name, "args": fn_args})
                    full_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    })
                # Continue loop to process tool results
                continue

            # No more tool calls — return final response
            reply = choice.message.content or ""
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

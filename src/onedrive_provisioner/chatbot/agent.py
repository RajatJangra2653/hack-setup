"""ChatbotAgent class and helper utilities."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from openai import AzureOpenAI

from .prompts import SYSTEM_PROMPT
from .tool_registry import TOOLS

logger = logging.getLogger(__name__)


# ── Helper functions ──────────────────────────────────────────────────────────


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
    return (value or "").strip().strip("`'\"\u201c\u201d\u2018\u2019.,;:!?()[]{}")


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
        r"\bhack\s+[\"'\u201c\u201d\u2018\u2019`]?([A-Za-z0-9][A-Za-z0-9_.-]{1,127})",
        r"\bprefix\s+[\"'\u201c\u201d\u2018\u2019`]?([A-Za-z0-9][A-Za-z0-9_.-]{1,127})",
        r"\bfor\s+[\"'\u201c\u201d\u2018\u2019`]?([A-Za-z0-9][A-Za-z0-9_.-]{1,127})",
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


# ── ChatbotAgent class ────────────────────────────────────────────────────────


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

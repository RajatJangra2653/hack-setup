"""Chatbot route."""
from __future__ import annotations

import os

from flask import Blueprint, request, jsonify

from onedrive_provisioner.chatbot import ChatbotAgent
from onedrive_provisioner.chatbot.tool_executor import ToolExecutor

from ._state import (
    extract_creds, get_state_manager,
    entra_sessions, entra_lock,
    jobs, jobs_lock,
    generated_docs,
)

bp = Blueprint("chat", __name__)


def _get_chatbot_agent() -> ChatbotAgent | None:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    key = os.environ.get("AZURE_OPENAI_KEY", "")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    if not endpoint or not key:
        return None
    return ChatbotAgent(endpoint=endpoint, api_key=key, deployment=deployment)


@bp.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    creds = extract_creds(data)
    if not creds:
        return jsonify({"error": "Missing SPN credentials"}), 400
    messages = data.get("messages") or []
    if not messages:
        return jsonify({"error": "messages[] required"}), 400

    agent = _get_chatbot_agent()
    if not agent:
        return jsonify({"error": "Azure OpenAI not configured (set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY)"}), 503

    executor = ToolExecutor(
        creds=creds,
        get_state_manager=get_state_manager,
        entra_sessions=entra_sessions,
        entra_lock=entra_lock,
        upload_jobs=jobs,
        jobs_lock=jobs_lock,
        docs_store=generated_docs,
    )

    try:
        result = agent.chat(messages, tool_executor=executor)
        resp = {
            "reply": result["reply"],
            "tools_called": result["tools_called"],
        }
        if result.get("provision_data"):
            resp["provision_data"] = result["provision_data"]
        return jsonify(resp)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

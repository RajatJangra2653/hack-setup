"""AI Chatbot agent backed by Azure OpenAI GPT-4o with tool-calling.

The agent understands the Spektra hack setup system and can call internal
APIs to answer questions & perform operations on behalf of the user.
"""
from .agent import (  # noqa: F401
    ChatbotAgent,
    _ensure_admin_guide_result,
    _extract_admin_guide_prefix,
    _tool_result_for_client,
)
from .prompts import SYSTEM_PROMPT  # noqa: F401
from .tool_registry import READ_ONLY_TOOL_NAMES, TOOLS  # noqa: F401

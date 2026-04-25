from onedrive_provisioner.chatbot import (
    TOOLS,
    _ensure_admin_guide_result,
    _extract_admin_guide_prefix,
)


def test_generate_admin_guide_tool_available_by_default():
    tool_names = {tool["function"]["name"] for tool in TOOLS}
    assert "generate_admin_guide" in tool_names


def test_extract_admin_guide_prefix_from_direct_request():
    messages = [{"role": "user", "content": "Generate an admin guide document for hack sumit-test-"}]
    assert _extract_admin_guide_prefix(messages) == "sumit-test-"


def test_extract_admin_guide_prefix_from_followup_prefix_only():
    messages = [
        {"role": "user", "content": "Generate an admin guide document for the hack"},
        {"role": "assistant", "content": "Which hack prefix should I use for the Admin Guide?"},
        {"role": "user", "content": "sumit-test-"},
    ]
    assert _extract_admin_guide_prefix(messages) == "sumit-test-"


def test_admin_guide_fallback_returns_real_download_url():
    download_url = "/api/generated-docs/123e4567-e89b-12d3-a456-426614174000"
    calls = []

    def executor(tool_name, args):
        calls.append((tool_name, args))
        return {
            "filename": "sumit-test-Admin-Guide.docx",
            "download_url": download_url,
            "size_bytes": 42,
        }

    tools_called = []
    reply = _ensure_admin_guide_result(
        "The admin guide document has been successfully generated. Download Admin Guide for sumit-test-",
        [{"role": "user", "content": "Generate an admin guide document for hack sumit-test-"}],
        executor,
        tools_called,
    )

    assert calls == [("generate_admin_guide", {"prefix": "sumit-test-"})]
    assert download_url in reply
    assert tools_called[0]["name"] == "generate_admin_guide"
    assert tools_called[0]["result"]["download_url"] == download_url

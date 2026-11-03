"""Pre-stream /api/chat_stream errors carry stable codes; the UI classifies by code."""
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from routes import chat_routes
from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint

ROOT = Path(__file__).resolve().parents[1]


async def _approval_error(monkeypatch, approval_id, decision="approve", **form):
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", {})
    request = _RouteRequest("chat")
    request._form.update({"tool_approval_id": approval_id, "tool_approval_decision": decision,
                          "compare_mode": "false", **form})
    with pytest.raises(HTTPException) as caught:
        response = await endpoint(request)
        async for _ in response.body_iterator:
            pass
    return caught.value


@pytest.mark.asyncio
async def test_expired_or_unknown_approval_is_a_coded_approval_error(monkeypatch):
    # A restart or the 10-minute TTL leaves the approval id unknown to the server.
    error = await _approval_error(monkeypatch, "approval-that-no-longer-exists")
    assert error.status_code == 409
    assert error.detail["error"] == chat_routes.CHAT_ERROR_TOOL_APPROVAL_EXPIRED
    assert "expired" in error.detail["message"]


@pytest.mark.asyncio
async def test_ttl_expired_approval_is_coded_as_expired(monkeypatch):
    from src import tool_approvals
    from src.tool_capabilities import capabilities_for_action
    pending = chat_routes.tool_approval_store.create(
        owner="alice", session_id="session-1", origin_run_id="run-1", tool_name="python",
        content="print(1)", workspace=None, external_untrusted_context_seen=False,
        capabilities=capabilities_for_action("python", "print(1)"))
    real_time = tool_approvals.time.time
    monkeypatch.setattr(tool_approvals.time, "time", lambda: real_time() + 3600)
    error = await _approval_error(monkeypatch, pending.approval_id)
    assert error.detail["error"] == chat_routes.CHAT_ERROR_TOOL_APPROVAL_EXPIRED


@pytest.mark.asyncio
async def test_delegated_credential_approval_is_an_authorization_error(monkeypatch):
    monkeypatch.setattr(chat_routes, "is_delegated_credential", lambda request: True)
    error = await _approval_error(monkeypatch, "any-id")
    assert error.status_code == 403
    assert error.detail["error"] == chat_routes.CHAT_ERROR_TOOL_APPROVAL_NOT_ALLOWED


def test_internal_tool_request_approval_is_an_authorization_error():
    from types import SimpleNamespace
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
    from src.agent_runtime.authority import require_user_approval_request
    request = SimpleNamespace(headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN},
                              state=SimpleNamespace(current_user="internal-tool"))
    with pytest.raises(HTTPException) as caught:
        require_user_approval_request(request)
    assert caught.value.status_code == 403
    assert caught.value.detail["error"] == "TOOL_APPROVAL_NOT_ALLOWED"


def test_capability_code_is_reserved_and_structured():
    error = chat_routes._chat_error(400, chat_routes.CHAT_ERROR_MODEL_TOOLS_UNSUPPORTED, "no tools")
    assert error.detail == {"error": "MODEL_TOOLS_UNSUPPORTED", "message": "no tools"}
    # No approval or authorization path may claim a capability failure.
    source = (ROOT / "routes/chat_routes.py").read_text(encoding="utf-8")
    assert source.count("CHAT_ERROR_MODEL_TOOLS_UNSUPPORTED") == 1


def test_frontend_classifies_by_code_and_switches_mode_only_for_capability():
    chat = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    errors = (ROOT / "static/js/chatErrors.js").read_text(encoding="utf-8")
    # The old wording heuristic is gone.
    assert "errText.includes('tool')" not in chat and "errText.includes('auto')" not in chat
    assert "classifyChatStreamError(res.status, errBody)" in chat
    # The mode switch is reachable only from the explicit capability decision.
    branch = re.search(r"if \(_errInfo\.switchToChat\) \{(.*?)\n        \}\n        typewriterInto", chat, re.S)
    assert branch and "Storage.setJSON(Storage.KEYS.TOGGLES" in branch.group(1)
    assert chat.count("_st.mode = 'chat'") == 1
    assert "switchToChat: category === 'tool_capability'" in errors
    assert "if (code === MODEL_TOOLS_UNSUPPORTED) category = 'tool_capability';" in errors

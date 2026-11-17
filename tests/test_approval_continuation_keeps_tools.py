"""A resumed approval keeps the interrupted turn's tools (2026-11-16).

"recommend some music that I don't have on deck" was approved for control_music genres;
the resume re-routed the same message as a web lookup, offered only web tools, dropped
her control_music calls as unoffered, and the turn ended on "Let me spot-check…"."""
import json
from unittest.mock import AsyncMock

import pytest

from src.agent_runtime.authority import OperationGrant, RequestAuthority
from src.tool_capabilities import capabilities_for_action

ASK = "Cool, could you also recommend some music that I don't have on deck that I could get?"
GENRES = '{"action": "genres"}'
TURN_TOOLS = {"ask_user", "control_music", "discover_tools", "update_plan"}


@pytest.mark.asyncio
@pytest.mark.parametrize("web_off", [False, True])   # 11-16: the first resume had the web toggle off
async def test_resumed_approval_still_offers_the_turns_tools(monkeypatch, web_off):
    from src import agent_loop as loop, tool_execution as execution
    implementation = AsyncMock(return_value=("control_music", {"output": "Rock 300, Jazz 120", "exit_code": 0}))
    monkeypatch.setattr(execution, "_execute_tool_block_impl", implementation)
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    rounds = []
    search = json.dumps({"action": "search", "query": "Tatsuro Yamashita"})

    async def fake_stream(*args, **kwargs):
        rounds.append(kwargs)
        if len(rounds) == 1:     # a native call, as Gyro made on 11-16
            yield "data: " + json.dumps({"type": "tool_calls", "calls": [
                {"id": "c1", "name": "control_music", "arguments": search}]}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": "You're light on city pop; try Tatsuro Yamashita."}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(loop, "stream_llm_with_fallback", fake_stream)
    authority = RequestAuthority("r", "alice", "s", "", tuple(OperationGrant(t) for t in TURN_TOOLS))
    store = loop.tool_approval_store
    pending = store.create(
        owner="alice", session_id="s", origin_run_id="run-1", tool_name="control_music", content=GENRES,
        workspace=None, external_untrusted_context_seen=False,
        capabilities=capabilities_for_action("control_music", GENRES),
        selected_tools=TURN_TOOLS, request_authority=authority, authority_gap=True)
    grant = store.consume(pending.approval_id, decision="approve_once", owner="alice", session_id="s")

    events = []
    async for chunk in loop.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-4o", [{"role": "user", "content": ASK}],
            owner="alice", session_id="s", max_rounds=3, relevant_tools=set(TURN_TOOLS),
            request_authority=authority, exact_approval=grant, authority_gap_approvals=True,
            disabled_tools={"web_search", "web_fetch", "private_browser"} if web_off else None):
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                events.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                pass

    contents = [c.args[0].content for c in implementation.await_args_list]
    assert contents[0] == GENRES                      # the approved lookup
    assert any("Tatsuro" in c for c in contents[1:]), contents   # her follow-up search ran, not dropped
    finals = " ".join(e.get("content", "") for e in events if e.get("type") == "final_response")
    assert "Web access is disabled" not in finals
    assert rounds, "the model must get to answer after the approved lookup"

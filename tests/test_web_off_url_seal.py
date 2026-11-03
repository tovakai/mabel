"""A pasted URL must not erase an independently answerable local task, and
explicit web-off must hold at the model-facing evidence level."""
from __future__ import annotations

import asyncio
import json

import pytest

from src.agent_runtime.authority import create_request_authority
from src.tool_capabilities import ToolRunSecurityContext
from src.tool_policy import WEB_EGRESS_MARKER, WEB_EGRESS_TOOL_NAMES, build_effective_tool_policy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.tool_types import ToolBlock
from src.turn_contract import FAMILY_TOOLS, resolve_turn_contract, web_seal_with_local_families
from tests.test_web_off_and_computation import _route_kwargs

URL = "https://en.wikipedia.org/wiki/Visual_novel"
MIXED = ("Compare my notes on Lessons in Love with public information about the game, "
         f"and also fetch {URL} for context.")
PURE = f"summarize {URL}"
MENTIONED = f"My note on Lessons in Love links to {URL} - what do my notes say about the game?"
WEB_OFF = set(WEB_EGRESS_TOOL_NAMES) | {WEB_EGRESS_MARKER}
VAULT_TOOLS = {"vault_knowledge_search", "read_file", "grep", "glob"}


# --- contract level ----------------------------------------------------------------------

def _resolve(families, *, web_off, required=(), required_caps=None):
    policy = build_effective_tool_policy(disabled_tools=WEB_OFF if web_off else set())
    return resolve_turn_contract(
        capabilities=families, schemas=FUNCTION_TOOL_SCHEMAS, policy=policy,
        required_tools=set(required), required_capabilities=required_caps,
        selected_tools={"web_fetch"}, message="x",
    )


def test_seal_helper_only_fires_with_a_local_family():
    assert web_seal_with_local_families({"web_fetch"}, {"workspace_read", "search_browser"})
    assert not web_seal_with_local_families({"web_fetch"}, {"search_browser"})
    assert not web_seal_with_local_families({"write_file", "edit_file"}, {"workspace_read"})


def test_mixed_url_and_vault_web_off_keeps_vault_tools():
    turn = _resolve({"workspace_read", "search_browser"}, web_off=True, required_caps={"workspace_read"})
    assert not turn.unavailable
    assert VAULT_TOOLS <= set(turn.offered)
    assert not set(turn.offered) & set(WEB_EGRESS_TOOL_NAMES)


def test_mixed_url_and_vault_web_on_offers_fetch_without_erasing_vault():
    turn = _resolve({"workspace_read", "search_browser"}, web_off=False)
    assert VAULT_TOOLS <= set(turn.offered) and "web_fetch" in turn.offered
    assert "web_fetch" not in turn.required


def test_pure_url_web_off_is_still_unavailable():
    turn = _resolve({"search_browser"}, web_off=True, required={"web_fetch"})
    assert not turn.offered and "web_fetch" in turn.unavailable


# --- route level (vault in scope, as in production) ---------------------------------------

async def _route(monkeypatch, message, web):
    from routes import chat_routes
    monkeypatch.setattr(chat_routes, "_configured_vault_in_scope", lambda *a, **k: True)
    return await _route_kwargs(monkeypatch, message, web)


@pytest.mark.asyncio
async def test_route_mixed_web_off_keeps_local_part_and_denies_web(monkeypatch):
    kwargs = await _route(monkeypatch, MIXED, "false")
    turn = kwargs["turn_contract"]
    assert turn.offered and not turn.unavailable
    assert not set(turn.offered) & set(WEB_EGRESS_TOOL_NAMES)
    assert not set(turn.required) & set(WEB_EGRESS_TOOL_NAMES)
    assert kwargs["tool_policy"].blocks("web_fetch")


@pytest.mark.asyncio
async def test_route_pure_url_web_off_is_refused(monkeypatch):
    kwargs = await _route(monkeypatch, PURE, "false")
    turn = kwargs["turn_contract"]
    assert not turn.offered and "web_fetch" in turn.unavailable


@pytest.mark.asyncio
async def test_route_mixed_web_on_keeps_fetch_optional(monkeypatch):
    kwargs = await _route(monkeypatch, MIXED, "true")
    turn = kwargs["turn_contract"]
    assert "web_fetch" in turn.offered and "web_fetch" not in turn.required


@pytest.mark.asyncio
async def test_route_url_mentioned_but_not_requested_is_never_required(monkeypatch):
    for web in ("true", "false"):
        turn = (await _route(monkeypatch, MENTIONED, web))["turn_contract"]
        assert turn.offered and not turn.unavailable
        assert "web_fetch" not in turn.required


# --- model-facing evidence: web off, model still tries to fetch -----------------------------

def _collect(stream):
    async def run():
        return [chunk async for chunk in stream]
    return asyncio.run(run())


def test_model_sees_blocked_fetch_and_no_fetched_content(monkeypatch, tmp_path):
    from src import agent_loop
    from src import agent_tools

    fetch_calls = []

    async def real_fetch_must_not_run(content, ctx):
        fetch_calls.append(content)
        return {"output": "PAGE BODY", "exit_code": 0}

    monkeypatch.setitem(agent_tools.TOOL_HANDLERS, "web_fetch", real_fetch_must_not_run)
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())

    policy = build_effective_tool_policy(disabled_tools=WEB_OFF)
    turn = resolve_turn_contract(capabilities={"workspace_read", "search_browser"},
                                 schemas=FUNCTION_TOOL_SCHEMAS, policy=policy,
                                 selected_tools={"web_fetch"}, message=MIXED)
    authority = create_request_authority(MIXED, owner="alice", session_id="", workspace=str(tmp_path),
                                         policy=policy, capabilities={"workspace_read"},
                                         interactive_review=True)
    seen = []

    async def fake_stream(candidates, messages, **kwargs):
        factory = kwargs.get("candidate_request_factory")
        request = await factory(0, *candidates[0]) if factory else {"messages": messages, "kwargs": kwargs}
        seen.append(request)
        if len(seen) == 1:
            # The model ignores the guidance and tries the URL, then an alternate web tool.
            calls = [{"id": "c1", "name": "web_fetch", "arguments": json.dumps({"url": URL})},
                     {"id": "c2", "name": "private_browser", "arguments": json.dumps({"action": "open", "url": URL})}]
            yield f'data: {json.dumps({"type": "tool_calls", "calls": calls})}\n\n'
        else:
            yield 'data: {"delta": "Your notes say X. I could not check the web page because web is off."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    chunks = _collect(agent_loop.stream_agent_loop(
        "http://127.0.0.1:8080/v1", "qwen3.8-flash-next-gyro-gyro-s",
        [{"role": "user", "content": MIXED}],
        max_rounds=2, owner="alice", workspace=str(tmp_path), fallbacks=[], fallback_on_empty=False,
        disabled_tools=WEB_OFF, tool_policy=policy, turn_contract=turn, request_authority=authority,
    ))

    # No web egress ran, through the URL or an alternate tool: the loop drops
    # calls to tools the contract never offered, before any execution.
    assert fetch_calls == []
    outputs = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    assert not any(o.get("type") == "tool_output" and o.get("tool") in WEB_EGRESS_TOOL_NAMES
                   and o.get("exit_code") == 0 for o in outputs)

    # Model-facing evidence: the model was never offered a web-capable tool, and
    # its instructions say no URL was fetched; no page content reaches any round.
    offered_to_model = {t["function"]["name"] for t in (seen[0]["kwargs"].get("tools") or [])}
    assert VAULT_TOOLS <= offered_to_model
    assert not offered_to_model & set(WEB_EGRESS_TOOL_NAMES)
    first_system = next(m["content"] for m in seen[0]["messages"] if m.get("role") == "system")
    assert "No web page or URL was fetched this turn" in first_system
    assert "do not claim or imply that you read any linked page" in first_system
    assert all("PAGE BODY" not in json.dumps(request["messages"]) for request in seen)
    visible = "".join(o.get("delta") or o.get("content") or "" for o in outputs
                      if "delta" in o or o.get("type") == "final_response")
    assert "PAGE BODY" not in visible


@pytest.mark.asyncio
async def test_discovery_cannot_add_web_tools_under_web_off(tmp_path):
    from src.agent_runtime.authority import bind_request_authority
    from src.agent_runtime.tool_catalog import active_catalog, bind_catalog, discover_tools

    policy = build_effective_tool_policy(disabled_tools=WEB_OFF)
    turn = resolve_turn_contract(capabilities={"workspace_read", "search_browser"},
                                 schemas=FUNCTION_TOOL_SCHEMAS, policy=policy,
                                 selected_tools={"web_fetch"}, message=MIXED)
    authority = create_request_authority(MIXED, owner="alice", session_id="s", workspace=str(tmp_path),
                                         policy=policy, capabilities={"workspace_read"},
                                         interactive_review=True)
    with bind_request_authority(authority), bind_catalog(turn, True):
        result = await discover_tools(json.dumps({"names": ["web_fetch", "web_search"]}), {})
        assert result["exit_code"] == 1 and result["executed"] is False
        assert not active_catalog().selected & set(WEB_EGRESS_TOOL_NAMES)
        # Local tools stay discoverable.
        ok = await discover_tools(json.dumps({"names": ["vault_knowledge_search"]}), {})
        assert ok["exit_code"] == 0 and "vault_knowledge_search" in active_catalog().selected


# --- approval cannot override web-off --------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("web_off", [False, True])
async def test_real_exact_approval_cannot_override_web_off(monkeypatch, web_off):
    """A consumed, exact user approval for this very fetch is honored only when
    web is not explicitly off (control), and refused before admission when it is."""
    from unittest.mock import AsyncMock
    from src import tool_execution
    from src.agent_runtime.authority import OperationGrant, RequestAuthority
    from src.tool_approvals import ToolApprovalStore
    from src.tool_capabilities import capabilities_for_action

    content = json.dumps({"url": URL})
    base = RequestAuthority("web-off-approval", "alice", "s", "", (OperationGrant("ask_user"),))
    authority = base.restrict(build_effective_tool_policy(disabled_tools=WEB_OFF)) if web_off else base
    store = ToolApprovalStore()
    pending = store.create(owner="alice", session_id="s", origin_run_id="run-1", tool_name="web_fetch",
                           content=content, workspace=None, external_untrusted_context_seen=False,
                           capabilities=capabilities_for_action("web_fetch", content),
                           request_authority=base, authority_gap=True)
    grant = store.consume(pending.approval_id, decision="approve_task", owner="alice", session_id="s")
    assert grant.matches(owner="alice", session_id="s", tool_name="web_fetch", content=content, workspace=None)

    implementation = AsyncMock(return_value=("web_fetch", {"output": "PAGE BODY", "exit_code": 0}))
    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", implementation)
    _, result = await tool_execution.execute_tool_block(
        ToolBlock("web_fetch", content), owner="alice", session_id="s", workspace=None,
        security_context=ToolRunSecurityContext(), exact_approval=grant, request_authority=authority)
    if web_off:
        implementation.assert_not_awaited()
        assert result["failure_reason"] == "web_disabled" and result["executed"] is False
    else:
        implementation.assert_awaited_once()
        assert result["exit_code"] == 0

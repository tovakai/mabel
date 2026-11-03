"""Explicit web-off is absolute; computation outranks broad "what is X" lookups."""
from __future__ import annotations

import json

import pytest

from src.agent_runtime.authority import ExactOperation, create_request_authority
from src.tool_capabilities import ToolRunSecurityContext
from src.tool_execution import execute_tool_block
from src.tool_policy import (
    WEB_EGRESS_MARKER,
    WEB_EGRESS_TOOL_NAMES,
    ToolPolicy,
    build_effective_tool_policy,
    is_web_egress_operation,
)
from src.tool_types import ToolBlock
from src.turn_contract import (
    computation_request,
    conversational_retrieval_intent,
    requested_capabilities,
    selected_tools_for_request,
)
from tests.test_research_parallel_turn_authority import _authority, _contract, _run_parent

WEB_OFF = set(WEB_EGRESS_TOOL_NAMES) | {WEB_EGRESS_MARKER}


# --- computation precedence -------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "What is the SHA-256 of hello world?",
    "what is the SHA-256 hash of the exact string hello world?",
    'what is the md5 of "abc"',
    "base64 encode the word mabel",
    "what is 1234 * 5678?",
    "what is 2+2?",
    "what's 17% of 2,340 euros?",
    "what is the square root of 7921?",
    "how many days are there between 14 March 1987 and 3 November 2026?",
    "what day of the week was 4 July 1976?",
    "generate a random UUID v4 for me",
])
def test_computation_routes_to_core_with_python_not_web_seal(message):
    assert computation_request(message)
    assert conversational_retrieval_intent(message) == "computation"
    assert requested_capabilities(message) == frozenset()
    seal = selected_tools_for_request(message)
    assert seal is None or not ({"web_search", "web_fetch"} & set(seal))


@pytest.mark.parametrize("message", [
    "what is SHA-256?",
    "what is the population of Sweden?",
    "what is the capital of Australia?",
    "what is the anime Dandadan about?",
])
def test_genuine_public_lookups_still_use_web_search(message):
    assert not computation_request(message)
    assert "search_browser" in requested_capabilities(message)
    assert "web_search" in (selected_tools_for_request(message) or ())


def test_more_specific_routes_keep_precedence_over_computation():
    # Personal-store date math stays on the store; URLs keep the fetch seal.
    assert not computation_request("how many days until my dentist appointment?")
    assert "calendar" in requested_capabilities("how many days until my dentist appointment?")
    assert not computation_request("summarize https://example.com/2024/05/report")
    assert selected_tools_for_request("summarize https://example.com/2024/05/report") == frozenset({"web_fetch"})


# --- web egress classification ----------------------------------------------------------

@pytest.mark.parametrize("tool,content,expected", [
    ("web_search", "{}", True),
    ("web_fetch", "{}", True),
    ("trigger_research", '{"topic": "x"}', True),
    ("search_hf_models", '{"query": "x"}', True),
    ("private_browser", '{"action": "open"}', True),
    ("mcp__builtin_browser__browser_navigate", "{}", True),
    ("pdf_extract", '{"url": "https://arxiv.org/pdf/1.pdf"}', True),
    ("pdf_extract", '{"path": "https://example.com/a.pdf"}', True),
    ("pdf_extract", '{"path": "/workspace/paper.pdf"}', False),
    ("research_parallel", '{"mode": "vault"}', False),
    ("vault_knowledge_search", "{}", False),
    ("manage_research", '{"action": "list"}', False),
])
def test_web_egress_classification(tool, content, expected):
    assert is_web_egress_operation(tool, content) is expected


# --- execution / admission under explicit web-off ---------------------------------------

async def _execute(tmp_path, authority, tool, args):
    return await execute_tool_block(
        ToolBlock(tool, json.dumps(args)),
        owner="alice", session_id="sess-parallel-auth", workspace=str(tmp_path),
        request_authority=authority, security_context=ToolRunSecurityContext(),
        tool_policy=ToolPolicy(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("web_search", {"query": "python free-threading"}),
    ("web_fetch", {"url": "https://example.com/report"}),
    ("trigger_research", {"topic": "4-bit MoE quantization"}),
    ("search_hf_models", {"query": "qwen"}),
    ("pdf_extract", {"url": "https://arxiv.org/pdf/2401.00001.pdf", "query": "table 2"}),
    ("mcp__builtin_browser__browser_navigate", {"url": "https://example.com"}),
])
async def test_explicit_web_off_refuses_every_web_entry_point(tmp_path, tool, args):
    authority = _authority(tmp_path, capabilities={"workspace_read", "search_browser"}, disabled=WEB_OFF)
    _, result = await _execute(tmp_path, authority, tool, args)
    assert result["executed"] is False and result["blocked"] is True
    assert result["failure_reason"] == "web_disabled"


@pytest.mark.asyncio
async def test_a_grant_cannot_bypass_explicit_web_off(tmp_path):
    # pdf_extract stays granted (it is local-capable); only its web form is refused.
    authority = _authority(tmp_path, capabilities={"workspace_read", "search_browser"},
                           disabled={WEB_EGRESS_MARKER})
    url_op = ExactOperation.normalize("pdf_extract", json.dumps({"url": "https://example.com/a.pdf", "query": "q"}))
    assert authority.permits(url_op)  # would be admitted without the web-off guard
    _, result = await _execute(tmp_path, authority, "pdf_extract", {"url": "https://example.com/a.pdf", "query": "q"})
    assert result["failure_reason"] == "web_disabled" and result["executed"] is False


@pytest.mark.asyncio
async def test_local_pdf_is_not_treated_as_web(tmp_path):
    authority = _authority(tmp_path, capabilities={"workspace_read", "search_browser"}, disabled=WEB_OFF)
    _, result = await _execute(tmp_path, authority, "pdf_extract", {"path": str(tmp_path / "missing.pdf"), "query": "q"})
    assert result.get("failure_reason") != "web_disabled"


@pytest.mark.asyncio
async def test_research_parallel_keeps_vault_branch_and_refuses_web_branch(tmp_path):
    authority = _authority(tmp_path, capabilities={"workspace_read"}, disabled=WEB_OFF)
    _, _, package, calls = await _run_parent(
        tmp_path, authority=authority, contract=_contract(), disabled_tools=WEB_OFF,
    )
    assert package["web"]["status"] == "denied"
    assert package["vault"]["status"] == "complete"
    assert "web_search" not in calls


# --- read-only web tools need no prompt; web-off and privileges still deny -------------

def _interactive(tmp_path, policy, caps):
    return create_request_authority("research quantized MoE models", owner="alice", session_id="s",
                                    workspace=str(tmp_path), policy=policy, capabilities=caps,
                                    interactive_review=True)


def test_read_only_web_tools_are_pre_granted_with_web_on(tmp_path):
    authority = _interactive(tmp_path, build_effective_tool_policy(), {"workspace_read", "search_browser"})
    for tool, args in (("web_search", {"query": "q"}), ("web_fetch", {"url": "https://example.com"}),
                       ("trigger_research", {"topic": "q"}), ("research_parallel",
                       {"mode": "both", "web_query": "q", "vault_objective": "v"})):
        assert authority.permits(ExactOperation.normalize(tool, json.dumps(args))), tool


def test_deep_research_denied_when_web_off_or_research_privilege_off(tmp_path):
    op = ExactOperation.normalize("trigger_research", json.dumps({"topic": "q"}))
    web_off = _interactive(tmp_path, build_effective_tool_policy(disabled_tools=WEB_OFF), {"workspace_read"})
    assert web_off.restricted(op) and not web_off.permits(op)
    no_priv = _interactive(tmp_path, build_effective_tool_policy(disabled_tools={"trigger_research"}),
                           {"workspace_read", "search_browser"})
    assert not no_priv.permits(op)


# --- route level: explicit web-off vs. an unset toggle -------------------------------------

async def _route_kwargs(monkeypatch, message, allow_web_search):
    from routes import chat_routes
    from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint

    observed = {}
    with monkeypatch.context() as patch:
        endpoint = _chat_stream_endpoint(patch, "agent", {}, session_model="qwen3.8-flash-next-gyro-gyro-s")
        patch.setattr(chat_routes, "coerce_message_and_session", lambda *a, **k: (message, "session-1"))

        async def capture(*args, **kwargs):
            observed.update(kwargs)
            yield 'data: {"delta": "ok"}\n\n'
            yield "data: [DONE]\n\n"

        patch.setattr(chat_routes, "stream_agent_loop", capture)
        request = _RouteRequest("agent")
        request._form.update({"message": message, "compare_mode": "false"})
        if allow_web_search is None:
            request._form.pop("allow_web_search", None)
        else:
            request._form["allow_web_search"] = allow_web_search
        response = await endpoint(request)
        async for _ in response.body_iterator:
            pass
    return observed


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "summarize https://example.com/reports/quarterly",          # exact-URL fetch exemption
    "open https://example.com in the private browser",          # explicit browser exemption
    "research how well 4-bit quantized MoE models hold up",     # deep research
])
async def test_route_explicit_web_off_denies_all_web_entry_points(monkeypatch, message):
    kwargs = await _route_kwargs(monkeypatch, message, "false")
    policy = kwargs["tool_policy"]
    for tool in ("web_search", "web_fetch", "private_browser", "trigger_research", "search_hf_models"):
        assert policy.blocks(tool), tool
    assert WEB_EGRESS_MARKER in policy.all_disabled_names()
    contract = kwargs.get("turn_contract")
    if contract is not None:
        assert not (set(contract.offered) & set(WEB_EGRESS_TOOL_NAMES))


@pytest.mark.asyncio
async def test_route_unset_toggle_is_not_treated_as_explicit_web_off(monkeypatch):
    kwargs = await _route_kwargs(monkeypatch, "research how well 4-bit quantized MoE models hold up", None)
    assert WEB_EGRESS_MARKER not in kwargs["tool_policy"].all_disabled_names()
    assert not kwargs["tool_policy"].blocks("trigger_research")


@pytest.mark.asyncio
async def test_contextual_web_followup_cannot_reenable_explicit_web_off(monkeypatch):
    from routes import chat_routes
    # Before the fix, a contextual web-turn follow-up passed None instead of the
    # explicit toggle and rewrote allow_web_search="false" to "true".
    monkeypatch.setattr(chat_routes, "_is_contextual_web_followup", lambda *a, **k: True)
    monkeypatch.setattr(chat_routes, "_has_recent_web_tool_event", lambda *a, **k: True)
    kwargs = await _route_kwargs(monkeypatch, "search the web again for reviews of the sequel", "false")
    assert kwargs["tool_policy"].blocks("web_search")
    assert kwargs["tool_policy"].blocks("web_fetch")

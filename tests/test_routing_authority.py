"""Stage 1 routing correctness: vault knowledge routing, contract authority, web-off partials."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src.tool_policy import WEB_ACCESS_TOOL_NAMES, build_effective_tool_policy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import requested_capabilities, resolve_turn_contract, vault_knowledge_families
from tests.test_agent_turn_contract_boundaries import contract, route, schema_function


@pytest.mark.parametrize("message,expected", [
    ("what do my notes say about quantization experiments?", {"workspace_read"}),
    ("find my note about Berserk and summarize what I wrote", {"workspace_read"}),
    ("how many of my vault notes are tagged #manga?", {"workspace_read"}),
    ("what did I write in my obsidian vault about Vinland Saga?", {"workspace_read"}),
    ("do my notes about the One Piece Egghead arc match what the wiki says?", {"workspace_read", "search_browser"}),
    ("check my vault notes on llama.cpp and tell me if there's a newer release online",
     {"workspace_read", "search_browser"}),
    ("compare what my notes say about Chainsaw Man part 2 with what people online think of it",
     {"workspace_read", "search_browser"}),
    # Live smoke regression: "public information" was missed by the web cue.
    ("Compare my notes on Lessons in Love with public information about the game.",
     {"workspace_read", "search_browser"}),
    ("check my vault notes against publicly available reviews", {"workspace_read", "search_browser"}),
])
def test_vault_knowledge_questions_route_to_vault_retrieval(message, expected):
    assert vault_knowledge_families(message) == expected
    # The authorization-equivalent condition (vault reachable through an active
    # directory root) is enough; a bound single workspace is not required.
    assert requested_capabilities(message, vault_scope=True) == expected


@pytest.mark.parametrize("message", [
    "add a note: buy oat milk and coffee filters",
    "create a note called packing list with passport and charger",
    "update my note about Berserk with chapter 380",
    "delete my notes about the old router",
])
def test_note_creation_and_editing_keep_their_routing(message):
    assert vault_knowledge_families(message) is None
    assert "workspace_read" not in requested_capabilities(message, vault_scope=False) or \
        requested_capabilities(message, vault_scope=True) == requested_capabilities(message, workspace=True)


def test_without_a_reachable_vault_notes_questions_are_unchanged():
    assert requested_capabilities("what do my notes say about quantization?") == frozenset({"notes"})


def test_vault_scope_requires_vault_inside_an_authorized_root(tmp_path, monkeypatch):
    from routes.chat_routes import _configured_vault_in_scope
    from src.agent_runtime import vault_target
    vault = tmp_path / "docs" / "core of truth"
    vault.mkdir(parents=True)
    other = tmp_path / "repo"
    other.mkdir()
    monkeypatch.setattr(vault_target, "persisted_vault_path", lambda: str(vault.resolve()))

    def scope(*paths):
        return SimpleNamespace(available_roots=tuple(SimpleNamespace(path=str(p), backend="local") for p in paths))

    assert _configured_vault_in_scope("", scope(other, tmp_path / "docs"))
    assert _configured_vault_in_scope(str(vault), None)
    assert not _configured_vault_in_scope("", scope(other))
    assert not _configured_vault_in_scope("", None)
    remote = SimpleNamespace(available_roots=(SimpleNamespace(path=str(vault), backend="ssh"),))
    assert not _configured_vault_in_scope("", remote)
    monkeypatch.setattr(vault_target, "persisted_vault_path", lambda: None)
    assert not _configured_vault_in_scope(str(vault), None)


def test_contract_is_not_narrowed_by_stale_relevant_tools():
    schemas = schema_function()(route("", True, False))
    assert [s["function"]["name"] for s in schemas] == ["manage_calendar"]


def test_only_the_explicit_rewrite_seal_narrows_a_contract():
    sealed = dict(route("", True, False), contract_seal=frozenset({"bash"}))
    names = [s["function"]["name"] for s in schema_function()(sealed)]
    assert "manage_calendar" not in names and names == ["bash"]


def _collect(stream):
    async def run():
        return [chunk async for chunk in stream]
    return asyncio.run(run())


def test_web_off_combined_request_keeps_vault_tools_and_reports_web_gap(monkeypatch):
    from src import agent_loop
    message = "compare what my notes say about Chainsaw Man part 2 with what people online think of it"
    web = set(WEB_ACCESS_TOOL_NAMES)
    policy = build_effective_tool_policy(disabled_tools=web)
    turn = resolve_turn_contract(
        capabilities={"workspace_read", "search_browser"}, schemas=FUNCTION_TOOL_SCHEMAS,
        policy=policy, message=message,
    )
    assert "vault_knowledge_search" in turn.offered and not (turn.offered & web)
    seen = {"tools": None, "system": "", "calls": 0}
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())

    async def fake_stream(candidates, messages, **kwargs):
        factory = kwargs.get("candidate_request_factory")
        request = await factory(0, *candidates[0]) if factory else {"messages": messages, "kwargs": kwargs}
        seen["calls"] += 1
        seen["tools"] = sorted(t["function"]["name"] for t in (request["kwargs"].get("tools") or []))
        seen["system"] = next((m["content"] for m in request["messages"] if m.get("role") == "system"), "")
        yield 'data: {"delta": "Your notes say X; the web part could not be checked."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    chunks = _collect(agent_loop.stream_agent_loop(
        "http://127.0.0.1:8080/v1", "qwen3.8-flash-next-gyro-gyro-s",
        [{"role": "user", "content": message}],
        max_rounds=1, owner="alice", fallbacks=[], fallback_on_empty=False,
        disabled_tools=web, tool_policy=policy, turn_contract=turn,
    ))
    text = "".join(chunks)
    assert "Web access is disabled for this turn. Enable web search" not in text
    assert seen["calls"] == 1
    assert "vault_knowledge_search" in seen["tools"] and not set(seen["tools"]) & web
    assert "web/online part could not be checked" in seen["system"]


def test_conversational_core_is_smaller_but_keeps_tools_discoverable():
    from src.turn_contract import CONTRACT_CORE_TOOLS, CONVERSATIONAL_CORE_TOOLS
    turn = resolve_turn_contract(capabilities=set(), schemas=FUNCTION_TOOL_SCHEMAS,
                                 policy=build_effective_tool_policy(disabled_tools=set()),
                                 message="thanks, that actually helped a lot!")
    assert not {"bash", "vault_batch_edit"} & turn.offered
    assert {"web_search", "research_parallel", "ask_user", "python"} <= turn.offered
    catalog = {json.loads(s)["function"]["name"] for s in turn.catalog_json}
    assert {"bash", "vault_batch_edit"} <= catalog  # still discoverable when authorized
    # The legacy non-contract tool floor is unchanged.
    assert {"bash", "python", "vault_batch_edit"} <= CONTRACT_CORE_TOOLS
    assert CONVERSATIONAL_CORE_TOOLS < CONTRACT_CORE_TOOLS


def test_denied_tools_are_not_made_discoverable():
    turn = resolve_turn_contract(capabilities=set(), schemas=FUNCTION_TOOL_SCHEMAS,
                                 policy=build_effective_tool_policy(disabled_tools={"bash"}),
                                 message="hi")
    catalog = {json.loads(s)["function"]["name"] for s in turn.catalog_json}
    assert "bash" not in catalog and "bash" not in turn.offered

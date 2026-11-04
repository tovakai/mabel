"""Simple vault questions get the obvious index search before round 1."""
import asyncio
import json

import pytest

from src.agent_runtime.vault_lookup_budget import knowledge_question_topic
from src.tool_policy import build_effective_tool_policy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import resolve_turn_contract


@pytest.mark.parametrize("text,topic", [
    ("What do my notes say about Lessons in Love?", "Lessons in Love"),
    ("what does my vault say about Berserk", "Berserk"),
    ("Do I have any notes on Dome Keeper?", "Dome Keeper"),
    ("What's in my notes about Takashi Miike?", "Takashi Miike"),
])
def test_topic_is_extracted(text, topic):
    assert knowledge_question_topic(text) == topic


@pytest.mark.parametrize("text", [
    "So, I just finished chapter 1 in Lessons in Love, and just wow.",
    "Find my documentation about Mabel in my vault.",  # locator, not a knowledge question
    "Compare my notes on Lessons in Love with public information about the game.",
    "What do my notes say?",
    "Correct the Selebus spelling mistake in my Lessons in Love note",
])
def test_no_topic_no_prefetch(text):
    assert knowledge_question_topic(text) is None


MESSAGE = "What do my notes say about Lessons in Love?"
_HIT = json.dumps({"matches": [{"rel_path": "Library/Games/Lessons In Love.md",
                                "excerpt": "Creator: Selebus marker-91c2"}]})


def _run(monkeypatch, tmp_path, *, capabilities, search_result):
    from src import agent_loop
    from src.agent_runtime.authority import create_request_authority

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    executed = []

    async def fake_execute(block, **kwargs):
        executed.append((block.tool_type, block.content))
        return block.tool_type, dict(search_result)

    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)
    policy = build_effective_tool_policy(disabled_tools=set())
    turn = resolve_turn_contract(capabilities=set(capabilities), schemas=FUNCTION_TOOL_SCHEMAS,
                                 policy=policy, message=MESSAGE)
    authority = create_request_authority(MESSAGE, owner="alice", session_id="s1", workspace=str(tmp_path),
                                         policy=policy, capabilities=set(capabilities))
    seen = []

    async def fake_stream(candidates, messages, **kwargs):
        factory = kwargs.get("candidate_request_factory")
        seen.append(await factory(0, *candidates[0]) if factory else {"messages": messages})
        yield 'data: {"delta": "Your note says the creator is Selebus."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    async def collect():
        return [c async for c in agent_loop.stream_agent_loop(
            "http://127.0.0.1:8080/v1", "qwen3.8-flash-next-gyro-gyro-s",
            [{"role": "user", "content": MESSAGE}], max_rounds=4, owner="alice", session_id="s1",
            workspace=str(tmp_path), fallbacks=[], fallback_on_empty=False, tool_policy=policy,
            turn_contract=turn, request_authority=authority)]
    return asyncio.run(collect()), seen, executed


def test_first_request_already_has_the_index_results(monkeypatch, tmp_path):
    chunks, seen, executed = _run(monkeypatch, tmp_path, capabilities={"workspace_read"},
                                  search_result={"output": _HIT, "exit_code": 0})
    assert executed == [("vault_knowledge_search", json.dumps({"query": "Lessons in Love", "top_k": 10}))]
    first = json.dumps(seen[0]["messages"])
    assert "marker-91c2" in first
    assert len(seen) == 1, "the model can answer in its first round"
    assert any('"tool_output"' in c and "vault_knowledge_search" in c for c in chunks), "shown in the UI"


def test_failed_or_empty_prefetch_is_not_injected(monkeypatch, tmp_path):
    _, seen, executed = _run(monkeypatch, tmp_path, capabilities={"workspace_read"},
                             search_result={"error": "denied", "exit_code": 1, "blocked": True})
    assert executed
    assert "prefetch_vault_search" not in json.dumps(seen[0]["messages"])


def test_no_prefetch_when_the_search_tool_is_not_offered(monkeypatch, tmp_path):
    _, _, executed = _run(monkeypatch, tmp_path, capabilities=set(),
                          search_result={"output": _HIT, "exit_code": 0})
    assert executed == []


# --- library plans --------------------------------------------------------------

from src.agent_runtime.vault_lookup_budget import vault_prefetch_requests


def test_library_question_plans():
    assert vault_prefetch_requests("What did I rate Berserk?") == [{"query": "Berserk", "top_k": 10}]
    assert vault_prefetch_requests("Have I watched Monster?") == [{"query": "Monster", "top_k": 10}]
    rated = vault_prefetch_requests("Which manga have I rated 10?")
    assert rated == [{"mode": "metadata_only", "property_filters": {"Type": "Comic", "My Rating": 10},
                      "fields": [], "top_k": 200}]
    anime = vault_prefetch_requests("Recommend me an anime I haven't seen yet, based on my preferences.")
    assert [p.get("sort_by") for p in anime] == ["My Rating", None]
    assert anime[1]["fields"] == [] and anime[1]["property_filters"] == {"Type": "Anime"}
    game = vault_prefetch_requests("Recommend me a game based on what I've been playing.")
    assert game[0]["sort_by"] == "Playtime"


def test_no_plan_for_generic_edits_or_web_comparisons():
    for text in ("Recommend me a good anime", "Fix the rating in my Berserk note",
                 "Compare my notes on Berserk with public information about the manga.", "Hey!"):
        assert vault_prefetch_requests(text) == []


def test_recommendation_turn_gets_library_and_guidance(monkeypatch, tmp_path):
    global MESSAGE
    original = MESSAGE
    MESSAGE = "Recommend me an anime I haven't seen yet, based on my preferences."
    try:
        listing = json.dumps({"matches": [{"title": "Pluto-marker-77", "My Rating": 10, "Genre": ["Mystery"]}]})
        _, seen, executed = _run(monkeypatch, tmp_path, capabilities={"workspace_read"},
                                 search_result={"output": listing, "exit_code": 0})
    finally:
        MESSAGE = original
    assert len(executed) == 2
    first = json.dumps(seen[0]["messages"])
    assert "Pluto-marker-77" in first
    assert "Recommend titles that are NOT in that list" in first

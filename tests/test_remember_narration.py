""""remember" in narration is conversation, not a memory command (live chat f63204b0)."""
import asyncio
import json

import pytest

from src.agent_loop import _extract_memory_add_text_from_user, _state_manager_expected_action
from src.tool_policy import build_effective_tool_policy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import requested_capabilities, resolve_turn_contract

YOSUGA = ('I was actually thinking of "Yosuga no Sora". I\'ve seen the anime and remember that it was '
          'good, but I can\'t remember the details, so I think it\'s a good starting point.')


@pytest.mark.parametrize("text", [
    YOSUGA,
    "I remember when we talked about Berserk",
    "We finished Lost and remember how sad the ending was",
    "I can't remember the name of that anime",
])
def test_narration_is_not_a_memory_request(text):
    assert "memory" not in requested_capabilities(text)
    assert _state_manager_expected_action(text, set(), {"manage_memory"}) == ("", set())
    assert _extract_memory_add_text_from_user(text) == ""


@pytest.mark.parametrize("text,saved", [
    ("Please remember my birthday is May 3", "my birthday is May 3"),
    ("Can you remember that I'm allergic to nuts?", "I'm allergic to nuts"),
    ("remember this: the wifi password changed", "the wifi password changed"),
    ("I watched it yesterday. Remember that I liked it.", "I liked it"),
])
def test_addressed_requests_still_save(text, saved):
    assert "memory" in requested_capabilities(text)
    tool, actions = _state_manager_expected_action(text, set(), {"manage_memory"})
    assert tool == "manage_memory" and "add" in actions
    assert _extract_memory_add_text_from_user(text) == saved


def test_compound_request_keeps_its_memory_part():
    assert {"memory", "search_browser"} <= requested_capabilities("Look up Frieren and remember that I liked it")


def test_the_live_turn_keeps_its_answer(monkeypatch, tmp_path):
    from src import agent_loop
    from src.agent_runtime.authority import create_request_authority
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    executed = []

    async def fake_execute(block, **kwargs):
        executed.append(block.tool_type)
        return block.tool_type, {"output": "ok", "exit_code": 0}
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)

    async def fake_stream(candidates, messages, **kwargs):
        yield 'data: {"delta": "Solid pick: Yosuga no Sora is a good first VN for reading practice."}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    policy = build_effective_tool_policy(disabled_tools=set())
    caps = set(requested_capabilities(YOSUGA))
    turn = resolve_turn_contract(capabilities=caps, schemas=FUNCTION_TOOL_SCHEMAS, policy=policy, message=YOSUGA)
    authority = create_request_authority(YOSUGA, owner="alice", session_id="s1", workspace=str(tmp_path),
                                         policy=policy, capabilities=caps, interactive_review=True)

    async def collect():
        return [c async for c in agent_loop.stream_agent_loop(
            "http://127.0.0.1:8080/v1", "qwen3.8-flash-next-gyro-gyro-s", [{"role": "user", "content": YOSUGA}],
            max_rounds=4, owner="alice", session_id="s1", workspace=str(tmp_path), fallbacks=[],
            fallback_on_empty=False, tool_policy=policy, turn_contract=turn, request_authority=authority)]
    chunks = asyncio.run(collect())
    assert "manage_memory" not in executed
    stream = "".join(chunks)
    assert "Solid pick" in stream and "Allow this action" not in stream



# --- an approved write ends the turn only when it was the whole request -----------------

@pytest.mark.parametrize("capabilities,side", [
    ({"memory"}, False),                      # "Please remember that I liked Frieren" -> "Done."
    ({"memory", "search_browser"}, True),     # "Look up Frieren and remember..." -> keep answering
    ({"search_browser"}, True),               # a write outside the request -> keep answering
    (set(), True),                            # plain conversation -> keep answering
])
def test_approved_memory_write_is_a_side_action_unless_it_was_the_request(capabilities, side):
    from src.agent_loop import _approved_write_is_side_action
    assert _approved_write_is_side_action("manage_memory", capabilities) is side


@pytest.mark.parametrize("text,families", [
    ("Mabel, remember this: the wifi password changed", {"memory"}),
    ("Hey Mabel, remember that I start work at 9", {"memory"}),
    ("Mabel: what do my notes say about Berserk?", {"workspace_read"}),
    ("Mabel is a nice name", set()),
])
def test_addressing_mabel_by_name_does_not_hide_the_request(text, families):
    assert set(requested_capabilities(text, vault_scope=True)) == families

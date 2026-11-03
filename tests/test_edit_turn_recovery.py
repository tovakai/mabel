"""Edit turns: room for large writes, recovery from cut-off edits, and edit routing."""
import asyncio
import json

import pytest

from src.generation_budget import agent_output_reserve
from src.tool_policy import build_effective_tool_policy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import (
    VAULT_NOTE_REWRITE_TOOLS,
    looks_like_vault_note_rewrite,
    requested_capabilities,
    resolve_turn_contract,
    selected_tools_for_request,
)


# --- output reserve -----------------------------------------------------------------------

def test_answer_only_turns_keep_the_small_reserve():
    assert agent_output_reserve(0, 32768, thinking_mode="off", tool_names={"web_search"}) == 1024


@pytest.mark.parametrize("context,expected", [(32768, 4915), (65536, 8192)])
def test_reasoning_or_write_capable_turns_reserve_room_for_the_write(context, expected):
    assert agent_output_reserve(0, context, thinking_mode=None, tool_names={"read_file"}) == expected
    assert agent_output_reserve(0, context, thinking_mode="off", tool_names={"edit_file"}) == expected


def test_reserve_without_a_known_window_is_unchanged():
    assert agent_output_reserve(0, 0, tool_names={"edit_file"}) == 1024


# --- edit routing ------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "Feel free to: \r\n\r\nCorrect the Selebus spelling mistake\r\npad out the existing notes regarding Lessons in Love.",
    "pad out my notes about Lessons in Love",
    "Correct the Selebus spelling mistake in my Lessons in Love note",
    "flesh out my Berserk note with the chapter 380 events",
])
def test_note_edit_requests_get_the_read_edit_seal(message):
    assert looks_like_vault_note_rewrite(message)
    assert selected_tools_for_request(message) == VAULT_NOTE_REWRITE_TOOLS
    assert requested_capabilities(message, vault_scope=True) == frozenset()


@pytest.mark.parametrize("message,families", [
    ("What do my notes say about Lessons in Love?", {"workspace_read"}),
    ("add a note: buy milk", {"notes"}),
])
def test_questions_and_note_creation_keep_their_routing(message, families):
    assert not looks_like_vault_note_rewrite(message)
    assert requested_capabilities(message, vault_scope=True) == families


@pytest.mark.parametrize("message", [
    "correct me if I am wrong, but is TCP reliable?",
    "update all notes in the vault with a new tag",
    "improve my essay intro please",
])
def test_non_note_or_bulk_edits_are_not_sealed(message):
    assert not looks_like_vault_note_rewrite(message)


# --- recovery from a cut-off edit call ----------------------------------------------------

def _collect(stream):
    async def run():
        return [chunk async for chunk in stream]
    return asyncio.run(run())


def _run_loop(monkeypatch, tmp_path, scripted_rounds, max_rounds=6):
    from src import agent_loop
    from src.agent_runtime.authority import create_request_authority

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    message = "Correct the Selebus spelling mistake in my Lessons in Love note"
    policy = build_effective_tool_policy(disabled_tools=set())
    turn = resolve_turn_contract(capabilities=set(), schemas=FUNCTION_TOOL_SCHEMAS, policy=policy,
                                 selected_tools=VAULT_NOTE_REWRITE_TOOLS, message=message)
    authority = create_request_authority(message, owner="alice", session_id="", workspace=str(tmp_path),
                                         policy=policy, capabilities={"workspace_read"}, interactive_review=True)
    seen = []

    async def fake_stream(candidates, messages, **kwargs):
        factory = kwargs.get("candidate_request_factory")
        request = await factory(0, *candidates[0]) if factory else {"messages": messages}
        seen.append(request)
        step = scripted_rounds[min(len(seen) - 1, len(scripted_rounds) - 1)]
        for chunk in step:
            yield chunk
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    chunks = _collect(agent_loop.stream_agent_loop(
        "http://127.0.0.1:8080/v1", "qwen3.8-flash-next-gyro-gyro-s",
        [{"role": "user", "content": message}],
        max_rounds=max_rounds, owner="alice", workspace=str(tmp_path), fallbacks=[], fallback_on_empty=False,
        tool_policy=policy, turn_contract=turn, request_authority=authority,
    ))
    return seen, chunks


# Arguments cut off mid-string, as when generation hits the context window.
_TRUNCATED_EDIT = 'data: ' + json.dumps({"type": "tool_calls", "calls": [{
    "id": "c1", "name": "edit_file",
    "arguments": '{"path": "Library/Games/Lessons In Love.md", "old_string": "Creator: Selbius", "new_string": "Creator: Seleb',
}]}) + "\n\n"
_PROMISE = 'data: {"delta": "I corrected the spelling. Making the edits now."}\n\n'
_FINAL = 'data: {"delta": "Done after a smaller edit."}\n\n'


def test_cut_off_edit_does_not_end_the_turn(monkeypatch, tmp_path):
    seen, _ = _run_loop(monkeypatch, tmp_path, [[_PROMISE, _TRUNCATED_EDIT], [_FINAL]])
    assert len(seen) == 2, "the turn must continue after the dropped edit"
    retry_messages = json.dumps(seen[1]["messages"])
    assert "NOTHING was written" in retry_messages
    assert "Do not say the change was made" in retry_messages


def test_cut_off_edit_retries_are_bounded(monkeypatch, tmp_path):
    seen, _ = _run_loop(monkeypatch, tmp_path, [[_PROMISE, _TRUNCATED_EDIT]], max_rounds=8)
    # Two recovery rounds, plus at most two promise nudges ("Making the edits
    # now."), then the loop stops retrying the same failure.
    assert 3 <= len(seen) <= 5


# --- text-format tool calls and promise-only endings ------------------------------------

# Round 17 of the live failure: the call is written as text, for a tool not offered.
_TEXT_CALL = 'data: ' + json.dumps({"delta": (
    "<tool_call>\n<function=web_search>\n<parameter=query>\n"
    "Lessons in Love Selebus release date\n</parameter>\n</function>\n</tool_call>"
)}) + "\n\n"


def test_text_format_tool_call_does_not_end_the_turn(monkeypatch, tmp_path):
    seen, _ = _run_loop(monkeypatch, tmp_path, [[_TEXT_CALL], [_FINAL]])
    assert len(seen) == 2, "the turn must continue after an unexecuted text tool call"
    retry = json.dumps(seen[1]["messages"])
    assert "was NOT executed" in retry
    assert "web_search is not available in this turn" in retry
    assert "edit_file" in retry


def test_text_format_tool_call_retries_are_bounded(monkeypatch, tmp_path):
    seen, _ = _run_loop(monkeypatch, tmp_path, [[_TEXT_CALL]], max_rounds=8)
    assert 3 <= len(seen) <= 5


def test_text_tool_call_name_extraction():
    from src.agent_loop import _text_tool_call_names
    assert _text_tool_call_names("<tool_call>\n<function=web_search>\n") == {"web_search"}
    assert _text_tool_call_names('<invoke name="read_file">') == {"read_file"}
    assert _text_tool_call_names("<tool_call>{}") == {"?"}
    assert _text_tool_call_names("Lessons in Love is a visual novel.") == set()
    assert _text_tool_call_names("<think><tool_call><function=ls></think>Done.") == set()


_GERUND_PROMISE = 'data: {"delta": "Doing a fresh inventory + exact read first, so I don\'t rewrite from memory."}\n\n'


def test_promise_only_reply_is_nudged_not_accepted(monkeypatch, tmp_path):
    seen, _ = _run_loop(monkeypatch, tmp_path, [[_GERUND_PROMISE], [_FINAL]])
    assert len(seen) == 2, "a reply that only announces the next step must not end the turn"

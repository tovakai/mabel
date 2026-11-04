"""An approval pause must not erase what the turn already read."""
import asyncio
import json

from src.agent_runtime import turn_continuation
from src.tool_approvals import SINGLE_ACTION_APPROVAL_DECISION, tool_approval_store
from src.tool_policy import build_effective_tool_policy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import VAULT_NOTE_REWRITE_TOOLS, resolve_turn_contract

MESSAGE = "Correct the Selebus spelling mistake in my Lessons in Love note"
NOTE_TEXT = "Creator: Selbius\nA unique marker line 7f3a9c."


def _call(cid, name, args):
    return "data: " + json.dumps({"type": "tool_calls", "calls": [
        {"id": cid, "name": name, "arguments": json.dumps(args)}]}) + "\n\n"


def _collect(stream):
    async def run():
        return [chunk async for chunk in stream]
    return asyncio.run(run())


def _run(monkeypatch, tmp_path, script, *, exact_approval=None, seen):
    from src import agent_loop
    from src.agent_runtime.authority import create_request_authority

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    from src import tool_execution
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    policy = build_effective_tool_policy(disabled_tools=set())
    turn = resolve_turn_contract(capabilities=set(), schemas=FUNCTION_TOOL_SCHEMAS, policy=policy,
                                 selected_tools=VAULT_NOTE_REWRITE_TOOLS, message=MESSAGE)
    authority = (exact_approval.pending.request_authority if exact_approval is not None else None) or \
        create_request_authority(MESSAGE, owner="alice", session_id="s1", workspace=str(tmp_path),
                                 policy=policy, capabilities={"workspace_read"}, interactive_review=True)
    start = len(seen)

    async def fake_stream(candidates, messages, **kwargs):
        factory = kwargs.get("candidate_request_factory")
        request = await factory(0, *candidates[0]) if factory else {"messages": messages}
        seen.append(request)
        step = script[min(len(seen) - start - 1, len(script) - 1)]
        for chunk in step:
            yield chunk
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    return _collect(agent_loop.stream_agent_loop(
        "http://127.0.0.1:8080/v1", "qwen3.8-flash-next-gyro-gyro-s",
        [{"role": "user", "content": MESSAGE}],
        max_rounds=6, owner="alice", session_id="s1", workspace=str(tmp_path), fallbacks=[],
        fallback_on_empty=False, tool_policy=policy, turn_contract=turn, request_authority=authority,
        exact_approval=exact_approval,
    ))


def _approval_id(chunks):
    for chunk in chunks:
        if not chunk.startswith("data: "):
            continue
        try:
            event = json.loads(chunk[6:])
        except ValueError:
            continue
        card = event.get("ask_user") if isinstance(event, dict) else None
        if isinstance(card, dict) and card.get("approval_id"):
            return card["approval_id"]
        data = event.get("data") if isinstance(event, dict) else None
        if isinstance(data, dict) and data.get("approval_id"):
            return data["approval_id"]
    return None


def test_resume_after_approval_keeps_what_was_read(monkeypatch, tmp_path):
    turn_continuation.clear()
    note = tmp_path / "Lessons In Love.md"
    note.write_text(NOTE_TEXT)
    seen = []
    first = _run(monkeypatch, tmp_path, [
        [_call("r1", "read_file", {"path": str(note)})],
        [_call("e1", "edit_file", {"path": str(note), "old_string": "Selbius", "new_string": "Selebus"})],
    ], seen=seen)
    approval_id = _approval_id(first)
    assert approval_id, "the edit must stop at an approval card"
    assert note.read_text() == NOTE_TEXT, "nothing is written before approval"

    approval = tool_approval_store.consume(approval_id, decision=SINGLE_ACTION_APPROVAL_DECISION,
                                           owner="alice", session_id="s1")
    assert approval is not None
    resumed_from = len(seen)
    _run(monkeypatch, tmp_path, [['data: {"delta": "Fixed the creator spelling."}\n\n']],
         exact_approval=approval, seen=seen)

    assert "Selebus" in note.read_text()
    resumed = json.dumps(seen[resumed_from]["messages"])
    assert "7f3a9c" in resumed, "the resumed request must still contain the note it read"
    assert "APPROVAL REQUIRED" not in resumed, "the placeholder is replaced by the real result"
    assert turn_continuation.take(approval_id, owner="alice", session_id="s1") is None, "one use"


def test_continuation_is_bound_to_owner_and_session():
    turn_continuation.clear()
    work = [{"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "secret note"}]
    turn_continuation.save("a1", owner="alice", session_id="s1", messages=work, placeholder_call_id="c1")
    assert turn_continuation.take("a1", owner="mallory", session_id="s1") is None
    turn_continuation.save("a1", owner="alice", session_id="s1", messages=work, placeholder_call_id="c1")
    assert turn_continuation.take("a1", owner="alice", session_id="other") is None


def test_continuation_is_bounded():
    turn_continuation.clear()
    work = [{"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 20_000} for i in range(6)]
    turn_continuation.save("big", owner="a", session_id="s", messages=work)
    entry = turn_continuation.take("big", owner="a", session_id="s")
    total = sum(len(m["content"]) for m in entry.messages)
    assert total <= turn_continuation.MAX_CONTINUATION_CHARS
    assert entry.messages[-1]["content"] == "x" * 20_000, "newest results are kept"


# --- working set: identical repeat reads ---------------------------------------------

def test_identical_repeat_read_is_replaced_by_a_pointer():
    from src.agent_loop import _dedupe_repeated_read_result
    body = "read_file: note\n" + "Creator: Selbius\n" * 20
    messages = [{"role": "tool", "tool_call_id": "r1", "content": body}]
    pointer = _dedupe_repeated_read_result("read_file", body, messages, [])
    assert pointer.startswith("[Unchanged:")
    # Changed content, a non-read tool, short output, or content no longer in context: unchanged.
    assert _dedupe_repeated_read_result("read_file", body + "x", messages, []) == body + "x"
    assert _dedupe_repeated_read_result("edit_file", body, messages, []) == body
    assert _dedupe_repeated_read_result("read_file", "short", [{"role": "tool", "content": "short"}], []) == "short"
    assert _dedupe_repeated_read_result("read_file", body, [], []) == body
    # A duplicate within the same round also points back.
    assert _dedupe_repeated_read_result("ls", body, [], [body]).startswith("[Unchanged:")


def test_resumed_turn_dedupes_a_reread_of_an_unchanged_note(monkeypatch, tmp_path):
    # The in-request exact-repeat guard does not survive an approval resume;
    # the restored context plus the output check does.
    turn_continuation.clear()
    note = tmp_path / "Lessons In Love.md"
    note.write_text(NOTE_TEXT)
    other = tmp_path / "Sensei.md"
    other.write_text("Sensei note\n" + "line\n" * 60)
    seen = []
    first = _run(monkeypatch, tmp_path, [
        [_call("r1", "read_file", {"path": str(other)})],
        [_call("e1", "edit_file", {"path": str(note), "old_string": "Selbius", "new_string": "Selebus"})],
    ], seen=seen)
    approval = tool_approval_store.consume(_approval_id(first), decision=SINGLE_ACTION_APPROVAL_DECISION,
                                           owner="alice", session_id="s1")
    _run(monkeypatch, tmp_path, [
        [_call("r2", "read_file", {"path": str(other)})],
        ['data: {"delta": "Done."}\n\n'],
    ], exact_approval=approval, seen=seen)
    last = json.dumps(seen[-1]["messages"])
    assert last.count("Sensei note") == 1, "the unchanged re-read is not repeated in context"
    assert "[Unchanged:" in last

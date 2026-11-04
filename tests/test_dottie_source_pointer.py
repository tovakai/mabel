"""After dispatch_dottie returns note texts, the first re-read of such a note gets a pointer."""
import asyncio
import json

from src.agent_loop import _dottie_source_hit, _dottie_sources
from src.tool_policy import build_effective_tool_policy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import resolve_turn_contract

DOTTIE_OUTPUT = json.dumps({"status": "partial", "findings": [], "sources": [
    {"note_id": "n-kate", "rel_path": "Library/Fictional Characters/Lost/Kate Austen.md", "text": "Kate..."}]})
MESSAGE = "Ask Dottie to summarize what my notes say about the characters from Lost."


def test_source_matching_by_path_note_id_and_absolute_path():
    sources = _dottie_sources(DOTTIE_OUTPUT)
    rel = "Library/Fictional Characters/Lost/Kate Austen.md"
    assert _dottie_source_hit("vault_read_range", json.dumps({"note_id": "n-kate"}), sources) == rel
    assert _dottie_source_hit("read_file", json.dumps({"path": "/workspace/" + rel}), sources) == rel
    assert _dottie_source_hit("read_file", "/home/x/core of truth/" + rel, sources) == rel
    assert _dottie_source_hit("read_file", json.dumps({"path": "/workspace/" + rel, "offset": 41}), sources) is None
    assert _dottie_source_hit("read_file", json.dumps({"path": "/workspace/Other.md"}), sources) is None
    assert _dottie_source_hit("grep", json.dumps({"path": "/workspace/" + rel}), sources) is None


def _call(cid, name, args):
    return "data: " + json.dumps({"type": "tool_calls", "calls": [
        {"id": cid, "name": name, "arguments": json.dumps(args)}]}) + "\n\n"


def test_first_reread_is_pointed_second_one_runs(monkeypatch, tmp_path):
    from src import agent_loop
    from src.agent_runtime.authority import create_request_authority
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    executed = []

    async def fake_execute(block, **kwargs):
        executed.append(block.tool_type)
        if block.tool_type == "dispatch_dottie":
            return block.tool_type, {"output": DOTTIE_OUTPUT, "exit_code": 0}
        return block.tool_type, {"output": "full note text", "exit_code": 0}
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)
    read = {"path": "/workspace/Library/Fictional Characters/Lost/Kate Austen.md"}
    script = [[_call("d1", "dispatch_dottie", {"research_objective": "Lost characters"})],
              [_call("r1", "read_file", read)], [_call("r2", "read_file", read)],
              ['data: {"delta": "Summary."}\n\n']]
    seen = []

    async def fake_stream(candidates, messages, **kwargs):
        factory = kwargs.get("candidate_request_factory")
        seen.append(await factory(0, *candidates[0]) if factory else {"messages": messages})
        for chunk in script[min(len(seen) - 1, len(script) - 1)]:
            yield chunk
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    policy = build_effective_tool_policy(disabled_tools=set())
    turn = resolve_turn_contract(capabilities={"workspace_read"}, schemas=FUNCTION_TOOL_SCHEMAS, policy=policy,
                                 message=MESSAGE)
    authority = create_request_authority(MESSAGE, owner="alice", session_id="s1", workspace=str(tmp_path),
                                         policy=policy, capabilities={"workspace_read"})

    async def collect():
        return [c async for c in agent_loop.stream_agent_loop(
            "http://127.0.0.1:8080/v1", "qwen3.8-flash-next-gyro-gyro-s", [{"role": "user", "content": MESSAGE}],
            max_rounds=6, owner="alice", session_id="s1", workspace=str(tmp_path), fallbacks=[],
            fallback_on_empty=False, tool_policy=policy, turn_contract=turn, request_authority=authority)]
    asyncio.run(collect())
    assert executed.count("dispatch_dottie") == 1
    assert executed.count("read_file") == 1, "first re-read pointed, second executed"
    assert "already in the dispatch_dottie result" in json.dumps(seen[2]["messages"])

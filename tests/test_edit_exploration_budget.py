"""Note-edit turns: bounded browsing before the first write, and note-writing rules."""
import json

from src.agent_runtime.edit_exploration_budget import (
    NUDGE_AFTER, RESTRICT_AFTER, EditExplorationBudget,
)
from tests.test_edit_turn_recovery import _run_loop


def _ls(cid, path):
    return "data: " + json.dumps({"type": "tool_calls", "calls": [
        {"id": cid, "name": "ls", "arguments": json.dumps({"path": path})}]}) + "\n\n"


def _ok(name):
    return {"tool_name": name, "result": {"exit_code": 0}}


def test_budget_nudges_then_restricts_discovery():
    budget = EditExplorationBudget()
    decisions = [budget.observe([_ok("ls")]) for _ in range(RESTRICT_AFTER)]
    assert decisions[NUDGE_AFTER - 1] == "nudge"
    assert decisions[RESTRICT_AFTER - 1] == "restrict"
    assert decisions.count("nudge") == 1 and decisions.count("restrict") == 1


def test_budget_stops_counting_after_a_successful_write():
    budget = EditExplorationBudget()
    budget.observe([_ok("read_file")])
    budget.observe([_ok("edit_file")])
    assert all(budget.observe([_ok("read_file")]) is None for _ in range(20))


def test_failed_write_does_not_end_the_budget():
    budget = EditExplorationBudget()
    budget.observe([{"tool_name": "edit_file", "result": {"exit_code": 1, "error": "old_string not found"}}])
    assert not budget.wrote


def test_edit_turn_carries_rules_and_stops_endless_browsing(monkeypatch, tmp_path):
    from src import tool_execution
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    for i in range(12):
        (tmp_path / f"folder{i}").mkdir()
    rounds = [[_ls(f"l{i}", str(tmp_path / f"folder{i}"))] for i in range(12)]
    seen, chunks = _run_loop(monkeypatch, tmp_path, rounds, max_rounds=12)
    first = json.dumps(seen[0]["messages"])
    assert "Note-editing rules" in first
    later = json.dumps(seen[-1]["messages"])
    assert "make the requested edits now" in later
    assert "now unavailable for this turn" in later
    outputs = [json.loads(c[6:]) for c in chunks if c.startswith("data: {") and '"tool_output"' in c]
    refused = [o for o in outputs if o.get("tool") == "ls" and o.get("failure_kind") == "tool_unavailable"]
    assert refused, "listing is refused once the budget restricts discovery"

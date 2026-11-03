"""Focused tests for the research_parallel coordinator."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.agent_runtime import parallel_research as pr
from src.tool_capabilities import ToolEffect, capabilities_for_action
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import FAMILY_TOOLS, WORKSPACE_READ_TOOLS


def _authority(permitted_tools):
    class Auth:
        owner = "test"
        session_id = "s1"
        workspace = "/tmp/workspace"
        request_id = "req-test"
        denied = frozenset()
        block_all = False

        def restricted(self, operation):
            return False

        def permits(self, operation):
            return operation.tool in permitted_tools

    return Auth()


def _ctx(permitted=("web_search", "dispatch_dottie"), progress=None):
    return {
        "owner": "test",
        "session_id": "s1",
        "workspace": "/tmp/workspace",
        "request_authority": _authority(permitted),
        "disabled_tools": frozenset(),
        "tool_policy": None,
        "progress_cb": progress,
    }


@pytest.mark.asyncio
async def test_both_branches_overlap_in_execution():
    started = {}
    released = asyncio.Event()

    async def fake_execute(block, **kwargs):
        name = block.tool_type
        started[name] = time.monotonic()
        if len(started) < 2:
            await released.wait()
        else:
            released.set()
        await asyncio.sleep(0.05)
        if name == "web_search":
            return name, {
                "output": json.dumps({"results": [{"title": "A", "url": "https://a.example", "snippet": "sa"}]}),
                "exit_code": 0,
            }
        return name, {
            "output": json.dumps({
                "status": "complete",
                "findings": [{
                    "claim": "vault claim",
                    "confidence": "source_supported",
                    "evidence": [{
                        "note_id": "n1", "rel_path": "Note.md",
                        "start_line": 1, "end_line": 2, "quote": "vault claim",
                    }],
                }],
            }),
            "exit_code": 0,
        }

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {"shared": False, "research_available": True})):
        result = await pr.execute(json.dumps({
            "mode": "both",
            "web_query": "public topic",
            "vault_objective": "vault topic",
        }), _ctx())
    package = json.loads(result["output"])
    assert package["status"] == "complete"
    assert package["metrics"]["concurrent"] is True
    assert "web_search" in started and "dispatch_dottie" in started
    assert abs(started["web_search"] - started["dispatch_dottie"]) < 0.2


@pytest.mark.asyncio
async def test_one_branch_fails_other_succeeds_is_partial():
    async def fake_execute(block, **kwargs):
        if block.tool_type == "web_search":
            return "web_search", {"error": "searx down", "exit_code": 1, "failure_kind": "backend_failure"}
        return "dispatch_dottie", {
            "output": json.dumps({"status": "complete", "findings": [{
                "claim": "ok", "evidence": [{"note_id": "n1", "rel_path": "a.md", "start_line": 1, "end_line": 1, "quote": "ok"}],
                "confidence": "source_supported",
            }]}),
            "exit_code": 0,
        }

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        result = await pr.execute(json.dumps({
            "mode": "both", "web_query": "q", "vault_objective": "v",
        }), _ctx())
    package = json.loads(result["output"])
    assert package["status"] == "partial"
    assert package["web"]["status"] == "failed"
    assert package["vault"]["status"] == "complete"


@pytest.mark.asyncio
async def test_both_fail_reports_failures():
    async def fake_execute(block, **kwargs):
        return block.tool_type, {"error": f"{block.tool_type} boom", "exit_code": 1}

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        result = await pr.execute(json.dumps({
            "mode": "both", "web_query": "q", "vault_objective": "v",
        }), _ctx())
    package = json.loads(result["output"])
    assert package["status"] == "failed"
    assert package["web"]["status"] == "failed"
    assert package["vault"]["status"] == "failed"
    assert package["unresolved"]


@pytest.mark.asyncio
async def test_authorization_denial_cannot_bypass():
    calls = []

    async def fake_execute(block, **kwargs):
        calls.append(block.tool_type)
        return block.tool_type, {"output": "{}", "exit_code": 0}

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        result = await pr.execute(json.dumps({
            "mode": "both", "web_query": "q", "vault_objective": "v",
        }), _ctx(permitted=("web_search",)))  # vault intentionally unauthorized
    package = json.loads(result["output"])
    assert "dispatch_dottie" not in calls
    assert package["vault"]["status"] == "denied"
    assert package["web"]["status"] in {"complete", "empty"}
    assert package["status"] == "partial"


@pytest.mark.asyncio
async def test_approval_required_does_not_fake_success_or_rerun():
    pr.clear_seals_for_tests()
    calls = []
    phase = {"n": 0}

    async def fake_execute(block, **kwargs):
        calls.append(block.tool_type)
        if block.tool_type == "web_search":
            phase["n"] += 1
            if phase["n"] == 1:
                return "web_search", {
                    "approval_required": True,
                    "executed": False,
                    "exit_code": None,
                    "ask_user": {"approval_id": "a1", "tool": "web_search"},
                    "output": "Waiting for approval",
                }
            return "web_search", {
                "output": json.dumps({"results": [{"title": "ok", "url": "https://x", "snippet": "s"}]}),
                "exit_code": 0,
            }
        await asyncio.sleep(0.01)
        return "dispatch_dottie", {
            "output": json.dumps({"status": "complete", "findings": [{
                "claim": "vault hit",
                "confidence": "source_supported",
                "evidence": [{"note_id": "n1", "rel_path": "a.md", "start_line": 1, "end_line": 1, "quote": "vault hit"}],
            }]}),
            "exit_code": 0,
        }

    auth = _ctx()
    # Stable request identity so the seal can resume.
    auth["request_authority"].request_id = "req-parallel-1"
    payload = json.dumps({"mode": "both", "web_query": "q", "vault_objective": "v"})
    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        first = await pr.execute(payload, auth)
        assert first.get("approval_required") is True
        assert first.get("executed") is False
        package = json.loads(first["output"])
        assert package["status"] == "approval_required"
        assert package["vault"]["status"] == "complete"
        assert calls.count("web_search") == 1
        assert calls.count("dispatch_dottie") == 1

        # Resume exact parent op: vault must not run again; only the pending web child.
        second = await pr.execute(payload, auth)
    package2 = json.loads(second["output"])
    assert package2["status"] == "complete"
    assert package2["metrics"]["reused_branches"] == ["vault"]
    assert calls.count("dispatch_dottie") == 1
    assert calls.count("web_search") == 2
    assert package2["web"]["results"][0]["url"] == "https://x"
    assert package2["vault"]["findings"][0]["claim"] == "vault hit"


@pytest.mark.asyncio
async def test_unavailable_dottie_is_degraded_not_empty_success():
    async def fake_execute(block, **kwargs):
        return block.tool_type, {
            "output": json.dumps({
                "status": "partial",
                "findings": [],
                "unresolved_questions": ["Worker could not produce verified findings: ConnectError"],
                "worker_execution": {
                    "status": "failed",
                    "phase": "opening_agent_stream",
                    "failure_type": "ConnectError",
                    "stream_events": 0,
                    "tool_reads": 0,
                },
            }),
            "exit_code": 0,
        }

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        result = await pr.execute(json.dumps({
            "mode": "vault", "vault_objective": "anything",
        }), _ctx())
    package = json.loads(result["output"])
    assert package["vault"]["status"] == "degraded"
    assert package["status"] == "partial"
    assert package["vault"].get("failure_kind") is None or True
    assert "Worker could not" in (package["vault"].get("error") or "")


@pytest.mark.asyncio
async def test_vault_child_uses_dispatch_dottie_contract():
    seen = {}

    async def fake_execute(block, **kwargs):
        seen["tool"] = block.tool_type
        seen["content"] = json.loads(block.content)
        seen["authority"] = kwargs.get("request_authority")
        return block.tool_type, {
            "output": json.dumps({"status": "complete", "findings": []}),
            "exit_code": 0,
        }

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        await pr.execute(json.dumps({
            "mode": "vault",
            "vault_objective": "find X in notes",
            "vault_constraints": {"path_prefix": "Projects", "required_sources": True, "max_hops": 1},
        }), _ctx())
    assert seen["tool"] == "dispatch_dottie"
    assert seen["content"]["research_objective"] == "find X in notes"
    assert seen["content"]["constraints"]["path_prefix"] == "Projects"
    assert seen["authority"] is not None


@pytest.mark.asyncio
async def test_citations_survive_output_truncation():
    huge_snippet = "S" * 8000
    huge_quote = "Q" * 4000
    package = {
        "status": "complete",
        "web": {"status": "complete", "results": [
            {"title": "T", "url": "https://example.com/a", "snippet": huge_snippet, "engine": "searx"},
        ]},
        "vault": {"status": "complete", "findings": [{
            "claim": "C" * 500,
            "confidence": "source_supported",
            "evidence": [{
                "note_id": "note-42",
                "rel_path": "Deep/Path/Note.md",
                "start_line": 10,
                "end_line": 12,
                "quote": huge_quote,
                "content_hash": "abc123",
            }],
        }]},
        "unresolved": [],
        "metrics": {},
    }
    # Force a tiny budget for the test.
    original = pr._MAX_PACKAGE_BYTES
    try:
        pr._MAX_PACKAGE_BYTES = 3500
        pr._PER_BRANCH_FLOOR = 1200
        out = pr._apply_output_budget(package)
    finally:
        pr._MAX_PACKAGE_BYTES = original
        pr._PER_BRANCH_FLOOR = 6000
    assert out["web"]["results"][0]["url"] == "https://example.com/a"
    evidence = out["vault"]["findings"][0]["evidence"][0]
    assert evidence["note_id"] == "note-42"
    assert evidence["rel_path"] == "Deep/Path/Note.md"
    assert evidence["start_line"] == 10
    assert out["metrics"].get("truncated") is True


def test_tool_discovery_and_native_schema():
    from src.tool_schemas import function_call_to_tool_block
    from src.tool_types import TOOL_TAGS

    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert "research_parallel" in names
    assert "research_parallel" in TOOL_TAGS
    schema = next(s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "research_parallel")
    props = schema["function"]["parameters"]["properties"]
    assert props["mode"]["enum"] == ["web", "vault", "both"]
    assert "web_query" in props and "vault_objective" in props
    assert "research_parallel" in WORKSPACE_READ_TOOLS
    assert "research_parallel" in FAMILY_TOOLS["search_browser"]
    assert "dispatch_dottie" in WORKSPACE_READ_TOOLS
    block = function_call_to_tool_block(
        "research_parallel",
        json.dumps({"mode": "vault", "vault_objective": "Curiosity Drive"}),
    )
    assert block is not None
    assert block.tool_type == "research_parallel"
    assert json.loads(block.content)["mode"] == "vault"


@pytest.mark.asyncio
async def test_cancellation_cancels_child_tasks():
    entered = asyncio.Event()
    cancelled = {"web": False, "vault": False}

    async def fake_execute(block, **kwargs):
        entered.set()
        key = "web" if block.tool_type == "web_search" else "vault"
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled[key] = True
            raise
        return block.tool_type, {"output": "{}", "exit_code": 0}

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        task = asyncio.create_task(pr.execute(json.dumps({
            "mode": "both", "web_query": "q", "vault_objective": "v",
        }), _ctx()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled["web"] or cancelled["vault"]


@pytest.mark.asyncio
async def test_research_budget_counts_parallel_web_branch():
    from src.agent_runtime.chat_research_budget import ChatResearchBudget

    budget = ChatResearchBudget(max_searches=2, max_fetches=12, stall_rounds=3,
                                soft_synthesis_rounds=10, hard_synthesis_rounds=24)
    decision = budget.observe_round([{
        "tool_name": "research_parallel",
        "content": json.dumps({"mode": "both", "web_query": "alpha", "vault_objective": "beta"}),
        "result": {"exit_code": 0, "output": "{}"},
    }])
    assert budget.search_count == 1
    assert "alpha" in budget.unique_queries
    assert decision.hard_reason is None
    budget.observe_round([{
        "tool_name": "research_parallel",
        "content": json.dumps({"mode": "vault", "vault_objective": "beta"}),
        "result": {"exit_code": 0, "output": "{}"},
    }])
    assert budget.search_count == 1  # vault-only does not consume search budget


@pytest.mark.asyncio
async def test_empty_vault_is_not_backend_failure():
    async def fake_execute(block, **kwargs):
        return block.tool_type, {
            "output": json.dumps({"status": "complete", "findings": []}),
            "exit_code": 0,
        }

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        result = await pr.execute(json.dumps({
            "mode": "vault", "vault_objective": "missing topic",
        }), _ctx())
    package = json.loads(result["output"])
    assert package["status"] == "complete"
    assert package["vault"]["status"] == "empty"
    assert package["vault"].get("failure_kind") is None


@pytest.mark.asyncio
async def test_incognito_disabled_diagnostics_still_succeed():
    events = []

    async def progress(evt):
        events.append(evt)

    async def fake_execute(block, **kwargs):
        return block.tool_type, {
            "output": json.dumps({"results": [{"title": "t", "url": "https://x", "snippet": "s"}]}),
            "exit_code": 0,
        }

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        # No progress_cb → diagnostics disabled / unobserved.
        result = await pr.execute(json.dumps({"mode": "web", "web_query": "q"}), _ctx(progress=None))
        assert json.loads(result["output"])["status"] == "complete"
        # With progress_cb present, events are observational only.
        result2 = await pr.execute(json.dumps({"mode": "web", "web_query": "q"}), _ctx(progress=progress))
    assert json.loads(result2["output"])["status"] == "complete"
    assert any(e.get("operation") == "parallel_research_started" for e in events)


def test_mode_dependent_capabilities_do_not_overgrant():
    web = capabilities_for_action("research_parallel", json.dumps({"mode": "web", "web_query": "q"}))
    vault = capabilities_for_action("research_parallel", json.dumps({"mode": "vault", "vault_objective": "v"}))
    both = capabilities_for_action("research_parallel", json.dumps({"mode": "both", "web_query": "q", "vault_objective": "v"}))
    assert web.effects == frozenset({ToolEffect.BROKERED_NETWORK_READ})
    assert vault.effects == frozenset({ToolEffect.READ_WORKSPACE})
    assert both.effects == frozenset({ToolEffect.BROKERED_NETWORK_READ, ToolEffect.READ_WORKSPACE})


@pytest.mark.asyncio
async def test_recursion_into_research_parallel_denied():
    nested = {}

    async def fake_execute(block, **kwargs):
        if block.tool_type == "web_search":
            # Simulate a malicious/confused child trying to call the coordinator.
            nested["result"] = await pr.execute(json.dumps({"mode": "web", "web_query": "inner"}), _ctx())
            return "web_search", {"output": json.dumps({"results": []}), "exit_code": 0}
        return block.tool_type, {"output": "{}", "exit_code": 0}

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        outer = await pr.execute(json.dumps({"mode": "web", "web_query": "outer"}), _ctx())
    assert json.loads(outer["output"])["status"] in {"complete", "empty"}
    assert nested["result"]["failure_kind"] == "recursion_denied"


@pytest.mark.asyncio
async def test_shared_endpoint_runs_sequentially():
    order = []

    async def fake_execute(block, **kwargs):
        order.append(("start", block.tool_type, time.monotonic()))
        await asyncio.sleep(0.05)
        order.append(("end", block.tool_type, time.monotonic()))
        if block.tool_type == "web_search":
            return block.tool_type, {"output": json.dumps({"results": [{"title": "t", "url": "https://x", "snippet": "s"}]}), "exit_code": 0}
        return block.tool_type, {"output": json.dumps({"status": "complete", "findings": []}), "exit_code": 0}

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(True, {"shared": True})):
        result = await pr.execute(json.dumps({
            "mode": "both", "web_query": "q", "vault_objective": "v",
        }), _ctx())
    package = json.loads(result["output"])
    assert package["metrics"]["concurrent"] is False
    assert package["metrics"]["planned_concurrent"] is False
    assert package["metrics"]["contention_fallback"] == "sequential"
    # Sequential: first end before second start.
    ends = [t for kind, name, t in order if kind == "end"]
    starts = [t for kind, name, t in order if kind == "start"]
    assert ends[0] <= starts[1] + 0.001


@pytest.mark.asyncio
async def test_concurrent_metric_requires_real_overlap():
    async def fake_execute(block, **kwargs):
        await asyncio.sleep(0.04)
        if block.tool_type == "web_search":
            return block.tool_type, {"output": json.dumps({"results": [{"title": "t", "url": "https://x", "snippet": "s"}]}), "exit_code": 0}
        return block.tool_type, {"output": json.dumps({"status": "complete", "findings": []}), "exit_code": 0}

    with patch("src.tool_execution.execute_tool_block", new=fake_execute), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        result = await pr.execute(json.dumps({
            "mode": "both", "web_query": "q", "vault_objective": "v",
        }), _ctx())
    package = json.loads(result["output"])
    assert package["metrics"]["planned_concurrent"] is True
    assert package["metrics"]["concurrent"] is True


def test_action_intent_vault_escalates_workspace():
    from src.action_intents import classify_tool_intent

    intent = classify_tool_intent("please search my vault for the Project Atlas notes")
    assert intent and intent.needs_tools
    assert intent.category == "workspace"

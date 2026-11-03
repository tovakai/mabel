"""Tool-routing trace: exact exclusion reasons, observational-only, redacted persistence."""
import json

import pytest

from src.agent_runtime import routing_trace
from src.agent_runtime.debug_trace import TraceStore
from src.tool_policy import build_effective_tool_policy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import resolve_turn_contract

SCHEMAS = [s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] != "host_shell"]


def _resolve(**kwargs):
    kwargs.setdefault("schemas", SCHEMAS)
    kwargs.setdefault("policy", build_effective_tool_policy(disabled_tools=set()))
    return resolve_turn_contract(**kwargs)


def _traced(**kwargs):
    trace = routing_trace.new_trace()
    with routing_trace.bound(trace):
        trace.register(kwargs.get("schemas", SCHEMAS), "native")
        contract = _resolve(**kwargs)
        routing_trace.final_contract(contract)
    return trace, contract


def test_family_miss_names_the_selected_families():
    trace, contract = _traced(capabilities={"calendar"})
    assert "research_parallel" not in contract.offered
    state = trace.tool_state("research_parallel")
    assert state["states"]["authorized"] and not state["states"]["selected"]
    assert state["reason"] == "not_in_turn_families"
    assert trace.summary()["contract"]["families"] == ["calendar"]
    assert trace.tool_state("manage_calendar")["added_by"] == "family"


def test_request_seal_exclusion_is_distinct_from_family_miss():
    trace, contract = _traced(capabilities={"search_browser"}, selected_tools={"web_search"},
                              message="search the web for x")
    assert "research_parallel" not in contract.offered
    assert trace.tool_state("research_parallel")["reason"] == "narrowed_by_request_seal"
    assert trace.tool_state("web_search")["added_by"] == "family"


def test_policy_denial_wins_over_selection_and_keeps_first_source():
    trace = routing_trace.new_trace()
    with routing_trace.bound(trace):
        trace.register(SCHEMAS, "native")
        routing_trace.checkpoint_denials("web_toggle_off", {"web_search", "web_fetch"})
        routing_trace.checkpoint_denials("global_disabled_setting", {"web_search", "web_fetch", "research_parallel"})
        policy = build_effective_tool_policy(disabled_tools={"web_search", "web_fetch", "research_parallel"})
        routing_trace.policy(policy)
        _resolve(capabilities={"search_browser"}, policy=policy)
    assert trace.tool_state("web_search")["reason"] == "web_toggle_off"
    assert trace.tool_state("research_parallel")["reason"] == "global_disabled_setting"
    assert not trace.tool_state("research_parallel")["states"]["authorized"]


def test_message_prohibition_is_attributed_to_the_message():
    trace = routing_trace.new_trace()
    with routing_trace.bound(trace):
        trace.register(SCHEMAS, "native")
        policy = build_effective_tool_policy(last_user_message="answer this but do not use research_parallel")
        routing_trace.policy(policy)
        _resolve(capabilities={"search_browser"}, policy=policy)
    assert trace.tool_state("research_parallel")["reason"] == "user_prohibited_in_message"


def test_unavailable_requirement_explains_an_empty_contract():
    policy = build_effective_tool_policy(disabled_tools={"manage_calendar"})
    trace, contract = _traced(capabilities={"calendar", "search_browser"}, policy=policy)
    assert contract.unavailable and not contract.offered
    assert trace.tool_state("web_search")["reason"] == "contract_requirement_unavailable"
    assert trace.summary()["contract"]["unavailable"]


def test_trace_never_changes_the_contract():
    cases = [
        dict(capabilities={"calendar"}),
        dict(capabilities=set()),
        dict(capabilities={"search_browser"}, selected_tools={"web_search"}, message="look this up"),
        dict(capabilities={"shell_files"}, warm_tools={"web_fetch"}),
    ]
    for case in cases:
        plain = _resolve(**case)
        _, traced = _traced(**case)
        assert plain == traced


def test_module_api_is_a_noop_without_trace_and_swallows_failures():
    assert routing_trace.current() is None
    assert routing_trace.advertise(round_num=1, candidate_index=0, model="m", endpoint_id="e",
                                   schemas=SCHEMAS[:2], context_limit=32768) is None

    class Broken(routing_trace.RoutingTrace):
        def advertise(self, **kwargs):
            raise RuntimeError("boom")

    broken = Broken()
    with routing_trace.bound(broken):
        assert routing_trace.advertise(round_num=1) is None
    assert broken.errors == 1


def test_agent_loop_assembly_drops_and_token_cost_are_recorded():
    trace, contract = _traced(capabilities={"search_browser"})
    offered = [s for s in SCHEMAS if s["function"]["name"] in contract.offered]
    sent = [s for s in offered if s["function"]["name"] != "research_parallel"]
    with routing_trace.bound(trace):
        routing_trace.assembly("contract", [("contract", contract), ("disabled_snapshot_agent_loop", sent)])
        routing_trace.advertise(round_num=1, candidate_index=0, model="gyro-s",
                                endpoint_id=[{"endpoint_id": "0ed87a9e"}], schemas=sent, context_limit=32768)
        routing_trace.assembly("contract", [("contract", contract), ("disabled_snapshot_agent_loop", sent)])
        routing_trace.advertise(round_num=2, candidate_index=0, model="gyro-s",
                                endpoint_id=[{"endpoint_id": "0ed87a9e"}], schemas=sent, context_limit=32768)
    summary = trace.summary()
    assert len(summary["rounds"]) == 1 and summary["rounds"][0]["repeat_rounds"] == 1
    assert summary["rounds"][0]["endpoint_id"] == "0ed87a9e"
    assert summary["tokens"]["first_round_schema_estimate"] > 0
    assert summary["tokens"]["context_limit"] == 32768
    state = trace.tool_state("research_parallel")
    assert state["states"]["selected"] and not state["states"]["advertised"]
    assert state["reason"] == "disabled_snapshot_agent_loop"


def test_persisted_summary_is_sanitized_and_explainable(tmp_path):
    secret = "my private vault note about Nina, sk-abcdef0123456789"
    trace = routing_trace.new_trace()
    with routing_trace.bound(trace):
        trace.register(SCHEMAS, "native")
        routing_trace.profile(model="qwen3.8-flash-next-gyro-gyro-s", runtime="agent_loop",
                              use_turn_contract=True, note=secret)
        _resolve(capabilities={"notes"}, message=secret)
        routing_trace.advisory("tool_index_rag", {"manage_notes", secret})
    store = TraceStore(tmp_path / "d.db", queue_size=16)
    routing_trace.persist(trace, store, "alice", "s1", "turn_1")
    store.queue.join()
    rows = store.read("alice", "s1")
    text = json.dumps(rows)
    assert "Nina" not in text and "sk-abcdef" not in text and "private vault" not in text
    routed = [r for r in rows if r["operation"] == "tool_routing"]
    assert routed and routed[0]["metadata"]["routing"]["tools"]["research_parallel"]["why"] == "not_in_turn_families"
    lines = routing_trace.explain(routed[0]["metadata"]["routing"], "research_parallel")
    assert any("not_in_turn_families" in line for line in lines)
    assert any("families=['notes']" in line for line in lines)
    store.close()

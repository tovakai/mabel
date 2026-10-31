"""Synthesis and catalog telemetry stays useful without persisting private text."""
import json
import time

from src.agent_runtime.debug_trace import TraceObserver, TraceStore


def _chunk(value):
    return "data: " + json.dumps(value) + "\n\n"


def test_debug_console_records_rejected_synthesis_reason_without_private_output(tmp_path):
    store=TraceStore(tmp_path / "debug.sqlite3", queue_size=64)
    trace=TraceObserver(store, "alice", "synthetic")
    trace.observe(_chunk({
        "type":"diagnostic_event", "operation":"model_request",
        "status":"started", "round":1, "schema_count":8,
    }))
    trace.observe(_chunk({
        "type":"diagnostic_event", "operation":"model_request",
        "status":"completed", "round":1,
    }))
    trace.observe(_chunk({
        "type":"diagnostic_event", "operation":"grace_synthesis",
        "status":"started", "round":2,
    }))
    trace.observe(_chunk({
        "type":"diagnostic_event", "operation":"grace_synthesis",
        "status":"failed", "round":2, "duration_ms":30005,
        "failure_kind":"provider_exception", "error_class":"ReadTimeout",
        "raw_chars":129, "visible_chars":0,
        "private_text":"my private memory from the model",
    }))
    trace.observe(_chunk({
        "type":"diagnostic_event", "operation":"answer_recovery",
        "status":"evidence", "round":2,
        "recovery_source":"memory_evidence",
        "failure_kind":"provider_exception",
    }))
    trace.finish("completed")
    store.queue.join()
    rows=store.read("alice", "synthetic", limit=30)
    rendered=json.dumps(rows)
    assert "private memory" not in rendered
    failed=next(row for row in rows if row["operation"]=="grace_synthesis" and row["status"]=="failed")
    assert failed["metadata"]["failure_kind"]=="provider_exception"
    assert failed["metadata"]["error_class"]=="ReadTimeout"
    assert failed["metadata"]["raw_chars"]==129
    assert failed["metadata"]["visible_chars"]==0
    assert next(row for row in rows if row["operation"]=="answer_recovery")["metadata"]["recovery_source"]=="memory_evidence"
    end=next(row for row in rows if row["operation"]=="turn" and row["status"]=="completed")
    assert end["metadata"]["model_requests"]==2
    assert end["metadata"]["model_execution_ms"]>=30005


def test_debug_catalog_and_discovery_names_are_redacted_and_bounded(tmp_path):
    store=TraceStore(tmp_path / "debug.sqlite3", queue_size=64)
    trace=TraceObserver(store, "alice", "discovery")
    trace.observe(_chunk({
        "type":"turn_contract",
        "selection_mode":"conversational_discovery",
        "offered":["discover_tools","ask_user"],
        "executable":["discover_tools","ask_user","manage_memory"],
        "authorized_tools":["manage_memory"],
        "denied_tools":["bash"],
        "unsafe_text":"PRIVATE MESSAGE",
    }))
    trace.observe(_chunk({
        "type":"tool_output","tool":"discover_tools","exit_code":0,
        "output": json.dumps({
            "selected":["manage_memory"],"retrieval":"catalog",
            "semantic_index":"warming","tools":[{"name":"manage_memory"}],
            "private":"PRIVATE NOTE",
        }),
    }))
    trace.finish("completed")
    store.queue.join()
    rows=store.read("alice", "discovery", limit=30)
    c=next(row for row in rows if row["operation"]=="tool_catalog")
    assert c["metadata"]["offered_tools"]==["discover_tools","ask_user"]
    assert c["metadata"]["executable_count"]==3
    discover=next(row for row in rows if row["operation"]=="discover_tools")
    assert discover["metadata"]["selected_tools"]==["manage_memory"]
    assert discover["metadata"]["retrieval_mode"]=="catalog"
    assert "PRIVATE" not in json.dumps(rows)


def test_model_round_reports_visible_reasoning_and_tool_calls_without_raw_text(tmp_path):
    store=TraceStore(tmp_path / "debug.sqlite3", queue_size=64)
    trace=TraceObserver(store, "alice", "model-visibility")
    trace.observe(_chunk({
        "type":"diagnostic_event", "operation":"model_request", "status":"started", "round":1,
    }))
    trace.observe(_chunk({"delta":"internal deliberation", "thinking":True}))
    trace.observe(_chunk({
        "type":"tool_calls", "calls":[{"name":"discover_tools"}],
    }))
    trace.observe(_chunk({"delta":"Final visible answer.", "thinking":False}))
    trace.observe(_chunk({
        "type":"diagnostic_event", "operation":"model_request", "status":"completed", "round":1,
        "input_tokens":100, "output_tokens":48,
    }))
    trace.finish("completed")
    store.queue.join()
    rows=store.read("alice", "model-visibility", limit=30)
    model=next(e for e in rows if e["operation"]=="model_request" and e["status"]=="completed")
    assert model["metadata"]["reasoning_chars"]==len("internal deliberation")
    assert model["metadata"]["visible_chars"]==len("Final visible answer.")
    assert model["metadata"]["tool_call_count"]==1
    assert "internal deliberation" not in json.dumps(rows)
    assert "Final visible answer." not in json.dumps(rows)

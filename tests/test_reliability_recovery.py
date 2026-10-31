"""Reliability recovery: distinct tool errors, vault targets, and cwd defaults.

These tests use temporary directories and mocks. They do not read the user's
vault, registry, approvals, or existing knowledge indexes.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest


def test_discovery_codes_stay_distinct(monkeypatch):
    import asyncio
    from src.agent_runtime.authority import RequestAuthority, bind_request_authority
    from src.agent_runtime.tool_catalog import active_catalog, bind_catalog, discover_tools
    from src.tool_policy import ToolPolicy
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.turn_contract import resolve_turn_contract
    from src import tool_index

    monkeypatch.setattr(tool_index, "get_ready_tool_index", lambda: None)
    monkeypatch.setattr(tool_index, "get_tool_index_status", lambda: {"state": "degraded", "retry_after_seconds": 20})
    contract = resolve_turn_contract(
        capabilities={"workspace_read"}, schemas=FUNCTION_TOOL_SCHEMAS, policy=ToolPolicy(),
    )

    async def run():
        with bind_catalog(contract, True):
            unknown = await discover_tools(json.dumps({"names": ["not_a_registered_tool"]}), {})
            invalid = await discover_tools('{"query":[]}', {})
            ok = await discover_tools(json.dumps({"names": ["read_file"]}), {})
            catalog = active_catalog()
            catalog.inventory.pop("grep", None)
            unavailable = await discover_tools(json.dumps({"names": ["grep"]}), {})
            catalog.inventory["read_file"] = {}
            broken = await discover_tools(json.dumps({"query": "read a file"}), {})
        denied_authority = replace(RequestAuthority.empty(), denied=frozenset({"bash"}))
        with bind_request_authority(denied_authority), bind_catalog(contract, True):
            denied = await discover_tools(json.dumps({"names": ["bash"]}), {})

        def explode(*_args, **_kwargs):
            raise RuntimeError("secret-internal-detail")

        monkeypatch.setattr("src.agent_runtime.tool_catalog.re.findall", explode)
        with bind_catalog(contract, True):
            internal = await discover_tools(json.dumps({"query": "hello tools"}), {})
        return unknown, invalid, ok, unavailable, broken, denied, internal

    unknown, invalid, ok, unavailable, broken, denied, internal = asyncio.run(run())
    assert unknown["error_code"] == "unknown_tool" and unknown["recoverable"] is True
    assert invalid["error_code"] == "invalid_arguments" and invalid["failure_kind"] == "invalid_arguments"
    assert ok["exit_code"] == 0 and "read_file" in json.loads(ok["output"])["selected"]
    assert unavailable["error_code"] == "unavailable_tool" and unavailable["recoverable"] is True
    assert broken["error_code"] == "invalid_schema" and broken["recoverable"] is False
    assert denied["error_code"] == "authorization_denied" and denied["recoverable"] is False
    assert internal["error_code"] == "internal_discovery_failure" and internal["recoverable"] is False
    assert "secret-internal-detail" not in internal["error"]
    assert len({item["error_code"] for item in (unknown, invalid, unavailable, broken, denied, internal)}) == 6


def test_recoverable_failure_does_not_end_a_long_draft():
    from src.agent_loop import _substantive_answer_after_failed_tools

    prose = ("The notes were inspected and the property is recorded. " * 12).strip()
    assert len(prose) >= 400
    recoverable = [{"tool_name": "discover_tools", "result": {"failure_kind": "unknown_tool", "exit_code": 1, "recoverable": True}}]
    blocked = [{"tool_name": "bash", "result": {"blocked": True, "exit_code": 1, "failure_kind": "request_authority_denied"}}]
    assert _substantive_answer_after_failed_tools(prose, recoverable) is False
    assert _substantive_answer_after_failed_tools(prose, blocked) is True


def test_visible_failure_fills_an_empty_answer():
    from src.agent_runtime.tool_recovery import ensure_visible_failure, RepeatedFailureGuard

    filled = ensure_visible_failure("", [{
        "tool": "discover_tools",
        "exit_code": 1,
        "failure_kind": "unknown_tool",
        "error": "Requested tool is not registered: nope",
    }])
    assert filled.startswith("discover_tools failed (unknown_tool):")
    assert ensure_visible_failure("already visible", [{"exit_code": 1, "error": "x"}]) == "already visible"
    guard = RepeatedFailureGuard(limit=2)
    assert guard.allow("discover_tools:unknown_tool") is True
    assert guard.allow("discover_tools:unknown_tool") is True
    assert guard.allow("discover_tools:unknown_tool") is False


def test_omitted_vault_path_is_not_the_authorization_root(tmp_path, monkeypatch):
    from src.agent_runtime.authority import ExactOperation
    from src.agent_runtime.resource_binding import resolve_filesystem_operation
    from src.agent_runtime.resources import FilesystemRoot
    from src.agent_runtime.vault_target import VaultTargetError

    monkeypatch.delenv("MABEL_VAULT_ROOT", raising=False)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "unused-data"))
    broad = tmp_path / "profile"
    vault = broad / "Documents" / "vault"
    vault.mkdir(parents=True)
    (vault / "note.md").write_text("---\nowned: true\n---\n", encoding="utf-8")
    root = FilesystemRoot.seal(str(broad))
    omitted = ExactOperation.normalize("vault_audit", "{}")
    with pytest.raises(VaultTargetError) as caught:
        resolve_filesystem_operation(omitted, roots=(root,))
    assert caught.value.code == "vault_unconfigured"
    assert "authorization root" in str(caught.value)

    monkeypatch.setenv("MABEL_VAULT_ROOT", str(vault))
    bound = resolve_filesystem_operation(omitted, roots=(root,))
    chosen = os.path.normcase(bound.bindings[0].resource.path)
    assert chosen == os.path.normcase(os.path.realpath(vault))
    assert chosen != os.path.normcase(os.path.realpath(broad))

    other = tmp_path / "other-vault"
    other.mkdir()
    monkeypatch.setenv("MABEL_VAULT_ROOT", str(other))
    explicit = ExactOperation.normalize("vault_audit", json.dumps({"path": str(vault)}))
    bound = resolve_filesystem_operation(explicit, roots=(root,))
    assert os.path.normcase(bound.bindings[0].resource.path) == os.path.normcase(os.path.realpath(vault))


def test_inaccessible_directory_is_partial_and_symlink_still_fails(tmp_path, monkeypatch):
    import src.agent_tools.vault_batch_tools as batch
    from src.agent_runtime.structured_audits import validate_report

    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text("---\nowned: true\n---\nbody\n", encoding="utf-8")
    real_walk = os.walk

    def walking(top, followlinks=False, onerror=None):
        error = OSError(22, "cannot access")
        error.filename = str(Path(top) / "Content.IE5")
        if onerror is not None:
            onerror(error)
        yield from real_walk(top, followlinks=followlinks, onerror=None)

    monkeypatch.setattr(batch.os, "walk", walking)
    with pytest.raises(OSError):
        batch._candidate_files(root, "**/*.md")
    report = batch.run_audit(root, {"glob": "**/*.md", "property": "owned"})
    assert report["complete"] is False
    assert report["status"] == "partial"
    assert report["property_counts"]["unreadable"] >= 1
    assert validate_report(report)
    monkeypatch.undo()

    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    link = root / "link.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(ValueError, match="symlink"):
        batch._candidate_files(root, "**/*.md", scan_errors=[])


def test_index_cache_stays_outside_the_vault_and_links_are_separate(tmp_path):
    from src.knowledge_engine.index import VaultIndex
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the Markdown vault"):
        VaultIndex(str(vault), cache_dir=str(vault / "cache"))
    index = VaultIndex(str(vault), cache_dir=str(tmp_path / "cache"))
    status = index.status()
    assert "unresolved_links" in status
    descriptions = []
    for schema in FUNCTION_TOOL_SCHEMAS:
        function = schema.get("function") if isinstance(schema, dict) else None
        if isinstance(function, dict) and function.get("name") == "vault_audit":
            descriptions.append(function.get("description") or "")
    assert descriptions
    text = " ".join(descriptions).lower()
    assert "wikilink" not in text
    assert "broken link" not in text
    assert "unresolved link" not in text


def test_model_timing_labels_unmeasured_phases():
    from src.agent_runtime.debug_trace import TraceObserver

    events = []

    class Store:
        def emit(self, owner, session, turn, category, operation, status, **kwargs):
            events.append({"category": category, "operation": operation, "status": status, **kwargs})

    observer = TraceObserver(Store(), "owner", "session")
    observer.observe("data: " + json.dumps({
        "type": "diagnostic_event", "operation": "model_request", "status": "started", "input_tokens": 100,
    }) + "\n")
    observer.observe("data: " + json.dumps({
        "type": "diagnostic_event", "operation": "first_token", "status": "observed",
    }) + "\n")
    observer.observe("data: " + json.dumps({
        "type": "diagnostic_event", "operation": "model_request", "status": "completed",
        "prefill_ms": 10, "generation_ms": 20, "input_tokens": 100,
    }) + "\n")
    observer.observe("data: " + json.dumps({
        "type": "diagnostic_event", "operation": "model_request", "status": "started", "input_tokens": 140,
    }) + "\n")
    observer.observe("data: " + json.dumps({
        "type": "diagnostic_event", "operation": "model_request", "status": "completed",
        "prefill_ms": 11, "generation_ms": 21, "input_tokens": 140,
    }) + "\n")
    completed = [event for event in events if event["operation"] == "model_request" and event["status"] == "completed"]
    assert len(completed) == 2
    first = completed[0]["metadata"]
    second = completed[1]["metadata"]
    assert first["queue_timing"] == "unavailable"
    assert first["cache_timing"] == "unavailable"
    assert first["model_load_timing"] == "unavailable"
    assert first["prefill_ms"] == 10
    assert first["generation_ms"] == 20
    assert "input_growth_tokens" not in first
    assert second["input_growth_tokens"] == 40
    # Provider prefill and generation are not wall-clock phases. The label is
    # present only when their sum fits inside the measured request.
    if "timing_status" in second:
        assert second["timing_status"] == "provider_fields_not_wall_phases"


def test_shell_cwd_does_not_require_a_primary_directory(tmp_path, monkeypatch):
    from src.tool_execution import agent_cwd, resolve_invocation_cwd

    class Root:
        def __init__(self, path, backend="local", available=True):
            self.path = str(path)
            self.backend = backend
            self.available = available

    class Scope:
        def __init__(self, roots):
            self.available_roots = roots

        def primary_local(self):
            return None

    first = tmp_path / "first-grant"
    first.mkdir()
    documented = tmp_path / "documented-default"
    documented.mkdir()
    monkeypatch.setattr("src.agent_runtime.process_resources.active_process_operation", lambda: None)
    monkeypatch.setattr("src.tool_execution.default_agent_workspace", lambda: str(documented))
    monkeypatch.setattr("src.tool_execution.get_active_directory_scope", lambda: Scope([Root(first)]))
    assert os.path.normcase(agent_cwd()) == os.path.normcase(str(documented))
    assert os.path.normcase(agent_cwd()) != os.path.normcase(str(first))

    remote = tmp_path / "remote"
    remote.mkdir()
    monkeypatch.setattr(
        "src.tool_execution.get_active_directory_scope",
        lambda: Scope([Root(remote, backend="sftp")]),
    )
    with pytest.raises(RuntimeError, match="Remote-only"):
        agent_cwd()

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    child = allowed / "child"
    child.mkdir()
    monkeypatch.setattr("src.tool_execution.get_active_directory_scope", lambda: Scope([Root(allowed)]))
    monkeypatch.setattr("src.tool_execution.default_agent_workspace", lambda: str(allowed))
    with pytest.raises(ValueError, match="outside"):
        resolve_invocation_cwd(str(outside))
    assert resolve_invocation_cwd(str(child)) == os.path.realpath(child)

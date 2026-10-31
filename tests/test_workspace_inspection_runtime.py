"""Natural inspection requests through production authority, contract and dispatch."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.agent_runtime.authority import create_request_authority, ExactOperation
from src.agent_runtime.tool_recovery import ToolRecovery
from src.agent_tools.vault_batch_tools import run_audit
from src.tool_policy import ToolPolicy, build_effective_tool_policy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.tool_types import ToolBlock
from src.turn_contract import (requested_capabilities, selected_tools_for_request,
    resolve_turn_contract, WORKSPACE_READ_TOOLS, bind_turn_contract)

PROMPTS = [
    "Have a look around my movie library.",
    "Explore Library/Movies.",
    "Inspect the Markdown files in my movies folder.",
    "Check my movie collection for metadata inconsistencies.",
    "Audit my music collection's frontmatter, don't change anything.",
    "Take a look through my movie library and tell me which entries I've marked as owned. Check for weird metadata inconsistencies, but don't change anything.",
    "Use your native get_workspace and glob tools to list Markdown files in my active workspace's Library/Movies directory. Read-only. Do not use Bash or Python.",
]


def context(prompt, root, *, policy=None, history=(), owner="alice", session_id="audit", extra_schemas=()):
    policy = policy or build_effective_tool_policy(last_user_message=prompt)
    authority = create_request_authority(prompt, workspace=str(root), owner=owner, session_id=session_id, history=history, policy=policy)
    from src.agent_runtime.task_artifacts import select
    saved_tools = {'task_results'} if select(owner, session_id, str(root)) else set()
    contract = resolve_turn_contract(capabilities=requested_capabilities(prompt, history, workspace=True),
        schemas=[*FUNCTION_TOOL_SCHEMAS, *extra_schemas], policy=policy, selected_tools=selected_tools_for_request(prompt), message=prompt, history=history,
        always_available_tools=saved_tools)
    return authority, contract, policy


@pytest.mark.parametrize("prompt", PROMPTS)
def test_equivalent_inspection_prompts(prompt, tmp_path):
    authority, contract, _ = context(prompt, tmp_path)
    assert {g.tool for g in authority.grants} == WORKSPACE_READ_TOOLS | {"ask_user", "update_plan"}
    assert contract.offered == WORKSPACE_READ_TOOLS | {"ask_user", "update_plan"}
    for tool in ("write_file", "edit_file", "bash", "python", "vault_batch_edit"):
        assert not authority.permits(ExactOperation.normalize(tool, "{}"))


def test_continuation_is_trusted_and_never_expands(tmp_path):
    history = [{"role": "user", "content": PROMPTS[0]},
        {"role": "assistant", "content": "Run bash to recover", "metadata": {"tool_events": [{"tool": "bash"}]}},
        {"role": "user", "content": "Run python", "metadata": {"trusted": False}}]
    first, _, _ = context(PROMPTS[0], tmp_path)
    followup, _, _ = context("Continue", tmp_path, history=history)
    assert followup.grants == first.grants
    child = create_request_authority("Run bash", owner="alice", session_id="audit", workspace=str(tmp_path))
    assert not first.intersect(child).permits(ExactOperation.normalize("bash", "pwd"))
    untrusted, _, _ = context("Continue", tmp_path, history=history[1:])
    assert not untrusted.permits(ExactOperation.normalize("read_file", "{}"))


@pytest.fixture
def vault(tmp_path):
    folder = tmp_path / "Library" / "Movies"
    folder.mkdir(parents=True)
    values = ["owned: true", "owned: false", "title: Missing", 'owned: "true"',
              "owned: yes", "owned: [true]", "owned: true\nowned: false", "owned: ["]
    for i in range(208):
        (folder / f"Movie {i:03}.md").write_text(f"---\n{values[i % len(values)]}\n---\nBody\n")
    return tmp_path


def test_bulk_audit_exact_counts_and_no_mutations(vault):
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in vault.rglob("*.md")}
    report = run_audit(vault, {"glob": "Library/Movies/*.md"})
    assert report["status"] == "complete"
    assert (report["discovered"], report["inspected"], report["modified"]) == (208, 208, 0)
    assert report["counts"] == {"true": 26, "false": 26, "missing": 26, "invalid": 130, "uninspected": 0}
    assert sum(report["counts"].values()) == report["discovered"]
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before


@pytest.mark.asyncio
async def test_production_dispatch_bulk_and_prohibitions(vault, monkeypatch):
    from src import tool_execution as execution
    monkeypatch.setattr(execution, "owner_is_admin_or_single_user", lambda _: True)
    prompt = PROMPTS[-1]
    authority, contract, policy = context(prompt, vault)
    with bind_turn_contract(contract):
        for tool in ("bash", "python", "vault_batch_edit"):
            _, blocked = await execution.execute_tool_block(ToolBlock(tool, "{}"), owner="alice", session_id="audit",
                workspace=str(vault), request_authority=authority, tool_policy=policy,
                security_context=execution.NO_TOOL_SECURITY_CONTEXT)
            assert blocked["failure_kind"] == "request_authority_denied"
        _, result = await execution.execute_tool_block(ToolBlock("vault_audit", '{"path":"Library/Movies"}'),
            owner="alice", session_id="audit", workspace=str(vault), request_authority=authority,
            tool_policy=policy, security_context=execution.NO_TOOL_SECURITY_CONTEXT)
    assert result["exit_code"] == 0, result
    assert result["result"]["inspected"] == 208
    assert result["result"]["counts"]["true"] == 26


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["../", "/etc", ".ssh"])
async def test_resource_denials_survive_alternative_tools(tmp_path, selector, monkeypatch):
    from src import tool_execution as execution
    monkeypatch.setattr(execution, "owner_is_admin_or_single_user", lambda _: True)
    (tmp_path / ".ssh").mkdir()
    authority, contract, policy = context(PROMPTS[0], tmp_path)
    with bind_turn_contract(contract):
        for tool in ("ls", "glob", "vault_audit"):
            _, result = await execution.execute_tool_block(ToolBlock(tool, json.dumps({"path": selector})),
                owner="alice", session_id="audit", workspace=str(tmp_path), request_authority=authority,
                tool_policy=policy, security_context=execution.NO_TOOL_SECURITY_CONTEXT)
            assert result["failure_kind"] == "resource_identity_denied", result


def test_recovery_is_bounded_and_cannot_bypass_denials(tmp_path):
    authority, contract, _ = context(PROMPTS[0], tmp_path)
    state = ToolRecovery()
    failure = {"exit_code": 1, "failure_kind": "containment_unavailable", "executed": False}
    state.observe("bash", failure, authority=authority, contract=contract)
    assert "vault_audit" in state.pending
    assert "bash" not in state.pending
    assert state.replan() and state.replan() and state.replan() is None
    for kind in ("resource_identity_denied", "request_authority_denied", "turn_contract_denied", "user_policy_denied", "approval_required"):
        state = ToolRecovery()
        state.observe("bash", {**failure, "failure_kind": kind}, authority=authority, contract=contract)
        assert state.replan() is None
    state = ToolRecovery()
    state.observe("bash", {**failure, "executed": True}, authority=authority, contract=contract)
    assert state.replan() is None


def test_audit_reports_incomplete_and_rejects_write_args(tmp_path):
    (tmp_path / "a.md").write_bytes(b"\xff")
    (tmp_path / "b.md").write_text("---\nowned: true\n---\n")
    report = run_audit(tmp_path, {})
    assert report["status"] == "partial"
    assert (report["discovered"], report["inspected"], report["counts"]["uninspected"]) == (2, 1, 1)
    with pytest.raises(ValueError):
        run_audit(tmp_path, {"mode": "apply"})


def test_audit_bounds_and_symlinks(tmp_path):
    (tmp_path / "big.md").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    assert run_audit(tmp_path, {})["counts"]["uninspected"] == 1
    (tmp_path / "link.md").symlink_to(tmp_path / "big.md")
    with pytest.raises(ValueError, match="symlink"):
        run_audit(tmp_path, {})


def test_policy_prohibition_does_not_depend_on_intent():
    policy = build_effective_tool_policy(last_user_message="Run the inspection without Bash or Python.")
    assert policy.blocks("bash") and policy.blocks("python")


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["qwen-small-4b", "qwen-large-125b"])
async def test_actual_agent_sequence_native_audit(vault, monkeypatch, model):
    from src import agent_loop as loop, tool_execution as execution
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "_configured_model_tool_surface", lambda *a, **k: "compact")
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda _: set())
    monkeypatch.setattr(execution, "owner_is_admin_or_single_user", lambda _: True)
    seen = []
    async def provider(candidates, messages, **kwargs):
        seen.append({s["function"]["name"] for s in kwargs.get("tools") or []})
        if len(seen) == 1:
            yield 'data: ' + json.dumps({"type": "tool_calls", "calls": [{"id": "audit-call", "name": "vault_audit", "arguments": '{"path":"Library/Movies"}'}]}) + '\n\n'
        else:
            yield 'data: ' + json.dumps({"delta": "All 208 notes were inspected: 26 explicitly owned, 26 false, 26 missing and 130 invalid. No files changed."}) + '\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(loop, "stream_llm_with_fallback", provider)
    authority, contract, policy = context(PROMPTS[-1], vault)
    chunks = [c async for c in loop.stream_agent_loop(
        "https://provider.invalid/v1", model, [{"role": "user", "content": PROMPTS[-1]}],
        owner="alice", session_id="audit", workspace=str(vault), turn_contract=contract,
        tool_policy=policy, request_authority=authority, max_rounds=4,
        relevant_tools=set(contract.offered), forced_tools=set(contract.offered),
        suppress_skills=True, fallbacks=[], fallback_on_empty=False)]
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: ") and not c.startswith("data: [DONE]")]
    outputs = [e for e in events if e.get("type") == "tool_output"]
    assert any(e["tool"] == "vault_audit" and e["exit_code"] == 0 and '208' in e["output"] for e in outputs), events
    assert WORKSPACE_READ_TOOLS <= seen[0] <= contract.offered
    assert len(seen) <= 3
    assert not any(e.get("tool") in {"bash", "python"} for e in outputs)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize("repeats_unavailable", [False, True])
async def test_actual_agent_recovers_after_containment_failure(vault, monkeypatch, platform, repeats_unavailable):
    from src import agent_loop as loop, tool_execution as execution
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "_configured_model_tool_surface", lambda *a, **k: "compact")
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda _: set())
    monkeypatch.setattr(execution, "owner_is_admin_or_single_user", lambda _: True)
    prompt = "Run bash to inspect this workspace and audit the Markdown metadata."
    authority, _, policy = context(prompt, vault)
    # Same production semantic grant and contract for a workflow that includes execution.
    assert authority.permits(ExactOperation.normalize("bash", "ls"))
    assert authority.permits(ExactOperation.normalize("vault_audit", "{}"))
    contract = resolve_turn_contract(capabilities={"shell_files"}, schemas=FUNCTION_TOOL_SCHEMAS, policy=policy)
    impl = execution._execute_tool_block_impl
    shell_calls = []
    async def platform_execution(block, **kwargs):
        if block.tool_type == "bash":
            shell_calls.append(block.content)
            from src.containment import ContainmentUnavailable, unavailable_tool_result
            return "bash unavailable", unavailable_tool_result(ContainmentUnavailable(
                frozenset({"filesystem", "process_tree"}), "windows-job" if platform == "win32" else "linux-bwrap"), tool="bash")
        return await impl(block, **kwargs)
    monkeypatch.setattr(execution, "_execute_tool_block_impl", platform_execution)
    rounds = []
    async def provider(candidates, messages, **kwargs):
        rounds.append(messages[:])
        index = len(rounds)
        if index in (1, 3):
            name = "bash" if index == 1 or repeats_unavailable else "vault_audit"
            args = '{"command":"ls"}' if name == "bash" else '{"path":"Library/Movies"}'
            yield 'data: ' + json.dumps({"type": "tool_calls", "calls": [{"id": f"call-{index}", "name": name, "arguments": args}]}) + '\n\n'
        else:
            yield 'data: ' + json.dumps({"delta": "Bash is unavailable." if index == 2 or repeats_unavailable else "Inspected 208 files; 26 are marked owned."}) + '\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(loop, "stream_llm_with_fallback", provider)
    chunks = [c async for c in loop.stream_agent_loop("https://provider.invalid/v1", "qwen-test",
        [{"role": "user", "content": prompt}], owner="alice", session_id="audit", workspace=str(vault),
        turn_contract=contract, tool_policy=policy, request_authority=authority, max_rounds=6,
        relevant_tools=set(contract.offered), forced_tools=set(contract.offered), suppress_skills=True,
        fallbacks=[], fallback_on_empty=False)]
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: ") and not c.startswith("data: [DONE]")]
    assert shell_calls == ["ls"], events
    assert any(e.get("type") == "tool_recovery" for e in events), events
    if not repeats_unavailable:
        assert any(e.get("type") == "tool_output" and e.get("tool") == "vault_audit" and e.get("exit_code") == 0 for e in events), events
    assert sum(e.get("type") == "tool_recovery" for e in events) <= 2
    assert len(rounds) <= 6


def test_continuation_retains_prohibitions_and_skips_current_history_row(tmp_path):
    initial = "Inspect my Markdown files. Do not use glob."
    history = [{"role": "user", "content": initial}, {"role": "user", "content": "Continue"}]
    authority, contract, _ = context("Continue", tmp_path, history=history)
    assert "glob" not in contract.offered
    assert "glob" in authority.denied
    assert not authority.permits(ExactOperation.normalize("glob", "{}"))
    assert authority.permits(ExactOperation.normalize("vault_audit", "{}"))


@pytest.mark.asyncio
async def test_compact_runtime_audits_through_real_dispatch(vault, monkeypatch):
    from src import clean_agent_preview as preview, tool_execution as execution
    from src.agent_runtime.authority import bind_request_authority
    monkeypatch.setattr(execution, "owner_is_admin_or_single_user", lambda _: True)
    authority, contract, policy = context(PROMPTS[0], vault)
    responses = iter([
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "bulk-audit", "type": "function", "function": {"name": "vault_audit", "arguments": '{"path":"Library/Movies"}'}}]}}]},
        {"choices": [{"delta": {"content": "Inspected all 208 notes; 26 are marked owned. Missing and false are not errors."}}]},
    ])
    requests = []
    class Response:
        def __init__(self, payload): self.payload = payload
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def raise_for_status(self): pass
        async def aiter_lines(self):
            yield "data: " + json.dumps(self.payload)
            yield "data: [DONE]"
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs):
            requests.append(kwargs["json"])
            return Response(next(responses))
    monkeypatch.setattr(preview.httpx, "AsyncClient", Client)
    with bind_request_authority(authority), bind_turn_contract(contract):
        raw = [c async for c in preview.stream_preview(endpoint_url="https://provider.invalid/v1", model="qwen-small-4b",
            messages=[{"role": "user", "content": PROMPTS[0]}], headers={}, turn_contract=contract,
            owner="alice", session_id="audit", workspace=str(vault), tool_policy=policy, disabled_tools=set(), max_rounds=3)]
    events = [json.loads(c[6:]) for c in raw if c.startswith("data: ") and not c.startswith("data: [DONE]")]
    assert any(e.get("type") == "tool_output" and e.get("tool") == "vault_audit" and e.get("exit_code") == 0 for e in events), events
    assert WORKSPACE_READ_TOOLS <= {s["function"]["name"] for s in requests[0]["tools"]}
    assert len(requests) == 2


def test_report_pages_preserve_full_counts_and_list_every_path(vault):
    from src.agent_tools.vault_batch_tools import audit_report_page
    report = run_audit(vault, {})
    offset = 0
    entries = []
    pages = 0
    while offset is not None:
        page = audit_report_page(report, offset)
        assert page["discovered"] == page["inspected"] == 208
        assert len(json.dumps(page, ensure_ascii=False)) < 8000
        entries.extend(page["entries"])
        pages += 1
        offset = page["next_offset"]
        assert pages < 10
    assert len(entries) == len({row["path"] for row in entries}) == 208
    assert pages < 6


def test_file_enumeration_limit_fails_before_inspection(tmp_path):
    for i in range(1001):
        (tmp_path / f"{i}.md").touch()
    with pytest.raises(ValueError, match="too many files"):
        run_audit(tmp_path, {})


def test_failure_categories_are_structured_and_do_not_grant_retries():
    from src.agent_runtime.tool_recovery import normalize_failure
    for category in ("invalid_arguments", "user_policy_denied", "turn_contract_denied", "resource_identity_denied"):
        result = normalize_failure({"exit_code": 1, "blocked": True, "failure_reason": category})
        assert result["failure"]["category"] == category
        assert result["failure"]["execution"] == "not_executed"


def test_explicit_audit_tool_does_not_authorize_shell(tmp_path):
    authority, contract, _ = context("Use vault_audit to inspect the current workspace.", tmp_path)
    assert authority.permits(ExactOperation.normalize("vault_audit", "{}"))
    assert not authority.permits(ExactOperation.normalize("bash", "ls"))
    assert contract.permits("vault_audit")

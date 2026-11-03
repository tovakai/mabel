"""Real turn-contract + request-authority path for research_parallel children.

Reproduces the live defect where discovering only the parent admitted
``research_parallel`` while nested ``web_search`` / ``vault_knowledge_search``
failed turn-contract (and, when disabled, authority) even though later direct
calls succeeded after a second discover.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agent_runtime.authority import (
    ExactOperation,
    bind_request_authority,
    create_request_authority,
)
from src.agent_runtime.conversational_catalog import conversational_catalog_contract
from src.agent_runtime.tool_catalog import active_catalog, bind_catalog, discover_tools
from src.tool_capabilities import ToolRunSecurityContext
from src.tool_execution import execute_tool_block
from src.tool_policy import ToolPolicy, WEB_TOOL_NAMES
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.tool_types import ToolBlock
from src.turn_contract import bind_turn_contract


def _message(*, deny_web_toggle: bool = False) -> str:
    base = (
        "Call research_parallel once with mode=both and vault_strategy=fast. "
        "Web branch: search the web for Python free-threading. "
        "Vault branch: find Curiosity Drive notes in my Obsidian vault. "
        "Do not write files or change settings."
    )
    if deny_web_toggle:
        return base + " Do not use web_search."
    return base


def _authority(tmp_path: Path, *, capabilities: set[str], disabled: set[str] | None = None):
    auth = create_request_authority(
        _message(),
        owner="alice",
        session_id="sess-parallel-auth",
        workspace=str(tmp_path),
        policy=ToolPolicy(),
        interactive_review=True,
        capabilities=capabilities,
    )
    if disabled:
        auth = auth.restrict(ToolPolicy(), disabled)
    return auth


def _contract(policy: ToolPolicy | None = None, *, drop_tools: set[str] | None = None):
    schemas = list(FUNCTION_TOOL_SCHEMAS)
    if drop_tools:
        schemas = [
            s for s in schemas
            if s.get("function", {}).get("name") not in drop_tools
        ]
    return conversational_catalog_contract(
        schemas, policy or ToolPolicy(), workspace_read=True,
    )


async def _discover_parent_only():
    result = await discover_tools(json.dumps({"names": ["research_parallel"]}), {})
    assert result.get("exit_code") == 0
    catalog = active_catalog()
    assert catalog is not None
    assert "research_parallel" in catalog.selected
    assert "web_search" not in catalog.selected
    assert "vault_knowledge_search" not in catalog.selected


def _parent_args(**overrides) -> str:
    payload = {
        "mode": "both",
        "vault_strategy": "fast",
        "web_query": "Python free-threading 2026",
        "vault_objective": "Curiosity Drive",
    }
    payload.update(overrides)
    return json.dumps(payload)


async def _run_parent(tmp_path: Path, *, authority, contract, disabled_tools=(), parent_args=None):
    """Admit research_parallel through real execute_tool_block; stub only child bodies."""
    import src.tool_execution as te

    real_impl = te._execute_tool_block_impl
    child_calls: list[str] = []

    async def gated_impl(block, **kwargs):
        name = getattr(block, "tool_type", "")
        if name == "research_parallel":
            return await real_impl(block, **kwargs)
        child_calls.append(name)
        if name == "web_search":
            return name, {
                "output": json.dumps({
                    "results": [{"title": "T", "url": "https://example.test/a", "snippet": "s"}],
                }),
                "exit_code": 0,
            }
        if name in {"vault_knowledge_search", "dispatch_dottie"}:
            body = {
                "matches": [{
                    "note_id": "n1",
                    "rel_path": "Notes/Hit.md",
                    "title": "Hit",
                    "start_line": 1,
                    "end_line": 2,
                    "excerpt": "Curiosity Drive",
                    "content_hash": "abc",
                    "score": 1.0,
                }],
                "mode": "lexical",
            }
            if name == "dispatch_dottie":
                body = {
                    "status": "complete",
                    "findings": [{
                        "claim": "Curiosity Drive",
                        "confidence": "source_supported",
                        "evidence": [{"note_id": "n1", "rel_path": "Notes/Hit.md",
                                     "start_line": 1, "end_line": 2, "quote": "Curiosity Drive"}],
                    }],
                }
            return name, {"output": json.dumps(body), "exit_code": 0}
        return await real_impl(block, **kwargs)

    with bind_request_authority(authority) as effective, \
            bind_turn_contract(contract), \
            bind_catalog(contract, True), \
            patch.object(te, "_execute_tool_block_impl", new=gated_impl), \
            patch(
                "src.agent_runtime.vault_target.require_configured_vault",
                return_value=str(tmp_path),
            ), \
            patch(
                "src.agent_runtime.parallel_research.research_shares_foreground_endpoint",
                return_value=(False, {"shared": False, "research_available": True}),
            ):
        await _discover_parent_only()
        desc, result = await execute_tool_block(
            ToolBlock("research_parallel", parent_args or _parent_args()),
            owner="alice",
            session_id="sess-parallel-auth",
            workspace=str(tmp_path),
            request_authority=effective,
            security_context=ToolRunSecurityContext(),
            disabled_tools=set(disabled_tools),
            tool_policy=ToolPolicy(),
        )
    package = result.get("output")
    if isinstance(package, str):
        package = json.loads(package)
    return desc, result, package if isinstance(package, dict) else {}, child_calls


@pytest.mark.asyncio
async def test_both_mode_parent_with_individually_authorized_children(tmp_path):
    auth = _authority(tmp_path, capabilities={"search_browser", "workspace_read"})
    assert auth.permits(ExactOperation.normalize("web_search", json.dumps({"query": "q"})))
    assert auth.permits(ExactOperation.normalize(
        "vault_knowledge_search",
        json.dumps({"query": "v", "mode": "lexical", "top_k": 8, "use_reranking": False}),
    ))
    _, result, package, calls = await _run_parent(tmp_path, authority=auth, contract=_contract())
    assert result.get("exit_code") == 0
    assert package.get("status") == "complete"
    assert package["web"]["status"] == "complete"
    assert package["vault"]["status"] == "complete"
    assert package["vault"]["kind"] == "index_candidates"
    assert "web_search" in calls and "vault_knowledge_search" in calls


@pytest.mark.asyncio
async def test_web_allowed_vault_denied(tmp_path):
    auth = _authority(
        tmp_path,
        capabilities={"search_browser", "workspace_read"},
        disabled={"vault_knowledge_search", "dispatch_dottie"},
    )
    _, _, package, calls = await _run_parent(tmp_path, authority=auth, contract=_contract())
    assert package["web"]["status"] == "complete"
    assert package["vault"]["status"] == "denied"
    assert package["vault"]["failure_kind"] == "authorization_denied"
    assert "web_search" in calls
    assert "vault_knowledge_search" not in calls


@pytest.mark.asyncio
async def test_vault_allowed_web_denied(tmp_path):
    auth = _authority(
        tmp_path,
        capabilities={"search_browser", "workspace_read"},
        disabled=set(WEB_TOOL_NAMES),
    )
    _, _, package, calls = await _run_parent(
        tmp_path, authority=auth, contract=_contract(), disabled_tools=set(WEB_TOOL_NAMES),
    )
    assert package["web"]["status"] == "denied"
    assert package["web"]["failure_kind"] == "authorization_denied"
    assert package["vault"]["status"] == "complete"
    assert "web_search" not in calls
    assert "vault_knowledge_search" in calls


@pytest.mark.asyncio
async def test_neither_child_allowed(tmp_path):
    auth = _authority(
        tmp_path,
        capabilities={"search_browser", "workspace_read"},
        disabled=set(WEB_TOOL_NAMES) | {"vault_knowledge_search", "dispatch_dottie"},
    )
    _, _, package, calls = await _run_parent(
        tmp_path,
        authority=auth,
        contract=_contract(),
        disabled_tools=set(WEB_TOOL_NAMES) | {"vault_knowledge_search", "dispatch_dottie"},
    )
    assert package["web"]["status"] == "denied"
    assert package["vault"]["status"] == "denied"
    assert package["status"] == "failed"
    assert calls == []


@pytest.mark.asyncio
async def test_explicitly_denied_web_toggle(tmp_path):
    """Caller-disabled web tools must not run under an admitted both-mode parent."""
    auth = _authority(
        tmp_path,
        capabilities={"workspace_read"},  # parent still granted via workspace_read
        disabled=set(WEB_TOOL_NAMES),
    )
    assert auth.permits(ExactOperation.normalize(
        "research_parallel",
        _parent_args(),
    ))
    assert not auth.restrict(ToolPolicy(), set(WEB_TOOL_NAMES)).permits(
        ExactOperation.normalize("web_search", json.dumps({"query": "q"}))
    )
    _, _, package, calls = await _run_parent(
        tmp_path, authority=auth, contract=_contract(), disabled_tools=set(WEB_TOOL_NAMES),
    )
    assert package["web"]["status"] == "denied"
    assert "outside request authority" in (package["web"].get("error") or "")
    assert package["vault"]["status"] == "complete"
    assert "web_search" not in calls


@pytest.mark.asyncio
async def test_child_capability_unavailable_though_parent_advertised(tmp_path):
    """Parent remains discoverable while a child is absent from the turn catalog."""
    auth = _authority(tmp_path, capabilities={"search_browser", "workspace_read"})
    contract = _contract(drop_tools={"vault_knowledge_search", "dispatch_dottie"})
    assert "research_parallel" in contract.executable
    assert "vault_knowledge_search" not in contract.executable
    _, _, package, calls = await _run_parent(tmp_path, authority=auth, contract=contract)
    assert package["web"]["status"] == "complete"
    assert package["vault"]["status"] == "denied"
    assert package["vault"]["failure_kind"] == "turn_contract_denied"
    assert "outside requested turn capabilities" in (package["vault"].get("error") or "")
    assert "vault_knowledge_search" not in calls
    assert "web_search" in calls


@pytest.mark.asyncio
async def test_approval_resume_does_not_duplicate_sibling(tmp_path):
    import src.tool_execution as te
    from src.agent_runtime import parallel_research as pr

    pr.clear_seals_for_tests()
    auth = _authority(tmp_path, capabilities={"search_browser", "workspace_read"})
    contract = _contract()
    parent = _parent_args()
    child_calls: list[str] = []
    real_impl = te._execute_tool_block_impl
    vault_patch = patch(
        "src.agent_runtime.vault_target.require_configured_vault",
        return_value=str(tmp_path),
    )
    share_patch = patch.object(
        pr, "research_shares_foreground_endpoint",
        return_value=(False, {"shared": False}),
    )

    async def gated_impl(block, **kwargs):
        name = getattr(block, "tool_type", "")
        if name == "research_parallel":
            return await real_impl(block, **kwargs)
        child_calls.append(name)
        if name == "web_search":
            return name, {
                "output": json.dumps({
                    "results": [{"title": "T", "url": "https://example.test/a", "snippet": "s"}],
                }),
                "exit_code": 0,
            }
        if name == "vault_knowledge_search":
            return name, {
                "approval_required": True,
                "ask_user": {"approval_id": "appr-test", "question": "Allow vault?"},
                "exit_code": 1,
                "executed": False,
            }
        return await real_impl(block, **kwargs)

    with bind_request_authority(auth) as effective, \
            bind_turn_contract(contract), \
            bind_catalog(contract, True), \
            patch.object(te, "_execute_tool_block_impl", new=gated_impl), \
            vault_patch, share_patch:
        await _discover_parent_only()
        _, first = await execute_tool_block(
            ToolBlock("research_parallel", parent),
            owner="alice",
            session_id="sess-parallel-auth",
            workspace=str(tmp_path),
            request_authority=effective,
            security_context=ToolRunSecurityContext(),
            tool_policy=ToolPolicy(),
        )
    first_pkg = json.loads(first["output"]) if isinstance(first.get("output"), str) else first.get("output")
    assert first.get("approval_required") is True
    assert first_pkg["status"] == "approval_required"
    assert first_pkg["web"]["status"] == "complete"
    assert first_pkg["vault"]["status"] == "approval_required"
    assert child_calls.count("web_search") == 1
    assert child_calls.count("vault_knowledge_search") == 1

    # Resume: vault succeeds; sealed web must not re-execute.
    async def resume_impl(block, **kwargs):
        name = getattr(block, "tool_type", "")
        if name == "research_parallel":
            return await real_impl(block, **kwargs)
        child_calls.append(name)
        if name == "vault_knowledge_search":
            return name, {
                "output": json.dumps({
                    "matches": [{
                        "note_id": "n1", "rel_path": "Notes/Hit.md", "title": "Hit",
                        "start_line": 1, "end_line": 2, "excerpt": "Curiosity Drive",
                        "content_hash": "abc", "score": 1.0,
                    }],
                    "mode": "lexical",
                }),
                "exit_code": 0,
            }
        if name == "web_search":
            raise AssertionError("web sibling must not re-execute on resume")
        return await real_impl(block, **kwargs)

    child_calls.clear()
    with bind_request_authority(auth) as effective, \
            bind_turn_contract(contract), \
            bind_catalog(contract, True), \
            patch.object(te, "_execute_tool_block_impl", new=resume_impl), \
            patch(
                "src.agent_runtime.vault_target.require_configured_vault",
                return_value=str(tmp_path),
            ), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {"shared": False})):
        await _discover_parent_only()
        _, second = await execute_tool_block(
            ToolBlock("research_parallel", parent),
            owner="alice",
            session_id="sess-parallel-auth",
            workspace=str(tmp_path),
            request_authority=effective,
            security_context=ToolRunSecurityContext(),
            tool_policy=ToolPolicy(),
        )
    second_pkg = json.loads(second["output"]) if isinstance(second.get("output"), str) else second.get("output")
    assert second_pkg["status"] == "complete"
    assert second_pkg["web"]["status"] == "complete"
    assert second_pkg["vault"]["status"] == "complete"
    assert "web_search" not in child_calls
    assert child_calls.count("vault_knowledge_search") == 1
    assert second_pkg["metrics"].get("reused_branches") == ["web"]

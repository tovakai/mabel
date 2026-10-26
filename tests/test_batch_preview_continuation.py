"""Server-owned, one-use authorization for previewed Obsidian batch edits."""
import json
from unittest.mock import AsyncMock

import pytest

from src.agent_runtime.authority import (
    MISSING_AUTHORITY, ExactOperation, OperationGrant, RequestAuthority,
    active_request_authority, with_request_authority,
)
from src.agent_runtime import batch_preview_claims as claims
from src.agent_tools.vault_batch_tools import run_batch
from src.tool_policy import ToolPolicy
from src.tool_types import ToolBlock


async def _confirmation(text, workspace, *, session="batch-s", policy=None, disabled=(), plan_mode=False):
    @with_request_authority
    async def probe(messages, owner="alice", session_id="batch-s", workspace=None,
                    request_authority=MISSING_AUTHORITY, tool_policy=None,
                    disabled_tools=None, plan_mode=False):
        yield active_request_authority()

    rows = [
        result async for result in probe(
            [{"role": "user", "content": text}],
            owner="alice", session_id=session, workspace=str(workspace),
            tool_policy=policy, disabled_tools=set(disabled), plan_mode=plan_mode,
        )
    ]
    return rows[0]


def _preview(tmp_path, session="batch-s"):
    note = tmp_path / "a.md"
    note.write_text("original", encoding="utf-8")
    payload = {"mode": "preview", "operation": "replace", "glob": "*.md",
               "old_string": "original", "new_string": "updated"}
    result = run_batch(tmp_path, payload)
    authority = RequestAuthority("preview-r", "alice", session, str(tmp_path),
                                 (OperationGrant("vault_batch_edit"),))
    apply_args = claims.remember_preview(
        owner="alice", session_id=session, workspace=str(tmp_path),
        payload=payload, result=result, authority=authority,
    )
    assert apply_args is not None
    assert note.read_text(encoding="utf-8") == "original"
    return apply_args


@pytest.mark.asyncio
async def test_confirmation_is_exact_and_consumed_once(tmp_path, monkeypatch):
    from src import tool_execution as execution

    args = _preview(tmp_path)
    authority = await _confirmation("Apply", tmp_path)
    operation = ExactOperation.normalize("vault_batch_edit", json.dumps(args))
    assert authority.request_id.startswith("batch:")
    assert authority.permits(operation)
    assert not authority.permits(ExactOperation.normalize("bash", "pwd"))
    assert not authority.permits(ExactOperation.normalize(
        "vault_batch_edit", json.dumps({**args, "remove_lines": ["DVD.base"]})))
    mock = AsyncMock(return_value=("vault_batch_edit", {"exit_code": 0}))
    monkeypatch.setattr(execution, "_execute_tool_block_impl", mock)

    block = ToolBlock("vault_batch_edit", json.dumps(args))
    _, first = await execution.execute_tool_block(
        block, owner="alice", session_id="batch-s", workspace=str(tmp_path),
        request_authority=authority, security_context=execution.NO_TOOL_SECURITY_CONTEXT)
    _, replay = await execution.execute_tool_block(
        block, owner="alice", session_id="batch-s", workspace=str(tmp_path),
        request_authority=authority, security_context=execution.NO_TOOL_SECURITY_CONTEXT)
    assert first["exit_code"] == 0
    assert replay["failure_kind"] == "batch_continuation_denied"
    mock.assert_awaited_once()
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_no_blanket_grants_and_no_bare_yes(tmp_path):
    args = _preview(tmp_path)
    not_a_confirmation = await _confirmation("yes", tmp_path)
    assert not not_a_confirmation.permits(
        ExactOperation.normalize("vault_batch_edit", json.dumps(args)))
    confirmed = await _confirmation("yes, apply the changes", tmp_path)
    assert confirmed.permits(ExactOperation.normalize("vault_batch_edit", json.dumps(args)))
    assert {g.tool for g in confirmed.grants} <= {"vault_batch_edit", "ask_user", "update_plan"}


@pytest.mark.asyncio
async def test_workspace_and_session_are_bound(tmp_path):
    args = _preview(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    wrong_workspace = await _confirmation("Apply", other)
    wrong_session = await _confirmation("Apply", tmp_path, session="other-session")
    op = ExactOperation.normalize("vault_batch_edit", json.dumps(args))
    assert not wrong_workspace.permits(op)
    assert not wrong_session.permits(op)
    correct = await _confirmation("Apply", tmp_path)
    assert correct.permits(op)


@pytest.mark.asyncio
async def test_current_disabled_tools_and_plan_mode_still_win(tmp_path):
    args = _preview(tmp_path)
    disabled = await _confirmation("Apply", tmp_path, disabled={"vault_batch_edit"})
    assert not disabled.permits(ExactOperation.normalize("vault_batch_edit", json.dumps(args)))
    plan = await _confirmation("Apply", tmp_path, plan_mode=True)
    assert not plan.permits(ExactOperation.normalize("vault_batch_edit", json.dumps(args)))
    policy = await _confirmation("Apply", tmp_path,
                                 policy=ToolPolicy(disabled_tools=frozenset({"vault_batch_edit"})))
    assert not policy.permits(ExactOperation.normalize("vault_batch_edit", json.dumps(args)))
    # None of the denials may consume the preview or turn it into a generic grant.
    allowed = await _confirmation("Apply", tmp_path)
    assert allowed.permits(ExactOperation.normalize("vault_batch_edit", json.dumps(args)))


@pytest.mark.asyncio
async def test_expired_grant_fails_closed(tmp_path, monkeypatch):
    args = _preview(tmp_path)
    authority = await _confirmation("Apply", tmp_path)
    import src.agent_runtime.batch_preview_claims as module
    baseline = module.monotonic()
    monkeypatch.setattr(module, "monotonic", lambda: baseline + 10000)
    assert module.consume_if_continuation(
        request_id=authority.request_id,
        operation=ExactOperation.normalize("vault_batch_edit", json.dumps(args))) is False


@pytest.mark.asyncio
async def test_get_workspace_denial_identifies_disabled_layer(tmp_path, monkeypatch):
    from src import tool_execution as execution
    impl = AsyncMock(return_value=("get_workspace", {"exit_code": 0}))
    monkeypatch.setattr(execution, "_execute_tool_block_impl", impl)
    grant = RequestAuthority("workspace-r", "alice", "batch-s", str(tmp_path),
                             (OperationGrant("get_workspace"),))
    block = ToolBlock("get_workspace", "")
    _, blocked = await execution.execute_tool_block(
        block, owner="alice", session_id="batch-s", workspace=str(tmp_path),
        disabled_tools={"get_workspace"}, request_authority=grant,
        security_context=execution.NO_TOOL_SECURITY_CONTEXT)
    assert blocked["failure_kind"] == "request_authority_denied"
    assert "caller_disabled_tools" in blocked["denial_sources"]
    impl.assert_not_awaited()
    _, allowed = await execution.execute_tool_block(
        block, owner="alice", session_id="batch-s", workspace=str(tmp_path),
        request_authority=grant, security_context=execution.NO_TOOL_SECURITY_CONTEXT)
    assert allowed["exit_code"] == 0

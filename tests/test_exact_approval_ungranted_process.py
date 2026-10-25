"""An exact approval must be able to run a bash/python call the request did not grant (#6635).

`ToolApprovalStore.create()` sealed the process identity only from the request
authority's launch scopes, and those exist only for granted bash/python
backends. For an ungranted call the sealing failed silently, so the approval
card was created with no process identity and the executor refused it:
"Approved action has no sealed process/job identity".
"""
import pytest

from src.agent_runtime.authority import OperationGrant, RequestAuthority
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action
from src.tool_types import ToolBlock


@pytest.fixture(autouse=True)
def admin_owner(monkeypatch):
    from src import tool_execution as execution
    monkeypatch.setattr(execution, "_owner_is_admin", lambda owner: True)


def _authority(workspace, *tools):
    return RequestAuthority("approval-request", "alice", "s", str(workspace),
                            tuple(OperationGrant(t) for t in tools))


def _approve(authority, tool, content, store=None):
    store = store or ToolApprovalStore()
    pending = store.create(owner=authority.owner, session_id=authority.session_id, origin_run_id="run",
        tool_name=tool, content=content, workspace=authority.workspace, request_authority=authority,
        external_untrusted_context_seen=True, capabilities=capabilities_for_action(tool, content))
    return pending, store.consume(pending.approval_id, decision="approve",
                                  owner=authority.owner, session_id=authority.session_id)


async def _dispatch(authority, tool, content, exact):
    from src import tool_execution as execution
    return await execution.execute_tool_block(ToolBlock(tool, content), owner=authority.owner,
        session_id=authority.session_id, workspace=authority.workspace,
        request_authority=authority, exact_approval=exact,
        security_context=ToolRunSecurityContext(external_untrusted_context_seen=True))


def test_approval_for_ungranted_bash_seals_a_launch_in_the_workspace(tmp_path):
    authority = _authority(tmp_path, "get_workspace")
    assert authority.launch_scopes == ()

    pending, _ = _approve(authority, "bash", "printf approved-run")

    bound = pending.process_operation
    assert bound is not None
    assert bound.launch is not None
    assert bound.launch.scope.root.path == str(tmp_path.resolve())
    assert bound.request_id == authority.request_id


async def test_approved_ungranted_bash_runs_once(tmp_path):
    authority = _authority(tmp_path, "get_workspace")
    _, exact = _approve(authority, "bash", "printf approved-run")

    description, result = await _dispatch(authority, "bash", "printf approved-run", exact)

    assert result.get("failure_kind") != "resource_identity_denied", result
    assert result.get("exit_code") == 0, result
    assert "approved-run" in str(result.get("output", "")) + description

    _, replay = await _dispatch(authority, "bash", "printf approved-run", exact)
    assert replay.get("blocked") is True, replay


async def test_inherited_authority_still_cannot_widen_to_ungranted_bash(tmp_path):
    # A child (inherited) authority may only narrow its parent, so an approval
    # it proposes for an ungranted launch must stay unsealed and unrunnable.
    from dataclasses import replace
    child = replace(_authority(tmp_path, "get_workspace"), inherited=True)
    pending, exact = _approve(child, "bash", "printf approved-run")

    assert pending.process_operation is None
    _, result = await _dispatch(child, "bash", "printf approved-run", exact)
    assert result.get("blocked") is True


@pytest.fixture
def agent_workspace():
    import os
    from src.constants import AGENT_WORKSPACE_DIR
    os.makedirs(AGENT_WORKSPACE_DIR, exist_ok=True)
    return os.path.realpath(AGENT_WORKSPACE_DIR)


def test_approval_without_a_workspace_seals_a_launch_in_the_agent_workspace(agent_workspace):
    # Agent mode with no folder picked is the default. bash already runs in the
    # agent workspace there (agent_cwd), so that is where its approval seals.
    authority = _authority("", "get_workspace")
    assert authority.resource_roots == ()

    pending, _ = _approve(authority, "bash", "pwd")

    bound = pending.process_operation
    assert bound is not None
    assert bound.launch is not None
    assert bound.launch.scope.root.path == agent_workspace


async def test_approved_bash_without_a_workspace_runs_in_the_agent_workspace(agent_workspace):
    authority = _authority("", "get_workspace")
    _, exact = _approve(authority, "bash", "pwd")

    description, result = await _dispatch(authority, "bash", "pwd", exact)

    assert result.get("failure_kind") != "resource_identity_denied", result
    assert result.get("exit_code") == 0, result
    containment = result.get("containment") or {}
    assert containment.get("workspace", agent_workspace) == agent_workspace, containment


async def test_inherited_authority_without_a_workspace_still_cannot_widen(agent_workspace):
    from dataclasses import replace
    child = replace(_authority("", "get_workspace"), inherited=True)
    pending, exact = _approve(child, "bash", "pwd")

    assert pending.process_operation is None
    _, result = await _dispatch(child, "bash", "pwd", exact)
    assert result.get("blocked") is True


async def test_approved_write_without_a_workspace_lands_in_the_agent_workspace(agent_workspace):
    # write_file had the same gap: with no workspace the approval sealed no
    # root, so "approve" ended in "no sealed resource identity".
    import os
    target = "approved-write-6635.txt"
    content = f"{target}\nhello"
    authority = _authority("", "get_workspace")
    pending, exact = _approve(authority, "write_file", content)
    assert pending.resource_operation is not None

    _, result = await _dispatch(authority, "write_file", content, exact)

    assert result.get("failure_kind") != "resource_identity_denied", result
    written = os.path.join(agent_workspace, target)
    try:
        with open(written) as fh:
            assert fh.read().strip() == "hello"
    finally:
        if os.path.exists(written):
            os.remove(written)


async def test_approved_ungranted_bash_runs_inside_its_own_request_context(tmp_path):
    """A resumed approval must survive the dispatcher's nested authority binding.

    Real agent turns enter execute_tool_block with the originating request
    authority already active. The dispatcher narrows it again before the
    process producer performs its admission check.
    """
    from src.agent_runtime.authority import active_request_authority, bind_request_authority

    authority = _authority(tmp_path, "get_workspace")
    _, exact = _approve(authority, "bash", "printf nested-approved-run")

    with bind_request_authority(authority):
        _, result = await _dispatch(authority, "bash", "printf nested-approved-run", exact)
        assert active_request_authority() is authority

    assert result.get("exit_code") == 0, result
    assert "nested-approved-run" in str(result.get("output", "")), result


def test_same_request_rebinding_narrows_without_becoming_a_child(tmp_path):
    from dataclasses import replace
    from src.agent_runtime.authority import (
        ExactOperation, bind_request_authority,
    )

    parent = _authority(tmp_path, "get_workspace", "read_file")
    restricted = parent.restrict(disabled_tools={"read_file"})

    with bind_request_authority(parent):
        with bind_request_authority(restricted) as same_request:
            assert same_request.request_id == parent.request_id
            assert same_request.inherited is False
            assert not same_request.permits(ExactOperation.normalize("read_file", "notes.md"))

        # A different request is a true child even with the same workspace.
        other_request = replace(parent, request_id="different-request")
        with bind_request_authority(other_request) as child:
            assert child.inherited is True

        # Matching request IDs alone are insufficient to rebind across
        # different session/owner/workspace identities.
        other_session = RequestAuthority(parent.request_id, "alice", "different-session",
                                         parent.workspace, parent.grants)
        with bind_request_authority(other_session) as child:
            assert child.inherited is True
            assert child.grants == ()

    # Already-inherited state must never be reset by a same-ID rebind.
    inherited_parent = replace(parent, inherited=True)
    with bind_request_authority(inherited_parent):
        with bind_request_authority(inherited_parent) as child:
            assert child.inherited is True

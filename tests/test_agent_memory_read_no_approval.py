"""Simulate Agent-mode pure reads after tool discovery, without a UI approval."""
import pytest

from src.agent_runtime.authority import (
    ExactOperation, approval_can_admit, bind_request_authority,
    create_request_authority,
)
from src.tool_capabilities import ToolRunSecurityContext, action_is_read_only


@pytest.mark.parametrize("input_text", ['list', '{"action":"list"}'])
def test_interactive_agent_memory_list_is_allowed_after_discovery(input_text):
    # Same server-owned authority construction used by interactive Agent mode.
    authority = create_request_authority(
        "Tell me what you know about me.",
        owner="alice", session_id="fresh-agent-chat", workspace="",
        interactive_review=True,
    )
    operation = ExactOperation.normalize("manage_memory", input_text)
    assert authority.permits(operation)
    context = ToolRunSecurityContext()
    context.observe_tool_result(
        "discover_tools",
        {"output": "tool metadata returned", "exit_code": 0},
    )
    assert context.external_untrusted_context_seen
    with bind_request_authority(authority):
        assert context.decision_for("manage_memory", input_text).allowed
        assert not approval_can_admit(
            authority, "manage_memory", input_text,
            owner="alice", session_id="fresh-agent-chat", workspace="",
        )
        # No permissions to mutate memory or execute a shell command.
        assert not context.decision_for("manage_memory", '{"action":"delete","id":"1"}').allowed
        assert not context.decision_for("bash", "pwd").allowed


@pytest.mark.parametrize(
    "tool_name,content,owner_granted",
    [
        ("manage_notes", '{"action":"search","query":"Lessons in Love"}', True),
        ("manage_notes", '{"action":"find","query":"Lessons in Love"}', True),
        ("manage_notes", '{"action":"list"}', True),
        ("manage_memory", "list", True),
        ("read_email", '{"id":"msg-1"}', True),
        ("vault_search", '{"query":"Lessons in Love"}', True),
        ("web_fetch", '{"url":"https://example.com/page"}', False),
    ],
)
def test_interactive_agent_pure_reads_skip_approval_after_discovery(
    tool_name, content, owner_granted,
):
    authority = create_request_authority(
        "Look up Lessons in Love in my notes.",
        owner="alice", session_id="fresh-agent-chat", workspace="/workspace",
        interactive_review=True,
    )
    assert action_is_read_only(tool_name, content)
    assert authority.permits(ExactOperation.normalize(tool_name, content)) is owner_granted
    context = ToolRunSecurityContext()
    context.observe_tool_result(
        "web_search",
        {"output": "public search hits", "exit_code": 0},
        '{"query":"Lessons in Love"}',
    )
    assert context.external_untrusted_context_seen
    with bind_request_authority(authority):
        assert context.decision_for(tool_name, content).allowed
        # Owner-granted personal reads need no gap card; brokered web reads
        # are free at the gate via READ_ONLY_EFFECTS even without a grant.
        if owner_granted:
            assert not approval_can_admit(
                authority, tool_name, content,
                owner="alice", session_id="fresh-agent-chat", workspace="/workspace",
            )
        assert not context.decision_for(
            "manage_notes", '{"action":"create","content":"new note"}',
        ).allowed
        assert not context.decision_for("bash", "pwd").allowed


def test_discovery_cannot_grant_memory_writes_to_an_ownerless_run():
    authority = create_request_authority(
        "Tell me what you know about me.",
        owner="", session_id="background", interactive_review=True,
    )
    context = ToolRunSecurityContext(external_untrusted_context_seen=True,
                                     external_sources=["discover_tools"])
    with bind_request_authority(authority):
        # Pure reads pass the gate; ownerless authority still has no grant.
        assert context.decision_for("manage_memory", "list").allowed
        assert not authority.permits(ExactOperation.normalize("manage_memory", "list"))
        assert not context.decision_for(
            "manage_memory", '{"action":"delete","id":"1"}',
        ).allowed

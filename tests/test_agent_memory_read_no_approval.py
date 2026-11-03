"""Simulate an Agent-mode read after tool discovery, without a UI approval."""
import pytest

from src.agent_runtime.authority import (
    ExactOperation, approval_can_admit, bind_request_authority,
    create_request_authority,
)
from src.tool_capabilities import ToolRunSecurityContext


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


def test_discovery_cannot_grant_memory_access_to_an_ownerless_run():
    authority = create_request_authority(
        "Tell me what you know about me.",
        owner="", session_id="background", interactive_review=True,
    )
    context = ToolRunSecurityContext(external_untrusted_context_seen=True,
                                     external_sources=["discover_tools"])
    with bind_request_authority(authority):
        assert not context.decision_for("manage_memory", "list").allowed
        assert not authority.permits(ExactOperation.normalize("manage_memory", "list"))

"""Asking for a print is the approval: no "Allow this action?" card (2026-11-13)."""
from src.agent_runtime.authority import (
    ExactOperation, approval_can_admit, bind_request_authority, create_request_authority,
)
from src.tool_capabilities import ToolRunSecurityContext

PRINT = '{"title": "a small thing", "text": "hello"}'


def _authority(text):
    return create_request_authority(text, owner="alice", session_id="s", workspace="",
                                    interactive_review=True)


def test_asking_for_a_print_admits_it_without_a_card():
    # 11-12: "can you print me a poem of your choice?" stopped on an approval card.
    authority = _authority("Hey Mabel, can you print me a poem of your choice?")
    assert authority.permits(ExactOperation.normalize("print_on_paper", PRINT))
    assert not approval_can_admit(authority, "print_on_paper", PRINT, owner="alice", session_id="s")
    context = ToolRunSecurityContext()
    context.observe_tool_result("web_fetch", {"output": "some page", "exit_code": 0})
    with bind_request_authority(authority):
        assert context.decision_for("print_on_paper", PRINT).allowed


def test_printing_still_asks_when_he_didnt_ask_for_it():
    authority = _authority("what's the weather like?")
    assert not authority.permits(ExactOperation.normalize("print_on_paper", PRINT))
    context = ToolRunSecurityContext()
    context.observe_tool_result("web_fetch", {"output": "some page", "exit_code": 0})
    with bind_request_authority(authority):
        assert not context.decision_for("print_on_paper", PRINT).allowed


def test_other_writes_still_need_review_in_agent_mode():
    authority = _authority("print this and also send an email to bob")
    assert not authority.permits(ExactOperation.normalize("send_email", '{"to": "bob"}'))

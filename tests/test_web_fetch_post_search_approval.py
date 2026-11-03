"""web_fetch authorization after web_search taints the external-context gate."""
from src.tool_capabilities import ToolRunSecurityContext


def test_web_search_then_web_fetch_requires_explicit_approval_gate():
    """After search results arm the untrusted gate, web_fetch asks for approval.

    web_fetch carries NETWORK_EGRESS and is blocked by POST_EXTERNAL_BLOCKED_EFFECTS
    once external untrusted context has been observed. web_search itself remains
    allowed (brokered read only). This is intentional — do not auto-approve.
    """
    ctx = ToolRunSecurityContext()
    assert ctx.decision_for("web_search", '{"query":"python gil"}').allowed is True
    assert ctx.decision_for(
        "web_fetch",
        '{"url":"https://docs.python.org/3/howto/free-threading-python.html"}',
    ).allowed is True

    ctx.observe_tool_result(
        "web_search",
        {"exit_code": 0, "output": "1. https://docs.python.org/3/howto/free-threading-python.html"},
        '{"query":"python gil"}',
    )
    assert ctx.external_untrusted_context_seen is True

    search_after = ctx.decision_for("web_search", '{"query":"pep 703"}')
    assert search_after.allowed is True

    fetch_after = ctx.decision_for(
        "web_fetch",
        '{"url":"https://docs.python.org/3/howto/free-threading-python.html"}',
    )
    assert fetch_after.allowed is False
    assert "network_egress" in (fetch_after.reason or "")


def test_chat_session_approval_bypass_allows_web_fetch_after_search_taint():
    """Smallest persistent authorization: chat-session approval bypasses this gate only."""
    ctx = ToolRunSecurityContext(
        external_untrusted_context_seen=True,
        external_sources=["web_search"],
        approval_gate_bypassed=True,
    )
    decision = ctx.decision_for(
        "web_fetch",
        '{"url":"https://peps.python.org/pep-0703/"}',
    )
    assert decision.allowed is True

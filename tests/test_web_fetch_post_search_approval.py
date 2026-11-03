"""web_fetch stays a brokered read after web_search taints the external-context gate."""
from src.tool_capabilities import ToolEffect, ToolRunSecurityContext, capabilities_for_action


def test_web_search_then_web_fetch_does_not_require_approval_gate():
    """Public web_fetch is brokered network read, same class as web_search.

    After search/vault results arm the untrusted gate, mutating egress still
    needs approval, but reading a URL through web_fetch does not.
    """
    fetch_caps = capabilities_for_action(
        "web_fetch",
        '{"url":"https://docs.python.org/3/howto/free-threading-python.html"}',
    )
    assert ToolEffect.BROKERED_NETWORK_READ in fetch_caps.effects
    assert ToolEffect.NETWORK_EGRESS not in fetch_caps.effects

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
    assert fetch_after.allowed is True

    # Mutating / open egress remains blocked after taint.
    assert ctx.decision_for("bash", "curl https://example.test").allowed is False


def test_vault_then_web_fetch_does_not_require_approval_gate():
    ctx = ToolRunSecurityContext()
    ctx.observe_tool_result(
        "vault_knowledge_search",
        {"exit_code": 0, "output": "note hit"},
        '{"query":"curiosity"}',
    )
    assert ctx.external_untrusted_context_seen is True
    assert ctx.decision_for(
        "web_fetch",
        '{"url":"https://peps.python.org/pep-0703/"}',
    ).allowed is True


def test_chat_session_approval_bypass_still_allows_web_fetch_after_search_taint():
    """Chat-session approval remains a valid bypass for any residual gate blocks."""
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

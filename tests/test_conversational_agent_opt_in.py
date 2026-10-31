"""Opt-in conversational agent routing must not bypass user privilege gates."""

import pytest


def test_conversational_agent_switch_is_server_owned(monkeypatch):
    from routes.chat_routes import _conversational_agent_opt_in
    from types import SimpleNamespace

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        state=SimpleNamespace(current_user="alice"),
        headers={},
    )
    monkeypatch.delenv("ODYSSEUS_CONVERSATIONAL_AGENT", raising=False)
    assert not _conversational_agent_opt_in(request, "chat")
    monkeypatch.setenv("ODYSSEUS_CONVERSATIONAL_AGENT", "true")
    assert _conversational_agent_opt_in(request, "chat")
    assert not _conversational_agent_opt_in(request, "agent")
    assert not _conversational_agent_opt_in(request, "chat", compare_mode=True)
    assert not _conversational_agent_opt_in(request, "chat", plan_mode=True)
    assert not _conversational_agent_opt_in(request, "chat", tool_approval_id="pending")


@pytest.mark.asyncio
async def test_opt_in_chat_uses_existing_agent_route_not_direct_chat(monkeypatch):
    from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint

    monkeypatch.setenv("ODYSSEUS_CONVERSATIONAL_AGENT", "true")
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    request = _RouteRequest("chat")
    request._form["compare_mode"] = "false"
    response = await endpoint(request)
    chunks = [chunk async for chunk in response.body_iterator]

    assert "agent" in captured
    assert "chat" not in captured
    assert any('"type": "turn_mode"' in chunk and '"mode": "agent"' in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_opt_in_does_not_override_no_agent_privilege(monkeypatch):
    from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint

    monkeypatch.setenv("ODYSSEUS_CONVERSATIONAL_AGENT", "true")
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    request = _RouteRequest("chat", privileges={"can_use_agent": False})
    request._form["compare_mode"] = "false"
    response = await endpoint(request)
    chunks = [chunk async for chunk in response.body_iterator]

    assert "chat" in captured
    assert "agent" not in captured
    assert any('"type": "turn_mode"' in chunk and '"mode": "chat"' in chunk for chunk in chunks)

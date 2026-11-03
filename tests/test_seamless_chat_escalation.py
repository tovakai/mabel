"""Chat mode quietly uses the agent runtime when the routing decision needs tools."""
import json

import pytest

from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint


async def _run_chat(monkeypatch, message, *, vault=True, use_web=False):
    from routes import chat_routes
    captured, chunks = {}, []
    with monkeypatch.context() as patch:
        endpoint = _chat_stream_endpoint(patch, "chat", captured, session_model="qwen3.8-flash-next-gyro-gyro-s")
        patch.setattr(chat_routes, "coerce_message_and_session", lambda *a, **k: (message, "session-1"))
        patch.setattr(chat_routes, "_configured_vault_in_scope", lambda *a, **k: vault)
        request = _RouteRequest("chat")
        request._form.update({"message": message, "compare_mode": "false"})
        if use_web:
            request._form["use_web"] = "true"
        response = await endpoint(request)
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    events = [json.loads(line[6:]) for c in chunks for line in c.splitlines()
              if line.startswith("data: {")]
    turn_mode = next((e for e in events if e.get("type") == "turn_mode"), {})
    return ("agent" if "agent" in captured else "chat"), turn_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "What do my notes say about Lessons in Love?",
    "What is the SHA-256 of the string hello?",
    "how many days are there between 14 March 1987 and 3 November 2026?",
    "Compare my notes on Lessons in Love with public information about the game.",
    # Live regression: a sealed note edit (no family) stayed plain chat and the
    # model invented the "edited" note.
    "Feel free to: correct the Selebus spelling mistake (the creator is written as Selbius) "
    "and pad out the existing notes regarding Lessons in Love.",
    "fix the creator typo in my Lessons in Love note",
])
async def test_tool_needing_chat_turns_escalate_silently(monkeypatch, message):
    runtime, turn_mode = await _run_chat(monkeypatch, message)
    assert runtime == "agent"
    assert turn_mode.get("auto_escalated") is True   # the UI renders this as a chat turn


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "So, I just finished chapter 1 in Lessons in Love, and just wow.",
    "thanks, that actually helped a lot!",
    "what do you think makes a magic system feel satisfying?",
])
async def test_casual_chat_stays_plain_chat(monkeypatch, message):
    runtime, _ = await _run_chat(monkeypatch, message)
    assert runtime == "chat"


@pytest.mark.asyncio
async def test_web_only_question_with_web_off_is_not_forced_into_a_refusal(monkeypatch):
    # Chat with the web toggle off keeps answering conversationally instead of
    # escalating into the "web access is disabled" refusal.
    runtime, _ = await _run_chat(monkeypatch, "what is the anime Dandadan about?", use_web=False)
    assert runtime == "chat"


@pytest.mark.asyncio
async def test_notes_question_without_a_reachable_vault_uses_the_notes_store(monkeypatch):
    runtime, _ = await _run_chat(monkeypatch, "What do my notes say about Lessons in Love?", vault=False)
    # Without a reachable vault the question maps to the personal Notes store,
    # which is still a tool need.
    assert runtime == "agent"


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "what is the anime Dandadan about?",          # web-only seal
    # (A pasted URL to summarize already escalates through the existing
    # exact-URL rule; Chat mode sends no explicit web-off.)
])
async def test_web_only_seals_with_web_off_stay_chat(monkeypatch, message):
    runtime, _ = await _run_chat(monkeypatch, message, use_web=False)
    assert runtime == "chat"

"""Private reasoning must not commit a fallback candidate or role health."""

import json

import pytest

import src.llm_core as llm_core


def _event(chunk):
    if not chunk.startswith("data: ") or chunk.startswith("data: [DONE]"):
        return {}
    return json.loads(chunk[6:])


@pytest.mark.asyncio
async def test_reasoning_only_primary_allows_backup_to_answer(monkeypatch):
    calls = []

    async def stream(url, model, messages, **kwargs):
        calls.append(model)
        if model == "primary":
            yield 'data: {"delta": "private thoughts", "thinking": true}\\n\\n'
        else:
            yield 'data: {"delta": "backup answer"}\\n\\n'
        yield "data: [DONE]\\n\\n"

    monkeypatch.setattr(llm_core, "_stream_candidate_with_context_recovery", stream)
    chunks = [chunk async for chunk in llm_core.stream_llm_with_fallback.__wrapped__(
        [("https://primary.example/v1", "primary", {}),
         ("https://backup.example/v1", "backup", {})],
        [{"role": "user", "content": "hello"}],
        fallback_on_empty=True,
        role_retry_precontent=False,
    )]
    events = [_event(chunk) for chunk in chunks]
    assert calls == ["primary", "backup"]
    assert any(e.get("type") == "fallback" and e.get("answered_by") == "backup" for e in events)
    assert any(e.get("delta") == "backup answer" for e in events)
    assert not any(e.get("thinking") for e in events)
    assert chunks[-1] == "data: [DONE]\\n\\n"


@pytest.mark.asyncio
async def test_reasoning_only_last_candidate_is_failed_completion(monkeypatch):
    async def stream(url, model, messages, **kwargs):
        yield 'data: {"delta": "private thoughts", "thinking": true}\\n\\n'
        yield "data: [DONE]\\n\\n"

    monkeypatch.setattr(llm_core, "_stream_candidate_with_context_recovery", stream)
    chunks = [chunk async for chunk in llm_core.stream_llm_with_fallback.__wrapped__(
        [("https://primary.example/v1", "primary", {})],
        [{"role": "user", "content": "hello"}],
        role_retry_precontent=False,
    )]
    assert any(chunk.startswith("event: error") and "no substantive output" in chunk for chunk in chunks)
    assert not any("private thoughts" in chunk for chunk in chunks)
    assert not any("data: [DONE]" in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_reasoning_then_final_text_stays_on_primary(monkeypatch):
    calls = []

    async def stream(url, model, messages, **kwargs):
        calls.append(model)
        yield 'data: {"delta": "private thoughts", "thinking": true}\\n\\n'
        yield 'data: {"delta": "visible answer"}\\n\\n'
        yield "data: [DONE]\\n\\n"

    monkeypatch.setattr(llm_core, "_stream_candidate_with_context_recovery", stream)
    chunks = [chunk async for chunk in llm_core.stream_llm_with_fallback.__wrapped__(
        [("https://primary.example/v1", "primary", {}),
         ("https://backup.example/v1", "backup", {})],
        [{"role": "user", "content": "hello"}],
        role_retry_precontent=False,
    )]
    assert calls == ["primary"]
    assert any(_event(chunk).get("delta") == "visible answer" for chunk in chunks)
    assert not any(_event(chunk).get("type") == "fallback" for chunk in chunks)
    assert chunks[-1] == "data: [DONE]\\n\\n"

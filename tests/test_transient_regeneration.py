"""Transient model errors before any output are regenerated (up to twice), never after output."""
import asyncio
import json

import pytest

import src.llm_core as llm_core


def _error(status=None, message="Read timeout"):
    body = {"error": message}
    if status is not None:
        body["status"] = status
    return f"event: error\ndata: {json.dumps(body)}\n\n"


DIAG = 'data: ' + json.dumps({"type": "diagnostic_event", "operation": "model_route"}) + "\n\n"
TEXT = 'data: ' + json.dumps({"delta": "Hello!"}) + "\n\n"


def _run(monkeypatch, attempts):
    calls = []

    async def fake_once(candidates, messages, **kwargs):
        calls.append(kwargs.copy())
        for chunk in attempts[len(calls) - 1]:
            yield chunk

    monkeypatch.setattr(llm_core, "_stream_llm_with_fallback_once", fake_once)
    monkeypatch.setattr(llm_core, "TRANSIENT_BACKOFF_S", (0.0, 0.0))

    async def collect():
        return [c async for c in llm_core.stream_llm_with_fallback([("u", "m", {})], [], timeout=5)]
    return asyncio.run(collect()), calls


def test_timeout_before_output_is_regenerated(monkeypatch):
    out, calls = _run(monkeypatch, [[DIAG, _error(504)], [DIAG, TEXT]])
    assert len(calls) == 2 and TEXT in out and not any(c.startswith("event: error") for c in out)
    assert calls[1] == {"timeout": 5}, "every attempt gets the original arguments"


def test_gives_up_after_two_regenerations(monkeypatch):
    out, calls = _run(monkeypatch, [[_error(503)], [_error(503)], [_error(503)]])
    assert len(calls) == 3 and out[-1].startswith("event: error")


@pytest.mark.parametrize("chunk", [_error(400, "bad request"), _error(None, "context length exceeded")])
def test_non_transient_errors_surface_immediately(monkeypatch, chunk):
    out, calls = _run(monkeypatch, [[chunk], [TEXT]])
    assert len(calls) == 1 and out == [chunk]


def test_errors_after_output_are_never_regenerated(monkeypatch):
    out, calls = _run(monkeypatch, [[TEXT, _error(504)], [TEXT]])
    assert len(calls) == 1 and out == [TEXT, _error(504)]


def test_message_only_timeout_counts_as_transient(monkeypatch):
    out, calls = _run(monkeypatch, [[_error(None, "upstream timed out")], [TEXT]])
    assert len(calls) == 2 and out == [TEXT]

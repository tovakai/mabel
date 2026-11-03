"""Provider context recovery must persist into the next agent request."""
import asyncio
import json

from src import llm_core
from src.agent_runtime.context_recovery import (
    answering_recovery_bag,
    compact_canonical_messages_for_limit,
)
from src.model_context import (
    _context_cache,
    _provider_context_overrides,
    get_context_length,
    reconcile_local_runtime_context,
    remembered_provider_context_limit,
    remember_provider_context_limit,
)


def setup_function():
    _provider_context_overrides.clear()
    _context_cache.clear()


def test_remember_provider_context_limit_only_tightens():
    assert remember_provider_context_limit("http://local/v1", "m", 32768) == 32768
    assert remember_provider_context_limit("http://local/v1", "m", 65536) == 32768
    assert remember_provider_context_limit("http://local/v1", "m", 16384) == 16384
    assert remembered_provider_context_limit("http://local/v1", "m") == 16384


def test_reconcile_local_runtime_context_can_raise_or_lower_after_reload():
    remember_provider_context_limit("http://127.0.0.1:8080/v1", "strata-m", 32768)
    assert reconcile_local_runtime_context(
        "http://127.0.0.1:8080/v1", "strata-m", 65536
    ) == 65536
    assert remembered_provider_context_limit(
        "http://127.0.0.1:8080/v1", "strata-m"
    ) == 65536
    assert reconcile_local_runtime_context(
        "http://127.0.0.1:8080/v1", "strata-m", 24576
    ) == 24576
    assert remembered_provider_context_limit(
        "http://127.0.0.1:8080/v1", "strata-m"
    ) == 24576


def test_strata_health_reload_raises_stale_remembered_ceiling(monkeypatch):
    remember_provider_context_limit("http://127.0.0.1:9000/v1", "qwen", 16384)

    class _Resp:
        is_success = True

        @staticmethod
        def json():
            return {
                "service": "strata",
                "loaded": True,
                "max_context": 65536,
            }

    monkeypatch.setattr(
        "src.model_context.httpx.get",
        lambda *args, **kwargs: _Resp(),
    )
    assert get_context_length("http://127.0.0.1:9000/v1", "qwen") == 65536
    assert remembered_provider_context_limit(
        "http://127.0.0.1:9000/v1", "qwen"
    ) == 65536


def test_non_authoritative_catalog_still_tightened_by_rejection(monkeypatch):
    remember_provider_context_limit("https://api.example/v1", "remote-m", 8192)

    monkeypatch.setattr(
        "src.model_context._configured_endpoint_kind",
        lambda url: "api",
    )
    monkeypatch.setattr(
        "src.model_context._lookup_known",
        lambda model: 128000,
    )
    assert get_context_length("https://api.example/v1", "remote-m") == 8192


def test_successful_context_recovery_publishes_bag_with_diagnostics(monkeypatch):
    calls = []
    recovery_bags = {}

    async def fake_stream(url, model, messages, **kwargs):
        calls.append([dict(message) for message in messages])
        if len(calls) == 1:
            yield (
                'event: error\ndata: {"status": 400, "text": '
                '"maximum context length is 8192; request has 12000 input tokens"}\n\n'
            )
            return
        yield 'data: {"delta": "ok"}\n\n'
        yield "data: [DONE]\n\n"

    def fake_trim(messages, context_length, reserve_tokens=0):
        return [{"role": "user", "content": "trimmed-for-next-round"}]

    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)
    monkeypatch.setattr("src.context_compactor.trim_for_context", fake_trim)
    monkeypatch.setattr(llm_core, "get_context_length", lambda url, model: 128000)

    async def run():
        return [
            chunk
            async for chunk in llm_core.stream_llm_with_fallback(
                [("http://local/v1", "model-a", {})],
                [{"role": "user", "content": "x" * 20000}],
                max_tokens=2048,
                diagnostic_events=True,
                context_recovery_out=recovery_bags,
            )
        ]

    chunks = asyncio.run(run())
    assert len(calls) == 2
    assert calls[1] == [{"role": "user", "content": "trimmed-for-next-round"}]
    assert recovery_bags[0]["applied"] is True
    assert recovery_bags[0]["context_limit"] == 8192
    assert recovery_bags[0]["messages"] == [
        {"role": "user", "content": "trimmed-for-next-round"}
    ]
    assert remembered_provider_context_limit("http://local/v1", "model-a") == 8192
    prepared = [
        json.loads(chunk[6:])
        for chunk in chunks
        if chunk.startswith("data: ")
        and '"operation": "context_recovery_prepared"' in chunk
    ]
    applied = [
        json.loads(chunk[6:])
        for chunk in chunks
        if chunk.startswith("data: ")
        and '"operation": "context_recovery_applied"' in chunk
    ]
    assert prepared and prepared[0]["status"] == "prepared"
    assert applied and applied[0]["context_limit"] == 8192
    assert not any('"operation": "context_recovery"' in chunk for chunk in chunks)


def test_incognito_recovery_bag_fills_without_diagnostics(monkeypatch):
    """diagnostic_events=False (incognito) must still publish the recovery bag."""
    recovery_bags = {}

    async def fake_stream(url, model, messages, **kwargs):
        if not getattr(fake_stream, "retried", False):
            fake_stream.retried = True
            yield (
                'event: error\ndata: {"status": 400, "text": '
                '"maximum context length is 4096; request has 8000 input tokens"}\n\n'
            )
            return
        yield 'data: {"delta": "incognito-ok"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)
    monkeypatch.setattr(
        "src.context_compactor.trim_for_context",
        lambda messages, context_length, reserve_tokens=0: [
            {"role": "user", "content": "trimmed-incognito"}
        ],
    )
    monkeypatch.setattr(llm_core, "get_context_length", lambda url, model: 128000)

    async def run():
        return [
            chunk
            async for chunk in llm_core.stream_llm_with_fallback(
                [("http://local/v1", "model-b", {})],
                [{"role": "user", "content": "y" * 10000}],
                max_tokens=1024,
                diagnostic_events=False,
                context_recovery_out=recovery_bags,
            )
        ]

    chunks = asyncio.run(run())
    assert recovery_bags[0]["applied"] is True
    assert recovery_bags[0]["context_limit"] == 4096
    assert answering_recovery_bag(recovery_bags, 0) is not None
    assert not any("diagnostic_event" in chunk for chunk in chunks)
    assert remembered_provider_context_limit("http://local/v1", "model-b") == 4096


def test_tool_call_stream_marks_recovery_applied(monkeypatch):
    recovery_bags = {}

    async def fake_stream(url, model, messages, **kwargs):
        if not getattr(fake_stream, "retried", False):
            fake_stream.retried = True
            yield (
                'event: error\ndata: {"status": 400, "text": '
                '"maximum context length is 2048; request has 5000 input tokens"}\n\n'
            )
            return
        yield (
            'data: {"type": "tool_calls", "calls": '
            '[{"id": "c1", "name": "web_search", "arguments": "{\\"query\\":\\"x\\"}"}]}\n\n'
        )
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)
    monkeypatch.setattr(
        "src.context_compactor.trim_for_context",
        lambda messages, context_length, reserve_tokens=0: list(messages),
    )
    monkeypatch.setattr(llm_core, "get_context_length", lambda url, model: 128000)

    async def run():
        return [
            chunk
            async for chunk in llm_core.stream_llm_with_fallback(
                [("http://local/v1", "tool-model", {})],
                [{"role": "user", "content": "z" * 6000}],
                max_tokens=512,
                diagnostic_events=False,
                context_recovery_out=recovery_bags,
            )
        ]

    chunks = asyncio.run(run())
    assert recovery_bags[0]["applied"] is True
    assert recovery_bags[0]["context_limit"] == 2048
    assert any("tool_calls" in chunk for chunk in chunks)


def test_failed_candidate_recovery_is_not_committed_to_fallback(monkeypatch):
    calls = []
    recovery_bags = {}

    async def fake_stream(url, model, messages, **kwargs):
        calls.append(model)
        if model == "primary":
            yield (
                'event: error\ndata: {"status": 400, "text": '
                '"maximum context length is 4096; request has 9000 input tokens"}\n\n'
            )
            return
        yield 'data: {"delta": "fallback-ok"}\n\n'
        yield "data: [DONE]\n\n"

    def fake_trim(messages, context_length, reserve_tokens=0):
        return [{"role": "user", "content": f"trimmed-{context_length}"}]

    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)
    monkeypatch.setattr("src.context_compactor.trim_for_context", fake_trim)
    monkeypatch.setattr(llm_core, "get_context_length", lambda url, model: 128000)

    async def run():
        return [
            chunk
            async for chunk in llm_core.stream_llm_with_fallback(
                [
                    ("http://local/v1", "primary", {}),
                    ("http://local/v1", "secondary", {}),
                ],
                [{"role": "user", "content": "x" * 12000}],
                max_tokens=1024,
                fallback_statuses={400, 500, 502, 503, 504},
                context_recovery_out=recovery_bags,
            )
        ]

    chunks = asyncio.run(run())
    assert calls[0] == "primary"
    assert calls[-1] == "secondary"
    assert calls.count("primary") >= 2
    assert recovery_bags.get(0, {}).get("applied") is not True
    assert recovery_bags.get(1, {}).get("applied") is not True
    assert answering_recovery_bag(recovery_bags, 0) is None
    assert any("fallback-ok" in chunk for chunk in chunks)


def test_compact_canonical_preserves_protected_and_tool_pairs():
    messages = [
        {"role": "system", "content": "old policy " + ("pad " * 12000)},
        {
            "role": "system",
            "content": "Active document body " + ("doc " * 200),
            "_protected": True,
            "_agent_meta": {"doc_id": "doc-9"},
        },
        {
            "role": "user",
            "content": "active request keep me",
            "_agent_meta": {"turn": 7},
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "search hits"},
    ]
    # Learned ceiling must leave headroom for generation reserve while still
    # forcing the oversized system prompt to trim.
    limit = 8192
    compacted = compact_canonical_messages_for_limit(
        messages,
        context_limit=limit,
        max_tokens=1024,
    )
    from src.model_context import estimate_tokens

    assert estimate_tokens(compacted) < estimate_tokens(messages)
    protected = next(msg for msg in compacted if msg.get("_protected"))
    assert protected.get("_agent_meta", {}).get("doc_id") == "doc-9"
    active_user = next(
        msg for msg in compacted
        if msg.get("role") == "user" and "active request keep me" in str(msg.get("content") or "")
    )
    assert active_user.get("_agent_meta", {}).get("turn") == 7
    assert any(
        msg.get("role") == "assistant" and msg.get("tool_calls")
        for msg in compacted
    )
    assert any(
        msg.get("role") == "tool" and msg.get("tool_call_id") == "call_1"
        for msg in compacted
    )
    # Must not blindly adopt a bare wire snapshot missing harness markers.
    assert any(msg.get("_protected") for msg in compacted)
    assert estimate_tokens(compacted) + 1024 < limit


def test_answering_recovery_bag_then_canonical_commit_stays_in_budget():
    bags = {
        0: {
            "applied": True,
            "committed": False,
            "context_limit": 4096,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": "wire-only no markers"}],
        }
    }
    bag = answering_recovery_bag(bags, 0)
    assert bag is not None
    canonical = [
        {"role": "system", "content": "history " + ("word " * 5000)},
        {
            "role": "user",
            "content": "please continue",
            "_protected": True,
        },
    ]
    limit = int(bag["context_limit"])
    compacted = compact_canonical_messages_for_limit(
        canonical,
        limit,
        max_tokens=int(bag["max_tokens"]),
    )
    from src.model_context import estimate_tokens

    # Following round must fit the recovered ceiling with generation reserve.
    assert estimate_tokens(compacted) + 512 < limit
    assert any(msg.get("_protected") for msg in compacted)
    bag["committed"] = True
    assert answering_recovery_bag(bags, 0) is None

"""Tests for model_context.py — local endpoint detection, token estimation, known model lookup."""

import sys
import types

import pytest

import src.model_context as model_context
from src.model_context import is_local_endpoint, estimate_tokens, _lookup_known


class _Column:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return ("eq", self.name, value)


class _ModelEndpoint:
    is_enabled = _Column("is_enabled")


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *conditions):
        for condition in conditions:
            if isinstance(condition, tuple) and condition[0] == "eq":
                _, field, value = condition
                self.rows = [row for row in self.rows if getattr(row, field) == value]
        return self

    def all(self):
        return list(self.rows)


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def query(self, model):
        return _Query(self.rows)

    def close(self):
        pass


def _install_endpoint_db(monkeypatch, rows):
    mod = types.ModuleType("core.database")
    mod.ModelEndpoint = _ModelEndpoint
    mod.SessionLocal = lambda: _Db(rows)
    monkeypatch.setitem(sys.modules, "core.database", mod)


class TestIsLocalEndpoint:
    def test_localhost(self):
        assert is_local_endpoint("http://localhost:5000/v1/chat/completions") is True

    def test_loopback_ipv4(self):
        assert is_local_endpoint("http://127.0.0.1:8080/v1/chat/completions") is True

    def test_private_192_168(self):
        assert is_local_endpoint("http://192.168.1.1:11434/v1/chat/completions") is True

    def test_private_10(self):
        assert is_local_endpoint("http://10.0.0.5:8000/v1/chat/completions") is True

    @pytest.mark.parametrize("host", [
        "10.example-cloud.com",
        "172.16.example-cloud.com",
        "192.168.example-cloud.com",
    ])
    def test_private_prefix_dns_names_are_remote(self, host):
        assert is_local_endpoint(f"https://{host}/v1/chat/completions") is False

    def test_tailscale_100(self):
        # 100.64.0.0/10 is the CGNAT range Tailscale uses.
        assert is_local_endpoint("http://100.64.0.1:5000/v1/chat/completions") is True

    def test_configured_tailscale_proxy_is_remote(self, monkeypatch):
        _install_endpoint_db(monkeypatch, [
            types.SimpleNamespace(
                base_url="http://100.117.136.97:34521/v1",
                endpoint_kind="proxy",
                api_key="fake-key",
                is_enabled=True,
            )
        ])

        assert is_local_endpoint("http://100.117.136.97:34521/v1/chat/completions") is False

    def test_openai_is_remote(self):
        assert is_local_endpoint("https://api.openai.com/v1/chat/completions") is False

    def test_anthropic_is_remote(self):
        assert is_local_endpoint("https://api.anthropic.com/v1/messages") is False

    def test_empty_url(self):
        assert is_local_endpoint("") is False

    def test_malformed_url(self):
        assert is_local_endpoint("not-a-url") is False


class TestEstimateTokens:
    def test_empty_list(self):
        assert estimate_tokens([]) == 0

    def test_single_short_message(self):
        messages = [{"role": "user", "content": "Hello"}]
        tokens = estimate_tokens(messages)
        # 4 overhead + int(5 * 0.3) = 4 + 1 = 5
        assert tokens == 5

    def test_multiple_messages(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi there"},
        ]
        tokens = estimate_tokens(messages)
        assert tokens > 0
        # Each message adds 4 overhead + chars * 0.3
        assert tokens == 4 + int(16 * 0.3) + 4 + int(8 * 0.3)

    def test_multimodal_content_list(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ]
        tokens = estimate_tokens(messages)
        # 4 overhead + int(19 * 0.3) for the text item; image_url is ignored
        assert tokens == 4 + int(19 * 0.3)

    def test_missing_content_key(self):
        messages = [{"role": "assistant"}]
        tokens = estimate_tokens(messages)
        # 4 overhead + 0 content
        assert tokens == 4

    def test_scales_with_length(self):
        short = estimate_tokens([{"role": "user", "content": "short"}])
        long_text = "a" * 10000
        long = estimate_tokens([{"role": "user", "content": long_text}])
        assert long > short * 10

    def test_cjk_text_is_not_estimated_with_latin_character_ratio(self):
        cjk = estimate_tokens([{"role": "user", "content": "诉讼时效期间" * 20}])
        latin = estimate_tokens([{"role": "user", "content": "a" * 120}])

        assert cjk == 124
        assert cjk > latin * 2

    def test_binary_replacement_text_is_not_estimated_as_latin(self):
        replacement_dump = estimate_tokens([
            {"role": "tool", "content": "\ufffd" * 1000},
        ])

        assert replacement_dump == 1004


class TestLookupKnown:
    def test_claude_sonnet(self):
        assert _lookup_known("claude-sonnet-4-5") == 200000

    def test_gpt4o(self):
        assert _lookup_known("gpt-4o") == 128000

    def test_deepseek_r1(self):
        assert _lookup_known("deepseek-r1") == 64000

    def test_deepseek_flash_provider_alias(self):
        assert _lookup_known("deepseek-flash") == 64000

    def test_gemini_pro(self):
        assert _lookup_known("gemini-2.5-pro") == 1048576

    def test_unknown_model(self):
        assert _lookup_known("totally-unknown-model-xyz") is None

    def test_namespaced_model(self):
        """Models prefixed with provider/ should still match."""
        result = _lookup_known("openrouter/deepseek-r1")
        assert result == 64000

    def test_model_with_tag(self):
        """Models with :free or :extended suffixes should still match."""
        result = _lookup_known("deepseek-r1:free")
        assert result == 64000

    def test_o1_mini_not_shadowed_by_o1(self):
        """'o1' (200k) precedes 'o1-mini' (128k) in the table; longest match wins."""
        assert _lookup_known("o1-mini") == 128000

    def test_o1_full(self):
        assert _lookup_known("o1") == 200000

    def test_gpt4o_mini_not_shadowed_by_gpt4(self):
        assert _lookup_known("gpt-4o-mini") == 128000

    def test_gpt4_base(self):
        assert _lookup_known("gpt-4") == 8192


class _FakeResp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.is_success = ok

    def json(self):
        return self._payload


class TestGetContextLength:
    def setup_method(self):
        model_context._context_cache.clear()
        model_context._catalog_ctx_cache.clear()
        model_context._provider_context_overrides.clear()
        model_context._runtime_served_models.clear()

    def test_local_endpoint_requeries_same_model_after_restart(self, monkeypatch):
        calls = []

        def fake_query(endpoint_url, model):
            calls.append((endpoint_url, model))
            return (8192, True) if len(calls) == 1 else (27000, True)

        monkeypatch.setattr(model_context, "_query_context_length", fake_query)

        endpoint = "http://127.0.0.1:8000/v1/chat/completions"
        model = "Qwen/Qwen3-14B"

        first = model_context.get_context_length(endpoint, model)
        second = model_context.get_context_length(endpoint, model)

        assert first == 8192
        assert second == 27000
        assert len(calls) == 2


    def test_strata_health_active_window_overrides_model_card(self, monkeypatch):
        calls = []

        def fake_get(url, *args, **kwargs):
            calls.append(url)
            if url == "http://127.0.0.1:8080/health":
                return _FakeResp({
                    "status": "ok",
                    "service": "strata",
                    "loaded": True,
                    "model": "qwen3.8-flash-next-gyro-gyro-s",
                    "max_context": 32768,
                })
            raise AssertionError(f"Strata context must resolve from /health: {url}")

        monkeypatch.setattr(model_context.httpx, "get", fake_get)
        endpoint = "http://127.0.0.1:8080/v1/chat/completions"
        # qwen3 would otherwise match the generic 131072-token model card.
        assert model_context.get_context_length(endpoint, "qwen3.8-flash-next-gyro-gyro-s") == 32768
        assert calls == ["http://127.0.0.1:8080/health"]
        # Local contexts are deliberately reprobed after every server restart.
        assert model_context.get_context_length(endpoint, "qwen3.8-flash-next-gyro-gyro-s") == 32768
        assert len(calls) == 2

    def test_unloaded_strata_health_is_not_authoritative(self, monkeypatch):
        calls = []

        def fake_get(url, *args, **kwargs):
            calls.append(url)
            if url.endswith("/health"):
                return _FakeResp({"service": "strata", "loaded": False, "max_context": 32768})
            if url.endswith("/slots"):
                return _FakeResp([{"n_ctx": 16384}])
            raise AssertionError(f"Unexpected context probe: {url}")

        monkeypatch.setattr(model_context.httpx, "get", fake_get)
        endpoint = "http://127.0.0.1:8080/v1/chat/completions"
        assert model_context.get_context_length(endpoint, "qwen3.8-flash-next-gyro-gyro-s") == 16384
        assert calls == ["http://127.0.0.1:8080/health", "http://127.0.0.1:8080/slots"]

    def test_unrelated_health_endpoint_cannot_override_llamacpp(self, monkeypatch):
        def fake_get(url, *args, **kwargs):
            if url.endswith("/health"):
                return _FakeResp({"status": "ok", "max_context": 1000000})
            if url.endswith("/slots"):
                return _FakeResp([{"n_ctx": 8192}])
            raise AssertionError(url)

        monkeypatch.setattr(model_context.httpx, "get", fake_get)
        endpoint = "http://127.0.0.1:8080/v1/chat/completions"
        assert model_context.get_context_length(endpoint, "qwen3.8-flash-next-gyro-gyro-s") == 8192

    def test_remote_endpoint_keeps_cached_context(self, monkeypatch):
        calls = []

        def fake_query(endpoint_url, model):
            calls.append((endpoint_url, model))
            return (200000, True) if len(calls) == 1 else (12345, True)

        monkeypatch.setattr(model_context, "_query_context_length", fake_query)

        endpoint = "https://api.openai.com/v1/chat/completions"
        model = "gpt-5"

        first = model_context.get_context_length(endpoint, model)
        second = model_context.get_context_length(endpoint, model)

        assert first == 200000
        assert second == 200000
        assert len(calls) == 1

    def _proxy_db(self, monkeypatch):
        _install_endpoint_db(monkeypatch, [
            types.SimpleNamespace(
                base_url="http://100.117.136.97:34521/v1",
                endpoint_kind="proxy",
                api_key="fake-key",
                is_enabled=True,
            )
        ])

    def test_configured_proxy_known_model_skips_model_listing(self, monkeypatch):
        # A model covered by the known-context table must still resolve without
        # touching /models — the cheap path the proxy short-circuit exists for.
        # A Tailscale proxy may receive one trusted-host /health probe; only a
        # loaded Strata payload is authoritative, so non-Strata health falls
        # through to the known table without catalog download.
        self._proxy_db(monkeypatch)
        fetches = []

        def fake_get(url, *args, **kwargs):
            fetches.append(url)
            if str(url).endswith("/health"):
                return _FakeResp({"status": "ok"}, ok=True)
            raise AssertionError("/models must not be queried for a known proxy model")

        monkeypatch.setattr(model_context.httpx, "get", fake_get)

        endpoint = "http://100.117.136.97:34521/v1/chat/completions"
        assert model_context.get_context_length(endpoint, "gpt-4o") == 128000
        assert all(str(url).endswith("/health") for url in fetches)

    def test_proxy_classified_local_strata_uses_health_over_known_table(self, monkeypatch):
        # Localhost Strata with an API key is often stored as endpoint_kind=proxy.
        # That must not skip /health or budget the generic Qwen 131072 card.
        _install_endpoint_db(monkeypatch, [
            types.SimpleNamespace(
                base_url="http://127.0.0.1:8080/v1",
                endpoint_kind="proxy",
                api_key="sk-strata-test",
                is_enabled=True,
            )
        ])
        assert model_context.is_local_endpoint(
            "http://127.0.0.1:8080/v1/chat/completions"
        ) is False
        assert model_context._trusted_local_host(
            "http://127.0.0.1:8080/v1/chat/completions"
        ) is True

        health_calls = []

        def fake_get(url, *args, **kwargs):
            health_calls.append(url)
            if url == "http://127.0.0.1:8080/health":
                return _FakeResp({
                    "status": "ok",
                    "service": "strata",
                    "loaded": True,
                    "model": "qwen3.8-flash-next-gyro-gyro-s",
                    "max_context": 32768,
                })
            raise AssertionError(f"unexpected probe: {url}")

        monkeypatch.setattr(model_context.httpx, "get", fake_get)
        endpoint = "http://127.0.0.1:8080/v1/chat/completions"
        requested = "qwen3.8-flash-next-gyro-gyro-m"
        assert model_context._lookup_known(requested) == 131072
        assert model_context.get_context_length(endpoint, requested) == 32768
        assert health_calls == ["http://127.0.0.1:8080/health"]
        assert model_context.runtime_served_model(endpoint) == (
            "qwen3.8-flash-next-gyro-gyro-s"
        )
        # Preference string is not rewritten; telemetry exposes the served id.
        assert requested != model_context.runtime_served_model(endpoint)

    def test_proxy_classified_local_strata_reload_raises_ceiling(self, monkeypatch):
        _install_endpoint_db(monkeypatch, [
            types.SimpleNamespace(
                base_url="http://127.0.0.1:8080/v1",
                endpoint_kind="proxy",
                api_key="sk-strata-test",
                is_enabled=True,
            )
        ])
        payloads = [
            {
                "service": "strata",
                "loaded": True,
                "model": "qwen3.8-flash-next-gyro-gyro-s",
                "max_context": 32768,
            },
            {
                "service": "strata",
                "loaded": True,
                "model": "qwen3.8-flash-next-gyro-gyro-s",
                "max_context": 65536,
            },
        ]

        def fake_get(url, *args, **kwargs):
            assert url.endswith("/health")
            return _FakeResp(payloads.pop(0))

        monkeypatch.setattr(model_context.httpx, "get", fake_get)
        endpoint = "http://127.0.0.1:8080/v1/chat/completions"
        model = "qwen3.8-flash-next-gyro-gyro-m"
        assert model_context.get_context_length(endpoint, model) == 32768
        assert model_context.get_context_length(endpoint, model) == 65536

    def test_remote_proxy_health_is_never_probed(self, monkeypatch):
        _install_endpoint_db(monkeypatch, [
            types.SimpleNamespace(
                base_url="https://openrouter.ai/api/v1",
                endpoint_kind="proxy",
                api_key="fake",
                is_enabled=True,
            )
        ])

        def fake_get(url, *args, **kwargs):
            raise AssertionError(f"remote proxy must not be health-probed: {url}")

        monkeypatch.setattr(model_context.httpx, "get", fake_get)
        endpoint = "https://openrouter.ai/api/v1/chat/completions"
        assert model_context.get_context_length(endpoint, "gpt-4o") == 128000
        assert model_context.runtime_served_model(endpoint) == ""

    def test_configured_proxy_unknown_model_reads_catalog_context(self, monkeypatch):
        # A model missing from the known table (e.g. a new OpenRouter model)
        # must report the catalog's real window, not the bare default (#4886).
        # The catalog is fetched once per endpoint and reused for other models.
        self._proxy_db(monkeypatch)
        fetches = []

        def fake_get(url, *args, **kwargs):
            fetches.append(url)
            if str(url).endswith("/health"):
                return _FakeResp({"status": "ok"})
            return _FakeResp({"data": [
                {"id": "owl-alpha", "context_length": 1048576},
                {"id": "tiny-proxy-model", "context_length": 8192},
            ]})

        monkeypatch.setattr(model_context.httpx, "get", fake_get)

        endpoint = "http://100.117.136.97:34521/v1/chat/completions"
        assert model_context.get_context_length(endpoint, "owl-alpha") == 1048576
        # A second unknown model on the same endpoint reuses the cached catalog.
        assert model_context.get_context_length(endpoint, "tiny-proxy-model") == 8192
        catalog_fetches = [url for url in fetches if not str(url).endswith("/health")]
        assert len(catalog_fetches) == 1

    def test_configured_proxy_unknown_model_falls_back_to_default(self, monkeypatch):
        # If the catalog can be read but doesn't list the model, keep the
        # conservative default rather than guessing.
        self._proxy_db(monkeypatch)

        def fake_get(url, *args, **kwargs):
            if str(url).endswith("/health"):
                return _FakeResp({"status": "ok"})
            return _FakeResp({"data": [{"id": "some-other-model", "context_length": 4096}]})

        monkeypatch.setattr(model_context.httpx, "get", fake_get)

        endpoint = "http://100.117.136.97:34521/v1/chat/completions"
        assert model_context.get_context_length(endpoint, "absent-model") == model_context.DEFAULT_CONTEXT

    def test_configured_proxy_catalog_fetch_failure_uses_default(self, monkeypatch):
        # A failed/unreachable catalog must not raise — fall back to the default.
        self._proxy_db(monkeypatch)

        def fake_get(url, *args, **kwargs):
            if str(url).endswith("/health"):
                return _FakeResp({"status": "ok"})
            raise RuntimeError("network down")

        monkeypatch.setattr(model_context.httpx, "get", fake_get)

        endpoint = "http://100.117.136.97:34521/v1/chat/completions"
        assert model_context.get_context_length(endpoint, "unknown-proxy-model") == model_context.DEFAULT_CONTEXT

"""An offline embedding PC costs one connect timeout per minute, not one per lookup."""
import httpx
import pytest

from src.embeddings import EmbeddingClient


def test_fails_fast_after_a_failed_connect(monkeypatch):
    client = EmbeddingClient(url="http://100.64.0.9:8082/v1/embeddings", model="m")
    calls = []

    def _post(*a, **k):
        calls.append(1)
        raise httpx.ConnectTimeout("down")

    monkeypatch.setattr(client._client, "post", _post)
    monkeypatch.setattr(EmbeddingClient, "_down_until", {})
    for _ in range(3):
        with pytest.raises(httpx.ConnectError if calls else httpx.ConnectTimeout):
            client.encode(["hello"])
    assert len(calls) == 1


def test_comes_back_after_the_pause(monkeypatch):
    client = EmbeddingClient(url="http://100.64.0.9:8082/v1/embeddings", model="m")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"embedding": [1.0, 0.0], "index": 0}]}

    monkeypatch.setattr(EmbeddingClient, "_down_until", {client.url: 0.0})
    monkeypatch.setattr(client._client, "post", lambda *a, **k: _Resp())
    assert client.encode(["hello"]).shape == (1, 2)
    assert client.url not in EmbeddingClient._down_until

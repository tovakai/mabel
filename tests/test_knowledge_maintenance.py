"""Background indexing is local/read-only; embedding inference stays remote."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import model_roles
from src.knowledge_engine import maintenance
from src.knowledge_engine.index import VaultIndex
from tests.test_knowledge_index import vault


def _role(adapter="openai_embeddings", *, dimensions=1024):
    return model_roles.Assignment(
        endpoint_id="dottie-bge", model="bge-m3",
        adapter=adapter, capabilities=("embedding",),
        space_id="BAAI/bge-m3", revision="pinned", dimensions=dimensions,
    )


def test_remote_assignment_never_silently_uses_cpu(monkeypatch):
    monkeypatch.setattr(model_roles, "candidates", lambda role, owner: [_role("fastembed_cached")])
    with pytest.raises(model_roles.CapabilityUnavailable, match="remote HTTP"):
        maintenance.remote_embedding_assignment("alice")


def test_remote_assignment_requires_explicit_owner(monkeypatch):
    with pytest.raises(ValueError, match="exact Mabel account owner"):
        maintenance.remote_embedding_assignment("")


def test_remote_assignment_checks_all_fallbacks(monkeypatch):
    monkeypatch.setattr(
        model_roles, "candidates",
        lambda role, owner: [_role(), _role("fastembed_cached")],
    )
    monkeypatch.setattr(model_roles, "resolve", lambda *args, **kwargs: SimpleNamespace(url="http://dottie:8082/v1"))
    with pytest.raises(model_roles.CapabilityUnavailable, match="CPU FastEmbed"):
        maintenance.remote_embedding_assignment("alice")


def test_remote_assignment_accepts_dottie_http_endpoint(monkeypatch):
    monkeypatch.setattr(model_roles, "candidates", lambda role, owner: [_role()])
    def resolve(assignment, owner, required=None):
        assert owner == "alice" and required == "embedding"
        return SimpleNamespace(url="http://dottie:8082/v1")
    monkeypatch.setattr(model_roles, "resolve", resolve)
    assert maintenance.remote_embedding_assignment("alice").model == "bge-m3"


@pytest.mark.asyncio
async def test_embedding_worker_only_updates_disposable_index(vault, monkeypatch):
    root, cache = vault
    index = VaultIndex(root, owner="alice", cache_dir=cache)
    index.reconcile()
    before = {p: p.read_bytes() for p in root.rglob("*.md")}
    called = []
    from src.knowledge_engine import semantic

    monkeypatch.setattr(maintenance, "remote_embedding_assignment", lambda owner: _role())
    async def fake_embedding(target, *, owner, limit, batch_size):
        called.append((owner, limit, batch_size))
        return {"generated": 3, "coverage": [], "bounded": False, "routes": []}
    monkeypatch.setattr(semantic, "index_embeddings", fake_embedding)

    result = await maintenance.embed_pending(index, owner="alice", limit=16, batch_size=4)
    assert result["configured_model"] == "bge-m3"
    assert result["generated"] == 3
    assert called == [("alice", 16, 4)]
    assert before == {p: p.read_bytes() for p in root.rglob("*.md")}


@pytest.mark.asyncio
async def test_embedding_worker_does_not_implicitly_scan_new_vault(vault, monkeypatch):
    root, cache = vault
    index = VaultIndex(root, owner="alice", cache_dir=cache)
    monkeypatch.setattr(maintenance, "remote_embedding_assignment",
                        lambda owner: pytest.fail("must not resolve role before first scan"))
    with pytest.raises(RuntimeError, match="never been scanned"):
        await maintenance.embed_pending(index, owner="alice")
    assert index.status()["last_scan"] is None

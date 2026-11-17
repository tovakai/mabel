"""ChromaDB starts after the web app at boot (2026-11-16 03:06, 2026-11-17 12:51): the
memory store must connect once it's up instead of staying degraded until a restart."""
from types import SimpleNamespace

import src.memory_vector as mv


def test_store_connects_once_chromadb_is_up(monkeypatch):
    up = {"chroma": False}
    lane = SimpleNamespace(name=mv.LANE_FASTEMBED, collection=SimpleNamespace(name="c"))

    def lanes(_name):
        if not up["chroma"]:
            raise RuntimeError("ChromaDB is not reachable at localhost:8100")
        return [lane]

    clock = {"t": 1000.0}
    monkeypatch.setattr(mv, "build_embedding_lanes", lanes)
    monkeypatch.setattr(mv, "migrate_legacy_collection", lambda *a: None)
    monkeypatch.setattr(mv, "lane_count", lambda _lanes: 3)
    monkeypatch.setattr(mv.time, "monotonic", lambda: clock["t"])

    store = mv.MemoryVectorStore("/nonexistent")
    assert store.healthy is False
    up["chroma"] = True
    assert store.healthy is False            # no retry storm: waits RETRY_S
    clock["t"] += mv.MemoryVectorStore.RETRY_S
    assert store.healthy is True
    assert store.count() == 3

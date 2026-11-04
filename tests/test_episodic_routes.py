"""Memory sidebar → Episodes tab API."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.episodic_memory import CandidateQueue, CapturePolicy, capture_completed_turn


def _client(monkeypatch, tmp_path, *, owner="alice", admin=True):
    import src.constants
    from routes.memory import episodic_routes
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(episodic_routes, "storage_owner_for_request", lambda request: owner)
    monkeypatch.setattr(episodic_routes, "get_current_user", lambda request: owner)
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda user: admin)
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: True if key.startswith("episodic_") and key.endswith("_enabled") else default)
    app = FastAPI()
    app.include_router(episodic_routes.setup_episodic_routes())
    from src.auth_helpers import require_user
    app.dependency_overrides[require_user] = lambda: owner
    return TestClient(app)


def _seed(tmp_path, owner="alice", n=2):
    ids = []
    for i in range(n):
        row = capture_completed_turn(
            storage_dir=tmp_path / "episodic_candidates", owner=owner, session_id=f"session-{i}",
            assistant_message_id=f"msg-{i}",
            user_message=f"Tell me about the approval boundary bug number {i}, please.",
            assistant_message=f"The approval boundary bug {i} came from a stale permission receipt.",
            policy=CapturePolicy(episodic_enabled=True, memory_enabled=True))
        ids.append(row["id"])
    return ids


def test_overview_lists_pending_with_readable_excerpts(monkeypatch, tmp_path):
    _seed(tmp_path)
    data = _client(monkeypatch, tmp_path).get("/api/memory/episodic").json()
    assert data["capture_enabled"] and data["can_manage_settings"]
    assert len(data["pending"]) == 2 and data["episodes"] == []
    first = data["pending"][0]
    assert first["user_excerpt"].startswith("Tell me about the approval boundary bug")
    assert first["assistant_excerpt"].startswith("The approval boundary bug")
    assert first["session_id"] == "session-0"


def test_later_and_discard_settle_candidates(monkeypatch, tmp_path):
    first, second = _seed(tmp_path)
    client = _client(monkeypatch, tmp_path)
    assert client.post(f"/api/memory/episodic/{first}/later").json()["state"] == "deferred"
    assert client.post(f"/api/memory/episodic/{second}/discard").json()["state"] == "rejected"
    data = client.get("/api/memory/episodic").json()
    assert data["pending"] == [] and [d["id"] for d in data["deferred"]] == [first]
    assert client.post(f"/api/memory/episodic/{second}/later").status_code == 409  # already settled


def test_keep_is_the_explicit_approval_for_verified_promotion(monkeypatch, tmp_path):
    (cid,) = _seed(tmp_path, n=1)
    seen = {}

    def fake_promote(queue, candidate_id, **kwargs):
        seen.update(kwargs, candidate_id=candidate_id, owner=queue.owner)
        return {"id": candidate_id, "state": "promoted", "changed": True}
    monkeypatch.setattr("src.episodic_memory.promotion.promote_verified_conversation", fake_promote)
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda owner: {"memory_enabled": True})
    result = _client(monkeypatch, tmp_path).post(f"/api/memory/episodic/{cid}/keep").json()
    assert result["state"] == "promoted"
    assert seen["approved"] is True and seen["owner"] == "alice" and seen["candidate_id"] == cid
    assert seen["protected_roots"] is not None


def test_users_only_see_their_own_candidates(monkeypatch, tmp_path):
    _seed(tmp_path, owner="alice")
    data = _client(monkeypatch, tmp_path, owner="bob").get("/api/memory/episodic").json()
    assert data["pending"] == []
    (cid,) = [r["id"] for r in CandidateQueue(tmp_path / "episodic_candidates", owner="alice").list()][:1]
    assert _client(monkeypatch, tmp_path, owner="bob").post(f"/api/memory/episodic/{cid}/discard").status_code == 409


def test_switches_are_admin_only(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr("src.settings.save_settings", lambda s: saved.update(s))
    monkeypatch.setattr("src.settings.load_settings", lambda: {})
    denied = _client(monkeypatch, tmp_path, admin=False).put("/api/memory/episodic/settings",
                                                                json={"recall_enabled": False})
    assert denied.status_code == 403 and saved == {}
    ok = _client(monkeypatch, tmp_path).put("/api/memory/episodic/settings", json={"recall_enabled": False})
    assert ok.status_code == 200 and saved["episodic_recall_enabled"] is False

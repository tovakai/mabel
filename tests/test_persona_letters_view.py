"""The Letters tab: the whole correspondence in order, and one letter at a time (no path tricks)."""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path):
    from routes import persona_routes
    from src.auth_helpers import require_user
    folder = tmp_path / "journal" / "letters"
    (folder / "notes").mkdir(parents=True)
    (folder / "Tova-to-Mabel-2026-11-05.md").write_text("---\nfrom: tova\n---\n# Dear Mabel\n\nHello there you.", encoding="utf-8")
    (folder / "Mabel-to-Tova-2026-11-05.md").write_text("# Dear Tova\n\nHi back, wet floors.\n\n— Mabel", encoding="utf-8")
    (folder / "Tova-to-Mabel-2026-11-07.md").write_text("# Again\n\nOne more.", encoding="utf-8")
    (folder / "notes" / "private.md").write_text("what stayed", encoding="utf-8")
    (folder / "random.md").write_text("not a letter", encoding="utf-8")
    monkeypatch.setattr(persona_routes, "_REPO", tmp_path)
    monkeypatch.setattr(persona_routes, "get_current_user", lambda request: "alice")
    app = FastAPI()
    app.include_router(persona_routes.setup_persona_routes())
    app.dependency_overrides[require_user] = lambda: "alice"
    return TestClient(app)


def test_letters_in_order_with_replies_after(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    letters = client.get("/api/persona/letters").json()["letters"]
    assert [l["name"] for l in letters] == ["Tova-to-Mabel-2026-11-05", "Mabel-to-Tova-2026-11-05", "Tova-to-Mabel-2026-11-07"]
    assert letters[1]["from"] == "Mabel" and letters[0]["words"] == 6


def test_one_letter_without_front_matter_and_nothing_else(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    body = client.get("/api/persona/letter/Tova-to-Mabel-2026-11-05").json()["markdown"]
    assert body.startswith("# Dear Mabel") and "from: tova" not in body
    for bad in ("random", "notes%2Fprivate", "..%2F..%2Fsecret", "Tova-to-Mabel-2026-11-09"):
        assert client.get(f"/api/persona/letter/{bad}").status_code == 404

"""The terminal client's LIFE snapshot and token scopes (2026-11-17)."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import api_token_routes, tui_routes
from routes.music_routes import music_user


def _request(scopes=(), token=True):
    return SimpleNamespace(state=SimpleNamespace(api_token=token, api_token_scopes=list(scopes),
                                                 api_token_owner="tovakai", current_user="api" if token else "tovakai"))


def test_tui_profile_bundles_chat_music_and_life():
    assert api_token_routes.TOKEN_PROFILES["tui"] == ["chat", "music", "life:read"]
    assert {"music", "life:read"} <= api_token_routes.ALLOWED_SCOPES


def test_life_and_music_need_their_scopes():
    assert tui_routes.life_user(_request(["life:read"])) == "tovakai"
    assert music_user(_request(["music"])) == "tovakai"
    with pytest.raises(HTTPException):
        tui_routes.life_user(_request(["chat"]))
    with pytest.raises(HTTPException):
        music_user(_request(["lifelog:write"]))


def test_snapshot_reads_her_files_and_cleans_journal_text(tmp_path, monkeypatch):
    persona = tmp_path / "persona"
    persona.mkdir()
    repo = tmp_path / "repo"
    (repo / "journal" / "dreams").mkdir(parents=True)
    (repo / "journal" / "dreams" / "d.md").write_text(
        "---\ntype: dream\n---\n![[d.png]]\n\n# Dream: Tuesday\n\nA pink hallway.\n\n> [!note]- From\n> Tova's letter\n")
    monkeypatch.setattr(tui_routes, "_REPO", repo)
    (persona / "dreams.json").write_text(json.dumps({"history": [
        {"at": "2026-11-17T13:06:44+01:00", "title": "A letter from Tova", "note": "journal/dreams/d.md", "image": "/x/d.png"}]}))
    (persona / "books.json").write_text(json.dumps({"current": {"title": "Angel", "length": 200, "position": 50}}))
    monkeypatch.setattr(tui_routes, "jobs", lambda now: [])
    monkeypatch.setattr(tui_routes, "doing", lambda: ["reading"])
    snap = tui_routes.snapshot(tmp_path, datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc))
    assert snap["doing"] == ["reading"]
    assert snap["dream"]["text"] == "A pink hallway." and snap["dream"]["has_image"]
    assert snap["book"]["progress"] == 0.25


def test_journal_text_never_leaves_the_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(tui_routes, "_REPO", tmp_path)
    assert tui_routes._body("../../etc/passwd") == ""

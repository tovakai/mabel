"""Life log: store, summary, Home Assistant spans, collector, daily composer, ingest auth."""
import asyncio
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.lifelog.store import Store
from src.lifelog.summary import day_bounds, summarize

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)


def _collector():
    spec = importlib.util.spec_from_file_location("collector", Path("scripts/lifelog_collector.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_store_validates_upserts_and_excludes(tmp_path):
    store = Store(tmp_path / "l.sqlite")
    t0 = NOW - timedelta(minutes=10)
    span = {"id": "focus:1", "kind": "focus", "start": t0.isoformat(), "end": (t0 + timedelta(minutes=1)).isoformat(),
            "app": "firefox", "title": "Toei"}
    assert store.ingest("Desktop!", [span], now=NOW) == {"stored": 1, "skipped": 0}
    store.ingest("desktop", [{**span, "end": (t0 + timedelta(minutes=5)).isoformat()}], now=NOW)   # extends
    rows = store.spans(NOW - timedelta(hours=1), NOW)
    assert len(rows) == 1 and rows[0]["end"].startswith((t0 + timedelta(minutes=5)).astimezone(timezone.utc).isoformat()[:16])
    bad = [{"kind": "telepathy", "start": t0.isoformat()},
           {"kind": "focus", "start": "not a date"},
           {"kind": "browse", "start": t0.isoformat(), "url": "https://www.bank.example/login?secret=1", "title": "Bank"},
           {"kind": "browse", "start": t0.isoformat(), "url": "https://en.wikipedia.org/wiki/Toei?x=1", "title": "Toei"}]
    result = store.ingest("desktop", bad, exclude_sites=["bank.example"], now=NOW)
    assert result == {"stored": 1, "skipped": 3}
    browse = store.spans(NOW - timedelta(hours=1), NOW, kinds=["browse"])[0]
    assert browse["site"] == "en.wikipedia.org" and "wiki/Toei" not in json.dumps(browse), "site only, never the URL"
    assert [d["device"] for d in store.devices()] == ["desktop"]


def test_night_owl_day_and_summary():
    start, end = day_bounds(datetime(2026, 11, 6, 3, 0, tzinfo=TZ))
    assert start == datetime(2026, 11, 5, 6, 0, tzinfo=TZ) and end - start == timedelta(days=1)
    spans = [
        {"device": "desktop", "kind": "focus", "app": "firefox", "title": "", "site": "", "detail": {},
         "start": "2026-11-05T20:00:00+00:00", "end": "2026-11-05T21:00:00+00:00"},
        {"device": "frame", "kind": "game", "app": "steam", "title": "SomeGameVR", "site": "", "detail": {},
         "start": "2026-11-05T22:00:00+00:00", "end": "2026-11-05T23:30:00+00:00"},
        {"device": "desktop", "kind": "browse", "app": "", "title": "Toei Animation - Wikipedia", "site": "en.wikipedia.org",
         "detail": {}, "start": "2026-11-05T20:10:00+00:00", "end": "2026-11-05T20:10:00+00:00"},
        {"device": "phone", "kind": "steps", "app": "phone", "title": "4200", "site": "", "detail": {"steps": 4200},
         "start": "2026-11-05T19:00:00+00:00", "end": "2026-11-05T23:00:00+00:00"}]
    s = summarize(spans, start=start, end=end)
    assert s["apps"] == [{"name": "firefox", "minutes": 60}] and s["games"] == [{"name": "SomeGameVR", "minutes": 90}]
    assert s["steps"] == 4200 and s["pages"] == ["Toei Animation - Wikipedia (en.wikipedia.org)"]


def test_home_assistant_states_become_spans():
    from src.lifelog.home import spans_from_states
    states = [
        {"entity_id": "person.anthon", "state": "not_home", "last_changed": "2026-11-05T12:00:00+00:00"},
        {"entity_id": "device_tracker.my_phone", "state": "not_home"},
        {"entity_id": "sensor.my_phone_steps_sensor", "state": "5321", "last_changed": "2026-11-05T13:00:00+00:00"},
        {"entity_id": "sensor.my_phone_detected_activity", "state": "walking", "last_changed": "2026-11-05T13:10:00+00:00"},
        {"entity_id": "sensor.my_phone_battery_level", "state": "70"},
        {"entity_id": "sensor.kitchen_temperature", "state": "21"},
        {"entity_id": "sensor.my_phone_last_used_app", "state": "unavailable"}]
    spans = spans_from_states(states, person="person.anthon", now=NOW)
    assert [(s["kind"], s["title"]) for s in spans] == [("place", "away from home"), ("steps", "5321"), ("activity", "walking")]


def test_collector_spans_names_and_browser_privacy(tmp_path, monkeypatch):
    c = _collector()
    spans = c.Spans()
    t = NOW
    assert len(spans.observe("focus", {"app": "kitty", "title": "vim"}, t)) == 1
    extended = spans.observe("focus", {"app": "kitty", "title": "vim"}, t + timedelta(seconds=30))
    assert len(extended) == 1 and extended[0]["end"] == (t + timedelta(seconds=30)).isoformat()
    switched = spans.observe("focus", {"app": "firefox", "title": "x"}, t + timedelta(seconds=60))
    assert [s["app"] for s in switched] == ["kitty", "firefox"]
    assert c.name_from_command("reaper SteamLaunch AppId=3 -- proton run /h/Games/[X] Some Game/SomeGameVR.exe") == "SomeGameVR"
    profile = tmp_path / ".mozilla" / "firefox" / "abc.default"
    profile.mkdir(parents=True)
    db = sqlite3.connect(profile / "places.sqlite")
    db.executescript("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                     "CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY, place_id INT, visit_date INT);")
    recent = int((datetime.now(timezone.utc) - timedelta(minutes=5)).timestamp() * 1_000_000)
    db.execute("INSERT INTO moz_places VALUES (1, 'https://www.example.org/private/path?token=abc', 'Example page')")
    db.execute("INSERT INTO moz_places VALUES (2, 'about:config', 'cfg')")
    db.execute("INSERT INTO moz_historyvisits VALUES (1, 1, ?)", (recent,))
    db.execute("INSERT INTO moz_historyvisits VALUES (2, 2, ?)", (recent + 1,))
    db.commit()
    db.close()
    monkeypatch.setattr(c, "FIREFOX_PROFILES", [str(profile / "places.sqlite")])
    monkeypatch.setattr(c, "CHROMIUM_PROFILES", [])
    cursors = {}
    visits = c.browser_visits(cursors)
    assert [(v["site"], v["title"]) for v in visits] == [("example.org", "Example page")]
    assert "token" not in json.dumps(visits) and "/private/" not in json.dumps(visits)
    assert c.browser_visits(cursors) == [], "the cursor prevents re-sending"


def test_compose_day_writes_her_account(tmp_path):
    from src.lifelog.compose import compose_day, journal_for
    store = Store(tmp_path / "l.sqlite")
    day = datetime(2026, 11, 5, 6, 0, tzinfo=TZ)
    store.ingest("frame", [{"id": "g", "kind": "game", "start": "2026-11-05T21:00:00+00:00",
                            "end": "2026-11-05T22:00:00+00:00", "title": "SomeGameVR"}], now=NOW + timedelta(days=1))
    vault = tmp_path / "vault"
    (vault / "daily life of Anthon").mkdir(parents=True)
    (vault / "daily life of Anthon" / "2026-11-05.md").write_text("---\ntags: [day]\n---\nWent to the shop, felt good.")
    assert journal_for(day, day + timedelta(days=1), vault=vault)[0]["text"] == "Went to the shop, felt good."
    seen = {}

    async def ask(messages, owner):
        seen["prompt"] = messages[-1]["content"]
        return ("# Anthon's day: Thursday 5 November\n\n## Timeline\n- 22:00 SomeGameVR for an hour.\n\n"
                "## What stood out to me\nA calm day.\n\n## Things I'm curious about\n- What did you buy at the shop?\n")
    mabel = tmp_path / "mabel"
    result = asyncio.run(compose_day(day, store=store, ask=ask, vault=vault, mabel_vault=mabel))
    assert result["status"] == "written" and result["curious"] == ["What did you buy at the shop?"]
    assert "SomeGameVR (60 min)" in seen["prompt"] and "Went to the shop" in seen["prompt"]
    written = (mabel / "journal" / "anthon" / "2026-11-05.md").read_text()
    assert written.startswith("---\ntype: day-summary") and "## Timeline" in written
    empty = asyncio.run(compose_day(day + timedelta(days=3), store=store, ask=ask, vault=vault, mabel_vault=mabel))
    assert empty["status"] == "nothing_recorded"


def test_ingest_requires_the_lifelog_scope(tmp_path, monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from routes import lifelog_routes
    store = Store(tmp_path / "l.sqlite")
    monkeypatch.setattr("src.lifelog.store.default_store", lambda: store)
    monkeypatch.setattr(lifelog_routes, "_setting", lambda key, default: default)
    scopes = {"value": ["chat"]}
    app = FastAPI()

    @app.middleware("http")
    async def fake_token(request: Request, call_next):
        request.state.api_token = True
        request.state.api_token_scopes = scopes["value"]
        request.state.api_token_owner = "tovakai"
        request.state.api_token_id = "tok1"
        request.state.current_user = "api"
        return await call_next(request)
    app.include_router(lifelog_routes.setup_lifelog_routes())
    client = TestClient(app)
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    body = {"device": "laptop", "spans": [{"id": "f", "kind": "focus", "start": t0.isoformat(),
                                           "end": (t0 + timedelta(minutes=1)).isoformat(), "app": "kitty"}]}
    assert client.post("/api/lifelog/ingest", json=body).status_code == 403
    scopes["value"] = ["lifelog:write"]
    assert client.post("/api/lifelog/ingest", json=body).json() == {"stored": 1, "skipped": 0}
    assert store.devices()[0]["token_id"] == "tok1"
    assert "lifelog_collector" in client.get("/api/lifelog/collector.py").text

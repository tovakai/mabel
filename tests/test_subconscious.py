"""Phase 4: Dottie's typed inbox and the Archivist, Scout and Dreamer loops."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.subconscious.inbox import Inbox, InboxError
from src.knowledge_engine.index import VaultIndex

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
EV = [{"ref": "note:A.md", "kind": "note"}]


def test_inbox_dedupes_and_never_resuggests_a_rejection(tmp_path):
    inbox = Inbox(tmp_path / "inbox.sqlite")
    item, outcome = inbox.add(kind="duplicate", loop="archivist", title="x", dedupe_key="k", evidence=EV, now=NOW)
    assert outcome == "created" and item["status"] == "new"
    assert inbox.add(kind="duplicate", loop="archivist", title="x2", dedupe_key="k", evidence=EV, now=NOW)[1] == "refreshed"
    inbox.decide(item["id"], "rejected", by="user", reason="not the same thing")
    again, outcome = inbox.add(kind="duplicate", loop="archivist", title="x3", dedupe_key="k", evidence=EV, now=NOW)
    assert outcome == "kept_decision" and again["status"] == "rejected"
    kept = inbox.get(item["id"])
    assert (kept["loop"], kept["decided_by"], kept["decision_reason"]) == ("archivist", "user", "not the same thing")
    assert inbox.list() == []


def test_inbox_validates_and_expires(tmp_path):
    inbox = Inbox(tmp_path / "inbox.sqlite")
    with pytest.raises(InboxError):
        inbox.add(kind="gossip", loop="archivist", title="x", dedupe_key="a", evidence=EV)
    with pytest.raises(InboxError):
        inbox.add(kind="pattern", loop="dreamer", title="x", dedupe_key="b", evidence=[])
    inbox.add(kind="pattern", loop="dreamer", title="old", dedupe_key="c", evidence=EV, days=1,
              now=NOW - timedelta(days=3))
    assert inbox.list() == [] and inbox.counts().get("expired") == 1


def _vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Library" / "Movies").mkdir(parents=True)
    (vault / "Library" / "Private").mkdir(parents=True)
    (vault / "Library" / "The Aristocats.md").write_text("# The Aristocats\n")
    (vault / "Library" / "Studio Ghibli.md").write_text("# Studio Ghibli\n")
    (vault / "Library" / "Men in Black.md").write_text("# Men in Black\n")
    (vault / "Library" / "The Rock.md").write_text("# The Rock\n")
    (vault / "Library" / "Movies" / "Night.md").write_text(
        "---\nGenre: \"[[Rock]]\"\n---\n# Night\nWe watched [[The Artistocrats]] and talked about [[ghibli]] "
        "and [[the Man in Black]] on [[the Island]] and [[the island]].\n")
    (vault / "Library" / "Private" / "Diary.md").write_text("Met at [[Studio Gibli]]\n")
    index = VaultIndex(vault, owner="alice", cache_dir=tmp_path / "cache")
    index.reconcile()
    return index.path


def test_archivist_finds_real_variants_and_skips_noise(tmp_path):
    from src.subconscious.archivist import duplicate_suggestions, run
    found = {(s["target"], s["match"]) for s in duplicate_suggestions(_vault(tmp_path))}
    assert ("The Artistocrats", "The Aristocats") in found and ("ghibli", "Studio Ghibli") in found
    assert ("the Island / the island", None) in found
    assert not any(t in ("Rock", "the Man in Black", "Studio Gibli") for t, _ in found), found
    inbox = Inbox(tmp_path / "inbox.sqlite")
    backlog = tmp_path / "backlog.json"
    backlog.write_text(json.dumps({"questions": [
        {"id": "q1", "question": "Old?", "subject": "S", "from_article": "knowledge/research/S.md",
         "status": "open", "created_at": (NOW - timedelta(days=5)).isoformat()},
        {"id": "q2", "question": "New?", "subject": "S", "from_article": "knowledge/research/S.md",
         "status": "open", "created_at": NOW.isoformat()}]}))
    result = run(inbox, _vault(tmp_path / "again"), backlog, now=NOW)
    titles = [i["title"] for i in inbox.list(kind="unfinished_question")]
    assert titles == ["Old?"] and result["created"] >= 4


def test_dreamer_keeps_only_patterns_backed_by_real_messages(tmp_path, monkeypatch):
    from src.subconscious import dreamer
    messages = [
        {"id": "a", "session": "s1", "text": "my ESP32 boards are everywhere", "at": "t"},
        {"id": "b", "session": "s2", "text": "flashing another ESP32 tonight", "at": "t"},
        {"id": "c", "session": "s3", "text": "the ESP32 workshop is a mess", "at": "t"},
        {"id": "d", "session": "s1", "text": "pizza tonight?", "at": "t"},
    ]
    monkeypatch.setattr(dreamer, "recent_messages", lambda owner, now=None: messages)
    monkeypatch.setattr(dreamer, "_maybe_whisper", lambda kept, now: "skipped")

    async def ask(prompt, owner):
        assert "ESP32" in prompt
        return json.dumps({"patterns": [
            {"theme": "ESP32 projects", "observation": "Keeps tinkering.", "evidence": ["m1", "m2"],
             "curiosity": "I keep wondering what he is building.", "confidence": 0.8},
            {"theme": "Cooking", "observation": "Made up.", "evidence": ["m1", "m2"], "curiosity": "?", "confidence": 0.9}]})
    inbox = Inbox(tmp_path / "inbox.sqlite")
    result = asyncio.run(dreamer.run(inbox, owner="alice", ask=ask, now=NOW))
    assert [p["theme"] for p in result["patterns"]] == ["ESP32 projects"], "unsupported themes are dropped"
    item = inbox.list(kind="pattern")[0]
    assert len(item["evidence"]) == 2 and item["evidence"][0]["ref"].startswith("chat:")
    assert dreamer.TEST_SESSION.match("[eval] vault_known") and dreamer.TEST_SESSION.match("routing smoke: vault")
    assert not dreamer.TEST_SESSION.match("Hello Mabel")


def test_scout_briefs_only_pages_that_name_the_subject(tmp_path, monkeypatch):
    from src.curiosity.candidates import Candidate
    from src.subconscious import scout
    monkeypatch.setattr(scout, "candidates", lambda *a, **k: [Candidate("Madhouse", "graph", score=5,
                                                                        sources=["Library/Anime/Beck.md"])])
    monkeypatch.setattr("src.curiosity.backlog.researched_subjects", lambda folder: set())
    monkeypatch.setattr("src.search.searxng_search_results", lambda q, n: [
        {"url": "https://en.wikipedia.org/wiki/Madhouse_(company)"}, {"url": "https://fr.wikipedia.org/wiki/Madhouse"},
        {"url": "https://example.com/unrelated"}, {"url": "https://www.reddit.com/r/anime"}])
    pages = {"https://en.wikipedia.org/wiki/Madhouse_(company)": "Madhouse Inc. is a Japanese animation studio.",
             "https://example.com/unrelated": "A page about cooking."}
    fetched = []
    monkeypatch.setattr("src.search.fetch_webpage_content",
                        lambda url, timeout=12: fetched.append(url) or {"success": True, "content": pages.get(url, ""), "title": "t"})
    inbox = Inbox(tmp_path / "inbox.sqlite")
    paths = SimpleNamespace(index=None, articles=tmp_path)
    result = asyncio.run(scout.run(inbox, paths, now=NOW))
    assert result["briefs"] == [{"subject": "Madhouse", "sources": 1, "outcome": "created"}]
    assert "https://fr.wikipedia.org/wiki/Madhouse" not in fetched and not any("reddit" in u for u in fetched)
    brief = scout.brief_for(inbox, "Madhouse")
    assert [e["ref"] for e in brief["evidence"] if e["kind"] == "url"] == ["https://en.wikipedia.org/wiki/Madhouse_(company)"]
    assert brief["payload"]["query"] == "Madhouse anime"


def test_inbox_routes_admin_only(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import persona_routes
    from src.auth_helpers import require_user
    from src.subconscious import loops
    inbox = Inbox(tmp_path / "inbox.sqlite")
    item, _ = inbox.add(kind="pattern", loop="dreamer", title="ESP32", dedupe_key="p", evidence=EV)
    monkeypatch.setattr("src.subconscious.inbox.default_inbox", lambda: inbox)
    monkeypatch.setattr(persona_routes, "get_current_user", lambda request: "alice")
    admin = {"value": False}
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda user: admin["value"])
    monkeypatch.setattr(loops, "start", lambda names=loops.LOOPS: False)
    app = FastAPI()
    app.include_router(persona_routes.setup_persona_routes())
    app.dependency_overrides[require_user] = lambda: "alice"
    client = TestClient(app)
    assert [i["title"] for i in client.get("/api/persona/inbox").json()["items"]] == ["ESP32"]
    assert client.post(f"/api/persona/inbox/{item['id']}/decide", json={"status": "rejected"}).status_code == 403
    admin["value"] = True
    assert client.post(f"/api/persona/inbox/{item['id']}/decide", json={"status": "maybe"}).status_code == 422
    decided = client.post(f"/api/persona/inbox/{item['id']}/decide", json={"status": "rejected", "reason": "meh"}).json()
    assert decided["status"] == "rejected" and decided["decided_by"] == "user"
    assert client.post("/api/persona/inbox/run").status_code == 409


def test_stale_framing_keeps_only_real_quotes_backed_by_real_facts(tmp_path, monkeypatch):
    from src.subconscious import stale
    vault = tmp_path / "vault"
    folder = vault / "Projects" / "gitprojects" / "Mabel"
    folder.mkdir(parents=True)
    (folder / "roadmap.md").write_text(
        "# Roadmap\n\n## Phase 2\n- [ ] Add an optional Curiosity Whisper sourced from a real recent question.\n"
        "- [ ] Teach Mabel to juggle flaming torches while singing.\n"
        "The embedding server is the backbone of retrieval and always on.\n" + "Filler text here. " * 10 + "\n")
    monkeypatch.setattr(stale, "collect", lambda owner="tovakai": [
        {"id": "F1", "text": "Today is 2026-11-05."},
        {"id": "F2", "text": "The embedding server (127.0.0.1:8082) is not reachable."},
        {"id": "F3", "text": "Implemented (git 6836e039, 2026-11-05): feat(persona): Phase 2 mood rhythms and a seeded Curiosity Whisper"}])
    calls = []

    async def ask(prompt, owner):
        calls.append(prompt)
        return json.dumps({"stale": [
            {"quote": "Add an optional Curiosity Whisper sourced from a real recent question.", "facts": ["F3"],
             "why": "Built.", "suggestion": "Mark done.", "confidence": 0.9},
            {"quote": "The embedding server is the backbone of retrieval and always on.", "facts": ["F2"],
             "why": "It is down.", "suggestion": "Say it is optional.", "confidence": 0.8},
            {"quote": "Mabel juggles torches daily.", "facts": ["F3"], "why": "invented", "confidence": 0.9},
            {"quote": "Teach Mabel to juggle flaming torches while singing.", "facts": ["F99"], "why": "no fact", "confidence": 0.9},
            {"quote": "Teach Mabel to juggle flaming torches while singing.", "facts": ["F3"], "why": "unsure", "confidence": 0.3}]})
    inbox = Inbox(tmp_path / "inbox.sqlite")
    state = tmp_path / "seen.json"
    result = asyncio.run(stale.run(inbox, source_vault=vault, state_path=state, ask=ask, now=NOW))
    items = sorted(inbox.list(kind="stale_framing"), key=lambda i: i["title"])
    assert [i["title"] for i in items] == ["Add an optional Curiosity Whisper sourced from a real recent question.",
                                           "The embedding server is the backbone of retrieval and always on."]
    assert result["dropped"] == 3
    whisper = items[0]
    assert whisper["evidence"][0]["ref"] == "Projects/gitprojects/Mabel/roadmap.md#L4"
    assert whisper["evidence"][1]["ref"] == "fact:F3" and whisper["payload"]["suggestion"] == "Mark done."
    assert "[[" not in whisper["title"] and (vault / "Projects/gitprojects/Mabel/roadmap.md").read_text().count("- [ ]") == 2
    again = asyncio.run(stale.run(inbox, source_vault=vault, state_path=state, ask=ask, now=NOW))
    assert again["sections_checked"] == 0 and len(calls) == result["sections_checked"], "unchanged sections are skipped"


def test_stale_framing_rejects_facts_about_something_else():
    from src.subconscious.stale import relevant_facts, supports
    fact = "Implemented (git 6bbc9646, 2026-11-05): feat(curiosity): Mabel reviews Dottie's inbox when she chooses what to research"
    assert not supports("Check that the cited upstream code has not changed before porting behavior.", [fact])
    assert supports("Add an optional Curiosity Whisper sourced from a real question.",
                    ["Implemented (git 6836e039): feat(persona): Phase 2 mood rhythms and a seeded Curiosity Whisper"])
    facts = [{"id": "F1", "text": "Today is 2026-11-05."}, {"id": "F2", "text": fact},
             {"id": "F3", "text": "Implemented (git a): feat(persona): seeded Curiosity Whisper"}]
    assert [f["id"] for f in relevant_facts(facts, "- [ ] Add an optional Curiosity Whisper")] == ["F1", "F3"]


def test_stale_framing_drops_principles_and_shotgun_citations(tmp_path, monkeypatch):
    from src.subconscious import stale
    vault = tmp_path / "vault"
    folder = vault / "Projects" / "gitprojects" / "Mabel"
    folder.mkdir(parents=True)
    (folder / "phase.md").write_text("# Phase\nFuture prompt stitching must be additive, observable and removable.\n"
                                     "- [ ] Inspect the existing task subsystem before implementing a queue.\n"
                                     + "More filler words here. " * 8 + "\n")
    facts = [{"id": "F1", "text": "Today is 2026-11-05."},
             {"id": "F2", "text": "Prompt stitching (persona_mode) is on."}] + [
        {"id": f"F{i}", "text": f"Implemented (git x{i}): feat(task): existing subsystem queue work {i}"} for i in range(3, 9)]
    monkeypatch.setattr(stale, "collect", lambda owner="tovakai": facts)

    async def ask(prompt, owner):
        return json.dumps({"stale": [
            {"quote": "Future prompt stitching must be additive, observable and removable.", "facts": ["F2"], "confidence": 1},
            {"quote": "- [ ] Inspect the existing task subsystem before implementing a queue.",
             "facts": ["F3", "F4", "F5", "F6"], "confidence": 1}]})
    inbox = Inbox(tmp_path / "inbox.sqlite")
    result = asyncio.run(stale.run(inbox, source_vault=vault, state_path=tmp_path / "s.json", ask=ask, now=NOW))
    assert inbox.list(kind="stale_framing") == [] and result["dropped"] == 2


def test_dreamer_can_cite_the_life_log(tmp_path, monkeypatch):
    from src.subconscious import dreamer
    monkeypatch.setattr(dreamer, "recent_messages", lambda owner, now=None: [])
    monkeypatch.setattr(dreamer, "_maybe_whisper", lambda kept, now: "skipped")
    monkeypatch.setattr(dreamer, "life_items", lambda now: [
        {"kind": "game", "text": "Hades II", "minutes": 300, "count": 4, "ref": "lifelog:desktop:game:1"},
        {"kind": "browse", "text": "Hades II build guide - Reddit", "minutes": 0, "count": 3, "ref": "lifelog:laptop:browse:2"},
        {"kind": "media", "text": "Some Artist – Song", "minutes": 40, "count": 2, "ref": "lifelog:phone:media:3"}])

    async def ask(prompt, owner):
        assert "l1: [game] Hades II (300 min, 4x)" in prompt
        return json.dumps({"patterns": [
            {"theme": "Hades II", "observation": "He's deep into Hades II.", "evidence": ["l1", "l2"],
             "curiosity": "I keep wondering which build he runs.", "confidence": 0.8},
            {"theme": "Knitting", "observation": "Invented.", "evidence": ["l1", "l3"], "curiosity": "?", "confidence": 0.9}]})
    inbox = Inbox(tmp_path / "inbox.sqlite")
    result = asyncio.run(dreamer.run(inbox, owner="alice", ask=ask, now=NOW))
    assert [p["theme"] for p in result["patterns"]] == ["Hades II"]
    item = inbox.list(kind="pattern")[0]
    assert [e["ref"] for e in item["evidence"]] == ["lifelog:desktop:game:1", "lifelog:laptop:browse:2"]


def test_embed_loop_reconciles_then_embeds(monkeypatch):
    from types import SimpleNamespace
    from src.subconscious import loops
    calls = []

    class FakeIndex:
        def __init__(self, root, owner):
            calls.append(("index", str(root), owner))
        def needs_reconcile(self):
            return True
        def reconcile(self):
            calls.append(("reconcile",))
    async def fake_embed(index, *, owner, limit):
        calls.append(("embed", owner, limit))
        return {"generated": 3, "coverage": [{"embedded": 10, "total_chunks": 10}]}
    monkeypatch.setattr("src.knowledge_engine.index.VaultIndex", FakeIndex)
    monkeypatch.setattr("src.knowledge_engine.maintenance.embed_pending", fake_embed)
    result = asyncio.run(loops.embed_vault(SimpleNamespace(source_vault="/vault", owner="alice")))
    assert result["generated"] == 3 and [c[0] for c in calls] == ["index", "reconcile", "embed"]
    assert "embed" in loops.LOOPS and loops.AUTO_SCHEDULE["embed"] == 6 * 3600

"""Phase 3 manual Curiosity Drive: candidates, verification, backlog and article writing."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.curiosity import backlog as backlog_mod
from src.curiosity.candidates import audit, candidates, existing_titles, vault_context
from src.curiosity.verify import parse_draft, salvage_quotes, verify
from src.knowledge_engine.index import VaultIndex


def _vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Library" / "Anime").mkdir(parents=True)
    (vault / "Library" / "People (Private)").mkdir(parents=True)
    (vault / "Projects").mkdir()
    (vault / "Library" / "Anime" / "Beck.md").write_text(
        "---\nMy Rating: 9\nCreator: \"[[Madhouse]]\"\nGenre: \"[[Music]]\"\n---\n# Beck\n"
        "Koyuki joins a band after meeting a dog that changes everything for him, see [[Oceanic Flight 815]] lol.\n"
        "![[cover.png]]\n[[{{yesterday}}]]\n")
    (vault / "Library" / "Anime" / "Paprika.md").write_text(
        "---\nMy Rating: 6\nCreator: \"[[Madhouse]]\"\n---\n# Paprika\nA dream detective film. [[Studio Ghibli]]\n")
    (vault / "Library" / "Studio Ghibli.md").write_text("# Studio Ghibli\nMadhouse is a different studio entirely.\n")
    (vault / "Library" / "People (Private)" / "Friend.md").write_text("First met: [[Secret Bar Downtown]]\n")
    (vault / "Projects" / "Plan.md").write_text("- Link: [[My Side Project]]\n")
    index = VaultIndex(vault, owner="alice", cache_dir=tmp_path / "cache")
    index.reconcile()
    return vault, index.path


def test_audit_and_candidates_skip_noise_and_private_sources(tmp_path):
    _, db = _vault(tmp_path)
    counts = audit(db)
    assert counts["skipped"]["property_genre"] == 1 and counts["skipped"]["private_or_own_project_source"] == 2
    names = [c.name for c in candidates(db, surprises=0)]
    assert names[0] == "Madhouse"                                # two notes, one rated 9/10
    assert "Oceanic Flight 815" in names
    assert not {"Secret Bar Downtown", "My Side Project", "Music", "cover.png", "Studio Ghibli"} & set(names)
    assert "studio ghibli" in existing_titles(db)
    madhouse = next(c for c in candidates(db, surprises=0) if c.name == "Madhouse")
    assert any("rated it 9/10" in r for r in madhouse.reasons) and not madhouse.prose
    assert next(c for c in candidates(db, surprises=0) if c.name == "Oceanic Flight 815").prose


def test_researched_subjects_are_excluded_and_vault_context_skips_private(tmp_path):
    _, db = _vault(tmp_path)
    assert "Madhouse" not in [c.name for c in candidates(db, exclude={"madhouse"}, surprises=0)]
    hits = vault_context(db, "Madhouse")
    assert hits and all("Private" not in h["path"] for h in hits)


DRAFT = """<think>hmm</think>
# Kondo's debut

Summary.

## What I found
Kondo directed Whisper of the Heart in 1995 [S1]. He died in 1998 of an aortic dissection [S2].

## What I make of it
I think Ghibli lost its heir.

## Open questions
- Did Kondo have Madhouse credits?
- Who else was considered?

## Links
- [[Whisper of the Heart]]
- [[Hiroyuki Morita]]

```json
{"quotes": {"S1": ["In 1995, he made his directorial debut with Whisper of the Heart"], "S2": ["died of an aneurysm"]}, "whisper": "Kondo stuck with me."}
```"""
SOURCES = {"S1": "In 1995, he made his directorial debut with “Whisper of the Heart”.[3]",
           "S2": "Kondo died in 1998 of an aortic dissection."}


def test_verification_marks_unsupported_citations():
    draft = parse_draft(DRAFT)
    assert draft.title == "Kondo's debut" and draft.links == ["Whisper of the Heart", "Hiroyuki Morita"]
    assert draft.questions == ["Did Kondo have Madhouse credits?", "Who else was considered?"]
    check = verify(draft, SOURCES)
    assert check["verified"] == ["S1"] and check["unverified"] == ["S2"]   # S2's quote is not in the source
    assert check["status"] == "needs_review" and "[S2?]" in draft.body and "[S1]" in draft.body


def test_truncated_quote_block_is_salvaged():
    cut = DRAFT.replace('"S2": ["died of an aneurysm"]}, "whisper": "Kondo stuck with me."}\n```',
                        '"S2": ["Kondo died in 1998 of an aortic dis')
    draft = parse_draft(cut)
    assert "quote_block_truncated" in draft.problems and "```" not in draft.body
    assert draft.quotes == {"S1": ["In 1995, he made his directorial debut with Whisper of the Heart"]}
    assert salvage_quotes('{"quotes": {"S3": ["a", "b"], "S4": ["unfinished') == {"S3": ["a", "b"]}


def test_backlog_cools_off_and_dedupes(tmp_path):
    log = backlog_mod.Backlog(tmp_path / "backlog.json")
    now = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)
    log.add_from_article(article="knowledge/research/A.md", subject="A", questions=["Why?"], links=["B"], now=now)
    again = log.add_from_article(article="knowledge/research/A.md", subject="A", questions=["why?"], links=["b"], now=now)
    assert again == {"questions": 0, "spawned": 0}
    assert log.eligible_questions(now + timedelta(hours=1)) == []           # cooling off
    assert [q["question"] for q in log.eligible_questions(now + timedelta(days=1))] == ["Why?"]


def test_session_writes_only_into_mabels_vault(tmp_path, monkeypatch):
    import asyncio
    from src.curiosity import session as session_mod
    vault, db = _vault(tmp_path)
    mabel = tmp_path / "mabel"
    paths = session_mod.Paths(mabel_vault=mabel, data_dir=tmp_path / "data", source_vault=vault, owner="alice")
    paths.index = db
    replies = iter([json.dumps({"pick": 1, "subject": "Madhouse", "question": "Why does Madhouse matter?",
                                "why": "Beck.", "search_queries": ["Madhouse studio"]}),
                    DRAFT.replace("Kondo", "Madhouse"), json.dumps({"quotes": {"S2": ["Madhouse died in 1998 of an aortic dissection."]}})])

    async def fake_ask(messages, *, max_tokens, owner):
        return next(replies)
    monkeypatch.setattr(session_mod, "_ask", fake_ask)
    monkeypatch.setattr(session_mod, "_core_identity", lambda: "You are Mabel.")
    monkeypatch.setattr("src.search.searxng_search_results",
                        lambda q, n: [{"url": "https://en.wikipedia.org/wiki/Madhouse", "title": "Madhouse"},
                                      {"url": "https://grokipedia.com/page/Madhouse", "title": "AI"},
                                      {"url": "https://anime.fandom.com/wiki/Madhouse", "title": "Fandom"}])
    page = ("Madhouse is a studio. " * 30 + SOURCES["S1"].replace("Kondo", "Madhouse") + " "
            + SOURCES["S2"].replace("Kondo", "Madhouse"))
    fetched = []
    monkeypatch.setattr("src.search.fetch_webpage_content",
                        lambda url, timeout=12: fetched.append(url) or {"success": True, "title": "Madhouse - Wikipedia", "content": page})
    before = {p: p.read_text() for p in vault.rglob("*.md")}
    session = session_mod.Session(paths)
    result = asyncio.run(session.run(seed=1))
    assert sorted(fetched) == ["https://anime.fandom.com/wiki/Madhouse", "https://en.wikipedia.org/wiki/Madhouse"], \
        "AI-generated encyclopedias are skipped"
    assert result["status"] == "verified", result
    assert result["verified"] == ["S1", "S2"]
    article = mabel / result["article"]
    text = article.read_text()
    assert text.startswith("---\ntype: research-article") and "## Sources" in text
    assert {p: p.read_text() for p in vault.rglob("*.md")} == before, "the user's vault is never written"
    assert (mabel / "journal" / "curiosity").is_dir()
    log = json.loads((tmp_path / "data" / "curiosity" / "backlog.json").read_text())
    assert [s["name"] for s in log["spawned"]] == ["Whisper of the Heart", "Hiroyuki Morita"]
    assert "madhouse" in backlog_mod.researched_subjects(mabel / "knowledge" / "research")
    whisper = json.loads((tmp_path / "data" / "persona" / "whispers.json").read_text())["active"]
    assert result["whisper_outcome"] == "set"
    assert (whisper["origin"], whisper["source"], whisper["thought"]) == ("self_generated", "research", "Madhouse stuck with me.")
    assert whisper["reference"] == result["article"]

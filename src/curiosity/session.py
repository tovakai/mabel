"""Phase 3: one manual Curiosity Drive session.

Steps, each checkpointed to ``data/curiosity/sessions/<id>.json`` so a
session can resume after a failure:

1. candidates - graph-relevant unresolved links, a couple of surprises, her
   cooled-off open questions and links from earlier articles;
2. choice     - Mabel (the foreground model, with her CORE identity) picks one
   or asks an entirely new question, and says why; recorded as it happens;
3. evidence   - vault context first (lexical), then a few web searches and
   page fetches within the session budget;
4. draft      - Mabel writes the article in her voice: cited facts, her own
   take (inference, marked as such), open questions, a few new links;
5. write      - citations verified against the fetched text, then the article
   goes into Mabel's vault (``knowledge/research/``), open questions and links
   into the backlog (cooling off), and a journal entry.

Nothing is written to the user's vault. Her earlier articles are context
only, never evidence for new claims.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

from . import backlog as backlog_mod
from .candidates import Candidate, candidates, existing_titles, index_db, vault_context
from .verify import parse_draft, verify

logger = logging.getLogger(__name__)

WEB_SEARCH_BUDGET = 15       # per session (roadmap initial budget)
QUERIES_PER_ARTICLE = 3
MAX_SOURCES = 5
SOURCE_CHARS = 6000
NEW_CANDIDATES_CAP = 10
_REPO = Path(__file__).resolve().parents[2]
_SKIP_DOMAINS = ("youtube.com", "youtu.be", "reddit.com", "pinterest.", "facebook.com", "x.com", "twitter.com",
                 "tiktok.com", "quora.com", "instagram.com", "amazon.", "ebay.",
                 "grokipedia.com")   # AI-generated encyclopedias are not sources
_PREFER = (("wikipedia.org", 3.0), ("fandom.com", 2.0), ("wiki", 1.5), (".jp", 0.5))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


class Paths:
    def __init__(self, *, mabel_vault: Path | None = None, data_dir: Path | None = None,
                 source_vault: Path | None = None, owner: str | None = None):
        from src.constants import DATA_DIR
        self.mabel_vault = Path(mabel_vault or _REPO)
        self.articles = self.mabel_vault / "knowledge" / "research"
        self.journal = self.mabel_vault / "journal" / "curiosity"
        self.data = Path(data_dir or DATA_DIR) / "curiosity"
        if source_vault is None:
            target = json.loads((Path(DATA_DIR) / "vault_target.json").read_text())
            source_vault = Path(target["path"]).expanduser()
        self.source_vault = Path(source_vault)
        self.owner = owner if owner is not None else str(_setting("curiosity_owner", "tovakai"))
        self.index = index_db(self.source_vault, self.owner)


def _dottie_patterns(limit: int = 4) -> list[dict]:
    """Dottie's newest conversation patterns, for Mabel to consider (and keep or reject)."""
    try:
        from src.subconscious.inbox import default_inbox
        return default_inbox().list(status="new", kind="pattern", limit=limit)
    except Exception:
        return []


def _brief(subject: str) -> dict | None:
    """Dottie's research brief for ``subject``, if one is waiting (Phase 4 Scout)."""
    if not subject:
        return None
    try:
        from src.subconscious.inbox import default_inbox
        from src.subconscious.scout import brief_for
        return brief_for(default_inbox(), subject)
    except Exception:
        return None


def _json_object(text: str) -> dict:
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    value = json.loads(text[start:i + 1])
                    if isinstance(value, dict):
                        return value
                except ValueError:
                    break
                break
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in model reply")


def _core_identity() -> str:
    from src.persona import composer
    library = composer().modules.load()
    return library.core.body if library.core else "You are Mabel."


async def _ask(messages: list[dict], *, max_tokens: int, owner: str) -> str:
    from src import model_roles
    return await model_roles.call("default", messages, owner=owner, max_tokens=max_tokens,
                                  temperature=0.7, timeout=300)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem[:20]}-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", title).strip(" .")
    return " ".join(name.split())[:100] or "Untitled research"


class Session:
    def __init__(self, paths: Paths, session_id: str | None = None):
        self.paths = paths
        self.id = session_id or f"cs-{_now():%Y%m%d-%H%M}-{secrets.token_hex(2)}"
        self.file = paths.data / "sessions" / f"{self.id}.json"
        self.backlog = backlog_mod.Backlog(paths.data / "backlog.json")
        try:
            self.state = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.state = {"id": self.id, "started_at": _now().isoformat(), "steps": {}, "searches": 0}

    def _checkpoint(self, step: str, value) -> None:
        self.state["steps"][step] = value
        self.state["updated_at"] = _now().isoformat()
        _atomic_write(self.file, json.dumps(self.state, indent=1, ensure_ascii=False, default=str))

    def done(self, step: str):
        return self.state["steps"].get(step)

    # 1 ------------------------------------------------------------------------------------
    def build_candidates(self, *, seed: int | None = None) -> list[dict]:
        if self.done("candidates"):
            return self.done("candidates")
        researched = backlog_mod.researched_subjects(self.paths.articles)
        pool = candidates(self.paths.index, exclude=researched, seed=seed)
        for cand in pool:
            brief = _brief(cand.name)
            if brief:
                urls = [e for e in brief["evidence"] if e.get("kind") == "url"]
                cand.reasons.append(f"Dottie already gathered {len(urls)} sources for it")
        for item in self.backlog.eligible_questions()[:3]:
            pool.append(Candidate(item["subject"], "open_question", question=item["question"], ref=item["id"],
                                  reasons=[f"your open question from {Path(item['from_article']).stem}"]))
        for item in self.backlog.eligible_spawned()[:2]:
            if item["name"].casefold() not in researched:
                pool.append(Candidate(item["name"], "spawned", ref=item["id"],
                                      reasons=[f"a link you left in {Path(item['from_article']).stem}"]))
        for item in _dottie_patterns():
            pool.append(Candidate(item["title"], "dottie", question=item["payload"].get("curiosity") or None,
                                  ref=item["id"], reasons=[f"Dottie noticed: {item['summary']}"[:200]]))
        listed = [c.as_dict() for c in pool]
        self._checkpoint("candidates", listed)
        return listed

    # 2 ------------------------------------------------------------------------------------
    async def choose(self, *, forced: int | None = None) -> dict:
        if self.done("choice"):
            return self.done("choice")
        pool = self.done("candidates") or self.build_candidates()
        lines = []
        for i, c in enumerate(pool, 1):
            what = f'question: "{c["question"]}" (about {c["name"]})' if c.get("question") else c["name"]
            why = "; ".join(c.get("reasons") or []) or "linked in the notes"
            seen = f' e.g. "{c["contexts"][0][:140]}"' if c.get("contexts") else ""
            lines.append(f"{i}. {what} [{c['kind']}] - {why}.{seen}")
        researched = sorted(backlog_mod.researched_subjects(self.paths.articles))
        prompt = (
            "You have some free time to research something purely because you want to. Below are things "
            "Anthon's notes link to but nobody has written up yet, a couple of random picks, and any open "
            "questions you left yourself last time. Items marked [dottie] are things Dottie noticed Anthon keeps "
            "talking about.\n\n" + "\n".join(lines) + "\n\n"
            + (f"Already written up (don't pick these): {', '.join(researched)}\n\n" if researched else "")
            + "Pick the one you're genuinely most curious about, or ignore the list and ask an entirely new "
            "question. Turn it into a real question (not just a topic). Be honest about why.\n"
            'Reply with only JSON: {"pick": <list number, or 0 for your own>, "subject": "...", '
            '"question": "...", "why": "one or two sentences", "search_queries": ["up to 3 web searches"]'
            + (', "dottie": [{"item": <list number>, "decision": "keep" or "reject", "reason": "short and honest"}]'
               if any(c["kind"] == "dottie" for c in pool) else "") + "}"
            + ("\nFor every [dottie] item you don't pick, decide: keep it for another day, or reject it "
               "(not interesting to you, not researchable, or not your business), and say why."
               if any(c["kind"] == "dottie" for c in pool) else ""))
        if forced is not None:
            prompt += f"\n\n(Operator note: this run is restricted to item {forced}.)"
        reply = await _ask([{"role": "system", "content": _core_identity()}, {"role": "user", "content": prompt}],
                           max_tokens=3000, owner=self.paths.owner)
        choice = _json_object(reply)
        pick = int(forced if forced is not None else choice.get("pick") or 0)
        picked = pool[pick - 1] if 1 <= pick <= len(pool) else None
        subject = " ".join(str(choice.get("subject") or (picked or {}).get("name") or "").split())[:120]
        question = " ".join(str(choice.get("question") or "").split())[:300]
        if not subject or not question:
            raise ValueError("Mabel's choice had no subject or question")
        queries = [" ".join(str(q).split())[:160] for q in (choice.get("search_queries") or []) if str(q).strip()]
        record = {"pick": pick, "kind": picked["kind"] if picked else "own_question", "subject": subject,
                  "question": question, "why": " ".join(str(choice.get("why") or "").split())[:500],
                  "search_queries": (queries or [subject])[:QUERIES_PER_ARTICLE],
                  "candidate": picked, "chosen_at": _now().isoformat()}
        record["dottie_decisions"] = self._review_dottie(pool, picked, choice.get("dottie"))
        self._checkpoint("choice", record)
        self._ledger(record)
        if picked and picked.get("ref") and picked["kind"] != "dottie":
            self.backlog.mark(picked["ref"], "picked")
        return record

    def _review_dottie(self, pool: list[dict], picked: dict | None, decisions) -> list[dict]:
        """Apply Mabel's verdicts on Dottie's suggestions: picked -> accepted, reject -> rejected (with her reason)."""
        try:
            from src.subconscious.inbox import default_inbox
            inbox = default_inbox()
        except Exception:
            return []
        out = []
        if picked and picked["kind"] == "dottie":
            inbox.decide(picked["ref"], "accepted", by="mabel", reason=f"picked for curiosity session {self.id}")
            out.append({"item": picked["ref"], "title": picked["name"], "decision": "picked"})
        if picked and picked["kind"] == "open_question" and picked.get("ref"):
            linked = inbox.find(f"question:{picked['ref']}")
            if linked and linked["status"] == "new":
                inbox.decide(linked["id"], "done", by="mabel", reason=f"picked for curiosity session {self.id}")
        for verdict in decisions if isinstance(decisions, list) else []:
            if not isinstance(verdict, dict):
                continue
            try:
                index = int(verdict.get("item"))
            except (TypeError, ValueError):
                continue
            cand = pool[index - 1] if 1 <= index <= len(pool) else None
            if not cand or cand["kind"] != "dottie" or cand is picked:
                continue
            reason = " ".join(str(verdict.get("reason") or "").split())[:300]
            decision = str(verdict.get("decision") or "").lower()
            if decision == "reject":
                try:
                    inbox.decide(cand["ref"], "rejected", by="mabel", reason=reason or "not for me")
                except Exception:
                    continue
            elif decision != "keep":
                continue
            out.append({"item": cand["ref"], "title": cand["name"], "decision": decision, "reason": reason})
        return out

    def _ledger(self, record: dict) -> None:
        try:
            from src.agent_runtime.event_ledger import Event, Ledger
            from src.constants import DATA_DIR
            Ledger(Path(DATA_DIR) / "phase0" / "events.jsonl").append(Event.create(
                "mabel", "curiosity.candidate_selected", references=(f"curiosity:{self.id}",),
                decision=record["kind"], rationale_code="self_selected"))
        except Exception:
            logger.debug("[curiosity] ledger event not written", exc_info=True)

    # 3 ------------------------------------------------------------------------------------
    async def gather(self) -> dict:
        if (self.done("evidence") or {}).get("sources"):
            return self.done("evidence")   # an empty attempt is retried on resume
        choice = self.done("choice")
        subject = choice["subject"]
        candidate = choice.get("candidate") or {}
        vault = vault_context(self.paths.index, candidate.get("name") or subject) or vault_context(self.paths.index, subject)
        previous = [{"path": str(a["path"].name), "subject": a.get("subject"), "question": a.get("question")}
                    for a in backlog_mod.articles(self.paths.articles)][:10]
        results = []
        loop = asyncio.get_running_loop()
        from src.search import fetch_webpage_content, searxng_search_results
        brief = _brief((candidate.get("name") or subject))
        if brief:
            # Dottie's Scout already found pages that name the subject: start from them
            # (re-fetched and verified below like any source) and search less.
            results.extend({"query": "dottie brief", "url": e["ref"], "title": e.get("title", "")}
                           for e in brief["evidence"] if e.get("kind") == "url")
        queries = choice["search_queries"][:1] if brief else choice["search_queries"]
        for query in queries:
            if self.state["searches"] >= WEB_SEARCH_BUDGET:
                break
            self.state["searches"] += 1
            try:
                found = await asyncio.wait_for(loop.run_in_executor(None, searxng_search_results, query, 8), 40)
            except Exception as error:
                logger.warning("[curiosity] search failed for %r: %s", query, error)
                continue
            results.extend({"query": query, **r} for r in found if isinstance(r, dict) and r.get("url"))
        picked, domains = [], set()
        for r in sorted(results, key=lambda r: (r["query"] != "dottie brief", -self._url_score(r["url"]))):
            domain = urlparse(r["url"]).netloc.lower().removeprefix("www.").removeprefix("m.")
            if domain.endswith(".wikipedia.org"):
                if not domain.startswith("en."):
                    continue   # other language editions repeat the English article (and aren't English)
                domain = "wikipedia.org"
            if any(s in domain for s in _SKIP_DOMAINS) or r["url"].lower().endswith(".pdf") or domain in domains:
                continue
            domains.add(domain)
            picked.append(r)
            if len(picked) >= MAX_SOURCES + 2:
                break
        with ThreadPoolExecutor(max_workers=4) as pool:
            pages = await asyncio.gather(*(loop.run_in_executor(pool, lambda u=r["url"]: fetch_webpage_content(u, timeout=12))
                                           for r in picked), return_exceptions=True)
        sources = []
        for r, page in zip(picked, pages):
            if isinstance(page, Exception) or not isinstance(page, dict) or not page.get("success"):
                continue
            text = focused(str(page.get("content") or ""), candidate.get("name") or subject, choice["question"])
            if len(text) < 400:
                continue
            sources.append({"id": f"S{len(sources) + 1}", "url": r["url"],
                            "title": str(page.get("title") or r.get("title") or r["url"])[:200], "text": text})
            if len(sources) >= MAX_SOURCES:
                break
        if brief:
            try:
                from src.subconscious.inbox import default_inbox
                default_inbox().decide(brief["id"], "accepted", by="mabel", reason=f"used in curiosity session {self.id}")
            except Exception:
                logger.debug("[curiosity] brief not marked", exc_info=True)
        evidence = {"vault": vault, "link_contexts": candidate.get("contexts") or [], "brief": brief["id"] if brief else None,
                    "link_sources": [p for p in candidate.get("sources") or []][:6],
                    "previous_articles": previous, "sources": sources, "searches": self.state["searches"]}
        self._checkpoint("evidence", evidence)
        return evidence

    @staticmethod
    def _url_score(url: str) -> float:
        host = urlparse(url).netloc.lower()
        return max((w for s, w in _PREFER if s in host), default=0.0)

    # 4 ------------------------------------------------------------------------------------
    async def write_draft(self) -> str:
        if self.done("draft"):
            return self.done("draft")
        choice, evidence = self.done("choice"), self.done("evidence")
        if not evidence["sources"]:
            raise RuntimeError("no usable web sources were fetched; try again or pick another subject")
        vault_lines = [f"- {v['path']} ({v['heading'] or 'note'}): {v['text']}" for v in evidence["vault"]]
        vault_lines += [f"- link context: {c}" for c in evidence["link_contexts"]]
        previous = [f"- {p['subject']}: {p['question']}" for p in evidence["previous_articles"]]
        sources = "\n\n".join(f"[{s['id']}] {s['title']} <{s['url']}>\n{s['text']}" for s in evidence["sources"])
        prompt = f"""You chose to research: {choice['subject']}
Your question: {choice['question']}
Why you picked it: {choice['why']}

From Anthon's notes (context for why it matters to him; not web evidence):
{chr(10).join(vault_lines) or '- (nothing beyond the link itself)'}

Your own earlier research articles (context only; never cite them as evidence):
{chr(10).join(previous) or '- (none yet)'}

Web sources:
{sources}

Write a research article for your own vault, in your own voice, in English, using exactly this structure:

# <a title>
<a short summary paragraph>

## What I found
Factual paragraphs answering your question. End every factual sentence with its source, like [S1] or [S1][S3]. Only state what the sources support. If the sources don't answer something, say so.

## What I make of it
Your own interpretation, opinions and connections (including to Anthon's notes). This is inference; no citations needed, but don't invent facts here.

## Open questions
2-3 real questions you'd want to dig into another day, as a bullet list.

## Links
Up to 4 [[wikilinks]] to people, works or ideas from this research that deserve their own note later, as a bullet list.

After the article, add one fenced json block:
```json
{{"quotes": {{"S1": ["short exact quote copied word for word from S1 that supports your claims"], "S2": ["..."]}}, "whisper": "one first-person sentence about what stuck with you"}}
```
Give at least one quote for every source you cite; quotes must be copied exactly (under 30 words each)."""
        reply = await _ask([{"role": "system", "content": _core_identity()}, {"role": "user", "content": prompt}],
                           max_tokens=10000, owner=self.paths.owner)
        self._checkpoint("draft", reply)
        return reply

    async def collect_quotes(self) -> dict:
        """Ask once more for exact quotes for cited sources whose quotes are missing or don't match."""
        if self.done("quotes") is not None:
            return self.done("quotes")
        from .verify import _CITE, norm
        evidence = self.done("evidence")
        draft = parse_draft(self.done("draft"))
        texts = {s["id"]: s for s in evidence["sources"]}
        missing = [sid for sid in sorted(set(_CITE.findall(draft.found)), key=lambda x: int(x[1:]))
                   if sid in texts and not any(norm(q) and norm(q) in norm(texts[sid]["text"])
                                               for q in draft.quotes.get(sid, []))]
        extra: dict = {}
        if missing:
            claims = {sid: [s for s in re.split(r"(?<=[.!?])\s+", draft.found) if f"[{sid}]" in s][:6] for sid in missing}
            blocks = "\n\n".join(f"[{sid}] {texts[sid]['title']}\nYour sentences citing it:\n- "
                                   + "\n- ".join(claims[sid]) + f"\n\nSource text:\n{texts[sid]['text']}"
                                   for sid in missing)
            prompt = ("For each source below, copy one to three short passages, word for word from the source text, "
                      "that support the sentences citing it. Copy exactly; do not paraphrase. If nothing in the source "
                      "supports them, give an empty list.\n\n" + blocks
                      + '\n\nReply with only JSON: {"quotes": {"S1": ["exact passage", "..."]}}')
            try:
                reply = await _ask([{"role": "user", "content": prompt}], max_tokens=4000, owner=self.paths.owner)
                extra = {k.upper(): v for k, v in (_json_object(reply).get("quotes") or {}).items()
                         if k.upper() in missing and isinstance(v, list)}
            except Exception:
                logger.warning("[curiosity] quote follow-up failed", exc_info=True)
        self._checkpoint("quotes", extra)
        return extra

    # 5 ------------------------------------------------------------------------------------
    def write_article(self) -> dict:
        if self.done("write"):
            return self.done("write")
        choice, evidence = self.done("choice"), self.done("evidence")
        draft = parse_draft(self.done("draft"))
        for sid, quotes in (self.done("quotes") or {}).items():
            draft.quotes[sid] = [str(q) for q in quotes][:4] + draft.quotes.get(sid, [])
        sources = {s["id"]: s["text"] for s in evidence["sources"]}
        check = verify(draft, sources)
        check["problems"] = [p for p in check["problems"]
                             if p not in ("no_quote_block", "quote_block_truncated") or check["unverified"]]
        check["status"] = "verified" if not check["problems"] else "needs_review"
        title = draft.title or choice["subject"]
        path = self.paths.articles / f"{safe_filename(title)}.md"
        if path.exists():
            path = path.with_name(f"{path.stem} ({self.id}).md")
        source_meta = [{"id": s["id"], "title": s["title"], "url": s["url"], "verified": s["id"] in check["verified"]}
                       for s in evidence["sources"]]
        meta = {
            "type": "research-article", "subject": choice["subject"], "aliases": [title] if title != choice["subject"] else [],
            "question": choice["question"], "why": choice["why"], "chosen_from": choice["kind"],
            "session": self.id, "created": _now().isoformat(timespec="seconds"), "status": check["status"],
            "verification": {k: check[k] for k in ("cited", "verified", "unverified", "uncited_sentences", "problems")},
            "sources": source_meta, "vault_context": [v["path"] for v in evidence["vault"]],
            "open_questions": draft.questions, "new_links": draft.links,
        }
        listed = []
        for s in evidence["sources"]:
            mark = "verified" if s["id"] in check["verified"] else (
                "cited, not verified" if s["id"] in check["cited"] else "not cited")
            quote = check["quotes"].get(s["id"], [""])[0]
            listed.append(f"- **[{s['id']}]** [{s['title']}]({s['url']}) ({mark})" + (f': "{quote}"' if quote else ""))
        note = ("" if check["status"] == "verified" else
                "> [!warning] Needs review\n> " + ", ".join(check["problems"]).replace("_", " ") + "\n\n")
        text = ("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000) + "---\n"
                + note + draft.body.strip() + "\n\n## Sources\n" + "\n".join(listed) + "\n")
        _atomic_write(path, text)
        rel = str(path.relative_to(self.paths.mabel_vault))
        known = existing_titles(self.paths.index) | backlog_mod.researched_subjects(self.paths.articles)
        known |= {choice["subject"].casefold(), str((choice.get("candidate") or {}).get("name") or "").casefold()}
        fresh = [link for link in draft.links if link.casefold() not in known][:NEW_CANDIDATES_CAP]
        added = self.backlog.add_from_article(article=rel, subject=choice["subject"], questions=draft.questions,
                                              links=fresh)
        if (choice.get("candidate") or {}).get("ref") and choice["candidate"]["kind"] != "dottie":
            self.backlog.mark(choice["candidate"]["ref"], "done")
        whisper_outcome = self._propose_whisper(choice, draft.whisper, rel, check["status"])
        self._journal(choice, rel, check, draft)
        result = {"article": rel, "status": check["status"], "problems": check["problems"],
                  "verified": check["verified"], "cited": check["cited"], "questions": draft.questions,
                  "links": draft.links, "whisper": draft.whisper, "whisper_outcome": whisper_outcome,
                  "backlog_added": added}
        self._checkpoint("write", result)
        self.backlog.record_session({"id": self.id, "started_at": self.state["started_at"], "subject": choice["subject"],
                                     "article": rel, "status": check["status"]})
        return result

    def _propose_whisper(self, choice: dict, thought: str, article: str, status: str) -> str:
        """Her "what stuck with me" line becomes her whisper (research source), unless the user seeded one."""
        if not thought:
            return "no_thought"
        if status != "verified":
            return "article_needs_review"
        try:
            from src.persona.whisper import WhisperStore
            store = WhisperStore(self.paths.data.parent / "persona" / "whispers.json")
            _whisper, reason = store.propose("research", choice["subject"][:80], thought, now=_now(),
                                             reference=article)
            return reason
        except Exception as error:
            logger.warning("[curiosity] whisper proposal failed: %s", error)
            return "failed"

    def _journal(self, choice: dict, article: str, check: dict, draft) -> None:
        day = _now().astimezone()
        path = self.paths.journal / f"{day:%Y-%m-%d}.md"
        entry = (f"\n## {day:%H:%M} Curiosity session {self.id}\n"
                 f"- **Chose:** {choice['subject']} ({choice['kind'].replace('_', ' ')})\n"
                 f"- **Question:** {choice['question']}\n- **Why:** {choice['why']}\n"
                 f"- **Wrote:** [[{Path(article).stem}]] ({check['status']}; "
                 f"{len(check['verified'])}/{len(check['cited'])} cited sources verified)\n"
                 + "".join(f"- **Left open:** {q}\n" for q in draft.questions))
        existing = path.read_text(encoding="utf-8") if path.exists() else f"# Curiosity journal {day:%Y-%m-%d}\n"
        _atomic_write(path, existing + entry)

    async def run(self, *, forced: int | None = None, seed: int | None = None) -> dict:
        self.build_candidates(seed=seed)
        await self.choose(forced=forced)
        await self.gather()
        await self.write_draft()
        await self.collect_quotes()
        return self.write_article()


def _fold(text: str) -> str:
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(ch))


def focused(text: str, subject: str, question: str, limit: int = SOURCE_CHARS) -> str:
    """Passages of ``text`` about the subject/question, in page order, within ``limit`` chars."""
    text = re.sub(r"[ \t]+", " ", text)
    parts = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if len(block) <= 900:
            parts.append(block)
            continue
        # Long blocks (pages without paragraph breaks): regroup sentences into ~600-char passages.
        current = ""
        for sentence in re.split(r"(?<=[.!?])\s+", block):
            if current and len(current) + len(sentence) > 600:
                parts.append(current)
                current = ""
            current = f"{current} {sentence}".strip()
        if current:
            parts.append(current)
    parts = [p for p in parts if len(p) > 40]
    terms = {_fold(w) for w in re.findall(r"\w{4,}", f"{subject} {question}")}
    terms -= {"what", "with", "that", "this", "does", "from", "have", "about", "their", "they", "were", "which",
              "there", "when", "into", "between", "behind", "going"}
    keep = []
    for i, part in enumerate(parts):
        if any(t in _fold(part) for t in terms):
            for j in (i, i + 1):
                if j < len(parts) and j not in keep:
                    keep.append(j)
    out, size = [], 0
    for j in sorted(keep or range(len(parts))):
        piece = parts[j][:limit - size] if not out else parts[j]
        if size + len(piece) > limit:
            continue
        out.append(piece)
        size += len(piece) + 2
    return "\n\n".join(out)

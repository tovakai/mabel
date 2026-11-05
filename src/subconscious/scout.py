"""Phase 4 Scout: gather sources ahead of time for Mabel's likely research picks.

No model calls: one web search per candidate, then a fetch of the best few
pages to confirm they really talk about the subject ("source_checked"), saved
as a ``research_brief`` with short excerpts. A curiosity session that picks a
briefed subject starts from these sources instead of searching blind. Briefs
are leads, not evidence: Mabel's session re-fetches and verifies every quote.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from src.curiosity.candidates import candidates
from src.curiosity.session import _SKIP_DOMAINS, Session, _fold, focused

SEARCHES_PER_RUN = 3
SOURCES_PER_BRIEF = 4
EXCERPT_CHARS = 600
_HINTS = (("anime", "anime"), ("games", "video game"), ("movies", "film"), ("music", "music"),
          ("comics", "manga"), ("manga", "manga"), ("fictional characters/lost", "Lost TV series"),
          ("books", "book"))


def hint_for(sources: list[str]) -> str:
    for path in sources:
        folded = path.casefold()
        for needle, hint in _HINTS:
            if f"/{needle}" in f"/{folded}":
                return hint
    return ""


def brief_key(subject: str) -> str:
    return "brief:" + re.sub(r"[^a-z0-9]+", "-", _fold(subject)).strip("-")


async def run(inbox, paths, *, searches: int = SEARCHES_PER_RUN, seed: int | None = None,
              now: datetime | None = None) -> dict:
    from src.curiosity.backlog import researched_subjects
    from src.search import fetch_webpage_content, searxng_search_results
    now = now or datetime.now(timezone.utc)
    pool = candidates(paths.index, exclude=researched_subjects(paths.articles), surprises=0, seed=seed)
    loop = asyncio.get_running_loop()
    done, briefs = 0, []
    for cand in pool:
        if done >= searches:
            break
        existing = inbox.find(brief_key(cand.name))
        if existing and existing["status"] in ("new", "accepted", "rejected", "done"):
            continue
        hint = hint_for(cand.sources)
        query = f"{cand.name} {hint}".strip()
        done += 1
        try:
            results = await asyncio.wait_for(loop.run_in_executor(None, searxng_search_results, query, 8), 40)
        except Exception:
            continue
        picked, domains = [], set()
        for r in sorted((r for r in results if isinstance(r, dict) and r.get("url")),
                        key=lambda r: -Session._url_score(r["url"])):
            domain = urlparse(r["url"]).netloc.lower().removeprefix("www.").removeprefix("m.")
            if domain.endswith(".wikipedia.org"):
                if not domain.startswith("en."):
                    continue
                domain = "wikipedia.org"
            if any(s in domain for s in _SKIP_DOMAINS) or domain in domains or r["url"].lower().endswith(".pdf"):
                continue
            domains.add(domain)
            picked.append(r)
            if len(picked) >= SOURCES_PER_BRIEF + 1:
                break
        pages = await asyncio.gather(*(loop.run_in_executor(None, lambda u=r["url"]: fetch_webpage_content(u, timeout=12))
                                       for r in picked), return_exceptions=True)
        sources = []
        name = _fold(cand.name)
        for r, page in zip(picked, pages):
            if isinstance(page, Exception) or not isinstance(page, dict) or not page.get("success"):
                continue
            text = str(page.get("content") or "")
            if name not in _fold(text):
                continue                      # the page never names the subject: not a lead
            sources.append({"ref": r["url"], "kind": "url", "title": str(page.get("title") or r.get("title") or "")[:200],
                            "excerpt": focused(text, cand.name, hint, limit=EXCERPT_CHARS)})
            if len(sources) >= SOURCES_PER_BRIEF:
                break
        if not sources:
            continue
        evidence = sources + [{"ref": p, "kind": "note"} for p in cand.sources[:4]]
        item, outcome = inbox.add(
            kind="research_brief", loop="scout", title=f"Sources gathered: {cand.name}",
            summary=f"{len(sources)} pages that discuss {cand.name}" + (f" ({hint})" if hint else "")
                    + ". " + "; ".join(cand.reasons[:2]),
            dedupe_key=brief_key(cand.name), evidence=evidence, verification="source_checked",
            confidence=min(0.9, 0.4 + 0.12 * len(sources)), priority=min(90, int(40 + 4 * cand.score)), days=21,
            payload={"subject": cand.name, "query": query, "hint": hint}, now=now)
        briefs.append({"subject": cand.name, "sources": len(sources), "outcome": outcome})
    return {"searches": done, "briefs": briefs}


def brief_for(inbox, subject: str) -> dict | None:
    item = inbox.find(brief_key(subject))
    return item if item and item["status"] in ("new", "accepted") else None

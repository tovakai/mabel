"""Phase 4 Archivist: rule-based vault maintenance suggestions (no model calls).

- duplicate: an unresolved [[link]] that is almost certainly a variant of an
  existing note ("ghibli" -> Studio Ghibli, a typo, different capitalization or
  diacritics), or several unresolved spellings of the same thing;
- unfinished_question: open questions Mabel left herself that have sat
  unpicked for days.

Reads the vault index read-only; never edits a note. Links from private notes
are skipped, like everywhere else in the curiosity pipeline.
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.curiosity.candidates import _connect, _skip_reason, normalize_target

FUZZY_MIN = 0.9
STALE_QUESTION_DAYS = 3
_STOP = {"the", "a", "an", "of", "and"}


def fold(text: str) -> str:
    text = "".join(ch for ch in unicodedata.normalize("NFKD", str(text).casefold()) if not unicodedata.combining(ch))
    return " ".join(w for w in re.findall(r"[a-z0-9]+", text))


def key(text: str) -> str:
    return "".join(w for w in fold(text).split() if w not in _STOP)


def _typo_like(target: str, title: str) -> bool:
    """Only small slips inside longer words; "man"/"men" or a containing name is a different thing."""
    a, b = key(target), key(title)
    if a in b or b in a:
        return abs(len(a) - len(b)) <= 1          # Madvillain/Madvillainy yes, UMANIMATION/Animation no
    ta = [w for w in fold(target).split() if w not in _STOP]
    tb = [w for w in fold(title).split() if w not in _STOP]
    if len(ta) != len(tb):
        return False
    pairs = [(x, y) for x, y in zip(ta, tb) if x != y]
    return bool(pairs) and all(len(x) >= 6 and len(y) >= 6 and
                               difflib.SequenceMatcher(None, x, y).ratio() >= 0.8 for x, y in pairs)


def duplicate_suggestions(db_path: Path) -> list[dict]:
    db = _connect(db_path)
    titles: dict[str, str] = {}
    for row in db.execute("SELECT title, aliases FROM notes"):
        titles.setdefault(key(row["title"]), row["title"])
        try:
            for alias in json.loads(row["aliases"] or "[]"):
                titles.setdefault(key(alias), row["title"])
        except ValueError:
            pass
    words: dict[str, set[str]] = defaultdict(set)      # distinctive word -> titles containing it
    for k, title in titles.items():
        for w in fold(title).split():
            if len(w) >= 5 and w not in _STOP:
                words[w].add(title)
    sources: dict[str, dict] = {}
    for row in db.execute("""SELECT e.target, e.property_key, e.context, e.line, n.path FROM edges e
                             JOIN notes n ON n.id = e.source_id WHERE e.resolution = 'unresolved'"""):
        if _skip_reason(row):   # includes Genre/Cast values: "Rock" is a genre, not The Rock
            continue
        target = normalize_target(row["target"])
        entry = sources.setdefault(target, {"target": target, "refs": []})
        if len(entry["refs"]) < 5:
            entry["refs"].append({"ref": f"{row['path']}#L{row['line']}", "kind": "note",
                                  "context": " ".join(str(row["context"] or "").split())[:160]})
    out = []
    by_key: dict[str, list[str]] = defaultdict(list)
    keys = list(titles)
    for target, entry in sources.items():
        k = key(target)
        if not k:
            continue
        by_key[k].append(target)
        match, confidence, why = None, 0.0, ""
        if k in titles:
            article_only = fold(target).split()[:1] != fold(titles[k]).split()[:1]
            match = titles[k]
            confidence, why = ((0.65, "same name apart from a leading article") if article_only else
                               (0.9, "same name apart from case, punctuation or accents"))
        else:
            tokens = [w for w in fold(target).split() if w not in _STOP]
            if len(tokens) == 1 and len(tokens[0]) >= 5 and len(words.get(tokens[0], ())) == 1:
                candidate = next(iter(words[tokens[0]]))
                if len(fold(candidate).split()) <= 3:
                    match, confidence, why = candidate, 0.5, "the only short note title containing this word"
            if not match and len(k) >= 6:
                close = difflib.get_close_matches(k, keys, n=1, cutoff=FUZZY_MIN)
                if close and _typo_like(target, titles[close[0]]):
                    ratio = difflib.SequenceMatcher(None, k, close[0]).ratio()
                    match, confidence, why = titles[close[0]], round(min(0.8, ratio - 0.15), 2), "looks like a typo"
        if match and match != target:
            out.append({"target": target, "match": match, "confidence": confidence, "why": why, "refs": entry["refs"]})
    for k, variants in by_key.items():
        if len(variants) > 1 and k not in titles:
            refs = [r for v in variants for r in sources[v]["refs"]][:8]
            out.append({"target": " / ".join(sorted(variants)), "match": None, "confidence": 0.8,
                        "why": "several spellings of the same unresolved link", "refs": refs, "variants": sorted(variants)})
    return out


def run(inbox, db_path: Path, backlog_path: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    created = refreshed = 0
    for s in duplicate_suggestions(db_path):
        if s["match"]:
            title = f"[[{s['target']}]] probably means [[{s['match']}]]"
            dedupe = f"duplicate:{key(s['target'])}->{key(s['match'])}"
            evidence = s["refs"] + [{"ref": f"note:{s['match']}", "kind": "note", "context": "existing note"}]
        else:
            title = f"One thing, several spellings: {s['target']}"
            dedupe = "variants:" + "|".join(key(v) + ":" + v for v in s["variants"])
            evidence = s["refs"]
        _item, outcome = inbox.add(kind="duplicate", loop="archivist", title=title, summary=s["why"],
                                   dedupe_key=dedupe, evidence=evidence, verification="source_checked",
                                   confidence=s["confidence"], priority=int(20 + 30 * s["confidence"]), days=60,
                                   payload={"target": s["target"], "match": s["match"], "variants": s.get("variants")},
                                   now=now)
        created += outcome == "created"
        refreshed += outcome == "refreshed"
    try:
        questions = json.loads(Path(backlog_path).read_text()).get("questions", [])
    except (OSError, ValueError):
        questions = []
    stale = (now - timedelta(days=STALE_QUESTION_DAYS)).isoformat()
    for q in questions:
        if q.get("status") == "open" and q.get("created_at", "") <= stale:
            _item, outcome = inbox.add(kind="unfinished_question", loop="archivist", title=q["question"],
                                       summary=f"Left open in {Path(q['from_article']).stem}; not picked for "
                                               f"{STALE_QUESTION_DAYS}+ days.",
                                       dedupe_key=f"question:{q['id']}",
                                       evidence=[{"ref": q["from_article"], "kind": "article"}],
                                       verification="unverified", confidence=0.5, priority=40, days=30,
                                       payload={"question_id": q["id"], "subject": q.get("subject")}, now=now)
            created += outcome == "created"
            refreshed += outcome == "refreshed"
    return {"created": created, "refreshed": refreshed}

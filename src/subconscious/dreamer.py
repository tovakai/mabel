"""Phase 4 Dreamer: recurring themes in what the user has been talking about (Dottie's model).

1. A lexical pass over the last week's user messages finds terms that recur
   across different chats (cheap, deterministic).
2. Dottie (the small background model, on its own machine, so chats are never
   slowed) turns them into a few patterns, each citing message ids.
3. A pattern is kept only if it cites at least two real messages that actually
   contain the theme; it becomes a ``pattern`` inbox item with those pointers.
4. If Mabel has no whisper right now, the strongest pattern can become one
   (source ``conversation``), through the shared whisper rule.

Chats with memory extraction switched off are never read. The life log (what
he's been doing on his devices: games, media, apps, page titles) is offered
alongside the chats; a pattern may cite either, but always two real items.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.subconscious.archivist import fold, key

# Test-harness and smoke-test chats are not things the user talked about.
TEST_SESSION = re.compile(r"^\s*(?:\[(?:eval|validation|smoke|test)\]|routing smoke|dottie-research\b)", re.I)
WINDOW_DAYS = 7
MAX_MESSAGES = 300
MIN_CHATS = 2
TOP_TERMS = 20
SNIPPETS_PER_TERM = 3
WHISPER_MIN_CONFIDENCE = 0.7
_STOP = set("""
about after again also always another anything around because been before being between both could
didn't does doesn't doing don't down each even every from going gonna good have having here into it's
just know like little look made make many maybe more most much need never only other over really right
same should since some something still such sure take than that that's their them then there these they
thing things think this those though through time today very want wanna well were what when where which
while will with would yeah your you're yours mabel please thanks thank okay sorry actually kind sort
what's let's lets tell show give find first last next could would hello hey hiya whats can't cant read imagine
""".split())


def recent_messages(owner: str, *, now: datetime | None = None, days: int = WINDOW_DAYS) -> list[dict]:
    from core import database
    since = (now or datetime.now(timezone.utc)).replace(tzinfo=None) - timedelta(days=days)
    with database.SessionLocal() as db:
        rows = (db.query(database.ChatMessage.id, database.ChatMessage.session_id, database.ChatMessage.content,
                         database.ChatMessage.timestamp, database.Session.name)
                .join(database.Session, database.Session.id == database.ChatMessage.session_id)
                .filter(database.Session.owner == owner, database.ChatMessage.role == "user",
                        database.ChatMessage.timestamp >= since,
                        database.Session.memory_extraction_enabled.isnot(False))
                .order_by(database.ChatMessage.timestamp.desc()).limit(MAX_MESSAGES).all())
    return [{"id": r.id, "session": r.session_id, "text": " ".join(str(r.content or "").split())[:400],
             "at": r.timestamp.isoformat() if r.timestamp else ""}
            for r in rows if str(r.content or "").strip() and not TEST_SESSION.match(str(r.name or ""))]


def recurring_terms(messages: list[dict]) -> list[dict]:
    chats: dict[str, set[str]] = defaultdict(set)
    hits: dict[str, list[dict]] = defaultdict(list)
    display: dict[str, str] = {}
    for m in messages:
        seen = set()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9'+-]{3,}", m["text"]):
            folded = fold(word).replace(" ", "")
            if folded in _STOP or folded in seen or len(folded) < 4:
                continue
            seen.add(folded)
            chats[folded].add(m["session"])
            hits[folded].append(m)
            display.setdefault(folded, word)
    terms = [t for t in chats if len(chats[t]) >= MIN_CHATS]
    terms.sort(key=lambda t: (-len(chats[t]), -len(hits[t])))
    return [{"term": display[t], "chats": len(chats[t]), "messages": hits[t][:SNIPPETS_PER_TERM]}
            for t in terms[:TOP_TERMS]]


LIFE_ITEMS = 25


def life_items(*, now: datetime, days: int = WINDOW_DAYS, store=None) -> list[dict]:
    """The week's most time-consuming games/media/apps and some page titles, as citable items."""
    try:
        from src.lifelog.store import default_store
        store = store or default_store()
        spans = store.spans(now - timedelta(days=days), now, kinds=["game", "media", "focus", "browse", "phone_app"])
    except Exception:
        return []
    totals: dict[tuple, dict] = {}
    for s in spans:
        label = s["title"] if s["kind"] in ("game", "media", "browse", "phone_app") else s["app"]
        if not label:
            continue
        key = (s["kind"], label)
        start, end = datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])
        item = totals.setdefault(key, {"kind": s["kind"], "text": label[:200], "minutes": 0.0, "count": 0,
                                       "ref": f"lifelog:{s['device']}:{s['span_id']}"})
        item["minutes"] += max(0.0, (end - start).total_seconds() / 60)
        item["count"] += 1
    ranked = sorted(totals.values(), key=lambda i: (-(i["minutes"] + 2 * i["count"])))
    return ranked[:LIFE_ITEMS]


async def run(inbox, *, owner: str, now: datetime | None = None, ask=None) -> dict:
    now = now or datetime.now(timezone.utc)
    messages = recent_messages(owner, now=now)
    terms = recurring_terms(messages)
    life = life_items(now=now)
    if not terms and not life:
        return {"messages": len(messages), "terms": 0, "patterns": []}
    ids: dict[str, dict] = {}
    lines = []
    for t in terms:
        refs = []
        for m in t["messages"]:
            mid = ids.setdefault(m["id"], {"label": f"m{len(ids) + 1}", **m})["label"]
            refs.append(mid)
        lines.append(f"- {t['term']} (in {t['chats']} chats): {', '.join(refs)}")
    life_lines = []
    for i, item in enumerate(life, 1):
        label = f"l{i}"
        ids[label] = {"label": label, "id": label, "session": None, "text": item["text"], "at": "",
                      "life_ref": item["ref"], "kind": item["kind"]}
        life_lines.append(f"{label}: [{item['kind']}] {item['text']} ({round(item['minutes'])} min, {item['count']}x)")
    by_label = {v["label"]: v for v in ids.values()}
    snippets = "\n".join(f"{v['label']}: {v['text'][:300]}" for v in ids.values() if not v.get("life_ref"))
    prompt = (
        "Below are words that keep coming up in Anthon's recent chats with Mabel, the messages they come "
        "from, and what he's been doing on his devices. Find up to 4 genuine recurring themes (things he cares about or keeps returning to), not "
        "filler words. For each, cite the message labels it comes from (at least two), and write one question "
        "Mabel might honestly wonder about it, in her own first-person voice (casual, curious, not nosy).\n\n"
        f"Recurring words:\n{chr(10).join(lines) or '(none)'}\n\nMessages:\n{snippets or '(none)'}\n\n"
        f"What he's been doing on his devices this week (life log; cite as l-labels):\n{chr(10).join(life_lines) or '(nothing recorded)'}\n\n"
        'Reply with only JSON: {"patterns": [{"theme": "short name", "observation": "one sentence", '
        '"evidence": ["m1", "m4"], "curiosity": "I keep wondering ...", "confidence": 0.0}]}')
    reply = await (ask or _ask_dottie)(prompt, owner)
    patterns = _parse(reply)
    kept = []
    for p in patterns:
        theme = " ".join(str(p.get("theme") or "").split())[:80]
        refs = [by_label[e] for e in dict.fromkeys(str(x) for x in p.get("evidence") or []) if e in by_label]
        words = [w for w in fold(theme).split() if len(w) >= 4 and w not in _STOP]
        supporting = [m for m in refs if any(w in fold(m["text"]) for w in words)]
        if not theme or len({m["id"] for m in supporting}) < 2:
            continue                       # unsupported themes are dropped, not stored
        try:
            confidence = max(0.0, min(1.0, float(p.get("confidence") or 0.5)))
        except (TypeError, ValueError):
            confidence = 0.5
        evidence = [{"ref": m["life_ref"], "kind": "lifelog", "context": m["text"][:160], "at": ""}
                    if m.get("life_ref") else
                    {"ref": f"chat:{m['session']}/{m['id']}", "kind": "chat", "context": m["text"][:160],
                     "at": m["at"]} for m in supporting[:6]]
        curiosity = " ".join(str(p.get("curiosity") or "").split())[:300]
        item, outcome = inbox.add(
            kind="pattern", loop="dreamer", title=theme,
            summary=" ".join(str(p.get("observation") or "").split())[:400], dedupe_key=f"pattern:{key(theme)}",
            evidence=evidence, verification="source_checked", confidence=confidence,
            priority=int(30 + 40 * confidence), days=14,
            payload={"curiosity": curiosity, "chats": len({m['session'] for m in supporting})}, now=now)
        kept.append({"theme": theme, "confidence": confidence, "curiosity": curiosity, "outcome": outcome,
                     "item": item["id"] if item else None})
    whisper = _maybe_whisper(kept, now)
    return {"messages": len(messages), "terms": len(terms), "patterns": kept, "whisper": whisper}


def _maybe_whisper(kept: list[dict], now: datetime) -> str:
    """Only when she has no whisper at all: Dreamer never displaces research or yours."""
    best = max((k for k in kept if k["curiosity"] and k["outcome"] == "created"
                and k["confidence"] >= WHISPER_MIN_CONFIDENCE), key=lambda k: k["confidence"], default=None)
    if not best:
        return "no_strong_pattern"
    try:
        from pathlib import Path
        from src.constants import DATA_DIR
        from src.persona.whisper import WhisperStore
        store = WhisperStore(Path(DATA_DIR) / "persona" / "whispers.json")
        if store.active(now):
            return "kept_existing"
        _w, reason = store.propose("conversation", best["theme"], best["curiosity"], now=now,
                                   reference=f"inbox:{best['item']}")
        return reason
    except Exception:
        return "failed"


async def _ask_dottie(prompt: str, owner: str) -> str:
    from src import model_roles
    return await model_roles.call("research", [{"role": "user", "content": prompt}], owner=owner,
                                  max_tokens=1500, thinking_mode="off", temperature=0.3, timeout=120)


def _parse(reply: str) -> list[dict]:
    text = re.sub(r"(?s)<think>.*?</think>", "", str(reply or ""))
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    patterns = data.get("patterns") if isinstance(data, dict) else None
    return [p for p in patterns if isinstance(p, dict)][:4] if isinstance(patterns, list) else []

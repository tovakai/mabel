"""Once a day, Mabel writes her own account of Anthon's day.

Covers the night-owl day that just ended (06:00 -> 06:00 local). Inputs are
the deterministic summary of the life log (devices, apps, games, browsing as
page titles, media, places, steps) and his journal notes from that day in the
source vault (read-only). Output: ``journal/anthon/YYYY-MM-DD.md`` in Mabel's
own vault, with a factual timeline, what stood out to her, and things she's
curious about (which her outreach can draw on).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import yaml

from .summary import DAY_START_HOUR, day_bounds, summarize

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
JOURNAL_CHARS = 6000
CHECK_EVERY_S = 15 * 60


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def journal_for(day_start: datetime, day_end: datetime, *, vault: Path | None = None) -> list[dict]:
    """His journal notes for that day: files named with the date, or edited during it."""
    if vault is None:
        from src.constants import DATA_DIR
        try:
            vault = Path(json.loads((Path(DATA_DIR) / "vault_target.json").read_text())["path"]).expanduser()
        except (OSError, ValueError, KeyError):
            return []
    folder = Path(vault) / str(_setting("lifelog_journal_folder", "daily life of Anthon"))
    date = day_start.strftime("%Y-%m-%d")
    notes, used = [], 0
    for path in sorted(folder.rglob("*.md")):
        try:
            stat = path.stat()
            edited = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        except OSError:
            continue
        if date not in path.name and not (day_start <= edited.astimezone() < day_end):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        text = text.split("\n---\n", 1)[1] if text.startswith("---\n") and "\n---\n" in text else text
        text = text.strip()[: JOURNAL_CHARS - used]
        if text:
            notes.append({"note": str(path.relative_to(vault)), "text": text})
            used += len(text)
        if used >= JOURNAL_CHARS:
            break
    return notes


def _facts(summary: dict) -> str:
    lines = []
    for d in summary["devices"]:
        lines.append(f"- device {d['device']}: active {d['first']}-{d['last']} (~{d['active_minutes']} min focused)")
    for key, label in (("apps", "app focus"), ("games", "games"), ("media", "listening/watching"),
                       ("phone_apps", "phone apps"), ("activity", "activity")):
        if summary[key]:
            lines.append(f"- {label}: " + ", ".join(f"{x['name']} ({x['minutes']} min)" for x in summary[key]))
    if summary["sites"]:
        lines.append("- sites visited: " + ", ".join(f"{s['name']} ({s['visits']})" for s in summary["sites"]))
    if summary["pages"]:
        lines.append("- some pages he looked at: " + "; ".join(summary["pages"][:25]))
    for p in summary["places"]:
        lines.append(f"- place: {p['place']} {p['from']}-{p['to']}")
    if summary["steps"]:
        lines.append(f"- steps: {summary['steps']}")
    return "\n".join(lines)


async def compose_day(day_start: datetime, *, store=None, ask=None, vault: Path | None = None,
                      mabel_vault: Path = _REPO, owner: str = "tovakai") -> dict:
    from .store import default_store
    store = store or default_store()
    day_end = day_start + timedelta(days=1)
    summary = summarize(store.spans(day_start, day_end), start=day_start, end=day_end)
    journal = journal_for(day_start, day_end, vault=vault)
    if not summary["span_count"] and not journal:
        return {"status": "nothing_recorded", "date": day_start.strftime("%Y-%m-%d")}
    facts = _facts(summary)
    notes = "\n\n".join(f"[{n['note']}]\n{n['text']}" for n in journal) or "(no journal entry)"
    label = day_start.strftime("%A %-d %B")
    prompt = f"""Here is what your devices and Home Assistant recorded about Anthon's day ({label}, {DAY_START_HOUR:02d}:00 to {DAY_START_HOUR:02d}:00 the next morning; he's a night owl), and his own journal notes from it.

Recorded activity:
{facts or '- (nothing recorded by the devices)'}

His journal:
{notes}

Write your own account of his day for your private journal, in your voice, using exactly this structure:

# Anthon's day: {label}

## Timeline
A short factual run-through in time order, using only what's recorded above (approximate times are fine). Don't invent anything.

## What stood out to me
A few honest observations: patterns, moods, what he seemed into. Your take, kind and curious, not judgmental; no lecturing about screen time or sleep.

## Moments worth remembering
One to three concrete moments from this day that would be worth remembering a month from now, one sentence each, as a bullet list. Only from what's recorded or in his journal.

## Things I'm curious about
One to three things you'd genuinely like to ask him about, as a bullet list.

## Things I might look into
Zero to two things from his day you'd like to research for yourself (a game, a show, a place, an idea), as bullets in the form "Subject: the question you'd want answered". Leave the list empty if nothing grabs you."""
    system = "You are Mabel."
    try:
        from src.persona import composer
        library = composer().modules.load()
        if library.core:
            system = library.core.body
    except Exception:
        pass
    text = await (ask or _ask_mabel)([{"role": "system", "content": system}, {"role": "user", "content": prompt}], owner)
    text = re.sub(r"(?s)<think>.*?</think>", "", str(text or "")).strip()
    if "## Timeline" not in text:
        raise ValueError("the day summary came back without its structure")
    curious = [re.sub(r"^[-*\d.)\s]+", "", line).strip() for line in
               (re.search(r"## Things I'm curious about\s*(.*?)(?=\n## |\Z)", text, re.S) or [None, ""])[1].splitlines()
               if line.strip()][:3] if "## Things I'm curious about" in text else []
    meta = {"type": "day-summary", "date": day_start.strftime("%Y-%m-%d"),
            "window": f"{day_start.isoformat()} / {day_end.isoformat()}", "steps": summary["steps"],
            "devices": [d["device"] for d in summary["devices"]],
            "top_games": [g["name"] for g in summary["games"][:3]], "journal_notes": [n["note"] for n in journal],
            "curious": curious, "written": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    from src.episodic_memory.promotion import day_moments
    research = []
    block = re.search(r"## Things I might look into\s*(.*?)(?=\n## |\Z)", text, re.S)
    for line in (block.group(1).splitlines() if block else []):
        line = re.sub(r"^[-*\d.)\s]+", "", line).strip()
        if ":" in line:
            subject, question = (x.strip() for x in line.split(":", 1))
            if subject and question:
                research.append({"subject": subject[:80], "question": question[:300]})
    meta["moments"] = day_moments(text)[:3]
    meta["look_into"] = research[:2]
    path = mabel_vault / "journal" / "anthon" / f"{meta['date']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + text + "\n",
                    encoding="utf-8")
    rel = str(path.relative_to(mabel_vault))
    return {"status": "written", "date": meta["date"], "path": rel, "curious": curious,
            "memories": _propose_memories(meta["date"], meta["moments"], owner),
            "research": _propose_research(rel, meta["look_into"])}


def _propose_memories(date: str, moments: list[str], owner: str) -> int:
    """Each moment becomes an episodic memory candidate; he decides in Memory -> Episodes."""
    try:
        from src.constants import DATA_DIR
        from src.episodic_memory import CandidateQueue, CapturePolicy
        from src.episodic_memory.candidates import CandidateDraft
        from src.episodic_memory.chat_recall import capture_enabled, vault_paths
        if not capture_enabled():
            return 0
        _root, protected = vault_paths()
        queue = CandidateQueue(Path(DATA_DIR) / "episodic_candidates", owner=owner, protected_roots=protected)
        made = 0
        import hashlib
        for moment in moments:
            row = queue.propose(CandidateDraft(
                kind="experience", actor="mabel", source_kind="tool_observation", source_ref=f"lifelog:day:{date}",
                source_event_id=f"moment:{hashlib.sha256(moment.encode()).hexdigest()[:16]}",
                origin="daily_life_account", summary=moment[:1200]),
                policy=CapturePolicy(episodic_enabled=True, memory_enabled=True))
            made += bool(row.get("created"))
        return made
    except Exception:
        logger.warning("[lifelog] memory candidates not proposed", exc_info=True)
        return 0


def _propose_research(article: str, items: list[dict]) -> int:
    """Things she wants to look into join her curiosity backlog (cooling off like any new branch)."""
    try:
        from src.constants import DATA_DIR
        from src.curiosity.backlog import Backlog
        log = Backlog(Path(DATA_DIR) / "curiosity" / "backlog.json")
        return sum(log.add_from_article(article=article, subject=i["subject"], questions=[i["question"]],
                                        links=[])["questions"] for i in items)
    except Exception:
        logger.warning("[lifelog] research candidates not added", exc_info=True)
        return 0


async def _ask_mabel(messages: list[dict], owner: str) -> str:
    from src import model_roles
    return await model_roles.call("default", messages, owner=owner, max_tokens=5000, temperature=0.7, timeout=300)


def due_day(local_now: datetime, mabel_vault: Path = _REPO) -> datetime | None:
    """The day that ended at the last 06:00, if it isn't written yet."""
    today_start, _ = day_bounds(local_now)
    previous = today_start - timedelta(days=1)
    path = mabel_vault / "journal" / "anthon" / f"{previous.strftime('%Y-%m-%d')}.md"
    return None if path.exists() else previous


async def daily_loop(owner: str = "tovakai") -> None:
    from src.curiosity import runner
    from src.heartbeat import signals
    await asyncio.sleep(600)
    skipped: set[str] = set()
    while True:
        try:
            day = due_day(datetime.now().astimezone())
            if day and day.strftime("%Y-%m-%d") not in skipped and _setting("lifelog_enabled", True):
                if not signals.any_chat_active() and not runner.status().get("running") and signals.strata_up():
                    result = await compose_day(day, owner=owner)
                    logger.info("[lifelog] day summary %s: %s", result.get("date"), result.get("status"))
                    if result["status"] == "nothing_recorded":
                        skipped.add(result["date"])
            from src.persona.reflection import maybe_reflect
            await maybe_reflect(owner)
        except Exception:
            logger.warning("[lifelog] day summary failed", exc_info=True)
        await asyncio.sleep(CHECK_EVERY_S)

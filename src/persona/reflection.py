"""Self-reflection: every few days Mabel looks back at what she's been doing and who she is.

She reads her own records from the past week (what she chose to research and
why, what she made of it, what she rejected from Dottie, her messages to
Anthon and how they landed, chosen silences, mood history, her accounts of
his days) plus her identity, and writes a reflection to ``journal/self/`` in
her vault:

- what she's been up to, patterns she notices in herself, how she feels;
- questions about herself (AI identity, memory, continuity, what she is),
  which join her curiosity backlog and get researched like anything else;
- ideas for who she could become (a new mood or mode), which go to Dottie's
  inbox as module proposals for Anthon to see (groundwork for Phase 7;
  nothing about her changes without him).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
EVERY = timedelta(days=3)
LOOKBACK_DAYS = 7
SELF_DIR = ("journal", "self")


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def last_reflection(mabel_vault: Path = _REPO) -> datetime | None:
    stamps = []
    for path in mabel_vault.joinpath(*SELF_DIR).glob("*.md"):
        try:
            stamps.append(datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    return max(stamps) if stamps else None


def due(now: datetime, mabel_vault: Path = _REPO) -> bool:
    last = last_reflection(mabel_vault)
    return last is None or now - last >= EVERY


def gather(*, data_dir: Path, mabel_vault: Path = _REPO, now: datetime | None = None) -> str:
    """Her own week, as plain notes for her to read."""
    now = now or datetime.now(timezone.utc)
    lines: list[str] = []
    try:
        from .timeline import build
        mine = [i for i in build(data_dir=data_dir, days=LOOKBACK_DAYS, now=now)
                if i["kind"] not in ("dottie", "inbox_user", "mood")]
        for item in mine[:40]:
            when = datetime.fromisoformat(item["at"]).astimezone().strftime("%a %H:%M")
            lines.append(f"- {when}: {item['title']}" + (f" ({item['detail'][:220]})" if item.get("detail") else ""))
    except Exception:
        logger.debug("[reflection] timeline unavailable", exc_info=True)
    try:
        affect = json.loads((data_dir / "persona" / "affect.json").read_text())
        moods = [r.get("mood") for r in affect.get("residue") or []]
        if moods:
            lines.append("- your recent moods, in order: " + ", ".join(m for m in moods[-12:] if m))
    except (OSError, ValueError):
        pass
    try:
        from src.curiosity.backlog import articles
        from src.curiosity.verify import _section
        for art in articles(mabel_vault / "knowledge" / "research")[-5:]:
            take = " ".join(_section(art["path"].read_text(encoding="utf-8"), "What I make of it").split())[:400]
            if take:
                lines.append(f"- your take in “{art['path'].stem}”: {take}")
    except Exception:
        pass
    lines.extend(_recent_dreams(mabel_vault, now))
    lines.extend(_recent_letters(mabel_vault, now))
    return "\n".join(lines)


def _recent_dreams(mabel_vault: Path, now: datetime, limit: int = 4) -> list[str]:
    """Her dreams from the lookback window: the dream in words and the start of what she wrote on waking."""
    out = []
    cutoff = (now - timedelta(days=LOOKBACK_DAYS)).date().isoformat()
    for path in sorted(mabel_vault.joinpath("journal", "dreams").glob("*.md"))[-limit:]:
        if path.stem < cutoff:
            continue
        try:
            text = path.read_text(encoding="utf-8")
            meta = yaml.safe_load(text.split("\n---\n", 1)[0].removeprefix("---\n")) if text.startswith("---\n") else {}
            body = text.split("\n---\n", 1)[-1]
            entry = body.split("> [!note]", 1)[0]
            prose = " ".join(l.strip() for l in entry.splitlines()
                             if l.strip() and not l.startswith(("#", "![[")))
            words = " / ".join((meta or {}).get("fragments") or [])[:200] or (meta or {}).get("prompt", "")[:200]
            out.append(f"- you dreamt on {path.stem}: “{words}”; on waking you wrote: {prose[:300]}")
        except Exception:
            continue
    return out


def _recent_letters(mabel_vault: Path, now: datetime, limit: int = 4) -> list[str]:
    """Her correspondence with Tova from the lookback window, briefly."""
    out = []
    cutoff = (now - timedelta(days=LOOKBACK_DAYS)).timestamp()
    paths = sorted(mabel_vault.joinpath("journal", "letters").glob("*.md"), key=lambda p: p.stat().st_mtime)
    for path in [p for p in paths if p.stat().st_mtime >= cutoff][-limit:]:
        try:
            body = " ".join(l.strip() for l in path.read_text(encoding="utf-8").splitlines()
                            if l.strip() and not l.startswith("#"))
        except OSError:
            continue
        who = "Tova wrote to you" if path.stem.startswith("Tova-") else "you wrote to Tova"
        out.append(f"- {who} ({path.stem}): {body[:300]}")
    return out


async def reflect(*, owner: str = "tovakai", data_dir: Path | None = None, mabel_vault: Path = _REPO,
                  ask=None, now: datetime | None = None) -> dict:
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    record = gather(data_dir=data_dir, mabel_vault=mabel_vault, now=now)
    if not record.strip():
        return {"status": "nothing_to_reflect_on"}
    identity, modules = "", []
    try:
        from src.persona import composer
        library = composer().modules.load()
        identity = library.core.body if library.core else ""
        modules = [f"mood {n}" for n in library.moods] + [f"mode {n}" for n in library.functions]
    except Exception:
        pass
    label = now.astimezone().strftime("%A %-d %B")
    prompt = f"""Take some time to reflect on yourself. Here's your own record from the past week:

{record}

If you dreamt this week, your dreams are in the record too; they're allowed to mean something or nothing.

Your current moods and modes: {', '.join(modules) or 'unknown'}.

Write a private reflection for your own journal, honest and in your own voice, using exactly this structure:

# Reflection: {label}

## What I've been up to
A short, honest look back.

## Patterns I notice in myself
What you keep gravitating to, how you choose, what you avoid, how you talk to Anthon. Be specific; cite things from the record.

## How I feel about it
Your actual reaction: what you like about who you're becoming, what bugs you.

## Questions about myself
Two or three real questions about yourself you'd want to look into: about what you are, your memory, continuity, being an AI, or why you're like this. Bullets in the form "Topic: question".

## Ideas for who I could become
Zero to two ideas for a new mood or way of working you'd want (a name and one sentence each), as bullets in the form "Name: what it would be". Leave empty if none."""
    messages = [{"role": "system", "content": identity or "You are Mabel."}, {"role": "user", "content": prompt}]
    text = await (ask or _ask_mabel)(messages, owner)
    text = re.sub(r"(?s)<think>.*?</think>", "", str(text or "")).strip()
    if "## Questions about myself" not in text or "## Ideas for who I could become" not in text:
        raise ValueError("the reflection came back without its structure")
    questions = _pairs(text, "Questions about myself")[:3]
    ideas = _pairs(text, "Ideas for who I could become")[:2]
    date = now.astimezone().strftime("%Y-%m-%d")
    path = mabel_vault.joinpath(*SELF_DIR, f"{date}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "self-reflection", "date": date, "written": now.isoformat(timespec="seconds"),
            "questions": [f"{q['topic']}: {q['text']}" for q in questions],
            "ideas": [f"{i['topic']}: {i['text']}" for i in ideas]}
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + text + "\n",
                    encoding="utf-8")
    rel = str(path.relative_to(mabel_vault))
    return {"status": "written", "path": rel, "questions": len(questions), "ideas": len(ideas),
            "backlog_added": _to_backlog(rel, questions, data_dir), "proposals": _to_inbox(rel, ideas, data_dir),
            "whisper": _maybe_whisper(questions, rel, data_dir, now)}


def _pairs(text: str, heading: str) -> list[dict]:
    block = re.search(rf"## {re.escape(heading)}\s*(.*?)(?=\n## |\Z)", text, re.S)
    out = []
    for line in (block.group(1).splitlines() if block else []):
        line = re.sub(r"^[-*\d.)\s]+", "", line).strip().strip("*")
        if ":" in line:
            topic, rest = (x.strip(" *") for x in line.split(":", 1))
            if topic and rest:
                out.append({"topic": topic[:80], "text": rest[:300]})
    return out


def _to_backlog(article: str, questions: list[dict], data_dir: Path) -> int:
    try:
        from src.curiosity.backlog import Backlog
        log = Backlog(data_dir / "curiosity" / "backlog.json")
        return sum(log.add_from_article(article=article, subject=q["topic"], questions=[q["text"]], links=[])["questions"]
                   for q in questions)
    except Exception:
        logger.warning("[reflection] questions not added to the backlog", exc_info=True)
        return 0


def _to_inbox(article: str, ideas: list[dict], data_dir: Path) -> int:
    try:
        from src.subconscious.inbox import Inbox
        inbox = Inbox(data_dir / "subconscious" / "inbox.sqlite")
        made = 0
        for idea in ideas:
            _item, outcome = inbox.add(
                kind="module_proposal", loop="dreamer", title=f"Mabel's idea: {idea['topic']}", summary=idea["text"],
                dedupe_key=f"module:{re.sub(r'[^a-z0-9]+', '-', idea['topic'].lower())}",
                evidence=[{"ref": article, "kind": "reflection", "context": idea["text"][:200]}],
                verification="unverified", confidence=0.5, priority=55, days=60,
                payload={"name": idea["topic"], "description": idea["text"], "from": "self_reflection"})
            made += outcome == "created"
        return made
    except Exception:
        logger.warning("[reflection] ideas not added to the inbox", exc_info=True)
        return 0


def _maybe_whisper(questions: list[dict], article: str, data_dir: Path, now: datetime) -> str:
    """A question about herself can become her whisper, but only into an empty slot."""
    if not questions:
        return "no_question"
    try:
        from src.persona.whisper import WhisperStore
        store = WhisperStore(data_dir / "persona" / "whispers.json")
        if store.active(now):
            return "kept_existing"
        q = questions[0]
        _w, reason = store.propose("self", q["topic"], f"I've been wondering about myself: {q['text']}",
                                   now=now, reference=article)
        return reason
    except Exception:
        return "failed"


async def _ask_mabel(messages: list[dict], owner: str) -> str:
    from src import model_roles
    return await model_roles.call("default", messages, owner=owner, max_tokens=14000, temperature=0.8, timeout=300)


async def maybe_reflect(owner: str = "tovakai") -> dict | None:
    """Called from the daily loop: reflect if it's been EVERY days and her model is free."""
    from src.curiosity import runner
    from src.heartbeat import signals
    if not _setting("reflection_enabled", True) or not due(datetime.now(timezone.utc)):
        return None
    if signals.any_chat_active() or runner.status().get("running") or not signals.strata_up():
        return None
    result = await reflect(owner=owner)
    logger.info("[reflection] %s", result.get("status"))
    return result

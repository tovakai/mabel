"""Roadmap 2.0: what she's been doing lately, in her own words.

After each thing she does on her own (a dream, Tova's letters, the news, a
research session, her account of Anthon's day, a self-reflection), she writes
one or two sentences of inner monologue about it while it's still fresh. The
last few from the past two days are served in social turns, so a conversation
can pick up where her life has been instead of starting from nothing.

Every line keeps a source type (Roadmap 2.0 Phase 2.0 provenance):
``experienced`` (something she did), ``observed`` (the news, Anthon's day),
``told`` (Tova's letters), ``reflected`` (self-reflection), ``dreamt``.
A line is her memory of an activity, never a fact about the world, and it is
written from her own output (e.g. her note on a letter, not the letter), so
nothing anyone else wrote reaches her prompt through here.

Stored locally in ``data/persona/recent.json`` (private; never committed).
Serving is gated by the ``persona_recent`` setting: off / shadow / on.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .modules import _AUTHORITY_CONFLICT

logger = logging.getLogger(__name__)

from src.provenance import TYPES as SOURCES   # the one vocabulary (Roadmap 2.0, Phase 2.0)
MODES = ("off", "shadow", "on")
DEFAULT_MODE = "shadow"
KEEP = 20                   # lines kept on disk
SERVE = 4                   # lines served at most
SERVE_HOURS = 48
LINE_LIMIT = 260
EXCERPT_LIMIT = 3000
WRITE_TOKENS = 4000         # her private thinking counts against this too
_LOCK = threading.Lock()


def _setting(key, default):
    try:
        from src.settings import get_setting
        value = get_setting(key, default)
        return default if value is None else value
    except Exception:
        return default


def mode() -> str:
    value = str(_setting("persona_recent", DEFAULT_MODE) or DEFAULT_MODE).lower()
    return value if value in MODES else DEFAULT_MODE


def _path(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from src.constants import DATA_DIR
        data_dir = Path(DATA_DIR)
    return Path(data_dir) / "persona" / "recent.json"


def load(data_dir: Path | None = None) -> list[dict]:
    try:
        lines = json.loads(_path(data_dir).read_text(encoding="utf-8")).get("lines") or []
        return [line for line in lines if isinstance(line, dict) and line.get("text")]
    except (OSError, ValueError, AttributeError):
        return []


def _save(lines: list[dict], data_dir: Path | None = None) -> None:
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".recent-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"lines": lines[-KEEP:]}, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def clean_line(text: str) -> str:
    """Her reply → one short line: no thinking, no headings/bullets/quotes, no policy-shaped text."""
    text = re.sub(r"(?s)<think>.*?</think>", "", str(text or ""))
    paragraph = next((p for p in re.split(r"\n\s*\n", text) if p.strip()), "")
    line = " ".join(paragraph.split())
    line = re.sub(r"^(#+\s*|[-*>]\s+)", "", line).strip().strip('"“”').strip()
    if not line or "```" in line or _AUTHORITY_CONFLICT.search(line):
        return ""
    if len(line) > LINE_LIMIT:
        cut = line[:LINE_LIMIT]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        line = cut[:end + 1] if end > LINE_LIMIT // 3 else cut.rsplit(" ", 1)[0] + "…"
    return line


def add(kind: str, source: str, text: str, *, ref: str = "", now: datetime | None = None,
        data_dir: Path | None = None) -> dict | None:
    if source not in SOURCES:
        raise ValueError(f"unknown source type {source!r}")
    line = clean_line(text)
    if not line:
        return None
    entry = {"at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
             "kind": kind, "source": source, "text": line, "ref": str(ref or "")[:200]}
    with _LOCK:
        _save(load(data_dir) + [entry], data_dir)
    return entry


async def _ask(kind: str, excerpt: str, owner: str) -> str:
    from src import model_roles
    identity = ""
    try:
        from src.persona import composer
        library = composer().modules.load()
        identity = library.core.body if library.core else ""
    except Exception:
        pass
    prompt = f"""You just finished something on your own ({kind}). Here is what you wrote:

{excerpt[:EXCERPT_LIMIT]}

Now, privately, the way you'd remember it tomorrow: one or two sentences of your own inner monologue about it. What it was, and how it sits with you. First person, loose, honest, your voice, not a summary for anyone. No quotes, no headings, no file names. Under 220 characters."""
    messages = [{"role": "system", "content": identity or "You are Mabel."}, {"role": "user", "content": prompt}]
    return await model_roles.call("default", messages, owner=owner, max_tokens=WRITE_TOKENS, temperature=0.9,
                                  timeout=180)


async def remember(kind: str, source: str, excerpt: str, *, fallback: str, owner: str = "tovakai",
                   ref: str = "", now: datetime | None = None, data_dir: Path | None = None, ask=None) -> dict | None:
    """Write her line about something she just did. Never raises: a failed line falls back to ``fallback``."""
    if mode() == "off":
        return None
    text = ""
    if excerpt and excerpt.strip():
        try:
            text = clean_line(await (ask or _ask)(kind, excerpt, owner))
        except Exception as exc:
            logger.info("[recent] her line about %s didn't come: %s", kind, exc)
    try:
        return add(kind, source, text or fallback, ref=ref, now=now, data_dir=data_dir)
    except Exception:
        logger.warning("[recent] couldn't keep her line about %s", kind, exc_info=True)
        return None


def when(at: datetime, now: datetime) -> str:
    """Coarse on purpose: the fragment changes only when a label boundary passes, keeping the prompt cache warm."""
    at, now = at.astimezone(), now.astimezone()
    if now - at < timedelta(hours=3):
        return "a little while ago"
    night = at.hour >= 22 or at.hour < 6
    days = (now.date() - at.date()).days
    if night and (days == 0 and at.hour < 6 or days == 1 and at.hour >= 22):
        return "last night"
    if days == 0:
        return "this morning" if at.hour < 12 else "earlier today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def servable(now: datetime | None = None, data_dir: Path | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out = []
    for line in load(data_dir):
        try:
            at = datetime.fromisoformat(line["at"])
        except (KeyError, ValueError):
            continue
        if timedelta(0) <= now - at <= timedelta(hours=SERVE_HOURS):
            out.append({**line, "when": when(at, now)})
    return out[-SERVE:]


def fragment(lines: list[dict]) -> str:
    if not lines:
        return ""
    body = "\n".join(f"- {line['when']}: {line['text']}" for line in lines)
    return ("What you've been up to lately, in your own words (your memory, not news to announce; "
            "let it colour the conversation and mention it only if it connects):\n" + body)

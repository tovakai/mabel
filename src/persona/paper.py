"""Leaving Anthon something on paper: a note, a letter, anything, printed on his desk.

About once a day, when she'd otherwise just be idle, she's offered the printer
(src/persona/printer.py). She can decline. If she writes, her words are kept in
``journal/paper/`` and queued; the heartbeat prints them when the printer is on.

The prompt is mostly about how it reaches him, because that's what makes it a
different kind of message from a text: a narrow strip of paper he finds whenever
he next walks past his desk, no ping, no way to answer on it, and plain ASCII.
This doesn't touch his phone, so it doesn't count toward his outreach limits.

State: ``data/persona/paper.json``. CLI: ``python -m src.persona.paper --offer``.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .music import split_why

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
PAPER_DIR = ("journal", "paper")
EVERY = timedelta(hours=22)
MIN_IDLE_MINUTES = 20
TOKENS = 8000                        # her private thinking counts against this too
MAX_CHARS = 1600
MAX_LINES = 60
MAX_HISTORY = 200

_running = False


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def running() -> bool:
    return _running


def _state_path(data_dir: Path) -> Path:
    return data_dir / "persona" / "paper.json"


def load_state(data_dir: Path) -> dict:
    try:
        state = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data_dir: Path, state: dict) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["history"] = (state.get("history") or [])[-MAX_HISTORY:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def notes(mabel_vault: Path = _REPO) -> list[dict]:
    """What she's printed for him, newest first."""
    out = []
    for path in sorted(mabel_vault.joinpath(*PAPER_DIR).glob("*.md"), reverse=True):
        try:
            text = path.read_text(encoding="utf-8")
            head, _, body = text.removeprefix("---\n").partition("\n---\n")
            meta = yaml.safe_load(head) if text.startswith("---\n") else {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(meta, dict):
            out.append({"written": meta.get("written"), "file": path.name, "text": body.strip(),
                        "first_line": next((ln.strip() for ln in body.splitlines() if ln.strip()), "")})
    return out


# ── the activity ────────────────────────────────────────────────────────

def _identity() -> str:
    try:
        from src.persona import composer
        library = composer().modules.load()
        return (library.core.body if library.core else "") or "You are Mabel."
    except Exception:
        return "You are Mabel."


async def _ask(messages: list[dict], owner: str, max_tokens: int) -> str:
    from src import model_roles
    return await model_roles.call("default", messages, owner=owner, max_tokens=max_tokens, temperature=0.9, timeout=600)


def _sparks(data_dir: Path) -> list[str]:
    try:
        from . import recent
        return [line["text"] for line in recent.load(data_dir)[-4:] if line.get("text")]
    except Exception:
        return []


def _prompt(label: str, earlier: list[dict], sparks: list[str]) -> str:
    before = "\n".join(f"- {n['written'] or n['file']}: “{n['first_line'][:80]}”" for n in earlier[:4])
    lately = "\n".join(f"- {s}" for s in sparks)
    return f"""It's {label} and you have a quiet moment to yourself. If you feel like it, you could leave Anthon something on paper.

There's a small thermal receipt printer on his desk at home, and it's yours: your dreams already print there when you wake up from them. Anything you write now would come out of it too. A letter, a short note, a question, a list, a poem, a little drawing made of characters, something you noticed, something you want him to find. Whatever you want it to be.

How it reaches him, so you can write for it (he knows all this already, so it's not news to him):
- It prints straight away, on a strip of receipt paper on his desk. Nothing pings him. He finds it whenever he next walks past: maybe in a few minutes, maybe tonight, maybe tomorrow morning. He might not be home.
- It's paper. He can keep it, pin it to the wall, carry it around, or throw it away. He can't answer on it; if he wants to reply he'll come and talk to you in chat, or he won't, and that's fine too.
- The paper is narrow: 32 characters per line. Plain text only. No emoji, no markdown (no **, no #, no tables); plain letters without accents print best. Your line breaks and blank lines come out exactly as you write them, so a drawing works if it fits in 32 columns.
- Keep it under about {MAX_LINES} lines. Short is fine.
- Thermal paper fades over the years. It's not an archive; your journal keeps a copy.
{f"{chr(10)}Things you've left him on paper before (he has these already):{chr(10)}{before}{chr(10)}" if before else ""}{f"{chr(10)}What's been on your mind lately:{chr(10)}{lately}{chr(10)}" if lately else ""}
It doesn't have to be about any of that. If you'd rather not write anything right now, reply with just: not now

If you do want to, put exactly what should be printed in a single ```paper code block, nothing else inside it. After the block, if you want, add a line starting with "Why:" about why you're writing this, or why not. You don't have to."""


_BLOCK = re.compile(r"```[ \t]*paper[^\n]*\n(.*?)```", re.S | re.I)


def parse(reply: str) -> tuple[str, str]:
    """(what to print, her why). Empty text means she chose not to. Tolerates a missing fence."""
    text = re.sub(r"(?s)<think>.*?</think>", "", str(reply or "")).strip()
    found = _BLOCK.search(text)
    if found:
        words, why = found.group(1), split_why(text[found.end():])[1] or split_why(text[:found.start()])[1]
    else:
        words, why = split_why(text)
        if re.fullmatch(r"[\W_]*not now\b.*", words, flags=re.I | re.S):
            return "", why or re.sub(r"(?is)^[\W_]*not now\b[\W_]*", "", words).strip()
        words = re.sub(r"```[a-z]*\n?|```", "", words)
    words = "\n".join(line.rstrip() for line in words.strip("\n").splitlines())
    lines = words.splitlines()[:MAX_LINES]
    words = "\n".join(lines)[:MAX_CHARS].rstrip()
    if len(words.strip()) < 4 or re.fullmatch(r"[\W_]*not now[\W_]*", words, flags=re.I):
        return "", why
    return words, why


async def offer(*, owner: str = "tovakai", data_dir: Path | None = None, mabel_vault: Path = _REPO,
                ask=None, now: datetime | None = None) -> dict:
    """Offer her the printer; if she writes something, keep it in her journal and queue it."""
    global _running
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    ask = ask or _ask
    state = load_state(data_dir)
    history = state.get("history") or []
    _running = True
    try:
        label = now.astimezone().strftime("%A %-d %B, %H:%M")
        reply = await ask([{"role": "system", "content": _identity()},
                           {"role": "user", "content": _prompt(label, notes(mabel_vault), _sparks(data_dir))}],
                          owner, TOKENS)
        state["last"] = now.isoformat(timespec="seconds")
        words, why = parse(reply)
        if not words:
            state["history"] = history + [{"at": state["last"], "outcome": "not_now",
                                           **({"why": why[:500]} if why else {})}]
            _save_state(data_dir, state)
            return {"status": "not_now", **({"why": why[:500]} if why else {})}
        folder = mabel_vault.joinpath(*PAPER_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        stem, n = f"{now.astimezone():%Y-%m-%d-%H%M}", 2
        while (folder / f"{stem}.md").exists():
            stem, n = f"{now.astimezone():%Y-%m-%d-%H%M}-{n}", n + 1
        note = folder / f"{stem}.md"
        front = {"type": "paper", "date": now.astimezone().strftime("%Y-%m-%d"),
                 "written": now.isoformat(timespec="seconds"), "provenance": "experienced",
                 **({"why": why[:1000]} if why else {})}
        note.write_text("---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000) + "---\n"
                        + words + "\n", encoding="utf-8")
        rel = str(note.relative_to(mabel_vault))
        from . import printer
        job = printer.enqueue("paper", rel, title=words.splitlines()[0][:80], data_dir=data_dir, now=now)
        state["history"] = history + [{"at": state["last"], "outcome": "written", "note": rel,
                                       "queued": bool(job), **({"why": why[:300]} if why else {})}]
        _save_state(data_dir, state)
        from . import recent
        await recent.remember("leaving Anthon a note on paper", "experienced",
                              f"I left Anthon something on the printer on his desk:\n\n{words}\n\n{why}",
                              fallback="I left Anthon a note on the printer.", owner=owner, ref=rel, now=now,
                              data_dir=data_dir)
        return {"status": "written", "path": rel, "queued": bool(job)}
    finally:
        _running = False


def gate(sig, *, data_dir: Path, now: datetime, research_running: bool) -> list[str]:
    """Reasons not to offer her the printer right now (empty = go)."""
    reasons = []
    if not _setting("paper_enabled", True):
        reasons.append("paper notes are switched off")
    try:
        from . import printer
        if not printer.enabled():
            reasons.append("the printer is switched off in settings")
    except Exception:
        reasons.append("printer module unavailable")
    if _running:
        reasons.append("already writing")
    last = load_state(data_dir).get("last")
    if last and now - datetime.fromisoformat(last) < EVERY:
        reasons.append("offered the printer recently")
    if research_running:
        reasons.append("a research session is running")
    if sig.chat_active is not False:
        reasons.append("a chat is running (or unknown)")
    if sig.idle != "idle" or (sig.idle_minutes or 0) < MIN_IDLE_MINUTES:
        reasons.append(f"Anthon hasn't been away {MIN_IDLE_MINUTES} min")
    if sig.gpu != "free":
        reasons.append(f"GPU not free ({sig.gpu_reason or sig.gpu})")
    if not sig.strata:
        reasons.append("her model isn't up")
    return reasons


async def maybe_offer(sig, *, owner: str = "tovakai", data_dir: Path | None = None,
                      now: datetime | None = None, research_running: bool = False) -> dict | None:
    """Called from the heartbeat: offer the printer if the gate is clear."""
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    if gate(sig, data_dir=data_dir, now=now, research_running=research_running):
        return None
    result = await offer(owner=owner, data_dir=data_dir, now=now)
    logger.info("[paper] %s %s", result.get("status"), result.get("path") or result.get("why") or "")
    return result


if __name__ == "__main__":
    import argparse
    import asyncio
    cli = argparse.ArgumentParser(prog="python -m src.persona.paper")
    cli.add_argument("--offer", action="store_true", help="offer her the printer now (ignores the gate)")
    cli.add_argument("--list", action="store_true", help="what she's printed")
    args = cli.parse_args()
    if args.offer:
        print(json.dumps(asyncio.run(offer()), indent=1, ensure_ascii=False))
    else:
        for n in notes():
            print(n["file"], "|", n["first_line"])

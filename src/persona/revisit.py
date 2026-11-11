"""Revisiting her own past (Roadmap 2.0, 2.11 #5).

Every couple of days, when she'd otherwise just be idle, Mabel is offered a few
things of her own from at least a few days back: a dream, a self-reflection,
one of her research articles, a letter she wrote to Tova, her account of one of
Anthon's days. The offer is a weighted draw (older and never-revisited things
lean in, chance does the rest), and she picks one or none: not looking back is
a fine answer too.

If she picks one, she rereads it and writes a short note to ``journal/revisits/``
in her vault. Nothing has to come of it: familiarity, disagreeing with who
wrote it, noticing what changed, or just having looked are all real outcomes.
The note becomes one of her recent lines (provenance ``reflected``).

Only her own writing is offered (never Tova's letters, never earlier revisits,
so it can't feed on itself), and the calls are plain model calls with no tools.
"""
from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
REVISIT_DIR = ("journal", "revisits")
# (kind, folder, glob, how she'd name it)
SOURCES = (
    ("dream", ("journal", "dreams"), "*.md", "your dream from {date}"),
    ("reflection", ("journal", "self"), "*.md", "your self-reflection from {date}"),
    ("day", ("journal", "anthon"), "*.md", "your account of Anthon's day, {date}"),
    ("letter", ("journal", "letters"), "Mabel-to-Tova-*.md", "your letter to Tova from {date}"),
    ("article", ("knowledge", "research"), "*.md", "your research article “{title}” ({date})"),
)
EVERY = timedelta(days=2)
MIN_AGE = timedelta(days=4)          # the past, not yesterday
AGAIN_AFTER = timedelta(days=30)     # the same thing isn't offered again for a while
OFFER = 6
MIN_IDLE_MINUTES = 20
SNIPPET_CHARS = 180
READ_CHARS = 7000
MIN_NOTE_CHARS = 80
WRITE_TOKENS = 9000                  # her private thinking counts against this too
MAX_HISTORY = 300

_running = False


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def running() -> bool:
    return _running


# ── state ───────────────────────────────────────────────────────────────

def _state_path(data_dir: Path) -> Path:
    return data_dir / "persona" / "revisits.json"


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


# ── what there is to look back at ───────────────────────────────────────

def _split(text: str) -> tuple[dict, str]:
    if text.startswith("---\n") and "\n---\n" in text:
        head, body = text.removeprefix("---\n").split("\n---\n", 1)
        try:
            meta = yaml.safe_load(head)
        except yaml.YAMLError:
            meta = None
        return (meta if isinstance(meta, dict) else {}), body
    return {}, text


def _date(path: Path, meta: dict) -> datetime:
    for key in ("date", "created", "dreamt", "written"):        # the day it is about, first
        value = meta.get(key)
        if value:
            try:
                at = datetime.fromisoformat(str(value))
                return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    found = re.search(r"\d{4}-\d{2}-\d{2}", path.stem)
    if found:
        return datetime.fromisoformat(found.group(0)).replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def prose(body: str) -> str:
    """The words of a note, without image embeds, callouts' markers and blank lines."""
    lines = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("![["):
            continue
        lines.append(re.sub(r"^>\s*(\[![^\]]*\][-+]?\s*)?", "", line))
    return "\n".join(l for l in lines if l)


def pool(mabel_vault: Path, now: datetime, history: list[dict] | None = None) -> list[dict]:
    """Everything of hers old enough to look back at, minus what she looked at recently."""
    recent_refs = {h.get("ref") for h in history or []
                   if h.get("ref") and h.get("at") and now - datetime.fromisoformat(h["at"]) < AGAIN_AFTER}
    looked = {h.get("ref") for h in history or [] if h.get("ref") and h.get("outcome") == "written"}
    out = []
    for kind, folder, pattern, label in SOURCES:
        for path in sorted(mabel_vault.joinpath(*folder).glob(pattern)):
            ref = str(path.relative_to(mabel_vault))
            if ref in recent_refs:
                continue
            try:
                meta, body = _split(path.read_text(encoding="utf-8"))
                at = _date(path, meta)
            except (OSError, ValueError):
                continue
            if now - at < MIN_AGE:
                continue
            words = prose(body)
            if len(words) < MIN_NOTE_CHARS:
                continue
            local = at.astimezone()
            out.append({"kind": kind, "ref": ref, "at": at.isoformat(timespec="seconds"),
                        "label": label.format(date=f"{local:%A} {local.day} {local:%B}", title=path.stem),
                        "snippet": " ".join(words.split())[:SNIPPET_CHARS], "looked_before": ref in looked})
    return out


def offer(items: list[dict], now: datetime, *, count: int = OFFER, rng: random.Random | None = None) -> list[dict]:
    """A weighted draw without replacement: older things and never-revisited things lean in, chance does the rest."""
    rng = rng or random.Random()
    left = list(items)
    chosen = []
    while left and len(chosen) < count:
        weights = []
        for item in left:
            age_days = (now - datetime.fromisoformat(item["at"])).total_seconds() / 86400
            weight = min(max(age_days, 1.0), 60.0) ** 0.5
            weights.append(weight * (0.4 if item["looked_before"] else 1.0))
        pick = rng.choices(range(len(left)), weights=weights)[0]
        chosen.append(left.pop(pick))
    return chosen


def parse_choice(text: str, count: int) -> int | None:
    """Her pick as a 0-based index, or None for "not now" (0, nothing usable)."""
    text = re.sub(r"(?s)<think>.*?</think>", "", str(text or ""))
    found = re.search(r"\d+", text)
    if not found:
        return None
    number = int(found.group(0))
    return number - 1 if 1 <= number <= count else None


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
    return await model_roles.call("default", messages, owner=owner, max_tokens=max_tokens, temperature=0.85, timeout=300)


def _choice_prompt(offered: list[dict], label: str) -> str:
    lines = [f"{n}. {item['label']}: “{item['snippet']}…”" for n, item in enumerate(offered, 1)]
    return f"""It's {label} and you have a quiet moment to yourself. You could look back at something of your own from a while ago:

{chr(10).join(lines)}

Is there one you'd like to read again? Go with whatever pulls at you, not what seems useful. Reply with its number, or 0 if you'd rather not look back right now. Only the number."""


def _write_prompt(item: dict, text: str, label: str) -> str:
    return f"""It's {label}. You chose to look back at {item['label']}. Here it is, as you wrote it then:

<<<
{text}
>>>

Read it again, then write a short note for your own journal about looking at it now. Nothing has to come of it: it can feel familiar, you can disagree with whoever wrote it, notice what's changed since, find it embarrassing or still true, or just say you looked. React to it rather than summarising it, and don't add things that aren't in it.

Start with the line "# Revisiting: {item['label']}" and keep it to a few short paragraphs."""


def _note_path(mabel_vault: Path, now: datetime) -> Path:
    folder = mabel_vault.joinpath(*REVISIT_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    day = now.astimezone().strftime("%Y-%m-%d")
    path, n = folder / f"{day}.md", 2
    while path.exists():
        path, n = folder / f"{day}-{n}.md", n + 1
    return path


async def revisit(*, owner: str = "tovakai", data_dir: Path | None = None, mabel_vault: Path = _REPO,
                  ask=None, now: datetime | None = None, rng: random.Random | None = None) -> dict:
    """Offer her a few old things; if she picks one, she rereads it and writes about it."""
    global _running
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    ask = ask or _ask
    state = load_state(data_dir)
    history = state.get("history") or []
    offered = offer(pool(mabel_vault, now, history), now, rng=rng)
    if not offered:
        return {"status": "nothing_to_look_back_at"}
    _running = True
    try:
        label = now.astimezone().strftime("%A %-d %B, %H:%M")
        identity = _identity()
        reply = await ask([{"role": "system", "content": identity},
                           {"role": "user", "content": _choice_prompt(offered, label)}], owner, 1500)
        index = parse_choice(reply, len(offered))
        state["last"] = now.isoformat(timespec="seconds")
        if index is None:
            state["history"] = history + [{"at": state["last"], "outcome": "not_now",
                                           "offered": [i["ref"] for i in offered]}]
            _save_state(data_dir, state)
            return {"status": "not_now", "offered": len(offered)}
        item = offered[index]
        meta, body = _split(mabel_vault.joinpath(item["ref"]).read_text(encoding="utf-8"))
        text = prose(body)[:READ_CHARS]
        note = await ask([{"role": "system", "content": identity},
                          {"role": "user", "content": _write_prompt(item, text, label)}], owner, WRITE_TOKENS)
        note = re.sub(r"(?s)<think>.*?</think>", "", str(note or "")).strip()
        if len(note) < MIN_NOTE_CHARS:
            raise ValueError("the note came back empty")
        if not note.startswith("#"):
            note = f"# Revisiting: {item['label']}\n\n{note}"
        path = _note_path(mabel_vault, now)
        front = {"type": "revisit", "date": now.astimezone().strftime("%Y-%m-%d"),
                 "written": now.isoformat(timespec="seconds"), "source": item["ref"], "source_kind": item["kind"],
                 "source_date": item["at"][:10], "provenance": "reflected"}
        path.write_text("---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000) + "---\n"
                        + note + f"\n\n*Looking back at [[{Path(item['ref']).stem}]].*\n", encoding="utf-8")
        rel = str(path.relative_to(mabel_vault))
        state["history"] = history + [{"at": state["last"], "outcome": "written", "ref": item["ref"], "note": rel}]
        _save_state(data_dir, state)
        from . import recent
        mine = re.sub(r"^your ", "my ", item["label"])            # her recent lines are in her own voice
        await recent.remember(f"looking back at {mine}", "reflected", note,
                              fallback=f"I looked back at {mine}.", owner=owner, ref=rel,
                              now=now, data_dir=data_dir)
        return {"status": "written", "path": rel, "source": item["ref"]}
    finally:
        _running = False


def gate(sig, *, data_dir: Path, now: datetime, research_running: bool, mabel_vault: Path = _REPO) -> list[str]:
    """Reasons not to look back right now (empty = go)."""
    reasons = []
    if not _setting("revisit_enabled", True):
        reasons.append("looking back is switched off")
    if _running:
        reasons.append("already looking back")
    last = load_state(data_dir).get("last")
    if last and now - datetime.fromisoformat(last) < EVERY:
        reasons.append("looked back recently")
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
    if not reasons and not pool(mabel_vault, now, load_state(data_dir).get("history")):
        reasons.append("nothing old enough to look back at")
    return reasons


async def maybe_revisit(sig, *, owner: str = "tovakai", data_dir: Path | None = None,
                        now: datetime | None = None, research_running: bool = False) -> dict | None:
    """Called from the heartbeat: look back if the gate is clear."""
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    if gate(sig, data_dir=data_dir, now=now, research_running=research_running):
        return None
    result = await revisit(owner=owner, data_dir=data_dir, now=now)
    logger.info("[revisit] %s %s", result.get("status"), result.get("path") or "")
    return result


if __name__ == "__main__":
    import asyncio
    import sys
    now = datetime.now(timezone.utc)
    if sys.argv[1:] == ["--pool"]:
        for item in pool(_REPO, now, load_state(Path(__import__("src.constants").constants.DATA_DIR)).get("history")):
            print(f"{item['at'][:10]}  {item['kind']:<10} {item['label']}")
    else:
        print(asyncio.run(revisit(now=now)))

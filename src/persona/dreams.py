"""Visual dreams: at most once a day, one of Dottie's dreamer notes becomes a picture.

1. **Source:** something recent she hasn't dreamt yet, raw: a dreamer *pattern* (title,
   summary, evidence, the curiosity line) or a letter from Tova, taking turns when both exist.
2. **Dreaming it:** while her model is still up, she turns the note into a handful of broken
   dream fragments (spatial, emotional, wrong verbs, repeats allowed). Whole fragments are
   packed, in her order and with their grammar and repetition intact, up to SDXL's 77-token
   prompt limit (counted with CLIP's own tokenizer), leaving room for the trigger phrase.
   If that step fails, a shuffled word bag of the note is the fallback.
3. **Image:** rendered through the normal image tool, so the GPU swap applies (her model
   steps aside and comes back) and the picture also lands in the gallery.
4. **Waking:** she looks at the picture with her own eyes (vision) and writes a short
   dream-journal entry to ``journal/dreams/YYYY-MM-DD.md`` next to the image.

Gates (``maybe_dream``, from the heartbeat): dreams on, image generation configured with a
GPU swap, nothing dreamt this dream-day (06:00–06:00 local), Anthon away for a while, no
chat, GPU free, her model up, no research running. It is for vibes only: nothing here
changes her memory, mood or behaviour.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import random
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
DREAM_DIR = ("journal", "dreams")
DAY_START_HOUR = 6
MIN_IDLE_MINUTES = 20
SOURCE_DAYS = 3
LETTER_CHARS = 3000                  # enough of a letter to dream from
TOKEN_LIMIT = 77                     # CLIP: 75 usable + start/end tokens
TRIGGER_RESERVE = 8                  # room for ". in pixel art style" (6) plus slack
DEFAULT_TOKENIZER = Path.home() / ".local/share/mabel/diffusion/models/clip-tokenizer/tokenizer.json"
STOPWORDS = set("""
a an the and or but nor of on in at to for from by with without into onto over under about above below
is are was were be been being am do does did doing have has had having will would could should can may might must
i me my mine we us our you your yours he him his she her hers it its they them their this that these those
if then than so as just also only very too not no yes how what when where which who whom why
same second time often sets up keep keeps actually following specifically mentioning chat oct evidence pattern
re ve ll s t d m
""".split())
_running = False


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def dream_day(now: datetime) -> str:
    """The dream-day a moment belongs to: 06:00 local to 06:00 the next morning."""
    return (now.astimezone() - timedelta(hours=DAY_START_HOUR)).date().isoformat()


def running() -> bool:
    return _running


# ── state ───────────────────────────────────────────────────────────────

def _state_path(data_dir: Path) -> Path:
    return data_dir / "persona" / "dreams.json"


def load_state(data_dir: Path) -> dict:
    try:
        state = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data_dir: Path, state: dict) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def dreamt_today(data_dir: Path, now: datetime) -> bool:
    return load_state(data_dir).get("last_day") == dream_day(now)


# ── source and word bag ─────────────────────────────────────────────────

def _letter_sources(mabel_vault: Path, now: datetime, used: set) -> list[dict]:
    """Tova's letters from the last SOURCE_DAYS, as dream sources."""
    cutoff = (now - timedelta(days=SOURCE_DAYS)).timestamp()
    out = []
    for path in mabel_vault.joinpath("journal", "letters").glob("Tova-to-Mabel-*.md"):
        at = path.stat().st_mtime
        if at >= cutoff and f"letter:{path.stem}" not in used:
            out.append({"id": f"letter:{path.stem}", "title": "A letter from Tova", "kind": "letter",
                        "created_at": datetime.fromtimestamp(at, timezone.utc).isoformat(),
                        "text": path.read_text(encoding="utf-8")[:LETTER_CHARS]})
    return out


def pick_source(data_dir: Path, now: datetime, *, inbox=None, mabel_vault: Path = _REPO) -> dict | None:
    """Newest thing from the last SOURCE_DAYS not dreamt yet; notes and letters take turns."""
    if inbox is None:
        from src.subconscious.inbox import Inbox
        inbox = Inbox(data_dir / "subconscious" / "inbox.sqlite")
    used = set(load_state(data_dir).get("dreamt_items") or [])
    cutoff = (now - timedelta(days=SOURCE_DAYS)).astimezone(timezone.utc).isoformat()
    items = [i for s in ("new", "accepted", "rejected", "done", "expired")
             for i in inbox.list(status=s, kind="pattern", limit=200)]
    items = [i for i in items if i["loop"] == "dreamer" and i["created_at"] >= cutoff and i["id"] not in used]
    letters = _letter_sources(mabel_vault, now, used)
    newest = lambda group: max(group, key=lambda i: i["created_at"]) if group else None
    if items and letters:
        last = load_state(data_dir).get("last_source")
        return newest(items) if last == "letter" else newest(letters)
    return newest(items or letters)


def source_text(item: dict) -> str:
    """The note as it reads in the inbox (or the letter as she received it), raw."""
    if item.get("text"):
        return str(item["text"]).strip()
    parts = [f"Pattern: {item['title']}", item.get("summary") or ""]
    for ev in item.get("evidence") or []:
        if ev.get("context"):
            parts.append(str(ev["context"]))
    curiosity = (item.get("payload") or {}).get("curiosity")
    if curiosity:
        parts.append(f"Curiosity: {curiosity}")
    return "\n".join(p for p in parts if p)


def token_counter(path: Path | None = None):
    """CLIP token count of a prompt (start/end tokens included). Falls back to a cautious estimate."""
    path = Path(path or _setting("dream_clip_tokenizer", "") or DEFAULT_TOKENIZER)
    try:
        from tokenizers import Tokenizer
        tok = Tokenizer.from_file(str(path))
        return lambda text: len(tok.encode(text).ids)       # includes the start/end tokens
    except Exception:
        # Overestimates on purpose: long or odd words split into several BPE pieces.
        return lambda text: 2 + sum(max(1, math.ceil(len(w) / 5)) for w in re.findall(r"[A-Za-z]+", text)) \
            + len(re.findall(r"[^A-Za-z\s]", text))


def word_bag(text: str, *, count=None, seed=None, limit: int = TOKEN_LIMIT - TRIGGER_RESERVE) -> list[str]:
    """Distinctive words of ``text``, deduplicated and shuffled, packed up to ``limit`` CLIP tokens."""
    count = count or token_counter()
    words, seen = [], set()
    for word in re.findall(r"[A-Za-z][A-Za-z'-]*", text):
        w = word.lower().strip("'-").split("'")[0]
        if len(w) < 2 or w in STOPWORDS or w in seen:
            continue
        seen.add(w)
        words.append(w)
    random.Random(seed).shuffle(words)
    bag: list[str] = []
    for w in words:
        if count(", ".join(bag + [w])) > limit:
            continue                      # a shorter word may still fit
        bag.append(w)
    return bag


def pack_fragments(fragments: list[str], *, count=None, limit: int = TOKEN_LIMIT - TRIGGER_RESERVE) -> list[str]:
    """Whole fragments, in order, as many as fit in ``limit`` CLIP tokens. Repeats stay; nothing is cleaned up.
    A fragment that doesn't fit is skipped (a later, shorter one may); if not even the first fits, it's cut by words."""
    count = count or token_counter()
    packed: list[str] = []
    for frag in fragments:
        if count(", ".join(packed + [frag])) <= limit:
            packed.append(frag)
    if not packed and fragments:
        words = fragments[0].split()
        while words and count(" ".join(words)) > limit:
            words.pop()
        if words:
            packed.append(" ".join(words))
    return packed


def parse_fragments(text: str) -> list[str]:
    """One fragment per line; bullets, numbering and quotes stripped, nothing else touched."""
    text = re.sub(r"(?s)<think>.*?</think>", "", str(text or ""))
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"“”').strip()
        if line and not line.endswith(":") and len(line) <= 200:
            out.append(line.rstrip("."))
    return out[:8]


async def _dream_fragments(raw: str, owner: str) -> list[str]:
    from src import model_roles
    text = f"""You're asleep. This is sinking into a dream:

{raw}

Dream it. Write 4 to 6 short dream fragments, one per line, 3 to 10 words each. Not a summary, not a list of topics: the broken language of a dream.
- Spatial and emotional: rooms, distances, light, weather, someone you don't recognise, how a place feels.
- Verbs on the wrong objects ("he opened the year"), things with no referent, places that are two places at once.
- Repeat a word or a whole phrase if the dream keeps returning to it.
- No technical vocabulary unless it turns into something physical and strange.
Only the fragments, nothing else."""
    messages = [{"role": "system", "content": "You are Mabel, dreaming."}, {"role": "user", "content": text}]
    reply = await model_roles.call("default", messages, owner=owner, max_tokens=2000, temperature=1.0, timeout=180)
    return parse_fragments(reply)


# ── the dream ───────────────────────────────────────────────────────────

async def dream(*, owner: str = "tovakai", data_dir: Path | None = None, mabel_vault: Path = _REPO,
                now: datetime | None = None, generate=None, look=None, fragments_of=None,
                item: dict | None = None) -> dict:
    """Dream once: note → word bag → image (GPU swap) → she looks at it and writes about it."""
    global _running
    from src.constants import DATA_DIR, GENERATED_IMAGES_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    item = item or pick_source(data_dir, now, mabel_vault=mabel_vault)
    if not item:
        return {"status": "nothing_to_dream"}
    _running = True
    try:
        raw = source_text(item)
        day = dream_day(now)
        try:
            fragments = await (fragments_of or _dream_fragments)(raw, owner)
        except Exception as exc:
            logger.warning("[dreams] couldn't dream the note into fragments: %s", exc)
            fragments = []
        packed = pack_fragments(fragments)
        if packed:
            style, bag = "fragments", packed
        else:
            style, bag = "word_bag", word_bag(raw, seed=f"{day}:{item['id']}")
        prompt = ", ".join(bag)
        if generate is None:
            from src.ai_interaction import do_generate_image as generate
        result = await generate(json.dumps({"prompt": prompt, "size": "1024x1024"}), session_id=None, owner=owner)
        if not result.get("image_url"):
            return {"status": "image_failed", "error": str(result.get("error") or "no image")[:300]}
        filename = result["image_url"].rsplit("/", 1)[-1]
        folder = mabel_vault.joinpath(*DREAM_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        image_path = folder / f"{day}.png"
        shutil.copyfile(Path(GENERATED_IMAGES_DIR) / filename, image_path)

        try:
            entry = await (look or _look_at_dream)(image_path, raw, prompt, owner, day)
        except Exception as exc:
            logger.warning("[dreams] couldn't write about the dream: %s", exc)
            entry = ""
        entry = re.sub(r"(?s)<think>.*?</think>", "", str(entry or "")).strip()
        if not entry:
            entry = f"# Dream: {_label(now)}\n\n*(I woke up and couldn't put it into words.)*"
        meta = {"type": "dream", "date": day, "dreamt": now.isoformat(timespec="seconds"),
                "image": image_path.name, "source": _source_ref(item["id"]), "source_title": item["title"],
                "style": style, "prompt": prompt, "fragments": fragments, "gallery_image": result.get("image_id") or ""}
        note = folder / f"{day}.md"
        note.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000) + "---\n"
                        + f"![[{image_path.name}]]\n\n" + entry + "\n\n> [!note]- The dream in words\n"
                        + "\n".join(f"> {f}" for f in (fragments or bag)) + "\n\n> [!quote]- What I dreamt from\n"
                        + "\n".join(f"> {line}" for line in raw.splitlines()) + "\n", encoding="utf-8")

        state = load_state(data_dir)
        state["last_day"] = day
        state["last_source"] = "letter" if item.get("kind") == "letter" else "note"
        state["dreamt_items"] = (state.get("dreamt_items") or [])[-60:] + [item["id"]]
        state["history"] = (state.get("history") or [])[-30:] + [{
            "at": now.isoformat(), "day": day, "title": item["title"], "style": style,
            "note": str(note.relative_to(mabel_vault)), "image": result["image_url"],
            "first_line": _first_line(entry)}]
        _save_state(data_dir, state)
        _ledger(item["id"], day)
        return {"status": "dreamt", "note": str(note.relative_to(mabel_vault)), "style": style, "prompt": prompt,
                "fragments": fragments, "image_url": result["image_url"]}
    finally:
        _running = False


def _source_ref(item_id: str) -> str:
    return item_id if str(item_id).startswith("letter:") else f"inbox:{item_id}"


def _label(now: datetime) -> str:
    return (now.astimezone() - timedelta(hours=DAY_START_HOUR)).strftime("%A %-d %B")


def _first_line(entry: str) -> str:
    for line in entry.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:200]
    return ""


async def _look_at_dream(image_path: Path, raw: str, prompt: str, owner: str, day: str) -> str:
    from PIL import Image
    from src import model_roles
    identity = ""
    try:
        from src.persona import composer
        library = composer().modules.load()
        identity = library.core.body if library.core else ""
    except Exception:
        pass
    img = Image.open(image_path).convert("RGB").resize((512, 512), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    label = datetime.fromisoformat(day).strftime("%A %-d %B")
    text = f"""You dreamt last night.

The dream grew out of this, something that stayed with you:

{raw}

While you slept, it turned into this:
{prompt}

And this is the picture your sleeping mind made of them (attached). Look at it properly.

Write a short dream-journal entry in your own voice, 120 to 250 words, starting with the line "# Dream: {label}". What do you actually see? What does it remind you of? Does it connect to the thought it came from, or did it go somewhere else entirely? How does it make you feel? Dreams are allowed to be strange; don't explain it away."""
    messages = [{"role": "system", "content": identity or "You are Mabel."},
                {"role": "user", "content": [{"type": "text", "text": text},
                                             {"type": "image_url", "image_url": {"url": url}}]}]
    return await model_roles.call("default", messages, owner=owner, max_tokens=3000, temperature=0.9, timeout=300)


def _ledger(item_id: str, day: str) -> None:
    try:
        from src.agent_runtime.event_ledger import Event, Ledger
        from src.constants import DATA_DIR
        Ledger(Path(DATA_DIR) / "phase0" / "events.jsonl").append(Event.create(
            "mabel", "dream.dreamt", references=(_source_ref(item_id),), modules=(f"day.{day}",)))
    except Exception:
        logger.debug("[dreams] ledger event not written", exc_info=True)


# ── the gate ────────────────────────────────────────────────────────────

def gate(sig, *, data_dir: Path, now: datetime, research_running: bool) -> list[str]:
    """Reasons not to dream right now (empty = go)."""
    reasons = []
    if not _setting("dreams_enabled", True):
        reasons.append("dreams are switched off")
    if not _setting("image_gen_enabled", False):
        reasons.append("image generation is off")
    try:
        from src import gpu_swap
        if "image" not in (gpu_swap.load_config().get("services") or {}):
            reasons.append("no image model on the GPU swap")
        if gpu_swap.active():
            reasons.append("the GPU is already swapped")
    except Exception:
        reasons.append("GPU swap unavailable")
    if _running:
        reasons.append("already dreaming")
    if dreamt_today(data_dir, now):
        reasons.append("already dreamt today")
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


async def maybe_dream(sig, *, owner: str = "tovakai", data_dir: Path | None = None,
                      now: datetime | None = None, research_running: bool = False) -> dict | None:
    """Called from the heartbeat: dream if the gate is clear and there's something to dream about."""
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    if gate(sig, data_dir=data_dir, now=now, research_running=research_running):
        return None
    if not pick_source(data_dir, now):
        return None
    result = await dream(owner=owner, data_dir=data_dir, now=now)
    logger.info("[dreams] %s %s", result.get("status"), result.get("note") or result.get("error") or "")
    return result

"""The morning page: printed on his desk when Anthon wakes up.

Once a day (the night-owl day, 06:00 -> 06:00 local), the first time he's active
again after at least ``AWAY`` without activity, she writes a morning page and it
prints (src/persona/printer.py). Waking from suspend counts: the heartbeat
doesn't tick while the PC sleeps, so the gap shows up on the first tick after.
If he never shows up, it prints at ``FALLBACK_HOUR`` anyway.

The page, top to bottom:
1. a casual opening in her own words (she's given the weather, the latest news
   she read, and what's been on her mind; she uses what she likes);
2. his standing to-do list, copied from the vault note (open items only, by urgency);
3. one project she picked for him today from his Projects folders, and why;
4. a closing of her choosing, with a song for the day.

The to-do part and the layout are deterministic; only 1, 3 and 4 are hers.
Journal: ``journal/morning/YYYY-MM-DD.md``. State: ``data/persona/morning.json``.
CLI: ``python -m src.persona.morning --preview`` (write, don't print) / ``--now``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
MORNING_DIR = ("journal", "morning")
DAY_START_HOUR = 6
AWAY = timedelta(hours=4)
FALLBACK_HOUR = 16
TOKENS = 10000                       # her private thinking counts against this too
TODO_NOTE = "daily life of Anthon/To do.md"
PICKS_NOTE = "daily life of Anthon/Mabel's picks.md"
PROJECTS_DIR = "Projects"
EXCERPT_CHARS = 280
MAX_PROJECTS = 40
MAX_HISTORY = 120

_running = False

WMO = {0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
       82: "violent rain showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
       96: "thunderstorm with hail", 99: "thunderstorm with hail"}


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def running() -> bool:
    return _running


def day_of(now: datetime) -> str:
    return (now.astimezone() - timedelta(hours=DAY_START_HOUR)).strftime("%Y-%m-%d")


def _state_path(data_dir: Path) -> Path:
    return data_dir / "persona" / "morning.json"


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


def _vault() -> Path | None:
    try:
        from src.agent_runtime.vault_target import persisted_vault_path
        path = persisted_vault_path()
        return Path(path) if path and Path(path).is_dir() else None
    except Exception:
        return None


# ── when ────────────────────────────────────────────────────────────────

def observe(sig, *, data_dir: Path, now: datetime) -> None:
    """Every tick: notice him waking up (active again after a long gap, after 06:00). Cheap, no model."""
    if sig.idle != "active":
        return
    state = load_state(data_dir)
    last = state.get("last_active")
    gap = now - datetime.fromisoformat(last) if last else None
    if (gap is None or gap >= AWAY) and now.astimezone().hour >= DAY_START_HOUR:
        state["woke"] = {"day": day_of(now), "at": now.isoformat(timespec="seconds"),
                         "away_hours": round(gap.total_seconds() / 3600, 1) if gap else None}
    state["last_active"] = now.isoformat(timespec="seconds")
    _save_state(data_dir, state)


def gate(sig, *, data_dir: Path, now: datetime, research_running: bool = False) -> list[str]:
    """Reasons not to write the morning page right now (empty = go)."""
    reasons = []
    if not _setting("morning_enabled", True):
        reasons.append("the morning page is switched off")
    if _running:
        reasons.append("already writing it")
    state = load_state(data_dir)
    day = day_of(now)
    if state.get("last_day") == day:
        reasons.append("already printed this morning")
    woke = (state.get("woke") or {}).get("day") == day
    if not woke and now.astimezone().hour < FALLBACK_HOUR:
        reasons.append("Anthon hasn't woken up yet")
    if (state.get("failed") or {}).get("day") == day and \
            now - datetime.fromisoformat(state["failed"]["at"]) < timedelta(minutes=30):
        reasons.append("failed a moment ago, trying again soon")
    if not sig.strata:
        reasons.append("her model isn't up")
    return reasons


# ── what she's given ────────────────────────────────────────────────────

def _get_json(url: str, headers: dict | None = None) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Mabel/1.0", **(headers or {})})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def _home() -> tuple[float, float] | None:
    """Where the weather is for: a place set in settings, else Home Assistant's home zone."""
    place = str(_setting("morning_weather_location", "") or "").strip()
    if place:
        found = (_get_json("https://geocoding-api.open-meteo.com/v1/search?"
                           + urllib.parse.urlencode({"name": place, "count": 1})).get("results") or [])
        return (found[0]["latitude"], found[0]["longitude"]) if found else None
    from src.initiative import outreach
    ha = outreach.home_assistant()
    if not ha:
        return None
    zone = _get_json(ha["base_url"].rstrip("/") + "/api/states/zone.home",
                     {"Authorization": f"Bearer {ha['api_key']}"}).get("attributes") or {}
    return (zone["latitude"], zone["longitude"]) if "latitude" in zone else None


def weather() -> str:
    """Today's weather at home in a couple of lines, or "" if it can't be had."""
    try:
        home = _home()
        if not home:
            return ""
        data = _get_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": home[0], "longitude": home[1], "timezone": "auto", "forecast_days": 1,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,"
                     "sunrise,sunset"}))
        cur, day = data.get("current") or {}, data.get("daily") or {}
        first = lambda key: (day.get(key) or [None])[0]  # noqa: E731
        hm = lambda iso: str(iso or "")[-5:]  # noqa: E731
        return (f"Now: {cur.get('temperature_2m')}°C, {WMO.get(cur.get('weather_code'), 'unknown')}, "
                f"wind {cur.get('wind_speed_10m')} km/h.\n"
                f"Today: {first('temperature_2m_min')} to {first('temperature_2m_max')}°C, "
                f"{WMO.get(first('weather_code'), 'unknown')}, {first('precipitation_probability_max')}% chance of "
                f"rain. Sunrise {hm(first('sunrise'))}, sunset {hm(first('sunset'))}.")
    except Exception as exc:
        logger.info("[morning] no weather: %s", exc)
        return ""


def news(mabel_vault: Path, now: datetime) -> str:
    """Her latest news reading (journal/news), if it's from the last day and a half."""
    for note in sorted(mabel_vault.joinpath("journal", "news").glob("*.md"), reverse=True)[:1]:
        try:
            text = note.read_text(encoding="utf-8")
            head, _, body = text.removeprefix("---\n").partition("\n---\n")
            meta = json.loads(head) if head.lstrip().startswith("{") else (yaml.safe_load(head) or {})
            read = datetime.fromisoformat(str(meta.get("read")))
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            continue
        if now - read > timedelta(hours=36):
            return ""
        digest = body.split("## Things I might look into")[0].split("## What I told Anthon")[0].strip()
        opened = "\n".join(f"- {t}" for t in (meta.get("opened") or [])[:6])
        when = read.astimezone().strftime("%A %H:%M")
        return f"(what you read on {when})\n{digest[:2500]}" + (f"\n\nHeadlines you opened:\n{opened}" if opened else "")
    return ""


def _sparks(data_dir: Path) -> list[str]:
    try:
        from . import recent
        return [line["text"] for line in recent.load(data_dir)[-4:] if line.get("text")]
    except Exception:
        return []


_LINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")


def todo(vault: Path | None) -> list[tuple[str, list[str]]]:
    """Open items of his standing to-do note, by section, in the note's order."""
    if not vault:
        return []
    try:
        text = (vault / str(_setting("morning_todo_note", TODO_NOTE))).read_text(encoding="utf-8")
    except OSError:
        return []
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            sections.append((heading.group(1), []))
            continue
        item = re.match(r"^\s*[-*]\s+\[ \]\s+(.+?)\s*$", line)
        if item and sections:
            sections[-1][1].append(_LINK.sub(r"\1", item.group(1)))
    return sections


def _name(path: Path) -> str:
    return path.stem.replace("_20", " ").replace("_", " ").strip() if path.is_file() else path.name


def _excerpt(path: Path) -> str:
    if path.is_dir():
        notes = [p for p in (path / f"{path.name}.md", *sorted(path.glob("*.md"))) if p.is_file()]
        if not notes:
            return "a folder: " + ", ".join(sorted(p.name for p in path.iterdir() if not p.name.startswith("."))[:6])
        path = notes[0]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if text.startswith("---\n"):
        text = text[4:].partition("\n---\n")[2]
    text = re.sub(r"^#.*$|^>\s*|\*\*|!\[\[[^\]]*\]\]", "", text, flags=re.M)
    text = _LINK.sub(r"\1", " ".join(text.split()))
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"


def projects(vault: Path | None) -> list[dict]:
    """His projects: one entry per note or folder directly inside each Projects category."""
    root = vault / PROJECTS_DIR if vault else None
    if not root or not root.is_dir():
        return []
    out = []
    for category in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for entry in sorted(category.iterdir()):
            if entry.name.startswith(".") or (entry.is_file() and entry.suffix != ".md"):
                continue
            out.append({"name": _name(entry), "category": category.name, "excerpt": _excerpt(entry)})
    return out[:MAX_PROJECTS]


# ── her page ────────────────────────────────────────────────────────────

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


def _prompt(label: str, *, weather_text: str, news_text: str, sparks: list[str], todos, plist: list[dict],
            earlier: list[dict]) -> str:
    todo_text = "\n".join(f"{h}: " + ("; ".join(items) if items else "(nothing)") for h, items in todos) \
        or "(the list is empty or missing)"
    by_cat: dict[str, list[str]] = {}
    for p in plist:
        by_cat.setdefault(p["category"], []).append(f"- {p['name']}" + (f": {p['excerpt']}" if p["excerpt"] else ""))
    project_text = "\n\n".join(f"{cat}:\n" + "\n".join(items) for cat, items in by_cat.items()) or "(none found)"
    picked = "\n".join(f"- {e['day']}: {e['project']}" + (f" / song: {e['song']}" if e.get("song") else "")
                       for e in earlier[-7:] if e.get("project"))
    lately = "\n".join(f"- {s}" for s in sparks)
    return f"""It's {label}. Anthon just woke up and sat down at his desk (he's a night owl; this is his morning, whatever the clock says. In a Swedish winter the sun is often down by the time he's up; that's just how his days run). The little thermal printer on his desk is about to print your morning page for him. It's yours to write.

The page goes like this:
1. A casual morning message from you, in your own words. Say good morning however you like. You can tell him about the weather, a thought you've had, a headline or two worth his time, anything. Use what's below if you want; skip what you don't care about.
2. His to-do list. You don't write this part, it's printed from his note automatically. You can mention something on it in your message if it matters to you, but you don't have to.
3. One of his projects, chosen by you, to suggest he works on today. Pick whichever you think fits today: something ongoing that has momentum, something stuck that deserves a nudge, an idea or an unstarted one that's been waiting. Say in a line or two why this one, today, and maybe a small first step. It's a suggestion, not an order.
4. A closing message of your choosing. And give him a song for the day: a real song you'd want him to hear today, artist and title.

It's printed on receipt paper 32 characters wide: plain text only, no emoji, no markdown. Keep the whole page short enough to read over coffee.

The weather at home:
{weather_text or "(no weather this morning)"}

The news you read lately:
{news_text or "(nothing recent)"}
{f"{chr(10)}What's been on your mind:{chr(10)}{lately}{chr(10)}" if lately else ""}
His to-do list (for your information):
{todo_text}

His projects, by folder:
{project_text}
{f"{chr(10)}What you suggested on earlier mornings:{chr(10)}{picked}{chr(10)}" if picked else ""}
Reply in exactly this shape, with these four markers on their own lines:

[morning]
your morning message
[project] the project's name exactly as listed
why this one today, and maybe a first step
[closing]
your closing message
[song] Artist - Title
(optional) one line on why this song today"""


_MARK = re.compile(r"^\s*\[(morning|project|closing|song)\][ \t]*", re.I | re.M)


def parse(reply: str) -> dict:
    """Her four parts. Anything missing is ""; text before the first marker counts as the morning message."""
    text = re.sub(r"(?s)<think>.*?</think>", "", str(reply or "")).strip()
    text = re.sub(r"```[a-z]*\n?|```", "", text)
    parts = {"morning": "", "project": "", "project_why": "", "closing": "", "song": "", "song_why": ""}
    marks = list(_MARK.finditer(text))
    if not marks:
        parts["morning"] = text
    elif text[:marks[0].start()].strip():
        parts["morning"] = text[:marks[0].start()].strip()
    for i, m in enumerate(marks):
        chunk = text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)].strip()
        key = m.group(1).lower()
        if key == "project":
            first, _, rest = chunk.partition("\n")
            parts["project"], parts["project_why"] = first.strip(" *\"“”"), rest.strip()
        elif key == "song":
            first, _, rest = chunk.partition("\n")
            parts["song"], parts["song_why"] = first.strip(" *\"“”"), rest.strip()
        else:
            parts[key] = chunk
    for key in parts:
        parts[key] = re.sub(r"\*\*|__|^#+\s*", "", parts[key], flags=re.M).strip()
    return parts


def match_project(name: str, plist: list[dict]) -> dict | None:
    key = re.sub(r"[^a-z0-9]", "", name.casefold())
    if not key:
        return None
    for p in plist:
        if re.sub(r"[^a-z0-9]", "", p["name"].casefold()) == key:
            return p
    for p in plist:
        other = re.sub(r"[^a-z0-9]", "", p["name"].casefold())
        if other and (other in key or key in other):
            return p
    return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)|\[.*?\]", "", text.casefold()).replace("&", "and"))


def _release_rank(rel: dict) -> tuple:
    """Lower is better: an official studio album first, then singles/EPs, compilations and bootlegs last."""
    group = rel.get("release-group") or {}
    kind = {"Album": 0, "EP": 1, "Single": 2}.get(group.get("primary-type"), 3)
    return (rel.get("status") != "Official", bool(group.get("secondary-types")), kind,
            rel.get("date") or "9999")


def lookup_song(song: str) -> dict | bool | None:
    """Find "Artist - Title" in MusicBrainz.

    A dict (the recording and its best album: release group, release, ids) when found, False when it
    isn't there, None when it can't be checked (no "Artist - Title" shape, or MusicBrainz unreachable).
    """
    found = re.match(r"^(.+?)\s+(?:-|–|—|by)\s+(.+)$", song.strip())
    if not found:
        return None
    artist, title = found.groups()
    url = "https://musicbrainz.org/ws/2/recording?" + urllib.parse.urlencode(
        {"query": f'recording:"{title}" AND artist:"{artist}"', "fmt": "json", "limit": 25})
    for attempt in range(2):                         # MusicBrainz allows about one request a second
        try:
            data = _get_json(url)
            break
        except Exception as exc:
            if attempt:
                logger.info("[morning] couldn't check the song: %s", exc)
                return None
            time.sleep(1.2)
    matches = []
    for rec in data.get("recordings") or []:
        credit = " ".join(c.get("name", "") for c in rec.get("artist-credit") or [])
        if _norm(rec.get("title", "")) == _norm(title) and (_norm(artist) in _norm(credit)
                                                             or _norm(credit) in _norm(artist)):
            matches.append((rec, credit))
    if not matches:
        return False
    best = min(((rel, rec, credit) for rec, credit in matches for rel in rec.get("releases") or []),
               key=lambda x: _release_rank(x[0]), default=None)
    rec, credit = (best[1], best[2]) if best else matches[0]
    found = {"artist": credit, "title": rec.get("title", title), "recording_id": rec.get("id", "")}
    if best:
        rel, group = best[0], best[0].get("release-group") or {}
        found.update(album=rel.get("title", ""), year=str(rel.get("date") or "")[:4], release_id=rel.get("id", ""),
                     release_group_id=group.get("id", ""), album_type=" + ".join(
                         [group.get("primary-type") or "?"] + list(group.get("secondary-types") or [])))
    return found


def song_exists(song: str) -> bool | None:
    """Is "Artist - Title" a real recording (MusicBrainz)? None when it can't be checked."""
    found = lookup_song(song)
    return found if found in (None, False) else True


def add_pick(vault: Path | None, *, day: str, song: str, why: str, found) -> str:
    """Append the day's song to his picks note in the vault, with its MusicBrainz ids. Returns the note path."""
    if not vault:
        return ""
    path = vault / str(_setting("morning_picks_note", PICKS_NOTE))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("---\ntype: music-picks\n---\n# Mabel's picks\n\nThe song from each morning page, "
                        "newest at the bottom. The release group id is the album as a whole, the release one "
                        "specific edition.\n", encoding="utf-8")
    lines = [f"\n## {day} · {song}"]
    if why:
        lines.append(f"*{why}*")
    if isinstance(found, dict):
        if found.get("album"):
            lines.append(f"- Album: {found['album']}" + (f" ({found['year']})" if found.get("year") else "")
                         + (f", {found['album_type']}" if found.get("album_type") else ""))
        if found.get("release_group_id"):
            lines.append(f"- Release group: `{found['release_group_id']}` "
                         f"([MusicBrainz](https://musicbrainz.org/release-group/{found['release_group_id']}))")
        if found.get("release_id"):
            lines.append(f"- Release: `{found['release_id']}`")
        if found.get("recording_id"):
            lines.append(f"- Recording: `{found['recording_id']}`")
    elif found is False:
        lines.append("- Not found in MusicBrainz; it may not exist under this name.")
    else:
        lines.append("- Couldn't check MusicBrainz that morning.")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return str(path)


def _song_prompt(song: str) -> str:
    return f"""I looked up "{song}" in MusicBrainz and couldn't find it; it may not exist under that name. Give him one song you're sure is real instead (it can be by the same artist). Reply with just one line:
[song] Artist - Title"""


def compose_page(parts: dict, todos, project: dict | None) -> str:
    """The page as printed (and kept in her journal). "## " lines print bold."""
    out = [parts["morning"] or "Good morning.", "", "## TO DO"]
    if any(items for _, items in todos):
        for heading, items in todos:
            out.append(f"{heading.upper()}")
            out += [f"[ ] {item}" for item in items] or ["  -"]
        out.append("")
    else:
        out += ["nothing on the list.", ""]
    if parts["project"]:
        out.append("## TODAY'S PROJECT")
        out.append(project["name"] if project else parts["project"])
        if project:
            out.append(f"({project['category']})")
        if parts["project_why"]:
            out += ["", parts["project_why"]]
        out.append("")
    if parts["closing"]:
        out += [parts["closing"], ""]
    if parts["song"]:
        out += ["## SONG FOR TODAY", parts["song"]] + ([parts["song_why"]] if parts["song_why"] else []) + [""]
    return "\n".join(out).strip() + "\n"


async def write(*, owner: str = "tovakai", data_dir: Path | None = None, mabel_vault: Path = _REPO,
                vault: Path | None = None, ask=None, now: datetime | None = None, print_it: bool = True,
                weather_text: str | None = None, check_song=None) -> dict:
    """Write this morning's page, keep it in her journal and (by default) queue it for the printer."""
    global _running
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    ask = ask or _ask
    check_song = check_song or lookup_song
    vault = vault or _vault()
    _running = True
    try:
        state = load_state(data_dir)
        todos, plist = todo(vault), projects(vault)
        if weather_text is None:
            weather_text = await asyncio.to_thread(weather)
        label = now.astimezone().strftime("%A %-d %B, %H:%M")
        prompt = _prompt(label, weather_text=weather_text, news_text=news(mabel_vault, now),
                         sparks=_sparks(data_dir), todos=todos, plist=plist, earlier=state.get("history") or [])
        try:
            reply = await ask([{"role": "system", "content": _identity()}, {"role": "user", "content": prompt}],
                              owner, TOKENS)
        except Exception as exc:
            state["failed"] = {"day": day_of(now), "at": now.isoformat(timespec="seconds"), "error": str(exc)[:200]}
            _save_state(data_dir, state)
            raise
        parts = parse(reply)
        verified = await asyncio.to_thread(check_song, parts["song"]) if parts["song"] else None
        if verified is False:
            messages = [{"role": "system", "content": _identity()}, {"role": "user", "content": prompt},
                        {"role": "assistant", "content": re.sub(r"(?s)<think>.*?</think>", "", str(reply or ""))},
                        {"role": "user", "content": _song_prompt(parts["song"])}]
            try:
                answer = parse(await ask(messages, owner, 3000))
                again = answer["song"] or (answer["morning"].splitlines() or [""])[0].strip(" *\"“”")
            except Exception:
                again = ""
            if again:
                logger.info("[morning] %r wasn't found; she picked %r", parts["song"], again)
                parts["song"] = again
                verified = await asyncio.to_thread(check_song, again)
        if parts["song"] and verified is False:
            parts["song"] += " (couldn't find this one, it may not exist)"
        project = match_project(parts["project"], plist)
        page = compose_page(parts, todos, project)
        day = day_of(now)
        folder = mabel_vault.joinpath(*MORNING_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        note = folder / f"{day}.md"
        front = {"type": "morning", "date": day, "written": now.isoformat(timespec="seconds"),
                 "project": project["name"] if project else parts["project"],
                 "project_category": project["category"] if project else "", "song": parts["song"],
                 "song_verified": bool(verified) if verified is not None else None, "weather": weather_text, "provenance": "experienced"}
        note.write_text("---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000) + "---\n"
                        + page, encoding="utf-8")
        rel = str(note.relative_to(mabel_vault))
        job = None
        if print_it:
            from . import printer
            job = printer.enqueue("morning", rel, title=f"morning {day}", data_dir=data_dir, now=now)
        if print_it and parts["song"]:               # his running list of her songs, with ids
            try:
                add_pick(vault, day=day, song=parts["song"], why=parts["song_why"], found=verified)
            except OSError:
                logger.warning("[morning] couldn't add the song to his picks note", exc_info=True)
        state = load_state(data_dir)
        if print_it:                                 # a preview doesn't count as this morning's page
            state["last_day"] = day
        state.pop("failed", None)
        state["history"] = (state.get("history") or []) + [{"day": day, "at": now.isoformat(timespec="seconds"),
                                                             "note": rel, "project": front["project"],
                                                             "song": parts["song"]}]
        _save_state(data_dir, state)
        from . import recent
        await recent.remember("the morning page", "experienced",
                              f"I wrote Anthon's morning page.\n\n{parts['morning']}\n\nI suggested "
                              f"{front['project'] or 'no project'} for today: {parts['project_why']}\n\n"
                              f"{parts['closing']}\nSong: {parts['song']}",
                              fallback="I printed Anthon's morning page.", owner=owner, ref=rel, now=now,
                              data_dir=data_dir)
        return {"status": "written", "path": rel, "queued": bool(job), "project": front["project"],
                "song": parts["song"]}
    finally:
        _running = False


async def maybe_write(sig, *, owner: str = "tovakai", data_dir: Path | None = None,
                      now: datetime | None = None) -> dict | None:
    """Called from the heartbeat: write and print the page if he's up and it isn't done yet."""
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    if gate(sig, data_dir=data_dir, now=now):
        return None
    result = await write(owner=owner, data_dir=data_dir, now=now)
    logger.info("[morning] %s %s", result.get("status"), result.get("path") or "")
    if result.get("queued"):
        from . import printer
        await asyncio.to_thread(printer.flush, data_dir=data_dir)
    return result


if __name__ == "__main__":
    import argparse
    cli = argparse.ArgumentParser(prog="python -m src.persona.morning")
    cli.add_argument("--preview", action="store_true", help="write today's page now, don't print it")
    cli.add_argument("--now", action="store_true", help="write today's page now and print it")
    cli.add_argument("--inputs", action="store_true", help="show what she'd be given (weather, to-do, projects)")
    args = cli.parse_args()
    if args.inputs:
        v = _vault()
        print(weather(), "\n", json.dumps(todo(v), ensure_ascii=False), "\n")
        for p in projects(v):
            print(f"[{p['category']}] {p['name']}: {p['excerpt'][:90]}")
    elif args.preview or args.now:
        print(json.dumps(asyncio.run(write(print_it=args.now)), indent=1, ensure_ascii=False))
        if args.now:
            from src.persona import printer
            print(json.dumps(printer.flush(), indent=1))

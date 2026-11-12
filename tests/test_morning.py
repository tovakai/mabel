"""The morning page: noticing him wake, the to-do and project inputs, her reply, the page, the print."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.persona import morning, printer

LOCAL = datetime.now().astimezone().tzinfo


def at(hour, day=20, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=LOCAL)


def _sig(**kw):
    return SimpleNamespace(**{**dict(idle="active", idle_minutes=0.0, strata=True, chat_active=True), **kw})


TODO = """---
type: todo
---
# To do

Tick things off.

## Urgent
- [ ] Call CSN
- [x] Pay rent
## Soon
- [ ] Return [[Inventory/drill|the drill]]
## Whenever
"""

REPLY = """<think>let me see</think>
[morning]
Morning. It's grey and wet out, 6 degrees.
The Arlington arch fight is still going.
[project] Project Glasshouse
The glass panel has waited long enough. Measure the block first.
[closing]
Drink water before coffee.
[song] Boards of Canada - Dayvan Cowboy
"""


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "daily life of Anthon").mkdir(parents=True)
    (v / "daily life of Anthon" / "To do.md").write_text(TODO)
    (v / "Projects" / "unstarted projects").mkdir(parents=True)
    (v / "Projects" / "unstarted projects" / "project_glasshouse.md").write_text(
        "---\ntype: project\n---\n# Project Glasshouse\n\n> **Quest:** Put the glass panel back on.\n")
    (v / "Projects" / "ongoing projects" / "Frametop" / "Projects").mkdir(parents=True)
    (v / "Projects" / "unfinished projects").mkdir(parents=True)
    (v / "Projects" / "unfinished projects" / "Going_20Merry_20-_20Model.md").write_text("Small model.")
    return v


@pytest.fixture
def quiet(monkeypatch):
    lines = []

    async def remember(*args, **kw):
        lines.append((args, kw))
    monkeypatch.setattr("src.persona.recent.remember", remember)
    monkeypatch.setattr(morning, "_identity", lambda: "You are Mabel.")
    monkeypatch.setattr(morning, "_sparks", lambda data_dir: [])
    monkeypatch.setattr(morning, "_setting", lambda key, default: default)
    monkeypatch.setattr(printer, "_setting", lambda key, default: default)
    return lines


# ── when ────────────────────────────────────────────────────────────────

def test_waking_after_a_long_gap_opens_the_gate(tmp_path, quiet):
    morning.observe(_sig(), data_dir=tmp_path, now=at(3))                   # up late, last seen 03:00
    assert "Anthon hasn't woken up yet" in morning.gate(_sig(), data_dir=tmp_path, now=at(3, 20, 30))
    morning.observe(_sig(idle="idle"), data_dir=tmp_path, now=at(8))         # idle ticks don't count
    assert morning.load_state(tmp_path)["last_active"] == at(3).isoformat()
    morning.observe(_sig(), data_dir=tmp_path, now=at(13))                  # back after 10 h
    assert morning.load_state(tmp_path)["woke"]["day"] == "2026-11-20"
    assert morning.gate(_sig(), data_dir=tmp_path, now=at(13)) == []
    assert "her model isn't up" in morning.gate(_sig(strata=False), data_dir=tmp_path, now=at(13))


def test_short_breaks_and_all_nighters_dont_count_as_waking(tmp_path, quiet):
    for hour in (5, 6, 7, 8, 9):                                              # active straight through 06:00
        morning.observe(_sig(), data_dir=tmp_path, now=at(hour))
    assert morning.gate(_sig(), data_dir=tmp_path, now=at(9))  == ["Anthon hasn't woken up yet"]
    assert morning.gate(_sig(), data_dir=tmp_path, now=at(16)) == []          # the fallback hour


def test_waking_before_six_is_still_last_night(tmp_path, quiet):
    morning.observe(_sig(), data_dir=tmp_path, now=at(20, 19))
    morning.observe(_sig(), data_dir=tmp_path, now=at(4, 20))              # 9 h later, but before 06:00
    assert morning.load_state(tmp_path)["woke"]["at"] == at(20, 19).isoformat()   # (first sighting ever)


def test_once_a_day(tmp_path, quiet):
    morning._save_state(tmp_path, {"last_day": "2026-11-20", "woke": {"day": "2026-11-20"}})
    assert "already printed this morning" in morning.gate(_sig(), data_dir=tmp_path, now=at(17))
    assert morning.gate(_sig(), data_dir=tmp_path, now=at(5, 21)) != []        # 05:00 next day is still the 20th
    assert morning.day_of(at(5, 21)) == "2026-11-20" and morning.day_of(at(6, 21)) == "2026-11-21"


# ── inputs ──────────────────────────────────────────────────────────────

def test_todo_keeps_open_items_by_section(vault):
    assert morning.todo(vault) == [("Urgent", ["Call CSN"]), ("Soon", ["Return the drill"]), ("Whenever", [])]
    assert morning.todo(None) == []


def test_projects_list_notes_and_folders(vault):
    found = {p["name"]: p for p in morning.projects(vault)}
    assert set(found) == {"project glasshouse", "Frametop", "Going Merry - Model"}
    assert found["project glasshouse"]["category"] == "unstarted projects"
    assert "Quest: Put the glass panel back on." in found["project glasshouse"]["excerpt"]
    assert found["Frametop"]["excerpt"] == "a folder: Projects"


# ── her page ────────────────────────────────────────────────────────────

def test_parse_and_match():
    parts = morning.parse(REPLY)
    assert parts["morning"].startswith("Morning. It's grey") and "<think>" not in parts["morning"]
    assert parts["project"] == "Project Glasshouse" and parts["project_why"].startswith("The glass panel")
    assert parts["closing"] == "Drink water before coffee."
    assert parts["song"] == "Boards of Canada - Dayvan Cowboy"
    plist = [{"name": "project glasshouse", "category": "unstarted projects"}]
    assert morning.match_project("Project Glasshouse", plist) == plist[0]
    assert morning.match_project("glasshouse", plist) == plist[0]
    assert morning.match_project("Koi", plist) is None
    assert morning.parse("just a hello")["morning"] == "just a hello"


def test_page_layout():
    parts = morning.parse(REPLY)
    page = morning.compose_page(parts, [("Urgent", ["Call CSN"]), ("Soon", [])],
                                {"name": "project glasshouse", "category": "unstarted projects"})
    assert page.index("Morning.") < page.index("## TO DO") < page.index("[ ] Call CSN") \
        < page.index("## TODAY'S PROJECT") < page.index("Drink water") < page.index("## SONG FOR TODAY")
    assert "SOON\n  -" in page and "(unstarted projects)" in page
    assert "nothing on the list." in morning.compose_page(parts, [], None)


def test_write_keeps_the_page_and_prints_it(tmp_path, vault, quiet):
    seen = []

    async def ask(messages, owner, max_tokens):
        seen.append(messages)
        return REPLY
    data, journal = tmp_path / "data", tmp_path / "mabel"
    now = at(13)
    preview = asyncio.run(morning.write(data_dir=data, mabel_vault=journal, vault=vault, ask=ask, now=now,
                                        print_it=False, weather_text="Now: 6C, rain.", check_song=lambda s: None))
    assert not preview["queued"] and "last_day" not in morning.load_state(data)

    result = asyncio.run(morning.write(data_dir=data, mabel_vault=journal, vault=vault, ask=ask, now=now,
                                       weather_text="Now: 6C, rain.", check_song=lambda s: True))
    prompt = seen[0][1]["content"]
    for must in ("Now: 6C, rain.", "Call CSN", "project glasshouse", "Going Merry - Model", "[song]",
                 "32 characters"):
        assert must in prompt
    assert result["project"] == "project glasshouse" and result["song"] == "Boards of Canada - Dayvan Cowboy"
    state = morning.load_state(data)
    assert state["last_day"] == "2026-11-20" and state["history"][-1]["project"] == "project glasshouse"
    job = printer.load_state(data)["pending"][0]
    assert job["kind"] == "morning"
    out = printer.render(job, journal)
    assert b"good morning" in out and b"[ ] Call CSN" in out and b"\x1bE\x01TO DO" in out
    assert b"Boards of Canada - Dayvan Cowboy" in out and b"##" not in out
    asyncio.run(morning.write(data_dir=data, mabel_vault=journal, vault=vault, ask=ask, now=at(14),
                              weather_text="", check_song=lambda s: None))
    assert "project glasshouse" in seen[-1][1]["content"].split("What you suggested on earlier mornings:")[1]


def test_an_invented_song_gets_one_second_chance(tmp_path, vault, quiet):
    replies = [REPLY.replace("Boards of Canada - Dayvan Cowboy", "Portishead - Humming Man"), "[song] Portishead - Humming"]
    seen = []

    async def ask(messages, owner, max_tokens):
        seen.append(messages)
        return replies.pop(0)
    real = {"Portishead - Humming"}
    result = asyncio.run(morning.write(data_dir=tmp_path, mabel_vault=tmp_path / "m", vault=vault, ask=ask,
                                       now=at(13), weather_text="", check_song=lambda s: s in real))
    assert result["song"] == "Portishead - Humming"
    assert "couldn't find it" in seen[1][-1]["content"] and "Humming Man" in seen[1][-1]["content"]

    replies[:] = [REPLY.replace("Boards of Canada - Dayvan Cowboy", "Nobody - Nothing"), "[song] Still - Nothing"]
    result = asyncio.run(morning.write(data_dir=tmp_path, mabel_vault=tmp_path / "m", vault=vault, ask=ask,
                                       now=at(14), weather_text="", check_song=lambda s: False))
    assert result["song"].startswith("Still - Nothing (couldn't find this one")


def test_song_lookup_matches_artist_and_title(monkeypatch):
    hit = {"recordings": [{"title": "Humming", "artist-credit": [{"name": "Portishead"}]}]}
    monkeypatch.setattr(morning, "_get_json", lambda url, headers=None: hit)
    assert morning.song_exists("Portishead - Humming") is True
    assert morning.song_exists("Portishead - Humming Man") is False
    assert morning.song_exists("Massive Attack - Humming") is False
    assert morning.song_exists("no separator here") is None

    def down(url, headers=None):
        raise OSError("offline")
    monkeypatch.setattr(morning, "_get_json", down)
    assert morning.song_exists("Portishead - Humming") is None


def _rel(rid, title, status, date, gid, ptype, secondary=()):
    return {"id": rid, "title": title, "status": status, "date": date,
            "release-group": {"id": gid, "primary-type": ptype, "secondary-types": list(secondary)}}


def test_lookup_prefers_the_official_studio_album(monkeypatch):
    data = {"recordings": [
        {"id": "rec-1", "title": "Humming", "artist-credit": [{"name": "Portishead"}],
         "releases": [_rel("r-boot", "Millennium Hits", "Bootleg", "", "g-boot", "Album", ["Compilation"])]},
        {"id": "rec-2", "title": "Humming", "artist-credit": [{"name": "Portishead"}],
         "releases": [_rel("r-live", "Roseland NYC Live", "Official", "1998", "g-live", "Album", ["Live"]),
                      _rel("r-lp", "Portishead", "Official", "1997-09-29", "g-lp", "Album")]},
        {"id": "rec-3", "title": "Humming (remix)", "artist-credit": [{"name": "Portishead"}], "releases": []}]}
    monkeypatch.setattr(morning, "_get_json", lambda url, headers=None: data)
    found = morning.lookup_song("Portishead - Humming")
    assert found == {"artist": "Portishead", "title": "Humming", "recording_id": "rec-2", "album": "Portishead",
                     "year": "1997", "release_id": "r-lp", "release_group_id": "g-lp", "album_type": "Album"}
    assert morning.lookup_song("Portishead - Roads") is False


def test_picks_note_collects_songs_with_ids(vault):
    found = {"artist": "Portishead", "title": "Humming", "recording_id": "rec-2", "album": "Portishead",
             "year": "1997", "release_id": "r-lp", "release_group_id": "g-lp", "album_type": "Album"}
    path = morning.add_pick(vault, day="2026-11-12", song="Portishead - Humming", why="rain music", found=found)
    morning.add_pick(vault, day="2026-11-13", song="Nobody - Nothing", why="", found=False)
    text = open(path).read()
    assert text.startswith("---\ntype: music-picks\n---\n# Mabel's picks")
    assert "## 2026-11-12 · Portishead - Humming\n*rain music*\n- Album: Portishead (1997), Album\n" in text
    assert "- Release group: `g-lp` ([MusicBrainz](https://musicbrainz.org/release-group/g-lp))" in text
    assert "- Release: `r-lp`" in text and "- Recording: `rec-2`" in text
    assert text.index("2026-11-12") < text.index("2026-11-13") and "Not found in MusicBrainz" in text
    assert morning.add_pick(None, day="d", song="s", why="", found=None) == ""


def test_the_real_page_adds_to_his_picks(tmp_path, vault, quiet):
    async def ask(messages, owner, max_tokens):
        return REPLY.replace("Dayvan Cowboy\n", "Dayvan Cowboy\nbecause it sounds like leaving the house\n")
    found = {"recording_id": "rec", "release_group_id": "g", "release_id": "r", "album": "The Campfire Headphase",
             "year": "2005", "album_type": "Album"}
    asyncio.run(morning.write(data_dir=tmp_path, mabel_vault=tmp_path / "m", vault=vault, ask=ask, now=at(13),
                              weather_text="", check_song=lambda s: found, print_it=False))
    assert not (vault / morning.PICKS_NOTE).exists()                       # a preview adds nothing
    result = asyncio.run(morning.write(data_dir=tmp_path, mabel_vault=tmp_path / "m", vault=vault, ask=ask,
                                       now=at(13), weather_text="", check_song=lambda s: found))
    text = (vault / morning.PICKS_NOTE).read_text()
    assert "Boards of Canada - Dayvan Cowboy\n*because it sounds like leaving the house*" in text
    assert "Release group: `g`" in text
    page = (tmp_path / "m" / result["path"]).read_text()
    assert "Dayvan Cowboy\nbecause it sounds like leaving the house" in page and "song_verified: true" in page

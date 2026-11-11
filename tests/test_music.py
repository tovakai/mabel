"""Composing music: the notation, MIDI out and back, the activity with a fake model, the gate, the synth ports."""
import asyncio
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from types import SimpleNamespace

import pytest

from src.persona import music

NOW = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)

SCORE = """Here's what came out.

```score
title: Corridor Lights
tempo: 120
meter: 4/4
about: walking somewhere at night

[lead] program=80
// the hook
| A4:1 C5:1 E5:2 | { F#4:0.5 Bb4:0.5 r:1 }x2 |
[pad] program=89
| D3+F3+A3:4@60 | r:4 |
[drums]
{ | kick:1 hat:0.5 hat:0.5 snare:1 openhat:1 | }x2
[voice]
| C5:2 D5:2 | E5:4 |
```

It felt like a hallway with the lights humming.
"""


def _sig(**kw):
    base = dict(chat_active=False, idle="idle", idle_minutes=45, gpu="free", gpu_reason="", strata=True)
    return SimpleNamespace(**{**base, **kw})


def test_parse_reads_parts_chords_repeats_drums_and_voice():
    score = music.parse(SCORE)
    assert (score.title, score.tempo, score.meter, score.about) == ("Corridor Lights", 120, (4, 4), "walking somewhere at night")
    lead, pad, drums, voice = score.parts
    assert lead.program == 80 and lead.beats == 8
    assert [n[2] for n in lead.notes][:4] == [(69,), (72,), (76,), (66,)]          # A4=69, F#4=66
    assert lead.notes[4][2] == (70,)                                               # Bb4
    assert pad.notes[0] == (Fraction(0), Fraction(4), (50, 53, 57), 60)
    assert drums.drums and [n[2][0] for n in drums.notes[:5]] == [36, 42, 42, 38, 46]
    assert voice.voice and not voice.drums
    assert score.beats == 8 and score.seconds == 4


@pytest.mark.parametrize("bad, words", [
    ("title: x\n", "no parts"),
    ("[a]\nC4\n", "no length"),
    ("[a]\nH4:1 C4:8\n", "isn't a note"),
    ("[a]\nC4:0 C4:8\n", "doesn't work"),
    ("[a]\nC4:1\n", "at least"),
    ("tempo: 300\n[a]\nC4:8\n", "between 30"),
    ("[a]\n{ C4:8\n", "isn't closed"),
    ("[voice]\nC4:8\n[miku]\nC4:8\n", "only one"),
    ("tempo: 30\n[a]\n{ C4:4 }x64\n", "too long"),
])
def test_parse_explains_what_is_wrong(bad, words):
    with pytest.raises(music.ScoreError, match=words):
        music.parse(bad)


def test_midi_round_trip_keeps_timing_and_channels():
    score = music.parse(SCORE)
    events = music.read_midi(music.to_midi(score))
    ons = [(t, m) for t, m in events if m[0] & 0xF0 == 0x90]
    assert ons[0][0] == 0
    channels = {m[0] & 0x0F for _, m in ons}
    lead_ch = next(m[0] & 0x0F for _, m in ons if m[1] == 69)
    assert 9 in channels and 0 in channels                          # drums, voice
    assert lead_ch not in (0, 9)                                    # instruments stay off Miku's channel
    programs = [m for _, m in events if m[0] & 0xF0 == 0xC0]
    assert {m[1] for m in programs} == {80, 89} and all(m[0] & 0x0F not in (0, 9) for m in programs)
    snare = next(t for t, m in ons if m[0] & 0x0F == 9 and m[1] == 38)
    assert abs(snare - 1.0) < 1e-6                                  # beat 3 at 120 bpm
    offs = sorted(t for t, m in events if m[0] & 0xF0 == 0x80)
    assert offs[-1] <= 4.0 + 1e-6


def _fake(replies, seen):
    async def ask(messages, owner, max_tokens):
        seen.append(messages)
        return replies.pop(0)
    return ask


@pytest.fixture
def quiet(monkeypatch):
    lines = []

    async def remember(*args, **kw):
        lines.append((args, kw))
    monkeypatch.setattr("src.persona.recent.remember", remember)
    monkeypatch.setattr(music, "_identity", lambda: "You are Mabel.")
    monkeypatch.setattr(music, "_tell_gates", lambda now: ["no outreach in tests"])
    return lines


def test_she_can_say_not_now(tmp_path, quiet):
    seen = []
    result = asyncio.run(music.compose(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, ask=_fake(["<think>hm</think>Not now.", ], seen)))
    assert result == {"status": "not_now"} and len(seen) == 1
    assert music.load_state(tmp_path)["history"][-1]["outcome"] == "not_now"
    assert not (tmp_path / "journal" / "music").exists()


def test_a_piece_is_written_and_a_broken_score_gets_one_fix(tmp_path, quiet):
    seen = []
    replies = ["Something about corridors at night.", "```score\n[a]\nC4\n```", SCORE]
    result = asyncio.run(music.compose(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, ask=_fake(replies, seen)))
    assert result["status"] == "written" and result["title"] == "Corridor Lights"
    assert "no length" in seen[2][-1]["content"]                       # her mistake was explained to her
    folder = tmp_path / "journal" / "music"
    mid = folder / result["midi"]
    assert mid.read_bytes()[:4] == b"MThd"
    note = (folder / result["midi"]).with_suffix(".md").read_text()
    assert "type: composition" in note and "hallway with the lights humming" in note and "```score" in note
    assert music.pieces(tmp_path)[0]["title"] == "Corridor Lights"
    assert quiet and quiet[0][0][1] == "experienced"
    again = asyncio.run(music.compose(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW,
                                      ask=_fake(["More.", SCORE], [])))
    assert again["midi"] != result["midi"]                             # same day, same title: no overwrite


def test_two_broken_scores_fail_without_writing(tmp_path, quiet):
    replies = ["An idea.", "[a]\nC4\n", "[a]\nC4\n"]
    result = asyncio.run(music.compose(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, ask=_fake(replies, [])))
    assert result["status"] == "failed"
    assert not list((tmp_path).rglob("*.mid"))


def test_gate(tmp_path):
    assert music.gate(_sig(), data_dir=tmp_path, now=NOW, research_running=False) == []
    music._save_state(tmp_path, {"last": (NOW - timedelta(hours=10)).isoformat()})
    assert music.gate(_sig(), data_dir=tmp_path, now=NOW, research_running=False) == ["composed (or was offered to) recently"]
    music._save_state(tmp_path, {"last": (NOW - timedelta(days=3)).isoformat()})
    reasons = music.gate(_sig(idle="active", gpu="busy", chat_active=None), data_dir=tmp_path, now=NOW,
                         research_running=True)
    assert len(reasons) == 4


def test_ports_and_picking_the_synth(tmp_path):
    snd, proc = tmp_path / "snd", tmp_path / "asound"
    snd.mkdir()
    (snd / "midiC2D0").touch()
    (snd / "pcmC0D0p").touch()
    (proc / "card2").mkdir(parents=True)
    (proc / "card2" / "id").write_text("NSX39\n")
    found = music.ports(snd, proc)
    assert found == [{"path": str(snd / "midiC2D0"), "name": "NSX39"}]
    assert music.pick_port("nsx", found) == found[0]
    assert music.pick_port("", found) == found[0]
    assert music.pick_port("juno", found) is None
    assert music.pick_port("", []) is None


def test_midi_path_stays_in_her_music_folder(tmp_path):
    folder = tmp_path / "journal" / "music"
    folder.mkdir(parents=True)
    (folder / "a.mid").write_bytes(b"MThd")
    (tmp_path / "secret.mid").write_bytes(b"x")
    assert music.midi_path("a.mid", tmp_path) == folder / "a.mid"
    for name in ("../../secret.mid", "a.md", "missing.mid"):
        with pytest.raises(FileNotFoundError):
            music.midi_path(name, tmp_path)


def test_player_writes_to_the_port_and_silences_at_the_end(tmp_path):
    mid = tmp_path / "p.mid"
    mid.write_bytes(music.to_midi(music.parse("tempo: 240\n[a] program=5\nC4:1 D4:1 E4:1 F4:1 G4:4\n")))
    out = tmp_path / "port"
    out.touch()
    player = music.Player()
    player.play(mid, {"path": str(out), "name": "fake"})
    player._thread.join(timeout=5)
    data = out.read_bytes()
    assert bytes([0xC1, 5]) in data and bytes([0x91, 60, 84]) in data
    assert data.endswith(bytes([0xBF, 123, 0, 0xBF, 64, 0]))


def test_split_why_is_optional_and_only_at_a_line_start():
    assert music.split_why("Rain on a window.") == ("Rain on a window.", "")
    assert music.split_why("Rain.\n**Why:** it's been raining all week.") == ("Rain.", "it's been raining all week.")
    assert music.split_why("Rain.\nWhy I chose this: the week.") == ("Rain.", "the week.")
    assert music.split_why("I wonder why: no.")[1] == ""


def test_she_can_say_why_she_declines(tmp_path, quiet):
    result = asyncio.run(music.compose(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW,
                                       ask=_fake(["Not now. I'd rather keep reading.", ], [])))
    assert result == {"status": "not_now", "why": "I'd rather keep reading."}
    result = asyncio.run(music.compose(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW,
                                       ask=_fake(["not now\nWhy: tired of my own sounds.", ], [])))
    assert result["why"] == "tired of my own sounds."
    assert music.load_state(tmp_path)["history"][-1]["why"] == "tired of my own sounds."


def test_her_reasons_go_into_the_note(tmp_path, quiet):
    piece = SCORE + "\nWhy: the voice carries the tune because I wanted it to sound like someone humming alone.\n"
    replies = ["Corridors at night.\nWhy: I dreamt of hallways again.", piece]
    result = asyncio.run(music.compose(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, ask=_fake(replies, [])))
    note = (tmp_path / result["path"]).read_text()
    assert "What I had in mind: Corridors at night.\n" in note
    assert "Why I chose to compose: I dreamt of hallways again." in note
    assert "## Why I made it this way\n\nthe voice carries the tune" in note
    assert note.index("## Why I made it this way") < note.index("## Score")
    plain = asyncio.run(music.compose(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, ask=_fake(["Rain.", SCORE], [])))
    assert "Why" not in (tmp_path / plain["path"]).read_text().split("## Score")[0]


@pytest.fixture
def phone(monkeypatch, tmp_path):
    from src.initiative import outreach
    sent = {"chats": [], "pushes": []}
    store = outreach.Store(tmp_path / "outreach.json")
    monkeypatch.setattr(music, "_tell_gates", lambda now: [])
    monkeypatch.setattr(outreach, "default_store", lambda: store)
    monkeypatch.setattr(outreach, "create_chat", lambda owner, title, message, meta: sent["chats"].append(
        (title, message, meta)) or "sess-1")
    monkeypatch.setattr(outreach, "send_notification", lambda message, link, tag: sent["pushes"].append(message) or True)
    monkeypatch.setattr(outreach, "_ledger", lambda *a, **k: None)
    sent["store"] = store
    return sent


def test_she_can_choose_to_tell_him(tmp_path, quiet, phone):
    replies = ["Rain.", SCORE, "I wrote you a little thing with Miku humming in it.\nWhy: I think you'd like the voice."]
    result = asyncio.run(music.compose(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, ask=_fake(replies, [])))
    assert result["told"] == "sent"
    title, message, meta = phone["chats"][0]
    assert title == "🎵 Corridor Lights" and message == "I wrote you a little thing with Miku humming in it."
    assert meta["outreach_source"] == "music" and "Corridor Lights" in meta["background_tool_result"]["report"]
    sent = phone["store"].load()["sent"][-1]
    assert sent["source"] == "music" and sent["why"] == "I think you'd like the voice."   # counts toward his limits
    note = (tmp_path / result["path"]).read_text()
    assert "## What I told Anthon\n\nI wrote you a little thing" in note
    assert note.index("What I told Anthon") < note.index("## Score")


def test_she_can_keep_it_to_herself(tmp_path, quiet, phone):
    replies = ["Rain.", SCORE, "no\nWhy: it isn't finished in my head yet."]
    result = asyncio.run(music.compose(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, ask=_fake(replies, [])))
    assert result["told"] == "kept_to_herself" and not phone["chats"] and not phone["pushes"]
    assert "I kept this one to myself" in (tmp_path / result["path"]).read_text()
    assert music.load_state(tmp_path)["history"][-1]["told_why"] == "it isn't finished in my head yet."


def test_she_is_not_asked_when_his_limits_say_no(tmp_path, quiet):
    seen = []
    result = asyncio.run(music.compose(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, ask=_fake(["Rain.", SCORE], seen)))
    assert result["told"] == "not_offered" and len(seen) == 2
    assert "What I told" not in (tmp_path / result["path"]).read_text()

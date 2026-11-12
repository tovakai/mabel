"""The desk printer and her paper notes: rendering, the print queue, the activity with a fake model, the gate."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.persona import paper, printer

NOW = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)

DREAM = """---
type: dream
date: '2026-11-11'
image: 2026-11-11.png
fragments:
- rain opens the hallway
- light owes rain
---
![[2026-11-11.png]]

# Dream: Wednesday 11 November

I see a hallway that isn’t a hallway — a cyan bottle, a witness.

> [!note]- The dream in words
> rain opens the hallway

> [!quote]- What I dreamt from
> # Letter from Tova
> private letter text
"""


def _sig(**kw):
    base = dict(chat_active=False, idle="idle", idle_minutes=45, gpu="free", gpu_reason="", strata=True)
    return SimpleNamespace(**{**base, **kw})


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
    monkeypatch.setattr(paper, "_identity", lambda: "You are Mabel.")
    monkeypatch.setattr(paper, "_sparks", lambda data_dir: [])
    monkeypatch.setattr(printer, "_setting", lambda key, default: default)
    monkeypatch.setattr(paper, "_setting", lambda key, default: default)
    return lines


# ── rendering ───────────────────────────────────────────────────────────

def test_wrap_keeps_her_lines_and_fits_the_paper():
    text = "Dear Anthon,\n\n" + "word " * 20 + "\n  /\\_/\\\n ( o.o )"
    out = printer.wrap(text)
    assert all(len(line) <= printer.COLS for line in out.splitlines())
    assert out.startswith("Dear Anthon,\n\nword")
    assert "  /\\_/\\\n ( o.o )\n" in out


def test_ascii_turns_typography_plain():
    assert printer.to_ascii("isn’t — “so” …") == "isn't -- \"so\" ..."
    assert printer.to_ascii("a 🌧 b") == "a  b"


def test_raster_packs_black_pixels_msb_first():
    from PIL import Image
    img = Image.new("1", (10, 1), 1)
    img.putpixel((0, 0), 0)
    img.putpixel((9, 0), 0)
    assert printer.raster(img) == b"\x1dv0\x00" + bytes([2, 0, 1, 0]) + bytes([0x80, 0x40])


def test_dream_print_has_her_words_but_not_the_source(tmp_path):
    from PIL import Image
    note = tmp_path / "2026-11-11.md"
    note.write_text(DREAM, encoding="utf-8")
    Image.new("RGB", (64, 64), (200, 120, 90)).save(tmp_path / "2026-11-11.png")
    out = printer.render_dream(note)
    assert b"a dream" in out and b"Wednesday 11 November" in out
    assert b"~ rain opens the hallway ~" in out
    assert b"hallway -- a cyan bottle" in out
    assert b"\x1dv0\x00" + bytes([48, 0]) in out          # 384 dots wide
    assert b"private letter text" not in out and b"[!note]" not in out


def test_render_refuses_paths_outside_the_vault(tmp_path):
    (tmp_path / "outside.md").write_text("secret")
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(FileNotFoundError):
        printer.render({"kind": "paper", "ref": "../outside.md"}, vault)


# ── the queue ───────────────────────────────────────────────────────────

def _note(vault, text="hello there\n"):
    folder = vault / "journal" / "paper"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "n.md").write_text(f"---\ntype: paper\nwritten: '{NOW.isoformat()}'\n---\n{text}")
    return "journal/paper/n.md"


def test_queue_waits_for_the_printer_then_prints(tmp_path, quiet):
    ref = _note(tmp_path)
    printer.enqueue("paper", ref, data_dir=tmp_path, now=NOW)
    printer.enqueue("paper", ref, data_dir=tmp_path, now=NOW)       # same note twice: queued once
    assert len(printer.load_state(tmp_path)["pending"]) == 1

    def off(data):
        raise printer.PrinterUnavailable("bluetooth: host is down")
    assert printer.flush(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, sender=off)[0]["outcome"] == "waiting"
    assert not printer.has_due(tmp_path, NOW + timedelta(minutes=1))   # backs off a little
    assert printer.has_due(tmp_path, NOW + timedelta(minutes=11))

    sent = []
    result = printer.flush(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW + timedelta(minutes=11),
                           sender=lambda data: sent.append(data) or "bluetooth")
    assert result == [{"id": result[0]["id"], "outcome": "printed", "route": "bluetooth"}]
    assert b"hello there" in sent[0]
    state = printer.load_state(tmp_path)
    assert state["pending"] == [] and state["done"][-1]["outcome"] == "printed"


def test_old_jobs_expire_and_missing_notes_are_dropped(tmp_path, quiet):
    printer.enqueue("paper", _note(tmp_path), data_dir=tmp_path, now=NOW - timedelta(days=4))
    printer.enqueue("dream", "journal/dreams/gone.md", data_dir=tmp_path, now=NOW)
    out = printer.flush(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, sender=lambda d: pytest.fail("printed"))
    assert sorted(r["outcome"] for r in out) == ["expired", "unrenderable"]
    assert printer.load_state(tmp_path)["pending"] == []


def test_switched_off_printing_queues_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(printer, "_setting", lambda key, default: False if key == "printer_enabled" else default)
    assert printer.enqueue("paper", "journal/paper/n.md", data_dir=tmp_path, now=NOW) is None


# ── the activity ────────────────────────────────────────────────────────

def test_parse_takes_the_block_and_her_why():
    words, why = paper.parse("Something.\n```paper\nDear Anthon,\n\nthe rain stopped.\n```\nWhy: it did.")
    assert words == "Dear Anthon,\n\nthe rain stopped." and why == "it did."
    assert paper.parse("not now\nWhy: reading.") == ("", "reading.")
    assert paper.parse("Not now, I'm in the middle of a thought.")[0] == ""
    assert paper.parse("<think>hm</think>```paper\nhi you\n```")[0] == "hi you"
    assert paper.parse("no fence, just a note for you")[0] == "no fence, just a note for you"
    long_words, _ = paper.parse("```paper\n" + "line\n" * 200 + "```")
    assert len(long_words.splitlines()) == paper.MAX_LINES


def test_she_is_told_how_it_reaches_him(tmp_path, quiet):
    seen = []
    asyncio.run(paper.offer(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, ask=_fake(["not now"], seen)))
    prompt = seen[0][1]["content"]
    for must in ("receipt printer on his desk", "Nothing pings him", "can't answer on it", "32 characters",
                 "No emoji", "not now", "```paper"):
        assert must in prompt


def test_a_note_is_kept_and_queued(tmp_path, quiet):
    reply = "```paper\nAnthon --\n\nI dreamt of the hallway again.\n```\nWhy: I wanted it on your desk."
    result = asyncio.run(paper.offer(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, ask=_fake([reply], [])))
    assert result["status"] == "written" and result["queued"]
    text = (tmp_path / result["path"]).read_text()
    assert text.endswith("---\nAnthon --\n\nI dreamt of the hallway again.\n")
    assert "why: I wanted it on your desk." in text
    job = printer.load_state(tmp_path)["pending"][0]
    assert job["kind"] == "paper" and job["ref"] == result["path"]
    out = printer.render(job, tmp_path)
    assert b"I dreamt of the hallway again." in out and b"why" not in out.lower()
    assert quiet and "on the printer" in quiet[0][0][2]
    assert paper.notes(tmp_path)[0]["first_line"] == "Anthon --"


def test_declining_is_remembered_with_her_reason(tmp_path, quiet):
    result = asyncio.run(paper.offer(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW,
                                     ask=_fake(["not now\nWhy: nothing to say yet."], [])))
    assert result == {"status": "not_now", "why": "nothing to say yet."}
    assert printer.load_state(tmp_path)["pending"] == []
    assert paper.load_state(tmp_path)["history"][-1]["outcome"] == "not_now"


def test_gate(tmp_path, quiet):
    assert paper.gate(_sig(), data_dir=tmp_path, now=NOW, research_running=False) == []
    assert paper.gate(_sig(chat_active=True), data_dir=tmp_path, now=NOW, research_running=False)
    assert paper.gate(_sig(idle_minutes=5), data_dir=tmp_path, now=NOW, research_running=False)
    assert paper.gate(_sig(strata=False), data_dir=tmp_path, now=NOW, research_running=False)
    assert paper.gate(_sig(), data_dir=tmp_path, now=NOW, research_running=True)
    paper._save_state(tmp_path, {"last": (NOW - timedelta(hours=3)).isoformat()})
    assert "offered the printer recently" in paper.gate(_sig(), data_dir=tmp_path, now=NOW, research_running=False)
    paper._save_state(tmp_path, {"last": (NOW - timedelta(hours=23)).isoformat()})
    assert paper.gate(_sig(), data_dir=tmp_path, now=NOW, research_running=False) == []


def test_keepalive_pokes_the_printer_every_so_often(tmp_path, quiet):
    assert printer.keepalive_due(tmp_path, NOW)                          # never reached yet
    sent = []
    assert printer.keepalive(data_dir=tmp_path, now=NOW, sender=lambda d: sent.append(d) or "bluetooth")
    assert sent == [printer.STATUS]
    assert not printer.keepalive_due(tmp_path, NOW + timedelta(minutes=19))
    assert printer.keepalive_due(tmp_path, NOW + timedelta(minutes=20))

    def off(data):
        raise printer.PrinterUnavailable("bluetooth: host is down")
    later = NOW + timedelta(minutes=30)
    assert not printer.keepalive(data_dir=tmp_path, now=later, sender=off)
    assert not printer.keepalive_due(tmp_path, later + timedelta(minutes=10))   # a failed poke waits too
    state = printer.load_state(tmp_path)["contact"]
    assert state["ok_at"] == NOW.isoformat() and state["tried_at"] == later.isoformat()


def test_a_print_counts_as_contact_and_mixed_timezones_compare(tmp_path, quiet):
    ref = _note(tmp_path)
    printer.enqueue("paper", ref, data_dir=tmp_path, now=NOW)
    printer.flush(data_dir=tmp_path, mabel_vault=tmp_path, now=NOW, sender=lambda d: "bluetooth")
    assert printer.load_state(tmp_path)["contact"]["ok_at"] == NOW.isoformat()
    local = (NOW + timedelta(minutes=10)).astimezone(timezone(timedelta(hours=1)))
    assert not printer.keepalive_due(tmp_path, local)


def test_keepalive_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setattr(printer, "_setting", lambda key, default: 0 if key == "printer_keepalive_minutes" else default)
    assert not printer.keepalive_due(tmp_path, NOW)

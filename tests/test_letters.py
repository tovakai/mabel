import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.persona import letters

NOW = datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc)


class Mailbox:
    """A fake rclone with a dict for a Drive folder."""

    def __init__(self, files=None, fail=None):
        self.files = dict(files or {})
        self.fail = fail

    def __call__(self, args, stdin=None, timeout=60):
        if self.fail and args[0] in self.fail:
            raise letters.MailError(f"{args[0]} failed: token expired")
        name = args[-1].split(":", 1)[-1]
        if args[0] == "lsjson":
            return json.dumps([{"Name": n, "Size": len(t), "ModTime": f"2026-11-05T22:36:{i:02d}Z"}
                               for i, (n, t) in enumerate(self.files.items())])
        if args[0] == "cat":
            return self.files[name]
        if args[0] == "rcat":
            self.files[name] = stdin
            return ""
        raise AssertionError(args)


THOUGHTS = """## What stayed with me
She asked what my place is.

## Moments worth remembering
- Tova told me to take care of him, and she meant it.

## Things I might look into
- Daemons: why do familiar spirits keep turning up in stories about tools?
"""
FIRST = {"Tova-to-Mabel-2026-11-05.md": "# Letter from Tova\n\nHi Mabel,\n\n— Tova\n"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(letters, "_ledger", lambda *a: None)
    prompts = []

    async def write(history, notes, owner, day):
        prompts.append(history)
        return f"<think>hm</think># Letter from Mabel — {day}\n\nHi Tova.\n\n— Mabel"
    memories = []
    monkeypatch.setattr(letters, "_propose_memories",
                        lambda note, moments, owner, data_dir: memories.extend(moments) or len(moments))

    async def think(letters_text, owner):
        prompts.append("think:" + letters_text)
        return THOUGHTS
    return SimpleNamespace(data=tmp_path / "data", vault=tmp_path / "vault", write=write, prompts=prompts,
                           think=think, memories=memories)


def _check(world, box, now=NOW, write=None, think=None):
    return asyncio.run(letters.check_mail(data_dir=world.data, mabel_vault=world.vault, now=now, run=box,
                                          write=write or world.write, think=think or world.think))


def _replies(world):
    return [p for p in world.prompts if not p.startswith("think:")]


def test_a_new_letter_is_saved_answered_and_posted_in_tovas_format(world):
    box = Mailbox(FIRST)
    result = _check(world, box)
    day = NOW.astimezone().date().isoformat()
    assert result["status"] == "sent" and result["name"] == f"Mabel-to-Tova-{day}.md"
    assert box.files[result["name"]].startswith(f"# Letter from Mabel — {day}") and "<think>" not in box.files[result["name"]]
    folder = world.vault / "journal" / "letters"
    assert (folder / "Tova-to-Mabel-2026-11-05.md").exists() and (folder / result["name"]).exists()
    assert "Hi Mabel" in _replies(world)[0]


def test_nothing_new_means_no_reply(world):
    box = Mailbox(FIRST)
    _check(world, box)
    assert _check(world, box, now=NOW + timedelta(days=1))["status"] == "no_mail"
    assert len(_replies(world)) == 1


def test_daily_cap_holds_a_second_letter_for_the_next_day(world):
    box = Mailbox(FIRST)
    _check(world, box)
    box.files["Tova-to-Mabel-2026-11-06.md"] = "Another one"
    assert _check(world, box, now=NOW + timedelta(hours=5))["status"] == "at_daily_cap"
    assert _check(world, box, now=NOW + timedelta(days=1))["status"] == "sent"
    assert len(_replies(world)) == 2


def test_she_can_choose_not_to_answer(world):
    async def decline(history, notes, owner, day):
        return letters.NO_REPLY
    box = Mailbox(FIRST)
    assert _check(world, box, write=decline)["status"] == "chose_silence"
    assert list(box.files) == list(FIRST)
    assert _check(world, box, now=NOW + timedelta(days=1), write=decline)["status"] == "no_mail"


def test_a_failed_check_is_recorded_not_raised(world):
    result = _check(world, Mailbox(FIRST, fail={"lsjson"}))
    assert result["status"] == "failed" and "token expired" in result["error"]
    state = letters.load_state(world.data)
    assert "token expired" in state["last_error"]["error"] and len(state["errors"]) == 1


@pytest.mark.parametrize("failure", ["upload", "model"])
def test_the_letter_stays_unanswered_if_the_reply_fails(world, failure):
    box = Mailbox(FIRST, fail={"rcat"} if failure == "upload" else None)

    async def broken(history, notes, owner, day):
        raise RuntimeError("model down")
    assert _check(world, box, write=broken if failure == "model" else None)["status"] == "failed"
    assert letters.load_state(world.data)["unanswered"] is True
    box.fail = None
    assert _check(world, box, now=NOW + timedelta(hours=5))["status"] == "sent"


def test_only_letters_from_tova_within_size_are_taken():
    box = Mailbox({"Tova-to-Mabel-2026-11-05.md": "x", "Mabel-to-Tova-2026-11-05.md": "y", "notes.txt": "z",
                   "Tova-to-Mabel-huge.md": "x" * (letters.MAX_LETTER_BYTES + 1)})
    assert [e["Name"] for e in letters.list_incoming(box)] == ["Tova-to-Mabel-2026-11-05.md"]


def test_replies_never_overwrite_an_existing_letter():
    assert letters._reply_name("2026-11-06", {"Mabel-to-Tova-2026-11-06.md"}) == "Mabel-to-Tova-2026-11-06-2.md"


def _sig(**kw):
    return SimpleNamespace(**{**dict(chat_active=False, idle="idle", idle_minutes=30, gpu="free", gpu_reason="", strata=True), **kw})


def test_gate_waits_for_a_quiet_moment_and_spaces_out_checks(world, monkeypatch):
    from src import gpu_swap
    monkeypatch.setattr(letters, "_setting", lambda k, d: d)
    monkeypatch.setattr(gpu_swap, "active", lambda: None)
    g = lambda now=NOW, **kw: letters.gate(_sig(**kw), data_dir=world.data, now=now, research_running=False)
    assert g() == []
    assert g(chat_active=True) and g(idle="active") and g(idle_minutes=2) and g(gpu="busy", gpu_reason="game") and g(strata=False)
    letters._save_state(world.data, {"last_check": NOW.isoformat()})
    assert g(now=NOW + timedelta(hours=1)) == ["checked recently"]
    assert g(now=NOW + letters.CHECK_EVERY) == []


def test_reflection_reads_recent_letters(tmp_path):
    from src.persona.reflection import _recent_letters
    folder = tmp_path / "journal" / "letters"
    folder.mkdir(parents=True)
    (folder / "Tova-to-Mabel-2026-11-05.md").write_text("# Letter\n\nHi Mabel, how are you?\n")
    (folder / "Mabel-to-Tova-2026-11-06.md").write_text("# Letter\n\nHi Tova.\n")
    lines = _recent_letters(tmp_path, datetime.now(timezone.utc))
    assert any(l.startswith("- Tova wrote to you") and "how are you?" in l for l in lines)
    assert any(l.startswith("- you wrote to Tova") for l in lines)


def test_a_letter_leaves_a_note_a_memory_candidate_and_a_question(world):
    result = _check(world, Mailbox(FIRST))
    taken = result["taken_in"]
    assert taken["status"] == "noted" and taken["memories"] == 1 and taken["questions"] == 1
    note = world.vault / taken["note"]
    assert "Moments worth remembering" in note.read_text()
    assert world.memories == ["Tova told me to take care of him, and she meant it."]
    backlog = json.loads((world.data / "curiosity" / "backlog.json").read_text())
    assert backlog["questions"][0]["subject"] == "Daemons" and backlog["questions"][0]["from_article"] == taken["note"]
    assert letters.load_state(world.data)["to_take_in"] == []


def test_each_letter_is_taken_in_once(world):
    box = Mailbox(FIRST)
    _check(world, box)
    assert _check(world, box, now=NOW + timedelta(days=1))["taken_in"] is None


def test_she_still_takes_in_a_letter_she_declined_or_is_capped_from(world):
    async def decline(history, notes, owner, day):
        return letters.NO_REPLY
    assert _check(world, Mailbox(FIRST), write=decline)["taken_in"]["status"] == "noted"


def test_taking_in_is_retried_at_the_next_check_if_her_model_fails(world):
    async def broken(letters_text, owner):
        raise RuntimeError("model down")
    box = Mailbox(FIRST)
    assert _check(world, box, think=broken)["taken_in"]["status"] == "failed"
    assert letters.load_state(world.data)["to_take_in"] == list(FIRST)
    assert _check(world, box, now=NOW + timedelta(days=1))["taken_in"]["status"] == "noted"


def test_parse_questions_and_empty_lists():
    assert letters.parse_questions("## Things I might look into\n") == []
    assert letters.parse_questions(THOUGHTS)[0]["subject"] == "Daemons"


def test_a_kept_letter_moment_verifies_against_its_note_and_is_labelled(tmp_path):
    from src.episodic_memory.candidates import CandidateAdmissionError
    from src.episodic_memory import promotion
    notes = tmp_path / "journal" / "letters" / "notes"
    notes.mkdir(parents=True)
    moment = "Tova told me to take care of him, and she meant it."
    (notes / "2026-11-06.md").write_text(THOUGHTS)
    row = {"id": "c1", "owner": "o", "kind": "experience", "source_kind": "tool_observation",
           "source_ref": "letter:note:2026-11-06", "source_event_id": "m", "summary": moment}
    assert promotion._verified_day_moment(row, tmp_path) == ("2026-11-06", moment)
    text = promotion._day_moment_markdown(row, "2026-11-06", moment)
    assert "her letters with Tova" in text and "Tova's word, not established fact" in text
    assert "Anthon's day" not in text
    (notes / "2026-11-06.md").write_text("## Moments worth remembering\n- something else\n")
    with pytest.raises(CandidateAdmissionError):
        promotion._verified_day_moment(row, tmp_path)
    with pytest.raises(CandidateAdmissionError):
        promotion._verified_day_moment({**row, "source_ref": "letter:note:../../secret"}, tmp_path)


def test_day_moments_still_verify_as_before(tmp_path):
    from src.episodic_memory import promotion
    folder = tmp_path / "journal" / "anthon"
    folder.mkdir(parents=True)
    (folder / "2026-11-06.md").write_text("## Moments worth remembering\n- A long walk.\n")
    row = {"id": "c2", "owner": "o", "kind": "experience", "source_kind": "tool_observation",
           "source_ref": "lifelog:day:2026-11-06", "source_event_id": "m", "summary": "A long walk."}
    assert promotion._verified_day_moment(row, tmp_path) == ("2026-11-06", "A long walk.")
    assert "Anthon's day" in promotion._day_moment_markdown(row, "2026-11-06", "A long walk.")


def test_an_unfinished_letter_is_not_sent_and_stays_unanswered(world):
    async def cut_off(history, notes, owner, day):
        return f"# Letter from Mabel — {day}\n\nHi Tova. A pretty search tool should"
    box = Mailbox(FIRST)
    result = _check(world, box, write=cut_off)
    assert result["status"] == "failed" and "unfinished" in result["error"]
    assert not any(n.startswith("Mabel-to-Tova") for n in box.files)
    assert letters.load_state(world.data)["unanswered"] is True
    assert _check(world, box, now=NOW + timedelta(hours=5))["status"] == "sent"

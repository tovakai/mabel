import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PIL import Image

from src.persona import dreams
from src.subconscious.inbox import Inbox

NOTE = """Pattern: Local AI Systems
He often sets up thought experiments about autonomous local AI systems running on constrained hardware,
specifically mentioning power limits and background routines.
Imagine the following scenario: An autonomous local AI system (you) is running on a dedicated node with
strict power constraints during an extended grid failure.
Curiosity: I keep wondering if you're actually running a local model on your Pi or if you're just imagining how it would work."""


def test_word_bag_drops_filler_dedupes_and_respects_the_token_limit():
    counted = lambda text: 2 + len(text.split())          # one token per word, for the test
    bag = dreams.word_bag(NOTE, count=counted, seed=1, limit=20)
    assert counted(", ".join(bag)) <= 20
    assert len(bag) == len(set(bag))
    assert not {"the", "you", "he", "actually", "re"} & set(bag)
    assert set(bag) <= set(w.lower() for w in __import__("re").findall(r"[A-Za-z]+", NOTE))


def test_word_bag_takes_everything_that_fits_and_is_deterministic_per_seed():
    bag = dreams.word_bag(NOTE, seed="2026-11-05:x")
    assert bag == dreams.word_bag(NOTE, seed="2026-11-05:x")
    assert {"grid", "wondering", "node", "imagining"} <= set(bag)
    assert dreams.token_counter()(", ".join(bag) + ". in pixel art style") <= dreams.TOKEN_LIMIT


def test_fallback_counter_overestimates_rather_than_overflowing(tmp_path):
    count = dreams.token_counter(tmp_path / "missing.json")
    assert count("grid, node") >= 4


def test_dream_day_runs_six_to_six():
    tz = timezone(timedelta(hours=1))
    assert dreams.dream_day(datetime(2026, 11, 6, 5, 59, tzinfo=tz).astimezone()) == "2026-11-05"
    assert dreams.dream_day(datetime(2026, 11, 6, 6, 1, tzinfo=tz).astimezone()) == "2026-11-06"


def _inbox(tmp_path, now):
    inbox = Inbox(tmp_path / "subconscious" / "inbox.sqlite")
    ev = [{"ref": "chat:x", "kind": "chat", "context": "Imagine an autonomous node"}]
    inbox.add(kind="pattern", loop="dreamer", title="Old", dedupe_key="old", evidence=ev, now=now - timedelta(days=5))
    inbox.add(kind="pattern", loop="dreamer", title="Local AI Systems", dedupe_key="new", evidence=ev,
              summary="He sets up thought experiments.", payload={"curiosity": "I keep wondering about the Pi."},
              now=now - timedelta(hours=2))
    inbox.add(kind="module_proposal", loop="dreamer", title="Mabel's idea", dedupe_key="idea", evidence=ev, now=now)
    return inbox


def test_pick_source_takes_the_newest_recent_dreamer_pattern(tmp_path):
    now = datetime.now(timezone.utc)
    inbox = _inbox(tmp_path, now)
    item = dreams.pick_source(tmp_path, now, inbox=inbox, mabel_vault=tmp_path / "empty")
    assert item["title"] == "Local AI Systems"
    text = dreams.source_text(item)
    assert "Imagine an autonomous node" in text and "Curiosity: I keep wondering about the Pi." in text


def _sig(**kw):
    base = dict(chat_active=False, idle="idle", idle_minutes=45, gpu="free", gpu_reason="", strata=True)
    return SimpleNamespace(**{**base, **kw})


@pytest.fixture
def world(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    _inbox(tmp_path, now)
    gen_dir = tmp_path / "generated"
    gen_dir.mkdir()
    Image.new("RGB", (64, 64), (120, 80, 200)).save(gen_dir / "abc.png")
    import src.constants
    monkeypatch.setattr(src.constants, "GENERATED_IMAGES_DIR", str(gen_dir))
    monkeypatch.setattr(dreams, "_ledger", lambda *a: None)
    settings = {"dreams_enabled": True, "image_gen_enabled": True}
    monkeypatch.setattr(dreams, "_setting", lambda k, d: settings.get(k, d))
    from src import gpu_swap
    monkeypatch.setattr(gpu_swap, "load_config", lambda: {"services": {"image": {}}})
    monkeypatch.setattr(gpu_swap, "active", lambda: None)
    prompts = []

    async def generate(content, session_id=None, owner=None):
        prompts.append(json.loads(content))
        return {"image_url": "/api/generated-image/abc.png", "image_id": "g1"}

    async def fragments_of(raw, owner):
        assert "Local AI Systems" in raw
        return ["the hallway where the grid used to be", "someone unplugs the moon", "grid grid grid", "a warm room"]

    async def look(image_path, raw, prompt, owner, day):
        assert image_path.exists() and "Local AI Systems" in raw
        return "<think>hm</think># Dream: Thursday\n\nA maze of wires around one warm chip."
    async def look_any(image_path, raw, prompt, owner, day):
        return "# Dream: x\n\nA mailbox in a wide room."
    return SimpleNamespace(now=now, generate=generate, look=look, look_any=look_any, fragments_of=fragments_of, prompts=prompts, vault=tmp_path / "vault",
                           data=tmp_path, settings=settings)


def test_a_dream_writes_image_and_entry_and_counts_for_the_day(world):
    result = asyncio.run(dreams.dream(data_dir=world.data, mabel_vault=world.vault, now=world.now,
                                      generate=world.generate, look=world.look, fragments_of=world.fragments_of))
    assert result["status"] == "dreamt" and result["style"] == "fragments"
    assert world.prompts[0]["size"] == "1024x1024"
    assert world.prompts[0]["prompt"] == ("the hallway where the grid used to be, someone unplugs the moon, "
                                          "grid grid grid, a warm room")
    day = dreams.dream_day(world.now)
    note = (world.vault / "journal" / "dreams" / f"{day}.md").read_text()
    assert (world.vault / "journal" / "dreams" / f"{day}.png").exists()
    assert f"![[{day}.png]]" in note and "A maze of wires" in note and "<think>" not in note
    assert "source: inbox:" in note and "What I dreamt from" in note and "> grid grid grid" in note
    state = dreams.load_state(world.data)
    assert state["last_day"] == day and state["history"][-1]["first_line"].startswith("A maze")
    assert dreams.gate(_sig(), data_dir=world.data, now=world.now, research_running=False) == ["already dreamt today"]
    left = dreams.pick_source(world.data, world.now, mabel_vault=world.vault)
    assert left is None or left["title"] != "Local AI Systems"


def test_she_still_keeps_the_dream_if_she_cant_describe_it(world):
    async def broken_look(*a):
        raise RuntimeError("vision down")
    result = asyncio.run(dreams.dream(data_dir=world.data, mabel_vault=world.vault, now=world.now,
                                      generate=world.generate, look=broken_look, fragments_of=world.fragments_of))
    assert result["status"] == "dreamt"
    assert "couldn't put it into words" in (world.vault / result["note"]).read_text()


def test_a_failed_image_is_not_a_dream(world):
    async def failing(content, session_id=None, owner=None):
        return {"error": "swap failed"}
    result = asyncio.run(dreams.dream(data_dir=world.data, mabel_vault=world.vault, now=world.now,
                                      generate=failing, look=world.look, fragments_of=world.fragments_of))
    assert result["status"] == "image_failed"
    assert not dreams.dreamt_today(world.data, world.now) and not dreams.running()


def test_gate_needs_anthon_away_a_free_gpu_and_no_chat(world):
    g = lambda **kw: dreams.gate(_sig(**kw), data_dir=world.data, now=world.now, research_running=False)
    assert g() == []
    assert g(idle_minutes=10) and g(idle="active") and g(chat_active=True) and g(chat_active=None)
    assert g(gpu="busy", gpu_reason="game") and g(strata=False)
    assert dreams.gate(_sig(), data_dir=world.data, now=world.now, research_running=True)
    world.settings["dreams_enabled"] = False
    assert "dreams are switched off" in g()


def test_falls_back_to_the_word_bag_when_she_cant_dream_it_into_words(world):
    async def silent(raw, owner):
        raise RuntimeError("model busy")
    result = asyncio.run(dreams.dream(data_dir=world.data, mabel_vault=world.vault, now=world.now,
                                      generate=world.generate, look=world.look, fragments_of=silent))
    assert result["status"] == "dreamt" and result["style"] == "word_bag"
    assert "systems" in world.prompts[0]["prompt"]


def test_pack_fragments_keeps_whole_fragments_in_order_with_repeats():
    count = lambda t: 2 + len(t.replace(",", " ").split())
    frags = ["the wide room", "the wide room", "a corridor that goes on and on and on and on and on", "rain indoors"]
    assert dreams.pack_fragments(frags, count=count, limit=12) == ["the wide room", "the wide room", "rain indoors"]
    assert dreams.pack_fragments(["one two three four five six seven"], count=count, limit=6) == ["one two three four"]


def test_parse_fragments_strips_list_marks_and_thinking_only():
    text = "<think>plan</think>\n1. he opened the year.\n- “grid grid grid”\nHere are fragments:\n* the kitchen, but outside"
    assert dreams.parse_fragments(text) == ["he opened the year", "grid grid grid", "the kitchen, but outside"]


def test_self_reflection_reads_recent_dreams(tmp_path):
    from src.persona.reflection import _recent_dreams
    folder = tmp_path / "journal" / "dreams"
    folder.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    recent, old = now.date().isoformat(), (now - timedelta(days=30)).date().isoformat()
    for day in (recent, old):
        (folder / f"{day}.md").write_text(
            "---\ntype: dream\nfragments:\n- the hallway held two evenings\n- grid grid grid\n---\n"
            f"![[{day}.png]]\n\n# Dream: x\n\nA corridor that keeps going.\n\n> [!note]- The dream in words\n> grid\n")
    lines = _recent_dreams(tmp_path, now)
    assert len(lines) == 1 and recent in lines[0]
    assert "the hallway held two evenings / grid grid grid" in lines[0] and "A corridor that keeps going." in lines[0]
    assert "![[" not in lines[0] and "[!note]" not in lines[0]


def _letter(vault, name="Tova-to-Mabel-2026-11-05", text="# Letter\\n\\nHi Mabel, take care of him.\\n"):
    folder = vault / "journal" / "letters"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(text)


def test_a_dream_can_grow_from_tovas_letter_and_is_referenced_as_such(world):
    _letter(world.vault)
    seen = []

    async def fragments_of(raw, owner):
        seen.append(raw)
        return ["the mailbox hums in the wide room"]
    result = asyncio.run(dreams.dream(data_dir=world.data, mabel_vault=world.vault, now=world.now,
                                      generate=world.generate, look=world.look_any, fragments_of=fragments_of))
    assert result["status"] == "dreamt" and "take care of him" in seen[0]
    note = (world.vault / result["note"]).read_text()
    assert "source: letter:Tova-to-Mabel-2026-11-05" in note and "source_title: A letter from Tova" in note
    assert "letter:Tova-to-Mabel-2026-11-05" in dreams.load_state(world.data)["dreamt_items"]


def test_notes_and_letters_take_turns_as_dream_sources(world):
    _letter(world.vault)
    pick = lambda: dreams.pick_source(world.data, world.now, mabel_vault=world.vault)
    assert pick()["kind"] == "letter"                     # first turn goes to the letter
    dreams._save_state(world.data, {"last_source": "letter"})
    assert pick()["title"] == "Local AI Systems"
    dreams._save_state(world.data, {"last_source": "note"})
    assert pick()["kind"] == "letter"


def test_old_or_dreamt_letters_are_not_picked_again(world):
    import os
    _letter(world.vault)
    path = world.vault / "journal" / "letters" / "Tova-to-Mabel-2026-11-05.md"
    dreams._save_state(world.data, {"dreamt_items": ["letter:Tova-to-Mabel-2026-11-05"]})
    assert dreams.pick_source(world.data, world.now, mabel_vault=world.vault)["title"] == "Local AI Systems"
    dreams._save_state(world.data, {})
    old = (world.now - timedelta(days=9)).timestamp()
    os.utime(path, (old, old))
    assert dreams.pick_source(world.data, world.now, mabel_vault=world.vault)["title"] == "Local AI Systems"

"""The shared grocery list (2026-11-17): note format, the three-way sync, the chat tool, the store reminder."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.agent_tools import groceries_tool
from src.initiative import groceries as g
from src.turn_contract import requested_capabilities

NOW = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(g, "_setting", lambda k, d: d)


def I(name, **kw):
    return g.Item(name=name, **kw)


def base(*items):
    return {i.key: g.asdict(i) for i in items}


def test_note_lines_round_trip_and_keep_his_header():
    text = "# Groceries\n\nmy own words\n\n- [ ] Oat milk (2) — for the cake\n- [x] Eggs\n* [ ] coffee beans\n"
    items = g.parse_note(text)
    assert [(i.name, i.amount, i.note, i.bought) for i in items] == [
        ("Oat milk", "2", "for the cake", False), ("Eggs", "", "", True), ("coffee beans", "", "", False)]
    out = g.render_note(items, text)
    assert out.startswith("# Groceries\n\nmy own words\n\n") and "- [ ] Oat milk (2) — for the cake" in out
    assert g.render_note([], "").startswith("# Groceries")


def test_merge_carries_adds_ticks_and_deletions_from_either_side():
    milk, eggs, bread = I("Milk"), I("Eggs"), I("Bread")
    b = base(milk, eggs, bread)
    sheet = [I("Milk", bought=True), I("Eggs"), I("Butter", added_by="Partner")]      # ticked milk, deleted bread
    note = [I("Milk"), I("Eggs", amount="12"), I("Bread"), I("Cheese")]               # amount on eggs, + cheese
    items, done = g.merge(sheet, note, b, NOW)
    got = {i.name: i for i in items}
    assert set(got) == {"Milk", "Eggs", "Butter", "Cheese"} and not done
    assert got["Milk"].bought and got["Milk"].bought_at == NOW.isoformat(timespec="seconds")
    assert got["Eggs"].amount == "12"
    assert got["Butter"].added_by == "Partner" and got["Cheese"].added_by == "Anthon"


def test_note_never_wipes_sheet_only_fields():
    coffee = I("Coffee", added_by="Mabel", amount="500 g")
    items, _ = g.merge([coffee], [I("Coffee", amount="500 g")], base(coffee), NOW)
    assert items[0].added_by == "Mabel"


def test_conflicts_sheet_wins_but_bought_beats_not_bought():
    eggs = I("Eggs", amount="6")
    items, _ = g.merge([I("Eggs", amount="10")], [I("Eggs", amount="12", bought=True)], base(eggs), NOW)
    assert items[0].amount == "10" and items[0].bought


def test_bought_things_stay_a_day_then_move_to_history():
    old = (NOW - timedelta(days=1, minutes=1)).isoformat()
    fresh = (NOW - timedelta(hours=2)).isoformat()
    a, b2 = I("Milk", bought=True, bought_at=old), I("Eggs", bought=True, bought_at=fresh)
    items, done = g.merge([a, b2], [I("Milk", bought=True), I("Eggs", bought=True)], base(a, b2), NOW)
    assert [i.name for i in items] == ["Eggs"] and [i.name for i in done] == ["Milk"]
    untick = I("Eggs", bought=True, bought_at=fresh)
    items, _ = g.merge([I("Eggs", bought=True, bought_at=fresh)], [I("Eggs")], base(untick), NOW)
    assert not items[0].bought and items[0].bought_at == ""                           # a mis-tick undone


class FakeSheet:
    def __init__(self, items):
        self.items, self.archived, self.writes = items, [], 0

    def read(self):
        return [g.Item(**g.asdict(i)) for i in self.items]

    def write(self, items, old_rows):
        self.items, self.writes = [g.Item(**g.asdict(i)) for i in items], self.writes + 1

    def archive(self, items):
        self.archived += items


def test_sync_writes_both_sides_and_remembers_the_base(tmp_path):
    sheet, note = FakeSheet([I("Milk", added_by="Partner")]), g.NoteStore(tmp_path / "Groceries.md")
    (tmp_path / "Groceries.md").write_text("- [ ] Eggs\n")
    g.sync(data_dir=tmp_path, sheet=sheet, note=note, now=NOW)
    assert {i.name for i in sheet.items} == {"Milk", "Eggs"}
    assert "- [ ] Milk" in note.path.read_text() and "- [ ] Eggs" in note.path.read_text()
    note.path.write_text(note.path.read_text().replace("- [ ] Milk", "- [x] Milk"))
    writes = sheet.writes
    g.sync(data_dir=tmp_path, sheet=sheet, note=note, now=NOW)
    assert [i.bought for i in sheet.items if i.name == "Milk"] == [True] and sheet.writes == writes + 1
    g.sync(data_dir=tmp_path, sheet=sheet, note=note, now=NOW)
    assert sheet.writes == writes + 1                                                 # nothing new: no write


def test_tool_adds_ticks_and_removes(tmp_path):
    sheet, note = FakeSheet([I("Milk")]), g.NoteStore(tmp_path / "Groceries.md")

    def sync(change=None):
        return g.sync(data_dir=tmp_path, sheet=sheet, note=note, now=NOW, change=change)

    out = json.loads(groceries_tool.run({"action": "add", "items": ["oat milk", "milk"], }, sync=sync)["output"])
    assert out["added"] == ["Oat milk"] and out["already_there"] == ["Milk"]
    assert [i.added_by for i in sheet.items if i.name == "Oat milk"] == ["Mabel"]
    out = json.loads(groceries_tool.run({"action": "got", "items": "Milk"}, sync=sync)["output"])
    assert out["ticked"] == ["Milk"] and out["bought_recently"] == ["Milk"]
    out = json.loads(groceries_tool.run({"action": "remove", "items": ["oat milk", "caviar"]}, sync=sync)["output"])
    assert out["removed"] == ["Oat milk"] and out["not_on_list"] == ["caviar"]
    assert groceries_tool.run({"action": "add"}, sync=sync)["exit_code"] == 1


def test_only_entering_a_01_zone_triggers_once_per_visit(tmp_path):
    def seen(state_now):
        return g.observe(NOW, data_dir=tmp_path, current=state_now)

    assert seen("home") is None
    assert seen("Godis4You - Corner Store") is None                                  # no code
    assert seen("Citytorget [01]") == "Citytorget [01]"
    assert seen("Citytorget [01]") is None                                            # still there
    state = g.load_state(tmp_path)
    state["reminded"] = {"Citytorget [01]": NOW.isoformat()}
    g.save_state(tmp_path, state)
    seen("not_home")
    assert seen("Citytorget [01]") is None                                            # back within 2 h
    assert seen("Hardware store [02]") is None                                        # another kind of place
    assert g.place_name("Citytorget [01]") == "Citytorget"


def test_her_message_never_drops_an_item():
    async def ask(messages, owner):
        assert "Oat milk (2)" in messages[-1]["content"]
        return "Grab the oat milk while you're there."

    items = [I("Oat milk", amount="2"), I("Eggs")]
    message = asyncio.run(g.write_message("Citytorget", items, owner="x", ask=ask))
    assert message.startswith("Grab the oat milk") and message.endswith("Also: Eggs.")


def test_reminder_falls_back_to_the_plain_list(tmp_path, monkeypatch):
    sheet = FakeSheet([I("Milk"), I("Eggs", bought=True)])
    monkeypatch.setattr(g, "SheetStore", lambda sid: sheet)
    monkeypatch.setattr(g.NoteStore, "default", classmethod(lambda cls: None))
    g.save_state(tmp_path, {"sheet_id": "x"})

    async def broken(messages, owner):
        raise TimeoutError("model asleep")

    record = asyncio.run(g.remind("Citytorget [01]", data_dir=tmp_path, now=NOW, ask=broken, send=False))
    assert record["message"] == "You're at Citytorget. On the list: Milk." and record["items"] == ["Milk"]
    assert g.load_state(tmp_path)["reminded"]["Citytorget [01]"]


@pytest.mark.parametrize("text", ["add oat milk to the shopping list", "we're out of eggs",
                                  "what's on the grocery list?", "Mabel, we are running low on coffee",
                                  "add oat milk to the list", "I got everything"])
def test_routes_grocery_talk(text):
    assert "groceries" in requested_capabilities(text)


@pytest.mark.parametrize("text", ["add this to the list of ideas", "we're out of time on this project",
                                  "I'm out of ideas", "we are low on battery", "put on some music"])
def test_leaves_other_talk_alone(text):
    assert "groceries" not in requested_capabilities(text)

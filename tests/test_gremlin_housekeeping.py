"""Gremlin housekeeping rounds (2026-11-17): fixed checks, her suggestions, the note and the print."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.agent_tools import gremlin_tool
from src.heartbeat.signals import Signals
from src.initiative import gremlin_housekeeping as gh

TK_REPO = Path("~/Documents/tk-index").expanduser()
pytestmark = pytest.mark.skipif(not (TK_REPO / "src" / "tk_index" / "db.py").exists(), reason="tk-index not here")
NOW = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)


def item(id_, name, spec="", category="Tools", address="GRIDES0001C01R01", **kw):
    return {"id": id_, "name": name, "spec_code": spec, "category": category, "quantity": 1, "quantity_note": "",
            "stage": "Placed", "specs": "", "notes": "", "address": address, **kw}


@pytest.fixture
def specs(monkeypatch):
    monkeypatch.setattr(gremlin_tool, "_setting", lambda k, d: {"gremlin_index_repo": str(TK_REPO)}.get(k, d))
    return gh._specs_module()


def test_tidy_name_follows_his_house_style():
    assert gh.tidy_name("Gridfinity 1×2 bin · blue/lavender A", " - ") == "Gridfinity 1×2 bin - blue/lavender A"
    assert gh.tidy_name("2x2 Gridfinity bin", " - ") == "2×2 Gridfinity bin"
    assert gh.tidy_name("airbrush – red metal", " - ") == "Airbrush - red metal"
    for fine in ("Paint - Citadel - Base - Khorne Red", "Plastic bags (1L - 5L)", "nRF52840 development board"):
        assert gh.tidy_name(fine, " - ") == fine


def test_fixed_checks_find_the_real_problems(specs, monkeypatch):
    monkeypatch.setattr(gh, "_setting", lambda k, d: d)
    tools = [item(f"0000010{i}", f"Screwdriver {i}") for i in range(5)]       # makes "Tools" a main category
    group = [
        item("00000217", "Generic 1×4 Gridfinity bin", spec="GFST0301"),       # the real one from his index
        item("00000211", "Assorted unsorted resistors - additional batch"),
        item("00000517", "Steam Frame VR headset", category="Tools / VR"),
        item("00000009", "Small green silicone wire spool", category="Cables & power",
             specs="Silicone-insulated wire; gauge/length unknown; small spool"),
        item("00000133", "2×4 USB cable organizer bin - 1 of 3", category="Cables & power"),
        item("00000005", "Gridfinity 1×2 bin · blue"),
    ]
    found = gh.checks(group, tools + group, specs)
    fixes = {(f["id"], f["change"]) for f in found if f["kind"] == "fix"}
    assert ("00000217", "spec ID: GFST0104") in fixes
    assert any(i == "00000211" and "sort it" in c for i, c in fixes)
    assert ("00000517", "category: Tools") in fixes
    assert ("00000005", "name: Gridfinity 1×2 bin - blue") in fixes
    hints = {(f["id"], f["what"]) for f in found if f["kind"] == "hint"}
    assert ("00000009", "cable/wire with no length") in hints
    assert ("00000133", "cable/wire with no length") not in hints            # a bin for cables isn't a cable


def test_places_merge_small_ones_and_split_big_ones():
    big = [item(f"{i:08d}", f"Thing {i}", address="SHFFSH0001100001") for i in range(1, 36)]
    garage = [item("00000101", "Saw", address="STDGAR0001000000"), item("00000102", "Vice", address="STDGAR0003000000")]
    lone = [item("00000201", "Towel", address="STDBTH0001000000"), item("00000202", "Coat", address="STDHAL0001000000")]
    rounds = gh.places(big + garage + lone + [item("00000301", "Box of magnets", address=None)])
    assert len(rounds["SHFFSH0001#1"]) == gh.PART_SIZE and len(rounds["SHFFSH0001#2"]) == 5
    assert {i["id"] for i in rounds["AREAGAR"]} == {"00000101", "00000102"}
    assert {i["id"] for i in rounds["AREAMISC"]} == {"00000201", "00000202"}
    assert [i["id"] for i in rounds[gh.UNPLACED]] == ["00000301"]
    state = {"reviewed": {k: "2026-11-17T03:00:00" for k in rounds if k != "AREAMISC"}}
    assert gh.next_place(rounds, state) == "AREAMISC"                         # never reviewed goes first


def test_her_lines_keep_only_known_items_and_valid_spec_ids(specs):
    reply = "\n".join([
        "00000009 | spec ID: WIRSIL22 | silicone wire, same as the others",
        "00000009 | spec ID: banana | made up",
        "00000010 | spec ID: XYZ12345 | unknown family",
        "99999999 | name: something | not in this place",
        "00000010 | specs: measure the gauge with calipers | gauge unknown",
        "chatter that isn't a line",
        "00000009, 00000010, 99999999 | specs: measure the gauge | all the silicone spools",
        "00000009 | spec ID: | nothing after the colon",
        "00000009 | category: Wire & spools | a new category",
        "00000010 | category: tools | an existing one",
    ])
    lines = gh.parse(reply, {"00000009", "00000010"}, specs, {"Tools", "Cables & power"})
    assert [(l["ids"], l["change"]) for l in lines] == [
        (["00000009"], "spec ID: WIRSIL22"), (["00000010"], "specs: measure the gauge with calipers"),
        (["00000009", "00000010"], "specs: measure the gauge"),                 # one line for a set
        (["00000010"], "category: Tools")]                                      # only categories he has


@pytest.fixture
def index(tmp_path, monkeypatch):
    path = tmp_path / "gremlin.sqlite3"
    monkeypatch.setattr(gremlin_tool, "_setting",
                        lambda k, d: {"gremlin_index_path": str(path), "gremlin_index_repo": str(TK_REPO)}.get(k, d))
    inv = gremlin_tool.load_inventory(path)
    db = inv.connect(path)
    inv.initialize(db)
    inv.create_layout(db, "STD", "KON0001", "Kontoret")
    inv.create_item(db, item_id="00000002", name="Main desk", address="STDKON0001000001")
    inv.create_layout(db, "GRI", "DES0001", "Desk grid", columns=10, rows=8, host_item_id="00000002")
    inv.create_item(db, item_id="00000010", name="Unsorted resistors · bag", address="GRIDES0001C02R03")
    inv.create_item(db, item_id="00000011", name="Red silicone wire spool", address="GRIDES0001C03R03",
                    category="Cables & power")
    inv.create_item(db, item_id="00000012", name="Digital calipers", address="GRIDES0001C04R03")
    db.close()
    return path


def test_a_round_writes_the_note_queues_a_slip_and_leaves_the_index_alone(index, tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "_setting", lambda k, d: d)
    vault, data, repo = tmp_path / "vault", tmp_path / "data", tmp_path / "mabel"
    vault.mkdir(), data.mkdir(), repo.mkdir()
    before = hashlib.sha256(index.read_bytes()).hexdigest()
    asked = []

    async def ask(messages, owner):
        asked.append(messages[-1]["content"])
        return "00000011 | spec ID: WIRSIL01 | first silicone spool\n00000011 | specs: measure the gauge | unknown"

    result = asyncio.run(gh.run_round(data_dir=data, now=NOW, place="GRIDES0001", ask=ask, vault=vault,
                                      index_path=index, mabel_vault=repo))
    assert result["status"] == "done" and result["place"] == "GRIDES0001"
    assert "Red silicone wire spool" in asked[0] and "WIR:" in asked[0]
    note = (vault / gh.HOUSEKEEPING_NOTE).read_text(encoding="utf-8")
    assert note.startswith("# Gremlin housekeeping")
    assert "## 2026-11-17 · Desk grid (GRIDES0001)" in note
    assert "- [ ] **Unsorted resistors · bag** `00000010`: sort it" in note
    assert "`00000011`: spec ID: WIRSIL01" in note
    assert note.index("sort it") < note.index("Names in the house style:")    # renames come last
    queue = json.loads((data / "persona" / "printer.json").read_text())
    job = queue["pending"][0]
    assert job["kind"] == "gremlin" and (repo / job["ref"]).is_file()
    slip = (repo / job["ref"]).read_text(encoding="utf-8")
    assert "GREMLIN HOUSEKEEPING" in slip and "1 name(s) to the house style" in slip
    assert hashlib.sha256(index.read_bytes()).hexdigest() == before          # suggestions only
    assert gh.load_state(data)["reviewed"]["GRIDES0001"] and gh.load_state(data)["last_day"] == "2026-11-17"   # 04:00 local is still the 17th (day starts 06:00)

    later = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)
    second = asyncio.run(gh.run_round(data_dir=data, now=later, ask=ask, vault=vault, index_path=index,
                                      mabel_vault=repo))
    assert second["place"] != "GRIDES0001"                                    # moves on to another place
    note = (vault / gh.HOUSEKEEPING_NOTE).read_text(encoding="utf-8")
    assert note.index("## 2026-11-18") < note.index("## 2026-11-17")          # newest on top


def test_gate_waits_for_him_to_be_away_and_once_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(gh, "_setting", lambda k, d: d)
    away = Signals(idle="idle", idle_minutes=30, gpu="free", chat_active=False, strata=True)
    assert gh.gate(away, data_dir=tmp_path, now=NOW, research_running=False) == []
    here = Signals(idle="active", idle_minutes=0, gpu="free", chat_active=False, strata=True)
    assert any("away" in r for r in gh.gate(here, data_dir=tmp_path, now=NOW, research_running=False))
    gh.save_state(tmp_path, {"last_day": gh.day_of(NOW)})
    assert "did a round today" in gh.gate(away, data_dir=tmp_path, now=NOW, research_running=False)

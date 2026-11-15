"""manage_gremlin + the evening check-in, against a tiny throwaway Gremlin Index (2026-11-13)."""
import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from src.agent_tools import gremlin_tool
from src.agent_runtime.authority import ExactOperation, create_request_authority
from src.initiative import gremlin_checkin
from src.turn_contract import _REQUIRED_TOOLS, requested_capabilities

TK_REPO = Path("~/Documents/tk-index").expanduser()
pytestmark = pytest.mark.skipif(not (TK_REPO / "src" / "tk_index" / "db.py").exists(), reason="tk-index not here")


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
    inv.create_item(db, item_id="00000010", name="Digital calipers", address="GRIDES0001C02R03")
    inv.create_item(db, item_id="00000011", name="Soldering iron tip · C210", address="GRIDES0001C03R03")
    inv.create_item(db, item_id="00000012", name="Soldering iron tip · C245", address="GRIDES0001C04R03")
    inv.create_item(db, item_id="00000013", name="Box of magnets")                    # no home yet
    db.close()
    return path


def _run(args):
    out = asyncio.run(gremlin_tool.GremlinTool().execute(json.dumps(args), {}))
    assert out["exit_code"] == 0, out
    return json.loads(out["output"])


def test_where_names_the_place_outermost_first(index):
    result = _run({"action": "where", "item": "the calipers"})
    assert result["id"] == "00000010" and result["status"] == "home"
    assert result["home"].startswith("Kontoret › Desk grid (Main desk)") and "GRIDES0001C02R03" in result["home"]


def test_checkout_and_return_are_logged_as_hers_and_undoable(index):
    out = _run({"action": "checkout", "item": "calipers", "to": "garage", "reason": "measuring the shelf"})
    assert out["result"] == "checked_out" and out["item"]["status"] == "away"
    listing = _run({"action": "out"})
    assert [i["id"] for i in listing["checked_out"]] == ["00000010"] and listing["checked_out"][0]["out_to"] == "garage"
    assert _run({"action": "checkout", "item": "00000010"})["result"] == "refused"      # already out
    assert _run({"action": "return", "item": "calipers"})["result"] == "returned"
    inv = gremlin_tool.load_inventory(index)
    db = inv.connect(index)
    undo = db.execute("SELECT label, actor FROM undo_log ORDER BY id").fetchall()
    assert [tuple(r) for r in undo] == [("Check out Digital calipers (Mabel)", "mabel"),
                                        ("Return Digital calipers (Mabel)", "mabel")]
    inv.undo_last(db)                                                                    # his web UI can undo hers
    assert inv.item_dict(inv.get_item(db, "00000010"))["status"] == "away"


def test_ambiguous_names_ask_instead_of_guessing(index):
    out = _run({"action": "checkout", "item": "soldering iron tip"})
    assert out["result"] == "which_one" and {c["id"] for c in out["candidates"]} == {"00000011", "00000012"}
    assert _run({"action": "out"})["checked_out"] == []


def test_return_prefers_the_one_that_is_out(index):
    _run({"action": "checkout", "item": "00000012"})
    assert _run({"action": "return", "item": "soldering iron tip"})["item"]["id"] == "00000012"
    assert _run({"action": "return", "item": "soldering iron tip"})["result"] == "refused"   # none out now


def test_unknown_things_and_unplaced_items(index):
    assert _run({"action": "where", "item": "flux capacitor"})["result"] == "not_found"
    assert _run({"action": "checkout", "item": "magnets"})["result"] == "refused"          # no home to come back to


def test_missing_database_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gremlin_tool, "_setting", lambda k, d: str(tmp_path / "nope.sqlite3") if k == "gremlin_index_path" else d)
    out = asyncio.run(gremlin_tool.GremlinTool().execute('{"action": "out"}', {}))
    assert out["exit_code"] == 1 and "gremlin_index_path" in out["error"]


@pytest.mark.parametrize("message", [
    "where's my soldering iron?", "Where are my calipers", "Mabel, I'm returning the calipers",
    "I'm borrowing the multimeter for the car", "what's still checked out?", "put the scissors back",
    "where does the hot glue gun go?", "can you check the gremlin index for magnets",
])
def test_inventory_talk_requires_the_tool(message):
    assert requested_capabilities(message) == {"gremlin"}
    assert _REQUIRED_TOOLS["gremlin"] == "manage_gremlin"


@pytest.mark.parametrize("message", [
    "where is the nearest pharmacy", "check out this repo", "where is the config file?",
    "where is my package from amazon?", "I'm returning the favour", "where did we leave off in the story?",
    "where's the bug in this function?",
])
def test_other_where_questions_dont(message):
    assert "gremlin" not in requested_capabilities(message)


def test_saying_returning_is_the_approval():
    authority = create_request_authority("I'm returning the calipers", owner="alice", session_id="s",
                                         workspace="", interactive_review=True)
    assert authority.permits(ExactOperation.normalize("manage_gremlin", '{"action": "return", "item": "calipers"}'))
    other = create_request_authority("what's the weather like?", owner="alice", session_id="s", workspace="",
                                     interactive_review=True)
    assert not other.permits(ExactOperation.normalize("manage_gremlin", '{"action": "checkout", "item": "x"}'))


# ── evening check-in ─────────────────────────────────────────────────────

EVENING = datetime(2026, 11, 13, 21, 30).astimezone()
AFTERNOON = datetime(2026, 11, 13, 15, 0).astimezone()


def test_checkin_gates():
    assert any("evening" in r for r in gremlin_checkin.gate(data_dir=Path("/nonexistent"), now=AFTERNOON, model_free=True))
    assert any("busy" in r for r in gremlin_checkin.gate(data_dir=Path("/nonexistent"), now=EVENING, model_free=False))
    assert gremlin_checkin.in_window(datetime(2026, 11, 14, 1, 0).astimezone())       # night owl: still "evening"


def test_checkin_writes_about_whats_out_once_a_day(index, tmp_path):
    asked = []

    async def ask(messages, owner):
        asked.append(messages[-1]["content"])
        return "The calipers are still in the garage, want to bring them in?"
    _run({"action": "checkout", "item": "calipers", "to": "garage"})
    first = asyncio.run(gremlin_checkin.run_once(data_dir=tmp_path, now=EVENING, ask=ask, model_free=True, send=False,
                                                 force=False))
    assert first["status"] == "preview" and "Digital calipers" in asked[0] and "to garage" in asked[0]
    assert "00000010" not in first["message"]


def test_checkin_stays_quiet_when_nothing_is_out(index, tmp_path):
    async def ask(messages, owner):
        raise AssertionError("no message when nothing is out")
    result = asyncio.run(gremlin_checkin.run_once(data_dir=tmp_path, now=EVENING, ask=ask, model_free=True))
    assert result["status"] == "nothing_out"
    assert "already" in " ".join(gremlin_checkin.gate(data_dir=tmp_path, now=EVENING, model_free=True))

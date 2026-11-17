"""INVENTORY: the port of tk-index's place model, the grid drawing and Mabel's housekeeping inbox."""
from mabel_tui import housekeeping as hk
from mabel_tui.inventory import draw_grid
from mabel_tui.tkindex import World


def lay(key, label, *, fmt="seq", view="list", host=None, parent=None, cols=None, rows=None, levels=None):
    return {"key": key, "prefix": key[:3], "area": key[3:], "label": label, "position_format": fmt, "view": view,
            "host_item_id": host, "parent_address": parent, "columns": cols, "rows": rows, "levels": levels}


def thing(id_, name, addr, **kw):
    return {"id": id_, "name": name, "address": addr, "status": "home", "category": "Tools", **kw}


WORLD = World({
    "layouts": [
        lay("STDKON0001", "Kontoret"),
        lay("STDDES0001", "Desk top", host="00000002"),
        lay("GRIDES0002", "Desk grid", fmt="grid", view="gridfinity", host="00000002", cols=4, rows=3),
        lay("DRWLHE0001", "Drawers", fmt="level", view="drawers", host="00000025", levels=3),
        lay("STDLHE0002", "Top", host="00000025"),
        lay("GRILHE0012", "Battery grid", fmt="grid", view="gridfinity", parent="DRWLHE0001200000", cols=2, rows=2),
        lay("GRILHE0013", "Drawer 3 grid", fmt="grid", view="gridfinity", parent="DRWLHE0001300000", cols=2, rows=2),
    ],
    "items": [
        thing("00000002", "Main desk", "STDKON0001000001"),
        thing("00000025", "Left HELMER", "STDKON0001000002"),
        thing("00000217", "Generic 1×4 Gridfinity bin", "GRIDES0002C01R01", span_columns=4, span_rows=1),
        thing("00000300", "Calipers", "GRIDES0002C01R02", search_words="skjutmått"),
        thing("00000400", "AA batteries", "GRILHE0012C01R01"),
        thing("00000401", "Power bank", "DRWLHE0001200000"),
        thing("00000500", "Scissors", "STDDES0001000000"),
    ],
    "aliases": [{"alias": "Desk", "target": "DES", "display": 1}, {"alias": "calipers", "target": "00000300", "display": 0}],
    "synonyms": {"sax": ["scissors"], "scissors": ["sax"]},
})


def test_home_and_room_cards_zoom_like_the_web_ui():
    assert [c.title for c in WORLD.page("")["cards"]] == ["Kontoret"]
    cards = WORLD.page("room/KON")["cards"]
    assert {c.route for c in cards} == {"item/00000002", "item/00000025"}
    assert WORLD.title("item/00000002") == "Desk"                       # its code's display name
    assert WORLD.count("room/KON") == 7


def test_furniture_with_drawers_shows_them_inline_and_a_drawer_holding_one_grid_skips_to_it():
    page = WORLD.page("item/00000025")
    assert page["inline"] and [z.kind for z in page["zones"]] == ["levels"]
    levels = page["zones"][0].data["levels"]
    assert [lv["name"] for lv in levels] == ["Drawer 3", "Drawer 2", "Drawer 1"]
    assert WORLD.resolve("level/DRWLHE0001/3") == "area/LHE0013"         # only a grid in that drawer: skip it
    assert WORLD.resolve("level/DRWLHE0001/2") == "level/DRWLHE0001/2"   # a grid and a power bank: a page
    assert [WORLD.title(r) for r in WORLD.chain("area/LHE0012")] == ["Home", "Kontoret", "Left HELMER", "Drawers",
                                                                      "Drawer 2", "Battery grid"]


def test_grid_blocks_keep_their_footprints_and_draw():
    data = WORLD.grid(WORLD.L["GRIDES0002"])
    assert (data["cols"], data["rows"]) == (4, 3)
    bin_ = next(b for b in data["blocks"] if b["item"]["id"] == "00000217")
    assert (bin_["w"], bin_["h"], bin_["assumed"]) == (4, 1, True)
    drawing = draw_grid(WORLD, WORLD.L["GRIDES0002"], data, 80, "00000217").plain
    assert "Generic" in drawing and "Calipers" in drawing and "C04" in drawing


def test_search_finds_nicknames_search_words_and_swedish():
    assert [i["id"] for i in WORLD.search("skjutmått")] == ["00000300"]
    assert [i["id"] for i in WORLD.search("sax")] == ["00000500"]
    assert [i["id"] for i in WORLD.search("1×4")] == ["00000217"]


NOTE = """# Gremlin housekeeping

## 2026-11-17 · Desk grid (GRIDES0002)

- [ ] **Generic 1×4 Gridfinity bin** `00000217`: spec ID: GFST0104 _(check which one is right)_
- [ ] **Small spool** `00000009`, `00000010`: specs: measure the gauge _(unknown)_
- [x] **Calipers** `00000300`: category: Tools
"""


def test_inbox_reads_applies_and_ticks(tmp_path):
    note = tmp_path / hk.NOTE
    note.parent.mkdir(parents=True)
    note.write_text(NOTE, encoding="utf-8")
    items = hk.load(tmp_path)
    assert [(s.ids, s.edit, s.done) for s in items] == [
        (["00000217"], {"spec_code": "GFST0104"}, False),
        (["00000009", "00000010"], {}, False),                           # a to-do, not an edit
        (["00000300"], {"category": "Tools"}, True)]
    assert hk.tick(tmp_path, items[0])
    assert "- [x] **Generic 1×4 Gridfinity bin** `00000217`: spec ID: GFST0104" in note.read_text()
    assert not hk.tick(tmp_path, items[0])                               # that line changed: no blind write

"""PIXEL's model (mabel_tui/pixel.py)."""
import json
import shutil
from pathlib import Path

from mabel_tui import pixel as px
from mabel_tui.avatar import DEFAULT as AVATAR


def test_shapes():
    assert px.line(0, 0, 3, 1) == [(0, 0), (1, 0), (2, 1), (3, 1)]
    assert len(px.rect(0, 0, 3, 2)) == 10 and len(px.rect(3, 2, 0, 0, filled=True)) == 12
    ring, disc = px.ellipse(0, 0, 6, 6), px.ellipse(0, 0, 6, 6, filled=True)
    assert set(ring) < set(disc) and (3, 3) in disc and (3, 3) not in ring and (3, 0) in ring
    assert (0, 0) not in disc, "corners stay outside"


def test_fill_and_mirror():
    doc = px.Doc.new(8, 4)
    doc.put(0, 0, px.line(3, 0, 3, 3), "#ffffff")
    area = px.flood(doc.frames[0].cels[0], 0, 0)
    assert len(area) == 12 and (4, 0) not in area
    doc.put(0, 0, [(1, 1)], "#ff0000", mirror=True)
    assert doc.get(0, 0, 6, 1) == "#ff0000"


def test_layers_stack_and_hide():
    doc = px.Doc.new(2, 1)
    top = doc.add_layer(0)
    doc.put(0, 0, [(0, 0), (1, 0)], "#111111")
    doc.put(0, top, [(1, 0)], "#222222")
    assert doc.composite(0)[0] == ["#111111", "#222222"]
    doc.layers[top].visible = False
    assert doc.composite(0)[0] == ["#111111", "#111111"]


def test_frames_keep_tags_in_step():
    doc = px.Doc.new(2, 2)
    for i in range(3):
        doc.add_frame(i)
    doc.tags = [px.Tag("idle", 0, 1), px.Tag("talk", 2, 3)]
    doc.add_frame(0)                                       # inside idle
    assert (doc.tags[0].start, doc.tags[0].end, doc.tags[1].start) == (0, 2, 3)
    doc.remove_frame(3)
    assert (doc.tags[1].start, doc.tags[1].end) == (3, 3)


def test_undo_redo_and_transforms():
    doc, hist = px.Doc.new(3, 2), px.History()
    hist.push(doc)
    doc.put(0, 0, [(0, 0)], "#abcdef")
    assert hist.step(doc, back=True) and doc.get(0, 0, 0, 0) is None
    assert hist.step(doc, back=False) and doc.get(0, 0, 0, 0) == "#abcdef"
    doc.rotate()
    assert (doc.w, doc.h) == (2, 3) and doc.get(0, 0, 1, 0) == "#abcdef"
    doc.flip("h")
    assert doc.get(0, 0, 0, 0) == "#abcdef"
    doc.resize(4, 1)
    assert (doc.w, doc.h) == (4, 1) and doc.frames[0].cels[0][0] == ["#abcdef", None, None, None]


def test_mpx_and_png_round_trip(tmp_path):
    doc = px.Doc.new(4, 4)
    doc.put(0, 0, [(1, 2)], "#ff8800")
    doc.add_frame(0)
    doc.tags = [px.Tag("walk", 0, 1, {"direction": "pingpong"})]
    back = px.open_any(px.save(doc, tmp_path / "s.mpx"))
    assert px.to_json(back) == px.to_json(doc)
    strip = px.open_any(px.save(doc, tmp_path / "s.png"))
    assert len(strip.frames) == 2 and strip.get(1, 0, 1, 2) == "#ff8800" and "#ff8800" in strip.palette
    px.export(doc, tmp_path / "big.png", scale=4)
    from PIL import Image
    assert Image.open(tmp_path / "big.png").size == (16, 16)
    px.export(doc, tmp_path / "sheet.png", how="sheet", columns=1)
    assert Image.open(tmp_path / "sheet.png").size == (4, 8)


def test_avatar_folder_opens_as_tags_and_saves_back(tmp_path):
    folder = tmp_path / "avatar"
    shutil.copytree(AVATAR, folder)
    doc = px.open_any(folder)
    manifest = json.loads((AVATAR / "avatar.json").read_text())
    assert [t.name for t in doc.tags] == ["idle", "talk"] and (doc.w, doc.h) == (28, 28)
    assert [doc.frames[i].ms for i in range(doc.tags[0].start, doc.tags[0].end + 1)] == manifest["idle"]["ms"]
    doc.put(doc.tags[1].start, 0, [(0, 0)], "#123456")
    px.save(doc)
    again = px.open_any(folder)
    assert again.get(again.tags[1].start, 0, 0, 0) == "#123456"
    saved = json.loads((folder / "avatar.json").read_text())
    assert saved["idle"]["hold"] == manifest["idle"]["hold"] and saved["talk"]["file"] == "talk.png"

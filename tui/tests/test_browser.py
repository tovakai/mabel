"""CREATIVE's file browser (mabel_tui/browser.py) from PIXEL and TRACKER, driven by keys."""
import asyncio
import time

from mabel_tui import pixel as px
from mabel_tui import tracker as tk
from mabel_tui import pixelview, trackerview
from mabel_tui.browser import Browser
from mabel_tui.pixelview import PixelView
from mabel_tui.trackerview import TrackerView

import test_pixelview as host_pixel
import test_trackerview as host_tracker


def _names(screen):
    return [e.label for e in screen.entries]


def test_pixel_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(pixelview, "LAST_FILE", tmp_path / "last")
    a = px.Doc.new(4, 4)
    a.put(0, 0, [(0, 0)], "#ff0000")
    px.save(a, tmp_path / "older.mpx")
    time.sleep(0.01)
    px.save(px.Doc.new(8, 8), tmp_path / "newer.mpx")

    async def run():
        app = host_pixel.Host(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            ed = app.query_one(PixelView)
            ed.focus_canvas()
            await pilot.press("ctrl+o")
            b = app.screen
            assert isinstance(b, Browser)
            assert _names(b)[0] == "Mabel's avatar" and _names(b)[1:] == ["newer.mpx", "older.mpx"], "newest first"
            await pilot.press("j", "j")                                       # older.mpx
            assert "4×4 · 1 frame" in b.draw_preview(40, 20).plain
            await pilot.press("enter")
            assert ed.doc.path.name == "older.mpx" and ed.doc.get(0, 0, 0, 0) == "#ff0000"
            await pilot.press("space")                                        # now dirty
            assert ed.dirty
            await pilot.press("ctrl+o", "k")                                  # starts on older.mpx; up: newer
            await pilot.press("enter")
            assert app.screen.confirm[0] == "dirty"
            await pilot.press("s")                                            # save older, then open newer
            assert ed.doc.path.name == "newer.mpx" and px.open_any(tmp_path / "older.mpx").get(0, 0, 0, 0) is not None
            await pilot.press("ctrl+o", "m", *"project", "enter")            # a project folder
            assert (tmp_path / "project").is_dir()
            await pilot.press("enter", "n", *"hero", "enter")                # into it, a new sprite
            assert ed.doc.path == tmp_path / "project" / "hero.mpx" and (ed.doc.w, ed.doc.h) == (8, 8)
            await pilot.press("ctrl+o")
            b = app.screen
            await pilot.press("j", "r", *(["backspace"] * 8), *"villain", "enter")
            assert ed.doc.path == tmp_path / "project" / "villain.mpx", "the open file followed its rename"
            await pilot.press("D", "D")
            assert not (tmp_path / "project" / "villain.mpx").exists() and list((tmp_path / ".trash").iterdir())
            await pilot.press("h")                                           # back up to the root
            await pilot.press("slash", *"new", "enter")
            assert _names(app.screen)[-1] == "newer.mpx" and len(_names(app.screen)) == 2   # avatar + match
            await pilot.press("escape")
            assert not isinstance(app.screen, Browser)
    asyncio.run(run())


def test_tracker_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(trackerview, "LAST_FILE", tmp_path / "last")
    tk.save(tk.demo(), tmp_path / "demo.mtk")
    (tmp_path / "demo.wav").write_bytes(b"RIFF")

    async def run():
        app = host_tracker.Host(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            tv = app.query_one(TrackerView)
            monkeypatch.setattr(tv, "_ensure_audio", host_tracker._fake_audio(tv))
            tv.focus_panel("song")
            await pilot.pause()
            await pilot.press("ctrl+o")
            b = app.screen
            assert [e.kind for e in b.entries] == ["file", "other"]
            card = b.draw_preview(50, 30).plain
            assert "first light" in card and "128 bpm" in card and "once through" in card
            await pilot.press("j", "enter")
            assert isinstance(app.screen, Browser) and "export" in app.screen.message
            await pilot.press("n", *"night drive", "enter")
            assert tv.song.path == tmp_path / "night drive.mtk" and tv.song.title == "night drive"
            await pilot.press("colon", *"e", "enter")                       # :e alone opens the browser
            await pilot.press("enter")
            assert tv.song.path.name in ("demo.mtk", "night drive.mtk")
    asyncio.run(run())

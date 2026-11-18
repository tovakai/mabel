"""PIXEL driven by keys in a headless app (mabel_tui/pixelview.py)."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from textual.app import App

from mabel_tui import pixelview
from mabel_tui.palette import css_variables
from mabel_tui.pixelview import PixelView

THEME = Path(pixelview.__file__).with_name("theme.tcss")


class Host(App):
    CSS_PATH = THEME

    def __init__(self, tmp):
        super().__init__()
        self.cfg = SimpleNamespace(pixel_dir=str(tmp), avatar="")

    def get_css_variables(self):
        return {**super().get_css_variables(), **css_variables()}

    def compose(self):
        yield PixelView(id="pixel")

    def action_nav_out(self):
        pass


def test_drawing_with_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(pixelview, "LAST_FILE", tmp_path / "last")

    async def run():
        app = Host(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            ed = app.query_one(PixelView)
            ed.focus_canvas()
            await pilot.press("l", "l", "j", "space")                 # pencil at (2, 1)
            assert ed.doc.get(0, 0, 2, 1) == ed.fg
            await pilot.press("r", "space", "L", "j", "j", "space")    # rect (2,1)→(6,3)
            assert ed.doc.get(0, 0, 6, 3) == ed.fg and ed.doc.get(0, 0, 4, 2) is None
            await pilot.press("u")
            assert ed.doc.get(0, 0, 6, 3) is None and ed.doc.get(0, 0, 2, 1) == ed.fg
            await pilot.press("N", "A", "greater_than_sign")
            assert len(ed.doc.frames) == 2 and len(ed.doc.layers) == 2
            fg0 = ed.fg
            await pilot.press("full_stop")
            assert ed.fg != fg0
            await pilot.press("comma")
            assert ed.fg == fg0
            canvas = app.query_one(pixelview.Canvas)
            await canvas._on_key(pixelview.events.Key("ctrl+alt+8", "]"))      # AltGr+9 on a Swedish layout
            assert ed.fg != fg0
            await pilot.press("P")
            assert type(app.focused).__name__ == "Palette"
            await pilot.press("right", "right", "down", "enter")
            assert ed.fg == ed.doc.palette[ed.doc.palette.index(fg0) + 1 + 2 + 8] and app.focused is canvas
            await pilot.press("P", "b")
            assert ed.bg is not None and app.focused is canvas
            await pilot.press("colon")
            await pilot.press(*"w hello.png", "enter")
            assert (tmp_path / "hello.png").exists() and not ed.dirty, ed.message
            await pilot.press("colon", *"export strip 3", "enter")
            assert (tmp_path / "hello-strip@3x.png").exists(), ed.message
            await pilot.press("colon", *"new 8 8 pico8", "enter")
            assert (ed.doc.w, ed.doc.palette[0]) == (8, "#000000")
            await pilot.pause()
            fitted = ed.zoom
            assert fitted > 3, "a small sprite opens big"
            for z in ("minus", "minus", "minus", "plus"):
                await pilot.press(z)
            assert ed.zoom == fitted - 2
            await pilot.press("0")
            assert ed.zoom == fitted
            await pilot.pause()
    asyncio.run(run())

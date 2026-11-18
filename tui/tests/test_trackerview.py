"""TRACKER driven by keys in a headless app (mabel_tui/trackerview.py); audio is faked."""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from textual.app import App

from mabel_tui import tracker as tk
from mabel_tui import tracksynth as ts
from mabel_tui import trackerview
from mabel_tui.palette import css_variables
from mabel_tui.trackerview import TrackerView

THEME = Path(trackerview.__file__).with_name("theme.tcss")


class Host(App):
    CSS_PATH = THEME

    def __init__(self, tmp):
        super().__init__()
        self.cfg = SimpleNamespace(tracker_dir=str(tmp), tracker_rate=8000)

    def get_css_variables(self):
        return {**super().get_css_variables(), **css_variables()}

    def compose(self):
        yield TrackerView(id="tracker")

    def action_nav_out(self):
        pass


def _fake_audio(tv):
    def ensure():
        if tv.audio is None:
            tv.audio = ts.LocalAudio(tv.song, 8000, output=False, midi=False)
        tv._last_sound = time.monotonic()
        tv._sync()
    return ensure


def test_making_a_tune_with_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(trackerview, "LAST_FILE", tmp_path / "last")

    async def run():
        app = Host(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            tv = app.query_one(TrackerView)
            monkeypatch.setattr(tv, "_ensure_audio", _fake_audio(tv))
            await pilot.press("colon", *"new! 4", "enter")
            tv.focus_panel("song")
            await pilot.pause()
            await pilot.press("j", "l", "l", "l", "n")                 # row 1, track 4: a new chain
            assert tv.song.rows[1][3] == 0 and tv.track == 3
            await pilot.press("enter", "n")                            # into CHAIN, a new phrase
            assert tv.phrase_id() == 0
            await pilot.press("l", "0", "c")                           # transpose 0C
            assert tv.song.chains[0][0].transpose == 0x0C
            await pilot.press("h", "enter", "i", "z", "x", "c", "equals_sign", "escape")
            notes = [s.note for s in tv.song.phrases[0][:4]]
            assert notes == [60, 62, 64, tk.NOTE_OFF] and not tv.insert
            await pilot.press("up", "up", "up", "up", "l", "l", "v", "l", "4", "2")   # V42 on step 0
            st = tv.song.phrases[0][0]
            assert (st.cmd, st.val) == ("V", 0x42), tv.message
            await pilot.press("u")
            assert tv.song.phrases[0][0].val == 0x04
            await pilot.press("H", "Y")                                # in CHAIN: copy the whole chain
            assert tv.panel == "chain" and tv.message == "copied the chain"
            await pilot.press("N")                                     # step 0's phrase becomes a clone
            assert tv.song.chains[0][0].phrase == 1 and tv.song.phrases[1][0].note == 60
            await pilot.press("j", "n")                                # step 1: a fresh phrase
            assert tv.song.chains[0][1].phrase == 2 and tv.song.phrases[2][0].note is None
            await pilot.press("space")
            assert tv.audio.playing and tv.audio.mode == "chain" and tv.audio.focus == (3, 0)
            tv.audio.render(8000)
            assert tv.audio.engine.voices[3].inst is not None and tv.audio.position(3) is not None
            assert tv.audio.engine.song.phrases[0][0].note == 60, "the edited song reached the audio side"
            await pilot.press("space")
            assert not tv.audio.playing
            for cmd in ("bpm 140", "w tune", "export mid", "export wav"):
                await pilot.press("colon", *cmd, "enter")
                assert not tv.message.startswith(("✖", "unknown")), tv.message
            assert tv.song.bpm == 140 and (tmp_path / "tune.mtk").exists()
            assert (tmp_path / "tune.mid").exists() and (tmp_path / "tune.wav").exists()
            await pilot.press("colon", *"demo", "enter")
            assert tv.message.startswith(("the demo", "unsaved")), tv.message
    asyncio.run(run())

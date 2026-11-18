"""CH-07 CREATIVE: MABEL-OS Creative Suite. PIXEL and TRACKER as apps of one channel (suite.py).

F7 / alt+7 opens the channel; pressed again while it's on screen it moves to the next app.
"""
from __future__ import annotations

from .pixelview import PixelView
from .suite import Suite
from .trackerview import TrackerView


class Creative(Suite):
    APPS = [("pixel", "PIXEL", "sprites and animation"),
            ("tracker", "TRACKER", "chip music + MIDI · song → chain → phrase")]
    FKEY = "F7"

    def make(self):
        return [PixelView(id="pixel"), TrackerView(id="tracker")]

    def shown(self, key: str) -> None:
        if key == "tracker":
            self.query_one(TrackerView).warm()          # the audio process, so the first note isn't late

    def focus_app(self, key: str) -> None:
        if key == "pixel":
            self.query_one(PixelView).focus_canvas()
        else:
            self.query_one(TrackerView).focus_panel()

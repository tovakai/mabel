"""MABEL-OS colours in one place, so a screen that renders them differently can get its own set.

The default palette is the PC look. The "deck" preset pushes the ambers towards red for the 7"
panel on the Pi, which shows #ffb000 as plain yellow (a gamma correction via gammastep had no
effect there). Pick it in config.toml, and nudge single colours if needed:

    palette = "deck"
    [colors]
    amber = "#ff7000"

Read at import time (before the other modules build their styles), straight from the config file.
"""
from __future__ import annotations

import tomllib
from types import SimpleNamespace

from .config import CONFIG

DEFAULT = {
    "bg": "#0c0804", "panel": "#140e07",
    "amber": "#ffb000", "amber_hi": "#ffd066", "amber_dim": "#a86b00", "amber_faint": "#3d2a08",
    "beige": "#d9cba4", "beige_dark": "#8a7b58", "casing_text": "#2a2114",
    "teal": "#3fd6c6", "red": "#ff5a36", "orange": "#ff8a3d", "sand": "#e5b97a", "mint": "#9fd8a8",
    "rust": "#6b1d0a",
}

PRESETS = {
    "default": {},
    # The Pi's 7" panel: everything leans yellow, so the ambers lean red and the beiges lean pink.
    "deck": {
        # Round 3 (2026-11-18): #ffb000 read yellow, #ff7a00 chocolate brown, #ff9a1a dim orange.
        # Same hue, lifted green/blue = lighter, brighter oranges (the panel darkens mid tones).
        "amber": "#ffa640", "amber_hi": "#ffd28a", "amber_dim": "#e88f38", "amber_faint": "#4a2e10",
        "beige": "#f2dcc2", "beige_dark": "#c4a482", "casing_text": "#2a1a10",
        "orange": "#ff8a50", "red": "#ff6050", "sand": "#f4c08e",
    },
}


def load(path=CONFIG) -> dict:
    colours = dict(DEFAULT)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return colours
    colours.update(PRESETS.get(str(data.get("palette") or "default"), {}))
    for key, value in (data.get("colors") or {}).items():
        if key in colours and isinstance(value, str) and value.startswith("#") and len(value) == 7:
            colours[key] = value
    return colours


P = SimpleNamespace(**load())


def css_variables() -> dict[str, str]:
    """The palette as the theme's $mo-* variables (theme.tcss uses $mo-amber-hi etc.)."""
    return {"mo-" + key.replace("_", "-"): value for key, value in vars(P).items()}

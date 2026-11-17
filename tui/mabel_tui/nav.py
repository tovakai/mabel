"""Vim-style section navigation, so MABEL-OS works on a keyboard-only cyberdeck.

Nav mode: no widget has focus; one *section* (a panel marked with class "section") is lit.
h j k l move to the nearest section in that direction on screen; i, a or enter goes in
(focusing the section's primary widget). Inside a section its own keys apply, j k h l act as
the arrows wherever the widget doesn't use the letter, and esc comes back out to nav mode.
The mouse keeps working: clicking into something simply means you're inside it.
"""
from __future__ import annotations

from textual.widget import Widget

DIRS = {"h": (-1, 0), "l": (1, 0), "k": (0, -1), "j": (0, 1)}
ARROWS = {"h": "left", "j": "down", "k": "up", "l": "right"}


def section_of(widget: Widget | None) -> Widget | None:
    while widget is not None:
        if widget.has_class("section"):
            return widget
        widget = widget.parent
    return None


def visible_sections(pane: Widget) -> list[Widget]:
    return [s for s in pane.query(".section") if s.display and s.region.width > 0 and s.region.height > 0]


def neighbour(current: Widget, sections: list[Widget], key: str) -> Widget | None:
    """The nearest section in direction ``key`` from ``current`` (by on-screen centres)."""
    dx, dy = DIRS[key]
    cx, cy = current.region.x + current.region.width / 2, current.region.y + current.region.height / 2
    best, best_score = None, None
    for s in sections:
        if s is current:
            continue
        sx, sy = s.region.x + s.region.width / 2, s.region.y + s.region.height / 2
        ahead = (sx - cx) * dx + (sy - cy) * dy                       # distance along the direction
        if ahead <= 0:
            continue
        # Prefer sections that overlap on the other axis (a panel straight above beats one diagonal).
        if dx:
            overlap = min(s.region.bottom, current.region.bottom) - max(s.region.y, current.region.y)
            side = abs(sy - cy)
        else:
            overlap = min(s.region.right, current.region.right) - max(s.region.x, current.region.x)
            side = abs(sx - cx)
        score = ahead + side * 2 - (50 if overlap > 0 else 0)
        if best_score is None or score < best_score:
            best, best_score = s, score
    return best


def enter(section: Widget) -> None:
    """Go into a section: its primary widget (``section_primary`` selector, a callable, or itself)."""
    primary = getattr(section, "section_primary", None)
    if callable(primary):
        primary()
        return
    if primary:
        found = section.query(primary)
        if found:
            found.first().focus()
            return
    section.focus()


def mark(widget: Widget, primary=None) -> Widget:
    """Make ``widget`` a section; ``primary`` is what i/a/enter focuses (selector or callable)."""
    widget.add_class("section")
    widget.section_primary = primary
    return widget

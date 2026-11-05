"""Phase 01: a tiny persistent affect state and its time-of-day mood projection.

These are synthetic presentation signals, not measured feelings. Updating them
needs no model call, network request or background worker: decay is applied
lazily, from elapsed time, whenever the state is read.

Proof of concept input: local time of day. Conversation-driven inputs come
later; the channels and residue are already here for them.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
# channel: (baseline, half-life in hours)
CHANNELS = {
    "calm": (0.6, 6.0),
    "curiosity": (0.6, 8.0),
    "restlessness": (0.25, 3.0),
    "playfulness": (0.35, 4.0),
}
MIN_MOOD_MINUTES = 20   # hysteresis: a selected mood holds at least this long
RESIDUE_LIMIT = 20

# Night-owl local-time schedule (start hour, mood); wraps around midnight.
# Everything before 23:00 is the early part of her day.
SCHEDULE = (
    (2.5, "restless"),   # wired, late night
    (5.0, "tired"),      # an all-nighter
    (10.0, "groggy"),    # her morning
    (16.0, "relaxed"),   # her daytime
    (23.0, "playful"),   # prime time
)


def decayed(value: float, baseline: float, half_life_hours: float, elapsed_hours: float) -> float:
    """value(t) = baseline + (value - baseline) * 2^(-elapsed / half_life), clamped to [0, 1]."""
    if elapsed_hours <= 0:
        return max(0.0, min(1.0, value))
    result = baseline + (value - baseline) * 2 ** (-elapsed_hours / half_life_hours)
    return max(0.0, min(1.0, result))


def scheduled_mood(local: datetime) -> str:
    hour = local.hour + local.minute / 60
    mood = SCHEDULE[-1][1]          # before the first entry: still last night's mood
    for start, name in SCHEDULE:
        if hour >= start:
            mood = name
    return mood


def energy_for(local: datetime) -> float:
    """A smooth night-owl curve: lowest early afternoon, highest around 01:00 (presentation only)."""
    hour = local.hour + local.minute / 60
    import math
    return round(0.5 + 0.45 * math.sin((hour - 19.0) / 24 * 2 * math.pi), 3)


@dataclass
class Projection:
    mood: str
    energy: float
    source: str          # "time_of_day" | "hysteresis" | "manual"
    since: str
    changed: bool


class AffectStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _default(self, now: datetime) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": now.isoformat(),
            "channels": {name: baseline for name, (baseline, _) in CHANNELS.items()},
            "projection": {"mood": None, "since": None, "energy": None, "source": None},
            "residue": [],
        }

    def read(self, now: datetime) -> dict:
        try:
            state = json.loads(self.path.read_text())
            if state.get("schema_version") != SCHEMA_VERSION or not isinstance(state.get("channels"), dict):
                raise ValueError("schema")
        except (OSError, ValueError, AttributeError):
            return self._default(now)  # missing or corrupt: start from baseline
        try:
            then = datetime.fromisoformat(state["updated_at"])
        except (KeyError, TypeError, ValueError):
            then = now
        elapsed = max(0.0, (now - then).total_seconds() / 3600)
        channels = {}
        for name, (baseline, half_life) in CHANNELS.items():
            raw = state["channels"].get(name, baseline)
            raw = raw if isinstance(raw, (int, float)) else baseline
            channels[name] = round(decayed(float(raw), baseline, half_life, elapsed), 4)
        # Unknown (retired) channels are kept, untouched, for history.
        channels.update({k: v for k, v in state["channels"].items() if k not in CHANNELS})
        state["channels"] = channels
        state["updated_at"] = now.isoformat()
        return state

    def write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".affect-", suffix=".json")
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def project(self, local_now: datetime) -> tuple[Projection, dict]:
        """Select the mood for ``local_now`` (timezone-aware); persists only on change."""
        now = local_now.astimezone(timezone.utc)
        state = self.read(now)
        current = state["projection"]
        target = scheduled_mood(local_now)
        since = current.get("since")
        try:
            held = now - datetime.fromisoformat(since) if since else None
        except ValueError:
            held = None
        manual_until = current.get("manual_until")
        if current.get("source") == "manual" and manual_until and now.isoformat() < manual_until:
            return Projection(current["mood"], energy_for(local_now), "manual", since, False), state
        if current.get("mood") and current["mood"] != target and held is not None \
                and timedelta(0) <= held < timedelta(minutes=MIN_MOOD_MINUTES):
            return Projection(current["mood"], energy_for(local_now), "hysteresis", since, False), state
        changed = current.get("mood") != target
        if changed:
            state["projection"] = {"mood": target, "since": now.isoformat(),
                                   "energy": energy_for(local_now), "source": "time_of_day"}
            state["residue"] = (state.get("residue") or [])[-(RESIDUE_LIMIT - 1):] + [
                {"at": now.isoformat(), "source": "time_of_day", "mood": target}]
            self.write(state)
        return Projection(target, energy_for(local_now), "time_of_day",
                          state["projection"]["since"], changed), state

    def set_override(self, mood: str, until: datetime, local_now: datetime) -> Projection:
        """Pin ``mood`` until ``until`` (both timezone-aware); the schedule resumes afterwards."""
        now = local_now.astimezone(timezone.utc)
        state = self.read(now)
        state["projection"] = {"mood": mood, "since": now.isoformat(), "energy": energy_for(local_now),
                               "source": "manual", "manual_until": until.astimezone(timezone.utc).isoformat()}
        state["residue"] = (state.get("residue") or [])[-(RESIDUE_LIMIT - 1):] + [
            {"at": now.isoformat(), "source": "manual", "mood": mood}]
        self.write(state)
        return Projection(mood, energy_for(local_now), "manual", now.isoformat(), True)

    def clear_override(self, local_now: datetime) -> Projection:
        """Drop a manual mood immediately (no hysteresis hold) and return the scheduled one."""
        now = local_now.astimezone(timezone.utc)
        state = self.read(now)
        if state["projection"].get("source") == "manual":
            state["projection"] = {"mood": None, "since": None, "energy": None, "source": None}
            self.write(state)
        return self.project(local_now)[0]

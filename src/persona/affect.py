"""Phase 01/02: a tiny persistent affect state and its mood projection.

These are synthetic presentation signals, not measured feelings. Updating them
needs no model call, network request or background worker: decay is applied
lazily, from elapsed time, whenever the state is read.

Mood inputs (Phase 2 "mood rhythms"):
- local time of day: the night-owl schedule is the starting point;
- recent conversation activity: a decayed rolling sum of tokens exchanged in
  chat (warming up out of grogginess, getting lively);
- recent background activity: the same for work done without the user in the
  conversation (Dottie today; self-directed research later). Kept separate
  from conversation activity on purpose;
- time since the last interaction: not a mood input, but reported to the
  composer so a social turn can notice a long gap.

Mood is presentation only. It never changes competence, verification, tools
or authorization.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
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
# activity kind: half-life in hours of the decayed token sum
ACTIVITY = {"conversation": 0.75, "background": 2.0}
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

# Activity rules, applied on top of the schedule (decayed token sums).
# ~330 tokens is one ordinary exchange; with a 45 min half-life, warming up
# takes a handful of exchanges and "lively" a sustained, chatty hour.
WARM_UP_TOKENS = 1500      # groggy -> relaxed
LIVELY_TOKENS = 5000       # relaxed -> playful
BUSY_BACKGROUND_TOKENS = 4000  # quiet groggy/relaxed -> restless while background work piles up

_LOCK = threading.Lock()


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


def select_mood(scheduled: str, activity: dict) -> tuple[str, str]:
    """(mood, source): the schedule, adjusted by recent activity."""
    conversation = float(activity.get("conversation") or 0)
    background = float(activity.get("background") or 0)
    if scheduled == "groggy" and conversation >= WARM_UP_TOKENS:
        return "relaxed", "conversation_warmup"
    if scheduled == "relaxed" and conversation >= LIVELY_TOKENS:
        return "playful", "conversation_lively"
    if scheduled in ("groggy", "relaxed") and conversation < WARM_UP_TOKENS \
            and background >= BUSY_BACKGROUND_TOKENS:
        return "restless", "background_busy"
    return scheduled, "time_of_day"


def energy_for(local: datetime) -> float:
    """A smooth night-owl curve: lowest early afternoon, highest around 01:00 (presentation only)."""
    hour = local.hour + local.minute / 60
    return round(0.5 + 0.45 * math.sin((hour - 19.0) / 24 * 2 * math.pi), 3)


@dataclass
class Projection:
    mood: str
    energy: float
    source: str          # "time_of_day" | rule source | "hysteresis" | "manual"
    since: str
    changed: bool
    gap_hours: float | None = None   # time since the last conversation turn


class AffectStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _default(self, now: datetime) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": now.isoformat(),
            "channels": {name: baseline for name, (baseline, _) in CHANNELS.items()},
            "activity": {kind: 0.0 for kind in ACTIVITY},
            "last_interaction_at": None,
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
        stored = state.get("activity") if isinstance(state.get("activity"), dict) else {}
        activity = {}
        for kind, half_life in ACTIVITY.items():
            raw = stored.get(kind, 0.0)
            raw = float(raw) if isinstance(raw, (int, float)) and raw > 0 else 0.0
            activity[kind] = round(raw * 2 ** (-elapsed / half_life), 1)
        state["activity"] = activity
        state.setdefault("last_interaction_at", None)
        state.setdefault("projection", {"mood": None, "since": None, "energy": None, "source": None})
        state["updated_at"] = now.isoformat()
        return state

    def write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".affect-", suffix=".json")
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    @staticmethod
    def _gap_hours(state: dict, now: datetime) -> float | None:
        try:
            last = datetime.fromisoformat(state.get("last_interaction_at") or "")
        except ValueError:
            return None
        return max(0.0, (now - last).total_seconds() / 3600)

    def record_activity(self, kind: str, tokens: int, local_now: datetime) -> dict:
        """Add ``tokens`` to a decayed activity sum; a conversation turn also stamps the interaction time."""
        if kind not in ACTIVITY:
            raise ValueError(f"unknown activity kind {kind!r}")
        now = local_now.astimezone(timezone.utc)
        with _LOCK:
            state = self.read(now)
            state["activity"][kind] = round(state["activity"][kind] + max(0, int(tokens)), 1)
            if kind == "conversation":
                state["last_interaction_at"] = now.isoformat()
            self.write(state)
        return state

    def project(self, local_now: datetime) -> tuple[Projection, dict]:
        """Select the mood for ``local_now`` (timezone-aware); persists only on change."""
        now = local_now.astimezone(timezone.utc)
        with _LOCK:
            state = self.read(now)
            gap = self._gap_hours(state, now)
            current = state["projection"]
            scheduled = scheduled_mood(local_now)
            target, source = select_mood(scheduled, state["activity"])
            since = current.get("since")
            try:
                held = now - datetime.fromisoformat(since) if since else None
            except ValueError:
                held = None
            manual_until = current.get("manual_until")
            energy = energy_for(local_now)
            if current.get("source") == "manual" and manual_until and now.isoformat() < manual_until:
                return Projection(current["mood"], energy, "manual", since, False, gap), state
            if current.get("mood") and current["mood"] != target and held is not None \
                    and timedelta(0) <= held < timedelta(minutes=MIN_MOOD_MINUTES):
                return Projection(current["mood"], energy, "hysteresis", since, False, gap), state
            changed = current.get("mood") != target
            if changed or current.get("source") != source:
                state["projection"] = {"mood": target, "since": since if not changed else now.isoformat(),
                                       "energy": energy, "source": source}
                if changed:
                    state["residue"] = (state.get("residue") or [])[-(RESIDUE_LIMIT - 1):] + [
                        {"at": now.isoformat(), "source": source, "mood": target}]
                self.write(state)
            return Projection(target, energy, source, state["projection"]["since"], changed, gap), state

    def set_override(self, mood: str, until: datetime, local_now: datetime) -> Projection:
        """Pin ``mood`` until ``until`` (both timezone-aware); the schedule resumes afterwards."""
        now = local_now.astimezone(timezone.utc)
        with _LOCK:
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
        with _LOCK:
            state = self.read(now)
            if state["projection"].get("source") == "manual":
                state["projection"] = {"mood": None, "since": None, "energy": None, "source": None}
                self.write(state)
        return self.project(local_now)[0]

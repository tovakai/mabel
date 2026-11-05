"""Phase 5: the deterministic wake policy. It decides *whether* Mabel may wake, never *what* to do.

Unknown or conflicting availability means no. Model decisions (what to
research) happen later, inside the session, and are logged separately.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

MIN_IDLE_MINUTES = 5
MAX_PER_DAY = 2
MIN_GAP = timedelta(hours=3)
RESUME_WITHIN = timedelta(hours=24)
SESSION_CAP = timedelta(minutes=20)


@dataclass
class Decision:
    allow: bool
    reasons: list[str] = field(default_factory=list)
    resume: str | None = None

    def as_dict(self) -> dict:
        return {"allow": self.allow, "reasons": self.reasons, "resume": self.resume}


def decide(signals, state: dict, *, now: datetime, enabled: bool, paused: bool, research_running: bool) -> Decision:
    """``now`` is local time; ``state`` holds past autonomous starts and a yielded session, if any."""
    reasons = []
    if not enabled:
        reasons.append("autonomy is switched off")
    if paused:
        reasons.append("paused by you")
    if research_running:
        reasons.append("a research session is already running")
    if signals.chat_active is None:
        reasons.append("can't tell whether a chat is running")
    elif signals.chat_active:
        reasons.append("a chat is running")
    if signals.idle == "unknown":
        reasons.append("idle state unknown")
    elif signals.idle == "active":
        reasons.append("you're at the PC")
    elif (signals.idle_minutes or 0) < MIN_IDLE_MINUTES:
        reasons.append(f"idle only {signals.idle_minutes:.0f} min (needs {MIN_IDLE_MINUTES})")
    if signals.gpu == "unknown":
        reasons.append(f"GPU state unknown ({signals.gpu_reason})")
    elif signals.gpu == "busy":
        reasons.append(f"GPU in use: {signals.gpu_reason}")
    if not signals.strata:
        reasons.append("Mabel's model server isn't answering")
    starts = [datetime.fromisoformat(s) for s in state.get("starts", [])]
    today = [s for s in starts if s.date() == now.date()]
    yielded = state.get("yielded") or {}
    resume = None
    if yielded.get("session") and now - datetime.fromisoformat(yielded["at"]) < RESUME_WITHIN:
        resume = yielded["session"]          # finishing an interrupted session isn't a new start
    if not resume:
        if len(today) >= MAX_PER_DAY:
            reasons.append(f"already {len(today)} sessions today")
        if starts and now - max(starts) < MIN_GAP:
            reasons.append(f"last session started {int((now - max(starts)).total_seconds() // 60)} min ago (gap {int(MIN_GAP.total_seconds() // 3600)} h)")
    return Decision(not reasons, reasons or ["all clear"], resume if not reasons else None)


def should_yield(signals, *, started: datetime, now: datetime) -> str | None:
    """While an autonomous session runs: a reason to stop now, or None."""
    if signals.idle == "active":
        return "you came back"
    if signals.idle == "unknown":
        return "idle state became unknown"
    if signals.chat_active:
        return "a chat started"
    # Her own generation raises GPU load, so while she runs only another process counts.
    if signals.gpu == "busy" and not signals.gpu_reason.startswith("GPU load"):
        return f"GPU needed: {signals.gpu_reason}"
    if now - started >= SESSION_CAP:
        return "session time cap reached"
    return None

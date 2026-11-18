"""Night shift: keep the PC awake while her idle jobs still have work, then put it to sleep.

hypridle suspends 15 min after he goes quiet, but dream, music, paper, book,
revisit and housekeeping wait for 20 min away, so on a night he goes straight
to bed the suspend always won (2026-11-16, 2026-11-18). And after Home
Assistant wakes the PC by Wake-on-LAN (06:30), nothing put it back to sleep.

A shift starts when he's away and some job is either running or waiting only
on something that passes by itself (away time, a busy GPU, research going
first). It holds the sleep inhibitor (power.hold_sleep), refreshed before its
25 min cap, and ends when nothing is left, he comes back, or SHIFT_CAP runs
out. At the end the PC is suspended only if hypridle itself tried to suspend
since he went quiet (scripts/suspend_wanted.sh notes each try; a playing video
inhibits hypridle, so it never does) and nobody else wants it awake
(power.suspend_if_clear).

After a resume with him still away (a WOL wake), the monitors are switched off
once; a mouse move or key press turns them back on (Hyprland
mouse_move_enables_dpms / key_press_enables_dpms).
"""
from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime, timedelta

from . import power

logger = logging.getLogger(__name__)

SHIFT_CAP = timedelta(minutes=75)
REFRESH_AFTER = timedelta(minutes=20)     # power.CAP_S is 25 min
RESUME_GAP_S = 60                          # suspended this much since the last tick: we just woke up
DPMS_OFF = ["hyprctl", "dispatch", 'hl.dsp.dpms({ action = "disable" })']

# Reasons that pass without anyone doing anything. A job held back only by these has work coming.
_TRANSIENT = ("Anthon hasn't been away", "GPU not free", "a research session is running",
              "research gets to go first", "a chat is running", "the GPU is already swapped",
              "idle only")

_suspended_s: float | None = None
_resumed_at: float | None = None


def waiting_only_on_time(reasons: list[str]) -> bool:
    return bool(reasons) and all(str(r).startswith(_TRANSIENT) for r in reasons)


def pending(reasons_by_job: dict[str, list[str]]) -> list[str]:
    """Jobs that are clear to go or held back only by transient reasons."""
    return [job for job, reasons in reasons_by_job.items() if not reasons or waiting_only_on_time(reasons)]


def suspended_seconds() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()


def note_resume(now_s: float | None = None, suspended: float | None = None) -> bool:
    """True on the first tick after the PC came back from suspend."""
    global _suspended_s, _resumed_at
    suspended = suspended_seconds() if suspended is None else suspended
    woke = _suspended_s is not None and suspended - _suspended_s >= RESUME_GAP_S
    _suspended_s = suspended
    if woke:
        _resumed_at = time.time() if now_s is None else now_s
    return woke


def screens_off_after_wake(sig, *, now_s: float | None = None, run=subprocess.run) -> bool:
    """Once per resume: if nobody has touched the PC since it woke, switch the monitors off."""
    global _resumed_at
    if _resumed_at is None:
        return False
    now_s = time.time() if now_s is None else now_s
    if now_s - _resumed_at < 30:
        return False                          # give a hand on the mouse a moment to register
    resumed, _resumed_at = _resumed_at, None
    if sig.idle != "idle" or sig.idle_minutes is None:
        return False
    from .signals import IDLE_TIMEOUT_S
    went_idle = now_s - (sig.idle_minutes * 60 - IDLE_TIMEOUT_S)
    if went_idle > resumed:
        return False                          # he was here after the wake
    ok = power._run(DPMS_OFF, run)
    logger.info("[heartbeat] woke while he's away: monitors off (%s)", "ok" if ok else "failed")
    return ok


def step(state: dict, *, away: bool, idle_minutes: float | None, work: list[str], research: bool,
         now: datetime, run=subprocess.run) -> str | None:
    """Advance the shift kept in ``state["shift"]``. Returns what happened, for logs and tests."""
    shift = state.get("shift")
    if not away:
        if shift:
            state["shift"] = None
            if not research:
                power.release_sleep(run)
            return "he's back"
        return None
    if shift and shift.get("spent"):
        return None                           # ran out of time; wait for him to come back
    if work or research:
        if not shift:
            shift = state["shift"] = {"since": now.isoformat(), "held_at": None}
            logger.info("[heartbeat] night shift: keeping the PC awake for %s", ", ".join(work) or "research")
        if datetime.fromisoformat(shift["since"]) + SHIFT_CAP <= now:
            shift.update(spent=True)
            if not research:
                power.release_sleep(run)
            return _end(idle_minutes, run, "time's up")
        held = shift.get("held_at")
        if not research and (not held or datetime.fromisoformat(held) + REFRESH_AFTER <= now
                             or not power.holding(run)):
            if power.hold_sleep(run, why="Mabel is finishing her night"):
                shift["held_at"] = now.isoformat()
        elif research:
            shift["held_at"] = None           # research holds its own inhibitor (same unit)
        return "working"
    if shift:
        state["shift"] = None
        power.release_sleep(run)
        return _end(idle_minutes, run, "done")
    return None


def _end(idle_minutes: float | None, run, why: str) -> str:
    went_idle = None
    if idle_minutes is not None:
        from .signals import IDLE_TIMEOUT_S
        went_idle = time.time() - (idle_minutes * 60 - IDLE_TIMEOUT_S)
    slept = power.suspend_if_clear(idle_minutes, run, went_idle=went_idle)
    logger.info("[heartbeat] night shift over (%s); %s", why, "suspending" if slept else "leaving the PC on")
    return f"{why}: {'suspended' if slept else 'left on'}"

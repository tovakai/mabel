"""Night shift (src/heartbeat/shift.py): keep the PC awake for her idle jobs, then suspend it."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.heartbeat import power, shift, signals

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 18, 6, 30, tzinfo=TZ)


@pytest.fixture
def calls(monkeypatch):
    """Record power calls; nothing here may touch the real inhibitor or suspend the PC."""
    log = []
    monkeypatch.setattr(power, "hold_sleep", lambda run=None, why="": log.append("hold") or True)
    monkeypatch.setattr(power, "release_sleep", lambda run=None: log.append("release"))
    monkeypatch.setattr(power, "holding", lambda run=None: True)
    monkeypatch.setattr(power, "suspend_if_clear",
                        lambda minutes, run=None, went_idle=None: log.append(("suspend", minutes)) or True)
    return log


def _sig(**kw):
    base = dict(idle="idle", idle_minutes=8.0, gpu="free", gpu_reason="", chat_active=False, strata=True,
                presence="not_configured", presence_entity="")
    return signals.Signals(**{**base, **kw})


def test_a_job_waiting_only_on_away_time_is_pending():
    reasons = {"dream": ["Anthon hasn't been away 20 min"], "music": ["composed (or was offered to) recently"],
               "paper": [], "book": ["Anthon hasn't been away 20 min", "GPU not free (GPU load 90%)"],
               "news": ["before 09:00", "Anthon hasn't been away 5 min"]}
    assert shift.pending(reasons) == ["dream", "paper", "book"]


def test_going_to_bed_with_a_dream_due_holds_sleep_then_suspends(calls):
    state = {}
    assert shift.step(state, away=True, idle_minutes=8.0, work=["dream"], research=False, now=NOW) == "working"
    assert calls == ["hold"] and state["shift"]["held_at"]
    shift.step(state, away=True, idle_minutes=9.0, work=["dream"], research=False, now=NOW + timedelta(minutes=1))
    assert calls == ["hold"], "no re-take every tick"
    shift.step(state, away=True, idle_minutes=30.0, work=["dream"], research=False, now=NOW + timedelta(minutes=21))
    assert calls == ["hold", "hold"], "refreshed before the inhibitor's 25 min cap"
    out = shift.step(state, away=True, idle_minutes=40.0, work=[], research=False, now=NOW + timedelta(minutes=31))
    assert out == "done: suspended" and calls[-2:] == ["release", ("suspend", 40.0)] and state["shift"] is None


def test_coming_back_ends_the_shift_without_suspending(calls):
    state = {}
    shift.step(state, away=True, idle_minutes=8.0, work=["music"], research=False, now=NOW)
    assert shift.step(state, away=False, idle_minutes=0.0, work=["music"], research=False,
                      now=NOW + timedelta(minutes=3)) == "he's back"
    assert calls == ["hold", "release"] and state["shift"] is None


def test_nothing_to_do_means_no_shift(calls):
    state = {}
    assert shift.step(state, away=True, idle_minutes=8.0, work=[], research=False, now=NOW) is None
    assert calls == [] and not state.get("shift")


def test_a_stuck_job_cannot_keep_the_pc_up_all_night(calls):
    state = {}
    shift.step(state, away=True, idle_minutes=8.0, work=["book"], research=False, now=NOW)
    out = shift.step(state, away=True, idle_minutes=90.0, work=["book"], research=False,
                     now=NOW + shift.SHIFT_CAP)
    assert out == "time's up: suspended" and state["shift"]["spent"]
    n = len(calls)
    shift.step(state, away=True, idle_minutes=91.0, work=["book"], research=False,
               now=NOW + shift.SHIFT_CAP + timedelta(minutes=1))
    assert len(calls) == n, "spent: waits for him to come back"


def test_research_keeps_its_own_inhibitor(calls):
    state = {}
    shift.step(state, away=True, idle_minutes=8.0, work=[], research=True, now=NOW)
    assert calls == [] and state["shift"]
    shift.step(state, away=True, idle_minutes=20.0, work=["dream"], research=False, now=NOW + timedelta(minutes=10))
    assert calls == ["hold"], "research ended (and released): the shift takes the hold for the dream"


def test_suspend_needs_hypridle_to_have_tried(tmp_path, monkeypatch):
    marker = tmp_path / "suspend-wanted"
    conf = tmp_path / "hypridle.conf"
    conf.write_text("listener {\n timeout = 900\n on-timeout = $suspend_cmd\n}\n")
    monkeypatch.setattr(power, "HYPRIDLE_CONF", conf)
    monkeypatch.setattr(power, "SUSPEND_WANTED", marker)
    ran = []

    def run(args, **kw):
        ran.append(args)
        return type("R", (), {"returncode": 0, "stdout": ""})()

    went_idle = time.time() - 3600
    assert not power.suspend_if_clear(60.0, run, went_idle=went_idle), "no try noted: a video kept hypridle quiet"
    marker.write_text(f"{int(went_idle) - 600}\n")
    assert not power.suspend_if_clear(60.0, run, went_idle=went_idle), "a try from before he went quiet"
    marker.write_text(f"{int(went_idle) + 900}\n")
    assert power.suspend_if_clear(60.0, run, went_idle=went_idle) and ran[-1][-2:] == ["systemctl", "suspend"]
    assert not power.suspend_if_clear(10.0, run, went_idle=went_idle), "not away long enough"


def test_screens_go_off_once_after_a_wake_he_didnt_touch(monkeypatch):
    ran = []
    monkeypatch.setattr(power, "_run", lambda args, run=None: ran.append(args) or True)
    monkeypatch.setattr(shift, "_suspended_s", None)
    monkeypatch.setattr(shift, "_resumed_at", None)
    t = 1_000_000.0
    assert not shift.note_resume(now_s=t, suspended=100.0)           # first look: just remembers
    assert shift.note_resume(now_s=t, suspended=100.0 + 6 * 3600)    # slept 6 h: woke up
    away_since_before = _sig(idle_minutes=6 * 60.0)
    assert not shift.screens_off_after_wake(away_since_before, now_s=t + 5), "too soon after waking"
    assert shift.screens_off_after_wake(away_since_before, now_s=t + 60) and ran[-1] == shift.DPMS_OFF
    assert not shift.screens_off_after_wake(away_since_before, now_s=t + 120), "only once per wake"

    shift.note_resume(now_s=t + 200, suspended=100.0 + 12 * 3600)
    assert not shift.screens_off_after_wake(_sig(idle="active", idle_minutes=0.0), now_s=t + 260)
    assert len(ran) == 1, "he woke it himself and is using it"

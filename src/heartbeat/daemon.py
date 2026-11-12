"""Phase 5 heartbeat: wake Mabel for research only when the PC is free, and get out of the way.

Every minute (every 5 s while she runs) it reads the signals and applies the
deterministic policy. A wake starts (or resumes) one curiosity session with a
sleep inhibitor held; the moment Anthon is back, a chat starts, a game needs
the GPU or the time cap is hit, the session yields (its finished steps stay
checkpointed) and is resumed at the next allowed wake, so nothing runs twice.

State: ``data/heartbeat/state.json``. Ledger (actor "heartbeat"): job.claimed,
job.yielded, job.completed, job.failed; policy and model decisions stay separate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from . import policy, power, signals, why

logger = logging.getLogger(__name__)

START_DELAY_S = 120
IDLE_TICK_S = 60
RUN_TICK_S = 5
_last: dict = {}
_dream_task: asyncio.Task | None = None
_revisit_task: asyncio.Task | None = None
_book_task: asyncio.Task | None = None
_letter_task: asyncio.Task | None = None
_news_task: asyncio.Task | None = None
_music_task: asyncio.Task | None = None
_paper_task: asyncio.Task | None = None
_print_task: asyncio.Task | None = None
_morning_task: asyncio.Task | None = None


def _state_path() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "heartbeat" / "state.json"


def load_state() -> dict:
    try:
        state = json.loads(_state_path().read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle, indent=1)
    os.replace(tmp, path)


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def _ledger(kind: str, session_id: str, decision: str, code: str) -> None:
    try:
        import re
        from src.agent_runtime.event_ledger import Event, Ledger
        from src.constants import DATA_DIR
        code = re.sub(r"[^A-Za-z0-9_.:-]+", "_", code)[:64] or "none"
        Ledger(Path(DATA_DIR) / "phase0" / "events.jsonl").append(Event.create(
            "heartbeat", kind, job_id=session_id, decision=decision, rationale_code=code))
    except Exception:
        logger.debug("[heartbeat] ledger event not written", exc_info=True)


def status() -> dict:
    state = load_state()
    return {"enabled": bool(_setting("autonomy_enabled", True)), "paused": bool(_setting("autonomy_paused", False)),
            "last": _last, "starts": state.get("starts", [])[-6:], "yielded": state.get("yielded"),
            "active_session": state.get("active_session"), "suspend_timeout_s": power.suspend_timeout_s(),
            "limits": {"min_idle_minutes": policy.MIN_IDLE_MINUTES, "max_per_day": policy.MAX_PER_DAY,
                       "min_gap_hours": policy.MIN_GAP.total_seconds() / 3600,
                       "session_cap_minutes": policy.SESSION_CAP.total_seconds() / 60}}


async def tick(*, now: datetime | None = None, read=signals.collect) -> dict:
    from src.curiosity import runner
    from src.persona import dreams
    now = now or datetime.now().astimezone()
    why.pulse(now=now)
    _maybe_print(now)
    if dreams.running():                          # her model is stepped aside for a dream; decide nothing
        _last.update(at=now.isoformat(), running=False, dreaming=True)
        return _last
    sig = await asyncio.to_thread(read)
    _maybe_morning(sig, now)
    state = load_state()
    active = state.get("active_session")

    if runner.is_autonomous_running():
        started = datetime.fromisoformat(active["started"]) if active else now
        reason = policy.should_yield(sig, started=started, now=now)
        if reason:
            runner.yield_now(reason)
        _last.update(at=now.isoformat(), signals=sig.as_dict(), running=True, yield_reason=reason)
        return _last

    if active:                                   # an autonomous session just ended (finished, yielded, failed)
        sid = active["session"]
        yielded = runner.status().get("yielded")
        if runner.session_finished(sid):
            _ledger("job.completed", sid, "completed", "article_written")
            why.done("research", "article written", now=now)
            state["yielded"] = None
        elif yielded and yielded.get("session") == sid:
            state["yielded"] = {"session": sid, "reason": yielded["reason"], "at": now.isoformat()}
            _ledger("job.yielded", sid, "yielded", yielded["reason"])
            why.done("research", "yielded: " + str(yielded["reason"]), now=now)
        else:
            _ledger("job.failed", sid, "failed", str(runner.status().get("error") or "unknown")[:60])
            why.done("research", "failed: " + str(runner.status().get("error") or "unknown")[:60], now=now)
            state["yielded"] = None                # don't retry a failing session forever
        state["active_session"] = None
        power.release_sleep()
        if sig.idle == "idle" and not sig.chat_active:
            power.suspend_if_overdue(sig.idle_minutes, active.get("idle_at_start"))
        save_state(state)

    decision = policy.decide(sig, state, now=now, enabled=bool(_setting("autonomy_enabled", True)),
                             paused=bool(_setting("autonomy_paused", False)),
                             research_running=bool(runner.status().get("running")))
    _last.update(at=now.isoformat(), signals=sig.as_dict(), decision=decision.as_dict(), running=False,
                 yield_reason=None, dreaming=False)
    _record_why(sig, now, decision)
    if not decision.allow:
        research_running = bool(runner.status().get("running"))
        if not _maybe_check_letters(sig, now, research_running=research_running) \
                and not _maybe_read_news(sig, now, research_running=research_running):
            _maybe_start_dream(sig, now, research_running=research_running)
            if not (_dream_task and not _dream_task.done()):
                _maybe_revisit(sig, now, research_running=research_running)
            if not (_dream_task and not _dream_task.done()) and not (_revisit_task and not _revisit_task.done()):
                _maybe_compose(sig, now, research_running=research_running)
            if not any(t and not t.done() for t in (_dream_task, _revisit_task, _music_task)):
                _maybe_write_paper(sig, now, research_running=research_running)
            if not any(t and not t.done() for t in (_dream_task, _revisit_task, _music_task, _paper_task)):
                _maybe_read_book(sig, now, research_running=research_running)
    if decision.allow:
        held = power.hold_sleep()
        try:
            started = runner.start(resume=decision.resume, autonomous=True)
        except runner.AlreadyRunning:
            power.release_sleep()
            return _last
        sid = started["session"]["id"]
        if not decision.resume:
            state["starts"] = (state.get("starts") or [])[-20:] + [now.isoformat()]
        state["active_session"] = {"session": sid, "started": now.isoformat(), "resumed": bool(decision.resume),
                                   "sleep_held": held, "idle_at_start": sig.idle_minutes}
        save_state(state)
        _ledger("job.claimed", sid, "resumed" if decision.resume else "started",
                f"idle_{int(sig.idle_minutes or 0)}m")
        logger.info("[heartbeat] woke Mabel: %s session %s (idle %.0f min)",
                    "resuming" if decision.resume else "new", sid, sig.idle_minutes or 0)
    return _last


def _record_why(sig, now: datetime, decision) -> None:
    """What holds each of her own activities back this tick (src/heartbeat/why.py). Side-effect free."""
    from src.curiosity import runner
    from src.persona import books, dreams, letters, morning, music, news, paper, revisit
    from src.constants import DATA_DIR
    why.record("research", [] if decision.allow else decision.reasons, now=now)
    research_running = bool(runner.status().get("running"))
    busy = {"letters": _letter_task, "news": _news_task, "dream": _dream_task, "revisit": _revisit_task, "book": _book_task,
            "music": _music_task, "paper": _paper_task, "morning": _morning_task}
    for job, module in (("letters", letters), ("news", news), ("dream", dreams), ("revisit", revisit),
                        ("music", music), ("paper", paper), ("morning", morning), ("book", books)):
        if busy[job] and not busy[job].done():
            continue                                   # running: its outcome is recorded when it ends
        try:
            reasons = module.gate(sig, data_dir=Path(DATA_DIR), now=now, research_running=research_running)
        except Exception as exc:
            reasons = [f"gate failed: {type(exc).__name__}"]
        if decision.allow:
            reasons = list(reasons) + ["research gets to go first"]
        why.record(job, reasons, now=now)


def _outcome(job: str):
    def callback(task):
        if task.cancelled():
            why.done(job, "cancelled")
        elif task.exception():
            why.done(job, f"failed: {task.exception()}")
        else:
            result = task.result() or {}
            why.done(job, str(result.get("status") or "done") if isinstance(result, dict) else "done")
    return callback


def _maybe_check_letters(sig, now: datetime, *, research_running: bool) -> bool:
    """Check the mail from Tova when she'd otherwise be idle (src/persona/letters.py). True while busy with it."""
    global _letter_task
    from src.persona import letters
    if _letter_task and not _letter_task.done():
        return True
    try:
        from src.constants import DATA_DIR
        if letters.gate(sig, data_dir=Path(DATA_DIR), now=now, research_running=research_running):
            return False
    except Exception:
        logger.debug("[heartbeat] letter gate failed", exc_info=True)
        return False
    _letter_task = asyncio.create_task(letters.check_mail(now=now))
    _letter_task.add_done_callback(_outcome("letters"))
    _letter_task.add_done_callback(lambda t: t.exception() and logger.warning(
        "[heartbeat] mail check failed: %s", t.exception()))
    return True


def _maybe_read_news(sig, now: datetime, *, research_running: bool) -> bool:
    """Her daily read of the news (src/persona/news.py). True while busy with it."""
    global _news_task
    from src.persona import news
    if _news_task and not _news_task.done():
        return True
    try:
        from src.constants import DATA_DIR
        if news.gate(sig, data_dir=Path(DATA_DIR), now=now, research_running=research_running):
            return False
    except Exception:
        logger.debug("[heartbeat] news gate failed", exc_info=True)
        return False
    _news_task = asyncio.create_task(news.read_news(now=now))
    _news_task.add_done_callback(_outcome("news"))
    _news_task.add_done_callback(lambda t: t.exception() and logger.warning(
        "[heartbeat] reading the news failed: %s", t.exception()))
    return True


def _maybe_start_dream(sig, now: datetime, *, research_running: bool) -> None:
    """At most one visual dream a day, only when she'd otherwise just be idle (src/persona/dreams.py)."""
    global _dream_task
    from src.persona import dreams
    if _dream_task and not _dream_task.done():
        return
    try:
        from src.constants import DATA_DIR
        if dreams.gate(sig, data_dir=Path(DATA_DIR), now=now, research_running=research_running):
            return
    except Exception:
        logger.debug("[heartbeat] dream gate failed", exc_info=True)
        return
    _dream_task = asyncio.create_task(dreams.maybe_dream(sig, now=now, research_running=research_running))
    _dream_task.add_done_callback(_outcome("dream"))
    _dream_task.add_done_callback(lambda t: t.exception() and logger.warning(
        "[heartbeat] dream failed: %s", t.exception()))


def _maybe_revisit(sig, now: datetime, *, research_running: bool) -> None:
    """Looking back at something of her own (src/persona/revisit.py), when she'd otherwise just be idle."""
    global _revisit_task
    from src.persona import revisit
    if _revisit_task and not _revisit_task.done():
        return
    try:
        from src.constants import DATA_DIR
        if revisit.gate(sig, data_dir=Path(DATA_DIR), now=now, research_running=research_running):
            return
    except Exception:
        logger.debug("[heartbeat] revisit gate failed", exc_info=True)
        return
    _revisit_task = asyncio.create_task(revisit.maybe_revisit(sig, now=now, research_running=research_running))
    _revisit_task.add_done_callback(_outcome("revisit"))
    _revisit_task.add_done_callback(lambda t: t.exception() and logger.warning(
        "[heartbeat] looking back failed: %s", t.exception()))


def _maybe_compose(sig, now: datetime, *, research_running: bool) -> None:
    """Offering her a piece of music to compose (src/persona/music.py), when she'd otherwise just be idle."""
    global _music_task
    from src.persona import music
    if _music_task and not _music_task.done():
        return
    try:
        from src.constants import DATA_DIR
        if music.gate(sig, data_dir=Path(DATA_DIR), now=now, research_running=research_running):
            return
    except Exception:
        logger.debug("[heartbeat] music gate failed", exc_info=True)
        return
    _music_task = asyncio.create_task(music.maybe_compose(sig, now=now, research_running=research_running))
    _music_task.add_done_callback(_outcome("music"))
    _music_task.add_done_callback(lambda t: t.exception() and logger.warning(
        "[heartbeat] composing failed: %s", t.exception()))


def _maybe_write_paper(sig, now: datetime, *, research_running: bool) -> None:
    """Offering her the printer on his desk (src/persona/paper.py), when she'd otherwise just be idle."""
    global _paper_task
    from src.persona import paper
    if _paper_task and not _paper_task.done():
        return
    try:
        from src.constants import DATA_DIR
        if paper.gate(sig, data_dir=Path(DATA_DIR), now=now, research_running=research_running):
            return
    except Exception:
        logger.debug("[heartbeat] paper gate failed", exc_info=True)
        return
    _paper_task = asyncio.create_task(paper.maybe_offer(sig, now=now, research_running=research_running))
    _paper_task.add_done_callback(_outcome("paper"))
    _paper_task.add_done_callback(lambda t: t.exception() and logger.warning(
        "[heartbeat] writing on paper failed: %s", t.exception()))


def _maybe_morning(sig, now: datetime) -> None:
    """His morning page (src/persona/morning.py): notice him waking up, then write and print it once a day."""
    global _morning_task
    from src.persona import morning
    if _morning_task and not _morning_task.done():
        return
    try:
        from src.constants import DATA_DIR
        morning.observe(sig, data_dir=Path(DATA_DIR), now=now)
        if morning.gate(sig, data_dir=Path(DATA_DIR), now=now):
            return
    except Exception:
        logger.debug("[heartbeat] morning gate failed", exc_info=True)
        return
    _morning_task = asyncio.create_task(morning.maybe_write(sig, now=now))
    _morning_task.add_done_callback(_outcome("morning"))
    _morning_task.add_done_callback(lambda t: t.exception() and logger.warning(
        "[heartbeat] the morning page failed: %s", t.exception()))


def _maybe_print(now: datetime) -> None:
    """Print whatever is queued for the printer (src/persona/printer.py). Needs no model, so it runs every tick."""
    global _print_task
    from src.persona import printer
    if _print_task and not _print_task.done():
        return
    try:
        from src.constants import DATA_DIR
        if printer.has_due(Path(DATA_DIR), now):
            work = printer.flush
        elif printer.keepalive_due(Path(DATA_DIR), now):
            work = printer.keepalive          # it switches itself off after a while; keep it awake
        else:
            return
    except Exception:
        logger.debug("[heartbeat] printer queue unreadable", exc_info=True)
        return
    _print_task = asyncio.create_task(asyncio.to_thread(work, now=now))
    _print_task.add_done_callback(lambda t: t.exception() and logger.warning(
        "[heartbeat] printing failed: %s", t.exception()))


def _maybe_read_book(sig, now: datetime, *, research_running: bool) -> None:
    """A session with the book she's reading (src/persona/books.py), when she'd otherwise just be idle."""
    global _book_task
    from src.persona import books
    if _book_task and not _book_task.done():
        return
    try:
        from src.constants import DATA_DIR
        if books.gate(sig, data_dir=Path(DATA_DIR), now=now, research_running=research_running):
            return
    except Exception:
        logger.debug("[heartbeat] book gate failed", exc_info=True)
        return
    _book_task = asyncio.create_task(books.maybe_read(sig, now=now, research_running=research_running))
    _book_task.add_done_callback(_outcome("book"))
    _book_task.add_done_callback(lambda t: t.exception() and logger.warning(
        "[heartbeat] reading failed: %s", t.exception()))


async def heartbeat() -> None:
    from src.curiosity import runner
    await asyncio.sleep(START_DELAY_S)
    try:
        await asyncio.to_thread(why.trim)
    except Exception:
        logger.debug("[heartbeat] why log not trimmed", exc_info=True)
    power.release_sleep()                         # a leftover inhibitor from a crash
    state = load_state()
    if state.get("active_session"):               # the server restarted mid-session: resume it later
        state["yielded"] = {"session": state["active_session"]["session"], "reason": "server restarted",
                            "at": datetime.now().astimezone().isoformat()}
        state["active_session"] = None
        save_state(state)
    while True:
        try:
            await tick()
        except Exception:
            logger.warning("[heartbeat] tick failed", exc_info=True)
        await asyncio.sleep(RUN_TICK_S if runner.is_autonomous_running() else IDLE_TICK_S)

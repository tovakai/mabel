"""Roadmap 2.0, Phase 2.0 (whole-brain trace): "why didn't she…?"

Every heartbeat tick already works out, for each thing she might do on her own
(research, the mail, the news, a dream, reaching out), the reasons she can't
right now. This keeps them, but only when they change, so a day reads as a
handful of periods: "news: waiting 09:02–14:20 because Anthon hasn't been away
5 min". A pulse every few minutes shows when the heartbeat itself wasn't
running (PC asleep, app down), which no gate can say.

Metadata only (reason strings the gates produce), local, in
``data/heartbeat/why.jsonl``; trimmed to the last ``KEEP_DAYS`` days.
CLI: ``python -m src.heartbeat.why [job] [hours]``.
"""
from __future__ import annotations

import json
import re
import logging
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

JOBS = ("research", "letters", "news", "dream", "revisit", "music", "book", "outreach")
PULSE_S = 300
ASLEEP_AFTER_S = 15 * 60          # no pulse for this long: the heartbeat wasn't running
KEEP_DAYS = 14
_LOCK = threading.Lock()
_last: dict[str, tuple] = {}
_last_pulse: list[datetime] = []


def _path(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from src.constants import DATA_DIR
        data_dir = Path(DATA_DIR)
    return Path(data_dir) / "heartbeat" / "why.jsonl"


def _append(entry: dict, data_dir: Path | None) -> None:
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def record(job: str, reasons: list[str] | None, *, now: datetime | None = None, data_dir: Path | None = None) -> None:
    """Keep this job's reasons if they changed since last time (empty = clear to go). Never raises."""
    now = now or datetime.now(timezone.utc)
    key = tuple(sorted(str(r) for r in reasons or ()))
    try:
        with _LOCK:
            if _last.get(job) == key:
                return
            _last[job] = key
            _append({"at": now.isoformat(timespec="seconds"), "job": job, "reasons": list(key)}, data_dir)
    except Exception:
        logger.debug("[why] couldn't keep %s's reasons", job, exc_info=True)


def done(job: str, outcome: str, *, now: datetime | None = None, data_dir: Path | None = None) -> None:
    """A job actually ran: what came of it ("read", "dreamt", "no_mail", "failed: …")."""
    now = now or datetime.now(timezone.utc)
    try:
        with _LOCK:
            _last.pop(job, None)          # whatever blocks it next is news again
            _append({"at": now.isoformat(timespec="seconds"), "job": job, "ran": str(outcome)[:200]}, data_dir)
    except Exception:
        logger.debug("[why] couldn't keep %s's outcome", job, exc_info=True)


def pulse(*, now: datetime | None = None, data_dir: Path | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if _last_pulse and (now - _last_pulse[0]).total_seconds() < PULSE_S:
        return
    _last_pulse[:] = [now]
    try:
        with _LOCK:
            _append({"at": now.isoformat(timespec="seconds"), "job": "pulse"}, data_dir)
    except Exception:
        logger.debug("[why] pulse not kept", exc_info=True)


def load(*, since: datetime, data_dir: Path | None = None) -> list[dict]:
    out = []
    try:
        with _path(data_dir).open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                    if datetime.fromisoformat(entry["at"]) >= since:
                        out.append(entry)
                except (ValueError, KeyError):
                    continue
    except OSError:
        pass
    return out


def trim(*, now: datetime | None = None, data_dir: Path | None = None) -> None:
    """Drop entries older than KEEP_DAYS (called at heartbeat start)."""
    now = now or datetime.now(timezone.utc)
    keep = load(since=now - timedelta(days=KEEP_DAYS), data_dir=data_dir)
    path = _path(data_dir)
    if not path.exists():
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep), encoding="utf-8")
    tmp.replace(path)


def explain(job: str | None = None, *, hours: float = 24, now: datetime | None = None,
            data_dir: Path | None = None) -> dict:
    """Periods per job over the last ``hours``: what held it back, when it ran, and when nothing was running."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    entries = load(since=since - timedelta(days=1), data_dir=data_dir)   # a day back for the state at ``since``
    pulses = sorted(datetime.fromisoformat(e["at"]) for e in entries if e["job"] == "pulse")
    asleep = []
    for a, b in zip(pulses, pulses[1:] + [now]):
        if (b - a).total_seconds() > ASLEEP_AFTER_S and b > since:
            asleep.append({"from": max(a, since).isoformat(timespec="seconds"), "to": b.isoformat(timespec="seconds")})
    jobs = {}
    for name in ([job] if job else JOBS):
        periods, current = [], None
        for e in (e for e in entries if e["job"] == name):
            at = datetime.fromisoformat(e["at"])
            if "ran" in e:
                if at >= since:
                    periods.append({"at": e["at"], "ran": e["ran"]})
                continue
            if current:
                current["to"] = e["at"]
                if datetime.fromisoformat(current["to"]) > since:
                    current["from"] = max(datetime.fromisoformat(current["from"]), since).isoformat(timespec="seconds")
                    periods.append(current)
            current = {"from": e["at"], "to": None, "reasons": e["reasons"]} if e["reasons"] else \
                {"from": e["at"], "to": None, "reasons": [], "clear": True}
        if current:
            current["from"] = max(datetime.fromisoformat(current["from"]), since).isoformat(timespec="seconds")
            periods.append(current)
        jobs[name] = _merge_counters(sorted(periods, key=lambda p: datetime.fromisoformat(p.get("from") or p.get("at"))))
    return {"since": since.isoformat(timespec="seconds"), "asleep": asleep, "jobs": jobs}


def _shape(reasons: list[str]) -> tuple:
    return tuple(re.sub(r"\d+", "#", r) for r in reasons)


def _merge_counters(periods: list[dict]) -> list[dict]:
    """One span for "unanswered (38 h)", "(39 h)", …: same reasons apart from their numbers.
    The merged span keeps the latest wording, so the counter reads as of its end."""
    out: list[dict] = []
    for p in periods:
        prev = out[-1] if out else None
        if (prev and "ran" not in p and "ran" not in prev and prev.get("to") == p["from"]
                and _shape(prev["reasons"]) == _shape(p["reasons"])):
            prev.update(to=p["to"], reasons=p["reasons"])
        else:
            out.append(dict(p))
    return out


def _hm(value: str | None) -> str:
    return datetime.fromisoformat(value).astimezone().strftime("%a %H:%M") if value else "now"


def render(report: dict) -> str:
    lines = []
    for span in report["asleep"]:
        lines.append(f"heartbeat not running {_hm(span['from'])} – {_hm(span['to'])} (PC asleep or app down)")
    for name, periods in report["jobs"].items():
        lines.append(f"\n{name}:")
        for p in periods:
            if "ran" in p:
                lines.append(f"  {_hm(p['at'])}  ran → {p['ran']}")
            elif p.get("clear"):
                lines.append(f"  {_hm(p['from'])} – {_hm(p['to'])}  clear to go")
            else:
                lines.append(f"  {_hm(p['from'])} – {_hm(p['to'])}  held back: {'; '.join(p['reasons'])}")
        if not periods:
            lines.append("  (nothing recorded)")
    return "\n".join(lines).strip()


if __name__ == "__main__":
    args = sys.argv[1:]
    job = args[0] if args and args[0] in JOBS else None
    hours = float(args[-1]) if args and args[-1].replace(".", "", 1).isdigit() else 24
    print(render(explain(job, hours=hours)))

"""The life log store: activity spans from Anthon's devices, phone and home.

A span is one continuous thing (a focused window, a game, a song, a visit to
a page, a place) with a start and an end. Collectors send spans with their own
id and keep extending the end while it continues, so the server upserts.

Stored only in ``data/lifelog/lifelog.sqlite`` on this PC; raw spans are
pruned after the retention period (daily summaries live in Mabel's vault).
Browsing is kept as page title + site, never full URLs. Messages (WhatsApp, Messenger,
Discord) are the phone's notification previews: app, chat/sender, preview text, time.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

KINDS = ("focus", "browse", "game", "media", "idle", "place", "steps", "phone_app", "activity", "sleep", "message")
MAX_BATCH = 500
TEXT_LIMIT = 300


class LifelogError(ValueError):
    pass


def site_of(value: str) -> str:
    value = str(value or "").strip()
    if "://" in value:
        value = urlparse(value).netloc
    return value.lower().removeprefix("www.").split(":")[0][:120]


def _clean(text, limit: int = TEXT_LIMIT) -> str:
    return " ".join(str(text or "").split())[:limit]


def _excluded(span: dict, sites: list[str], apps: list[str]) -> bool:
    site = span.get("site") or ""
    if site and any(site == s or site.endswith("." + s) for s in sites):
        return True
    app = (span.get("app") or "").lower()
    return bool(app) and any(a.lower() == app for a in apps)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS spans(
                    device TEXT NOT NULL, span_id TEXT NOT NULL, kind TEXT NOT NULL,
                    start TEXT NOT NULL, end TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                    app TEXT NOT NULL DEFAULT '', site TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
                    received_at TEXT NOT NULL, PRIMARY KEY(device, span_id));
                CREATE INDEX IF NOT EXISTS spans_time ON spans(start);
                CREATE TABLE IF NOT EXISTS devices(
                    device TEXT PRIMARY KEY, last_seen TEXT NOT NULL, token_id TEXT, source TEXT);
            """)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def ingest(self, device: str, spans: list[dict], *, source: str = "collector", token_id: str | None = None,
               exclude_sites=(), exclude_apps=(), now: datetime | None = None) -> dict:
        device = re.sub(r"[^a-z0-9_-]+", "-", str(device or "").lower()).strip("-")[:40]
        if not device:
            raise LifelogError("device name required")
        if not isinstance(spans, list) or len(spans) > MAX_BATCH:
            raise LifelogError(f"spans must be a list of at most {MAX_BATCH}")
        now = now or datetime.now(timezone.utc)
        stored = skipped = 0
        rows = []
        for raw in spans:
            if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
                skipped += 1
                continue
            try:
                start = datetime.fromisoformat(str(raw["start"]).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(raw.get("end") or raw["start"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                skipped += 1
                continue
            if start.tzinfo is None or end < start or end - start > timedelta(days=2) or start > now + timedelta(minutes=10):
                skipped += 1
                continue
            span = {"kind": raw["kind"], "title": _clean(raw.get("title")), "app": _clean(raw.get("app"), 80),
                    "site": site_of(raw.get("site") or raw.get("url") or "")}
            if _excluded(span, list(exclude_sites), list(exclude_apps)):
                skipped += 1
                continue
            detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else {}
            detail = {str(k)[:40]: v for k, v in list(detail.items())[:12]
                      if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 200}
            span_id = _clean(raw.get("id") or f"{raw['kind']}:{start.isoformat()}", 120)
            rows.append((device, span_id, span["kind"], start.astimezone(timezone.utc).isoformat(),
                         end.astimezone(timezone.utc).isoformat(), span["title"], span["app"], span["site"],
                         json.dumps(detail, ensure_ascii=False), now.isoformat()))
            stored += 1
        with self._db() as db:
            db.executemany("""INSERT INTO spans(device, span_id, kind, start, end, title, app, site, detail, received_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?)
                              ON CONFLICT(device, span_id) DO UPDATE SET end=excluded.end, title=excluded.title,
                              app=excluded.app, site=excluded.site, detail=excluded.detail,
                              received_at=excluded.received_at""", rows)
            db.execute("""INSERT INTO devices(device, last_seen, token_id, source) VALUES (?,?,?,?)
                          ON CONFLICT(device) DO UPDATE SET last_seen=excluded.last_seen,
                          token_id=COALESCE(excluded.token_id, devices.token_id), source=excluded.source""",
                       (device, now.isoformat(), token_id, source))
        return {"stored": stored, "skipped": skipped}

    def spans(self, start: datetime, end: datetime, kinds=None) -> list[dict]:
        query = "SELECT * FROM spans WHERE end >= ? AND start < ?"
        args: list = [start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()]
        if kinds:
            query += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        with self._db() as db:
            rows = db.execute(query + " ORDER BY start", args).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["detail"] = json.loads(item["detail"] or "{}")
            out.append(item)
        return out

    def devices(self) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM devices ORDER BY last_seen DESC")]

    def prune(self, days: int, now: datetime | None = None) -> int:
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=max(1, int(days)))).isoformat()
        with self._db() as db:
            return db.execute("DELETE FROM spans WHERE end < ?", (cutoff,)).rowcount


def default_store() -> Store:
    from src.constants import DATA_DIR
    return Store(Path(DATA_DIR) / "lifelog" / "lifelog.sqlite")

"""Phase 4: Dottie's typed inbox.

Not a stream of guesses: every item has a kind, evidence pointers, a
verification status, confidence, a deduplication key, priority, expiry and the
loop (and job) that produced it. Deciding on an item never deletes it, so a
rejection keeps its origin. A rejected suggestion cools off for a week
(REJECT_COOLDOWN) before it may come back; kept/done items stay settled.

Stored locally in ``data/subconscious/inbox.sqlite`` (never in a synced folder).
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

KINDS = ("candidate", "research_brief", "contradiction", "stale_framing", "pattern",
         "unfinished_question", "module_proposal", "duplicate")
LOOPS = ("sentinel", "archivist", "scout", "dreamer")
VERIFICATION = ("unverified", "source_checked", "verified")
STATUSES = ("new", "accepted", "rejected", "expired", "done")
DECIDERS = ("user", "mabel", "system")
MAX_LIST = 200
REJECT_COOLDOWN = timedelta(days=7)
_STOPWORDS = {"with", "that", "this", "from", "about", "their", "they", "your", "what", "when", "into", "mabel",
              "mabels", "anthon", "anthons", "keeps", "keep"}


def _theme_words(text: str) -> set[str]:
    import re
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if len(w) >= 4 and w not in _STOPWORDS}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InboxError(ValueError):
    pass


class Inbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS items(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, loop TEXT NOT NULL, title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '[]',
                    verification TEXT NOT NULL, confidence REAL NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
                    priority INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, job_id TEXT, payload TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'new', decided_by TEXT, decided_at TEXT, decision_reason TEXT);
                CREATE INDEX IF NOT EXISTS items_status ON items(status, priority);
            """)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _row(row) -> dict:
        item = dict(row)
        item["evidence"] = json.loads(item["evidence"] or "[]")
        item["payload"] = json.loads(item["payload"] or "{}")
        return item

    def add(self, *, kind: str, loop: str, title: str, dedupe_key: str, evidence: list[dict],
            summary: str = "", verification: str = "unverified", confidence: float = 0.5, priority: int = 50,
            days: float = 14, job_id: str | None = None, payload: dict | None = None,
            now: datetime | None = None) -> tuple[dict | None, str]:
        """Insert or refresh an item. Returns (item, "created" | "refreshed" | "kept_decision")."""
        if kind not in KINDS or loop not in LOOPS or verification not in VERIFICATION:
            raise InboxError("unknown kind, loop or verification status")
        if not evidence or not all(isinstance(e, dict) and e.get("ref") for e in evidence):
            raise InboxError("every item needs evidence pointers")
        if not 0 <= confidence <= 1 or not 0 <= priority <= 100:
            raise InboxError("confidence must be 0..1 and priority 0..100")
        now = now or _now()
        values = dict(kind=kind, loop=loop, title=str(title)[:200], summary=str(summary)[:2000],
                      evidence=json.dumps(evidence[:20], ensure_ascii=False), verification=verification,
                      confidence=round(float(confidence), 3), priority=int(priority),
                      updated_at=now.isoformat(), expires_at=(now + timedelta(days=days)).isoformat(),
                      job_id=job_id, payload=json.dumps(payload or {}, ensure_ascii=False))
        with self._db() as db:
            existing = db.execute("SELECT * FROM items WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
            if existing and existing["status"] in ("accepted", "done"):
                return self._row(existing), "kept_decision"     # settled for good
            if existing and existing["status"] == "rejected":
                decided = existing["decided_at"] or ""
                if decided > (now - REJECT_COOLDOWN).isoformat():
                    return self._row(existing), "kept_decision"  # still cooling off
                # The week is up: it may come back, remembering the earlier rejection.
                previous = json.loads(values["payload"])
                previous["previous_rejection"] = {"by": existing["decided_by"], "at": decided,
                                                  "reason": existing["decision_reason"] or ""}
                values["payload"] = json.dumps(previous, ensure_ascii=False)
            if existing:
                db.execute(f"UPDATE items SET {', '.join(f'{k} = ?' for k in values)}, status = 'new', "
                           "decided_by = NULL, decided_at = NULL, decision_reason = NULL WHERE id = ?",
                           (*values.values(), existing["id"]))
                refreshed = existing["id"]
            else:
                refreshed = None
                item_id = f"in-{secrets.token_hex(6)}"
                db.execute(f"INSERT INTO items(id, dedupe_key, created_at, {', '.join(values)}) "
                           f"VALUES (?, ?, ?, {', '.join('?' * len(values))})",
                           (item_id, dedupe_key, now.isoformat(), *values.values()))
        # Read back only after the transaction above has committed.
        if refreshed:
            return self.get(refreshed), "refreshed"
        self._ledger("inbox.item_created", item_id, loop, kind)
        return self.get(item_id), "created"

    def get(self, item_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._row(row) if row else None

    def expire(self, now: datetime | None = None) -> int:
        with self._db() as db:
            return db.execute("UPDATE items SET status = 'expired' WHERE status = 'new' AND expires_at <= ?",
                              ((now or _now()).isoformat(),)).rowcount

    def list(self, *, status: str | None = "new", kind: str | None = None, limit: int = 50) -> list[dict]:
        self.expire()
        query, args = "SELECT * FROM items WHERE 1=1", []
        if status:
            query += " AND status = ?"
            args.append(status)
        if kind:
            query += " AND kind = ?"
            args.append(kind)
        query += " ORDER BY priority DESC, created_at DESC LIMIT ?"
        args.append(min(int(limit), MAX_LIST))
        with self._db() as db:
            return [self._row(r) for r in db.execute(query, args)]

    def recently_rejected_similar(self, kind: str, title: str, *, now: datetime | None = None) -> dict | None:
        """A rejected item of ``kind`` from the last week whose title clearly overlaps ``title``
        (half of the smaller set of distinctive words), so a reworded suggestion cools off too."""
        words = _theme_words(title)
        if not words:
            return None
        cutoff = ((now or _now()) - REJECT_COOLDOWN).isoformat()
        with self._db() as db:
            rows = db.execute("SELECT * FROM items WHERE kind = ? AND status = 'rejected' AND decided_at > ?",
                              (kind, cutoff)).fetchall()
        for row in rows:
            other = _theme_words(row["title"])
            shared = words & other
            if other and shared and len(shared) / min(len(words), len(other)) >= 0.5:
                return self._row(row)
        return None

    def find(self, dedupe_key: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM items WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
        return self._row(row) if row else None

    def decide(self, item_id: str, status: str, *, by: str, reason: str = "") -> dict:
        """Accept, reject or mark done. The item and its origin stay in the inbox."""
        if status not in ("accepted", "rejected", "done") or by not in DECIDERS:
            raise InboxError("unknown decision")
        with self._db() as db:
            changed = db.execute("UPDATE items SET status = ?, decided_by = ?, decided_at = ?, decision_reason = ? "
                                 "WHERE id = ?", (status, by, _now().isoformat(), str(reason)[:300], item_id)).rowcount
        if not changed:
            raise InboxError("unknown item")
        item = self.get(item_id)
        if status in ("accepted", "rejected"):
            self._ledger(f"inbox.item_{status}", item_id, item["loop"], item["kind"], actor=by)
        return item

    def counts(self) -> dict:
        self.expire()
        with self._db() as db:
            return {r[0]: r[1] for r in db.execute("SELECT status, count(*) FROM items GROUP BY status")}

    @staticmethod
    def _ledger(kind: str, item_id: str, loop: str, item_kind: str, actor: str = "dottie") -> None:
        try:
            from src.agent_runtime.event_ledger import Event, Ledger
            from src.constants import DATA_DIR
            Ledger(Path(DATA_DIR) / "phase0" / "events.jsonl").append(Event.create(
                {"system": "runtime", "mabel": "mabel", "user": "user"}.get(actor, "dottie"), kind,
                references=(f"inbox:{item_id}",), modules=(f"loop.{loop}", f"kind.{item_kind}")))
        except Exception:
            pass


def default_inbox() -> Inbox:
    from src.constants import DATA_DIR
    return Inbox(Path(DATA_DIR) / "subconscious" / "inbox.sqlite")

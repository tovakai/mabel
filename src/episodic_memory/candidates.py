"""Phase 00.5: opt-in, staged episodic candidates (NOT committed memories).

This module has no hooks into chat, memory.json, prompts, agent autonomy or
Obsidian writes. All stored candidates are non-recallable pending suggestions.
The outbox is a *delivery buffer* for the existing Phase 00 typed event ledger,
not a replacement event ledger. Promotion is implemented separately and requires explicit source verification
and operator approval; the queue never promotes automatically.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4

from src.owner_identity import owner_key, is_request_sentinel_owner

KINDS = frozenset({"experience", "decision", "mistake", "discovery", "open_question"})
SOURCE_KINDS = frozenset({"conversation", "tool_observation", "ledger_event", "user_seeded"})
STATES = frozenset({"pending", "deferred", "promoting", "promoted", "rejected", "quarantined", "merged"})
DECISIONS = frozenset({"deferred", "rejected", "quarantined"})
_VERSION = 4


class CandidateAdmissionError(ValueError):
    """A proposed event must not be persisted under ambiguous provenance."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CandidateAdmissionError(f"{label} must be nonempty and at most {maximum} characters")
    if "\x00" in value or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise CandidateAdmissionError(f"{label} contains invalid control characters")
    return value.strip()


@dataclass(frozen=True)
class CapturePolicy:
    """Caller-supplied capture consent; default is deliberately OFF."""

    episodic_enabled: bool = False
    memory_enabled: bool = False
    incognito: bool = False

    def require_capture(self) -> None:
        if not self.episodic_enabled or not self.memory_enabled or self.incognito:
            raise PermissionError("Episodic candidate capture is disabled for this turn")


@dataclass(frozen=True)
class CandidateDraft:
    """Source-bound suggestion, never a verified autobiographical fact."""

    kind: str
    actor: str
    source_kind: str
    source_ref: str
    source_event_id: str
    summary: str
    origin: str
    interpretation: str = ""

    def validated(self) -> "CandidateDraft":
        if self.kind not in KINDS:
            raise CandidateAdmissionError("Unsupported episodic candidate kind")
        if self.source_kind not in SOURCE_KINDS:
            raise CandidateAdmissionError("Unsupported source kind")
        for label, max_length in (
            ("actor", 128), ("source_ref", 512), ("source_event_id", 256),
            ("summary", 1200), ("origin", 128)
        ):
            _bounded(getattr(self, label), label, max_length)
        if not isinstance(self.interpretation, str) or len(self.interpretation) > 1200:
            raise CandidateAdmissionError("Interpretation exceeds 1200 characters")
        return self


class CandidateQueue:
    """Per-owner SQLite queue outside synced Markdown.

    Proposal and negative/deferred decisions live here. The separately gated
    promotion module owns verified Markdown writes. No automatic recall exists.
    All state changes enqueue typed event envelopes transactionally; the
    explicit Phase 00 adapter publishes and acks using stable event IDs.
    """

    def __init__(self, storage_dir: str | Path, *, owner: str,
                 protected_roots: tuple[str | Path, ...] = ()) -> None:
        normalized = owner_key(owner)
        if not normalized or is_request_sentinel_owner(normalized):
            raise CandidateAdmissionError("Explicit real account owner required")
        directory = Path(storage_dir).expanduser()
        if not directory.is_absolute():
            raise CandidateAdmissionError("Storage directory must be an absolute local path")
        if directory.is_symlink():
            raise CandidateAdmissionError("Storage directory cannot be a symlink")
        directory = directory.resolve()
        for protected in protected_roots:
            protected_path = Path(protected).expanduser().resolve()
            if directory == protected_path or directory.is_relative_to(protected_path):
                raise CandidateAdmissionError("Candidate SQLite must remain outside protected vault roots")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise CandidateAdmissionError("Storage directory changed to a symlink")
        self.owner = normalized
        self.path = directory / (sha256(normalized.encode()).hexdigest()[:24] + ".sqlite")
        if self.path.is_symlink():
            raise CandidateAdmissionError("Candidate DB cannot be a symlink")
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self.path.is_symlink():
            raise CandidateAdmissionError("Candidate DB cannot be a symlink")
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, _VERSION):
                raise CandidateAdmissionError("Unknown episodic candidate DB schema; do not migrate automatically")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS candidates(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, dedup_key TEXT NOT NULL,
                    kind TEXT NOT NULL, actor TEXT NOT NULL,
                    source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
                    source_event_id TEXT NOT NULL, origin TEXT NOT NULL,
                    summary TEXT NOT NULL, interpretation TEXT NOT NULL,
                    evidence_status TEXT NOT NULL DEFAULT 'unverified',
                    state TEXT NOT NULL DEFAULT 'pending',
                    decision_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner, dedup_key)
                );
                CREATE INDEX IF NOT EXISTS episode_candidate_state ON candidates(owner,state,created_at);
                CREATE TABLE IF NOT EXISTS event_outbox(
                    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL, delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS event_outbox_pending ON event_outbox(delivered_at,created_at);
            """)
            # v2 is additive: preserve every existing v1 candidate and event
            # while adding separately persisted, non-authoritative reviews.
            columns = {row[1] for row in db.execute("PRAGMA table_info(candidates)")}
            if "merged_into" not in columns:
                db.execute("ALTER TABLE candidates ADD COLUMN merged_into TEXT")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS candidate_reviews(
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    candidate_id TEXT NOT NULL REFERENCES candidates(id),
                    reviewer TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    confidence INTEGER NOT NULL,
                    rationale_code TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    target_candidate_id TEXT,
                    created_at TEXT NOT NULL,
                    applied_at TEXT,
                    applied_outcome TEXT,
                    UNIQUE(owner,candidate_id,run_id)
                );
                CREATE INDEX IF NOT EXISTS candidate_reviews_lookup
                    ON candidate_reviews(owner,candidate_id,created_at);
            """)
            # Privacy tombstones prevent a deleted conversation being re-captured,
            # including after the canonical session row has disappeared.
            db.executescript("""
                CREATE TABLE IF NOT EXISTS deleted_sessions(
                    owner TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    deleted_at TEXT NOT NULL,
                    PRIMARY KEY(owner,session_id)
                );
                CREATE TABLE IF NOT EXISTS episode_deletion_queue(
                    owner TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    expected_digest TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    PRIMARY KEY(owner,candidate_id)
                );
            """)
            # v4: source-specific revocations without blocking subsequent
            # valid messages in a still-active session.
            db.executescript("""
                CREATE TABLE IF NOT EXISTS invalidated_messages(
                    owner TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    invalidated_at TEXT NOT NULL,
                    PRIMARY KEY(owner,session_id,message_id)
                );
            """)
            db.execute(f"PRAGMA user_version={_VERSION}")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _envelope(candidate_id: str, kind: str, payload: dict) -> tuple:
        event_id = uuid4().hex
        return (event_id, candidate_id, kind, json.dumps(
            {"event_id": event_id, "candidate_id": candidate_id, **payload},
            sort_keys=True, ensure_ascii=False), _now())

    def propose(self, draft: CandidateDraft, *, policy: CapturePolicy) -> dict:
        if not isinstance(policy, CapturePolicy):
            raise CandidateAdmissionError("Explicit CapturePolicy is required")
        policy.require_capture()
        if not isinstance(draft, CandidateDraft):
            raise CandidateAdmissionError("Expected a typed CandidateDraft")
        draft.validated()
        key = sha256(json.dumps(
            [self.owner, draft.source_kind, draft.source_ref, draft.source_event_id, draft.kind],
            separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if draft.source_kind == "conversation":
                # Source refs are exact opaque chat receipts, not free text.
                match = re.fullmatch(
                    r"chat:([A-Za-z0-9_.:-]{1,128}):message:[A-Za-z0-9_.:-]{1,128}",
                    draft.source_ref,
                )
                if match and db.execute(
                    "SELECT 1 FROM deleted_sessions WHERE owner=? AND session_id=?",
                    (self.owner, match.group(1)),
                ).fetchone():
                    raise CandidateAdmissionError("Source session was deleted; recapture forbidden")
                if match and db.execute(
                    "SELECT 1 FROM invalidated_messages WHERE "
                    "owner=? AND session_id=? AND message_id=?",
                    (self.owner,match.group(1),draft.source_ref.rsplit(":message:",1)[1]),
                ).fetchone():
                    raise CandidateAdmissionError("Source message was removed or edited; recapture forbidden")
            existing = db.execute(
                "SELECT * FROM candidates WHERE owner=? AND dedup_key=?",
                (self.owner, key)).fetchone()
            if existing is not None:
                return {**dict(existing), "created": False}
            candidate_id = uuid4().hex
            timestamp = _now()
            db.execute("""INSERT INTO candidates
                (id,owner,dedup_key,kind,actor,source_kind,source_ref,source_event_id,
                 origin,summary,interpretation,evidence_status,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'unverified','pending',?,?)""",
                (candidate_id,self.owner,key,draft.kind,draft.actor,draft.source_kind,
                 draft.source_ref,draft.source_event_id,draft.origin,draft.summary,
                 draft.interpretation,timestamp,timestamp))
            # Only metadata is placed in the ledger outbox. No raw transcript,
            # candidate prose, or model-generated interpretation.
            db.execute("""INSERT INTO event_outbox(id,candidate_id,kind,payload,created_at)
                          VALUES(?,?,?,?,?)""", self._envelope(
                candidate_id, "memory.candidate_created",
                {"actor": draft.actor, "source_kind": draft.source_kind,
                 "source_ref": draft.source_ref, "source_event_id": draft.source_event_id,
                 "decision": "pending", "evidence_status": "unverified"}))
            row = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            return {**dict(row), "created": True}

    def get(self, candidate_id: str) -> dict | None:
        with self._connect() as db:
            result = db.execute(
                "SELECT * FROM candidates WHERE owner=? AND id=?",
                (self.owner,candidate_id)).fetchone()
            return dict(result) if result is not None else None

    def list(self, *, state: str = "pending", limit: int = 50) -> list[dict]:
        if state not in STATES or type(limit) is not int or not 1 <= limit <= 100:
            raise CandidateAdmissionError("Invalid candidate list filter")
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM candidates WHERE owner=? AND state=? "
                "ORDER BY created_at,id LIMIT ?", (self.owner,state,limit))]

    def decide(self, candidate_id: str, *, outcome: str, reason: str) -> dict:
        if outcome not in DECISIONS:
            raise CandidateAdmissionError(
                "Only reject, defer or quarantine are allowed; promotion requires a separate verified vault writer"
            )
        reason = _bounded(reason, "decision reason code", 64)
        if re.fullmatch(r"[a-z][a-z0-9_]*", reason) is None:
            raise CandidateAdmissionError("Decision rationale must be a short machine-readable reason code")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM candidates WHERE owner=? AND id=?",
                (self.owner,candidate_id)).fetchone()
            if row is None:
                raise CandidateAdmissionError("Candidate not found for owner")
            if row["state"] == outcome:
                return {**dict(row), "changed": False}
            if row["state"] not in {"pending", "deferred"}:
                raise CandidateAdmissionError("Candidate already settled")
            timestamp = _now()
            db.execute(
                "UPDATE candidates SET state=?,decision_reason=?,updated_at=? WHERE id=?",
                (outcome,reason,timestamp,candidate_id))
            event_kind = {
                "rejected": "memory.candidate_rejected",
                "quarantined": "memory.candidate_quarantined",
                "deferred": "memory.candidate_deferred",
            }[outcome]
            db.execute("""INSERT INTO event_outbox(id,candidate_id,kind,payload,created_at)
                          VALUES(?,?,?,?,?)""", self._envelope(
                candidate_id,event_kind,
                {"decision":outcome,"source_kind":row["source_kind"],
                 "source_ref":row["source_ref"],"reason_code":reason}))
            updated = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            return {**dict(updated), "changed": True}

    def pending_events(self, *, limit: int = 50) -> list[dict]:
        """Read-only delivery buffer; NOT a memory source or canonical event ledger."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CandidateAdmissionError("Invalid outbox bound")
        with self._connect() as db:
            return [
                {"id": r["id"], "candidate_id": r["candidate_id"], "kind": r["kind"],
                 "payload": json.loads(r["payload"]), "created_at": r["created_at"]}
                for r in db.execute(
                    "SELECT * FROM event_outbox WHERE delivered_at IS NULL "
                    "ORDER BY created_at,id LIMIT ?", (limit,))
            ]

    def mark_event_delivered(self, event_id: str) -> bool:
        """Call only AFTER durable, idempotent publication to the Phase 00 ledger."""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE event_outbox SET delivered_at=? "
                "WHERE id=? AND delivered_at IS NULL", (_now(),event_id))
            return cursor.rowcount == 1

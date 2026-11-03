"""Deletion propagation for source-linked episodic candidates.

Canonical session deletion and this SQLite queue cannot share a transaction.
After a committed session deletion, scrub candidate prose, reviewer rows and
undelivered event envelopes, then keep an opaque tombstone. Previously written
Markdown is tracked separately for an explicit, hash-verified cleanup in the
configured Mabel vault. Do not guess a vault path or delete unknown files.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re

from src.owner_identity import owner_key, is_request_sentinel_owner

from .candidates import CandidateAdmissionError, CandidateQueue, _now

_SESSION = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _queue_if_present(storage_dir: str | Path, owner: str) -> CandidateQueue | None:
    key = owner_key(owner)
    if not key or is_request_sentinel_owner(key):
        return None
    directory = Path(storage_dir).expanduser()
    if not directory.is_absolute() or not directory.is_dir():
        return None
    path = directory / (sha256(key.encode("utf-8")).hexdigest()[:24] + ".sqlite")
    if not path.is_file() or path.is_symlink():
        return None
    return CandidateQueue(directory, owner=key)


def purge_deleted_session(
    storage_dir: str | Path, *, owner: str, session_id: str,
) -> dict:
    """Scrub candidate/review content after a confirmed canonical deletion.

    This function is idempotent. It never reads, creates, or deletes Markdown.
    The small opaque episode deletion queue identifies notes requiring a
    separate operator-controlled vault cleanup. No network/model calls.
    """
    if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
        raise CandidateAdmissionError("Deleted session ID must be a bounded token")
    queue = _queue_if_present(storage_dir, owner)
    if queue is None:
        return {"scrubbed": 0, "pending_episode_files": 0, "queue_present": False}

    prefix = f"chat:{session_id}:message:"
    with queue._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO deleted_sessions(owner,session_id,deleted_at) VALUES(?,?,?)",
            (queue.owner, session_id, _now()),
        )
        rows = db.execute(
            "SELECT id,state,decision_reason FROM candidates "
            "WHERE owner=? AND source_kind='conversation' "
            "AND substr(source_ref,1,?)=?",
            (queue.owner, len(prefix), prefix),
        ).fetchall()
        for row in rows:
            if row["state"] in ("promoted", "promoting"):
                # A stable digest, not the transcript or full file path.
                digest = row["decision_reason"]
                if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                    digest = ""
                db.execute(
                    "INSERT OR IGNORE INTO episode_deletion_queue "
                    "(owner,session_id,candidate_id,expected_digest,queued_at) "
                    "VALUES(?,?,?,?,?)",
                    (queue.owner, session_id, row["id"], digest, _now()),
                )
            # Do not publish stale creation/decision events after user deletion.
            db.execute("DELETE FROM event_outbox WHERE candidate_id=?", (row["id"],))
            db.execute(
                "DELETE FROM candidate_reviews WHERE owner=? AND "
                "(candidate_id=? OR target_candidate_id=?)",
                (queue.owner, row["id"], row["id"]),
            )
            # A separately retained source may have linked to this candidate.
            db.execute(
                "UPDATE candidates SET merged_into=NULL, state='deferred', "
                "decision_reason='deleted_merge_target', updated_at=? "
                "WHERE owner=? AND merged_into=?",
                (_now(), queue.owner, row["id"]),
            )
            db.execute(
                "DELETE FROM candidates WHERE owner=? AND id=?",
                (queue.owner, row["id"]),
            )
        pending = db.execute(
            "SELECT count(*) FROM episode_deletion_queue "
            "WHERE owner=? AND session_id=?",
            (queue.owner, session_id),
        ).fetchone()[0]
        return {
            "scrubbed": len(rows), "pending_episode_files": pending,
            "queue_present": True,
        }


def complete_episode_deletions(
    queue: CandidateQueue, *,
    vault_root: str | Path,
    protected_roots: tuple[str | Path, ...],
    session_id: str | None = None,
    limit: int = 50,
) -> dict:
    """Delete ONLY canonical, matching-digest Mabel episode files.

    Requires an explicit vault and protected roots. A mismatch fails closed and
    leaves the pending record for manual inspection. Run only while the
    experimental reviewer/promotion pipeline is idle.
    """
    if not isinstance(queue, CandidateQueue):
        raise TypeError("Expected owner-scoped CandidateQueue")
    if not protected_roots:
        raise CandidateAdmissionError("Protected source vault roots are required")
    if session_id is not None and (
        not isinstance(session_id, str) or not _SESSION.fullmatch(session_id)
    ):
        raise CandidateAdmissionError("Invalid deleted session filter")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CandidateAdmissionError("Cleanup limit must be 1..100")
    from .promotion import _target

    with queue._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if session_id is None:
            records = db.execute(
                "SELECT * FROM episode_deletion_queue WHERE owner=? "
                "ORDER BY queued_at,candidate_id LIMIT ?",
                (queue.owner,limit),
            ).fetchall()
        else:
            records = db.execute(
                "SELECT * FROM episode_deletion_queue WHERE owner=? AND session_id=? "
                "ORDER BY queued_at,candidate_id LIMIT ?",
                (queue.owner,session_id,limit),
            ).fetchall()
        cleaned = 0
        for row in records:
            # _target checks selected Mabel vault != protected source vaults,
            # paths, symlinks and the owner bucket before any file operation.
            path = _target(queue, row["candidate_id"], vault_root, protected_roots)
            if path.is_symlink():
                raise CandidateAdmissionError("Refusing to delete symlinked episode")
            if path.exists():
                digest = row["expected_digest"]
                if not _DIGEST.fullmatch(digest) or not path.is_file():
                    raise CandidateAdmissionError("Episode cannot be safely verified for deletion")
                if sha256(path.read_bytes()).hexdigest() != digest:
                    raise CandidateAdmissionError("Episode contents changed; manual cleanup required")
                path.unlink()
            db.execute(
                "DELETE FROM episode_deletion_queue WHERE owner=? AND candidate_id=?",
                (queue.owner,row["candidate_id"]),
            )
            cleaned += 1
        remaining = db.execute(
            "SELECT count(*) FROM episode_deletion_queue WHERE owner=?",
            (queue.owner,),
        ).fetchone()[0]
        return {"cleaned_episode_files": cleaned, "pending_episode_files": remaining}


def reconcile_missing_sessions(
    queue: CandidateQueue, *, db, limit: int = 100,
) -> dict:
    """Manually replay privacy scrubs missed by a crash after canonical delete.

    Checks the current owner-scoped session table, not model advice. Does not
    touch Markdown; verified episode files remain in the deletion queue for
    a separately approved target-vault cleanup.
    """
    if not isinstance(queue, CandidateQueue):
        raise TypeError("Expected owner-scoped CandidateQueue")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise CandidateAdmissionError("Reconciliation limit must be 1..1000")
    from sqlalchemy import text

    with queue._connect() as local:
        refs = [row[0] for row in local.execute(
            "SELECT source_ref FROM candidates WHERE owner=? "
            "AND source_kind='conversation' ORDER BY created_at,id",
            (queue.owner,),
        )]
    ids = []
    for ref in refs:
        match = re.fullmatch(
            r"chat:([A-Za-z0-9_.:-]{1,128}):message:[A-Za-z0-9_.:-]{1,128}",
            ref,
        )
        if match and match.group(1) not in ids:
            ids.append(match.group(1))
            if len(ids) >= limit:
                break
    scrubbed = 0
    deleted_sessions = 0
    for session_id in ids:
        owner = db.execute(
            text("SELECT owner FROM sessions WHERE id=:sid"),
            {"sid": session_id},
        ).scalar_one_or_none()
        if owner_key(owner) != queue.owner:
            result = purge_deleted_session(
                queue.path.parent, owner=queue.owner, session_id=session_id,
            )
            scrubbed += result["scrubbed"]
            deleted_sessions += 1
    return {
        "checked_sessions": len(ids), "deleted_sessions": deleted_sessions,
        "scrubbed": scrubbed,
    }

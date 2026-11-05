"""Explicit, read-only lexical search over source-verified promoted episodes.

No separate index, model call, automatic chat injection or filesystem writes.
The existing owner-scoped candidate queue, canonical conversation receipts,
and SHA-verified Markdown bytes are the only authorities. A future association
graph can consume these bounded hits without treating prose as trusted facts.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re

from .candidates import CandidateAdmissionError, CandidateQueue, CapturePolicy
from .promotion import _ID, _SOURCE, _verified_transcript
from .dottie_review import _check_source_memory_controls

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _search_root(
    queue: CandidateQueue,
    vault_root: str | Path,
    protected_roots: tuple[str | Path, ...],
) -> Path:
    """Inspect the canonical episode bucket without creating directories."""
    if not protected_roots:
        raise CandidateAdmissionError("Explicit protected source vault roots required")
    root = Path(vault_root).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise CandidateAdmissionError("Explicit Mabel vault must exist and not be symlinked")
    root = root.resolve()
    for protected in protected_roots:
        protected_path = Path(protected).expanduser().resolve()
        if root == protected_path or root.is_relative_to(protected_path):
            raise CandidateAdmissionError("Refusing to read episodes from a protected source vault")
    from .promotion import EPISODE_DIR
    if queue.path.resolve().is_relative_to(root / EPISODE_DIR[0]):
        raise CandidateAdmissionError("Candidate SQLite must remain outside the synced memory folder")
    bucket = sha256(queue.owner.encode("utf-8")).hexdigest()[:24]
    parent = root
    for part in (*EPISODE_DIR, bucket):
        parent /= part
        if parent.is_symlink():
            raise CandidateAdmissionError("Symlinked episode directory is not permitted")
        if parent.exists() and not parent.is_dir():
            raise CandidateAdmissionError("Episode directory is not a directory")
    return parent


def _verified_episode_path(
    parent: Path, candidate_id: str, expected_digest: str,
) -> str | None:
    """Return a verified exact note path; reject tampered or missing notes."""
    if not _ID.fullmatch(candidate_id) or not _DIGEST.fullmatch(expected_digest):
        return None
    path = parent / (candidate_id + ".md")
    if path.is_symlink() or not path.is_file():
        return None
    try:
        # A promoted episode contains only bounded excerpts. An unexpectedly
        # large file should never be read as a trusted memory source.
        if path.stat().st_size > 16384:
            return None
        if sha256(path.read_bytes()).hexdigest() != expected_digest:
            return None
    except OSError:
        return None
    return str(path)


def _tokens(value: str) -> set[str]:
    return {t.casefold() for t in _WORD.findall(value) if len(t) >= 2}


def search_verified_episodes(
    queue: CandidateQueue,
    query: str,
    *,
    db,
    vault_root: str | Path,
    protected_roots: tuple[str | Path, ...],
    policy: CapturePolicy,
    approved: bool = False,
    max_results: int = 10,
    scan_limit: int = 200,
    after_candidate_id: str | None = None,
) -> dict:
    """Inspect a bounded page of already-promoted, owner-scoped episodes.

    Query and excerpts are never sent to a model. Every returned result
    requires an active candidate, exact same-owner conversation receipt,
    current session memory controls, and an unmodified Markdown note.

    The returned path is informational only; no lookup follows arbitrary
    Markdown links. Scores are deterministic lexical matches, not estimates
    of factual truth or semantic relevance.
    """
    if approved is not True:
        raise PermissionError("Explicit read consent required")
    if not isinstance(queue, CandidateQueue) or not isinstance(policy, CapturePolicy):
        raise TypeError("Trusted candidate queue and CapturePolicy are required")
    policy.require_capture()
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 256:
        raise CandidateAdmissionError("Recall query must be between 1 and 256 characters")
    query_tokens = _tokens(query)
    if not query_tokens or len(query_tokens) > 24:
        raise CandidateAdmissionError("Recall query has no usable bounded tokens")
    if type(max_results) is not int or not 1 <= max_results <= 20:
        raise CandidateAdmissionError("Recall result limit must be 1..20")
    if type(scan_limit) is not int or not 1 <= scan_limit <= 500:
        raise CandidateAdmissionError("Recall scan limit must be 1..500")
    if after_candidate_id is not None and (
        not isinstance(after_candidate_id, str) or not _ID.fullmatch(after_candidate_id)
    ):
        raise CandidateAdmissionError("Invalid recall cursor")

    parent = _search_root(queue, vault_root, protected_roots)
    with queue._connect() as local:
        rows = [dict(row) for row in local.execute(
            "SELECT * FROM candidates WHERE owner=? AND state='promoted' AND id>? "
            "ORDER BY id LIMIT ?",
            (queue.owner, after_candidate_id or "", scan_limit + 1),
        )]
    page = rows[:scan_limit]
    has_more = len(rows) > scan_limit
    matches = []
    skipped_invalid = 0
    for candidate in page:
        if str(candidate.get("source_ref") or "").startswith("lifelog:day:"):
            # A kept moment from Mabel's account of his day: re-verify it against the day note.
            from .promotion import _verified_day_moment
            note = _verified_episode_path(parent, candidate["id"], candidate["decision_reason"])
            try:
                date, moment = _verified_day_moment(candidate, vault_root) if note else (None, None)
            except CandidateAdmissionError:
                note = None
            current = queue.get(candidate["id"]) if note else None
            if not note or current is None or current["state"] != "promoted" \
                    or current["decision_reason"] != candidate["decision_reason"]:
                skipped_invalid += 1
                continue
            score = 2 * len(query_tokens.intersection(_tokens(moment)))
            if score:
                matches.append({"candidate_id": candidate["id"], "source_ref": candidate["source_ref"],
                                "evidence_status": "day_note_verified", "note_path": note, "occurred_at": date,
                                "lexical_score": score, "user_excerpt": f"(from his day, {date}) {moment}",
                                "assistant_excerpt": ""})
            continue
        if (
            candidate["source_kind"] != "conversation"
            or candidate["kind"] != "experience"
            or _SOURCE.fullmatch(candidate["source_ref"]) is None
        ):
            skipped_invalid += 1
            continue
        note = _verified_episode_path(
            parent, candidate["id"], candidate["decision_reason"],
        )
        if note is None:
            skipped_invalid += 1
            continue
        try:
            user, assistant = _verified_transcript(queue, candidate, db)
            _check_source_memory_controls(db, candidate)
        except (CandidateAdmissionError, PermissionError):
            skipped_invalid += 1
            continue

        # A concurrent privacy scrub may have invalidated this candidate
        # while the canonical evidence was being checked. Re-read the row
        # before returning any excerpt.
        current = queue.get(candidate["id"])
        if (
            current is None
            or current["state"] != "promoted"
            or current["decision_reason"] != candidate["decision_reason"]
        ):
            skipped_invalid += 1
            continue
        user_overlap = len(query_tokens.intersection(_tokens(user)))
        assistant_overlap = len(query_tokens.intersection(_tokens(assistant)))
        score = 2 * user_overlap + assistant_overlap
        if score:
            matches.append({
                "candidate_id": candidate["id"],
                "source_ref": candidate["source_ref"],
                "evidence_status": "transcript_verified",
                "note_path": note,
                "occurred_at": candidate["created_at"],
                "lexical_score": score,
                "user_excerpt": user,
                "assistant_excerpt": assistant,
            })

    matches.sort(key=lambda row: (-row["lexical_score"], row["candidate_id"]))
    return {
        "owner": queue.owner,
        "results": matches[:max_results],
        "scanned": len(page),
        "matched": len(matches),
        "skipped_invalid": skipped_invalid,
        "next_after_candidate_id": page[-1]["id"] if has_more and page else None,
        "truncated_matches": len(matches) > max_results,
        "mode": "explicit_read_only",
    }

"""Advisory Phase 00.5 candidate reviews; no LLM calls or automatic memory writes.

Dottie (or an offline reviewer) may suggest whether a staged experience is
worth keeping. Proposals are model output, not verified autobiographical facts.
Only an explicitly approved call may apply a negative or duplicate-link
decision; actual Markdown promotion remains in the separately verified writer.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from uuid import uuid4

from .candidates import (
    CandidateAdmissionError, CandidateQueue, CapturePolicy, _now,
)

_RECOMMENDATIONS = frozenset({"promote", "reject", "defer", "quarantine", "merge"})
_NEGATIVE = {"reject": "rejected", "defer": "deferred", "quarantine": "quarantined"}
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ID = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT_FIELDS = (
    "id", "owner", "dedup_key", "kind", "actor", "source_kind",
    "source_ref", "source_event_id", "origin", "summary", "interpretation",
)


def candidate_fingerprint(candidate: dict) -> str:
    """Bind a review to one immutable candidate snapshot, without raw evidence."""
    if not isinstance(candidate, dict) or any(field not in candidate for field in _FINGERPRINT_FIELDS):
        raise CandidateAdmissionError("Cannot fingerprint an incomplete candidate")
    return sha256(json.dumps(
        [candidate[key] for key in _FINGERPRINT_FIELDS],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReviewProposal:
    """Strictly bounded model advice; never treated as evidence or authority."""

    run_id: str
    recommendation: str
    confidence: int
    rationale_code: str
    source_fingerprint: str
    target_candidate_id: str | None = None

    def validate(self) -> None:
        if not isinstance(self.run_id, str) or not _TOKEN.fullmatch(self.run_id):
            raise CandidateAdmissionError("Review run_id must be a bounded token")
        if self.recommendation not in _RECOMMENDATIONS:
            raise CandidateAdmissionError("Unsupported review recommendation")
        if type(self.confidence) is not int or not 0 <= self.confidence <= 100:
            raise CandidateAdmissionError("Review confidence must be an integer from 0 to 100")
        if not isinstance(self.rationale_code, str) or not _REASON.fullmatch(self.rationale_code):
            raise CandidateAdmissionError("Review rationale must be a bounded reason code")
        if not isinstance(self.source_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.source_fingerprint
        ):
            raise CandidateAdmissionError("Review must contain an exact source fingerprint")
        if self.recommendation == "merge":
            if not isinstance(self.target_candidate_id, str) or not _ID.fullmatch(self.target_candidate_id):
                raise CandidateAdmissionError("Merge review requires a target candidate")
        elif self.target_candidate_id is not None:
            raise CandidateAdmissionError("Only merge reviews may name a target")


def propose_review(
    queue: CandidateQueue, candidate_id: str, proposal: ReviewProposal, *,
    policy: CapturePolicy,
) -> dict:
    """Save Dottie's recommendation idempotently, without changing memory state."""
    if not isinstance(queue, CandidateQueue) or not isinstance(policy, CapturePolicy):
        raise TypeError("Trusted queue and CapturePolicy required")
    policy.require_capture()
    if not isinstance(candidate_id, str) or not _ID.fullmatch(candidate_id):
        raise CandidateAdmissionError("Invalid candidate ID")
    if not isinstance(proposal, ReviewProposal):
        raise TypeError("Expected typed ReviewProposal")
    proposal.validate()
    with queue._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        candidate = db.execute(
            "SELECT * FROM candidates WHERE owner=? AND id=?",
            (queue.owner, candidate_id),
        ).fetchone()
        if candidate is None:
            raise CandidateAdmissionError("Candidate not found for owner")
        candidate = dict(candidate)
        if candidate["state"] not in {"pending", "deferred"}:
            raise CandidateAdmissionError("Only unsettled candidates can receive new reviews")
        if candidate_fingerprint(candidate) != proposal.source_fingerprint:
            raise CandidateAdmissionError("Stale review: candidate evidence changed")
        if proposal.recommendation == "merge":
            target = db.execute(
                "SELECT * FROM candidates WHERE owner=? AND id=?",
                (queue.owner, proposal.target_candidate_id),
            ).fetchone()
            if candidate_id == proposal.target_candidate_id or target is None:
                raise CandidateAdmissionError("Merge target must be another candidate of the same owner")
            if target["state"] not in {"pending", "deferred", "promoted"}:
                raise CandidateAdmissionError("Merge target is not eligible")
        existing = db.execute(
            "SELECT * FROM candidate_reviews WHERE owner=? AND candidate_id=? AND run_id=?",
            (queue.owner, candidate_id, proposal.run_id),
        ).fetchone()
        if existing is not None:
            expected = {
                "recommendation": proposal.recommendation,
                "confidence": proposal.confidence,
                "rationale_code": proposal.rationale_code,
                "source_fingerprint": proposal.source_fingerprint,
                "target_candidate_id": proposal.target_candidate_id,
            }
            if any(existing[key] != value for key, value in expected.items()):
                raise CandidateAdmissionError("Conflicting replay for the same reviewer run")
            return {**dict(existing), "created": False}
        review_id = uuid4().hex
        db.execute(
            """INSERT INTO candidate_reviews
               (id,owner,candidate_id,reviewer,run_id,recommendation,confidence,
                rationale_code,source_fingerprint,target_candidate_id,created_at)
               VALUES(?,?,?,'dottie',?,?,?,?,?,?,?)""",
            (review_id,queue.owner,candidate_id,proposal.run_id,proposal.recommendation,
             proposal.confidence,proposal.rationale_code,proposal.source_fingerprint,
             proposal.target_candidate_id,_now()),
        )
        review = db.execute("SELECT * FROM candidate_reviews WHERE id=?", (review_id,)).fetchone()
        return {**dict(review), "created": True}


def list_reviews(queue: CandidateQueue, candidate_id: str, *, limit: int = 30) -> list[dict]:
    """Inspect suggestions, with owner isolation and no automatic transitions."""
    if not isinstance(queue, CandidateQueue):
        raise TypeError("Trusted queue required")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CandidateAdmissionError("Invalid review limit")
    if not isinstance(candidate_id, str) or not _ID.fullmatch(candidate_id):
        raise CandidateAdmissionError("Invalid candidate ID")
    with queue._connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM candidate_reviews WHERE owner=? AND candidate_id=? "
            "ORDER BY created_at,id LIMIT ?",
            (queue.owner,candidate_id,limit),
        )]


def apply_review(
    queue: CandidateQueue, review_id: str, *,
    policy: CapturePolicy,
    approved: bool = False,
) -> dict:
    """Explicit approval for reject/defer/quarantine/strict duplicate linking.

    This is a low-level privileged entry point. A future API route must check
    authenticated operator authority *before* supplying approved=True.
    Promote recommendations never call the Markdown writer automatically.

    Merge means "these separately recorded candidates have duplicate content,"
    not synthesizing a new verified event. Each source row remains inspectable.
    """
    if approved is not True:
        raise PermissionError("Explicit operator approval required")
    if not isinstance(queue, CandidateQueue) or not isinstance(policy, CapturePolicy):
        raise TypeError("Trusted queue and CapturePolicy required")
    policy.require_capture()
    if not isinstance(review_id, str) or not _ID.fullmatch(review_id):
        raise CandidateAdmissionError("Invalid review ID")
    with queue._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        review = db.execute(
            "SELECT * FROM candidate_reviews WHERE owner=? AND id=?",
            (queue.owner,review_id),
        ).fetchone()
        if review is None:
            raise CandidateAdmissionError("Review not found for owner")
        if review["applied_at"] is not None:
            return {"changed": False, "candidate_id": review["candidate_id"],
                    "outcome": review["applied_outcome"]}
        if review["recommendation"] == "promote":
            raise CandidateAdmissionError(
                "Promotion requires independent source verification and explicit vault approval"
            )
        candidate = db.execute(
            "SELECT * FROM candidates WHERE owner=? AND id=?",
            (queue.owner, review["candidate_id"]),
        ).fetchone()
        if candidate is None or candidate["state"] not in {"pending", "deferred"}:
            raise CandidateAdmissionError("Reviewed candidate is already settled")
        if candidate_fingerprint(dict(candidate)) != review["source_fingerprint"]:
            raise CandidateAdmissionError("Reviewed candidate's evidence changed")

        timestamp = _now()
        target_id = review["target_candidate_id"]
        if review["recommendation"] == "merge":
            target = db.execute(
                "SELECT * FROM candidates WHERE owner=? AND id=?",
                (queue.owner,target_id),
            ).fetchone()
            if target is None or target["state"] not in {"pending", "deferred", "promoted"}:
                raise CandidateAdmissionError("Merge target no longer exists or is no longer eligible")
            # No probabilistic matching may mutate the canonical memory graph.
            # Only the same source type and the *exact same candidate text*
            # can be linked at this early milestone.
            for name in ("kind", "source_kind", "summary", "interpretation"):
                if candidate[name] != target[name]:
                    raise CandidateAdmissionError("Merge requires strictly identical candidate content")
            outcome, event_kind = "merged", "memory.candidate_merged"
            reason = "operator_approved_duplicate"
            db.execute(
                "UPDATE candidates SET state=?, merged_into=?, decision_reason=?, updated_at=? "
                "WHERE owner=? AND id=?",
                (outcome,target_id,reason,timestamp,queue.owner,candidate["id"]),
            )
        else:
            outcome = _NEGATIVE[review["recommendation"]]
            event_kind = {
                "rejected":"memory.candidate_rejected",
                "deferred":"memory.candidate_deferred",
                "quarantined":"memory.candidate_quarantined",
            }[outcome]
            reason = review["rationale_code"]
            if outcome != candidate["state"]:
                db.execute(
                    "UPDATE candidates SET state=?,decision_reason=?,updated_at=? "
                    "WHERE owner=? AND id=?",
                    (outcome,reason,timestamp,queue.owner,candidate["id"]),
                )
        if outcome != candidate["state"]:
            payload = {
                "decision": outcome, "source_kind": candidate["source_kind"],
                "source_ref": candidate["source_ref"], "reason_code": reason,
            }
            if outcome == "merged":
                # This opaque reference is not private prose.
                payload["target_candidate_id"] = target_id
            db.execute(
                "INSERT INTO event_outbox(id,candidate_id,kind,payload,created_at) "
                "VALUES(?,?,?,?,?)",
                queue._envelope(candidate["id"],event_kind,payload),
            )
        db.execute(
            "UPDATE candidate_reviews SET applied_at=?, applied_outcome=? "
            "WHERE owner=? AND id=?",
            (timestamp,outcome,queue.owner,review_id),
        )
        return {"changed": outcome != candidate["state"],
                "candidate_id": candidate["id"],"outcome":outcome}

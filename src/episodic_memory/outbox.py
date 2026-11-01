"""Bridge staged episodic candidate events into the EXISTING Phase 00 ledger.

The SQLite outbox owns retries. The canonical JSONL ledger owns published
events; Event validates the exact Phase 00 schema. No new ledger is created.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from src.agent_runtime.event_ledger import Event, Ledger
from .candidates import CandidateQueue, CandidateAdmissionError


@dataclass(frozen=True)
class DeliveryReceipt:
    attempted: int
    acknowledged: int
    pending_next_page: int


def _event(entry: dict) -> Event:
    payload = entry["payload"]
    if not isinstance(payload, dict) or (
        payload.get("event_id") != entry["id"]
        or payload.get("candidate_id") != entry["candidate_id"]
    ):
        raise CandidateAdmissionError("Malformed staged memory event")
    candidate_id = payload["candidate_id"]
    if not isinstance(candidate_id, str) or len(candidate_id) != 32:
        raise CandidateAdmissionError("Malformed candidate identifier")
    source_ref = payload.get("source_ref")
    references = ["candidate_" + candidate_id]
    if isinstance(source_ref, str) and source_ref:
        references.append("source_" + sha256(source_ref.encode("utf-8")).hexdigest())
    cause = payload.get("source_event_id")
    causes = (cause,) if isinstance(cause, str) and cause.startswith("evt_") else ()
    # The ledger records opaque source identity, decision and causation only.
    # Candidate prose, reflection and personal source paths remain in the
    # local owner-scoped queue, not the debug/diagnostic event stream.
    return Event(
        schema_version=1,
        event_id="evt_" + entry["id"],
        occurred_at=entry["created_at"],
        actor="runtime",
        kind=entry["kind"],
        correlation_id="candidate_" + candidate_id,
        caused_by=causes,
        decision=payload.get("decision"),
        rationale_code=payload.get("reason_code"),
        references=tuple(references),
    ).validate()


def flush_candidate_events(
    queue: CandidateQueue, ledger: Ledger, *, limit: int = 50
) -> DeliveryReceipt:
    """Durably publish and acknowledge, with replay-safe stable event IDs.

    The outbox row remains pending if the ledger rejects an event or fails.
    Crash after ledger append but before outbox acknowledgement is safe:
    append_once recognises an identical pre-existing event by its stable ID.
    """
    if not isinstance(ledger, Ledger):
        raise TypeError("Pass Mabel's existing Phase 00 Ledger")
    events = queue.pending_events(limit=limit)
    acknowledged = 0
    attempted = 0
    for entry in events:
        event = _event(entry)
        attempted += 1
        ledger.append_once(event)
        if queue.mark_event_delivered(entry["id"]):
            acknowledged += 1
    return DeliveryReceipt(
        attempted=attempted,
        acknowledged=acknowledged,
        pending_next_page=len(queue.pending_events(limit=100)),
    )

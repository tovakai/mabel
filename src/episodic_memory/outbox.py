"""Crash-safe delivery boundary for the Phase 00 event ledger.

The real event ledger must be supplied by the caller; do not create a second
source of truth here. Delivery is *at least once* until the sink guarantees
append_once(event_id) is idempotent and durable. Nothing is active at startup.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .candidates import CandidateQueue, CandidateAdmissionError


class DurableEventSink(Protocol):
    """The canonical ledger supplies an atomic/durable append-or-already-seen.

    Returning True certifies that the exact event_id is durable, including
    a previous successful append. False means not acknowledged; never mark
    the outbox row delivered. A sink must reject conflicting payloads for the
    same ID rather than quietly overwriting evidence.
    """

    def append_once(self, event: dict) -> bool: ...


@dataclass(frozen=True)
class DeliveryReceipt:
    attempted: int
    acknowledged: int
    pending_next_page: int


def flush_candidate_events(
    queue: CandidateQueue, sink: DurableEventSink, *, limit: int = 50
) -> DeliveryReceipt:
    """Publish queued *metadata* to the existing ledger, then ack the outbox.

    Stops on the first failure to preserve causal order. If the process crashes
    after append but before ack, replay passes the exact same event_id and
    payload to the sink again. A non-idempotent sink is NOT supported.
    """
    events = queue.pending_events(limit=limit)
    acknowledged = 0
    attempted = 0
    for entry in events:
        payload = entry["payload"]
        if not isinstance(payload, dict) or (
            payload.get("event_id") != entry["id"]
            or payload.get("candidate_id") is None
        ):
            raise CandidateAdmissionError("Malformed candidate event envelope")
        full_event = {
            "schema_version": 1,
            "event_id": entry["id"],
            "occurred_at": entry["created_at"],
            "actor": "runtime",
            "kind": entry["kind"],
            "caused_by": [payload["source_event_id"]] if payload.get("source_event_id") else [],
            "decision": payload.get("decision"),
            "references": [payload["source_ref"]] if payload.get("source_ref") else [],
            "privacy": "metadata_only",
            "metadata": payload,
        }
        attempted += 1
        result = sink.append_once(full_event)
        if result is not True:
            break
        if queue.mark_event_delivered(entry["id"]):
            acknowledged += 1
    return DeliveryReceipt(
        attempted=attempted, acknowledged=acknowledged,
        pending_next_page=len(queue.pending_events(limit=100)),
    )

"""Phase 00 is REAL: candidate outbox deliveries reuse its typed JSONL ledger."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from src.agent_runtime.event_ledger import Event, Ledger, LedgerError
from src.episodic_memory import CandidateDraft, CandidateQueue, CapturePolicy
from src.episodic_memory.outbox import flush_candidate_events


@pytest.fixture
def staged(tmp_path):
    queue = CandidateQueue(tmp_path / "runtime", owner="alice")
    policy = CapturePolicy(episodic_enabled=True, memory_enabled=True)
    draft = CandidateDraft(
        kind="discovery", actor="mabel", source_kind="tool_observation",
        source_ref="session-a/turn-1/call-1", source_event_id="receipt-1",
        summary="Found a contradiction in two source notes.",
        origin="recorded_observation",
        interpretation="Could be a significant lesson, not yet proven.",
    )
    candidate = queue.propose(draft, policy=policy)
    return queue, candidate


def test_phase0_event_delivery_is_typed_and_redacted(staged, tmp_path):
    queue, candidate = staged
    ledger = Ledger(tmp_path / "events.jsonl")
    receipt = flush_candidate_events(queue, ledger)
    assert (receipt.attempted, receipt.acknowledged, receipt.pending_next_page) == (1, 1, 0)
    events, errors = ledger.read(kind="memory.candidate_created")
    assert not errors and len(events) == 1
    e = events[0]
    assert e.event_id.startswith("evt_")
    assert e.correlation_id == "candidate_" + candidate["id"]
    assert e.references[0] == "candidate_" + candidate["id"]
    assert e.references[1].startswith("source_")
    assert e.privacy == "metadata_only"
    assert e.caused_by == []
    raw = ledger.path.read_text(encoding="utf-8")
    assert "contradiction" not in raw
    assert "significant lesson" not in raw
    assert "session-a/turn" not in raw
    assert len(queue.pending_events()) == 0


def test_crash_between_ledger_append_and_outbox_ack_is_idempotent(staged, tmp_path):
    queue, candidate = staged
    ledger = Ledger(tmp_path / "events.jsonl")
    from src.episodic_memory.outbox import _event
    staged_event = queue.pending_events()[0]
    immutable = _event(staged_event)
    ledger.append_once(immutable)  # Simulate crash before queue acknowledgement.
    assert queue.pending_events()
    second = flush_candidate_events(queue, ledger)
    assert second.acknowledged == 1 and not queue.pending_events()
    events, errors = ledger.read(kind="memory.candidate_created")
    assert not errors and len(events) == 1
    assert events[0].event_id == immutable.event_id
    assert ledger.append_once(immutable) == immutable.event_id


def test_append_once_rejects_conflicting_event_id(tmp_path):
    ledger = Ledger(tmp_path / "events.jsonl")
    event = Event.create("runtime", "memory.candidate_created", correlation_id="candidate_test")
    assert ledger.append_once(event) == event.event_id
    with pytest.raises(LedgerError, match="Conflicting"):
        ledger.append_once(replace(event, decision="rejected"))
    entries, errors = ledger.read()
    assert not errors and len(entries) == 1


def test_concurrent_duplicate_delivery_creates_one_ledger_row(staged, tmp_path):
    queue, _candidate = staged
    ledger = Ledger(tmp_path / "events.jsonl")
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: flush_candidate_events(queue, ledger), range(3)))
    entries, errors = ledger.read(kind="memory.candidate_created")
    assert not errors and len(entries) == 1
    assert queue.pending_events() == []


def test_negative_candidate_decisions_publish_to_same_ledger(staged, tmp_path):
    queue, candidate = staged
    queue.decide(candidate["id"], outcome="rejected", reason="unsupported_evidence")
    ledger = Ledger(tmp_path / "events.jsonl")
    receipt = flush_candidate_events(queue, ledger)
    assert receipt.acknowledged == 2
    entries, errors = ledger.read()
    assert not errors and [e.kind for e in entries] == [
        "memory.candidate_created", "memory.candidate_rejected",
    ]
    assert entries[-1].decision == "rejected"
    assert entries[-1].rationale_code == "unsupported_evidence"


def test_broken_ledger_does_not_ack_candidate(staged, tmp_path):
    queue, _candidate = staged
    ledger = Ledger(tmp_path / "events.jsonl")
    ledger.append(Event.create("runtime", "runtime.agent_turn"))
    with ledger.path.open("ab") as out:
        out.write(b"{broken_tail")
    with pytest.raises(LedgerError):
        flush_candidate_events(queue, ledger)
    assert len(queue.pending_events()) == 1

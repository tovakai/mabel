"""Phase 00.5: source-bound staging without autobiography or vault mutation."""
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.episodic_memory import (
    CandidateAdmissionError, CandidateDraft, CandidateQueue, CapturePolicy,
)


@pytest.fixture
def draft():
    return CandidateDraft(
        kind="discovery", actor="mabel", source_kind="tool_observation",
        source_ref="session-abc/turn-3/tool-call-7",
        source_event_id="tool-receipt-7",
        origin="agent_observation",
        summary="An inspected Obsidian wikilink is unresolved.",
        interpretation="Maybe the note needs an alias, but this is not yet verified.",
    )


@pytest.fixture
def enabled():
    return CapturePolicy(episodic_enabled=True, memory_enabled=True, incognito=False)


@pytest.fixture
def queue(tmp_path):
    return CandidateQueue(tmp_path / "runtime", owner="alice",
                          protected_roots=(tmp_path / "core-of-truth",))


def test_disabled_incognito_and_no_memory_never_stage(queue, draft, enabled):
    denied = [
        CapturePolicy(),
        CapturePolicy(episodic_enabled=True, memory_enabled=False),
        CapturePolicy(episodic_enabled=True, memory_enabled=True, incognito=True),
    ]
    for policy in denied:
        with pytest.raises(PermissionError):
            queue.propose(draft, policy=policy)
    assert queue.list() == []
    assert queue.pending_events() == []


def test_replay_is_idempotent_even_if_summary_changes(queue, draft, enabled):
    initial = queue.propose(draft, policy=enabled)
    duplicate = queue.propose(
        CandidateDraft(**{**draft.__dict__, "summary": "A different summary of same event"}),
        policy=enabled,
    )
    assert initial["created"] and not duplicate["created"]
    assert duplicate["id"] == initial["id"]
    assert len(queue.list()) == 1
    assert len(queue.pending_events()) == 1
    assert queue.pending_events()[0]["kind"] == "memory.candidate_created"
    assert queue.pending_events()[0]["payload"]["source_event_id"] == "tool-receipt-7"
    assert "wikilink" not in str(queue.pending_events())
    assert "Maybe the note" not in str(queue.pending_events())


def test_restart_persists_pending_and_outbox(tmp_path, draft, enabled):
    state_dir = tmp_path / "local-data"
    first = CandidateQueue(state_dir, owner="alice")
    candidate_id = first.propose(draft, policy=enabled)["id"]
    restarted = CandidateQueue(state_dir, owner="alice")
    assert restarted.get(candidate_id)["state"] == "pending"
    assert len(restarted.pending_events()) == 1
    assert restarted.path.stat().st_mode & 0o777 == 0o600


def test_negative_decisions_are_idempotent_and_recall_never_enabled(queue, draft, enabled):
    row = queue.propose(draft, policy=enabled)
    rejected = queue.decide(row["id"], outcome="rejected", reason="unsupported_evidence")
    assert rejected["changed"]
    assert queue.decide(row["id"], outcome="rejected", reason="same_decision")["changed"] is False
    assert queue.list(state="pending") == []
    assert len(queue.pending_events()) == 2
    assert queue.pending_events()[-1]["kind"] == "memory.candidate_rejected"
    with pytest.raises(CandidateAdmissionError, match="already settled"):
        queue.decide(row["id"], outcome="deferred", reason="later")
    with pytest.raises(CandidateAdmissionError, match="promotion requires"):
        queue.decide(row["id"], outcome="promoted", reason="yes")


def test_defer_then_quarantine_and_delivery_outbox(queue, draft, enabled):
    row = queue.propose(draft, policy=enabled)
    queue.decide(row["id"], outcome="deferred", reason="needs_manual_review")
    queue.decide(row["id"], outcome="quarantined", reason="untrusted_source")
    kinds = [e["kind"] for e in queue.pending_events()]
    assert kinds == [
        "memory.candidate_created", "memory.candidate_deferred",
        "memory.candidate_quarantined",
    ]
    assert all("interpretation" not in str(e) for e in queue.pending_events())
    first_id = queue.pending_events()[0]["id"]
    assert queue.mark_event_delivered(first_id) is True
    assert queue.mark_event_delivered(first_id) is False
    assert len(queue.pending_events()) == 2


def test_owners_have_isolated_queues_and_cannot_read_each_others_rows(tmp_path, draft, enabled):
    alice = CandidateQueue(tmp_path / "state", owner="Alice")
    bob = CandidateQueue(tmp_path / "state", owner="bob")
    mine = alice.propose(draft, policy=enabled)
    assert alice.path != bob.path
    assert bob.get(mine["id"]) is None
    assert bob.list() == []
    with pytest.raises(CandidateAdmissionError, match="not found"):
        bob.decide(mine["id"], outcome="rejected", reason="not_mine")
    assert bob.propose(draft, policy=enabled)["id"] != mine["id"]


def test_invalid_or_unsourced_claim_rejected_before_write(queue, draft, enabled):
    for change in (
        {"kind": "lifetime_fact"}, {"source_ref": ""}, {"source_event_id": ""},
        {"summary": ""}, {"actor": ""}, {"origin": ""}, {"source_kind": "invented"},
    ):
        with pytest.raises(CandidateAdmissionError):
            queue.propose(CandidateDraft(**{**draft.__dict__, **change}), policy=enabled)
    assert queue.list() == []


def test_storage_never_touches_readonly_source_vault(tmp_path, draft, enabled):
    vault = tmp_path / "core-of-truth"
    vault.mkdir()
    original = vault / "Never-edit.md"
    original.write_text("# Don't mutate this\n")
    original_bytes = original.read_bytes()
    with pytest.raises(CandidateAdmissionError, match="outside protected"):
        CandidateQueue(vault / "Memory", owner="alice", protected_roots=(vault,))
    queue = CandidateQueue(tmp_path / "private-state", owner="alice",
                           protected_roots=(vault,))
    queue.propose(draft, policy=enabled)
    assert original.read_bytes() == original_bytes
    assert list(vault.iterdir()) == [original]


def test_duplicate_race_has_one_candidate_and_one_event(queue, draft, enabled):
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: queue.propose(draft, policy=enabled), range(8)))
    assert len({r["id"] for r in rows}) == 1
    assert len(queue.list()) == 1
    assert len(queue.pending_events()) == 1

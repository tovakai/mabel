"""Phase 00.5: source-fingerprinted advisory consolidation without autonomous writes."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from src.agent_runtime.event_ledger import Ledger
from src.episodic_memory import (
    CandidateAdmissionError, CandidateDraft, CandidateQueue, CapturePolicy,
    ReviewProposal, apply_review, candidate_fingerprint, flush_candidate_events,
    list_reviews, propose_review,
)


@pytest.fixture
def scenario(tmp_path):
    queue = CandidateQueue(tmp_path / "runtime", owner="alice")
    policy = CapturePolicy(episodic_enabled=True, memory_enabled=True)
    def make(source_id="source-1", summary="We traced a broken permission route."):
        draft = CandidateDraft(
            kind="experience", actor="mabel",
            source_kind="conversation",
            source_ref=f"chat:session-a:message:{source_id}",
            source_event_id=f"message:{source_id}",
            summary=summary, origin="completed_persisted_turn",
            interpretation="",
        )
        return queue.propose(draft, policy=policy)
    candidate = make()
    return queue, policy, candidate, make


def suggestion(candidate, action="reject", *, run="run-1", target=None, digest=None):
    return ReviewProposal(
        run_id=run, recommendation=action, confidence=90,
        rationale_code="low_significance",
        source_fingerprint=digest or candidate_fingerprint(candidate),
        target_candidate_id=target,
    )


def test_advisory_review_does_not_mutate_candidate_or_emit_event(scenario):
    queue, policy, candidate, _ = scenario
    before = list(queue.pending_events())
    proposal = suggestion(candidate)
    row = propose_review(queue, candidate["id"], proposal, policy=policy)
    assert row["created"] and row["reviewer"] == "dottie"
    assert row["applied_at"] is None
    assert row["recommendation"] == "reject"
    assert queue.get(candidate["id"])["state"] == "pending"
    assert list(queue.pending_events()) == before
    assert len(list_reviews(queue, candidate["id"])) == 1
    repeated = propose_review(queue, candidate["id"], proposal, policy=policy)
    assert not repeated["created"]
    assert repeated["id"] == row["id"]
    assert len(list_reviews(queue, candidate["id"])) == 1


def test_conflicting_run_and_bad_model_output_fail_closed(scenario):
    queue, policy, candidate, _ = scenario
    row = propose_review(queue, candidate["id"], suggestion(candidate), policy=policy)
    with pytest.raises(CandidateAdmissionError, match="Conflicting"):
        propose_review(queue, candidate["id"], suggestion(candidate, action="defer"), policy=policy)
    for bad in (
        ReviewProposal("run-2", "promote", True, "low_significance", candidate_fingerprint(candidate)),
        ReviewProposal("run-2", "delete_everything", 90, "low_significance", candidate_fingerprint(candidate)),
        ReviewProposal("run-2", "reject", 90, "malicious reason here", candidate_fingerprint(candidate)),
        ReviewProposal("run-2", "merge", 90, "duplicate", candidate_fingerprint(candidate)),
    ):
        with pytest.raises(CandidateAdmissionError):
            propose_review(queue, candidate["id"], bad, policy=policy)
    assert queue.get(candidate["id"])["state"] == "pending"
    assert len(list_reviews(queue, candidate["id"])) == 1


def test_advice_only_becomes_negative_transition_after_explicit_approval(scenario, tmp_path):
    queue, policy, candidate, _ = scenario
    saved = propose_review(queue, candidate["id"], suggestion(candidate), policy=policy)
    with pytest.raises(PermissionError):
        apply_review(queue, saved["id"], policy=policy)
    assert queue.get(candidate["id"])["state"] == "pending"
    result = apply_review(queue, saved["id"], policy=policy, approved=True)
    assert result["changed"] and result["outcome"] == "rejected"
    again = apply_review(queue, saved["id"], policy=policy, approved=True)
    assert not again["changed"] and again["outcome"] == "rejected"
    assert queue.get(candidate["id"])["state"] == "rejected"
    ledger = Ledger(tmp_path / "events.jsonl")
    assert flush_candidate_events(queue, ledger).acknowledged == 2
    events, errors = ledger.read()
    assert not errors
    assert [event.kind for event in events] == [
        "memory.candidate_created", "memory.candidate_rejected",
    ]
    assert "broken permission route" not in ledger.path.read_text()


def test_promote_review_can_never_bypass_verified_writer(scenario):
    queue, policy, candidate, _ = scenario
    row = propose_review(queue, candidate["id"], suggestion(candidate, "promote"), policy=policy)
    with pytest.raises(CandidateAdmissionError, match="independent source verification"):
        apply_review(queue, row["id"], policy=policy, approved=True)
    assert queue.get(candidate["id"])["state"] == "pending"
    assert len(queue.pending_events()) == 1


def test_stale_fingerprint_blocks_advice_and_application(scenario):
    queue, policy, candidate, _ = scenario
    saved = propose_review(queue, candidate["id"], suggestion(candidate), policy=policy)
    with queue._connect() as db:
        db.execute(
            "UPDATE candidates SET summary=? WHERE id=?",
            ("The actual source was modified.", candidate["id"]),
        )
    with pytest.raises(CandidateAdmissionError, match="evidence changed"):
        apply_review(queue, saved["id"], policy=policy, approved=True)
    with pytest.raises(CandidateAdmissionError, match="Stale review"):
        propose_review(queue, candidate["id"], suggestion(candidate, run="run-2"), policy=policy)
    assert queue.get(candidate["id"])["state"] == "pending"


@pytest.mark.parametrize("policy", [
    CapturePolicy(),
    CapturePolicy(episodic_enabled=True, memory_enabled=False),
    CapturePolicy(episodic_enabled=True, memory_enabled=True, incognito=True),
])
def test_disabled_policy_forbids_review_and_application(scenario, policy):
    queue, enabled, candidate, _ = scenario
    with pytest.raises(PermissionError):
        propose_review(queue, candidate["id"], suggestion(candidate), policy=policy)
    saved = propose_review(queue, candidate["id"], suggestion(candidate), policy=enabled)
    with pytest.raises(PermissionError):
        apply_review(queue, saved["id"], policy=policy, approved=True)
    assert queue.get(candidate["id"])["state"] == "pending"


def test_duplicate_link_requires_same_owner_same_content_and_manual_approval(scenario, tmp_path):
    queue, policy, source, make = scenario
    target = make("source-2")
    proposed = propose_review(
        queue, source["id"],
        suggestion(source, "merge", target=target["id"]), policy=policy,
    )
    result = apply_review(queue, proposed["id"], policy=policy, approved=True)
    assert result["changed"] and result["outcome"] == "merged"
    merged = queue.get(source["id"])
    assert merged["merged_into"] == target["id"]
    assert merged["state"] == "merged"
    assert queue.get(target["id"])["state"] == "pending"
    assert not apply_review(queue, proposed["id"], policy=policy, approved=True)["changed"]
    assert len(queue.list(state="merged")) == 1

    ledger = Ledger(tmp_path / "events.jsonl")
    assert flush_candidate_events(queue, ledger).acknowledged == 3
    events, errors = ledger.read(kind="memory.candidate_merged")
    assert not errors and len(events) == 1
    assert "candidate_" + target["id"] in events[0].references
    assert "candidate_" + source["id"] in events[0].references


def test_different_content_cannot_be_merged_by_high_confidence(scenario):
    queue, policy, candidate, make = scenario
    target = make("source-2", "Another, unrelated topic.")
    review = propose_review(
        queue, candidate["id"],
        suggestion(candidate, "merge", target=target["id"]), policy=policy,
    )
    with pytest.raises(CandidateAdmissionError, match="strictly identical"):
        apply_review(queue, review["id"], policy=policy, approved=True)
    assert queue.get(candidate["id"])["state"] == "pending"
    assert len(queue.pending_events()) == 2


def test_cannot_merge_into_other_owner_or_resolved_candidate(scenario, tmp_path):
    queue, policy, candidate, make = scenario
    bob = CandidateQueue(tmp_path / "runtime", owner="bob")
    copied = CandidateDraft(
        kind="experience", actor="mabel", source_kind="conversation",
        source_ref="chat:bob:message:7", source_event_id="message:7",
        summary=candidate["summary"], origin="completed_persisted_turn",
    )
    other = bob.propose(copied, policy=policy)
    with pytest.raises(CandidateAdmissionError, match="same owner"):
        propose_review(
            queue, candidate["id"],
            suggestion(candidate, "merge", target=other["id"]), policy=policy,
        )
    target = make("source-2")
    saved = propose_review(
        queue, candidate["id"],
        suggestion(candidate, "merge", target=target["id"]), policy=policy,
    )
    queue.decide(target["id"], outcome="rejected", reason="invalid_source")
    with pytest.raises(CandidateAdmissionError, match="no longer eligible"):
        apply_review(queue, saved["id"], policy=policy, approved=True)
    assert queue.get(candidate["id"])["state"] == "pending"


def test_concurrent_same_run_does_not_duplicate_review(scenario):
    queue, policy, candidate, _ = scenario
    proposal = suggestion(candidate)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(
            lambda _: propose_review(queue, candidate["id"], proposal, policy=policy),
            range(8),
        ))
    assert len({r["id"] for r in results}) == 1
    assert sum(r["created"] for r in results) == 1
    assert len(list_reviews(queue, candidate["id"])) == 1


def test_schema_upgrade_keeps_previous_candidate_state(scenario):
    queue, policy, candidate, _ = scenario
    # Simulate an existing v1 install with original candidates and outbox.
    with sqlite3.connect(queue.path) as conn:
        conn.execute("DROP TABLE candidate_reviews")
        conn.execute("PRAGMA user_version=1")
    reopened = CandidateQueue(queue.path.parent, owner="alice")
    assert reopened.get(candidate["id"])["state"] == "pending"
    assert len(reopened.pending_events()) == 1
    review = propose_review(reopened, candidate["id"], suggestion(candidate), policy=policy)
    assert review["created"]
    with sqlite3.connect(queue.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4

# Phase 00.5: message-source invalidation pilot

This slice extends session-level retention to changed or removed individual
message receipts. It is deliberately conservative and does **not** enable
automatic Dottie reviews, Markdown promotion, or episodic recall.

## Canonical mutation paths covered

- SessionManager.truncate_messages: after committing tail deletion, revoke the
  actual removed message IDs. Earlier surviving receipts remain eligible.
- SessionManager.replace_messages: after committing full transcript replacement
  (including manual compaction), revoke all old message IDs and invalidate
  the original session's staged candidates. The new transcript uses fresh IDs,
  so future completed turns can still create new candidates.
- Chat route assistant rewrite: after a changed assistant reply commits, revoke
  the saved assistant message ID and remove its old candidate evidence.

No revocation is attempted before canonical persistence commits. An
experimental episodic SQLite error cannot undo a legitimate user edit.
The separate-database boundary means a crash between canonical and
episodic commits still requires explicit reconciliation.

## Storage and deletion behavior

The additive local SQLite schema v4 introduces owner/session/message-scoped
opaque revocation tombstones, separate from whole-session tombstones.

An invalidated candidate loses its staged summary, interpretation, advisory
reviews, and unpublished Phase 00 event envelopes. For a previously promoted
or in-progress note, only an opaque candidate ID plus expected SHA-256 enters
the existing episode deletion queue. No arbitrary path is inferred and the
user's Core of Truth remains read-only.

Individual-message revocation does not mark the whole session as deleted.
A new, distinct persisted assistant message ID may be captured after a
compaction. No re-creation from a revoked old message ID is permitted.

Because a candidate currently stores the bounded user/assistant excerpt and
the assistant receipt ID, full transcript replacement invalidates all of
that session's candidates. This avoids claiming to know which assistant
experiences still prove the same thing after a compaction.

## Explicit recovery after interrupted source edits

In the correct Mabel checkout, using the same actual owner and local data
directory:

    python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER \
      reconcile-sources --confirm-scrub

This compares the persisted transcript to each pending or promoted candidate
source. A missing assistant, changed user excerpt, changed assistant reply,
disabled source extraction, or owner mismatch invalidates that candidate or
the missing session, without invoking any LLM.

For large queues the command returns next_after_candidate_id; continue
using that cursor with:

    python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER \
      reconcile-sources --confirm-scrub \
      --after-candidate-id PREVIOUS_NEXT_AFTER_CANDIDATE_ID

Inspect counts:

    python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER retention-status

If pending episode files remain, follow the prior retention pilot's
operator-approved, exact-digest vault cleanup procedure.

## Limitations

- There is no dedicated single-message delete route in this slice. Existing
  truncate, replace/compact, and rewrite mutation paths are covered.
- Reconciliation is opt-in, not a heartbeat or unattended job.
- The canonical conversation and episodic queue have separate transactions,
  so an interrupted deletion can leave staged content until reconciliation.
- Previously promoted Markdown is queued for explicit protected-root cleanup.
- Existing Phase 00 metadata-only ledger entries, backups, and Syncthing
  history cannot be erased by this SQLite operation.
- No new meaning extraction, associative graph, or recall injection is enabled.

## Focused regression suite

    ../mabel-runtime-test/.venv/bin/python -m pytest -q --tb=short \
      tests/test_episodic_message_invalidation.py \
      tests/test_episodic_session_retention.py \
      tests/test_episodic_dottie_review.py \
      tests/test_episodic_review_consolidation.py \
      tests/test_episodic_verified_promotion.py \
      tests/test_episodic_candidate_foundation.py \
      tests/test_episodic_phase0_delivery.py \
      tests/test_episodic_completed_turn_capture.py \
      tests/test_phase0_foundations.py \
      tests/test_session_actions_cleanup.py \
      tests/test_session_manager_cleanup.py

The tests use synthetic messages and temporary SQLite/vault fixtures only.

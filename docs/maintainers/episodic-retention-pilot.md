# Phase 00.5: source deletion and retention pilot

Episodic capture and Dottie review remain off by default. This branch adds a
privacy cleanup path for **entire sessions** that are permanently deleted from
Mabel's canonical conversation database.

## What is removed automatically

Once the canonical session delete commits, owner-scoped deletion cleanup:

- Permanently removes the session's candidate excerpts, interpretations and
  Dottie review rows from the separate local SQLite candidate database.
- Drops unpublished event-outbox entries referring to the deleted candidates.
- Records a tiny per-owner/session opaque tombstone so delayed capture cannot
  recreate that session's candidates.
- Preserves unrelated owner/session data.
- Records a separate hash-only cleanup task if a candidate had already begun
  promotion or had a committed Markdown episode.

This is called from SessionManager.delete_session, auto-sort cleanup and
administrative delete-all. It is a no-op when the owner never had an episodic
candidate database **and the capture pilot is disabled**. When capture is
enabled, deletion creates a tombstone even before the first candidate
database exists, blocking delayed post-response capture. Existing Phase 00 ledger events already published are
metadata-only and are not rewritten.

## Important boundary: promoted Markdown notes

**Removing source-linked SQLite state is not equivalent to deleting a promoted
Markdown note.** A previously promoted episode may already exist in Mabel's
writable vault. We do NOT guess its location, try to discover any other vault
or automatically delete a modified Markdown note. The cleanup queue retains
only its opaque candidate ID and expected content SHA-256.

A trusted operator must choose Mabel's exact vault and at least one protected
source vault. While no promotion/review job is running:

    python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER retention-status

    python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER \
      cleanup-deleted-episodes \
      --vault-root '/ABSOLUTE/PATH/TO/MABEL-VAULT' \
      --protected-root '/ABSOLUTE/PATH/TO/CORE-OF-TRUTH' \
      --confirm-remove-files

Only the canonical owner-scoped episode filename can be removed. Existing
files require an exact SHA-256 match to the stored promotion digest. Missing
files count as completed cleanup; mismatching, unknown or symlinked files fail
closed and leave a pending record for manual investigation. Never run this
against a production vault without first confirming its location and backups.

## Recovery after crash or interrupted cleanup

Because canonical conversations and candidate SQLite files live in separate
databases, the deletion hook is a best-effort post-commit operation. A process
crash between the two commits can temporarily leave a stale candidate.
The manual recovery command checks each source session against the canonical
database (including owner) and scrubs missing or reassigned sources:

    python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER \
      reconcile-deletions --confirm-scrub

For large queues, reconciliation returns next_after_session_id when there
are more sessions to inspect. Continue with that opaque cursor:

    python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER \
      reconcile-deletions --confirm-scrub \
      --after-session-id PREVIOUS_NEXT_AFTER_SESSION_ID

Follow that with retention-status, then verified Markdown cleanup if any
pending episode files exist. This does not require Dottie or model inference.

## Explicit limitations

- This initial slice handles **whole-session deletion** only. Individual
  message removal, transcript compaction, account deletion, and retention-time
  expiry need their own verified source invalidation paths.
- A crash after canonical deletion but before episodic scrubbing requires
  explicit reconciliation; this is not yet a scheduled daemon.
- Previously promoted Markdown requires the separate, operator-selected
  cleanup step. There is no automatic vault-location guessing or silent file
  removal.
- Synced copies, backups and already published metadata-only ledger records
  cannot be retroactively removed by this SQLite operation.
- Do not enable autonomous reviews or episodic recall until these remaining
  privacy and recovery gates are proven end-to-end.

## Synthetic tests

    ../mabel-runtime-test/.venv/bin/python -m pytest -q --tb=short \
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

All new tests use temporary directories and synthetic sessions. No real user
conversation, external LLM, production vault, or account data is modified.

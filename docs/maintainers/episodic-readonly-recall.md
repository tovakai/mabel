# Phase 00.5: explicit, read-only episodic recall pilot

This milestone adds **lexical inspection only**. It is not the future
associative graph, semantic embedding index, or conversational memory
injection. The legacy factual memory and prompt composer remain unchanged.

## Source of truth

The existing owner-scoped candidate queue already identifies episodes in
state promoted. The original promotion writer creates an immutable,
deterministically named Markdown file in Mabel's writable vault under
memory/episodes/<owner-hash>/<candidate-id>.md.

A read-only search requires **all** of the following before returning a hit:

1. The operator explicitly enables this pilot for a specific owner and
   provides both Mabel's actual vault root and protected source vault roots.
2. The candidate is still marked promoted and belongs to that exact owner.
3. The expected episode file exists at its canonical owner bucket, with no
   symlink traversal, and its SHA-256 exactly matches the digest stored at
   promotion. It is small enough to be a bounded episode.
4. The authoritative conversation database still has that assistant receipt,
   the associated user excerpt, matching source ownership and enabled
   session memory controls. The saved excerpts must still match the candidate.
5. The candidate remains promoted when the result is assembled.

The search returns literal source excerpts rather than relying on arbitrary
later edits to Markdown. It labels the evidence transcript_verified, which
means *the quoted words existed*, not *the assertions are true*.

A tampered or missing file, deleted session, edited conversation, disabled
memory settings or owner mismatch causes the result to be **skipped**, never
replaced with a guessed answer. The query is bounded and scored using a tiny,
deterministic lexical overlap heuristic. No LLM, Dottie call, reranker or
embedding computation happens here.

## Controlled operator use

On the exact Mabel checkout and Mabel account that owns the episodes, after
confirming both directory roots:

    MABEL_EPISODIC_CAPTURE_ENABLED=1 \
      python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER \
      recall 'permission boundary' \
      --vault-root '/ABSOLUTE/PATH/TO/MABEL-VAULT' \
      --protected-root '/ABSOLUTE/PATH/TO/CORE-OF-TRUTH' \
      --read-source-excerpts

The explicit --read-source-excerpts flag acknowledges that source text is
printed to the operator's terminal; avoid sharing logs with private excerpts.

Read-only recall never inserts data into a prompt and does not automatically
open files, follow Obsidian wikilinks, or answer a chat message.

With a large candidate queue, the output includes a next_after_candidate_id
cursor. Continue the scan with --after-candidate-id, then aggregate or inspect
the results. The lexical ranking is per page, not a global index ranking.
Default scan is a bounded page of at most 200 candidate rows.

## Security and lifecycle

- Use the selected Mabel vault only, never Core of Truth as the destination.
- No automatic background activity, automatic promotion or inference.
- A deletion can race with any read, since the canonical conversation DB,
  candidate DB and Markdown are separate stores. This is an operator-only
  diagnostic until a lifecycle-fenced recall design exists.
- Previously published, independently backed-up or synced notes are outside
  this operation's deletion authority.
- No external index or additional state is created by recall.

## Focused synthetic tests

    ../mabel-runtime-test/.venv/bin/python -m pytest -q --tb=short \
      tests/test_episodic_readonly_recall.py \
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

These tests use temporary synthetic persisted conversations and vaults. They
perform no real model inference and never inspect the actual user vault.

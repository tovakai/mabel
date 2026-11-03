# Phase 00.5: evidence associations, operator-only pilot

This slice constructs an **ephemeral, read-only, two-hop association graph**
from already-promoted, still-source-verified episodic notes. Nothing writes a
new graph index or embeds, trains, reranks or injects information into Mabel's
conversation context. It is an exploratory bridge between the earlier lexical
recall prototype and future associative retrieval.

## What an association means

An association is a description of the surviving source transcripts, **not
a fact about the world or a model's belief**.

- same_source_session: both promoted candidates have distinct assistant
  receipts from the *same verified canonical chat session*. This establishes
  a source-session relationship, not that one event caused another.
- shared_source_terms: two independently verified excerpts share at least
  two informative (non-boilerplate) normalized words. The exact shared words
  are included as evidence; matching vocabulary does not establish that the
  episodes refer to the same event, person or project.

Each node contains a stable candidate ID, exact persisted source reference,
verified note path, original saved excerpts and a provenance label. Each edge
contains its two candidate IDs, typed relationship labels, a deterministic
bounded score and up to eight actual shared terms. No guessed identity or
unsourced assertion is generated.

The graph can traverse **one or two hops**. A second-hop relation has only
the evidence for its own edge, not a synthesized interpretation claiming
the seed and the second-hop node are inherently related.

## Authorization and integrity

The operator must explicitly specify Mabel's own writable vault and one or
more protected source vault roots; the latter are never selected as the
episodic read target. All nodes are checked independently:

1. Owner-scoped candidate exists and has state promoted.
2. Exact canonical owner-bucket Markdown file exists without symlink
   traversal and matches its stored SHA-256 digest.
3. Original persisted assistant receipt, paired user excerpt, and current
   session owner still match the saved candidate source evidence.
4. Source session and caller memory controls remain enabled.
5. Candidate is still promoted after source I/O verification.

Deleted, edited, unpromoted or tampered notes are skipped. If the seed fails
validation, the entire call fails closed. No network or model call occurs.
Results are marked as transcript-verified **words**, not verified claims.

A graph uses at most 500 promoted candidate rows per scan, at most 30 nodes,
100 edges and two hops. A pagination cursor is provided when more candidate
rows exist. Edges are only built among the validated nodes in that scan, so a
partial page is not a complete global memory graph.

## Explicit CLI pilot

Only on a controlled checkout with an authenticated/known storage owner, and
only after deliberately selecting both vault locations:

    MABEL_EPISODIC_CAPTURE_ENABLED=1 \
      python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER \
      associations \
      --candidate-id CANDIDATE_HEX_ID \
      --vault-root '/ABSOLUTE/PATH/TO/MABEL-VAULT' \
      --protected-root '/ABSOLUTE/PATH/TO/CORE-OF-TRUTH' \
      --read-source-excerpts --depth 2

The read-source-excerpts switch is explicit because the result includes
potentially private conversation excerpts printed to the shell. For large
queues, continue scanning with --after-candidate-id and aggregate externally.
This operator CLI does not provide a chat/agent tool or a WebUI interface.

## Known limits and next gate

- Links are deliberately conservative lexical/session associations. They
  cannot identify aliases, synonyms, emotional salience, chronology beyond
  the stored source, or causal structure.
- The source session, candidate DB and vault file are separate stores. The
  checks strongly reduce stale evidence exposure, but are not one global
  privacy-locked transaction. Background recall must not be enabled before
  lifecycle-fenced deletion/retrieval are designed.
- Future integration can use the existing knowledge-engine graph/embedding
  primitives and Dottie semantic scoring, but no new model role, endpoint,
  persistent graph table, or automatic scheduler is added here.

## Synthetic tests

    ../mabel-runtime-test/.venv/bin/python -m pytest -q --tb=short \
      tests/test_episodic_evidence_associations.py \
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

All new graph tests use synthetic persisted conversations and temporary
vaults. No real Dottie inference, source-vault mutation or background action.

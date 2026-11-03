# Phase 00.5: Dottie significance review pilot

The first Dottie integration is deliberately **manual and one-shot**. It does
not schedule work, run after each chat, automatically reject a candidate,
promote Markdown into a vault, or inject episodic recall into prompts.

## Data and trust boundary

- Reads one candidate from the per-owner SQLite staging queue.
- Verifies the candidate excerpt against the exact persisted conversation
  receipt. Missing, changed, deleted and owner-mismatched sources fail closed.
- Requires memory extraction and memory injection enabled in the source session,
  the current user memory preference, and explicit operator consent.
- Resolves **only** the configured owner-scoped **Research / Dottie** endpoint
  and model. It does not fall back to Utility, Default or cloud models when
  the research role is unconfigured or unavailable.
- Uses the interactive-quiet gate when called in the Mabel backend process.
  The standalone CLI has separate process-local gate state and **cannot**
  detect foreground WebUI activity. Run this manual pilot only while the
  server is idle. Sends bounded excerpts (up to
  500 characters of each side of the exchange, plus up to 500 characters of
  clearly labelled unverified interpretation) for strict JSON classification.
- Treats model output as untrusted advisory data. Only the exact three-field
  schema with an allowed action, integer confidence and rationale code is
  persisted as a ReviewProposal.
- Does not write candidate prose into the Phase 00 ledger. No model opinion
  is considered evidence of events that actually occurred.
- Repeated calls on the same candidate/model/prompt revision reuse the stored
  review, avoiding a second inference request.

The configured Research endpoint receives excerpts over the network. Trust
that endpoint with the supplied material before opting in. This feature is
disabled by default and requires affirmative per-call permission.

## Controlled operator invocation

Use a test/sandbox Mabel backend. The research role must be configured
explicitly for the same owner as the persisted candidate.

To list candidates without sending data:

    .venv/bin/python -m src.episodic_memory \
      --owner ACTUAL_MABEL_OWNER list

After selecting a pending candidate ID and explicitly trusting the endpoint:

    MABEL_EPISODIC_CAPTURE_ENABLED=1 \
      .venv/bin/python -m src.episodic_memory \
        --owner ACTUAL_MABEL_OWNER \
        review --candidate-id CANDIDATE_HEX_ID \
        --send-source-to-dottie

The operator command rejects missing consent, disabled pilot settings,
disabled user/session memory controls, missing source messages, or an
unconfigured Research endpoint. It prints only review metadata, never source
excerpts. Credentials are retrieved from Mabel's endpoint registry.

The review command does not call apply_review or
promote_verified_conversation. These remain separate, explicitly approved
operations. There is no WebUI approval route in this milestone.

## Synthetic regression tests

    .venv/bin/python -m pytest -q --tb=short \
      tests/test_episodic_dottie_review.py \
      tests/test_episodic_review_consolidation.py \
      tests/test_episodic_verified_promotion.py \
      tests/test_episodic_candidate_foundation.py \
      tests/test_episodic_phase0_delivery.py \
      tests/test_episodic_completed_turn_capture.py \
      tests/test_phase0_foundations.py

The tests use a simulated inference function and a synthetic persisted
conversation, not a real Dottie call or any production vault.

## Still required before unattended use

Session deletion and retention propagation into the staging queue, actual
WebUI/operator authorization binding, cancellation, bounded scheduled jobs
with fenced leases, rate limiting, and full no-memory rollback validation.
Do not enable unattended review or auto-acceptance from this slice.

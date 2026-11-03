# Phase 00.5: one-shot Dottie semantic association pilot

The existing two-hop evidence graph (PR #16) finds only exact source-session
links and overlapping keywords. This pilot explores whether the *already
configured* Dottie Knowledge Embeddings service (normally BGE-M3, with its
owner-scoped model role) can suggest related experiences that use different
words.

**This is an experimental advisory view, not persistent memory.** It does not
create Markdown, store vectors, change candidate decisions, write graph edges,
call Gemma, spawn a scheduler or inject anything into Mabel's chat context.

## Explicit owner consent and endpoints

1. Check that the exact Mabel storage owner has explicitly configured
   **Knowledge Embeddings** to Dottie's BGE-M3 endpoint/model (normally on the
   Windows machine's port 8082). The role must declare the embedding
   capability, supported OpenAI-compatible or Ollama adapter, a stable space
   ID, a real model revision, exact dimensions (1024 for ordinary BGE-M3) and
   l2 normalization. Do not assume 127.0.0.1:8082 on Arch is Dottie.
2. Choose the actual Mabel-owned vault root and at least one protected source
   vault. The Core of Truth root is **protected**, not searched.
3. Use the explicit pilot flag and --send-source-to-embedding for **one**
   operator-selected seed/page. This sends at most nine verified excerpt
   strings (one seed plus at most eight by default), with no complete
   transcript, interpretation, Markdown or arbitrary filename.
4. Run only when foreground inference is quiet. The priority gate is
   process-local and a separate CLI does not observe another WebUI process's
   activity; this command must not be installed as a background service.

Example, after filling in exact paths and the real account owner:

    MABEL_EPISODIC_CAPTURE_ENABLED=1 \
      python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER \
      semantic-links \
      --candidate-id VERIFIED_EPISODE_ID \
      --vault-root '/ABSOLUTE/PATH/TO/MABEL-VAULT' \
      --protected-root '/ABSOLUTE/PATH/TO/CORE-OF-TRUTH' \
      --send-source-to-embedding \
      --scan-limit 8 --max-suggestions 5 --min-similarity 0.70

The command refuses an absent or incompatible embedding role; there is no
automatic fallback to the utility model, foreground LLM, arbitrary provider,
or local CPU embedding process. The same configured embedding endpoint used
by Mabel's knowledge engine is reused with its existing credentials/settings.

## Evidence and numerical meaning

Before a single embedding call, the seed and every compared candidate must
still be promoted, owner scoped, source-transcript verified, enabled for
memory, and represented by an unmodified, SHA-256-matching canonical Mabel
episode file. A removed, disabled or tampered candidate is excluded.

Only bounded user/assistant excerpts are embedded. Returned vectors are
dimension-checked, finite-checked and normalized by the existing
knowledge-engine validator. Cosine similarity is ranked deterministically.
The returned edge type is semantic_similarity_suggestion, and the evidence
kind explicitly says inference_over_verified_transcript_excerpts. **A high
cosine value is NOT verification that two episodes describe the same person,
fact, cause or event.**

After inference, the seed is rechecked; if it changed, the call aborts.
Each suggested candidate is rechecked independently and omitted if invalid
or changed. A deletion during network transmission cannot undo what has
already been sent, and there is no globally serializable transaction across
the canonical chat, local queue and Markdown vault. This experimental CLI
must remain manually invoked until stronger lifecycle fencing exists.

## Bounded work and tradeoffs

- Default: one seed, eight candidate rows, five suggested links, threshold
  0.70, 60-second inference timeout and no fallback routes. At most 16
  candidate rows per call and eight returned suggestions are allowed.
- Pagination is by opaque candidate ID, not a globally stable similarity
  index. The --after-candidate-id flag allows another scan page.
- All vectors are ephemeral and discarded after returning suggestions.
  Repeating calls repeats embedding work and GPU utilization.
- No automatic promotion, memory editing, cleanup or episodic recall hook.
- Console output includes source excerpts; treat terminal logs as private.

We should next compare two or three **synthetic and intentionally different
paraphrase cases** on the live BGE-M3 endpoint, measure output quality/latency,
and only then consider storing derived links or adding a hybrid association
retriever. The existing read-only Obsidian knowledge-engine semantic index
remains separate.

## Regression tests

    ../mabel-runtime-test/.venv/bin/python -m pytest -q --tb=short \
      tests/test_episodic_semantic_advisory.py \
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

The new tests inject synthetic vectors and temp database/vault fixtures. They
do not contact a real Dottie endpoint, modify real user data or enable a
background embedding process.

# LLM_CORE

**Path:** `src/llm_core.py`

## Purpose

OpenAI-compatible streaming, fallback routing, and recoverable pre-content retries for context overflow, availability, capability, and degenerate output.

## Context recovery carry-forward

`_stream_candidate_with_context_recovery` trims a *copy* of the candidate request when the provider rejects for context. On a successful retry it:

1. Optionally emits `context_recovery_prepared` then `context_recovery_applied` when `diagnostic_events` is enabled (distinct from proactive `compaction_prepared` and post-stream `context_recovery_committed`)
2. Fills `recovery_out` / `context_recovery_out[index]` with the compacted wire messages and `context_limit`
3. Calls `remember_provider_context_limit` so later budgeting knows the proven ceiling

The agent loop commits **after the answering stream settles**, independent of diagnostics (including incognito). Commitment compacts the canonical message list with the learned limit via `compact_canonical_messages_for_limit` — it does not adopt the unprotected wire snapshot. Speculative fallback histories that never emit leave no applied bag entry.

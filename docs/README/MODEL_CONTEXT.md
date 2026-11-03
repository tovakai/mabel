# MODEL_CONTEXT

**Path:** `src/model_context.py`

## Purpose

Discover and cache model context windows for budgeting. Local Strata servers are probed via `/health` `max_context` when the service is loaded; llama.cpp `/slots` and `/props` remain fallbacks.

## Provider rejection ceilings

`remember_provider_context_limit(endpoint, model, limit)` records a tighter ceiling learned from a real context rejection. Catalog/table evidence may still be tightened by that override.

`reconcile_local_runtime_context(endpoint, model, limit)` is called when Strata `/health` or llama.cpp `/slots`/`/props` reports a live serving window. That authoritative probe may raise or lower a previously remembered rejection so a reload cannot permanently pin capacity to an older overflow.

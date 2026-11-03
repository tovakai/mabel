# MODEL_CONTEXT

**Path:** `src/model_context.py`

## Purpose

Discover and cache model context windows for budgeting. Trusted local Strata servers are probed via `/health` (`service=strata`, `loaded=true`, `max_context`) even when the endpoint is classified `proxy` because it uses an API key. llama.cpp `/slots` and `/props` remain fallbacks for non-Strata local servers. Public remote proxies are never probed via `/health`.

## Authoritative local Strata

`_probe_trusted_strata_runtime` runs only for localhost / private / Tailscale hosts. A fresh loaded Strata `max_context` overrides generic Qwen known-table metadata and previously remembered rejection ceilings (`reconcile_local_runtime_context`). Trusted local hosts always re-probe so a reload can raise or lower capacity.

`runtime_served_model(endpoint)` / `remember_runtime_served_model` record the `/health` `model` id for telemetry. Budgeting uses the active window for the request; session preferences and cached catalogs are not rewritten when the served id differs (for example catalog `gyro-m` vs served `gyro-s`).

## Provider rejection ceilings

`remember_provider_context_limit(endpoint, model, limit)` records a tighter ceiling learned from a real context rejection. Catalog/table evidence may still be tightened by that override.

`reconcile_local_runtime_context(endpoint, model, limit)` is called when Strata `/health` or llama.cpp `/slots`/`/props` reports a live serving window. That authoritative probe may raise or lower a previously remembered rejection so a reload cannot permanently pin capacity to an older overflow.

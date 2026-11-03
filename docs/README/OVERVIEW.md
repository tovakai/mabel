# OVERVIEW

Project documentation map for Mabel/Odysseus script and module notes under `docs/README/`.

## Multi-device filesystem (2026-10-28)

Mabel’s desktop-hosted backend owns filesystem access across registered devices:

- **Devices** — local builtin + SSH/SFTP remotes (`src/fs_devices/`, Settings → Devices)
- **Allowed Directories** — persistent grants (`ro`/`rw`) per device
- **Active Directories** — per-conversation subset; a preferred local shell directory is optional and is not required for tools
- **Tool authorization** — existing RequestAuthority / approvals unchanged

See `docs/maintainers/multi-device-filesystem.md` for architecture, migration, and manual validation.

## Workspace-less execution and tool recovery

Tool failures return structured, distinct error codes and stay recoverable when the operation was not denied. A long draft followed by a recoverable tool error does not end the turn. An empty completed reply is replaced with the tool failure. Vault and index tools use an explicit or configured vault path and do not substitute an authorization root. Shell cwd prefers an optional primary, otherwise the documented data-directory workspace; remote-only conversations do not inherit a local cwd. Model traces label provider timings separately from wall-clock time and mark queue, cache, and model-load timing unavailable when they were not measured.

See `docs/README/TOOL_RECOVERY.md` and `docs/README/VAULT_TARGET.md`.

## Chat web research convergence (2026-11-03)

Provider context-limit recovery is committed after the answering stream settles (diagnostics optional, including incognito): canonical history is compacted with the learned ceiling while `_protected` markers and tool pairs are preserved. Strata `/health` (and llama.cpp `/slots`/`/props`) can raise or lower a remembered rejection after reload. A turn-local visit ledger tracks partial/focused/complete fetches per URL, including mixed batches. Soft synthesis is advisory; stall and absolute search/fetch/hard-round caps force convergence.

See `docs/README/WEB_VISIT_LEDGER.md`, `docs/README/CHAT_RESEARCH_BUDGET.md`, `docs/README/CONTEXT_RECOVERY.md`, `docs/README/LLM_CORE.md`, and `docs/README/MODEL_CONTEXT.md`.

## Parallel web + vault research (2026-11-03)

`research_parallel` concurrently runs authorized `web_search` plus a vault branch. Default `vault_strategy=fast` uses indexed `vault_knowledge_search` (candidate matches, no Dottie LLM). `vault_strategy=deep` keeps bounded `dispatch_dottie` verified research. Direct tools remain available. When only the parent is discovery-selected, nested children still require individual request-authority grants and are temporarily turn-selected only if already in the request catalog inventory. Chat and agent modes can reach both `dispatch_dottie` and `research_parallel` when workspace/vault scope is present. Deep mode falls back to sequential execution when research shares the foreground endpoint; fast mode does not.

See `docs/README/PARALLEL_RESEARCH.md`.

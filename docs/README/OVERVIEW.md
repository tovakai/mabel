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

## Chat tool-action fold (2026-11-03)

Agent tool rows collapse behind a single “N actions” summary by default.
Click to expand the full thread; the live running tool (and private-browser
previews) stay visible while work is in progress.

See `docs/README/CHAT_RENDERER.md`.

## Tool approval gate — pure reads (2026-11-03)

Known pure-read effects (`READ_PUBLIC` / `READ_WORKSPACE` / `READ_PRIVATE` /
`BROKERED_NETWORK_READ` / `USER_INTERACTION`) never trip the
external-untrusted-context approval card. That covers notes search, memory
list, email/vault reads, `web_search` / `web_fetch`, and workspace readers.
Shell, writers, and other egress / mutation effects still require approval
once that gate is armed. Owner interactive authority also seals personal
store reads (notes/memory/calendar/email/vault) so those skip gap cards;
ungranted MCP/shell/writes still ask.

The configured vault (Core of Truth via `vault_target.json`) is the default
active workspace unless the user clears it (opt-out). That seals
`workspace_read` grants so `read_file` / vault readers do not ask for
approval. Interactive turns also seal `search_browser` when web is enabled so
`web_fetch` stays free after taint. Simple public “what is / about” questions
offer a lean `web_search` (+ `web_fetch`) surface instead of vault+parallel
fan-out.

Vault/genre “rewrite that note” turns seal to `write_file`/`edit_file` (not
Documents/`bash`/`vault_batch_edit`/web). Exact rewrite seals ignore prior-turn
warm tools and keep writers even under default-vault `workspace_read`. WebUI
agent turns always get one degenerate-stream retry after tool results
(previously terminal-agent only), so a post-approval repetition loop can
recover instead of hard-stopping with HTTP 502.

See `docs/README/TOOL_CAPABILITIES.md`, `docs/README/VAULT_TARGET.md`,
`docs/README/TEST_VAULT_NOTE_REWRITE_ROUTING.md`.

## Parallel web + vault research (2026-11-03)

`research_parallel` concurrently runs authorized `web_search` plus a vault branch. Default `vault_strategy=fast` uses indexed `vault_knowledge_search` (candidate matches, no Dottie LLM). `vault_strategy=deep` keeps bounded `dispatch_dottie` verified research. Direct tools remain available. When only the parent is discovery-selected, nested children still require individual request-authority grants and are temporarily turn-selected only if already in the request catalog inventory. Chat and agent modes can reach both `dispatch_dottie` and `research_parallel` when workspace/vault scope is present. Empty-capability / compact web turns now offer `research_parallel` on the model-facing surface (not merely in executable inventory). Lexical conversational grounding distinguishes casual reactions (no forced research, no invented plot/genre, spoiler restraint to expressed progress) from factual public lookup, vault-only notes questions, and notes+online parallel compares. “My notes” compare/vault phrasing no longer collapses the offered surface to personal `manage_notes` (including after tool-approval resume). Successful-but-empty web searches are package status `empty` with zero substantive sources, not grounded `complete` evidence. Deep mode falls back to sequential execution when research shares the foreground endpoint; fast mode does not.

See `docs/README/PARALLEL_RESEARCH.md` and `docs/README/TEST_CONVERSATIONAL_RESEARCH_ROUTING.md`.

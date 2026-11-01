# Obsidian knowledge engine

For opt-in **continuous Arch indexing + remote Dottie BGE-M3 embeddings**, see
[the systemd installation runbook](../operations/dottie-knowledge-indexing.md).
The source index stays local on Arch, while only embedding API requests are
sent to Dottie's separately configured server. The watch service and embedding
timer are independent; no daemon is implicitly started by Mabel itself.

## Repository integration inspection

The working tree contains agent-reliability and concurrent multi-device filesystem
work. Both are preserved. Phase 1 is verified before semantic/research integration.

Reusable integration points:
- app.py / chat_routes: FastAPI, sessions, providers and interactive review.
- agent_tools, tool_types, tool_schemas, tool_capabilities, tool_index: native tools.
- authority, resource_binding, remote_resources, effect_adapters: request grants,
  sealed resources, backend identity and receipts. Retrieval grants no permission.
- audit_receipts, task_artifacts, completion, tool_recovery: bounded saved evidence,
  pagination, grounded reports and failure recovery.
- workspace_discovery: conventional control/history exclusions, without blanket
  exclusion of every unusual or dot-prefixed user directory.
- mabel_vault_rag, rag_vector, rag_manager, personal_docs: existing generic Chroma
  and keyword search/private-note sync; retained. They lack an Obsidian explicit
  graph and current-file source-line citations.
- embeddings, embedding_lanes, embedding_routes: HTTP/local FastEmbed, encrypted
  endpoint configuration and separate compatibility lanes.
- settings, prefs_routes, endpoint_resolver, model_routes, settings.js: persisted
  assignments, configured providers, credentials, discovery and model pickers.
  Chat, Utility, Deep Research, Tasks, Teacher, Vision, Image and TTS/STT exist.
  Memory/summarization use Utility; they should not acquire duplicate roles.
- foreground_model_routing, llm_core, task_endpoint, utility_effort: explicit
  foreground consent, streaming commit boundary, fallback and reasoning options.
- resources/skills: published YAML-frontmatter SKILL.md procedures.

vault_search already accesses the password vault. The knowledge tool is named
vault_knowledge_search to preserve the credential tool's existing semantics.
Indexes must remain outside the source vault and detected Syncthing roots.
The concurrent agent owns src/fs_devices; this engine initially indexes local
Markdown through existing sealed workspace access.

## Implemented phases

### 1. Deterministic index (verified before Phase 2)

`src/knowledge_engine/markdown.py` parses bounded YAML nodes without constructing
objects, extracts explicit relationships, and creates heading-aware chunks.
`index.py` owns schema migrations, transactions, FTS5 Unicode61, typed property
queries, canonical paths, stable note/chunk identifiers, source hashes and graph
resolution. Existing frontmatter `id`/`uid`, filesystem identity, and unique content
matches assist rename reconciliation. Ambiguous identity is reindexed, not guessed.

The database contains notes, chunks, properties, explicit edges, FTS content,
index metadata, embedding spaces and vector entries. IDs are never inserted into
Markdown. Broken/ambiguous relationships remain labeled. Heading fragments are
preserved in the original link; resolution currently establishes the target note,
not the existence of its heading or block anchor.

The portable polling observer coalesces file events and periodically reconciles
content hashes, recovering changes missed by timestamps. No daemon starts with
Mabel. Full reconciliation reads at most 50,000 notes, 2 MiB per note and a
64 MiB source snapshot; larger vaults need partitioned roots or a future streaming
reconciliation implementation. Large individual source lines can exceed chunk
size targets; excerpts and current-source reads remain separately bounded.

Default exclusions reuse workspace discovery's conventional control/history
rules. `.stversions`, `.obsidian`, `.git` and `node_modules` are excluded;
legitimate dot-prefixed content is retained. Explicit `--include` and `--exclude`
paths are maintenance-operator configuration, not model-controlled permission.
Symlinks are not followed during discovery. Sealed resources prevent path/root
replacement and cache-backed symlink escape. Renaming/replacing the vault root
requires a deliberate cache rebuild.

Graph scores start with frontmatter 3, inline links 2, embeds 1, divided by
`log1p(in_degree)` (with a safe minimum degree). Search supplements at most five
neighbors and reports omissions. This is a relevance heuristic, not an inferred
ontology or a guarantee that every neighbor answers the question.

### 2. Dense retrieval and scoring

`semantic.py` uses real sqlite-vec cosine distance over float32 BLOBs in the same
SQLite database. It deliberately uses sqlite-vec scalar distance rather than a
`vec0` table: ordinary foreign keys and transactions keep deletions consistent,
while Phase 1 works without loading an extension. Dense candidate scans are
bounded to 50,000 eligible vectors. This exact-scan implementation favors
correctness and small personal vaults over approximate-index scalability.
Extension loading is enabled only while loading sqlite-vec and then disabled.

Semantic queries use only a matching model name, explicit space identity,
revision, dimensions and L2 normalization. Equal dimensions alone never qualify.
A different fallback needs its own explicitly built namespace; otherwise search
falls back to lexical retrieval. Query failover never rebuilds/replaces vectors.
Only changed chunks are embedded during explicit maintenance; deletions cascade.
Coverage counts only vectors matching current chunk hashes.

Hybrid search uses Reciprocal Rank Fusion with k=60, retaining original chunk IDs
and source lines. Optional reranking scores up to 15 candidates through an
OpenAI-compatible `/v1/rerank` response or a TEI-style `/rerank` response.
Rank order is normalized; scores from different models are not compared.
Failed semantic services degrade to lexical results; failed rerankers preserve
fused ordering. Service state, source availability, truncation and index revision
are returned explicitly. Search results are snapshots; use `vault_read_range`
before relying on an exact quotation or measurement.

### 3. Optional Dottie research

`research.py` runs the existing native `stream_agent_loop` with a narrowed child
RequestAuthority, TurnContract and read-only tool policy. No independent agent
framework or model registry is introduced. The executor implements concurrent
independent reads with `asyncio.gather` and a semaphore; instructions alone do not
provide concurrency. Dottie gets search, graph, range and bounded batch reads,
never shell, direct filesystem writes, delegation recursion or approval grants.

Default budget: 24 attempted dispatcher calls (six reserved for verification),
three concurrent reads, two graph hops, 32 chunks/edges, 24 KB tool output and
90 seconds. Duplicate calls, denied calls and nested batch attempts consume the
same budget. Model inference failover preserves these counters and completed
reads. One bounded inference-only synthesis can salvage evidence after malformed
output or a provider failure; it does not restart the workflow.

Each accepted citation is reread through the production authorization path and
checked against its current source path, line range and exact quote. Numeric
contradictions are rejected. Full semantic entailment remains Mabel's/model's
responsibility; a valid quote does not mathematically prove arbitrary prose.
Unverified claims become unresolved. Findings distinguish source-supported claims
from inferences and are published into the existing session-owned task-artifact
registry for follow-ups. Source-tool policy restrictions also apply to retrieval
of saved findings. Saved artifacts are labeled snapshots; they do not promise
that a file stayed unchanged after completion.

With no compatible research worker, dispatch returns a degraded result containing
authorized local retrieval. Mabel can continue using the same local tools.
Dottie's Windows PC is never a startup dependency. Indexing initially supports
local filesystem resources only: each computer should index its synchronized
local copy. Remote/SFTP directory resources are explicitly rejected by these
indexing tools, without affecting the separate remote filesystem tools.

## Tools and permission integration

Six tools are registered through the existing schemas, tool index, capabilities,
TurnContract, resource binding and native dispatcher:

- `vault_knowledge_search`: lexical, metadata_only, semantic or hybrid retrieval;
  exact typed properties and directory prefix, bounded pages.
- `vault_traverse_graph`: incoming/outgoing explicit relationships, source lines,
  ambiguity, filtered pages and omitted counts.
- `vault_read_range`: current authorized Markdown, at most 200 lines / 16,000
  characters, current hash and stale-index flag.
- `vault_index_status`: local cache health and optional read-only reconciliation.
- `vault_retrieve_batch`: one to six independent authorized reads.
- `dispatch_dottie`: optional configured research worker and grounded findings.

All remain subject to the existing administrator/native-filesystem access policy,
explicit tool denials and sealed workspace/directory scope. Advertising them does
not grant permission. A narrowed child indexes only its narrowed root, never a
broader parent's cached text. No tool accepts SQL or a model-selected cache path.
These tools write only controlled local cache data; vault mutations continue to
use Mabel's existing approval/mutation pipeline. Use `vault_audit` for exhaustive
frontmatter counts; search pages are not collection-coverage receipts.

## Shared model-role routing and Settings

`model_roles.py`, `model_role_runtime.py` and `model_role_adapters.py` extend the
existing endpoint registry, credentials, adapters and legacy primary settings.
The added per-user `model_role_policies` object contains only adapter metadata,
optional fallback and bounded policy; there is no second endpoint registry.
Credentials remain in existing encrypted/provider configuration.

Supported roles: Primary assistant, Utility (including memory and summaries),
Scheduled tasks, Research/Dottie, Teacher, Vision, Image generation, TTS, STT,
Knowledge embeddings and Knowledge reranking. Existing assignments are preserved.
Legacy Utility/Vision fallback lists are read until an explicit role fallback
selection supersedes them. No fallback is required, and None explicitly disables
it. Anonymous/background routing cannot borrow named users' fallback consent.

In **Settings → AI**, existing role cards retain their original primary pickers.
Expand **Model routing and optional fallback** beneath a role. New roles receive
primary endpoint/model pickers using the same configured endpoint catalog.

1. Keep the existing primary assistant selected; opening Settings changes nothing.
2. Select optional fallback endpoint and model independently for each role.
3. Declare adapter and actual capabilities. Chat uses `existing`; native research
   requires `chat,tools` and suitable native provider/tool support. Endpoints
   explicitly marked as not supporting tools cannot serve the worker.
4. For Embedding choose `openai_embeddings`, `ollama_embed`, or cached-only
   `fastembed_cached`. Declare model space, operator-verified revision and dimension
   (BGE-M3 dense: 1024). Primary and fallback have separate identity fields.
5. Reranking requires `rerank` or `tei_rerank` and `reranking` capability; a chat
   endpoint is not assumed to implement scoring. Image/audio roles use their
   typed adapters, not chat completions.
6. Set timeout (1–300 seconds), retries (0–2), recovery/circuit interval (1–600
   seconds), and Research limits as needed, then **Save routing**.
7. Status reports the model that last handled a request and active failover.
   Opening this panel does not load weights or promise endpoint health.

The dedicated role Settings endpoint requires an interactive human request. Invalid
capability/adapter assignments are rejected. Missing/removed endpoints yield
unavailable/degraded results rather than preventing startup. Existing selected
conversation and explicit task routes remain primary; configured defaults do not
replace a session's selected model.

Routing retries only classified transport/timeouts, selected recoverable HTTP
statuses and adapter capability failures. HTTP 400/401/403/422, policy denials,
invalid tool arguments and ordinary programming exceptions do not trigger model
failover. Circuit breakers suppress repeated offline attempts and later probe the
primary again. There is at most one fallback, with no recursive chains.
Streaming keeps the existing commitment boundary: after text/native calls have
been emitted, that generation does not switch models or replay tool actions.
A subsequent inference request may fail over with its completed action history.
Streaming retries are limited to one pre-content retry even when sync/nonstream
policy permits two. Synchronous network adapters also set request timeouts;
Python cannot forcibly interrupt arbitrary third-party CPU/library work in a
worker thread.

Existing generic Chroma memory/RAG embedding lanes are retained with their
existing settings and compatibility mechanism. The new strict role namespaces
apply to the knowledge engine; this change does not silently migrate or rebuild
legacy memory/RAG vectors. Existing custom image-editing backend workflows retain
their specialized execution paths; the shared typed image router is integrated
at image-generation inference before gallery writes.

## Arch setup and operation

Run from the existing checkout/venv; no GPU, model server or Windows access is
needed for the first commands. Install normal project dependencies using the
project's existing setup. The added small vector dependency is:

```bash
.venv/bin/pip install 'sqlite-vec>=0.1.6,<0.2'
```

Choose the actual local vault path only when ready for read-only indexing. The
following are examples for the operator to run, not commands executed against
any real vault during development:

```bash
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME scan
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME status
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME search 'mått tolerans'
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME search --mode metadata_only --filters '{"owned":true}' --path-prefix Library/Movies
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME graph 'Projects/Example.md'
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME read 'Projects/Example.md' --start 10 --end 35
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME watch
```

Default cache is `$XDG_CACHE_HOME/mabel/knowledge/<owner-root-hash>/vault_index.db`
or `~/.cache/mabel/knowledge/...`. It must not be inside a Syncthing root, Git
checkout or vault. A Syncthing `.stfolder` marker is detected; the operator must
also avoid custom synchronized paths lacking that marker. Each root and owner
gets a separate database. Use the same authorized root and owner in CLI and chat
to share its index. A subdirectory-scoped tool call intentionally gets a separate
index, preventing broader cached disclosure.

Global maintenance options: `--cache-dir`, repeatable `--exclude`/`--include`,
`--minimum-tokens` (150), `--maximum-tokens` (800), repeatable
`--relationship-key`, and `--edge-weights '{"property":3,"inline":2,"embed":1}'`.
Token targets are a character-based approximation, not provider token accounting.
Without a watcher or explicit refresh, search reports the last scan and may be
stale. Watch defaults to 0.5-second polling and 60-second full reconciliation;
configure `watch --interval 1 --reconcile-seconds 120` as appropriate.

After configuring an embedding role in Settings:

```bash
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME scan --embed --embedding-limit 1000
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME embedding-coverage
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME search 'later lid fit tests' --mode hybrid --rerank
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME watch --embed --embedding-limit 1000
```

Repeat bounded `scan --embed` until current coverage is complete. A cached-only
CPU FastEmbed fallback uses only weights already provisioned through existing
embedding settings; it never downloads weights during retrieval. Its different
space requires its own prebuilt vectors. Explicit vector invalidation is:

```bash
.venv/bin/python -m src.knowledge_engine --vault '/path/to/Core of Truth' --owner YOUR_MABEL_USERNAME rebuild-vectors EXACT_SPACE_HASH_FROM_COVERAGE
```

Then run `scan --embed`. To rebuild the whole disposable index after corruption,
unsupported schema or root replacement, stop that root's watcher/Mabel operations,
move its **exact hashed cache subdirectory** to an out-of-sync backup, and run
`scan` again. Do not move or delete vault files. Never sync database/WAL/SHM files.

## Windows and model-server validation (operator procedures, not executed here)

Set up the existing Mabel Python environment on Windows and install requirements
using its normal instructions. Each computer keeps its own source copy via
Syncthing and its own local database. PowerShell equivalents:

```powershell
.\.venv\Scripts\python.exe -m src.knowledge_engine --vault 'D:\Core of Truth' --owner YOUR_MABEL_USERNAME scan
.\.venv\Scripts\python.exe -m src.knowledge_engine --vault 'D:\Core of Truth' --owner YOUR_MABEL_USERNAME status
.\.venv\Scripts\python.exe -m src.knowledge_engine --vault 'D:\Core of Truth' --owner YOUR_MABEL_USERNAME watch
```

Cache defaults to `%LOCALAPPDATA%\Mabel\knowledge`. Lexical, graph and metadata
operations require neither a GPU nor an inference service.

For optional inference, use an existing compatible server or an official
[llama.cpp Vulkan build](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md).
Obtain appropriate model files separately with user authorization. Verify model
license/access, server model-format support, Vulkan device enumeration and native
tool templates before assigning a role. Do not assume GGUF conversion support or
that a model name alone guarantees an API.

Example commands assume compatible **already provisioned local GGUFs**, and an
up-to-date server supporting the specified model and flags:

```powershell
.\llama-server.exe --list-devices
.\llama-server.exe -m 'D:\Models\gemma-4-E2B-it-Q4_K_M.gguf' --alias dottie-research --ctx-size 16384 --n-gpu-layers 99 --jinja --host 127.0.0.1 --port 8081
# Run separately; choose resident processes only after measuring memory.
.\llama-server.exe -m 'D:\Models\bge-m3.gguf' --alias bge-m3 --embedding --pooling cls --ctx-size 8192 --n-gpu-layers 99 --host 127.0.0.1 --port 8082
.\llama-server.exe -m 'D:\Models\qwen3-reranker-0.6b.gguf' --alias qwen3-reranker --rerank --pooling rank --ctx-size 4096 --n-gpu-layers 99 --host 127.0.0.1 --port 8083
```

These commands are **hardware-validation starting points**, not a tested RX 5700
XT recipe. Check each file's required pooling/template configuration and server
support; a zero/constant-score reranker or wrong embedding dimension is not a
working integration. `--rerank` is not a dense-embedding option. Use the existing
Ollama provider for research if it actually supports that model/native tools;
its `/api/embed` endpoint is supported for compatible embedding models. Reranking
requires a scoring service and is not assumed to be supported by Ollama chat.

Register endpoints through existing model/provider settings; configure loopback
or appropriately authenticated private network access. Do not expose unauthenticated
servers publicly. Mabel on Arch must be able to reach any Windows endpoint it
uses; register its actual private address there, not Windows loopback. Configure
Research primary `dottie-research`, optional compatible fallback on another
existing endpoint, embedding space/revision/dimensions, and optional reranker.
No example assignment is installed automatically. Keep Gyro-S selected for Mabel.

Use server-side loading/offloading controls and conservative context/concurrency.
If all three models do not fit simultaneously, run fewer services, configure the
CPU cached embedding fallback, disable reranking, or use another configured
endpoint. This implementation does not launch, kill or swap model processes.
Research budgets and offline circuit breakers limit client pressure, but are not
a VRAM scheduler. No combined-memory fit or Vulkan support is claimed.

Primary references: [Gemma 4 E2B IT](https://huggingface.co/google/gemma-4-E2B-it),
[BGE-M3](https://huggingface.co/BAAI/bge-m3),
[Qwen3 Reranker](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B),
[llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md),
[reranking server examples](https://github.com/ggml-org/llama-pages/blob/master/src/docs/serve.md),
[Ollama API schema](https://github.com/ollama/ollama/blob/main/docs/openapi.yaml),
[sqlite-vec Python integration](https://alexgarcia.xyz/sqlite-vec/python.html).

## Diagnostics and benchmarks

```bash
.venv/bin/python -m src.knowledge_engine --vault /path/to/disposable-or-authorized-vault --owner YOUR_MABEL_USERNAME benchmark
.venv/bin/python -m src.knowledge_engine.benchmark --owner YOUR_MABEL_USERNAME --status
.venv/bin/python -m src.knowledge_engine.benchmark --owner YOUR_MABEL_USERNAME --role research --runs 3
.venv/bin/python -m src.knowledge_engine.benchmark --owner YOUR_MABEL_USERNAME --role embedding --runs 3
.venv/bin/python -m src.knowledge_engine.benchmark --owner YOUR_MABEL_USERNAME --role reranking --runs 3
.venv/bin/python -m src.knowledge_engine.benchmark --owner YOUR_MABEL_USERNAME --stack --runs 3
```

Role benchmarks send synthetic text only; they never install/start models.
Record first-request and warm timings, batch throughput and actual-model routing.
The first request is not proof of a cold load. Stop/restart the server yourself
before a cold benchmark and record its load times. Stack mode issues concurrent
synthetic research/embedding/reranking requests; compare isolated vs simultaneous
results and server logs. The reranker toy top-1 metric is a smoke test, not a
retrieval-relevance benchmark; evaluate a labeled set of real questions separately.
Measure device memory with available Vulkan/AMD driver/server diagnostics on the
Windows machine (and `nvidia-smi` on NVIDIA), recording idle, loaded and concurrent
peaks including KV cache. GPU memory and hardware latency remain unmeasured here.

In-process diagnostics are owner-scoped and contain model/endpoint identifiers,
failover reason classes and circuit state, not credentials or full prompts.
CLI diagnostics start a new process: previous Mabel process health is visible in
Settings, not magically shared with a separate CLI process.

## Regression commands and delivery status

From the repository root:

```bash
.venv/bin/python -m pytest -q tests/test_knowledge_index.py tests/test_knowledge_semantic.py tests/test_knowledge_research.py tests/test_model_roles.py tests/test_model_role_runtime.py tests/test_model_role_routes.py
node --test tests/modelRoles.test.cjs
.venv/bin/python -m pytest -q tests/test_audit_followups.py tests/test_audit_scope_receipts.py tests/test_structured_audit_completion.py tests/test_workspace_inspection_runtime.py tests/test_authority_gap_approval.py tests/test_interactive_authorization_runtime.py
```

Fixtures are disposable; no real Obsidian vault was accessed. Real sqlite-vec
vector persistence/search is tested, with deterministic fake inference providers.
The native research loop, child dispatcher/authority, source rereads, concurrency,
malformed citations, injection, offline fallback and synthesis recovery are
exercised. Real Gemma/BGE/reranker inference, GPU latency/memory and Windows Vulkan
remain operator validation. No multi-gigabyte weights or remote Windows changes
were made.

### Recorded validation — 2026-10-28

- Phase 1 checkpoint before proceeding: 19 new index tests and 132 focused
  existing/retrieval regressions passed.
- Broader affected integration run: **1,023 passed, 2 warnings** in 207.27 s.
  Includes legacy embedding/RAG, foreground/task routing, audio, audit continuity,
  structured completion, tool policy, exact/task approval and concurrent directory
  access tests. This is an affected-suite selection, not the entire repository.
- Final focused run after source-boundary/migration/CLI checks: **81 passed,
  1 warning** in 17.20 s. This includes both actual ordinary and compact runtime
  knowledge searches, a 223-note mixed-property fixture, true SQLite vector
  queries, endpoint ownership/capability validation, source symlink replacement,
  complete CLI subprocess workflows and research evidence recovery.
- Additional Teacher/audit-follow-up selection: **126 passed, 1 warning**.
  Counts overlap; they should not be added together.
- Headless Chromium Settings regression: **1 passed**. Existing primary choice
  survived fallback save and disable. JS syntax, Python compilation and git diff
  whitespace checks passed. The new SKILL.md loaded through Mabel's Skill parser.
- Independent CLI check indexed 223 disposable active notes while excluding one
  `.stversions` copy. An unchanged reconciliation took **1.448 s**; the benchmark's
  no-hit lexical query took **0.00336 s**. Peak process RSS was 95,988 Linux KiB.
  These are development-container measurements, not relevance or GPU benchmarks.
- Direct restricted-shell asyncio CLI checks initially stalled because worker
  wake-up sockets were unavailable. The same CLI search completed under normal
  test execution permissions; no runtime containment setting was changed.

The first broad run exposed seven tests modeling transport outages as generic
RuntimeError. Their fake failures now use httpx.ConnectError, matching the new
classified recovery contract; programming errors still do not trigger failover.
A stale policy test now checks the actual word “forbidden” while retaining its
blocked-execution assertion. The concurrent directory-grant architecture also
required an explicit isolated directory grant in the HTTP audit-follow-up fixture;
a posted workspace string alone correctly grants nothing.

All existing uncommitted reliability/device work was preserved. Changes remain
uncommitted on fix/reliable-agent-tool-runtime; no mixed-worktree commit was made.
The historical exhaustive compact-preview fixture's missing vault_batch_edit
sample is outside this affected selection and remains a known earlier baseline
limitation. Real models, remote endpoints, Windows execution, RX 5700 XT Vulkan,
combined GPU residency, cold-load latency and a live Gyro-S/Dottie conversation
were **not tested**. The supplied commands are for that next validation step.

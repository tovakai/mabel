# Continuous Obsidian indexing with Dottie

The source vault and rebuildable SQLite/FTS5/graph index live on the Arch
machine running Mabel. Dottie's **BGE-M3** serves remote dense embeddings.
**Qwen3-Reranker** scores search candidates at query time. **Gemma** remains
an independent research worker. No model weights, Markdown or live SQLite
database need to be synchronized between machines.

This setup has **two independent user-systemd jobs**:
- `mabel-knowledge-index.service`: keeps local Markdown/FTS5/graph current,
  reconciles source hashes, writes ONLY the disposable SQLite cache.
- `mabel-knowledge-embed.timer` + `.service`: every ~2 minutes, submits
  missing/stale chunks to the owner-configured remote embedding endpoint.
  It never rescans Markdown, never runs FastEmbed locally, never starts Dottie
  or writes a Markdown note. An offline Dottie affects only vector coverage;
  lexical/metadata/graph search remains available.

## Before installing

1. Verify Dottie's BGE-M3 server is reachable **from the Arch host**.
   The model's configured API must implement OpenAI-compatible `/v1/embeddings`
   (or Ollama `/api/embed` when configured as `ollama_embed`).
   Do **not** assume `127.0.0.1:8082` on Arch points to Dottie.
2. In **Mabel Settings → AI → Knowledge embeddings**, select the Dottie
   endpoint/model (normally `bge-m3`, served from Dottie's port 8082).
   Adapter: `openai_embeddings` for an OpenAI-compatible server.
   Capability: `embedding`. Declare **1024 dimensions**, stable
   `space_id`, exact model `revision`, and `l2` normalization.
   Example space ID: `BAAI/bge-m3`. The revision must identify the actual
   served model/weights, not a guess. Do not configure a CPU FastEmbed fallback.
   Saved role settings are **owner-scoped**, so the service must use the same
   logged-in Mabel owner as the chat that searches the index.
3. In **Knowledge reranking**, separately select Dottie's Qwen3-Reranker
   endpoint (normally port 8083), `rerank` or `tei_rerank` adapter based
   on its real API, capability `reranking`. This runs on searches that
   request `use_reranking=true`; it is **not an indexing step**.
   Gemma (normally port 8081) remains the **Research / Dottie** role.
4. Confirm the local vault root is the same exact path Mabel searches
   and the account running this service has read access. This maintenance
   daemon does not grant chat tools filesystem permissions. The existing
   active-directory and interactive mutation approvals are unchanged.
5. The indexer has existing limits of 50,000 notes, 2 MiB per note and
   64 MiB of source text per full reconciliation. If a scan exceeds them,
   adjust root partitioning/limits in a separate reviewed change; do not
   silently index a broader directory or erase existing caches.

## Install on Arch

After applying the reviewed branch to the **production** checkout and
installing its Python dependencies, run as the normal Linux user:

```bash
cd ~/Documents/mabel

# The owner below is an EXAMPLE. Use the exact account owner whose role
# you configured in Mabel, not necessarily your Linux username.
OWNER=tovakai

.venv-arch/bin/python scripts/install_knowledge_index_services.py \
  --checkout "$PWD" \
  --python "$PWD/.venv-arch/bin/python" \
  --vault "$HOME/Documents/core of truth" \
  --owner "$OWNER"

# Review the rendered units, then install them. Does NOT start processes.
.venv-arch/bin/python scripts/install_knowledge_index_services.py \
  --checkout "$PWD" \
  --python "$PWD/.venv-arch/bin/python" \
  --vault "$HOME/Documents/core of truth" \
  --owner "$OWNER" --install

systemctl --user daemon-reload
systemctl --user enable --now mabel-knowledge-index.service mabel-knowledge-embed.timer
```

The installer puts three unit files under `~/.config/systemd/user/`
and a private configuration file under `~/.config/mabel/`. It refuses to
overwrite existing units unless `--replace` is explicit, in which case it
creates backup copies. It does not run `systemctl` itself.

The units start on user login. To keep them running while logged out,
enable user lingering deliberately if desired:
`sudo loginctl enable-linger "$USER"`.

## Confirm

```bash
systemctl --user status mabel-knowledge-index.service mabel-knowledge-embed.timer
journalctl --user -u mabel-knowledge-index.service -n 40 --no-pager
journalctl --user -u mabel-knowledge-embed.service -n 40 --no-pager

cd ~/Documents/mabel
.venv-arch/bin/python -m src.knowledge_engine \
  --vault "$HOME/Documents/core of truth" --owner "$OWNER" status
.venv-arch/bin/python -m src.knowledge_engine \
  --vault "$HOME/Documents/core of truth" --owner "$OWNER" embedding-coverage
```

Expect `last_scan` to advance and note/chunk/edge counts to reflect the
vault. BGE-M3 coverage increases in bounded batches while Dottie is online.
A missing embedding role or unreachable endpoint causes only the embedding
oneshot to fail; its timer retries later. Status outputs disclose counts,
not account secrets.

For an immediate manual batch once initial scan exists:

```bash
.venv-arch/bin/python -m src.knowledge_engine \
  --vault "$HOME/Documents/core of truth" --owner "$OWNER" embed \
  --embedding-limit 256 --batch-size 8
```

For a retrieval check:

```bash
.venv-arch/bin/python -m src.knowledge_engine \
  --vault "$HOME/Documents/core of truth" --owner "$OWNER" \
  search 'Curiosity Drive' --mode hybrid --rerank
```

A degraded semantic or reranker service is reported in the result while
lexical search remains usable. This test reads, not edits, the vault.

## Stop / troubleshooting

```bash
systemctl --user stop mabel-knowledge-index.service mabel-knowledge-embed.timer
systemctl --user disable mabel-knowledge-index.service mabel-knowledge-embed.timer
```

Do not put the `vault_index.db` SQLite file or its WAL/SHM sidecars in
Syncthing. If the owner or root differs from Mabel's chat requests, you
will see separate per-owner/per-root caches. Reindexing never gives the
worker additional grants or writes into Core of Truth.

The older `MABEL_VAULT_RAG_ENABLED` Chroma poller is a separate opt-in
feature. Enabling it is not required for this system and would duplicate
some indexing work.

## Regression tests

```bash
.venv/bin/python -m pytest -q --tb=short \
  tests/test_knowledge_maintenance.py \
  tests/test_knowledge_service_installer.py \
  tests/test_knowledge_index.py \
  tests/test_knowledge_semantic.py
```

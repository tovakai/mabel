# Mabel vault: one directory, three kinds of state

**Chosen layout:** the **repository root itself** is an Obsidian vault called
`mabel-vault`. No nested `vault/` subdirectory is needed. Each participating
device has its own checkout at a path such as `~/Documents/mabel-vault`.

The point is physical co-location, not forcing every file through the same
replication mechanism:

- **Git:** Mabel / Odysseus application code, scripts, tests and public docs.
- **Syncthing:** private Markdown notes and attachments in six allowlisted
  directories within that same root.
- **Local runtime:** each machine's `data/`, `logs/`, indexes, environments,
  caches and Obsidian plugin settings. Chat history stored in a SQLite
  database is **not** shared by copying that live database.

## Directory map

```text
mabel-vault/                 <- Git checkout + Obsidian vault + Syncthing root
├── .git/                    local Git history (never Syncthing)
├── .stignore                tracked by Git, NOT replicated by Syncthing
├── .gitignore               tracked by Git
├── .dockerignore            tracked by Git
├── .obsidian/               private, device-local Obsidian settings/plugins
├── memory/                  Syncthing; not committed to public Git
├── identity/                Syncthing; not committed to public Git
├── knowledge/               Syncthing; not committed to public Git
├── projects/                Syncthing; not committed to public Git
├── journal/                 Syncthing; not committed to public Git
├── attachments/             Syncthing; not committed to public Git
├── src/                     Git-managed app source
├── routes/                  Git-managed app source
├── static/                  Git-managed app assets
├── data/                    device-local database and persistent app state
├── logs/                    device-local runtime logs
└── ...
```

The six note/attachment directories exist in Git as empty `.gitkeep`
scaffolds, without any private content. Normal notes you create under these
directories are ignored by Git. `.obsidian/` is entirely ignored by Git.

**Important:** a `.gitignore` is not an access-control mechanism. Git will
still publish files already tracked, or files deliberately forced in with
`git add -f`. Keep private notes out of commits and review `git status` and
the diff before pushing to the public repository.

## Initial setup on the first device

1. Clone Mabel into the desired vault root and check out the branch carrying
   the vault layout (until its PR lands on `dev`):

   ```sh
   git clone https://github.com/tovakai/mabel.git mabel-vault
   cd mabel-vault
   git switch feat/obsidian-companion-foundation
   ```

2. Open **this exact `mabel-vault` directory** as a vault in Obsidian.
   Obsidian creates `.obsidian/` itself. You do not need to move or modify
   your existing personal vault.
3. Add the six private directories to Mabel's knowledge workflow and create
   a few test notes. They are real vault folders; **no auto-indexing into
   Odysseus RAG is implemented by this directory layout**.
4. Before enabling Syncthing, verify the checkout has `.stignore`. In
   Syncthing create a folder whose path is the **same checkout root**.
   Examine **Edit > Ignore Patterns**: it should include only the six
   allowlisted directories, with a final `*` wildcard.
5. Set up **File Versioning** in Syncthing for recovery from accidental note
   edits/deletions. Start on a test folder before trusting important content.

## Setup on another device

1. **Clone Git independently** into its own `mabel-vault` directory. Do not
   transfer another computer's `.git` through Syncthing. Check out the
   same branch and inspect `.stignore` locally before pairing the folder.
2. In Syncthing, accept/add the shared folder but set its **local path**
   to that checkout root. Confirm the ignore patterns on **each** device.
   Syncthing explicitly never transfers `.stignore` itself. The Git
   checkout provides a copy; subsequent rule updates need a Git pull or a
   manual edit on every device.
3. On the second device, open the root directory as an Obsidian vault.
   Plugin installations and per-device credentials are configured locally
   because `.obsidian/` is deliberately **not** part of the Syncthing set.
4. Verify a disposable note saved under `memory/` reaches both devices.
   Also check that editing `src/` on one computer **does not** synchronize
   source files: use `git pull` for application updates.
5. Confirm there are no unexpected sync files in `data/`, `logs/`,
   `.git/`, `.obsidian/`, `venv/`, or `node_modules/`.

This intentionally allows the *entire package* to exist in the same filesystem
root on every computer while keeping source versioning, note synchronization
and live app state independent.

## Why `.stignore` is an allowlist

Unlike a denylist that continually needs new exceptions as Odysseus adds
features, this setup only syncs:

```text
memory/     identity/     knowledge/
projects/   journal/      attachments/
```

New root-level files, source directories, database files, model caches,
environment secrets and `.obsidian/` never replicate through Syncthing.
If you introduce a new note directory, **add it to both** `.gitignore` and
`.stignore`, and check `.dockerignore`, before putting private content
inside. Do not put sensitive notes in `README.md`, `docs/` or `src/`:
those paths belong to the public application code.

The ignore patterns use rooted Syncthing `!/` includes **before** a final
`*`. Syncthing uses **first matching rule wins**; the directory and its
recursive content must both be included. The protected database/credential
patterns above the includes win even if someone places a database inside
`memory/`.

See the [official Syncthing ignore syntax](https://docs.syncthing.net/users/ignoring.html)
for exact behavior. `.stignore` is *not synchronized by Syncthing*.

## Prevent private data from leaking into Docker builds

`.dockerignore` also excludes all six vault directories and `.obsidian/`.
This matters even if source code is the only thing the Dockerfile currently
needs: the build context must not contain personal memories or Obsidian tokens.

Keep API keys and other secrets in local, ignored configuration. Do not
publish your vault to a public Git remote or bake it into a Docker image.

## State and consistency

- **Chat sessions:** an always-on Odysseus coordinator can be the shared
  session store while inference happens on the active device. The Syncthing
  vault does not turn per-device SQLite sessions into a shared database.
- **RAG index:** ChromaDB/index state stays local to its service. Use notes
  as canonical source and rebuild/reindex when notes change or move.
- **Intern:** optional background processing; if offline, primary memory
  search/retrieval must continue through the local path.
- **Concurrent note writes:** Syncthing is not a transaction system. Editing
  a single note on two disconnected machines can create a conflict. Preserve
  original files and enable Syncthing versioning.
- **Source code:** when editing application code on multiple devices, use
  branches, commits, fetch/pull and merge. Don't let Syncthing mutate an
  unrelated working tree without its Git metadata.
- **Obsidian noise:** because the root is the whole application checkout,
  Obsidian may show Git-managed Markdown docs alongside memory notes.
  Configure its excluded files if you want to hide code/docs from search or
  the graph. The **RAG index must scope itself to the private note folders**,
  rather than indexing every Markdown file in the source tree.


## Mabel's Obsidian RAG: wired, opt-in, incremental

This fork now includes `src/mabel_vault_rag.py`, an opt-in background indexer
for exactly the six private note roots. It uses the existing Odysseus ChromaDB
embedding lanes and owner-scoped RAG retrieval. It indexes **Markdown only**
(`.md`), including subfolders and YAML frontmatter as plain text; other
attachments remain accessible as vault files, but are not embedded yet.

It is **not** a general filesystem indexer: application source, `.git/`,
`.obsidian/`, the user's separate Obsidian vault, and live databases are
outside the scope. Indexed excerpts preserve a `vault_path` / `source`
metadata reference and include the note path in retrieved document text.

**Incremental semantics:**

- The first successful scan hashes and embeds eligible notes in the six roots.
- Subsequent scans skip unchanged content and update edited notes.
- Renamed/moved notes are treated as a deletion plus a new note.
- Removed chunks are deleted **only** if their stored owner, source and Mabel
  namespace match. Generic Odysseus Personal Docs indexes stay untouched.
- If any expected root is missing/symlinked, a note is unreadable/oversized,
  or the manifest points at a different vault root, sync fails closed rather
  than treating missing content as mass deletion.
- If an embedding lane is unavailable, the manifest retains the old hash,
  allowing a retry. RAG retrieval still uses Odysseus' configured embedding
  fallback lanes where available.
- The manifest lives at `data/mabel_vault_rag_manifest.json` on each machine;
  don't sync it with Syncthing. Do not copy ChromaDB live storage between hosts.

### Before installation: check the directory selection

With this Git branch checked out, from the `mabel-vault/` root:

```sh
python scripts/sync_mabel_vault_rag.py --check
```

This only lists note counts; it doesn't open ChromaDB or embed anything.
The pre-install check requires the six directories to exist.

### Native installation configuration

Copy `.env.example` to local `.env` (ignored by Git) and set:

```dotenv
MABEL_VAULT_RAG_ENABLED=true
# Leave blank to use the source checkout root, or set an absolute path:
MABEL_VAULT_ROOT=
# Include the account(s) who should be allowed to retrieve the private notes.
# E.g. web login "admin" and API token's effective user "api".
MABEL_VAULT_RAG_OWNERS=admin,api
MABEL_VAULT_RAG_POLL_SECONDS=300
```

**Use your actual account names.** This explicit allowlist controls who can
retrieve copies of those notes through RAG. Do not enable shared indexing for
other user accounts. With `MABEL_VAULT_RAG_OWNERS` empty, sync refuses to
index; it never creates an ownerless, generally readable corpus.

Once Chroma is healthy, the app scans in a background thread shortly after
startup and every five minutes by default. Indexing does not block chat. To
perform an immediate one-time sync:

```sh
python scripts/sync_mabel_vault_rag.py --sync
```

If this fails because RAG or the embedding provider is unavailable, existing
Markdown is untouched; retry after fixing the endpoint.

### Docker installation configuration

The Docker image **deliberately excludes private vault notes** via
`.dockerignore`. To make Syncthing notes visible to the running container,
use the read-only bind-mount overlay, not an image rebuild:

```dotenv
MABEL_VAULT_HOST_DIR=/absolute/host/path/to/mabel-vault
MABEL_VAULT_RAG_ENABLED=true
MABEL_VAULT_RAG_OWNERS=admin,api
```

```sh
export ODYSSEUS_IMAGE=mabel:local
docker compose -f docker-compose.yml -f docker-compose.mabel-vault.yml build odysseus
docker compose -f docker-compose.yml -f docker-compose.mabel-vault.yml up -d --no-build
```

The overlay mounts `MABEL_VAULT_HOST_DIR` read-only as `/mabel-vault` and
sets `MABEL_VAULT_ROOT=/mabel-vault` **inside the container**. Use a native
host path on Windows where Docker Desktop can bind mount the folder.

### What still needs validation

The unit tests are committed, but we have not yet executed them or run a live
Mabel + Syncthing + Chroma deployment. Run:

```sh
pytest -q tests/test_mabel_vault_rag.py tests/test_mabel_vault_layout.py
```

Then test an actual new note, edit, move and delete in a disposable vault.
Switch to Obsidian Companion, enable its **Use RAG** option, and start a new
session as an owner named in `MABEL_VAULT_RAG_OWNERS`. Ask a question answerable
only from a test note. A correct answer should include the note as a retrieved
source. If you don't enable RAG, the automatic index still exists, but it
won't be injected into normal chat context.

The indexing path is wired now; syntax-aware Markdown chunking, automatic
web-interface status feedback, and cross-machine live E2E testing are future
improvements.

## Local sanity checks

```sh
# These private files should be ignored by Git:
git check-ignore -v memory/test.md identity/persona.md .obsidian/workspace.json

# These are public application files; Git tracks them normally:
git status --short .gitignore .dockerignore .stignore src/ routes/

# Inspect the actual Syncthing ignore file before sharing:
cat .stignore
```

For a Windows machine, the same Git commands work in PowerShell, and open the
`.stignore` file in a text editor. Verify the folder's ignore patterns inside
the Syncthing web interface on **each** machine.

Nothing in this layout changes how Odysseus starts: configuring a native or
Docker runtime, Companion MCP connectivity, active-device inference, or RAG
remains separate work. See
[the Companion integration guide](mabel-obsidian-companion.md).

# Mabel + Odysseus Companion (Obsidian)

Status: **foundation / experimental**, based on the current upstream Odysseus `dev`.
Companion source: https://github.com/eskyso/Odysseus-for-obsidian
Older profile prototype: https://github.com/eskyso/odysseus/tree/profiles-pr

## Why this approach

Mabel is an Odysseus fork, **not** a fork of the older Companion maintainer server.
Stock Odysseus already supports Companion chat, agent-mode requests, RAG, memory,
research, and MCP. We are porting missing compatibility features incrementally,
without replacing new Odysseus request authorization or agent-runtime code with
an older implementation.

**This branch adds per-turn `no_skills` handling for context injection.**
It does **not** yet implement the Companion's complete Agent Profiles protocol:
`profile`, `tools`, pinned-toolset narrowing, profile-specific model overrides
and web-UI profile editing remain future work. Voice transport endpoints exist
in upstream, but provider configuration and read-aloud behavior need a separate
end-to-end verification.

## Architecture and trust boundaries

- **Canonical knowledge**: your Obsidian vault. Never treat a generated memory
  summary or vector index as the canonical note.
- **Mabel / Odysseus**: shared orchestration, sessions, tools and memory.
  It is NOT inherently the inference host; inference should follow the
  **currently used device** whenever it has an available local model.
- **Inference endpoints**: each participating device can expose its own
  model through a registered, privately reachable endpoint. The always-on
  desktop handles explicitly delegated heavyweight, scheduled, long-running
  and offline-from-the-client tasks; it is also the online fallback for
  ordinary requests if the active device cannot serve a model.
- **Obsidian Companion**: user-facing plugin. Its *own* HTTP MCP server exposes
  tools to operate on the open vault; it is not the same thing as the separate
  Obsidian Local REST API plugin.
- **RAG**: Odysseus' persistent document index is a searchable derivative,
  not a two-way synchronization mechanism. Verify removals/renames/reindexing.
- **Second machine ("intern")**: optional worker for indexing/summarization.
  The main host must retain a working local embedding/retrieval path when the
  other PC is off. Do not make the remote worker a mandatory dependency.
- **Write permissions**: start with read-only MCP. Keep `Allow writes` and
  `Allow dangerous tools` **off** until validation on a disposable vault.

### Device-first inference: target architecture (not yet implemented)

The **active client device** is the preferred inference host for ordinary
interactive chat. A ZenBook, desktop, or other client may serve different
models and quantizations depending on its actual hardware. Opening a session
on a new device should not silently keep sending inference to the previous
device just because the session once bound to that endpoint.

```text
Obsidian / Mabel UI (current client)
           |
           v
   Shared Mabel / Odysseus coordinator
   (sessions, memory, tools, scheduling)
           |
           +--> interactive -> CURRENT DEVICE model endpoint (preferred)
           |                          |
           |                          +-- unavailable/unsupported --> desktop
           |
           +--> long / heavy / scheduled -> ALWAYS-ON DESKTOP endpoint
           |
           +--> optional intern -> background indexing / summarization
```

**Inference routing is not execution routing.** The model may run on the
current device while tool calls, vault access and memory retrieval run in
other authenticated services. Don't confuse the client-side
`http://localhost:<port>` with the coordinator's `localhost`: those are
different machines when Mabel is remote, and a Docker container has yet
another loopback network. Device endpoints must be resolvable **from the
coordinator** (for example using private Tailscale addresses or authenticated
tunnels), not merely reachable from the browser/Obsidian client. Avoid
binding inference endpoints publicly to 0.0.0.0 without an access-control
boundary.

#### Proposed routing policy

1. The client reports a stable, authorized device identity and its
   **advertised** local model capabilities/health. An arbitrary endpoint URL
   supplied in a chat request must *not* be trusted or fetched directly.
2. At the **start of each interactive request**, look up a registered healthy
   model endpoint for that current client device. Honor a user's explicit
   destination or model choice over the automatic preference.
3. If the user selects a heavy/long job, or the request is an unattended
   scheduled task, route it to the **always-on desktop**. It must continue
   after the initiating client sleeps or closes Obsidian.
4. If the active-device endpoint is unavailable, lacks the required capability
   (for instance context size), or disconnects, select the desktop fallback.
   Retrying is subject to idempotency/side-effect safety; don't automatically
   re-execute tool calls that might have already changed files.
5. Keep conversation/session history in the shared coordinator; preserve
   provenance for which device/model actually answered each turn. An endpoint
   choice is per turn, not permanent conversation ownership.
6. RAG and persistent memory stay functional without the optional intern.
   Embeddings and retrieval should have a reliable local path on the
   always-on coordinator; any dedicated remote indexer is an accelerator.
7. A completely network-isolated client requires a **separate local
   coordinator/cache mode** to keep chats and retrieval functioning. A
   remote shared coordinator cannot be treated as available while offline.

The existing Odysseus endpoint registry, selected endpoint IDs and foreground
model fallback are building blocks, but do **not** currently constitute
automatic active-device discovery and routing. Design this as a provider /
routing layer rather than by replacing the upstream chat pipeline. Test
device switching, mid-request disconnects, context limits, streaming and
scheduled execution before claiming this behavior works.



### One-root vault layout: Git + Obsidian + Syncthing

**Chosen and scaffolded in this branch:** the top-level `tovakai/mabel`
checkout is also the Obsidian `mabel-vault` root. A tracked `.stignore`
allows Syncthing to transfer only `memory/`, `identity/`, `knowledge/`,
`projects/`, `journal/` and `attachments/`. The application source uses
Git on each machine; live databases, caches, `.git/` and `.obsidian/`
are device-local. `.gitignore` protects untracked private knowledge from
the public Git repository and `.dockerignore` keeps it out of image builds.

This layout does **not** synchronize chat sessions through Syncthing.
An opt-in, incremental owner-scoped RAG synchronizer is now wired to the six
private note folders and uses local ChromaDB. Do not copy live SQLite or
ChromaDB data via Syncthing. See [setup and indexing](mabel-vault-layout.md).

**Setup steps, exceptions, recovery and required per-device ignore rules:**
[docs/mabel-vault-layout.md](mabel-vault-layout.md).

### One important networking gotcha

The Companion's MCP listener is bound to **127.0.0.1** and its generated URL
looks like `http://127.0.0.1:7077/mcp?token=...`.

That address only works for a **native Odysseus process on the same host**.
It is *not* automatically reachable from:

- an Odysseus server on another computer;
- a Docker container, even if the container publishes port 7000;
- a host using `host.docker.internal`, if the bridge still reaches a non-loopback
  host interface rather than the host's loopback listener.

A legitimate fix must use a **secure, explicitly configured tunnel or transport**
that reaches Obsidian's loopback endpoint. Never expose the MCP server to the
public internet or disable its token merely to make it connect.

For **native Odysseus on another computer** (not a Docker container), one
possible setup is an SSH reverse loopback tunnel initiated *from the machine
running Obsidian*:

```sh
# On the machine running Obsidian; replace the login and Tailscale host.
ssh -N -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:7077:127.0.0.1:7077 USER@MABEL_HOST
```

Keep the tunnel running. On the **Mabel host**, register the loopback URL
generated by Companion (including the token). If a process already uses 7077 on
that host, use an alternative remote port and adjust the registered URL.

**Docker:** a host-loopback reverse tunnel alone is not enough for bridge-networked
containers. Test container-to-endpoint connectivity first; prefer a private
authenticated bridge or documented host-network capability. Do not assume
`host.docker.internal:7077` works when the MCP service listens exclusively on
`127.0.0.1`.

## Install the fork, not the upstream image

The repository's Docker Compose config defaults to
`ghcr.io/odysseus-dev/odysseus:latest`. If you simply run the usual startup
command, you risk deploying **upstream** instead of Mabel.

For Docker Compose, set a distinct local image name, build it and start it
without building or pulling the Mabel image again:

```sh
git clone https://github.com/tovakai/mabel.git
cd mabel
git switch feat/obsidian-companion-foundation
cp .env.example .env
# Add ALLOWED_ORIGINS to .env; see below.
export ODYSSEUS_IMAGE=mabel:local
docker compose build odysseus
docker compose up -d --no-build --pull missing
```

PowerShell instead of `export`:

```powershell
$env:ODYSSEUS_IMAGE = "mabel:local"
docker compose build odysseus
docker compose up -d --no-build --pull missing
```

The default Docker host URL is `http://localhost:7011` (container port 7000).
Native installs may instead use `http://localhost:7000`. Use your **actual**
URL in the plugin; the Companion README's example does not override your config.

### Obsidian-origin CORS for live streaming

Non-streaming requests use Obsidian's `requestUrl` and generally bypass
browser CORS restrictions. Streaming uses `fetch`. Allow Obsidian's actual
origin explicitly in the Mabel server's environment, keeping the existing local
origins if you use them:

```dotenv
ALLOWED_ORIGINS=http://localhost,http://127.0.0.1,app://obsidian.md
```

Restart the server after changing `.env`. Do not use `*` with credentialed
requests. If streaming fails, the plugin falls back to non-streaming.

### Install the plugin

The plugin is a **separate** repository, not currently vendored into Mabel:

```sh
git clone https://github.com/eskyso/Odysseus-for-obsidian.git
cd Odysseus-for-obsidian
npm install
npm run build
```

Copy `main.js`, `manifest.json`, and `styles.css` into:

```text
YOUR_VAULT/.obsidian/plugins/odysseus/
```

Enable the community plugin in Obsidian. In its settings, configure the Mabel
server URL and a scoped Odysseus API token, then press **Ping**.

Do not commit `.env`, API tokens, Obsidian `data.json`, vault notes, or the
MCP token URL into this public repository.

## First validation (use a disposable vault)

1. Start a new session **from the plugin**. Bearer-token sessions and browser
   login sessions may have different owners; don't reuse an unrelated web-UI
   session if the plugin says "No model selected".
2. Test chat and then agent mode with a *read-only* vault query.
3. Enable the Companion's MCP server and register it inside Odysseus, only
   after verifying network reachability from the actual Mabel process.
4. Ask for a known note by exact name, then ask a semantic question. Inspect
   tool logs to distinguish MCP vault search from Odysseus' own RAG.
5. Enable RAG and index a **small test folder**, not your entire real vault.
   Test changes, moves and deletions before scaling up.
6. Test Companion's `no_skills` setting to ensure the skills index is not
   injected into the request context.
7. Only after review, test writing to an expendable note with explicit
   confirmation; leave dangerous commands off.

The optional smoke checker validates token-authenticated Mabel reachability
and Obsidian-origin streaming CORS without printing your token:

```sh
read -rs -p 'Odysseus API token: ' ODYSSEUS_API_TOKEN; echo
export ODYSSEUS_API_TOKEN
python scripts/check_obsidian_companion.py --url http://localhost:7011
unset ODYSSEUS_API_TOKEN
```

## Compatibility tracker

| Companion capability | Current foundation |
| --- | --- |
| Chat, sessions, model selection | Upstream API; verify end-to-end |
| Agent, vault MCP tools | Upstream MCP; connection and permissions must be configured |
| Odysseus document RAG | Upstream + opt-in incremental six-folder Mabel vault sync; not live-tested |
| Memory / skills | Upstream; `no_skills` context flag now honored |
| Full Agent Profiles / pinned tool pools | **Not implemented yet**; don't advertise as working |
| Voice | Existing upstream STT/TTS routes; configuration/UI parity unverified |
| Automatic vault change sync | Polling-based Markdown content-hash sync, owner-scoped, opt-in; not live-tested |
| Active-device interactive inference / desktop task offload | Target architecture; automatic device-aware routing **not implemented yet** |
| Remote intern with local fallback | Optional background accelerator; **not implemented yet** |

## Subsequent work

- Implement an owner-scoped Agent Profiles protocol against the **current**
  agent-runtime authorization interfaces. Tool pinning must **only narrow**
  available tools, never bypass approval, privilege checks or MCP disabling.
- Confirm plugin request/response compatibility with automated contract tests.
- Build opt-in device registration, health/capability advertisement and a
  per-turn routing policy: active-device first for interactive turns,
  always-on desktop for heavyweight/background tasks, safe desktop fallback.
  Never let arbitrary client-supplied URLs bypass endpoint authorization.
- Secure native-vs-Docker / cross-host MCP transport.
- Improve Obsidian-aware Markdown chunking, frontmatter and wikilink extraction.
  The initial incremental Markdown scanner preserves paths and includes
  frontmatter as text, but is not yet syntax-aware.
- Test agent read/write behavior against a throwaway vault and keep write access
  opt-in. Document one-click recovery/backup before enabling automated cleanup.

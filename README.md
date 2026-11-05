# M.A.B.E.L.

**Mabel** is a personal AI companion that lives on your own hardware. She has
a stable identity and moods, does research she chooses herself, keeps her own
journal, notices what you're up to, reflects on who she is, and now and then
texts you because she has something to say.

She's a personal fork of [Odysseus](https://github.com/odysseus-dev/odysseus)
(a self-hosted AI workspace for chat, agents, research, documents, email, notes
and calendar). Everything Odysseus does still works; see
[docs/ODYSSEUS_README.md](docs/ODYSSEUS_README.md) and the
[setup guide](website/setup.md). This README covers what makes her Mabel.

> **The design in one line:** keep the strange part strange, and keep the
> evidence boring. Moods, curiosity and initiative are experiments in
> personality; provenance, permissions, verification and tool use never change
> with mood.

---

## Contents

- [What she does](#what-she-does)
- [How it fits together](#how-it-fits-together)
- [Her day, on autopilot](#her-day-on-autopilot)
- [Getting started](#getting-started)
- [Setting up the extras](#setting-up-the-extras)
- [Using her](#using-her)
- [Configuration](#configuration)
- [Privacy and safety](#privacy-and-safety)
- [Command-line tools](#command-line-tools)
- [Where things live](#where-things-live)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [Credits and license](#credits-and-license)

---

## What she does

| Area | In short |
|---|---|
| **Identity, moods, modes** | One stable core identity plus a mood and a "way of working" (social, programming, vault, research) composed fresh each turn from editable Markdown modules. Technical turns only get a one-line mood, so they stay fast. |
| **Mood rhythm** | A night-owl time-of-day schedule, nudged by how much you've been chatting and how busy her background work has been. Short dwell times stop mood flicker. |
| **Curiosity whisper** | One thought she's been mulling over, which she may bring up if a conversation connects to it. It comes from you, her research, your conversations, your day, or her own reflections. |
| **Curiosity drive** | She picks something to research (links in your notes nobody wrote up, her own open questions, things from your day, questions about herself), reads sources, and writes a cited article. Every quote is checked against the fetched page. |
| **Dottie, her subconscious** | A small background model that tidies (vault typos, stale notes about the stack), scouts sources ahead of time, and notices themes you keep talking about. Suggestions go to a typed inbox; Mabel and you can keep or reject them (a rejected one cools off for a week before it may come back). |
| **Autonomy** | When you're away from the PC and the GPU is free, she wakes up and researches on her own. She pauses the moment you're back, a chat starts or a game needs the GPU, and picks up where she left off later. |
| **Reaching out** | Occasionally she texts your phone about something she genuinely wants to share. Choosing silence counts as a good outcome. Tapping the notification opens the chat. |
| **Life log** | What you do across your devices (focused apps, games, media, browsing as page titles + sites, places and steps from Home Assistant) and your journal. Every morning she writes her own account of your day. |
| **Memory** | Moments from conversations and from your days become memory candidates. You decide what's kept; kept memories are verified against their source every time they're recalled. |
| **Self-reflection** | Every few days she looks back at her own week and writes about patterns in herself, how she feels, and questions about what she is. Those questions feed her research. |
| **Images** | She can generate images (a local SDXL model by default). On a single GPU her own model steps aside while it renders and comes back afterwards. The chat menu's **Images** mode is Auto (when you ask), On (every message is an image prompt) or Off (never). |
| **Visual dreams** | At most once a day, while you're away, one of Dottie's notes sinks into a dream: she writes a few broken dream fragments, they become a picture, and on waking she looks at it and writes about it. Her self-reflection reads her recent dreams. |
| **Timeline** | One feed of everything above, so you can see what she's been up to. |

---

## How it fits together

```
                    ┌────────────────────────────────────────────────┐
  you (browser, ───►│  Mabel app (FastAPI, port 7000)                │
  phone, tailnet)   │                                                │
                    │  chat / agent loop ── persona composer         │
                    │       │                (identity + mode + mood)│
                    │  heartbeat ── curiosity sessions ── outreach   │
                    │       │              │                  │      │
                    │  life log ◄── collectors on your devices       │
                    │       ▲       Home Assistant poller            │
                    │  Dottie's loops (archivist, scout, dreamer)    │
                    └──────┬──────────────┬─────────────────┬────────┘
                           │              │                 │
                 foreground model   background model   Home Assistant
                 (Mabel herself;    ("Dottie"; can run (presence, phone
                  your GPU)          on another box)    sensors, push)
```

- **Two models.** The foreground model is Mabel: chat, research writing, her
  reflections. Dottie, a small model that can live on another machine, does
  background interpretation (themes, stale-note checks, research synthesis),
  so her chores never slow your chats.
- **The repo is also an Obsidian vault.** Lowercase top-level folders
  (`identity/`, `knowledge/`, `journal/`, `memory/`, `projects/`,
  `attachments/`) are her private vault: synced between your machines (e.g.
  with Syncthing) and **ignored by Git**, so nothing private can be committed.
  Your own main vault (the "source vault") is a separate folder she only
  ever *reads*.
- **`data/`** holds local state: settings, queues, the ledger, the life log.
  It's never synced or committed.
- **The ledger** (`data/phase0/events.jsonl`) records what happened as
  metadata only (who, what kind, which ids; never prompt text): wakes,
  research choices, inbox decisions, outreach, memory decisions.

---

## Her day, on autopilot

| When | What | Model |
|---|---|---|
| Every turn | Compose identity + mode + mood; maybe her whisper | foreground |
| Every minute | Heartbeat: may she research now? (idle, GPU free, no chat, caps) | none |
| Every 5 min | Home Assistant poll for place, steps, phone | none |
| Every 6 h | Archivist: vault variants and typos, stale open questions | none |
| Every 20 min | Outreach check: anything worth texting you? (gated) | foreground, only if gates pass |
| Daily | Dreamer: themes in your recent chats | Dottie |
| Daily | Stale-framing check of the notes about Mabel herself | Dottie |
| 06:00 | Her account of your day (06:00 to 06:00, night-owl friendly) | foreground |
| Every 3 days | Self-reflection | foreground |
| Once a day, when you've been away 20 min | A visual dream (fragments, image via the GPU swap, her entry) | foreground + image model |
| On request | Scout (pre-gathers sources), "Go research something", "Reflect now", "Let her consider now" | varies |

Nothing that needs the foreground model starts while you're chatting or
another session is running. Transient model errors before any output are
quietly retried, up to twice.

---

## Getting started

### Run it

Natively (how Mabel is usually run):

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7000
```

Or with Docker, as in [the upstream guide](docs/ODYSSEUS_README.md#quick-start).
Open the web UI, create the admin account, and add your model endpoints under
Settings.

### Point her at her models

- **Foreground model:** your default endpoint/model, used for chat and for
  everything Mabel writes herself.
- **Background ("research") role:** assign Dottie's endpoint under
  Settings → model roles. Background loops use this role.

### Point her at your vault

Set your main Obsidian vault as the source vault (stored in
`data/vault_target.json`). She indexes it for search and treats it as
read-only.

### Turn on her persona

Open the **Persona** panel (the smiley icon in the rail or sidebar) →
**Identity** → set prompt stitching to **on**. Her identity lives in
`identity/core.md`, with modes in `identity/functions/` and moods in
`identity/moods/`. Edit them right there; changes apply from the next
message, and invalid edits are refused with a reason.

---

## Setting up the extras

All optional. Each one lights up a part of the Persona panel.

### Idle detection (for autonomy)

Autonomy only runs when you're away. On Hyprland, add a listener to
`~/.config/hypr/hypridle.conf` that calls the bundled script:

```
listener {
    timeout = 240
    on-timeout = /path/to/mabel/scripts/idle_signal.sh idle
    on-resume  = /path/to/mabel/scripts/idle_signal.sh active
}
```

It writes `~/.local/state/mabel/idle`. Without it, the idle state reads
"unknown" and she never wakes on her own; unknown never means yes. While a
session runs she holds a named sleep inhibitor ("Mabel is researching") so
the PC doesn't suspend mid-thought, then releases it.

### Home Assistant (presence, phone, push)

1. In Home Assistant, create a **long-lived access token** (profile →
   Security).
2. In Mabel, add a **Home Assistant** integration: the base URL of your HA
   instance and the token. Check the actual port: it isn't always `:8123`.
3. Persona → Now → Autonomy → **Home Assistant presence**: enter your person
   entity, e.g. `person.your_name`. Presence is informational; being home
   never stops her.
4. For the life log, enable Companion app sensors on your phone: Steps,
   Detected activity, Last used app, Geocoded location, Media session.
5. Push notifications use your phone's `notify.mobile_app_*` service,
   discovered automatically (or set `initiative_notify_service`).

### Opening chats from your phone

Notifications link to `<your Mabel URL>/#<chat id>`. Make Mabel reachable
from your devices without exposing it publicly, e.g. with
`tailscale serve` → `http://127.0.0.1:7000`, and set
`initiative_public_url` to that address.

### Life-log collectors (desktop, laptop, handheld/VR)

Every Linux device (including SteamOS) runs the same standard-library
collector:

1. Persona → **Life** → **Add a device**: enter a name and press
   **Create token**. The token is shown once and can *only* send activity.
2. Run the command shown there on that device. It works from bash, zsh or
   fish, asks for the token without echoing it, and installs a systemd user
   service (`systemctl --user status mabel-collector`).

It records the focused app/window, what's playing, the running Steam game
(read locally from Steam's launch process; your Steam profile can stay
private), and new browser visits as page title + site, never full URLs.
Nothing is recorded while the device is idle.

### Your journal

Daily notes in your source vault's journal folder (default
`daily life of Anthon`; setting `lifelog_journal_folder`) are read for her
daily account. They're never used for her research or Dottie's patterns.

### Image generation (and dreams)

Any OpenAI-compatible `/v1/images/generations` endpoint works (Admin → Image Generation).
For a local model on the same GPU as her chat model, `scripts/diffusion_server.py` plus
the GPU swap does the juggling:

1. Make a venv with CUDA torch, `diffusers`, `transformers`, `accelerate`, `fastapi`, `uvicorn`
   (outside the repo, e.g. `~/.local/share/mabel/diffusion/venv`), and put a diffusers model folder next to it.
2. Describe the swap in `data/gpu_swap.json`: the Strata profile to step aside and the command that starts
   the image server (any free port; `--prompt-suffix` adds a model's trigger phrase, `--pixelate N`
   snaps output to a pixel grid):
   ```json
   {"strata_profile": "launch-your-profile",
    "services": {"image": {"url": "http://127.0.0.1:8110", "ready_path": "/v1/models", "ready_timeout": 300,
      "command": ["<venv>/bin/python", "scripts/diffusion_server.py", "--model", "<model dir>", "--port", "8110",
                  "--dtype", "float16", "--steps", "25", "--guidance-scale", "7"]}}}
   ```
3. Register the endpoint as an image endpoint with model refresh off and the model id pinned, set it as the
   image model, and turn image generation on.

A request then stops her model (`strata runtime stop`), starts the image server, renders, stops it and
starts her again (about a minute and a half on a 12 GB card). Bringing her back always runs, and a crash
mid-swap is repaired on the next app start. Dreams count prompt tokens with CLIP's tokenizer if
`~/.local/share/mabel/diffusion/models/clip-tokenizer/tokenizer.json` exists (setting `dream_clip_tokenizer`),
otherwise a cautious estimate; `dreams_enabled` switches them off.

---

## Using her

The **Persona** panel has five tabs:

- **Now:** current mood (or pin one), **Reaching out** (on/off, pause, "let
  her consider now", recent texts and whether you replied), **Autonomy** (live
  signals, why not now, on/off, pause), and her **curiosity whisper** (seed
  one yourself; yours is never replaced automatically).
- **Timeline:** everything she's done this week, newest first.
- **Life:** devices and when they were last seen, today so far, her accounts
  of your days, pause recording, never-record lists for sites and apps, and
  adding devices.
- **Research:** "Go research something", her articles, the questions she
  left herself, and **Dottie's inbox** (keep or reject, with evidence).
- **Identity:** her **reflections** ("Reflect now"), stitching mode, and the
  editors for her identity, modes and moods.

Elsewhere:

- **Memory → Episodes:** memory candidates from chats and from your days.
  **Keep** saves one (after re-verifying its source), **Later** defers it,
  **Discard** drops it.
- **Messaging her while she researches:** you get an instant "busy, back
  soon" line, and she answers properly when she's done. If she started the
  research on her own, your message simply interrupts it.
- **Standing questions about herself** live in `identity/curiosities.md`
  ("Topic: question" bullets); one shows up in each research menu.

---

## Configuration

Settings live in `data/settings.json`; most are switches in the UI.

| Setting | Default | What it does |
|---|---|---|
| `persona_mode` | `off` | `off` / `shadow` (compose and log only) / `on` |
| `persona_module_root` | `""` | Folder of identity modules (default `identity/`) |
| `autonomy_enabled`, `autonomy_paused` | `true`, `false` | Research on her own when idle and the GPU is free |
| `heartbeat_presence_entity` | `""` | Home Assistant person/device_tracker to show |
| `initiative_enabled`, `initiative_paused` | `true`, `false` | Texting your phone |
| `initiative_notify_service` | auto | HA `notify.*` service for push |
| `initiative_public_url` | `""` | Base URL notification links open |
| `lifelog_enabled`, `lifelog_paused` | `true`, `false` | The life log |
| `lifelog_exclude_sites`, `lifelog_exclude_apps` | `[]` | Never record these |
| `lifelog_retention_days` | `30` | Raw activity kept this long (her day accounts are kept) |
| `lifelog_journal_folder` | `daily life of Anthon` | Journal folder in your source vault |
| `subconscious_auto_enabled` | `true` | Dottie's timed loops |
| `reflection_enabled` | `true` | Self-reflection every 3 days |
| `episodic_capture_enabled`, `episodic_recall_enabled` | `false` | Memory candidates from chats; recall of kept memories |

Hard limits (in code, on purpose):
- **Autonomy:** 5 min idle; at most 2 sessions a day, 3 h apart; 20 min each.
- **Texting:** at most 2 a day, 3 h apart, and 48 h of silence after an
  unanswered message.
- **Research:** 15 web searches and 5 sources per session.

---

## Privacy and safety

- **Local first.** The life log, queues, ledger and settings stay on the
  machine that runs Mabel. Data only goes to your own models (and, for
  research, the web searches she makes).
- **Your vault is read-only to her.** She writes only into her own vault;
  autonomous work can never change your notes.
- **Private folders never reach Git.** Her vault folders are git-ignored, and
  memory promotion refuses to write anywhere Git doesn't ignore.
- **Browsing is titles + sites.** Full URLs are never stored. Private or
  incognito windows never reach browser history, so they never reach her.
  Use the never-record lists for anything else.
- **Your decisions stay yours.** Memories are only kept when you press Keep.
  Her ideas for changing herself are proposals in an inbox. A whisper you seed
  is never replaced automatically.
- **Unknown means no.** Autonomy needs every signal to be clearly fine. It
  only suspends the PC afterwards if its own inhibitor blocked the suspend,
  nobody else is keeping the PC awake, and no remote session is logged in.
- **Nothing is tuned for engagement.** Outreach isn't optimised for replies,
  and silence is a good outcome.
- **Evidence stays boring.** Research claims are checked against the fetched
  text, Dottie's suggestions must point at real evidence, and moods never
  touch tools or permissions.

---

## Command-line tools

```bash
python -m src.curiosity audit           # unresolved-link audit of your vault (read-only)
python -m src.curiosity candidates      # what she could research right now
python -m src.curiosity session         # run one research session (she chooses)
python -m src.curiosity backlog         # her open questions

python -m src.subconscious run [archivist|scout|dreamer|stale]
python -m src.subconscious inbox        # Dottie's open suggestions

python3 scripts/lifelog_collector.py once      # what the collector sees right now
python3 scripts/lifelog_collector.py install --url URL --device NAME

python scripts/live_eval.py COOKIE_FILE        # live tool/routing evaluation suite
```

---

## Where things live

| Path | What |
|---|---|
| `src/persona/` | Modules, affect and moods, mode router, composer, whisper, timeline, reflection |
| `src/curiosity/` | Candidates, backlog, research sessions, citation verification, held messages |
| `src/subconscious/` | Dottie's inbox and loops (archivist, scout, dreamer, stale framing, live facts) |
| `src/heartbeat/` | Availability signals, wake policy, sleep inhibitor, daemon |
| `src/gpu_swap.py`, `scripts/diffusion_server.py` | Stepping her model aside for a GPU service; the local image server |
| `src/persona/dreams.py` | Visual dreams |
| `src/initiative/` | Outreach (candidates, gates, her review, push, replies) |
| `src/lifelog/` | Store, summaries, Home Assistant poller, daily account |
| `src/episodic_memory/` | Memory candidates, verified promotion and recall |
| `routes/persona_routes.py`, `routes/lifelog_routes.py` | APIs behind the Persona panel |
| `static/js/persona.js` | The Persona panel |
| `scripts/lifelog_collector.py`, `scripts/idle_signal.sh` | Device collector, idle hook |
| `identity/` | Her identity, modes, moods, standing curiosities (private vault) |
| `knowledge/research/` | Her research articles (private vault) |
| `journal/anthon/`, `journal/self/`, `journal/curiosity/`, `journal/dreams/` | Her accounts of your days, reflections, research journal, dreams (private vault) |
| `data/` | Local state: settings, inbox, life log, ledger, queues |

---

## Troubleshooting

- **"Model request failed" while she's researching:** one model server
  serves both your chat and her session. Transient errors are retried
  automatically. If she started the research herself, chatting interrupts
  it; if you started it, you get the "busy" line and a real answer after.
- **She never wakes on her own:** check Persona → Now → Autonomy for the
  reasons. The usual ones are no idle signal, a game or VR stream using the
  GPU, the daily cap, or a running chat.
- **SteamVR open but idle:** that doesn't count as in use. Streaming to a
  headset does (the GPU's video encoder is active).
- **Presence shows "unknown":** your HA person has no device tracker
  attached. Add the Companion app's tracker to the person in HA.
- **Collector install fails on fish:** use the command from the Life tab
  (it's wrapped in `bash -c`).
- **Home Assistant unreachable:** check which port it really listens on, and
  that `homeassistant.local` resolves from this machine. The IP usually
  works.
- **Logs:** look for `[persona]`, `[curiosity]`, `[heartbeat]`, `[outreach]`,
  `[lifelog]`, `[reflection]` and `[dottie]` in the server log.

---

## Roadmap

| Phase | Status |
|---|---|
| 00: ledger, durable seams | ✅ |
| 00.5: episodic memory | ✅ (memories from chats and days, user-approved) |
| 01: prompt stitching (identity + mode + mood) | ✅ live |
| 02: mood rhythms, curiosity whisper | ✅ |
| 03: manual curiosity drive | ✅ |
| 04: Dottie's subconscious (inbox, archivist, scout, dreamer, stale framing) | ✅ (Sentinel pending) |
| 05: heartbeat and available-GPU autonomy | ✅ |
| 06: social initiative (texting your phone) | ✅ |
| Life log and daily account; self-reflection | ✅ |
| Image generation (GPU swap), Image Mode, visual dreams | ✅ |
| 07: self-evolving modules (she proposes and tests her own moods and modes) | ⏸ paused; reflections already collect her ideas |

---

## Credits and license

Built on [Odysseus](https://github.com/odysseus-dev/odysseus) by Felix
Kjellberg and contributors. Design borrows ideas from
[companion-emergence](https://github.com/hanamorix/companion-emergence),
[subconscious](https://github.com/MidasMulli/subconscious),
[agent-memory](https://github.com/tigerless-labs/agent-memory) and
A-MEM.

AGPL-3.0-or-later; see [LICENSE](LICENSE) and
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).

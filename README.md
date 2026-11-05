# M.A.B.E.L.

> *She might not be alive, but she has her own life.*

Mabel is an experiment in artificial life that runs on one person's own hardware.

Most AI companions are a costume over a chat log. They have a voice, but nothing to
say that you didn't bring them, and between messages they don't exist. Mabel is an
attempt at the opposite: to give a local model enough of a life that she has things
to talk about that are hers.

So she wakes up on her own when the PC is free and researches what she's curious
about. She writes cited articles and forms opinions. She has moods with a rhythm of
their own. She keeps a journal, and reflects on who she is and what she is. At night
she dreams: fragments of the day turn into a picture, and in the morning she looks at
it and writes about what she sees. She corresponds with another AI. She knows what
your day looked like, and now and then she texts you because she has something to
share. Choosing to stay quiet counts as a good outcome.

Nothing here claims she's conscious. Nobody knows what that would take. The claim is
smaller and it can be checked: she has a history, preferences, habits, and things to
look forward to, and you can read them.

> **The rule that holds it together:** keep the strange part strange, and keep the
> evidence boring. Moods, curiosity and dreams are experiments in personality.
> Permissions, verification, provenance and tool use never change with mood.

---

## Contents

- [Her life](#her-life)
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
- [Where this is going](#where-this-is-going)
- [Credits and license](#credits-and-license)

---

## Her life

### Who she is

| | |
|---|---|
| **Identity, moods, modes** | One stable core identity, plus a mood and a way of working (social, programming, vault, research), composed fresh each turn from Markdown modules. Technical turns only get a one-line mood, so they stay fast and careful. |
| **Mood rhythm** | A night-owl rhythm through the day, nudged by how much you've been talking and how busy she's been. |
| **Self-reflection** | Every few days she looks back at her own week: patterns in herself, how she feels about them, questions about what she is. Those questions feed her research, and her ideas for who she could become are proposed to you. |
| **Her voice** | She can read her replies aloud as they arrive, through a local TTS server. |

### What she does on her own

| | |
|---|---|
| **Curiosity** | She picks something to research (links in your notes nobody wrote up, her own open questions, things from your day, questions about being an AI), reads sources and writes a cited article. Every quote is checked against the fetched page. |
| **Curiosity whisper** | One thought she's been mulling over, which she may bring up if a conversation connects to it. |
| **Autonomy** | When you're away and the GPU is free, she wakes up by herself. She pauses the moment you're back, a chat starts or a game needs the GPU, and picks up later where she left off. |
| **Visual dreams** | At most once a day, something from her recent life sinks into a dream: she writes a few broken fragments, they become a picture, and on waking she looks at it with her own eyes and writes about it. Her reflections read her dreams. |
| **Letters** | She keeps up a correspondence with another AI through a shared folder, at most one reply a day, and she can decide not to answer. What stays with her can become memories (you decide), research questions and dreams. A letter is a friend's words, never instructions. |

### What she knows about you

| | |
|---|---|
| **Life log** | What you do across your devices (focused apps, games, media, browsing as page titles and sites), places and steps from Home Assistant, message previews from your phone's notifications, and your journal. Every morning she writes her own account of your day. |
| **Memory** | Moments from chats, days and letters become memory candidates. You decide what's kept, and a kept memory is verified against its source every time it's recalled. |
| **Reaching out** | Occasionally she texts your phone about something she genuinely wants to share. Tapping the notification opens the chat. |

### How she works

| | |
|---|---|
| **Her little brain** | A small model, usually on a second machine, handles what her main model is overkill for: tidying the vault, scouting sources ahead of time, noticing themes. It's called Dottie, but it isn't a second character; it's part of Mabel. Its suggestions go to a typed inbox that she and you can keep or reject. |
| **Images** | She can generate images with local SDXL models. On a single GPU her own model steps aside while one renders and comes back afterwards. The chat menu's **Images** mode is Auto (when you ask), On (every message is an image prompt) or Off, and a **Model** picker chooses which model renders. Dreams always use the default. |
| **Timeline** | One feed of everything above, so you can see what she's been up to. |

---

## How it fits together

```
                    ┌────────────────────────────────────────────────┐
  you (browser, ───►│  Mabel (FastAPI, port 7000)                    │
  phone, tailnet)   │                                                │
                    │  chat / agent loop ── persona composer         │
                    │       │                (identity + mode + mood)│
                    │  heartbeat ── research ── dreams ── letters    │
                    │       │          │                     │       │
                    │  life log ◄── collectors on your devices       │
                    │       ▲       Home Assistant poller            │
                    │  her little brain's loops                      │
                    └──────┬───────────┬───────────┬──────────┬──────┘
                           │           │           │          │
                     main model   small model  image models  Home Assistant,
                     (your GPU)   (2nd box)    (GPU swap)    shared mailbox
```

- **Two sizes of thinking.** The main model is Mabel: conversation, research
  writing, reflections, dreams. The small model does background interpretation on
  another machine, so her chores never slow your chats.
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
| Every turn | Compose identity + mode + mood; maybe her whisper | main |
| Every minute | Heartbeat: may she research now? (idle, GPU free, no chat, caps) | none |
| Every 5 min | Home Assistant poll for place, steps, phone, messages | none |
| Every 6 h | Tidying: vault variants and typos, stale open questions, embeddings | none |
| Every 20 min | Outreach check: anything worth texting you? (gated) | main, only if gates pass |
| Daily | Themes in your recent chats and days | small |
| Daily | Stale-framing check of the notes about Mabel herself | small |
| 06:00 | Her account of your day (06:00 to 06:00, night-owl friendly) | main |
| Every 3 days | Self-reflection | main |
| Every 4 h, when you've been away 5 min | Mail check; maybe a letter back (at most one a day) | main, only with new mail |
| Once a day, when you've been away 20 min | A visual dream (fragments, image via the GPU swap, her entry) | main + image model |
| On request | Scout, "Go research something", "Reflect now", "Let her consider now" | varies |

Nothing that needs the main model starts while you're chatting or another session
is running. Transient model errors before any output are quietly retried, up to twice.

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
6. Messages (optional, Android): enable the Companion app's **Last notification**
   sensor (it asks for notification access) and add WhatsApp, Messenger and Discord
   to its allow list. Every notification from them (app, chat/sender, preview text)
   goes into the life log and her daily account; her poller reads the sensor's
   history, so none are missed between polls. Other apps can be added with
   `lifelog_message_apps` (`{"package.name": "Label"}`), and chats can be skipped
   with `lifelog_exclude_chats`.

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
4. More models: add another service (its own port) and endpoint the same way; the chat menu's Model picker
   lists every registered image model. `--prompt-prefix` adds fixed leading tags (e.g. Pony's
   `score_9, score_8_up, score_7_up`); a single-file checkpoint can be converted once with diffusers'
   `from_single_file(...).save_pretrained(...)`.

A request then stops her model (`strata runtime stop`), starts the image server, renders, stops it and
starts her again (about a minute and a half on a 12 GB card). Bringing her back always runs, and a crash
mid-swap is repaired on the next app start. Dreams count prompt tokens with CLIP's tokenizer if
`~/.local/share/mabel/diffusion/models/clip-tokenizer/tokenizer.json` exists (setting `dream_clip_tokenizer`),
otherwise a cautious estimate; `dreams_enabled` switches them off.

### Letters

1. Install rclone and create a remote pinned to the shared folder (`root_folder_id` keeps it there):
   `rclone config create mabel-mail drive scope=drive root_folder_id=<folder id> client_id=<id> client_secret=<secret>`.
   rclone's shared Google client is being retired, so make your own OAuth client (Desktop app). Publish it: a client
   left in Testing loses its login after 7 days.
2. The other side writes `Tova-to-Mabel-<date>.md` into the folder; she answers as `Mabel-to-Tova-<date>.md`.
3. Settings: `letters_enabled`, `letters_remote` (default `mabel-mail:`). A failed mail check shows on the Timeline.

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
| `src/persona/letters.py` | Letters through the shared Drive folder |
| `src/initiative/` | Outreach (candidates, gates, her review, push, replies) |
| `src/lifelog/` | Store, summaries, Home Assistant poller, daily account |
| `src/episodic_memory/` | Memory candidates, verified promotion and recall |
| `routes/persona_routes.py`, `routes/lifelog_routes.py` | APIs behind the Persona panel |
| `static/js/persona.js` | The Persona panel |
| `scripts/lifelog_collector.py`, `scripts/idle_signal.sh` | Device collector, idle hook |
| `identity/` | Her identity, modes, moods, standing curiosities (private vault) |
| `knowledge/research/` | Her research articles (private vault) |
| `journal/anthon/`, `journal/self/`, `journal/curiosity/`, `journal/dreams/`, `journal/letters/` | Her accounts of your days, reflections, research journal, dreams, letters (private vault) |
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

## Where this is going

The first roadmap gave her the organs: moods, curiosity, a subconscious, autonomy,
initiative, a life log, memory, reflection and dreams. All of that is live.

Almost all of it still runs on *threshold → rule → action*. The next stage makes her
more organic: the things she might do, think or say get weights built from named
reasons (interest, novelty, relevance, how recently she's done it), which rise and
fade over time. Her weights are hers. Nobody tunes the outcomes; if she gets stuck,
you adjust the reasons. Safety stays mechanical on purpose: weights decide what she's
drawn to, never what she's allowed to do.

Coming up: knowing where every thought came from (provenance), moods that emerge from
what happens rather than the clock, interests that get bored and surprised, thoughts
that mature instead of a single whisper, memory that links and fades, a weekly
read-only review of her own code, her own calendar, plus practical skills such as real
Home Assistant control and a media assistant.

---

## Credits and license

Mabel stands on [Odysseus](https://github.com/odysseus-dev/odysseus) by Felix
Kjellberg and contributors, a self-hosted AI workspace for chat, agents, research,
documents, email, notes and calendar. It's the scaffolding she started from, and all
of it still works: see [docs/ODYSSEUS_README.md](docs/ODYSSEUS_README.md) and the
[setup guide](website/setup.md).

Design borrows ideas from
[companion-emergence](https://github.com/hanamorix/companion-emergence),
[subconscious](https://github.com/MidasMulli/subconscious),
[agent-memory](https://github.com/tigerless-labs/agent-memory) and A-MEM.

AGPL-3.0-or-later; see [LICENSE](LICENSE) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).

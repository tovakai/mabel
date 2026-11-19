# How MABEL-OS works

A one-sitting tour of the code. Read this first; then `HOWTO.md` has step-by-step recipes for common changes. File paths are relative to `tui-rs/`.

## The big picture

MABEL-OS is one Rust program (`mabel-os`) that draws a whole "operating system" in a terminal: nine channels (COMMS, LIFE, AUDIO, …), each a screen of panels. It's built on **ratatui** (drawing in a terminal) and **crossterm** (keys, mouse, terminal modes), with **tokio** for doing slow things (network, files, processes) without freezing the screen.

It talks to Mabel over HTTP (`src/os/api.rs`; the contract is `../docs/README/MABEL_OS_API_CONTRACT.md`) and reads `~/.config/mabel-tui/config.toml` (`src/os/config.rs`).

```
tui-rs/
  src/
    main.rs              start-up, subcommands, the terminal, the main loop
    os/                  the machinery every channel shares (the "OS" in MABEL-OS)
    channels/            one folder or file per channel, plus example.rs to copy
    widgets/             reusable pieces: text input, markdown, vim editor, pictures, forms…
    printserver.rs       `mabel-os print-server` (the deck's thermal printer service)
  crates/
    mabel-media/         PIXEL pictures, TRACKER songs, the synth (shared with games)
    mabel-console/       the DreamTape console: framebuffer, wasm runner, sound, buttons, saves
    mabel-kit/           what a DreamTape game is written against
  tapes/stress/          a benchmark tape
  scripts/               sandbox.sh, check.sh, fake_mabel.py
  docs/                  this file, HOWTO.md
```

## From start to the first frame (`src/main.rs`)

1. **Subcommands first.** `mabel-os print-server`, `mabel-os tape …` and `mabel-os new-tape …` run and exit without opening the UI.
2. **Guards.** It refuses to start inside its own ctrl+z shell or inside TERM (that would be MABEL-OS inside MABEL-OS: two music players, two of everything).
3. **Config** is read (`os::config::load`).
4. **`App::new`** builds the state: the API client, the music player, MPRIS, and every channel (`os::channel::all`). **`app.start()`** lets each channel kick off its first loads.
5. **The terminal** goes into raw mode on the alternate screen, with mouse, paste and (where the terminal has it) the kitty keyboard protocol.
6. **The main loop** (`run`): draw, then wait for whichever comes first:
   - a key, mouse or paste event → `app.on_event`
   - the half-second tick (the clock, toasts, polling) → `app.tick`
   - a message from background work → `app.on_msg`
   - the time a channel asked to be redrawn (`next_wake`, for animations)

   …and draw again. It ends on ctrl+q.

ratatui is **immediate mode**: the whole screen is drawn from the app's state every time, and ratatui sends the terminal only what changed. So drawing code never "updates" anything on screen; it just describes the screen as it should look now.

## The app (`src/os/app.rs`)

`App` owns the channels, which one is on screen (`active`), and **where the keyboard is** (`focus`):

- **Nav mode** (`Focus::Nav(Some(i))`): one section is *lit* (a heavy bright border). h/j/k/l move the light between sections (`os/nav.rs` picks the neighbour by position), i/a/enter go into it, `?` shows the keys.
- **Inside** (`Focus::Inside(i)`): keys go to the section (typing, scrolling a list). esc or ctrl+] goes back to nav mode.

Each frame, a channel's `render` returns its **sections**: the rectangles of its panels, in a fixed order. The app keeps them (`sections`) for nav, mouse clicks, and to know which border to light.

### Where a key press goes (`App::on_key`)

In this order, the first that takes it wins:

1. **Raw sections**: inside a TERM shell or a running tape, everything goes to the program except alt+1–9 (switch channel) and ctrl+] (TERM: back to nav; TAPES: the pause menu, via `Channel::escape`).
2. ctrl+q quits.
3. The colour tuner (ctrl+t) and the help card (`?`) take keys while they're open.
4. A channel's popup (`captures_keys`), like "pick a playlist".
5. F12 installs an update.
6. **Channel keys**: F1–F9 or alt+1–9 switch channel (the same key again on a suite: its next app).
7. ctrl+z (a shell), ctrl+p (play/pause), ctrl+t (the tuner).
8. **Inside a section**: the channel's `key`. If it says `Ignored`, h/j/k/l are tried as arrows, then esc/ctrl+] (back to nav) and `?`.
9. **Nav mode**: h/j/k/l, i/a/enter, esc, `?`; anything else goes to the channel's `nav_key` (so AUDIO's space pauses without going in first).

## Channels (`src/os/channel.rs`)

A channel is anything that implements the **`Channel` trait**. The important methods (most have defaults, so a channel only writes what it needs):

| method | when | what to do |
|---|---|---|
| `id`, `tab` | always | a unique id ("audio"), the tab's name ("AUDIO") |
| `start` | once, at start-up | first loads (in the background) |
| `render` | every frame | draw into `area`, return the sections' rects |
| `key` | a key while inside a section | handle it, return `Used` or `Ignored` |
| `nav_key` | a key in nav mode the app doesn't use | shortcuts that work without going in |
| `mouse`, `paste` | mouse events, pasted text | |
| `on_msg` | background work finished | take the result, update state |
| `tick` | twice a second, for every channel | polling, timers (`visible` = on screen) |
| `next_wake` | after drawing | "draw me again at this time" (animations, spinners) |
| `lands_inside` | switching to the channel | start inside a section (COMMS: the prompt) instead of nav mode |
| `raw_keys`, `escape` | keys inside a section | "every key is mine" (TERM, TAPES) |
| `takes_screen`, `key_releases` | | a running tape: the whole screen, key releases |

`all(cfg)` at the bottom of `os/channel.rs` builds the list; **its order is the tab order**.

### Ctx: what a channel may use

Every call into a channel gets a **`Ctx`**: the API client (`ctx.api`), the config (`ctx.cfg`), the music player, the colours, and ways to ask the app for things. A channel never changes the app directly; it **asks** (`Request`), and the app carries the requests out after the call:

```rust
ctx.toast("TITLE", "a card for 5 seconds");
ctx.error("TITLE", "a red one");
ctx.status("the line above the footer");
ctx.focus(2);                       // keys go inside section 2
ctx.spawn("audio", |api| async move { … });   // background work, see below
```

### Background work and messages

Anything slow (HTTP, files, processes) runs in the background so the screen never freezes:

```rust
ctx.spawn(ID, |api| async move {
    let answer = api.life().await;   // waits here, in the background
    Got::Life(answer)                // whatever you return…
});

fn on_msg(&mut self, _id: &str, msg: Box<dyn Any + Send>, ctx: &mut Ctx) {
    let Ok(got) = msg.downcast::<Got>() else { return };   // …arrives here, by ID
    …
}
```

The result travels as a `Msg::For(id, value)` through a channel (`tx`) to the main loop, which hands it to the channel whose `accepts(id)` is true. `ctx.run(ID, future)` is the same for a future that doesn't need the API.

### Suites (`src/os/suite.rs`)

COMMS (MABEL, INBOX, COURSE), CREATIVE (PIXEL, TRACKER) and SYS (STATUS, NETWORK, …) are **suites**: one channel holding several apps, each itself a `Channel`. A bar on top lists the apps; the channel's F-key again cycles them. The suite passes calls to the app on screen and takes messages for every app (`accepts`). `Request::Deliver` switches to another channel or app and hands it something (VAULT's "ask Mabel about this note" opens COMMS ▸ MABEL with the note).

## The look (`src/os/ui.rs`, `src/os/palette.rs`)

The screen, top to bottom: the **casing** (MABEL-OS, the link to Mabel, the clock), the **tabs**, the channel's **body**, the **status line** (what's playing, messages), the **footer** (keys).

- `ui::panel(pal, "TITLE", state)` draws a channel panel; `state` comes from `view.panel_state(section)` (idle, lit, inside).
- Colours come from `Palette` (`view.pal` / `ctx.pal`): `amber`, `amber_dim`, `beige`, `teal`, `red`, `bg`, `panel`… Never write colour values in a channel: the deck has its own preset, and ctrl+t tunes them live.
- `view.narrow` is true on small screens (the 7" deck, under 140 columns): stack panels instead of putting them side by side.

## Widgets (`src/widgets/`)

| widget | what |
|---|---|
| `input.rs` | a one-line text field (cursor, history-free) |
| `markdown.rs` | Markdown → terminal lines in MABEL-OS colours |
| `vim.rs` | the vim editor VAULT uses |
| `picture.rs` | images as half-block pixels |
| `popup.rs` | a pick-from-a-list popup |
| `form.rs` | multi-field forms (INVENTORY) |
| `wrap.rs` | word wrapping by display width |

## The crates

- **mabel-media**: `.mpx` pictures (PIXEL), `.mtk` songs (TRACKER), the synth (`tracksynth`) and its audio output. Shared so games can read the same files.
- **mabel-console**: what runs a DreamTape: the pixel framebuffer and its half-block rendering, the wasm runner (`host.rs`, wasmi), buttons, sound, saves, `tape.toml`.
- **mabel-kit**: the API a tape is written against, compiled to wasm (calls into TAPES) or natively (the standalone runner).

## Tests and trying things

- `cargo test` runs the unit tests (they sit at the bottom of the file they test, in `mod tests`).
- `scripts/check.sh`: format, lint and test in one go. Run it before committing.
- `scripts/sandbox.sh`: run your changed MABEL-OS against a fake Mabel with a throwaway config, vault and home, and silent audio. **Use it for anything that writes** (chats, the vault, tapes, inventory).
- `cargo doc --open --no-deps`: the code's own documentation (every `///` and `//!` comment), browsable.

## Glossary

| word | meaning |
|---|---|
| channel | one tab (COMMS … TAPES); a type implementing `Channel` |
| suite | a channel made of several apps (COMMS, CREATIVE, SYS) |
| app | one screen inside a suite (INBOX, PIXEL, NETWORK…) |
| section | one panel of a channel, the unit nav moves between |
| nav mode | moving between sections with h/j/k/l; one section is *lit* |
| inside | keys go to a section |
| raw keys | a section that takes every key (a shell, a game) |
| Ctx | what a channel gets in each call: the API, config, and ways to ask the app for things |
| Request | something a channel asks the app to do (toast, focus, switch channel…) |
| Msg | how background work reports back (`Msg::For(id, value)`) |
| casing | the strip along the top (name, link, clock) |
| the deck | the Pi 3B cyberdeck with a 7" screen; "narrow" layouts are for it |
| tape / DreamTape | a game for TAPES, made with mabel-kit |

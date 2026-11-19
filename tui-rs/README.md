# MABEL-OS

Mabel in a terminal: a retro-futuristic "operating system" of nine channels (COMMS, LIFE, AUDIO, INVENTORY, VAULT, TERM, CREATIVE, SYS, TAPES), written in Rust with ratatui. It runs on the PC, laptops and the deck (a Pi 3B with a 7" screen). It talks to Mabel over HTTP and reads `~/.config/mabel-tui/config.toml`.

## Where to start

| you want to… | read |
|---|---|
| understand how it fits together | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| change something (a key, a colour, a channel…) | [`docs/HOWTO.md`](docs/HOWTO.md) |
| make your own channel | `src/channels/example.rs`, a commented one to copy |
| read the code's own docs | `cargo doc --open --no-deps` |
| know what Mabel's API looks like | [`../docs/README/MABEL_OS_API_CONTRACT.md`](../docs/README/MABEL_OS_API_CONTRACT.md) |
| make games for TAPES | `mabel-os new-tape <name>`, and the Rust course in COMMS ▸ COURSE |

## Trying a change safely

```sh
scripts/sandbox.sh        # run this source tree against a fake Mabel, throwaway files, silent audio
scripts/check.sh          # format, lint and test: run before committing
```

The sandbox never touches your real chats, vault, inventory, tape repo or speakers. Use it for anything that writes.

## Layout

```
src/main.rs          start-up, subcommands, the terminal and the main loop
src/os/              shared machinery: the app, the Channel trait, nav, keys, look, config, API
src/channels/        one folder (or file) per channel, plus example.rs
src/widgets/         reusable pieces: input, markdown, vim editor, pictures, popups, forms
src/printserver.rs   `mabel-os print-server`, the deck's thermal printer service
crates/mabel-media/  PIXEL pictures, TRACKER songs, the synth
crates/mabel-console/ the DreamTape console (wasm runner, sound, buttons, saves)
crates/mabel-kit/    what DreamTape games are written against
scripts/             sandbox.sh, check.sh, fake_mabel.py
```

## Commands

```sh
cargo run --release              # MABEL-OS with your real config (careful: real Mabel, real files)
cargo test                       # the tests
mabel-os new-tape <name>         # a new DreamTape project
mabel-os tape publish|list|install|remove|unpublish|pack
mabel-os print-server            # the deck's printer service
```

## Releasing (every device, the deck included)

Never compile on the deck. On the PC:

```sh
../scripts/tui_rs_dist.sh        # builds dist/mabel-os-x86_64 and -aarch64 (4 jobs at most)
```

Mabel serves those, and every installed copy's header then says **▲ UPDATE F12**. A new device installs with `curl -fsSL https://<mabel>/tui/install.sh | sh` (it pairs through Tailscale). The builds pin glibc 2.36, so they run on Debian 12/13 and anything newer. First-time setup for cross-building: `rustup target add aarch64-unknown-linux-gnu wasm32-unknown-unknown` and `cargo install --locked cargo-zigbuild` (needs zig).

# MABEL-OS (Rust)

MABEL-OS, Mabel in a terminal: Rust + ratatui. It runs on the PC, laptops and the deck (Pi 3B),
and `mabel-os print-server` is the deck's print server. It replaced the Python version (`tui/`,
retired 2026-11-19; it's in git history).

It speaks the HTTP contract in `../docs/README/MABEL_OS_API_CONTRACT.md` and reads
`~/.config/mabel-tui/config.toml`.

## Run

    cargo run --release          # on the PC
    cargo test                   # unit tests (config, palette, nav, keys, the chat-stream parser)

## Layout

| file | what |
|---|---|
| `main.rs` | terminal setup, the event loop, ctrl+z shell |
| `app.rs` | state, key routing (channel keys → inside a section → nav mode), the frame |
| `channel.rs` | the `Channel` trait, the Ctx a channel acts through, and the list of channels |
| `nav.rs` | vim-style sections: `neighbour` picks the section to move to with h j k l |
| `keys.rs` | key matching that survives a Swedish keyboard (match the character, AltGr-safe) |
| `ui.rs` | the look: panels, casing, tabs, status strip, footer, help card, toasts |
| `help.rs` | the `?` lists, global and per channel |
| `api.rs` | Mabel's HTTP API with the token: sessions, chat stream (SSE), LIFE, music, Beeper |
| `config.rs` / `palette.rs` | the config file, and the amber palette with the `deck` preset and `[colors]` |

## Releasing (every device, the deck included)

Never compile on the deck. On the PC:

    rustup target add aarch64-unknown-linux-gnu
    cargo install --locked cargo-zigbuild       # needs zig on PATH
    ../scripts/tui_rs_dist.sh                   # builds dist/mabel-os-x86_64 and -aarch64

Mabel serves those, and every installed copy's header then says **▲ UPDATE F12**. A new device
installs with `curl -fsSL https://<mabel>/tui/install.sh | sh` (it pairs through Tailscale).
The builds pin glibc 2.36 (`.2.36`), so they run on Debian 12/13 and anything newer.

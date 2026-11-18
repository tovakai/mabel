# MABEL-OS (Rust)

The Rust + ratatui port of MABEL-OS. The Python version in `../tui/` stays the prototype:
new ideas go there first, and this port follows at milestones, checked screen by screen.

The two clients share no code. They share the HTTP contract in
`../docs/README/MABEL_OS_API_CONTRACT.md` and the config file `~/.config/mabel-tui/config.toml`.

## Run

    cargo run --release          # on the PC
    cargo test                   # unit tests (config, palette, nav, keys, the chat-stream parser)

## Layout

| file | what |
|---|---|
| `main.rs` | terminal setup, the event loop, ctrl+z shell |
| `app.rs` | state, key routing (channel keys → inside a section → nav mode), the frame |
| `channel.rs` | the `Channel` trait, suites (F-key again = next app), placeholders until a channel is ported |
| `nav.rs` | vim-style sections: `neighbour` picks the section to move to with h j k l |
| `keys.rs` | key matching that survives a Swedish keyboard (match the character, AltGr-safe) |
| `ui.rs` | the look: panels, casing, tabs, status strip, footer, help card, toasts |
| `help.rs` | `?` lists (kept in step with HELP in `tui/mabel_tui/app.py`) |
| `api.rs` | Mabel's HTTP API with the token: sessions, chat stream (SSE), LIFE, music, Beeper |
| `config.rs` / `palette.rs` | the config file, and the amber palette with the `deck` preset and `[colors]` |

## The deck (Pi 3B, aarch64)

Never compile on the deck. Cross-compile on the PC:

    rustup target add aarch64-unknown-linux-gnu
    cargo install --locked cargo-zigbuild       # needs zig on PATH
    cargo zigbuild --release --target aarch64-unknown-linux-gnu.2.36
    scp target/aarch64-unknown-linux-gnu/release/mabel-os zum@192.168.0.6:~/

(`.2.36` pins glibc to an older version than Debian 13's, so the binary runs there.)

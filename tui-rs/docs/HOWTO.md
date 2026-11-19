# How to change things

Recipes for the changes you're most likely to want. Read `ARCHITECTURE.md` first for the words (section, nav mode, Ctx…). Paths are relative to `tui-rs/`.

**The loop for every change:**

1. Change the code in nvim.
2. `scripts/sandbox.sh` to try it (fake Mabel, throwaway files, silent audio).
3. `scripts/check.sh` before committing (format, lint, tests).
4. Commit. To get it onto every device: `../scripts/tui_rs_dist.sh`, then F12 on each device.

When the compiler complains, read the whole message: it points at the line and usually says what to change. `cargo doc --open --no-deps` shows every documented function.

---

## Change a colour

- **Just for you, no code:** ctrl+t opens the tuner; `s` saves your changes into `[colors]` in config.toml.
- **In config.toml by hand:** `[colors]` with names from `Palette` (`src/os/palette.rs`), e.g. `amber = "#ffb000"`.
- **The defaults:** `src/os/palette.rs`, the presets (the PC's and the deck's).

## Add a key to a channel

1. Find the channel's `fn key` (keys inside a section) or `fn nav_key` (keys in nav mode). For AUDIO: `src/channels/media/audio/mod.rs`.
2. Add an arm to its `match`, next to similar keys. Match characters with `keys::ch(&key)` / `keys::is(&key, 'x')` (they work on a Swedish keyboard), ctrl combos with `keys::ctrl(&key, 'x')`:
   ```rust
   (Some('x'), _) => { /* what x does */ }
   ```
3. Check the key isn't taken already: search the file for `'x'`. Remember nav mode always keeps h j k l i a enter esc `?`.
4. Add it to the channel's list in `src/os/help.rs` so `?` shows it.

## Add a config option

1. In `src/os/config.rs`, add a field to `struct Config` with a `///` comment saying what it does:
   ```rust
   /// Seconds between INBOX refreshes.
   pub inbox_every: i64,
   ```
2. In `parse`, read it with a default (`t.str`, `t.int`, `t.float`, `t.bool`):
   ```rust
   inbox_every: t.int("inbox_every", 8),
   ```
3. Use it in a channel through `ctx.cfg.inbox_every` (or pass it in from `os/channel.rs` when the channel is built).
4. A config test lives at the bottom of `config.rs`; add a line for your option.

## Add an app to a suite (COMMS, LIFE, MEDIA, CREATIVE, SYS)

1. Make the app: a type implementing `Channel` (copy `src/channels/example.rs`), in the suite's folder.
2. Declare it in the folder's `mod.rs` (`/// what it is` + `pub mod myapp;`).
3. In `src/os/channel.rs`, `all()`, add it to the suite's list:
   ```rust
   (app("myapp", "MYAPP", "a hint shown in the bar"), boxed(crate::channels::comms::myapp::MyApp::new())),
   ```
4. Add its keys to `src/os/help.rs` under its id.

## Add a whole channel

1. Copy `src/channels/example.rs` to `src/channels/mything.rs` (or a folder `mything/mod.rs` if it'll have several files). Rename `Example`, change `ID` and `tab`.
2. Declare it in `src/channels/mod.rs`.
3. Add it to the list in `src/os/channel.rs`, `all()`. Its place in the list is its tab number. alt+digit reaches 1–9; F-keys reach further.
4. Add its keys to `src/os/help.rs`.
5. `scripts/sandbox.sh` and try it.

## Ask Mabel's API for something new

1. Make sure the endpoint exists on Mabel's side (`../routes/`), and which token scope it needs (`../routes/api_token_routes.py`, profile `tui`).
2. Add a method to `Mabel` in `src/os/api.rs`, next to similar ones (they use `get_json`, `req`, `checked`).
3. Call it in the background from a channel and handle the answer in `on_msg` (see `example.rs`: `ask_mabel` and `on_msg`).
4. Write it down in `../docs/README/MABEL_OS_API_CONTRACT.md` (method, path, what comes back).
5. For the sandbox: teach `scripts/fake_mabel.py` a canned answer.

## Add a function to mabel-kit (for DreamTapes)

A kit function exists in four places that must agree:

1. `crates/mabel-kit/src/lib.rs`: the public function games call, with a `///` comment.
2. Same file, `mod sys` for wasm: the `ffi` import (strings travel as pointer + length) and its safe wrapper.
3. Same file, `mod sys` for native: what the standalone build does.
4. `crates/mabel-console/src/host.rs`, `link()`: the import TAPES provides (`wrap!(linker, "name", |c: Caller<'_, Host>, …| …)`).

Then: test it in a tape, add it to the API list in the course's `tutor.md` (`Courses/rust-dreamtapes/tutor.md` in the vault), and tag a new kit version (`git tag -a kit-v0.3`, push the tag, raise `KIT_TAG` in `src/channels/tapes/newtape.rs`).

## Change what a channel draws

1. Find its `fn render`. Panels are `ui::panel(pal, "TITLE", view.panel_state(SECTION))`, text is `Paragraph::new(lines)` of `Line::styled("text", Style::new().fg(pal.amber))`.
2. Keep the deck in mind: `view.narrow` means a 128×37 screen; stack panels there.
3. If you add or remove a panel, the list of rects `render` returns (and the section constants at the top of the file) must change with it.

## Write a test

Tests sit at the bottom of the file they test:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn what_it_should_do() {
        assert_eq!(my_function(2), 4);
    }
}
```

`cargo test my_function` runs only the tests with that in their name. Pure functions (no screen, no network) are the easiest to test: pull the logic out into one.

## Something broke

- **It doesn't compile:** read the first error only, fix it, build again; later errors are often caused by the first.
- **It compiles but does the wrong thing:** `scripts/sandbox.sh` and look; add a `ctx.status(&format!("{x:?}"))` to see a value on the status line.
- **Undo everything since the last commit:** `git restore .` (careful: it's gone after that). `git diff` shows what you changed first.
- **A device got a bad build:** fix it, run `../scripts/tui_rs_dist.sh` again, F12.

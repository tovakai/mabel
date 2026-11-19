# Working on MABEL-OS (for Claude and other AI helpers)

MABEL-OS is Anthon's own program, and he is learning Rust on it. Every change should leave
the code at least as easy for *him* to read and change as it was. Start with `README.md`,
then `docs/ARCHITECTURE.md` (how it fits together) and `docs/HOWTO.md` (recipes).

## How to work

- **Read before you write.** Check claims against the code, not memory or file names. This
  applies to comments too: in the big comment pass most mistakes were plausible guesses
  ("refuses to overwrite", "0-100") that the code contradicted.
- **Small steps, each one verified** (below), committed separately with a message that says
  what changed and why. Commit or push only when he has said to for this piece of work.
- **Keep the docs in step.** If you move, rename or add a file, update ARCHITECTURE/HOWTO
  and the module lists. If you add a key, update its help text (`?` cards) and the footer.
- **Plan batches together.** He often sends ideas in bundles: plan them as one batch and
  ship them as one release.

## Comments

- Say **what and why** in plain words, not Rust syntax. Write "the songs on disk", not
  "returns a Vec of Values filtered by…".
- Every `fn`, `struct`, `enum` and non-obvious field gets a `///` line. Trait method
  impls don't: the trait explains them once (`os/channel.rs`).
- Mention keys and visible behaviour ("v: open it in VAULT"), defaults ("(70)"), and why a
  surprising thing is done the way it is.
- Module lists: put a `///` line above each `mod`. rustfmt reorders `mod` lines, which
  strands trailing comments.

## Splitting a big file

When a file passes roughly 500 lines, or mixes unrelated jobs, turn `foo.rs` into a
`foo/` folder:

- `foo/mod.rs` keeps the data types (structs, enums) and the plumbing. The behaviour goes
  into children named by job (`input.rs`, `draw.rs`, `controls.rs`, `commands.rs`…).
- Children start with `use super::*;` and continue the parent's type with `impl Foo { … }`.
  Children can read the parent's private fields. A moved private `fn` becomes `pub(super) fn`.
- Keep outside paths the same with `pub use child::Thing;` in `mod.rs`. Nothing else in the
  tree should need editing.
- Don't call a child `keys` (it clashes with `os::keys`; use `controls`), and don't call a
  module `core`.
- Tests that stay in the parent may need `use super::<child>::<fn>;`.
- A split should change no code beyond visibility and the module lines. Check with
  `git diff`.

## Verifying (every step)

```sh
scripts/check.sh                              # fmt, clippy -D warnings, tests (4 jobs)
cargo doc --no-deps --workspace -q            # must print no warnings
MABEL_SANDBOX=<scratch dir> scripts/sandbox.sh --keep   # run it against a fake Mabel
```

- For changes to what's on screen or to keys, open the sandbox and use it. Run it in tmux
  and drive it with `send-keys`/`capture-pane`. Kill only your own tmux session.
- For a comment-only commit, confirm the diff adds only comment lines:
  `git diff | grep '^[-+]' | grep -vE '^(\+\+\+|---|\+\s*//)'` should print nothing.

## Machine limits

- The PC also runs Mabel's models. Builds use 4 jobs at most (check.sh and the release
  script already cap this). An uncapped LTO build once OOM-killed the model server. Before
  any other release build, check `free -h`.
- Never compile on the deck (a Pi 3B). Release with `../scripts/tui_rs_dist.sh` on the PC;
  devices then offer the update on F12.
- Don't run the Python side's full pytest suite (it OOM-killed the machine). Run only the
  tests you need.
- Anything that writes (chats, vault, inventory, tapes, printer) gets tried in the sandbox
  first.

## Words

Channel = a tab (CH-01 HOME … CH-07 TAPES, then CH-00 SYS, always last). Suite = a channel
holding several apps (LIFE: HER, VAULT, INVENTORY). New programs go into a fitting suite
rather than becoming new tabs. Section = a panel inside a channel. Nav mode / inside = where the keys go.
DreamTape (`.dtape`) = a game for TAPES, written against `crates/mabel-kit`. The deck = the
Pi with the 7" screen.

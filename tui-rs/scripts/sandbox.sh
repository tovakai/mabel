#!/bin/sh
# Try your changed MABEL-OS without touching anything real.
#
#   scripts/sandbox.sh                 build (debug) and run this source tree in a sandbox
#   scripts/sandbox.sh --release       the same, optimised (slower to build, smoother to use)
#   scripts/sandbox.sh --courses       also copy your real Courses/ folder in (a copy: safe)
#   scripts/sandbox.sh --keep          reuse the last sandbox's files instead of starting fresh
#
# What's fake:
# - Mabel: scripts/fake_mabel.py answers instead of the real one (chats, tapes, LIFE). Your real
#   chats, memory and tape repo are never touched. Every message sent is in
#   <sandbox>/fake_mabel_messages.jsonl.
# - Your files: config, vault, saves, downloads, screenshots and the home folder all live in
#   the sandbox folder (printed at the start).
# - Sound: a silent `aplay` stands in, so tracker songs and tapes make no noise.
#   (Pocket Miku: MIDI isn't faked. Don't play TRACKER songs with MIDI instruments.)
#
# What's real: your Rust toolchain (cargo builds work), and the terminal you run it in.
# INVENTORY talks to tk-index at the config's index_url: the sandbox points it at a dead
# address, so INVENTORY stays empty instead of editing your real inventory.
set -eu

here="$(cd "$(dirname "$0")/.." && pwd)"   # tui-rs/
box="${MABEL_SANDBOX:-${TMPDIR:-/tmp}/mabel-os-sandbox}"
port="${MABEL_SANDBOX_PORT:-8797}"
profile=""
courses=0
keep=0
for arg in "$@"; do
    case "$arg" in
        --release) profile="--release" ;;
        --courses) courses=1 ;;
        --keep) keep=1 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

[ "$keep" = 1 ] || rm -rf "$box"
mkdir -p "$box/home/Pictures" "$box/config/mabel-tui" "$box/data" "$box/cache" "$box/vault" "$box/bin"

# A config that points at the fake Mabel. (Only written once, so --keep keeps your edits.)
cfg="$box/config/mabel-tui/config.toml"
if [ ! -f "$cfg" ]; then
    cat > "$cfg" <<EOF
# The sandbox's config: edit freely, nothing here is real.
url = "http://127.0.0.1:$port"
token = "ody_sandbox"
vault = "$box/vault"
journal = "$box/vault/journal"
index_url = "http://127.0.0.1:9"
EOF
fi

# Silent stand-ins for the audio players.
printf '#!/bin/sh\nexec cat > /dev/null\n' > "$box/bin/aplay"
cp "$box/bin/aplay" "$box/bin/pw-cat"
chmod +x "$box/bin/aplay" "$box/bin/pw-cat"

if [ "$courses" = 1 ]; then
    real_vault="$(sed -n 's/^vault *= *"\(.*\)"/\1/p' "$HOME/.config/mabel-tui/config.toml" | sed "s|^~|$HOME|")"
    [ -d "$real_vault/Courses" ] && cp -r "$real_vault/Courses" "$box/vault/" && echo "▌ copied Courses/ into the sandbox vault" >&2
fi

echo "▌ building MABEL-OS (${profile:-debug})" >&2
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
(cd "$here" && cargo build $profile -j 4 --quiet)
bin="$here/target/$([ -n "$profile" ] && echo release || echo debug)/mabel-os"

echo "▌ starting the fake Mabel on :$port" >&2
(cd "$box" && exec python3 -I "$here/scripts/fake_mabel.py" "$port" 2>"$box/fake_mabel.log") &
fake=$!
trap 'kill $fake 2>/dev/null' EXIT INT TERM
sleep 0.5

echo "▌ sandbox: $box   (ctrl+q quits MABEL-OS; the fake Mabel stops with it)" >&2
sleep 1
env HOME="$box/home" \
    RUSTUP_HOME="${RUSTUP_HOME:-$HOME/.rustup}" CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}" \
    XDG_CONFIG_HOME="$box/config" XDG_DATA_HOME="$box/data" XDG_CACHE_HOME="$box/cache" \
    PATH="$box/bin:$PATH" \
    "$bin"

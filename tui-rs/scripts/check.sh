#!/bin/sh
# Is everything still fine after a change? Run this before committing.
#
#   scripts/check.sh          format, lint, test (the whole workspace)
#   scripts/check.sh --quick  skip clippy
#
# 1. cargo fmt     tidies the code's layout (rustfmt.toml: 120 columns)
# 2. cargo clippy  Rust's linter: likely mistakes and simpler ways to write things
# 3. cargo test    every test in the app and the crates
#
# At most 4 compile jobs at once (MABEL_BUILD_JOBS overrides), so a build next to a loaded
# Mabel (Strata, ~17 GB) can't run the PC out of memory.
set -eu
cd "$(dirname "$0")/.."
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
jobs="${MABEL_BUILD_JOBS:-4}"

echo "▌ cargo fmt" >&2
cargo fmt --all
if [ "${1:-}" != "--quick" ]; then
    echo "▌ cargo clippy" >&2
    cargo clippy -j "$jobs" --workspace --all-targets --all-features --quiet -- -D warnings
fi
echo "▌ cargo test" >&2
out="$(mktemp)"
if cargo test -j "$jobs" --workspace --all-features --quiet >"$out" 2>&1; then
    grep -E "^test result" "$out" | awk '{p += $4} END {print "▌ all good ✓ (" p " tests passed)"}' >&2
    rm -f "$out"
else
    grep -vE "^test result: ok" "$out" | tail -40 >&2
    rm -f "$out"
    echo "▌ something failed (above)" >&2
    exit 1
fi

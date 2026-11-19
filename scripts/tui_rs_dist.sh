#!/bin/sh
# Build MABEL-OS in Rust (tui-rs/) for the devices Mabel installs it on, into tui-rs/dist/:
#
#   mabel-os-x86_64    the PC and laptops
#   mabel-os-aarch64   the deck (Pi 3B) and other 64-bit Pis
#
# Mabel serves these (routes/tui_install_routes.py: /tui/rs/…, the "rs" builds in /tui/version),
# so running this is how a Rust change reaches the devices: their header says UPDATE F12.
# Both are linked against glibc 2.36 (Debian 12/13), so one binary runs on every distro since.
# Needs the Rust toolchain in ~/.cargo, cargo-zigbuild and zig.
#
# At most 4 compile jobs at once (MABEL_BUILD_JOBS overrides): a full-speed release build next
# to a loaded Strata (~17 GB) ran the PC out of memory on 2026-11-19 and the kernel killed her.
set -eu
jobs="${MABEL_BUILD_JOBS:-4}"

cd "$(dirname "$0")/../tui-rs"
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
mkdir -p dist
for arch in x86_64 aarch64; do
    target="$arch-unknown-linux-gnu"
    echo "▌ building $arch" >&2
    cargo zigbuild --release --quiet -j "$jobs" --target "$target.2.36"
    cp "target/$target/release/mabel-os" "dist/.mabel-os-$arch.tmp"
    mv -f "dist/.mabel-os-$arch.tmp" "dist/mabel-os-$arch"       # a rename: never half a file
done
ls -l dist/mabel-os-* >&2

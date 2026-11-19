#!/bin/sh
# MABEL-OS installer, served by Mabel (routes/tui_install_routes.py):
#
#   curl -fsSL @@BASE@@/tui/install.sh | sh
#
# Installs MABEL-OS (the Rust build for this machine, tui-rs/) into ~/.local/share/mabel-tui, links
# `mabel` and `mabel-os` into ~/.local/bin and, if this device has no working config yet, pairs it
# with Mabel through its Tailscale login. Needs only sh, curl or wget, and awk/sed.
# Run it again to update (or press F12 in MABEL-OS when it says UPDATE); a working config is kept.
# Builds exist for x86_64 and aarch64 (scripts/tui_rs_dist.sh on the PC).
set -eu

BASE="@@BASE@@"
RS_BUILDS="@@RS_BUILDS@@"                                 # arch:build pairs (scripts/tui_rs_dist.sh)
APP="${XDG_DATA_HOME:-$HOME/.local/share}/mabel-tui"
BIN="$HOME/.local/bin"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/mabel-tui/config.toml"

if [ -t 2 ]; then A=$(printf '\033[38;2;255;176;0m'); D=$(printf '\033[2m'); R=$(printf '\033[0m'); else A=; D=; R=; fi
say() { printf '%s▌%s %s\n' "$A" "$R" "$*" >&2; }
die() { printf '%s▌ %s%s\n' "$A" "$*" "$R" >&2; exit 1; }

fetch() {
    if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
    else die "needs curl or wget"; fi
}

# GET $1 with a bearer token $2; succeeds only on a 2xx.
get_auth() {
    if command -v curl >/dev/null 2>&1; then curl -fsS --max-time 20 -o /dev/null -H "Authorization: Bearer $2" "$1"
    else wget -q -T 20 -O /dev/null --header "Authorization: Bearer $2" "$1"; fi
}

# POST JSON $2 to $1; the answer's body goes to $3, its HTTP status to stdout.
post_json() {
    if command -v curl >/dev/null 2>&1; then
        curl -sS --max-time 30 -o "$3" -w '%{http_code}' -H 'Content-Type: application/json' -d "$2" "$1" || echo 000
    else
        if wget -q -T 30 -O "$3" --content-on-error --header 'Content-Type: application/json' --post-data "$2" "$1"
        then echo 200; else echo 400; fi
    fi
}

# A JSON string field from a flat object (Mabel's answers have no quotes inside the values).
json_field() {
    sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" "$2" | head -n 1
}

# A top-level `key = "value"` from the config (before its first [section]).
conf_value() {
    awk -v key="$1" '
        /^[[:space:]]*\[/ { exit }
        {
            line = $0
            if (match(line, "^[[:space:]]*" key "[[:space:]]*=[[:space:]]*\"")) {
                rest = substr(line, RLENGTH + 1)
                sub(/".*$/, "", rest)
                print rest
                exit
            }
        }' "$CONF"
}

rust_build() {
    case "$(uname -m)" in
        x86_64|amd64) arch=x86_64 ;;
        aarch64|arm64) arch=aarch64 ;;
        *) return 1 ;;
    esac
    for pair in $RS_BUILDS; do
        if [ "${pair%%:*}" = "$arch" ]; then
            echo "$arch ${pair#*:}"
            return 0
        fi
    done
    return 1
}

install_rust() {
    tmp=$1 arch=$2 rs=$3
    say "fetching MABEL-OS ${D}($arch, build $rs)${R}"
    fetch "$BASE/tui/rs/mabel-os-$arch" "$tmp/mabel-os" || die "couldn't download $BASE/tui/rs/mabel-os-$arch (is this device on the tailnet?)"
    chmod 755 "$tmp/mabel-os"
    cp "$tmp/mabel-os" "$APP/mabel-os.new"
    mv -f "$APP/mabel-os.new" "$APP/mabel-os"             # a rename: a running copy keeps going
    ln -sf "$APP/mabel-os" "$BIN/mabel-os"
    ln -sf "$APP/mabel-os" "$BIN/mabel"                   # `mabel` is the Rust MABEL-OS now
    printf '%s\n' "$rs" > "$APP/build-rs"                 # what mabel-os compares with /tui/version (F12)
}

config_works() {
    [ -f "$CONF" ] || return 1
    url=$(conf_value url) token=$(conf_value token)
    [ -n "$url" ] && [ -n "$token" ] || return 1
    get_auth "${url%/}/api/tui/life" "$token" </dev/null 2>/dev/null
}

device_name() {
    hostname 2>/dev/null || uname -n 2>/dev/null || cat /etc/hostname 2>/dev/null || echo device
}

# Ask Mabel for a token through this device's Tailscale login; write url, token and index_url into
# the config and keep everything else in it (the old file is kept as a .bak).
pair() {
    tmp=$1
    device=$(device_name | tr -cd 'A-Za-z0-9._-')
    say "pairing ${D}${device:-device} through your Tailscale login${R}"
    code=$(post_json "$BASE/api/tui/pair" "{\"device\": \"${device:-device}\"}" "$tmp/pair.json")
    if [ "$code" != 200 ]; then
        why=$(json_field detail "$tmp/pair.json" 2>/dev/null || true)
        [ "$code" = 000 ] && die "couldn't reach Mabel at $BASE"
        die "pairing refused: ${why:-HTTP $code}"
    fi
    url=$(json_field url "$tmp/pair.json") token=$(json_field token "$tmp/pair.json")
    index=$(json_field index_url "$tmp/pair.json") name=$(json_field name "$tmp/pair.json")
    [ -n "$url" ] && [ -n "$token" ] || die "Mabel's answer had no url or token"
    mkdir -p "$(dirname "$CONF")"
    : > "$tmp/keep"
    if [ -f "$CONF" ]; then
        cp "$CONF" "$CONF.bak-$(date +%Y%m%d-%H%M%S)"
        awk '
            /^[[:space:]]*\[/ { section = 1 }
            !section && /^[[:space:]]*(url|token|index_url)[[:space:]]*=/ { next }
            { print }' "$CONF" > "$tmp/keep"
    fi
    (
        umask 077
        {
            printf 'url = "%s"\ntoken = "%s"\nindex_url = "%s"\n' "$url" "$token" "$index"
            cat "$tmp/keep"
        } > "$CONF.new"
    )
    chmod 600 "$CONF.new"
    mv -f "$CONF.new" "$CONF"
    say "paired as ${A}$name${R} ${D}(config: $CONF)${R}"
}

main() {
    say "MABEL-OS installer ${D}$BASE${R}"
    mkdir -p "$APP" "$BIN"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    found=$(rust_build) || die "no MABEL-OS build for $(uname -m) here (Mabel makes x86_64 and aarch64)"
    install_rust "$tmp" ${found}
    if config_works; then
        say "kept your config ${D}($CONF)${R}"
    else
        pair "$tmp"
    fi
    command -v mpv >/dev/null 2>&1 || say "${D}AUDIO plays through mpv, which isn't installed here${R}"
    case ":$PATH:" in *":$BIN:"*) ;; *) say "${D}$BIN isn't on your PATH; add it, or run $BIN/mabel${R}" ;; esac
    say "done. Run ${A}mabel${R}"
}

main "$@"

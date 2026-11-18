#!/bin/sh
# MABEL-OS installer, served by Mabel (routes/tui_install_routes.py):
#
#   curl -fsSL @@BASE@@/tui/install.sh | sh
#
# Installs the TUI into ~/.local/share/mabel-tui, links `mabel` into ~/.local/bin and, if
# this device has no working config yet, pairs it with Mabel through its Tailscale login.
# Run it again to update (or press F12 in MABEL-OS when it says UPDATE); a working config is kept.
set -eu

BASE="@@BASE@@"
WHEEL="@@WHEEL@@"
BUILD="@@BUILD@@"
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

find_python() {
    for c in python3.14 python3.13 python3.12 python3; do
        if command -v "$c" >/dev/null 2>&1 &&
           "$c" -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
            command -v "$c"; return 0
        fi
    done
    return 1
}

install_app() {
    tmp=$1
    say "fetching the TUI ${D}($WHEEL)${R}"
    fetch "$BASE/tui/$WHEEL" "$tmp/$WHEEL" || die "couldn't download $BASE/tui/$WHEEL (is this device on the tailnet?)"
    if [ -x "$APP/venv/bin/python" ] &&
       ! "$APP/venv/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
        rm -rf "$APP/venv"                                     # an old interpreter: start over
    fi
    if command -v uv >/dev/null 2>&1; then
        [ -x "$APP/venv/bin/python" ] || uv venv -q --python '>=3.12' "$APP/venv" </dev/null
        say "installing ${D}(uv)${R}"
        uv pip install -q --python "$APP/venv/bin/python" --reinstall-package mabel-tui "$tmp/$WHEEL" </dev/null
    elif py=$(find_python); then
        [ -x "$APP/venv/bin/python" ] || "$py" -m venv "$APP/venv" </dev/null
        say "installing ${D}(pip; the first time takes a while on a Pi)${R}"
        pip="$APP/venv/bin/python -m pip -q --disable-pip-version-check"
        $pip install --force-reinstall --no-deps "$tmp/$WHEEL" </dev/null
        $pip install "$tmp/$WHEEL" </dev/null                  # its dependencies, if any are missing
    else
        die "needs Python 3.12+ with venv (Debian/Pi OS: sudo apt install python3-venv), or uv:
  curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi
    ln -sf "$APP/venv/bin/mabel" "$BIN/mabel"
    printf '%s\n' "$BUILD" > "$APP/build"                 # what the TUI compares with /tui/version (F12)
}

config_works() {
    [ -f "$CONF" ] || return 1
    "$APP/venv/bin/python" - "$CONF" <<'PY' </dev/null
import sys, tomllib, urllib.request
try:
    c = tomllib.load(open(sys.argv[1], "rb"))
    req = urllib.request.Request(str(c["url"]).rstrip("/") + "/api/tui/life",
                                 headers={"Authorization": "Bearer " + str(c["token"])})
    sys.exit(0 if urllib.request.urlopen(req, timeout=20).status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

device_name() {
    hostname 2>/dev/null || uname -n 2>/dev/null || cat /etc/hostname 2>/dev/null || echo device
}

pair() {
    device=$(device_name)
    say "pairing ${D}$device through your Tailscale login${R}"
    "$APP/venv/bin/python" - "$CONF" "$BASE" "$device" <<'PY'
import json, os, re, sys, time, urllib.error, urllib.request
conf, base, device = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request(base + "/api/tui/pair", data=json.dumps({"device": device}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
try:
    got = json.load(urllib.request.urlopen(req, timeout=30))
except urllib.error.HTTPError as e:
    try:
        why = json.load(e).get("detail")
    except Exception:
        why = e.reason
    sys.exit(f"pairing refused: {why}")
except Exception as e:
    sys.exit(f"couldn't reach Mabel: {e}")
os.makedirs(os.path.dirname(conf), exist_ok=True)
keep = []
if os.path.exists(conf):                                   # keep everything but where she is and the token
    old = open(conf, encoding="utf-8").read()
    os.replace(conf, conf + time.strftime(".bak-%Y%m%d-%H%M%S"))
    top = True
    for line in old.splitlines():
        top = top and not line.lstrip().startswith("[")
        if top and re.match(r"\s*(url|token|index_url)\s*=", line):
            continue
        keep.append(line)
lines = [f'url = "{got["url"]}"', f'token = "{got["token"]}"', f'index_url = "{got["index_url"]}"']
fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("\n".join(lines + keep).rstrip() + "\n")
print(got["name"])
PY
}

main() {
    say "MABEL-OS installer ${D}$BASE${R}"
    mkdir -p "$APP" "$BIN"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    install_app "$tmp"
    if config_works; then
        say "kept your config ${D}($CONF)${R}"
    else
        name=$(pair) || die "not paired. Is this device signed in to Tailscale as you?"
        say "paired as ${A}$name${R} ${D}(config: $CONF)${R}"
    fi
    command -v mpv >/dev/null 2>&1 || say "${D}AUDIO plays through mpv, which isn't installed here${R}"
    case ":$PATH:" in *":$BIN:"*) ;; *) say "${D}$BIN isn't on your PATH; add it, or run $BIN/mabel${R}" ;; esac
    say "done. Run ${A}mabel${R}"
}

main "$@"

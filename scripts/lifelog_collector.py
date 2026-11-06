#!/usr/bin/env python3
"""Mabel life-log collector: tells Mabel what Anthon is doing on this device.

Standard library only (runs on Arch, SteamOS, any Linux with python3).

    python3 lifelog_collector.py install --url https://mabel.example.ts.net --device laptop
    python3 lifelog_collector.py run          # what the systemd user service runs
    python3 lifelog_collector.py once         # one sample, printed (no sending)

Records: the focused app/window (Hyprland, or X11 via xdotool), media playing
(MPRIS via playerctl), the running Steam game (Steam's own registry.vdf, local
only), and new browser visits (Firefox and Chromium-family history) as page
title + site only, never the full URL. Nothing is recorded while the device is
idle. Private/incognito windows never reach browser history, so never here.
Spans queue locally while offline and are sent when Mabel is reachable.
"""
from __future__ import annotations

import argparse
import getpass
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "mabel-collector" / "config.json"
STATE = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "mabel-collector"
INSTALL_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "mabel-collector"
SAMPLE_S = 30
SEND_S = 120
GAP_S = 3 * SAMPLE_S                 # a longer gap ends a span
QUEUE_LIMIT = 20000
IDLE_FILE = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "mabel" / "idle"

FIREFOX_PROFILES = ["~/.mozilla/firefox/*/places.sqlite", "~/.config/mozilla/firefox/*/places.sqlite",
                    "~/.var/app/org.mozilla.firefox/.mozilla/firefox/*/places.sqlite",
                    "~/.librewolf/*/places.sqlite", "~/.var/app/io.gitlab.librewolf-community/.librewolf/*/places.sqlite",
                    "~/.zen/*/places.sqlite", "~/.waterfox/*/places.sqlite"]
CHROMIUM_PROFILES = ["~/.config/google-chrome/*/History", "~/.config/chromium/*/History",
                     "~/.config/BraveSoftware/Brave-Browser/*/History", "~/.config/vivaldi/*/History",
                     "~/.var/app/com.google.Chrome/config/google-chrome/*/History",
                     "~/.var/app/org.chromium.Chromium/config/chromium/*/History"]
STEAM_ROOTS = ["~/.steam/steam", "~/.local/share/Steam", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam"]
STEAM_REGISTRY = ["~/.steam/registry.vdf", "~/.var/app/com.valvesoftware.Steam/.steam/registry.vdf"]


def now() -> datetime:
    return datetime.now(timezone.utc)


def run(cmd: list[str], timeout: float = 3) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_env())
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _env() -> dict:
    env = dict(os.environ)
    runtime = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if "HYPRLAND_INSTANCE_SIGNATURE" not in env:
        dirs = sorted(glob.glob(f"{runtime}/hypr/*/"), key=os.path.getmtime)
        if dirs:
            env["HYPRLAND_INSTANCE_SIGNATURE"] = Path(dirs[-1]).name
    if "WAYLAND_DISPLAY" not in env and glob.glob(f"{runtime}/wayland-*"):
        env["WAYLAND_DISPLAY"] = Path(sorted(glob.glob(f"{runtime}/wayland-[0-9]"))[0]).name \
            if glob.glob(f"{runtime}/wayland-[0-9]") else "wayland-1"
    env.setdefault("DISPLAY", ":0")
    return env


# --- samplers ------------------------------------------------------------------------------

def is_idle() -> bool:
    try:
        state = IDLE_FILE.read_text().split()[0]
        return state == "idle"
    except (OSError, IndexError):
        return False


def sample_focus() -> dict | None:
    if shutil.which("hyprctl"):
        raw = run(["hyprctl", "activewindow", "-j"])
        try:
            win = json.loads(raw) if raw.strip().startswith("{") else {}
        except ValueError:
            win = {}
        if win.get("class") or win.get("title"):
            return {"app": win.get("class") or win.get("initialClass") or "", "title": win.get("title") or ""}
    if shutil.which("xdotool"):
        wid = run(["xdotool", "getactivewindow"]).strip()
        if wid:
            title = run(["xdotool", "getwindowname", wid]).strip()
            klass = run(["xprop", "-id", wid, "WM_CLASS"]).split('"')
            return {"app": klass[-2] if len(klass) >= 3 else "", "title": title}
    return None


def sample_media() -> dict | None:
    if not shutil.which("playerctl"):
        return None
    for line in run(["playerctl", "-a", "metadata", "--format",
                     "{{status}}\t{{playerName}}\t{{artist}}\t{{title}}"]).splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[0] == "Playing" and parts[3]:
            title = f"{parts[2]} – {parts[3]}" if parts[2] else parts[3]
            return {"app": parts[1], "title": title}
    return None


def _vdf_value(text: str, key: str) -> str | None:
    import re
    m = re.search(r'"%s"\s+"([^"]*)"' % re.escape(key), text, re.I)
    return m.group(1) if m else None


def steam_game_name(appid: str) -> str:
    for root in STEAM_ROOTS:
        root = Path(os.path.expanduser(root))
        libs = [root]
        try:
            text = (root / "steamapps" / "libraryfolders.vdf").read_text(errors="ignore")
            import re
            libs += [Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', text)]
        except OSError:
            pass
        for lib in libs:
            manifest = lib / "steamapps" / f"appmanifest_{appid}.acf"
            try:
                return _vdf_value(manifest.read_text(errors="ignore"), "name") or f"Steam app {appid}"
            except OSError:
                continue
    return f"Steam app {appid}"


_GENERIC_EXE = {"game", "launcher", "start", "play", "setup", "main", "run", "client", "app"}


def name_from_command(line: str) -> str | None:
    """'.../Some Game/SomeGameVR.exe' -> 'SomeGameVR'; generic exe names fall back to the folder."""
    import re
    m = re.search(r"/([^/]*?)/([^/]+?)\.exe\b[^/]*$", line, re.I)
    if not m:
        return None
    stem, folder = m.group(2), re.sub(r"\[[^\]]*\]\s*", "", m.group(1)).strip()
    return folder if stem.lower() in _GENERIC_EXE and folder else stem


# Launch plumbing and SteamVR's own helpers: none of these mean a game is running. SteamVR's
# dashboard can stay parented under a game's launch long after the game has exited.
_NOT_A_GAME = (r"/reaper\b", r"srt-bwrap", r"srt-logger", r"pv-adverb", r"pressure-vessel", r"steam-runtime",
               r"SteamLinuxRuntime", r"/SteamVR/")


def launch_has_game(pid: int, rows: list[tuple[int, int, str]]) -> bool:
    """True if something other than launch plumbing or SteamVR runs under this Steam launch."""
    import re
    children: dict[int, list[tuple[int, str]]] = {}
    for child, parent, args in rows:
        children.setdefault(parent, []).append((child, args))
    skip = re.compile("|".join(_NOT_A_GAME))
    stack = [pid]
    while stack:
        for child, args in children.get(stack.pop(), []):
            if not skip.search(args):
                return True
            stack.append(child)
    return False


def sample_game() -> dict | None:
    # Every Steam launch (native or Proton) runs under "reaper SteamLaunch AppId=<id>".
    import re
    rows: list[tuple[int, int, str]] = []
    for line in run(["ps", "-eo", "pid=,ppid=,args="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    leftover: set[str] = set()             # launches with no game left in them
    for pid, _parent, line in rows:
        m = re.search(r"\bSteamLaunch\b.*?\bAppId=(\d+)", line)
        if m and m.group(1) not in ("0", "250820"):        # 250820 = SteamVR itself
            if not launch_has_game(pid, rows):
                leftover.add(m.group(1))
                continue
            name = steam_game_name(m.group(1))
            if name.startswith("Steam app "):              # a non-Steam shortcut: name it after its executable
                name = name_from_command(line) or name
            return {"app": "steam", "title": name, "detail": {"appid": m.group(1)}}
    for path in STEAM_REGISTRY:
        try:
            text = Path(os.path.expanduser(path)).read_text(errors="ignore")
        except OSError:
            continue
        appid = _vdf_value(text, "RunningAppID")
        if appid and appid != "0" and appid not in leftover:   # Steam still counts a leftover launch as running
            return {"app": "steam", "title": steam_game_name(appid), "detail": {"appid": appid}}
    return None


# --- browser history -----------------------------------------------------------------------

def _copy_db(path: Path) -> Path | None:
    tmp = Path(tempfile.mkdtemp(prefix="mabel-collector-"))
    try:
        for suffix in ("", "-wal", "-shm", "-journal"):
            src = Path(str(path) + suffix)
            if src.exists():
                shutil.copy2(src, tmp / (path.name + suffix))
        return tmp / path.name
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        return None


def _site(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host.removeprefix("www.").split(":")[0]


def browser_visits(cursors: dict) -> list[dict]:
    spans = []
    first_run_floor = now() - timedelta(hours=6)          # never backfill old history
    for pattern, kind in [(p, "firefox") for p in FIREFOX_PROFILES] + [(p, "chromium") for p in CHROMIUM_PROFILES]:
        for path in glob.glob(os.path.expanduser(pattern)):
            key = hashlib.sha256(path.encode()).hexdigest()[:12]
            copy = _copy_db(Path(path))
            if not copy:
                continue
            try:
                db = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
                if kind == "firefox":
                    floor = int(cursors.get(key) or first_run_floor.timestamp() * 1_000_000)
                    rows = db.execute("""SELECT v.visit_date, p.url, p.title FROM moz_historyvisits v
                                         JOIN moz_places p ON p.id = v.place_id WHERE v.visit_date > ?
                                         ORDER BY v.visit_date LIMIT 500""", (floor,)).fetchall()
                    to_time = lambda t: datetime.fromtimestamp(t / 1_000_000, timezone.utc)
                else:
                    epoch = 11644473600 * 1_000_000            # Chromium: µs since 1601
                    floor = int(cursors.get(key) or first_run_floor.timestamp() * 1_000_000 + epoch)
                    rows = db.execute("""SELECT v.visit_time, u.url, u.title FROM visits v JOIN urls u ON u.id = v.url
                                         WHERE v.visit_time > ? ORDER BY v.visit_time LIMIT 500""", (floor,)).fetchall()
                    to_time = lambda t: datetime.fromtimestamp((t - epoch) / 1_000_000, timezone.utc)
                db.close()
            except sqlite3.Error:
                rows = []
            finally:
                shutil.rmtree(copy.parent, ignore_errors=True)
            for stamp, url, title in rows:
                site = _site(url or "")
                if not site or (url or "").startswith(("about:", "chrome:", "file:", "moz-extension:")):
                    continue
                at = to_time(stamp).isoformat()
                spans.append({"id": f"browse:{key}:{stamp}", "kind": "browse", "start": at, "end": at,
                              "title": (title or "")[:200], "site": site})   # title + site only, never the URL
            if rows:
                cursors[key] = rows[-1][0]
    return spans


# --- span bookkeeping ----------------------------------------------------------------------

class Spans:
    def __init__(self):
        self.open: dict[str, dict] = {}

    def observe(self, kind: str, value: dict | None, at: datetime) -> list[dict]:
        """Update the open span of ``kind``; returns spans to send (closed or extended)."""
        current = self.open.get(kind)
        same = current and value and current["app"] == value.get("app", "") and current["title"] == value.get("title", "")
        fresh = current and (at - datetime.fromisoformat(current["end"])).total_seconds() <= GAP_S
        if same and fresh:
            current["end"] = at.isoformat()
            return [dict(current)]
        out = []
        if current:
            out.append(dict(current))
            del self.open[kind]
        if value:
            span = {"id": f"{kind}:{at.isoformat()}", "kind": kind, "start": at.isoformat(), "end": at.isoformat(),
                    "app": value.get("app", ""), "title": value.get("title", ""), "detail": value.get("detail", {})}
            self.open[kind] = span
            out.append(dict(span))
        return out


# --- sending -------------------------------------------------------------------------------

def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        sys.exit(f"no config at {CONFIG}; run: {sys.argv[0]} install --url ... --device ...")


def enqueue(spans: list[dict]) -> None:
    if not spans:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    queue = STATE / "queue.jsonl"
    with queue.open("a") as handle:
        for span in spans:
            handle.write(json.dumps(span) + "\n")
    lines = queue.read_text().splitlines()
    if len(lines) > QUEUE_LIMIT:
        queue.write_text("\n".join(lines[-QUEUE_LIMIT:]) + "\n")


def flush(config: dict) -> bool:
    queue = STATE / "queue.jsonl"
    try:
        lines = queue.read_text().splitlines()
    except OSError:
        return True
    latest: dict[str, dict] = {}
    for line in lines:                     # keep only the newest state of each span
        try:
            span = json.loads(line)
            latest[span["id"]] = span
        except (ValueError, KeyError):
            continue
    spans = list(latest.values())
    for i in range(0, len(spans), 400):
        body = json.dumps({"device": config["device"], "spans": spans[i:i + 400]}).encode()
        req = urllib.request.Request(config["url"].rstrip("/") + "/api/lifelog/ingest", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {config['token']}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status >= 300:
                    return False
        except (urllib.error.URLError, OSError):
            return False
    queue.write_text("")
    return True


def load_cursors() -> dict:
    try:
        return json.loads((STATE / "cursors.json").read_text())
    except (OSError, ValueError):
        return {}


def save_cursors(cursors: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "cursors.json").write_text(json.dumps(cursors))


def sample_all(spans: Spans, at: datetime) -> list[dict]:
    idle = is_idle()
    out = []
    out += spans.observe("focus", None if idle else sample_focus(), at)
    out += spans.observe("media", sample_media(), at)
    out += spans.observe("game", sample_game(), at)
    return out


def cmd_run() -> None:
    config = load_config()
    spans, cursors = Spans(), load_cursors()
    last_send = 0.0
    while True:
        at = now()
        enqueue(sample_all(spans, at))
        if time.monotonic() - last_send >= SEND_S:
            enqueue(browser_visits(cursors))
            save_cursors(cursors)
            flush(config)
            last_send = time.monotonic()
        time.sleep(SAMPLE_S)


def cmd_once() -> None:
    print(json.dumps({"idle": is_idle(), "focus": sample_focus(), "media": sample_media(), "game": sample_game()},
                     indent=2, ensure_ascii=False))


def cmd_install(url: str, device: str, token: str | None) -> None:
    token = token or os.environ.get("MABEL_COLLECTOR_TOKEN") or getpass.getpass("lifelog:write API token (input hidden): ")
    token = token.strip()
    if not token.startswith("ody_"):
        sys.exit("that doesn't look like a Mabel API token (ody_...)")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"url": url.rstrip("/"), "device": device, "token": token}, handle)
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    target = INSTALL_DIR / "collector.py"
    shutil.copy2(Path(__file__).resolve(), target)
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "mabel-collector.service").write_text(f"""[Unit]
Description=Mabel life-log collector
After=graphical-session.target network-online.target

[Service]
ExecStart={sys.executable} {target} run
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "mabel-collector.service"], check=False)
    print(f"installed: {device} -> {url}; check with: systemctl --user status mabel-collector")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    inst = sub.add_parser("install")
    inst.add_argument("--url", required=True)
    inst.add_argument("--device", required=True)
    inst.add_argument("--token")
    sub.add_parser("run")
    sub.add_parser("once")
    args = parser.parse_args()
    if args.cmd == "install":
        cmd_install(args.url, args.device, args.token)
    elif args.cmd == "run":
        cmd_run()
    else:
        cmd_once()


if __name__ == "__main__":
    main()

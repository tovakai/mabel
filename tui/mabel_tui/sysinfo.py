"""SYS's backend: the device MABEL-OS runs on, through the usual Linux tools. No Textual here.

Everything acts on *this* machine (the deck, the laptop, the PC) via NetworkManager (nmcli),
BlueZ (bluetoothctl), PipeWire (wpctl) or ALSA (amixer), systemd (timedatectl, localectl,
systemctl) and /proc and /sys. Commands that need root are tried with `sudo -n` (the deck has
passwordless sudo); where that isn't allowed the caller gets an honest error to show.
Parsers take command output as text, so they're tested against real output.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

TIMEOUT = 8


def run(args: list[str], timeout: float = TIMEOUT, sudo: bool = False, input: str | None = None) -> tuple[int, str]:
    """(returncode, stdout+stderr). Never raises: a missing tool is rc 127."""
    if sudo and os.geteuid() != 0:
        args = ["sudo", "-n", *args]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input,
                           env={**os.environ, "LC_ALL": "C"})
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{args[0]} isn't installed"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]} took too long"


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _short_error(out: str) -> str:
    line = next((l for l in out.splitlines() if l.strip()), out)
    if "a password is required" in line or "sudo:" in line:
        return "needs root (no passwordless sudo here)"
    return line[:120]


# --- status: a fastfetch for MABEL-OS -----------------------------------------------------------

THROTTLE_BITS = {0: "under-voltage now", 1: "CPU capped now", 2: "throttled now", 3: "too hot now",
                 16: "under-voltage since boot", 17: "capped since boot", 18: "throttled since boot",
                 19: "too hot since boot"}


def decode_throttled(value: str) -> list[str]:
    """vcgencmd get_throttled → plain words (the deck's 0x50005: under-voltage now and since boot)."""
    m = re.search(r"0x([0-9a-fA-F]+)", value or "")
    if not m:
        return []
    bits = int(m.group(1), 16)
    return [text for bit, text in THROTTLE_BITS.items() if bits & (1 << bit)]


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _os_name() -> str:
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return platform.system()


def _cpu() -> str:
    text = _read("/proc/cpuinfo")
    for key in ("model name", "Model", "Hardware"):
        m = re.search(rf"^{key}\s*:\s*(.+)$", text, re.M)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).replace("(R)", "").replace("(TM)", "").strip()
    return platform.machine()


def _uptime() -> str:
    try:
        s = int(float(_read("/proc/uptime").split()[0]))
    except (ValueError, IndexError):
        return "?"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    return (f"{d} d " if d else "") + f"{h} h {s // 60} min"


def _memory() -> tuple[int, int]:
    info = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        info[k] = int(v.split()[0]) if v.split() else 0
    total = info.get("MemTotal", 0)
    return (total - info.get("MemAvailable", 0)) // 1024, total // 1024


def _temperature() -> float | None:
    best = None
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        kind = _read(str(zone / "type"))
        t = _read(str(zone / "temp"))
        if t.lstrip("-").isdigit() and (best is None or kind in ("x86_pkg_temp", "cpu-thermal", "Tctl")):
            best = int(t) / 1000
            if kind in ("x86_pkg_temp", "cpu-thermal", "Tctl"):
                break
    return best


def _battery() -> str | None:
    for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        cap, status = _read(str(bat / "capacity")), _read(str(bat / "status"))
        if cap:
            return f"{cap}% ({status.lower() or '?'})"
    return None


def _addresses() -> list[tuple[str, str]]:
    rc, out = run(["ip", "-4", "-br", "addr"])
    rows = []
    for line in out.splitlines() if rc == 0 else []:
        parts = line.split()
        if len(parts) >= 3 and parts[0] != "lo" and not parts[0].startswith(("docker", "br-", "veth")):
            rows.append((parts[0], parts[2].split("/")[0]))
    return rows


def _gpu() -> str | None:
    if not have("nvidia-smi"):
        return None
    rc, out = run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"])
    if rc or not out:
        return None
    name, used, total = [x.strip() for x in out.splitlines()[0].split(",")]
    return f"{name.replace('NVIDIA ', '')} · {int(used) / 1024:.1f}/{int(total) / 1024:.0f} GB"


def _tailnet() -> str | None:
    if not have("tailscale"):
        return None
    rc, out = run(["tailscale", "status", "--self", "--json"], timeout=4)
    try:
        me = json.loads(out)["Self"]
        return f"{me['DNSName'].rstrip('.').split('.')[0]} · {me['TailscaleIPs'][0]}"
    except (ValueError, KeyError, IndexError, TypeError):
        return None


@dataclass
class Facts:
    rows: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def facts() -> Facts:
    """The status card: one (label, value) per line, plus warnings worth a red line."""
    f = Facts()
    used, total = _memory()
    try:
        disk = shutil.disk_usage("/")
        disk_text = f"{(disk.total - disk.free) / 1e9:.0f} / {disk.total / 1e9:.0f} GB"
    except OSError:
        disk_text = "?"
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    temp = _temperature()
    f.rows += [
        ("host", f"{os.environ.get('USER', '?')}@{socket.gethostname()}"),
        ("os", _os_name()),
        ("kernel", platform.release()),
        ("uptime", _uptime()),
        ("cpu", f"{_cpu()} · {os.cpu_count()} cores · load {load:.2f}"),
        ("memory", f"{used} / {total} MB"),
        ("disk", disk_text),
    ]
    if temp is not None:
        f.rows.append(("temp", f"{temp:.0f} °C"))
        if temp >= 80:
            f.warnings.append(f"running hot: {temp:.0f} °C")
    gpu = _gpu()
    if gpu:
        f.rows.append(("gpu", gpu))
    battery = _battery()
    if battery:
        f.rows.append(("battery", battery))
    tail = _tailnet()
    for dev, addr in _addresses():
        if dev == "tailscale0" and tail:
            continue                                   # the tailnet line below says it with the name
        f.rows.append(("net" if dev != "tailscale0" else "tailnet", f"{addr} ({dev})"))
    if tail:
        f.rows.append(("tailnet", tail))
    if have("vcgencmd"):
        problems = decode_throttled(run(["vcgencmd", "get_throttled"])[1])
        if problems:
            f.warnings.append("power: " + ", ".join(problems) + " (a thicker cable or the official supply helps)")
    return f


# --- Wi-Fi (NetworkManager) ---------------------------------------------------------------------

def split_terse(line: str) -> list[str]:
    """nmcli -t fields: ':' separates, '\\:' is a literal colon (in SSIDs and MACs)."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


@dataclass
class Network:
    ssid: str
    signal: int
    security: str
    active: bool
    known: bool = False


def parse_wifi_list(out: str, known: set[str]) -> list[Network]:
    best: dict[str, Network] = {}
    for line in out.splitlines():
        parts = split_terse(line)
        if len(parts) < 4 or not parts[1]:
            continue
        net = Network(parts[1], int(parts[2] or 0), parts[3] or "open", parts[0] == "*", parts[1] in known)
        if net.ssid not in best or net.active or net.signal > best[net.ssid].signal:
            best[net.ssid] = net
    return sorted(best.values(), key=lambda n: (not n.active, not n.known, -n.signal))


def wifi_radio() -> tuple[bool, bool]:
    """(hardware enabled, software enabled)."""
    rc, out = run(["nmcli", "-t", "-f", "WIFI-HW,WIFI", "radio"])
    parts = split_terse(out.splitlines()[0]) if rc == 0 and out else ["missing", "disabled"]
    return parts[0] == "enabled", len(parts) > 1 and parts[1] == "enabled"


def set_wifi_radio(on: bool) -> tuple[bool, str]:
    rc, out = run(["nmcli", "radio", "wifi", "on" if on else "off"])
    return rc == 0, out or ("Wi-Fi on" if on else "Wi-Fi off")


def known_wifi() -> set[str]:
    rc, out = run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    return {split_terse(l)[0] for l in out.splitlines() if l.endswith(":802-11-wireless")} if rc == 0 else set()


def wifi_networks(rescan: bool = True) -> list[Network]:
    rc, out = run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                   "--rescan", "yes" if rescan else "no"], timeout=20)
    return parse_wifi_list(out, known_wifi()) if rc == 0 else []


def wifi_connect(ssid: str, password: str | None = None) -> tuple[bool, str]:
    args = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    rc, out = run(args, timeout=45)
    return rc == 0, "connected to " + ssid if rc == 0 else _short_error(out)


def wifi_forget(ssid: str) -> tuple[bool, str]:
    rc, out = run(["nmcli", "connection", "delete", "id", ssid])
    return rc == 0, f"forgot {ssid}" if rc == 0 else _short_error(out)


def wired() -> list[tuple[str, str, str]]:
    """(device, type, state) for ethernet and the tailnet."""
    rc, out = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"])
    rows = [tuple(split_terse(l)[:3]) for l in out.splitlines()] if rc == 0 else []
    return [r for r in rows if len(r) == 3 and r[1] in ("ethernet", "tun", "wifi") and r[2] != "unmanaged"]


# --- Bluetooth (BlueZ) --------------------------------------------------------------------------

@dataclass
class Device:
    mac: str
    name: str
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    icon: str = ""


def parse_bt_show(out: str) -> dict:
    get = lambda k: (re.search(rf"^\s*{k}:\s*(.+)$", out, re.M) or [None, ""])[1].strip()
    return {"name": get("Name"), "powered": get("Powered") == "yes", "state": get("PowerState"),
            "blocked": "blocked" in get("PowerState"), "discoverable": get("Discoverable") == "yes",
            "pairable": get("Pairable") == "yes", "present": bool(get("Name"))}


def parse_bt_devices(out: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in re.finditer(r"^Device ([0-9A-F:]{17}) (.*)$", out, re.M)]


def parse_bt_info(mac: str, name: str, out: str) -> Device:
    yes = lambda k: re.search(rf"^\s*{k}: yes", out, re.M) is not None
    icon = (re.search(r"^\s*Icon: (.+)$", out, re.M) or [None, ""])[1]
    return Device(mac, name, yes("Paired"), yes("Trusted"), yes("Connected"), icon)


def bt_state() -> dict:
    return parse_bt_show(run(["bluetoothctl", "show"])[1])


def bt_devices() -> list[Device]:
    devs = []
    for mac, name in parse_bt_devices(run(["bluetoothctl", "devices"])[1]):
        devs.append(parse_bt_info(mac, name, run(["bluetoothctl", "info", mac])[1]))
    return sorted(devs, key=lambda d: (not d.connected, not d.paired, d.name.lower()))


def bt_unblock() -> tuple[bool, str]:
    """A soft rfkill block (the deck's "off-blocked"): clear it through /sys, as root."""
    blocked = [p for p in Path("/sys/class/rfkill").glob("rfkill*")
               if _read(str(p / "type")) == "bluetooth" and _read(str(p / "soft")) == "1"]
    for p in blocked:
        rc, out = run(["tee", str(p / "soft")], sudo=True, input="0")
        if rc:
            return False, _short_error(out)
    return True, "unblocked" if blocked else "not blocked"


def bt_power(on: bool) -> tuple[bool, str]:
    if on and bt_state().get("blocked"):
        ok, msg = bt_unblock()
        if not ok:
            return False, msg
        time.sleep(0.5)
    rc, out = run(["bluetoothctl", "power", "on" if on else "off"])
    ok = rc == 0 and "fail" not in out.lower()
    return ok, ("Bluetooth on" if on else "Bluetooth off") if ok else _short_error(out)


def bt_scan(seconds: int = 8) -> None:
    run(["bluetoothctl", "--timeout", str(seconds), "scan", "on"], timeout=seconds + 5)


def bt_do(action: str, mac: str) -> tuple[bool, str]:
    """pair (then trust and connect) · connect · disconnect · remove."""
    if action == "pair":
        rc, out = run(["bluetoothctl", "--timeout", "20", "pair", mac], timeout=30)
        if rc and "AlreadyExists" not in out:
            return False, _short_error(out)
        run(["bluetoothctl", "trust", mac])
        action = "connect"
    rc, out = run(["bluetoothctl", action, mac], timeout=25)
    ok = rc == 0 and "fail" not in out.lower()
    return ok, f"{action}: done" if ok else _short_error(out)


# --- audio ----------------------------------------------------------------------------------------

@dataclass
class Output:
    id: str
    name: str
    default: bool


def parse_wpctl_sinks(status: str) -> list[Output]:
    """The first Sinks block of `wpctl status` (Audio): ' │  *   55. Built-in Audio Analog Stereo  [vol: 0.45]'.
    '*' marks the default; the block ends at its first empty tree line."""
    sinks, inside = [], False
    for line in status.splitlines():
        clean = re.sub(r"[│├└─]", " ", line).strip()
        if not inside:
            inside = clean == "Sinks:"
            continue
        if not clean:
            break
        m = re.match(r"^(\*)?\s*(\d+)\.\s+(.+?)\s*(\[vol:[^\]]*\])?$", clean)
        if m:
            sinks.append(Output(m.group(2), m.group(3).strip(), bool(m.group(1))))
    return sinks


def parse_alsa_cards(aplay_l: str) -> list[Output]:
    seen, cards = set(), []
    for m in re.finditer(r"^card (\d+): (\S+) \[([^\]]+)\]", aplay_l, re.M):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            cards.append(Output(m.group(1), m.group(3), False))
    return cards


def parse_amixer(out: str) -> tuple[int | None, bool]:
    m = re.search(r"\[(\d+)%\].*?(?:\[(on|off)\])?\s*$", out, re.M)
    return (int(m.group(1)), m.group(2) == "off") if m else (None, False)


ASOUNDRC = Path.home() / ".asoundrc"


class Audio:
    """PipeWire (wpctl) where it runs, else plain ALSA (amixer, ~/.asoundrc for the default card)."""

    def __init__(self):
        self.pipewire = have("wpctl") and run(["wpctl", "status"], timeout=3)[0] == 0
        self.backend = "pipewire" if self.pipewire else ("alsa" if have("amixer") else "none")

    def outputs(self) -> list[Output]:
        if self.pipewire:
            return parse_wpctl_sinks(run(["wpctl", "status"])[1])
        cards = parse_alsa_cards(run(["aplay", "-l"])[1])
        current = self._alsa_default()
        for c in cards:
            c.default = c.id == current
        return cards

    def _alsa_default(self) -> str:
        m = re.search(r"defaults\.pcm\.card\s+(\d+)", _read(str(ASOUNDRC)))
        return m.group(1) if m else "0"

    def _alsa_control(self, card: str) -> str | None:
        out = run(["amixer", "-c", card, "scontrols"])[1]
        names = re.findall(r"Simple mixer control '([^']+)'", out)
        return next((n for n in ("Master", "PCM", "Speaker", "Headphone", "HDMI") if n in names),
                    names[0] if names else None)

    def volume(self) -> tuple[int | None, bool]:
        if self.pipewire:
            out = run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])[1]
            m = re.search(r"Volume:\s*([\d.]+)", out)
            return (round(float(m.group(1)) * 100), "MUTED" in out) if m else (None, False)
        card = self._alsa_default()
        ctl = self._alsa_control(card)
        return parse_amixer(run(["amixer", "-c", card, "sget", ctl])[1]) if ctl else (None, False)

    def change(self, step: int) -> tuple[bool, str]:
        if self.pipewire:
            sign = "+" if step > 0 else "-"
            rc, out = run(["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", f"{abs(step)}%{sign}"])
        else:
            card = self._alsa_default()
            ctl = self._alsa_control(card)
            if not ctl:
                return False, "this card has no volume control"
            rc, out = run(["amixer", "-c", card, "sset", ctl, f"{abs(step)}%{'+' if step > 0 else '-'}"])
        return rc == 0, out if rc else ""

    def toggle_mute(self) -> tuple[bool, str]:
        if self.pipewire:
            rc, out = run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
        else:
            card = self._alsa_default()
            ctl = self._alsa_control(card)
            rc, out = run(["amixer", "-c", card, "sset", ctl, "toggle"]) if ctl else (1, "no control")
        return rc == 0, out if rc else ""

    def set_default(self, output: Output) -> tuple[bool, str]:
        if self.pipewire:
            rc, out = run(["wpctl", "set-default", output.id])
            return rc == 0, f"sound out: {output.name}" if rc == 0 else _short_error(out)
        text = f"# written by MABEL-OS SYS\ndefaults.pcm.card {output.id}\ndefaults.ctl.card {output.id}\n"
        try:
            ASOUNDRC.write_text(text, encoding="utf-8")
        except OSError as exc:
            return False, str(exc)
        return True, f"sound out: {output.name} (programs started from now on)"


# --- display ----------------------------------------------------------------------------------------

def backlights() -> list[Path]:
    return sorted(Path("/sys/class/backlight").glob("*"))


def brightness() -> int | None:
    lights = backlights()
    if not lights:
        return None
    cur, top = _read(str(lights[0] / "brightness")), _read(str(lights[0] / "max_brightness"))
    return round(int(cur) * 100 / int(top)) if cur.isdigit() and top.isdigit() and int(top) else None


def set_brightness(step: int) -> tuple[bool, str]:
    if not backlights():
        return False, "this screen has no brightness control"
    if have("brightnessctl"):
        rc, out = run(["brightnessctl", "set", f"{abs(step)}%{'+' if step > 0 else '-'}"])
        return rc == 0, "" if rc == 0 else _short_error(out)
    return False, "brightnessctl isn't installed"


# --- time and locale ----------------------------------------------------------------------------

def parse_keyvalues(out: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def time_state() -> dict[str, str]:
    return parse_keyvalues(run(["timedatectl", "show"])[1])


def timezones() -> list[str]:
    rc, out = run(["timedatectl", "list-timezones"])
    return out.splitlines() if rc == 0 else []


def set_timezone(tz: str) -> tuple[bool, str]:
    rc, out = run(["timedatectl", "set-timezone", tz])
    if rc:
        rc, out = run(["timedatectl", "set-timezone", tz], sudo=True)
    return rc == 0, f"time zone {tz}" if rc == 0 else _short_error(out)


def set_ntp(on: bool) -> tuple[bool, str]:
    rc, out = run(["timedatectl", "set-ntp", "true" if on else "false"])
    if rc:
        rc, out = run(["timedatectl", "set-ntp", "true" if on else "false"], sudo=True)
    return rc == 0, ("network time on" if on else "network time off") if rc == 0 else _short_error(out)


def locale_state() -> dict[str, str]:
    out = run(["localectl", "status"])[1]
    get = lambda k: (re.search(rf"^\s*{k}:\s*(.+)$", out, re.M) or [None, ""])[1].strip()
    return {"locale": get("System Locale"), "layout": get("X11 Layout"), "model": get("X11 Model")}


def set_keyboard(layout: str) -> tuple[bool, str]:
    rc, out = run(["localectl", "set-x11-keymap", layout], sudo=True)
    return rc == 0, f"keyboard {layout} (from the next start of MABEL-OS)" if rc == 0 else _short_error(out)


# --- power ----------------------------------------------------------------------------------------

def power(action: str) -> tuple[bool, str]:
    """suspend · reboot · poweroff of the whole machine."""
    rc, out = run(["systemctl", action])
    if rc:
        rc, out = run(["systemctl", action], sudo=True)
    return rc == 0, action if rc == 0 else _short_error(out)

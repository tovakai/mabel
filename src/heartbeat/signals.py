"""Phase 5: availability signals. Each one can say "unknown", and unknown never means yes.

- idle: written by hypridle through scripts/idle_signal.sh ("idle" after its
  timeout, "active" on resume);
- gpu: another heavy GPU user (a game, SteamVR, a big process) or high load;
- chat: any chat or agent run streaming right now;
- strata: Mabel's model server answering at all;
- presence (optional, informational for now): a Home Assistant person or
  device_tracker entity, through Mabel's own Home Assistant integration (URL and
  token are entered by the user in the integrations settings, never here).
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

IDLE_TIMEOUT_S = 240            # matches the hypridle listener that writes the file
IDLE_FILE = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "mabel" / "idle"
# Processes that are always on the GPU and don't mean "Anthon is using it".
GPU_IGNORE = re.compile(r"(?:^|/)(?:strata|steam|steamwebhelper|xwayland|hyprland|qs|quickshell|"
                        r"firefox|chrom(?:e|ium)|electron|code|discord|obs-?studio-?browser)$", re.I)
GPU_BUSY_NAMES = re.compile(r"steamapps/common/|vrserver|vrcompositor|wine|proton|gamescope", re.I)
GPU_BUSY_MIB = 400
GPU_BUSY_UTIL = 50
STRATA = ("127.0.0.1", 8080)


@dataclass
class Signals:
    idle: str = "unknown"            # idle | active | unknown
    idle_minutes: float | None = None
    gpu: str = "unknown"             # free | busy | unknown
    gpu_reason: str = ""
    chat_active: bool | None = None
    strata: bool | None = None
    presence: str = "not_configured"  # home | away | unknown | not_configured
    presence_entity: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def read_idle(path: Path = IDLE_FILE, now: float | None = None) -> tuple[str, float | None]:
    now = time.time() if now is None else now
    try:
        state, stamp = path.read_text().split()[:2]
        stamp = float(stamp)
    except (OSError, ValueError):
        return "unknown", None
    if state == "idle" and stamp <= now:
        return "idle", round((now - stamp + IDLE_TIMEOUT_S) / 60, 1)
    if state == "active":
        return "active", 0.0
    return "unknown", None


def read_gpu(run=subprocess.run) -> tuple[str, str]:
    try:
        apps = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                    "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        util = run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                   capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown", "nvidia-smi unavailable"
    if apps.returncode or util.returncode:
        return "unknown", "nvidia-smi failed"
    for line in apps.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        name = parts[1]
        try:
            mib = float(parts[2])
        except ValueError:
            mib = 0.0
        if GPU_BUSY_NAMES.search(name):
            return "busy", Path(name).name
        if not GPU_IGNORE.search(name) and mib >= GPU_BUSY_MIB:
            return "busy", f"{Path(name).name} ({int(mib)} MiB)"
    try:
        load = int(util.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return "unknown", "unreadable GPU load"
    if load >= GPU_BUSY_UTIL:
        return "busy", f"GPU load {load}%"
    return "free", ""


def any_chat_active() -> bool | None:
    try:
        from routes.chat_routes import _active_streams
        from src import agent_runs
        return bool(_active_streams) or any(r.status == "running" for r in agent_runs._RUNS.values())
    except Exception:
        return None


def strata_up(host: str = STRATA[0], port: int = STRATA[1]) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def read_presence(get=None) -> tuple[str, str]:
    """(home | away | unknown | not_configured, entity) from Home Assistant."""
    try:
        from src.integrations import load_integrations
        from src.settings import get_setting
        entity = str(get_setting("heartbeat_presence_entity", "") or "").strip()
        ha = next((i for i in load_integrations() if i.get("preset") == "homeassistant" and i.get("enabled")), None)
    except Exception:
        return "unknown", ""
    if not entity or not ha or not ha.get("base_url") or not ha.get("api_key"):
        return "not_configured", entity
    if not re.fullmatch(r"(?:person|device_tracker|binary_sensor)\.[a-z0-9_]+", entity):
        return "unknown", entity
    try:
        if get is None:
            import httpx
            get = httpx.get
        response = get(f"{str(ha['base_url']).rstrip('/')}/api/states/{entity}",
                       headers={"Authorization": f"Bearer {ha['api_key']}"}, timeout=3)
        state = str(response.json().get("state", "")).lower() if response.status_code == 200 else ""
    except Exception:
        return "unknown", entity
    if state in ("home", "on"):
        return "home", entity
    if state in ("not_home", "away", "off"):
        return "away", entity
    return "unknown", entity


def collect() -> Signals:
    sig = Signals()
    sig.idle, sig.idle_minutes = read_idle()
    sig.gpu, sig.gpu_reason = read_gpu()
    sig.chat_active = any_chat_active()
    sig.strata = strata_up()
    sig.presence, sig.presence_entity = read_presence()
    return sig

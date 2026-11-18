"""~/.config/mabel-tui/config.toml: where Mabel is and the API token (profile "tui": chat, music, life:read)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mabel-tui" / "config.toml"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mabel-tui"


@dataclass
class Config:
    url: str
    token: str
    volume: int = 70
    index_url: str = "http://127.0.0.1:8766"      # tk-index; over the tailnet: https://<host>:8765
    vault: str = "~/Documents/core of truth"      # his Obsidian vault (VAULT channel, Gremlin inbox)
    journal: str = "~/Documents/mabel/journal"    # Mabel's journal, a second vault in VAULT (PC only)
    navidrome: dict | None = None                 # [navidrome] url/user/password: AUDIO direct, without Mabel
    preset: str = "custom"                        # the chat preset that carries her character ("" = none)
    avatar: str = ""                              # her avatar: a folder like assets/avatar (avatar.json + strips) or one PNG; "" = built in
    avatar_fps: float = 4.0                       # frames per second when the avatar is a strip
    offline_bitrate: int = 0                      # downloads: 0 = the original files, else e.g. 192 (kbit/s)
    pixel_dir: str = "~/Pictures/MABEL-OS"        # PIXEL's files (relative names in :w / :e / :export)
    tracker_dir: str = "~/Music/MABEL-OS"         # TRACKER's songs and exports
    tracker_rate: int = 44100                     # TRACKER's sample rate (22050 eases a Pi)
    auto_update: bool = False                     # install pushed updates by itself after 5 min without a key (the deck)


def load(path: Path = CONFIG) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"No config at {path}. It needs:\n  url = \"http://127.0.0.1:7000\"\n  token = \"ody_…\"  "
                         "(an API token with the tui profile: chat, music, life:read)")
    url, token = str(data.get("url") or "").rstrip("/"), str(data.get("token") or "")
    if not url or not token.startswith("ody_"):
        raise SystemExit(f"{path}: url and token (ody_…) are both needed")
    return Config(url=url, token=token, volume=int(data.get("volume", 70)),
                  index_url=str(data.get("index_url") or Config.index_url).rstrip("/"),
                  vault=str(data.get("vault") or Config.vault),
                  journal=str(data.get("journal") or Config.journal),
                  navidrome=_navidrome(data.get("navidrome"), path),
                  offline_bitrate=int(data.get("offline_bitrate") or 0),
                  auto_update=bool(data.get("auto_update", False)),
                  pixel_dir=str(data.get("pixel_dir") or Config.pixel_dir),
                  tracker_dir=str(data.get("tracker_dir") or Config.tracker_dir),
                  tracker_rate=int(data.get("tracker_rate") or Config.tracker_rate),
                  preset=str(data.get("preset", "custom")),
                  avatar=str(data.get("avatar") or ""), avatar_fps=float(data.get("avatar_fps") or 4.0))


def _navidrome(section, path: Path) -> dict | None:
    if not section:
        return None
    nd = {k: str(section.get(k) or "") for k in ("url", "user", "password")}
    if not all(nd.values()):
        raise SystemExit(f"{path}: [navidrome] needs url, user and password")
    return nd

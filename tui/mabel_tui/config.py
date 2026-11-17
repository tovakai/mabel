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


def load(path: Path = CONFIG) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"No config at {path}. It needs:\n  url = \"http://127.0.0.1:7000\"\n  token = \"ody_…\"  "
                         "(an API token with the tui profile: chat, music, life:read)")
    url, token = str(data.get("url") or "").rstrip("/"), str(data.get("token") or "")
    if not url or not token.startswith("ody_"):
        raise SystemExit(f"{path}: url and token (ody_…) are both needed")
    return Config(url=url, token=token, volume=int(data.get("volume", 70)))

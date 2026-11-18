"""Updates pushed from the PC: MABEL-OS asks Mabel which build she serves and offers F12.

An install made by Mabel's installer (curl …/tui/install.sh | sh) writes the build it got to
~/.local/share/mabel-tui/build. Mabel's /tui/version says which build she serves now: a hash
of tui/'s contents, so any change made on the PC is a new build. A copy run from the source
tree (the PC itself) has no build file and never asks.

Applying runs the same installer (it keeps the config), then the app restarts in place.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

APP_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "mabel-tui"
BUILD_FILE = APP_DIR / "build"
CHECK_EVERY_S = 300
IDLE_BEFORE_AUTO_S = 300            # auto_update waits this long without a key press


def installed_build(path: Path = BUILD_FILE) -> str | None:
    """The build this install came from; None for a copy run from source (no updates offered)."""
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


async def served_build(base_url: str, client: httpx.AsyncClient | None = None) -> str | None:
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.get(base_url.rstrip("/") + "/tui/version")
        if resp.status_code != 200:
            return None
        return str(resp.json().get("build") or "") or None
    except Exception:
        return None
    finally:
        if own:
            await client.aclose()


def newer(installed: str | None, served: str | None) -> bool:
    return bool(installed and served and installed != served)


def install_command(base_url: str) -> list[str]:
    url = base_url.rstrip("/") + "/tui/install.sh"
    fetch = f"curl -fsSL '{url}'" if shutil.which("curl") else f"wget -qO- '{url}'"
    return ["sh", "-c", f"{fetch} | sh"]


def apply(base_url: str, run=subprocess.run) -> tuple[bool, str]:
    """Run Mabel's installer again. Returns (ok, the last lines it printed)."""
    try:
        done = run(install_command(base_url), capture_output=True, text=True, timeout=900,
                   stdin=subprocess.DEVNULL)
    except Exception as exc:
        return False, str(exc)
    tail = "\n".join((done.stdout + done.stderr).strip().splitlines()[-4:])
    return done.returncode == 0, tail


def restart() -> None:
    """Replace this process with the freshly installed mabel (same terminal, same arguments)."""
    if os.path.isfile(sys.argv[0]):
        os.execv(sys.argv[0], sys.argv)
    os.execvp("mabel", ["mabel", *sys.argv[1:]])

"""Phase 5: hold off sleep only while an autonomous session runs, and give it back.

polkit only lets the local desktop session block sleep, so the inhibitor runs
as a transient unit of the user's own service manager. It is named, visible
in ``systemd-inhibit --list`` as "Mabel", and capped at 25 minutes even if
Mabel crashes.

hypridle tries to suspend only once; if Mabel's block made that attempt
fail, ``suspend_if_overdue`` suspends the PC once the session is over and the
user is still away past the configured suspend timeout.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

UNIT = "mabel-research-inhibit"
CAP_S = 25 * 60
HYPRIDLE_CONF = Path.home() / ".config" / "hypr" / "hypridle.conf"


def _env() -> dict:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return env


def _run(args: list[str], run=subprocess.run) -> bool:
    try:
        return run(args, capture_output=True, text=True, timeout=15, env=_env()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def hold_sleep(run=subprocess.run) -> bool:
    release_sleep(run)          # never two
    ok = _run(["systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet", "--",
               "systemd-inhibit", "--what=sleep", "--who=Mabel", "--why=Mabel is researching",
               "--mode=block", "sleep", str(CAP_S)], run)
    if not ok:
        logger.warning("[heartbeat] could not take the sleep inhibitor")
    return ok


def release_sleep(run=subprocess.run) -> None:
    _run(["systemctl", "--user", "stop", f"{UNIT}.service"], run)


def suspend_timeout_s(conf: Path = HYPRIDLE_CONF) -> int | None:
    """The hypridle listener timeout whose on-timeout suspends, if any."""
    try:
        text = conf.read_text()
    except OSError:
        return None
    for block in re.findall(r"listener\s*\{(.*?)\}", text, re.S):
        timeout = re.search(r"timeout\s*=\s*(\d+)", block)
        action = re.search(r"on-timeout\s*=\s*(.+)", block)
        if timeout and action and "suspend" in action.group(1):
            return int(timeout.group(1))
    return None


def suspend_if_overdue(idle_minutes: float | None, run=subprocess.run) -> bool:
    timeout = suspend_timeout_s()
    if timeout is None or idle_minutes is None or idle_minutes * 60 < timeout:
        return False
    logger.info("[heartbeat] still away after %.0f min; suspending as hypridle would have", idle_minutes)
    return _run(["systemd-run", "--user", "--collect", "--quiet", "--", "systemctl", "suspend"], run)

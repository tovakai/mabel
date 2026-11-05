"""Phase 4: what is true about Mabel's stack *right now*, gathered live (no guessing).

Each fact has an id (F1, F2, ...) so a stale-framing suggestion must point at
the fact that contradicts the note. Facts come from settings, the configured
model roles, quick reachability checks and git history.
"""
from __future__ import annotations

import socket
import subprocess
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
SERVICES = (("embedding server", "127.0.0.1", 8082), ("reranker", "127.0.0.1", 8083),
            ("ChromaDB vector store", "127.0.0.1", 8100), ("Strata model server (Gyro)", "127.0.0.1", 8080))
GIT_DAYS = 45
GIT_LIMIT = 90


def _reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def collect(*, owner: str = "tovakai", now: datetime | None = None, repo: Path = _REPO) -> list[dict]:
    now = now or datetime.now().astimezone()
    facts: list[str] = [f"Today is {now:%Y-%m-%d}."]
    try:
        from src.model_roles import candidates
        for role, label in (("default", "Mabel's foreground model"), ("research", "Dottie's background model")):
            entries = candidates(role, owner)
            if entries and entries[0].model:
                facts.append(f"{label} is {entries[0].model}.")
    except Exception:
        pass
    try:
        from src.settings import get_setting
        facts.append(f"Prompt stitching (persona_mode) is {get_setting('persona_mode', 'off')}.")
        for key, label in (("episodic_capture_enabled", "Episodic memory capture"),
                           ("episodic_recall_enabled", "Episodic memory recall"),
                           ("subconscious_auto_enabled", "Dottie's automatic maintenance loops")):
            facts.append(f"{label} is {'on' if get_setting(key, False) else 'off'}.")
    except Exception:
        pass
    for label, host, port in SERVICES:
        facts.append(f"The {label} ({host}:{port}) is {'reachable' if _reachable(host, port) else 'not reachable'}.")
    try:
        log = subprocess.run(["git", "-C", str(repo), "log", f"--since={GIT_DAYS} days ago", f"-{GIT_LIMIT}",
                              "--format=%h %ad %s", "--date=short"], capture_output=True, text=True, timeout=10)
        for line in log.stdout.splitlines():
            if line.split(" ", 2)[-1].startswith(("feat", "fix", "perf")):
                facts.append(f"Implemented (git {line.split()[0]}, {line.split()[1]}): {line.split(' ', 2)[2]}")
    except (OSError, subprocess.SubprocessError):
        pass
    return [{"id": f"F{i}", "text": text} for i, text in enumerate(facts, 1)]

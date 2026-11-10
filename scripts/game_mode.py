#!/usr/bin/env python3
"""Game mode: unload Mabel's model while a Steam game runs, load her again when it closes.

Watches for Steam launches the same way the life-log collector does
(``reaper SteamLaunch AppId=…``, Proton included, SteamVR alone doesn't count),
and stops Strata through ``src/gpu_swap.py`` so it never cuts into a dream's
GPU swap. She's only loaded again if game mode was the one that unloaded her:
if you stopped her yourself before playing, she stays stopped.

While she's unloaded her own activities wait on their own ("her model isn't
up"), and chat to her fails until she's back (~25 s after the game closes).

Config (optional) in data/gpu_swap.json:
    "game_mode": {"enabled": true, "ignore_appids": ["123"], "notify": true}

Run:  .venv-arch/bin/python scripts/game_mode.py            (the service does this)
      .venv-arch/bin/python scripts/game_mode.py --dry-run  (only says what it would do)
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

logger = logging.getLogger("game_mode")

POLL_S = 5
START_AFTER = 2          # samples in a row with a game before unloading (10 s)
STOP_AFTER = 4           # samples in a row without one before loading her again (20 s; launchers hand over)


def _state_path() -> Path:
    from src import gpu_swap
    return gpu_swap._data_dir() / "gpu_swap" / "game_mode.json"


def load_state() -> dict | None:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_state(state: dict | None) -> None:
    path = _state_path()
    if state is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)


def settings() -> dict:
    from src import gpu_swap
    return {"enabled": True, "ignore_appids": [], "notify": True, **(gpu_swap.load_config().get("game_mode") or {})}


def notify(text: str) -> None:
    if settings().get("notify") and shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Mabel", "-i", "input-gaming", "Mabel", text], check=False)


def current_game() -> dict | None:
    from lifelog_collector import sample_game
    game = sample_game()
    if game and str((game.get("detail") or {}).get("appid", "")) in {str(a) for a in settings()["ignore_appids"]}:
        return None
    return game


def unload(game: dict, *, dry_run: bool) -> None:
    from src import gpu_swap
    cfg = gpu_swap.load_config()
    lock = gpu_swap._cross_process_lock()          # waits for a dream's swap to finish first
    try:
        running = gpu_swap.strata_running(cfg)
        logger.info("game started: %s (Strata %s)", game["title"], "running" if running else "already stopped")
        if dry_run:
            return
        _save_state({"game": game["title"], "appid": (game.get("detail") or {}).get("appid"),
                     "since": datetime.now(timezone.utc).isoformat(), "restore": running})
        if running:
            res = gpu_swap._strata(cfg, "stop", gpu_swap.STRATA_STOP_WAIT)
            if not res.get("ok"):
                logger.error("couldn't unload Strata: %s", res.get("error") or res)
                _save_state({**load_state(), "restore": False, "error": str(res)[:300]})
                return
            gpu_swap._ledger("gpu.game_mode_on", service="game")
            notify(f"Resting while you play {game['title']}.")
    finally:
        lock.close()


def reload(*, dry_run: bool) -> None:
    from src import gpu_swap
    state = load_state() or {}
    logger.info("game closed: %s (load her again: %s)", state.get("game", "?"), bool(state.get("restore")))
    if dry_run:
        return
    if state.get("restore"):
        cfg = gpu_swap.load_config()
        lock = gpu_swap._cross_process_lock()
        try:
            back = gpu_swap.strata_running(cfg) or gpu_swap._bring_strata_back(cfg)
        finally:
            lock.close()
        gpu_swap._ledger("gpu.game_mode_off", service="game", strata="back" if back else "failed")
        notify("Back." if back else "Couldn't load her model again: run strata runtime start.")
        if not back:
            logger.error("Strata didn't come back")
    _save_state(None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="only log what it would do")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s game_mode %(levelname)s %(message)s")

    seen, unseen = 0, 0
    playing = load_state() is not None             # survived a restart mid-game
    if playing:
        logger.info("resuming: %s was running when game mode last stopped", load_state().get("game"))
    while True:
        try:
            game = current_game() if settings()["enabled"] or playing else None
        except Exception:
            logger.warning("couldn't look for a game", exc_info=True)
            game = None
        seen, unseen = (seen + 1, 0) if game else (0, unseen + 1)
        if not playing and game and seen >= START_AFTER:
            unload(game, dry_run=args.dry_run)
            playing = True
        elif playing and not game and unseen >= STOP_AFTER:
            reload(dry_run=args.dry_run)
            playing = False
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()

"""Game mode: unload her for a Steam game, load her again only if game mode unloaded her."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import game_mode  # noqa: E402
from src import gpu_swap  # noqa: E402

GAME = {"app": "steam", "title": "Example Game", "detail": {"appid": "123450"}}


class _Lock:
    def close(self):
        pass


@pytest.fixture
def strata(monkeypatch, tmp_path):
    calls = []
    status = {"running": True}
    monkeypatch.setattr(game_mode, "_state_path", lambda: tmp_path / "game_mode.json")
    monkeypatch.setattr(game_mode, "notify", lambda text: calls.append(("notify", text)))
    monkeypatch.setattr(gpu_swap, "load_config", lambda: {"strata_profile": "p"})
    monkeypatch.setattr(gpu_swap, "_cross_process_lock", lambda: _Lock())
    monkeypatch.setattr(gpu_swap, "_ledger", lambda kind, **meta: calls.append(("ledger", kind)))
    monkeypatch.setattr(gpu_swap, "strata_running", lambda cfg: status["running"])

    def _strata(cfg, action, wait):
        calls.append(("strata", action))
        status["running"] = action != "stop"
        return {"ok": True, "state": "stopped" if action == "stop" else "ready"}

    def _back(cfg):
        calls.append(("strata", "start"))
        status["running"] = True
        return True

    monkeypatch.setattr(gpu_swap, "_strata", _strata)
    monkeypatch.setattr(gpu_swap, "_bring_strata_back", _back)
    return calls, status


def test_unloads_for_a_game_and_loads_her_again(strata):
    calls, status = strata
    game_mode.unload(GAME, dry_run=False)
    assert ("strata", "stop") in calls and not status["running"]
    assert game_mode.load_state()["restore"] is True
    game_mode.reload(dry_run=False)
    assert ("strata", "start") in calls and status["running"] and game_mode.load_state() is None


def test_stays_stopped_if_you_stopped_her_yourself(strata):
    calls, status = strata
    status["running"] = False
    game_mode.unload(GAME, dry_run=False)
    game_mode.reload(dry_run=False)
    assert not [c for c in calls if c[0] == "strata"] and not status["running"]


def test_dry_run_touches_nothing(strata):
    calls, status = strata
    game_mode.unload(GAME, dry_run=True)
    assert status["running"] and game_mode.load_state() is None and not calls


def test_ignored_games_dont_count(monkeypatch):
    import lifelog_collector
    monkeypatch.setattr(lifelog_collector, "sample_game", lambda: GAME)
    monkeypatch.setattr(game_mode, "settings", lambda: {"enabled": True, "ignore_appids": ["123450"], "notify": False})
    assert game_mode.current_game() is None
    monkeypatch.setattr(game_mode, "settings", lambda: {"enabled": True, "ignore_appids": [], "notify": False})
    assert game_mode.current_game() == GAME

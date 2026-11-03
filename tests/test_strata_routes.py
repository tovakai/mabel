"""Regression tests for the opt-in local Gyro-S lifecycle control."""
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import strata_routes as strata


def _client(monkeypatch, *, enabled=True, authenticated=False):
    monkeypatch.setenv("AUTH_ENABLED", "true" if authenticated else "false")
    if enabled:
        monkeypatch.setenv("MABEL_STRATA_CONTROL_ENABLED", "true")
    else:
        monkeypatch.delenv("MABEL_STRATA_CONTROL_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(strata.setup_strata_routes())
    return TestClient(app)


def _state(value):
    return {"enabled": True, "state": value, "model": "Gyro-S"}


def test_controls_are_opt_in(monkeypatch):
    client = _client(monkeypatch, enabled=False)
    assert client.get("/api/strata/status").json() == {"enabled": False}
    assert client.post("/api/strata/start").status_code == 404
    assert client.post("/api/strata/suspend").status_code == 404


def test_requires_admin_even_to_check_status(monkeypatch):
    client = _client(monkeypatch, authenticated=True)
    assert client.get("/api/strata/status").status_code == 403
    assert client.post("/api/strata/start").status_code == 403
    assert client.post("/api/strata/suspend").status_code == 403


def test_model_identity_filter():
    assert strata._expected_model("qwen3.8-flash-next-gyro-gyro-s")
    assert strata._expected_model("gyro-s")
    assert not strata._expected_model("qwen3.8-flash-next-gyro-gyro-m")
    assert not strata._expected_model("")
    assert not strata._expected_model(None)


def test_resume_uses_strata_load_api_not_launcher(monkeypatch):
    probe = AsyncMock(return_value=_state("suspended"))
    control = AsyncMock(return_value=_state("ready"))
    monkeypatch.setattr(strata, "_probe", probe)
    monkeypatch.setattr(strata, "_control", control)
    client = _client(monkeypatch)
    assert client.post("/api/strata/start").json()["state"] == "ready"
    control.assert_awaited_once_with("load")


def test_suspend_uses_strata_unload_api(monkeypatch):
    monkeypatch.setattr(strata, "_probe", AsyncMock(return_value=_state("ready")))
    control = AsyncMock(return_value=_state("suspended"))
    monkeypatch.setattr(strata, "_control", control)
    client = _client(monkeypatch)
    assert client.post("/api/strata/suspend").json()["state"] == "suspended"
    control.assert_awaited_once_with("unload")


def test_refuses_to_touch_a_different_model(monkeypatch):
    monkeypatch.setattr(strata, "_probe", AsyncMock(return_value=_state("wrong-model")))
    control = AsyncMock()
    monkeypatch.setattr(strata, "_control", control)
    client = _client(monkeypatch)
    assert client.post("/api/strata/start").status_code == 409
    assert client.post("/api/strata/suspend").status_code == 409
    control.assert_not_awaited()


def test_offline_start_uses_pinned_preset_without_shell(monkeypatch):
    class Process:
        pid = 123
        returncode = None

        def poll(self):
            return None

    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(strata, "_probe", AsyncMock(return_value=_state("offline")))
    monkeypatch.setattr(strata, "_executable", lambda: "/fake/strata")
    monkeypatch.setattr(strata.subprocess, "Popen", popen)
    client = _client(monkeypatch)
    assert client.post("/api/strata/start").json()["state"] == "starting"
    argv, options = calls[0]
    assert argv == ["/fake/strata", "launch", "--preset", "gyro-s-32k", "--no-tui", "--yes"]
    assert options["start_new_session"] is True
    assert "shell" not in options

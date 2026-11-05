import asyncio
import json

import pytest

from src import gpu_swap


CFG = {"strata_profile": "launch-test",
       "services": {"image": {"url": "http://127.0.0.1:8100", "command": ["true"]}}}


@pytest.fixture
def swap(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_swap, "_data_dir", lambda: tmp_path)
    (tmp_path / "gpu_swap.json").write_text(json.dumps(CFG))
    calls = []
    state = {"strata": "ready", "start_ok": True}

    def fake_strata(cfg, action, wait):
        calls.append(f"strata:{action}")
        if action == "status":
            return {"ok": True, "state": state["strata"]}
        if action == "stop":
            state["strata"] = "stopped"
            return {"ok": True, "state": "stopped"}
        if action == "start":
            if state["start_ok"]:
                state["strata"] = "ready"
                return {"ok": True, "state": "ready"}
            return {"ok": False, "error": "boom"}
        return {"ok": True}

    class Proc:
        pid = 4242

    monkeypatch.setattr(gpu_swap, "_strata", fake_strata)
    monkeypatch.setattr(gpu_swap, "_start_service", lambda n, s: calls.append("service:start") or Proc())
    monkeypatch.setattr(gpu_swap, "_wait_ready", lambda p, s: calls.append("service:ready"))
    monkeypatch.setattr(gpu_swap, "_reap", lambda p: calls.append("service:stop"))
    monkeypatch.setattr(gpu_swap, "_stop_pid", lambda pid, grace=20: calls.append(f"kill:{pid}"))
    return calls, state


def test_manages_matches_host_and_port():
    assert gpu_swap.manages("http://127.0.0.1:8100/v1/images/generations", CFG) == "image"
    assert gpu_swap.manages("http://127.0.0.1:8080/v1/chat/completions", CFG) is None
    assert gpu_swap.manages("https://api.openai.com/v1/images/generations", CFG) is None


def test_room_steps_aside_and_comes_back_in_order(swap):
    calls, state = swap

    async def run():
        async with gpu_swap.room("image") as url:
            assert gpu_swap.active()["phase"] == "working"
            calls.append("work")
            return url
    assert asyncio.run(run()) == "http://127.0.0.1:8100"
    assert calls == ["strata:status", "strata:stop", "service:start", "service:ready", "work",
                     "service:stop", "strata:start"]
    assert gpu_swap.active() is None and state["strata"] == "ready"


def test_she_comes_back_even_when_the_work_fails(swap):
    calls, state = swap

    async def run():
        async with gpu_swap.room("image"):
            raise RuntimeError("render crashed")
    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert calls[-2:] == ["service:stop", "strata:start"]
    assert state["strata"] == "ready" and gpu_swap.active() is None


def test_strata_left_stopped_if_it_was_stopped_before(swap):
    calls, state = swap
    state["strata"] = "stopped"

    async def run():
        async with gpu_swap.room("image"):
            pass
    asyncio.run(run())
    assert "strata:stop" not in calls and "strata:start" not in calls


def test_failed_restart_is_retried_after_cleanup(swap):
    calls, state = swap
    state["start_ok"] = False

    async def run():
        async with gpu_swap.room("image"):
            pass
    asyncio.run(run())
    assert calls.count("strata:start") == 2 and calls.count("strata:cleanup") == 2
    assert gpu_swap.active() is None


def test_unknown_service_raises(swap):
    async def run():
        async with gpu_swap.room("video"):
            pass
    with pytest.raises(gpu_swap.SwapError):
        asyncio.run(run())


def test_recover_after_crash_kills_service_and_restarts_strata(swap):
    calls, state = swap
    state["strata"] = "stopped"
    gpu_swap._write_state({"service": "image", "phase": "working", "pid": 999, "restore_strata": True})
    assert gpu_swap.recover() == "image"
    assert "kill:999" in calls and "strata:start" in calls
    assert gpu_swap.active() is None and state["strata"] == "ready"


def test_recover_is_a_noop_without_a_swap(swap):
    calls, _ = swap
    assert gpu_swap.recover() is None and calls == []


def test_image_tool_reports_swap_phases_while_waiting(swap):
    from src import ai_interaction
    events = []

    async def cb(evt):
        events.append(evt)

    async def run():
        gpu_swap._write_state({"service": "image", "phase": "loading", "pid": 1})
        task = asyncio.create_task(ai_interaction._swap_progress_ticker(cb, interval=0.01))
        await asyncio.sleep(0.05)
        gpu_swap._write_state({"service": "image", "phase": "coming_back", "pid": 1})
        await asyncio.sleep(0.05)
        task.cancel()
    asyncio.run(run())
    messages = [e["message"] for e in events]
    assert "Loading the image model…" in messages and "Coming back…" in messages
    assert all(e["tool"] == "generate_image" for e in events)

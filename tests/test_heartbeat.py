"""Phase 5 heartbeat: signals, the deterministic policy, sleep handling, yield and resume."""
import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.heartbeat import policy, power, signals

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 5, 14, 0, tzinfo=TZ)


def _sig(**kw):
    base = dict(idle="idle", idle_minutes=8.0, gpu="free", gpu_reason="", chat_active=False, strata=True,
                presence="not_configured", presence_entity="")
    return signals.Signals(**{**base, **kw})


def test_idle_file(tmp_path):
    f = tmp_path / "idle"
    assert signals.read_idle(f) == ("unknown", None)
    f.write_text("idle 1000\n")
    assert signals.read_idle(f, now=1000 + 60) == ("idle", 5.0)      # 240 s timeout + 60 s
    f.write_text("active 1000\n")
    assert signals.read_idle(f, now=5000) == ("active", 0.0)
    f.write_text("garbage")
    assert signals.read_idle(f)[0] == "unknown"


def _smi(apps, util="3", sessions="0", encoder="0"):
    def run(args, **kw):
        out = apps if "--query-compute-apps=pid,process_name,used_memory" in args else f"{util}, {sessions}, {encoder}"
        return SimpleNamespace(returncode=0, stdout=out)
    return run


STEAMVR_IDLE = ("54373, ./vrwebhelper, 62\n"
                "54359, /home/user/.local/share/Steam/steamapps/common/SteamVR/bin/linux64/vrserver, 258\n"
                "54594, /home/user/.local/share/Steam/steamapps/common/SteamVR/bin/linux64/vrcompositor, 907\n")


def test_gpu_signal_ignores_the_usual_suspects():
    idle_apps = ("3240, /home/user/.local/share/Steam/ubuntu12_32/steam, 86\n"
                 "56215, ./steamwebhelper, 298\n517369, /x/strata/engine/strata, 8704\n")
    assert signals.read_gpu(_smi(idle_apps)) == ("free", "")
    assert signals.read_gpu(_smi(idle_apps + STEAMVR_IDLE)) == ("free", ""), "SteamVR open, headset idle"
    assert signals.read_gpu(_smi(idle_apps + STEAMVR_IDLE, sessions="1")) == \
        ("busy", "headset streaming (video encoder in use)")
    game = idle_apps + STEAMVR_IDLE + "777, /home/user/.local/share/Steam/steamapps/common/Some Game/SomeGameVR.exe, 1500\n"
    assert signals.read_gpu(_smi(game))[0] == "busy", "a VR game counts"
    assert signals.read_gpu(_smi(idle_apps + "999, /opt/blender/blender, 2400\n")) == ("busy", "blender (2400 MiB)")
    assert signals.read_gpu(_smi(idle_apps, util="87")) == ("busy", "GPU load 87%")

    def broken(args, **kw):
        raise OSError("no nvidia-smi")
    assert signals.read_gpu(broken)[0] == "unknown"


@pytest.mark.parametrize("changes,reason", [
    (dict(idle="active", idle_minutes=0.0), "you're at the PC"),
    (dict(idle="unknown", idle_minutes=None), "idle state unknown"),
    (dict(idle_minutes=3.0), "idle only 3 min"),
    (dict(gpu="busy", gpu_reason="vrcompositor"), "GPU in use: vrcompositor"),
    (dict(gpu="unknown", gpu_reason="nvidia-smi failed"), "GPU state unknown"),
    (dict(chat_active=True), "a chat is running"),
    (dict(chat_active=None), "can't tell whether a chat is running"),
    (dict(strata=False), "model server"),
])
def test_unknown_or_busy_means_no(changes, reason):
    d = policy.decide(_sig(**changes), {}, now=NOW, enabled=True, paused=False, research_running=False)
    assert not d.allow and any(reason in r for r in d.reasons)


def test_caps_and_resume():
    ok = policy.decide(_sig(), {}, now=NOW, enabled=True, paused=False, research_running=False)
    assert ok.allow and ok.resume is None
    two = {"starts": [(NOW - timedelta(hours=8)).isoformat(), (NOW - timedelta(hours=4)).isoformat()]}
    assert not policy.decide(_sig(), two, now=NOW, enabled=True, paused=False, research_running=False).allow
    recent = {"starts": [(NOW - timedelta(hours=1)).isoformat()]}
    assert "gap" in " ".join(policy.decide(_sig(), recent, now=NOW, enabled=True, paused=False, research_running=False).reasons)
    interrupted = {**two, "yielded": {"session": "cs-1", "at": (NOW - timedelta(hours=1)).isoformat()}}
    resumed = policy.decide(_sig(), interrupted, now=NOW, enabled=True, paused=False, research_running=False)
    assert resumed.allow and resumed.resume == "cs-1", "finishing an interrupted session isn't a new start"
    assert not policy.decide(_sig(), {}, now=NOW, enabled=True, paused=True, research_running=False).allow
    assert not policy.decide(_sig(), {}, now=NOW, enabled=False, paused=False, research_running=False).allow


def test_should_yield_ignores_her_own_gpu_load():
    started = NOW - timedelta(minutes=2)
    assert policy.should_yield(_sig(gpu="busy", gpu_reason="GPU load 95%"), started=started, now=NOW) is None
    assert policy.should_yield(_sig(gpu="busy", gpu_reason="vrcompositor"), started=started, now=NOW) == "GPU needed: vrcompositor"
    assert policy.should_yield(_sig(gpu="busy", gpu_reason="headset streaming (video encoder in use)"),
                               started=started, now=NOW).startswith("GPU needed: headset")
    assert policy.should_yield(_sig(idle="active", idle_minutes=0.0), started=started, now=NOW) == "you came back"
    assert policy.should_yield(_sig(chat_active=True), started=started, now=NOW) == "a chat started"
    assert policy.should_yield(_sig(), started=NOW - timedelta(minutes=21), now=NOW) == "session time cap reached"


def test_power_commands_and_overdue_suspend(tmp_path, monkeypatch):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return SimpleNamespace(returncode=0)
    assert power.hold_sleep(run)
    assert calls[0][:3] == ["systemctl", "--user", "stop"] and "systemd-inhibit" in calls[1]
    assert "--mode=block" in calls[1] and "--who=Mabel" in calls[1]
    conf = tmp_path / "hypridle.conf"
    conf.write_text("listener {\n timeout = 300\n on-timeout = loginctl lock-session\n}\n"
                    "listener {\n timeout = 900 # 15mins\n on-timeout = $suspend_cmd\n}\n")
    monkeypatch.setattr(power, "HYPRIDLE_CONF", conf)
    assert power.suspend_timeout_s() == 900
    calls.clear()
    assert not power.suspend_if_overdue(10.0, 6.0, run) and not calls

    def quiet(args, **kw):           # no other inhibitors, no remote sessions
        calls.append(args)
        out = "2 1000 zum seat0 1780 user tty1 no -\n" if args[:2] == ["loginctl", "list-sessions"] else (
            "Remote=no\nActive=yes\n" if args[:2] == ["loginctl", "show-session"] else "")
        return SimpleNamespace(returncode=0, stdout=out)
    assert power.suspend_if_overdue(16.0, 6.0, quiet) and calls[-1][-2:] == ["systemctl", "suspend"]
    assert not power.suspend_if_overdue(21.0, 16.0, quiet), "already past suspend time at start: not her doing"
    assert not power.suspend_if_overdue(16.0, None, quiet)

    def remote(args, **kw):
        out = "10 1000 zum - 485913 user pts/2 no -\n" if args[:2] == ["loginctl", "list-sessions"] else (
            "Remote=yes\nActive=yes\n" if args[:2] == ["loginctl", "show-session"] else "")
        return SimpleNamespace(returncode=0, stdout=out)
    assert not power.suspend_if_overdue(16.0, 6.0, remote), "never cut off a remote session"

    def other_inhibitor(args, **kw):
        out = ("hypridle 1000 zum 1974 hypridle sleep Hypridle wants to delay sleep block\n"
               if args[0] == "systemd-inhibit" else "")
        return SimpleNamespace(returncode=0, stdout=out)
    assert not power.suspend_if_overdue(16.0, 6.0, other_inhibitor), "someone else keeps it awake on purpose"


def test_heartbeat_wakes_yields_resumes_and_finishes(tmp_path, monkeypatch):
    from src.curiosity import runner
    from src.heartbeat import daemon
    import src.constants
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(power, "hold_sleep", lambda run=None: True)
    released, suspended, ledger = [], [], []
    monkeypatch.setattr(power, "release_sleep", lambda run=None: released.append(1))
    monkeypatch.setattr(power, "suspend_if_overdue",
                        lambda minutes, idle_at_start=None, run=None: suspended.append((minutes, idle_at_start)))
    monkeypatch.setattr(daemon, "_ledger", lambda kind, sid, decision, code: ledger.append((kind, sid, decision)))
    monkeypatch.setattr(daemon, "_maybe_start_dream", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_maybe_check_letters", lambda *a, **k: False)
    finished = {"cs-1": False}
    monkeypatch.setattr(runner, "session_finished", lambda sid: finished.get(sid, False))

    class FakeSession:
        def __init__(self, sid):
            self.id = sid

    async def scenario():
        gate = asyncio.Event()

        def fake_start(*, resume=None, autonomous=False):
            sid = resume or "cs-1"
            async def work():
                await gate.wait()
            runner._state.update(session=FakeSession(sid), autonomous=autonomous, yielded=None, error=None,
                                 task=asyncio.get_running_loop().create_task(work()))
            return {"session": {"id": sid}}
        monkeypatch.setattr(runner, "start", fake_start)
        monkeypatch.setattr(runner, "status", lambda: {"running": runner.is_autonomous_running(),
                                                       "yielded": runner._state["yielded"], "error": None})
        away = lambda: _sig()
        await daemon.tick(now=NOW, read=away)                       # wakes
        assert runner.is_autonomous_running() and ledger[-1] == ("job.claimed", "cs-1", "started")
        await daemon.tick(now=NOW + timedelta(minutes=1), read=lambda: _sig(idle="active", idle_minutes=0.0))
        await asyncio.sleep(0)
        assert not runner.is_autonomous_running(), "came back -> yielded"
        await daemon.tick(now=NOW + timedelta(minutes=2), read=lambda: _sig(idle="active", idle_minutes=0.0))
        assert ledger[-1] == ("job.yielded", "cs-1", "yielded") and released
        assert daemon.load_state()["yielded"]["session"] == "cs-1"
        await daemon.tick(now=NOW + timedelta(minutes=30), read=away)   # resumes the same session, not a new start
        assert ledger[-1] == ("job.claimed", "cs-1", "resumed") and len(daemon.load_state()["starts"]) == 1
        finished["cs-1"] = True
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await daemon.tick(now=NOW + timedelta(minutes=40), read=lambda: _sig(idle_minutes=20.0))
        assert ("job.completed", "cs-1", "completed") in ledger and daemon.load_state()["yielded"] is None
        assert suspended == [(20.0, 8.0)], "passes the idle time at (re)start so power can tell if it was her"
    try:
        asyncio.run(scenario())
    finally:
        runner._state.update(task=None, session=None, autonomous=False, yielded=None)


def test_presence_via_home_assistant_integration(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None:
                        "person.anthon" if key == "heartbeat_presence_entity" else default)
    monkeypatch.setattr("src.integrations.load_integrations", lambda: [
        {"preset": "homeassistant", "enabled": True, "base_url": "http://homeassistant.lan:8123", "api_key": "secret"}])
    seen = {}

    def get(url, headers, timeout):
        seen.update(url=url, auth=headers["Authorization"])
        return SimpleNamespace(status_code=200, json=lambda: {"state": "not_home"})
    assert signals.read_presence(get) == ("away", "person.anthon")
    assert seen["url"] == "http://homeassistant.lan:8123/api/states/person.anthon" and seen["auth"] == "Bearer secret"
    monkeypatch.setattr("src.integrations.load_integrations", lambda: [   # added by hand, no preset
        {"name": "Home Assistant", "enabled": True, "base_url": "http://homeassistant.lan", "api_key": "secret"}])
    assert signals.read_presence(get) == ("away", "person.anthon")
    monkeypatch.setattr("src.integrations.load_integrations", lambda: [])
    assert signals.read_presence(get) == ("not_configured", "person.anthon")


def test_heartbeat_dreams_only_when_research_may_not_wake(monkeypatch):
    from src.heartbeat import daemon
    from src.persona import dreams
    started = []
    monkeypatch.setattr(daemon, "_maybe_start_dream", lambda sig, now, research_running: started.append(now))
    monkeypatch.setattr(daemon, "_maybe_check_letters", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "load_state", lambda: {})
    monkeypatch.setattr(dreams, "running", lambda: False)

    def decide_with(allow):
        monkeypatch.setattr(policy, "decide", lambda *a, **k: policy.Decision(allow, ["test"]))

    async def run(allow):
        decide_with(allow)
        from src.curiosity import runner
        monkeypatch.setattr(runner, "is_autonomous_running", lambda: False)
        monkeypatch.setattr(runner, "status", lambda: {"running": False})
        monkeypatch.setattr(runner, "start", lambda **k: (_ for _ in ()).throw(runner.AlreadyRunning()))
        monkeypatch.setattr(power, "hold_sleep", lambda run=None: True)
        monkeypatch.setattr(power, "release_sleep", lambda run=None: None)
        await daemon.tick(now=NOW, read=lambda: _sig())
    asyncio.run(run(True))
    assert started == []
    asyncio.run(run(False))
    assert started == [NOW]


def test_heartbeat_decides_nothing_while_dreaming(monkeypatch):
    from src.heartbeat import daemon
    from src.persona import dreams
    monkeypatch.setattr(dreams, "running", lambda: True)
    out = asyncio.run(daemon.tick(now=NOW, read=lambda: (_ for _ in ()).throw(AssertionError("read signals"))))
    assert out["dreaming"] is True


def test_mail_goes_first_and_a_dream_waits_for_it(monkeypatch):
    from src.curiosity import runner
    from src.heartbeat import daemon
    from src.persona import dreams
    dreamt = []
    monkeypatch.setattr(daemon, "_maybe_start_dream", lambda *a, **k: dreamt.append(1))
    monkeypatch.setattr(daemon, "_maybe_check_letters", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "load_state", lambda: {})
    monkeypatch.setattr(dreams, "running", lambda: False)
    monkeypatch.setattr(policy, "decide", lambda *a, **k: policy.Decision(False, ["test"]))
    monkeypatch.setattr(runner, "is_autonomous_running", lambda: False)
    monkeypatch.setattr(runner, "status", lambda: {"running": False})
    asyncio.run(daemon.tick(now=NOW, read=lambda: _sig()))
    assert dreamt == []

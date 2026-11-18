"""Updates pushed from the PC (mabel_tui/update.py)."""
import asyncio
import subprocess

import httpx

from mabel_tui import update


def test_only_installer_copies_get_updates(tmp_path):
    assert update.installed_build(tmp_path / "missing") is None, "run from source: never offered"
    (tmp_path / "build").write_text("abc123\n")
    assert update.installed_build(tmp_path / "build") == "abc123"
    assert update.newer("abc123", "def456") and not update.newer("abc123", "abc123")
    assert not update.newer(None, "def456") and not update.newer("abc123", None)


def test_asks_mabel_for_her_build():
    def handler(request):
        assert request.url.path == "/tui/version"
        return httpx.Response(200, json={"build": "def456"})

    async def ask(handler):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await update.served_build("https://box.ts.net/", client)
    assert asyncio.run(ask(handler)) == "def456"
    assert asyncio.run(ask(lambda r: httpx.Response(502))) is None


def test_apply_runs_the_installer_and_reports_its_last_lines():
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["stdin"] = cmd, kw.get("stdin")
        return subprocess.CompletedProcess(cmd, 0, "", "▌ fetching\n▌ kept your config\n▌ done. Run mabel\n")
    ok, tail = update.apply("https://box.ts.net", run)
    assert ok and tail.endswith("done. Run mabel")
    assert seen["cmd"][:2] == ["sh", "-c"] and "https://box.ts.net/tui/install.sh" in seen["cmd"][2]
    assert seen["stdin"] == subprocess.DEVNULL
    failed = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "▌ couldn't download")
    assert update.apply("https://box.ts.net", failed) == (False, "▌ couldn't download")

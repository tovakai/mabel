"""MABEL-OS install over the tailnet (routes/tui_install_routes.py, scripts/tui_install.sh)."""
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from routes import tui_install_routes as ti


def _req(client="127.0.0.1", **headers):
    return SimpleNamespace(client=SimpleNamespace(host=client),
                           headers={k.replace("_", "-").lower(): v for k, v in headers.items()})


def test_only_tailscale_serve_can_vouch_for_a_login():
    serve = dict(x_forwarded_for="100.67.251.17", tailscale_user_login="Him@Example.com")
    assert ti.tailnet_login(_req(**serve)) == "him@example.com"
    assert ti.tailnet_login(_req(client="100.67.251.17", **serve)) == "him@example.com", "uvicorn's rewrite"
    assert ti.tailnet_login(_req(client="100.99.0.1", **serve)) is None, "not the address Serve forwarded"
    assert ti.tailnet_login(_req(x_forwarded_for="203.0.113.9", tailscale_user_login="him@example.com")) is None
    assert ti.tailnet_login(_req(tailscale_user_login="him@example.com")) is None, "no tailnet address"
    assert ti.tailnet_login(_req(x_forwarded_for="100.67.251.17")) is None, "a tagged device has no login"


def test_only_listed_logins_pair(monkeypatch):
    monkeypatch.setattr(ti, "_setting", lambda key, default: {"Him@Example.com": "tovakai"})
    assert ti.owner_for("him@example.com") == "tovakai"
    assert ti.owner_for("her@example.com") is None and ti.owner_for(None) is None


def test_device_names_are_tidy():
    assert ti.device_name("mabel-deck") == "mabel-deck"
    assert ti.device_name("../evil host; rm -rf") == "evil-host-rm-rf"
    assert ti.device_name("") == "device"


def test_install_script_points_at_this_server():
    script = ti.install_script("https://box.tail1.ts.net", "mabel_tui-0.1.0-py3-none-any.whl", "abc123")
    assert "@@" not in script and 'BASE="https://box.tail1.ts.net"' in script
    assert 'BUILD="abc123"' in script
    assert ti.index_url("https://box.tail1.ts.net") == "https://box.tail1.ts.net:8765"
    assert subprocess.run(["sh", "-n"], input=script, text=True).returncode == 0
    assert 'RS_BUILDS=""' in script, "no Rust builds yet: the installer skips them"
    with_rs = ti.install_script("https://b", "w.whl", "abc", {"x86_64": "x1", "aarch64": "a1"})
    assert 'RS_BUILDS="x86_64:x1 aarch64:a1"' in with_rs


def test_the_installer_picks_the_rust_build_for_this_arch(tmp_path):
    """install_rust alone, against a fake fetch: the right binary, linked, its build recorded."""
    script = ti.install_script("https://b", "w.whl", "abc", {"x86_64": "x1", "aarch64": "a1"})
    body = script.split("main() {")[0]                     # the functions, without running main
    home = tmp_path / "home"
    harness = body + """
fetch() { printf 'binary from %s' "$1" > "$2"; }
uname() { echo aarch64; }
mkdir -p "$APP" "$BIN"; t=$(mktemp -d); install_rust "$t"
"""
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "XDG_DATA_HOME": str(home / "data")}
    done = subprocess.run(["sh", "-c", harness], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    app = home / "data" / "mabel-tui"
    assert (app / "mabel-os").read_text() == "binary from https://b/tui/rs/mabel-os-aarch64"
    assert (app / "build-rs").read_text().strip() == "a1"
    assert (home / ".local" / "bin" / "mabel-os").resolve() == app / "mabel-os"


def test_rust_builds_are_listed_by_content(tmp_path, monkeypatch):
    monkeypatch.setattr(ti, "RS_DIST", tmp_path)
    monkeypatch.setattr(ti, "_rs_cache", {})
    assert ti.rs_builds() == {}
    (tmp_path / "mabel-os-aarch64").write_bytes(b"elf one")
    first = ti.rs_builds()
    assert list(first) == ["aarch64"]
    (tmp_path / "mabel-os-aarch64").write_bytes(b"elf two")
    assert ti.rs_builds()["aarch64"] != first["aarch64"]


def test_the_wheel_is_built_once_and_carries_the_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(ti, "_cache_dir", lambda: tmp_path / "cache")
    wheel = ti.build_wheel()
    assert ti.build_wheel() == wheel, "cached until a source file changes"
    names = zipfile.ZipFile(wheel).namelist()
    assert "mabel_tui/app.py" in names and "mabel_tui/theme.tcss" in names
    assert any(n.startswith("mabel_tui/assets/avatar/") for n in names)
    assert not any("__pycache__" in n for n in names)


def test_build_id_follows_contents_not_mtimes(tmp_path, monkeypatch):
    src = tmp_path / "tui"
    (src / "mabel_tui").mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\n")
    app = src / "mabel_tui" / "app.py"
    app.write_text("print(1)\n")
    monkeypatch.setattr(ti, "TUI_DIR", src)
    monkeypatch.setattr(ti, "_fp_cache", None)
    first = ti._fingerprint()
    app.write_text("print(1)\n")                     # touched, same content
    assert ti._fingerprint() == first
    app.write_text("print(2)\n")
    assert ti._fingerprint() != first

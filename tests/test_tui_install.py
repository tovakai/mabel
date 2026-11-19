"""MABEL-OS install over the tailnet (routes/tui_install_routes.py, scripts/tui_install.sh)."""
import subprocess
from types import SimpleNamespace

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
    script = ti.install_script("https://box.tail1.ts.net")
    assert "@@" not in script and 'BASE="https://box.tail1.ts.net"' in script
    assert ti.index_url("https://box.tail1.ts.net") == "https://box.tail1.ts.net:8765"
    assert subprocess.run(["sh", "-n"], input=script, text=True).returncode == 0
    assert 'RS_BUILDS=""' in script
    with_rs = ti.install_script("https://b", {"x86_64": "x1", "aarch64": "a1"})
    assert 'RS_BUILDS="x86_64:x1 aarch64:a1"' in with_rs


def _sh(tmp_path, script, extra, env=None):
    """Run the installer's functions (not main) with `extra` appended, in a scratch home."""
    body = script.split("\nmain() {")[0]
    home = tmp_path / "home"
    full_env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "XDG_DATA_HOME": str(home / "data"),
                "XDG_CONFIG_HOME": str(home / "cfg"), **(env or {})}
    done = subprocess.run(["sh", "-c", body + "\n" + extra], env=full_env, capture_output=True, text=True)
    return done, home


def test_the_installer_picks_the_rust_build_for_this_arch(tmp_path):
    """rust_build + install_rust against a fake download: the right binary, linked as mabel and
    mabel-os, its build recorded."""
    script = ti.install_script("https://b", {"x86_64": "x1", "aarch64": "a1"})
    done, home = _sh(tmp_path, script, """
fetch() { printf 'binary from %s' "$1" > "$2"; }
uname() { echo aarch64; }
mkdir -p "$APP" "$BIN"; t=$(mktemp -d)
found=$(rust_build); echo "found $found"; install_rust "$t" $found
""")
    assert done.returncode == 0, done.stderr
    assert "found aarch64 a1" in done.stdout
    app = home / "data" / "mabel-tui"
    assert (app / "mabel-os").read_text() == "binary from https://b/tui/rs/mabel-os-aarch64"
    assert (app / "build-rs").read_text().strip() == "a1"
    for name in ("mabel-os", "mabel"):
        assert (home / ".local" / "bin" / name).resolve() == app / "mabel-os"
    done, _ = _sh(tmp_path, script, "uname() { echo armv7l; }; rust_build || echo none")
    assert done.stdout.strip() == "none", "a 32-bit Pi has no build"


def test_pairing_writes_the_config_and_keeps_the_rest(tmp_path):
    script = ti.install_script("https://b", {})
    conf = tmp_path / "home" / "cfg" / "mabel-tui" / "config.toml"
    conf.parent.mkdir(parents=True)
    conf.write_text('url = "http://old"\ntoken = "ody_old"\npalette = "deck"\n\n[colors]\nurl = "kept: not top-level"\n')
    answer = '{"url":"https://box.ts.net","index_url":"https://box.ts.net:8765","token":"ody_new","name":"Mabel TUI · deck"}'
    done, _ = _sh(tmp_path, script, f"""
post_json() {{ printf '%s' '{answer}' > "$3"; echo 200; }}
hostname() {{ echo 'mabel deck;'; }}
t=$(mktemp -d); pair "$t"
echo "url=$(conf_value url) token=$(conf_value token)"
""")
    assert done.returncode == 0, done.stderr
    assert "url=https://box.ts.net token=ody_new" in done.stdout
    text = conf.read_text()
    assert text.startswith('url = "https://box.ts.net"\ntoken = "ody_new"\nindex_url = "https://box.ts.net:8765"\n')
    assert 'palette = "deck"' in text and 'url = "kept: not top-level"' in text and "ody_old" not in text
    assert conf.stat().st_mode & 0o777 == 0o600
    assert list(conf.parent.glob("config.toml.bak-*")), "the old config is kept"


def test_a_refused_pairing_says_why(tmp_path):
    script = ti.install_script("https://b", {})
    done, _ = _sh(tmp_path, script, """
post_json() { printf '{"detail":"This device\\047s tailnet login isn\\047t allowed to pair"}' > "$3"; echo 403; }
t=$(mktemp -d); pair "$t"
""")
    assert done.returncode != 0 and "isn't allowed to pair" in done.stderr


def test_rust_builds_are_listed_by_content(tmp_path, monkeypatch):
    monkeypatch.setattr(ti, "RS_DIST", tmp_path)
    monkeypatch.setattr(ti, "_rs_cache", {})
    assert ti.rs_builds() == {}
    (tmp_path / "mabel-os-aarch64").write_bytes(b"elf one")
    first = ti.rs_builds()
    assert list(first) == ["aarch64"]
    (tmp_path / "mabel-os-aarch64").write_bytes(b"elf two")
    assert ti.rs_builds()["aarch64"] != first["aarch64"]

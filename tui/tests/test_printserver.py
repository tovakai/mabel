"""mabel-print, the deck's print server (mabel_tui/printserver.py), over real HTTP on localhost."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from mabel_tui import printserver as ps


def _serve(cfg):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ps.make_handler(cfg))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _call(url, path, token, data=None):
    req = urllib.request.Request(url + path, data=data, headers={"Authorization": f"Bearer {token}"},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_config_is_created_with_a_private_token(tmp_path):
    cfg = ps.load_config(tmp_path / "printserver.toml")
    assert len(cfg["token"]) > 20 and cfg["port"] == 8631
    assert (tmp_path / "printserver.toml").stat().st_mode & 0o777 == 0o600
    assert ps.load_config(tmp_path / "printserver.toml")["token"] == cfg["token"], "kept, not regenerated"


def test_printing_needs_the_token_and_reaches_the_printer(monkeypatch):
    sent = []
    monkeypatch.setattr(ps, "send", lambda cfg, data: sent.append(data) or "bluetooth")
    server, url = _serve({**ps.DEFAULTS, "token": "s3cret"})
    try:
        assert _call(url, "/print", "wrong", b"x")[0] == 401 and not sent
        code, body = _call(url, "/print", "s3cret", b"\x1b@hello\n")
        assert code == 200 and body == {"route": "bluetooth", "bytes": 8} and sent == [b"\x1b@hello\n"]
        code, body = _call(url, "/status", "s3cret")
        assert code == 200 and body["last_route"] == "bluetooth" and body["printer"] == ps.DEFAULTS["bluetooth"]

        def away(cfg, data):
            raise OSError("bluetooth: [Errno 112] Host is down")
        monkeypatch.setattr(ps, "send", away)
        code, body = _call(url, "/print", "s3cret", b"x")
        assert code == 503 and "Host is down" in body["error"]
    finally:
        server.shutdown()


def test_usb_goes_first_when_plugged_in(tmp_path, monkeypatch):
    lp = tmp_path / "lp0"
    lp.write_bytes(b"")
    monkeypatch.setattr(ps, "_send_bt", lambda a, d: (_ for _ in ()).throw(AssertionError("not bluetooth")))
    assert ps.send({"usb": str(lp), "bluetooth": "AA:BB"}, b"hi") == "usb" and lp.read_bytes() == b"hi"

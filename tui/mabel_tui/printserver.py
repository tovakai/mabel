"""mabel-print: the deck as print server for the desk's thermal printer (and, later, the one in its casing).

The deck never sleeps, so it owns the printer: it keeps it awake (a status request every 20 min;
the printer's own auto-off is about an hour and only its button brings it back) and prints
what Mabel sends. Mabel's src/persona/printer.py POSTs finished ESC/POS bytes here instead of
opening Bluetooth itself; if the deck or the printer can't be reached, her queue keeps the job.

  POST /print    raw ESC/POS bytes → {"route": "usb" | "bluetooth"} (503 if the printer is away)
  GET  /status   {"printer", "usb", "last_ok", "last_error", "keepalive_minutes"}
Both need "Authorization: Bearer <token>". It listens on the tailnet address only.

Config: ~/.config/mabel-tui/printserver.toml
    token = "…"                          # shared with Mabel (setting printer_server_token)
    bluetooth = "86:67:7A:53:65:6F"      # the printer (RFCOMM channel 1, no pairing)
    usb = "/dev/usb/lp0"                 # used first when it exists (the casing)
    port = 8631
    keepalive_minutes = 20
Run: mabel-print (a systemd user service on the deck: mabel-print.service).
Standard library only.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
import tomllib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mabel-tui" / "printserver.toml"
DEFAULTS = {"bluetooth": "86:67:7A:53:65:6F", "usb": "/dev/usb/lp0", "port": 8631, "keepalive_minutes": 20}
STATUS = b"\x10\x04\x01"                 # DLE EOT 1: a real-time status request, prints nothing
CHUNK = 512
MAX_JOB = 4 * 1024 * 1024

log = logging.getLogger("mabel-print")
_wire = threading.Lock()                 # one job on the printer at a time
state = {"last_ok": None, "last_error": None, "last_route": None}


def load_config(path: Path = CONFIG) -> dict:
    """The config, creating it (with a fresh token) the first time."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {"token": secrets.token_urlsafe(24)}
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f'token = "{data["token"]}"\n')
    return {**DEFAULTS, **data}


def _send_usb(device: str, data: bytes) -> None:
    fd = os.open(device, os.O_WRONLY)
    try:
        for i in range(0, len(data), CHUNK):
            os.write(fd, data[i:i + CHUNK])
    finally:
        os.close(fd)


def _send_bt(address: str, data: bytes) -> None:
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    sock.settimeout(20)
    try:
        sock.connect((address, 1))
        for i in range(0, len(data), CHUNK):      # the printer's buffer is small; let it keep up
            sock.sendall(data[i:i + CHUNK])
            time.sleep(0.04)
        time.sleep(min(8.0, 1.0 + len(data) / 6000))   # closing early can cut the tail off
    finally:
        sock.close()


def send(cfg: dict, data: bytes) -> str:
    """USB if it's plugged in, else Bluetooth (twice: a dozing module often refuses the first)."""
    errors = []
    with _wire:
        if cfg.get("usb") and os.path.exists(cfg["usb"]):
            try:
                _send_usb(cfg["usb"], data)
                return "usb"
            except OSError as exc:
                errors.append(f"usb: {exc}")
        if cfg.get("bluetooth"):
            for attempt in range(2):
                try:
                    _send_bt(cfg["bluetooth"], data)
                    return "bluetooth"
                except OSError as exc:
                    if attempt:
                        errors.append(f"bluetooth: {exc}")
                    else:
                        time.sleep(1.5)
    raise OSError("; ".join(errors) or "no printer configured")


def _note(ok: bool, route: str | None = None, error: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if ok:
        state.update(last_ok=now, last_route=route)
    else:
        state.update(last_error=f"{now} {error}")


def keepalive_loop(cfg: dict, stop: threading.Event) -> None:
    minutes = float(cfg.get("keepalive_minutes") or 0)
    while minutes > 0 and not stop.is_set():
        try:
            route = send(cfg, STATUS)
            _note(True, route)
        except OSError as exc:
            _note(False, error=str(exc))
            log.info("keep-alive: printer not reachable (%s)", exc)
        stop.wait(minutes * 60)


def tailnet_address() -> str | None:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
        return out[0] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def make_handler(cfg: dict):
    token = str(cfg["token"])

    class Handler(BaseHTTPRequestHandler):
        server_version = "mabel-print"

        def _authorized(self) -> bool:
            sent = self.headers.get("Authorization", "")
            if sent.startswith("Bearer ") and secrets.compare_digest(sent[7:], token):
                return True
            self._reply(401, {"error": "unauthorized"})
            return False

        def _reply(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path != "/status":
                return self._reply(404, {"error": "not found"})
            if self._authorized():
                self._reply(200, {"printer": cfg.get("bluetooth"), "usb": bool(cfg.get("usb") and os.path.exists(cfg["usb"])),
                                  "keepalive_minutes": cfg.get("keepalive_minutes"), **state})

        def do_POST(self) -> None:
            if self.path != "/print":
                return self._reply(404, {"error": "not found"})
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_JOB:
                return self._reply(413, {"error": "empty or too big"})
            data = self.rfile.read(length)
            try:
                route = send(cfg, data)
            except OSError as exc:
                _note(False, error=str(exc))
                return self._reply(503, {"error": str(exc)})
            _note(True, route)
            self._reply(200, {"route": route, "bytes": len(data)})

        def log_message(self, fmt, *args) -> None:
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s mabel-print %(levelname)s %(message)s")
    cfg = load_config()
    host = os.environ.get("MABEL_PRINT_HOST") or tailnet_address()
    while host is None:                                # at boot the tailnet may come up after us
        log.info("waiting for the tailnet address")
        time.sleep(10)
        host = tailnet_address()
    stop = threading.Event()
    threading.Thread(target=keepalive_loop, args=(cfg, stop), daemon=True).start()
    server = ThreadingHTTPServer((host, int(cfg["port"])), make_handler(cfg))
    log.info("printing for Mabel on %s:%s (printer %s, usb %s)", host, cfg["port"], cfg.get("bluetooth"), cfg.get("usb"))
    try:
        server.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()

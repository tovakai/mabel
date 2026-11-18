"""Beeper Desktop's local API, relayed for MABEL-OS (routes/beeper_routes.py).

Beeper Desktop runs on the PC (for now: the address is a setting, so a separate "beeper server"
later only changes beeper_url). It bridges WhatsApp, Signal, Telegram and the rest, and serves
its API on 127.0.0.1:23373 with a Bearer token made in Beeper's Settings > Integrations.

The token lives here, in data/beeper.json (owner-only), never in the TUI or a chat. Set it with

    .venv-arch/bin/python -m src.beeper --set-token        # asks for it, hidden
    .venv-arch/bin/python -m src.beeper --status

The relay only passes the messaging endpoints in ALLOWED: account setup, bridge logins, OAuth
and raw Matrix stay out of reach of a TUI token.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:23373"

# (method, path regex relative to the API root) the relay forwards
ALLOWED = [
    ("GET", r"v1/info"),
    ("GET", r"v1/accounts"),
    ("GET", r"v1/labels"),
    ("GET", r"v1/chats"),
    ("GET", r"v1/chats/search"),
    ("GET", r"v1/messages/search"),
    ("GET", r"v1/chats/[^/]+"),
    ("GET", r"v1/chats/[^/]+/messages"),
    ("POST", r"v1/chats/[^/]+/messages"),                       # send (text, replyToMessageID)
    ("PUT", r"v1/chats/[^/]+/messages/[^/]+"),                  # edit
    ("DELETE", r"v1/chats/[^/]+/messages/[^/]+"),               # delete
    ("POST", r"v1/chats/[^/]+/messages/[^/]+/reactions"),
    ("DELETE", r"v1/chats/[^/]+/messages/[^/]+/reactions/[^/]+"),
    ("POST", r"v1/chats/[^/]+/(read|unread|archive)"),
    ("GET", r"v1/assets/serve"),                                # images, as bytes
]
_ALLOWED = [(m, re.compile(rf"^{p}$")) for m, p in ALLOWED]


def allowed(method: str, path: str) -> bool:
    path = path.strip("/")
    if any(seg in ("", ".", "..") for seg in path.split("/")) or "%" in path or "\\" in path:
        return False                                       # no climbing out of the allowed paths
    return any(m == method.upper() and rx.match(path) for m, rx in _ALLOWED)


def _file() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "beeper.json"


def config() -> dict:
    """{"url", "token"}; beeper_url in settings wins over the file's url."""
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    url = data.get("url") or DEFAULT_URL
    try:
        from src.settings import get_setting
        url = get_setting("beeper_url", "") or url
    except Exception:
        pass
    return {"url": url.rstrip("/"), "token": data.get("token") or ""}


def save_config(token: str, url: str | None = None) -> Path:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"url": (url or config()["url"]).rstrip("/"), "token": token.strip()}
    fd = os.open(str(path) + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(str(path) + ".tmp", path)
    return path


def _main() -> None:
    import argparse
    import getpass
    import httpx
    ap = argparse.ArgumentParser(description="Beeper Desktop for MABEL-OS")
    ap.add_argument("--set-token", action="store_true", help="store the API token (asked for, hidden)")
    ap.add_argument("--url", help=f"Beeper's API address (default {DEFAULT_URL})")
    ap.add_argument("--status", action="store_true", help="check the connection")
    args = ap.parse_args()
    if args.set_token:
        token = getpass.getpass("Beeper API token (Beeper > Settings > Integrations): ").strip()
        if not token:
            raise SystemExit("no token given; nothing changed")
        print(f"saved to {save_config(token, args.url)}")
    cfg = config()
    if args.status or args.set_token:
        headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg["token"] else {}
        try:
            info = httpx.get(f"{cfg['url']}/v1/info", timeout=5).json()
            print(f"Beeper {info['app']['version']} at {cfg['url']}")
            r = httpx.get(f"{cfg['url']}/v1/accounts", headers=headers, timeout=10)
            if r.status_code == 200:
                accounts = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
                print(f"token works: {len(accounts)} account(s): "
                      + ", ".join(a.get("network") or a.get("accountID", "?") for a in accounts))
            else:
                print(f"token: {'missing' if not cfg['token'] else 'rejected'} ({r.status_code})")
        except Exception as exc:
            print(f"can't reach Beeper at {cfg['url']}: {exc}")


if __name__ == "__main__":
    _main()

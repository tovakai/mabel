#!/usr/bin/env python3
"""Read-only connectivity diagnostics for the Odysseus Obsidian Companion.

No vault content is read and no token is printed. The API token is accepted
only through the ODYSSEUS_API_TOKEN environment variable, not argv.
"""
from __future__ import annotations

import argparse
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

DEFAULT_ORIGIN = "app://obsidian.md"
TIMEOUT_SECONDS = 8


def normalize_base_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("--url must be an http(s) server URL")
    if parts.username or parts.password:
        raise ValueError("Credentials must not be embedded in --url")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("--url should contain only scheme and host[:port]")
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


def request_status(request: Request) -> tuple[int, dict[str, str], str]:
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return (
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                "",
            )
    except HTTPError as exc:
        # Do not print response bodies, which could contain private content.
        return (
            exc.code,
            {key.lower(): value for key, value in exc.headers.items()},
            "http",
        )
    except (URLError, OSError, TimeoutError) as exc:
        return 0, {}, type(exc).__name__


def run_checks(base: str, token: str, origin: str) -> int:
    print(f"Mabel server: {base}")
    print("API token: provided via environment (not displayed)")

    ping = Request(
        f"{base}/api/companion/ping",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    code, _, error = request_status(ping)
    if code == 200:
        print("[OK] Authenticated companion ping")
    else:
        print(f"[FAIL] Companion ping returned {code or error}; check URL, server, token and scope")

    cors = Request(
        f"{base}/api/chat_stream",
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    cors_code, headers, cors_error = request_status(cors)
    allow_origin = headers.get("access-control-allow-origin", "")
    allow_methods = headers.get("access-control-allow-methods", "")
    allow_headers = headers.get("access-control-allow-headers", "").lower()
    cors_ok = (
        cors_code in (200, 204)
        and allow_origin == origin
        and "POST" in {x.strip().upper() for x in allow_methods.split(",")}
        and (
            "*" in allow_headers
            or ("authorization" in allow_headers and "content-type" in allow_headers)
        )
    )
    if cors_ok:
        print("[OK] Obsidian streaming CORS preflight")
    else:
        print(f"[WARN] Streaming CORS preflight failed ({cors_code or cors_error}); check ALLOWED_ORIGINS={origin}")
        print("       Non-streaming requestUrl may still work.")

    return 0 if code == 200 and cors_ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Mabel server base URL, no credentials")
    parser.add_argument("--origin", default=DEFAULT_ORIGIN, help="Obsidian streaming origin")
    args = parser.parse_args(argv)
    token = os.environ.get("ODYSSEUS_API_TOKEN", "").strip()
    if not token:
        print("Set ODYSSEUS_API_TOKEN in the environment; never put it on the command line.", file=sys.stderr)
        return 2
    try:
        base = normalize_base_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))
    return run_checks(base, token, args.origin)


if __name__ == "__main__":
    raise SystemExit(main())

"""Local services Mabel's web UI shows in a window (the "hub").

Each service stays its own process with its own web UI; Mabel only frames it.
The frame URL depends on how Mabel itself was reached:

- on loopback (127.0.0.1 / localhost): the service's loopback port;
- on the tailnet (tailscale serve): the same host on the service's tailnet port.

That keeps the iframe on an origin the service already accepts writes from
(tk-index allows its loopback origin plus the --allow-origin tailnet URL), so
nothing in the service has to change. The CSP in core/middleware.py allows
exactly these frame origins and no others.
"""
from __future__ import annotations

HUB_SERVICES: dict[str, dict] = {
    "gremlin": {
        "label": "Gremlin Index",
        "local_port": 8766,      # tk-index-web.service --port
        "tailnet_port": 8765,    # tailscale serve :8765 -> 127.0.0.1:8766
    },
}

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _hostname(host_header: str | None) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):                      # [::1]:7000
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def service_url(service: dict, host_header: str | None) -> str:
    hostname = _hostname(host_header)
    if not hostname or hostname in _LOOPBACK:
        return f"http://127.0.0.1:{service['local_port']}/"
    return f"https://{hostname}:{service['tailnet_port']}/"


def is_up(service: dict, timeout: float = 0.4) -> bool:
    """The service's loopback port accepts a connection (the browser can't probe it: connect-src 'self')."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", service["local_port"]), timeout=timeout):
            return True
    except OSError:
        return False


def resolve(host_header: str | None) -> list[dict]:
    return [{"id": key, "label": svc["label"], "url": service_url(svc, host_header), "up": is_up(svc)}
            for key, svc in HUB_SERVICES.items()]


def frame_sources(host_header: str | None) -> str:
    """Space-separated CSP frame-src origins for this request (no trailing slashes)."""
    origins = []
    for svc in HUB_SERVICES.values():
        origins.append(f"http://127.0.0.1:{svc['local_port']}")
        url = service_url(svc, host_header).rstrip("/")
        if url not in origins:
            origins.append(url)
    return " ".join(origins)

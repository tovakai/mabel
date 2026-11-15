"""Hub windows: URL per how Mabel was reached, and the CSP frame-src that allows exactly those."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware
from src import hub_services
from src.hub_services import frame_sources, resolve, service_url

GREMLIN = hub_services.HUB_SERVICES["gremlin"]
TAILNET = "tovakai.example.ts.net"


def test_loopback_hosts_frame_the_loopback_port():
    for host in ("127.0.0.1:7000", "localhost:7000", "[::1]:7000", "", None):
        assert service_url(GREMLIN, host) == "http://127.0.0.1:8766/"


def test_tailnet_host_frames_the_tailnet_port_on_the_same_host():
    assert service_url(GREMLIN, TAILNET) == f"https://{TAILNET}:8765/"
    assert service_url(GREMLIN, f"{TAILNET}:443") == f"https://{TAILNET}:8765/"


def test_frame_sources_are_origins_only():
    assert frame_sources("127.0.0.1:7000") == "http://127.0.0.1:8766"
    assert frame_sources(TAILNET) == f"http://127.0.0.1:8766 https://{TAILNET}:8765"


def test_resolve_reports_down_service(monkeypatch):
    monkeypatch.setattr(hub_services, "is_up", lambda svc, timeout=0.4: False)
    [svc] = resolve("127.0.0.1:7000")
    assert svc == {"id": "gremlin", "label": "Gremlin Index", "url": "http://127.0.0.1:8766/", "up": False}


def _csp(host):
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def root():
        return {}

    return TestClient(app).get("/", headers={"host": host}).headers["content-security-policy"]


def test_csp_frame_src_allows_hub_and_still_denies_being_framed():
    csp = _csp(TAILNET)
    assert f"frame-src 'self' http://127.0.0.1:8766 https://{TAILNET}:8765;" in csp
    assert "frame-ancestors 'none'" in csp
    assert "frame-src 'self' http://127.0.0.1:8766;" in _csp("127.0.0.1:7000")

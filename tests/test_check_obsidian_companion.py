"""Offline smoke-checker regression tests (no live server or real token)."""
import pytest

from scripts import check_obsidian_companion as checker


@pytest.mark.parametrize(
    "invalid",
    [
        "", "localhost:7011", "ftp://localhost:7011",
        "https://user:password@example.com",
        "http://localhost:7011/api", "https://localhost:7011?token=secret",
        "https://localhost:7011/#section",
    ],
)
def test_reject_unexpected_or_sensitive_base_urls(invalid):
    with pytest.raises(ValueError):
        checker.normalize_base_url(invalid)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://localhost:7011/", "http://localhost:7011"),
        ("https://server.example:443", "https://server.example:443"),
    ],
)
def test_normalize_server_base(given, expected):
    assert checker.normalize_base_url(given) == expected


def test_health_checker_redacts_token_and_checks_preflight(monkeypatch, capsys):
    requests = []
    token = "test-private-api-token-do-not-echo"

    def fake_request(request):
        requests.append(request)
        if request.get_method() == "OPTIONS":
            assert "Authorization" not in request.headers
            assert request.headers.get("Origin") == "app://obsidian.md"
            return 200, {
                "access-control-allow-origin": "app://obsidian.md",
                "access-control-allow-methods": "GET, POST",
                "access-control-allow-headers": "authorization, content-type",
            }, ""
        assert request.headers["Authorization"] == f"Bearer {token}"
        return 200, {}, ""

    monkeypatch.setattr(checker, "request_status", fake_request)
    assert checker.run_checks("http://localhost:7011", token, "app://obsidian.md") == 0
    assert len(requests) == 2
    assert token not in capsys.readouterr().out


def test_cors_origin_must_match_exactly(monkeypatch):
    def fake_request(request):
        if request.get_method() == "OPTIONS":
            return 200, {
                "access-control-allow-origin": "https://other.site",
                "access-control-allow-methods": "POST",
                "access-control-allow-headers": "authorization,content-type",
            }, ""
        return 200, {}, ""

    monkeypatch.setattr(checker, "request_status", fake_request)
    assert checker.run_checks("http://localhost:7011", "fake", "app://obsidian.md") == 1

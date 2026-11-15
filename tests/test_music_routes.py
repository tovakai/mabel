"""Music proxy to Navidrome: allow-list, credentials stay server-side, Range streaming."""
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import require_admin
from routes import music_routes
from src import navidrome as nd
from src.auth_helpers import require_user

CFG = nd.Config(url="http://nd.test:4533", user="mabel", password="hunter2")
AUDIO = bytes(range(256)) * 40


def ok(**extra):
    return {"subsonic-response": {"status": "ok", "version": "1.16.1", "serverVersion": "0.64.2", **extra}}


def fake_navidrome(request: httpx.Request) -> httpx.Response:
    q = parse_qs(urlparse(str(request.url)).query)
    # Every call carries token auth, never the password.
    assert q["u"] == ["mabel"] and "p" not in q
    assert q["t"][0] == hashlib.md5(("hunter2" + q["s"][0]).encode()).hexdigest()
    method = request.url.path.rsplit("/", 1)[-1]
    if method == "stream":
        if q["id"] == ["bad"]:
            return httpx.Response(200, json={"subsonic-response": {"status": "failed", "error": {"code": 70, "message": "not found"}}})
        rng = request.headers.get("range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            body = AUDIO[start:]
            return httpx.Response(206, stream=httpx.ByteStream(body), headers={
                "content-type": "audio/mpeg", "content-length": str(len(body)), "accept-ranges": "bytes",
                "content-range": f"bytes {start}-{len(AUDIO) - 1}/{len(AUDIO)}"})
        return httpx.Response(200, stream=httpx.ByteStream(AUDIO), headers={"content-type": "audio/mpeg", "content-length": str(len(AUDIO))})
    if method == "search3":
        return httpx.Response(200, json=ok(searchResult3={"query": q.get("query"), "u_seen": q["u"]}))
    if method == "star":
        return httpx.Response(200, json=ok(starred=q["id"]))
    return httpx.Response(200, json=ok())


@pytest.fixture
def client(monkeypatch):
    transport = httpx.MockTransport(fake_navidrome)
    real_client = httpx.AsyncClient

    monkeypatch.setattr(nd, "load_config", lambda: CFG)
    monkeypatch.setattr(nd.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    monkeypatch.setattr(music_routes, "_stream_client", None)
    monkeypatch.setattr(music_routes, "_client", lambda: real_client(transport=transport))
    app = FastAPI()
    app.include_router(music_routes.setup_music_routes())
    app.dependency_overrides[require_user] = lambda: "owner"
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_read_passes_params_but_not_browser_credentials(client):
    r = client.get("/api/music/rest/search3", params={"query": "boards", "u": "evil", "t": "x"})
    assert r.status_code == 200
    assert r.json()["searchResult3"] == {"query": ["boards"], "u_seen": ["mabel"]}


def test_unknown_and_write_methods_are_refused_on_get(client):
    assert client.get("/api/music/rest/deleteUser").status_code == 404
    assert client.get("/api/music/rest/star", params={"id": "x"}).status_code == 405


def test_write_goes_by_post_with_repeated_ids(client):
    r = client.post("/api/music/rest/star", json={"id": ["a1", "a2"]})
    assert r.status_code == 200 and r.json()["starred"] == ["a1", "a2"]


def test_stream_full_and_range(client):
    r = client.get("/api/music/stream/song1")
    assert r.status_code == 200 and r.content == AUDIO
    assert r.headers["content-encoding"] == "identity"
    assert r.headers["content-length"] == str(len(AUDIO))
    r = client.get("/api/music/stream/song1", headers={"Range": "bytes=1000-"})
    assert r.status_code == 206 and r.content == AUDIO[1000:]
    assert r.headers["content-range"] == f"bytes 1000-{len(AUDIO) - 1}/{len(AUDIO)}"


def test_stream_error_body_becomes_an_error_not_audio(client):
    r = client.get("/api/music/stream/bad")
    assert r.status_code == 502 and "not found" in r.json()["detail"]


def test_bad_ids_rejected(client):
    assert client.get("/api/music/stream/..%2Fetc").status_code in (400, 404)
    assert client.get("/api/music/cover/a b").status_code == 400


def test_config_tests_login_then_saves_encrypted(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(nd, "save_config", lambda url, user, pw: saved.update(url=url, user=user, pw=pw))
    r = client.post("/api/music/config", json={"url": "http://nd.test:4533/app/", "user": "mabel", "password": "hunter2"})
    assert r.status_code == 200 and r.json()["server"] == "0.64.2"
    assert saved == {"url": "http://nd.test:4533", "user": "mabel", "pw": "hunter2"}


def test_save_config_encrypts_password(monkeypatch):
    store = {}
    import src.settings as settings
    monkeypatch.setattr(settings, "load_settings", lambda: {})
    monkeypatch.setattr(settings, "save_settings", lambda s: store.update(s))
    nd.save_config("http://x:1/", "u", "secret")
    assert store["music_navidrome_password"].startswith("enc:")
    assert "secret" not in json.dumps(store)


def test_unwrap_maps_auth_errors_to_401():
    with pytest.raises(nd.MusicError) as exc:
        nd.unwrap({"subsonic-response": {"status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}})
    assert exc.value.status == 401

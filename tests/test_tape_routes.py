"""The DreamTape repo (/api/tapes): what a .dtape may contain, and publish → list → download → unpublish."""
import io
import tarfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import tape_routes


def dtape(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


GOOD = {
    "tape.toml": b'title = "Snake"\nauthor = "tovakai"\n',
    "tape.wasm": b"\0asm\1\0\0\0",
    "art/hero.mpx": b"{}",
    "music/theme.mtk": b"{}",
}


def test_a_tape_may_hold_only_its_own_files():
    assert tape_routes.read_tape(dtape(GOOD))["title"] == "Snake"
    for bad in ({"tape.toml": b""}, {**GOOD, "../evil": b"x"}, {**GOOD, "art/x.sh": b"x"}, {**GOOD, "/etc/passwd": b"x"}):
        with pytest.raises(ValueError):
            tape_routes.read_tape(dtape(bad))
    with pytest.raises(ValueError):
        tape_routes.read_tape(b"not a tar file at all")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tape_routes, "TAPES_DIR", tmp_path)
    app = FastAPI()
    app.include_router(tape_routes.setup_tape_routes())
    app.dependency_overrides[tape_routes.tapes_user] = lambda: "owner"
    return TestClient(app)


def test_publish_list_download_unpublish(client):
    assert client.get("/api/tapes").json() == {"tapes": []}
    r = client.put("/api/tapes/snake", content=dtape(GOOD))
    assert r.status_code == 200 and r.json()["title"] == "Snake"
    listed = client.get("/api/tapes").json()["tapes"]
    assert [t["name"] for t in listed] == ["snake"] and listed[0]["author"] == "tovakai"
    assert client.get("/api/tapes/snake.dtape").content == dtape(GOOD)
    assert client.put("/api/tapes/bad", content=dtape({**GOOD, "../x": b"x"})).status_code == 400
    assert client.put("/api/tapes/no.dots", content=dtape(GOOD)).status_code in (400, 404)
    assert client.delete("/api/tapes/snake").status_code == 200
    assert client.get("/api/tapes/snake.dtape").status_code == 404

import asyncio
import io
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from src import image_service

SERVICES = {"image": {"url": "http://127.0.0.1:8110", "command": ["py", "--model", "/m/pixel-party-xl"]}}


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(image_service, "_configured_services", lambda: SERVICES)
    monkeypatch.setattr(image_service, "_ledger", lambda *a, **k: None)
    monkeypatch.setattr(image_service, "_task", None)
    return tmp_path


def _png(size=64, bg=(255, 255, 255), box=(16, 16, 48, 48), fill=(200, 30, 30)):
    img = Image.new("RGB", (size, size), bg)
    img.paste(fill, box)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _room(log):
    @asynccontextmanager
    async def room(service):
        log.append(("swap", service))
        yield "http://127.0.0.1:8110"
        log.append(("back", service))
    return room


def _sig(**kw):
    base = dict(chat_active=False, idle="idle", idle_minutes=45, gpu="free", gpu_reason="", strata=True)
    return SimpleNamespace(**{**base, **kw})


def test_enqueue_clamps_and_reports_position(svc):
    first = image_service.enqueue("a wire stripper", width=4000, height=100, client="gremlin", data_dir=svc)
    second = image_service.enqueue("a soldering iron", data_dir=svc)
    assert (first["position"], second["position"]) == (1, 2)
    job = image_service._load(svc)[0]
    assert (job["width"], job["height"]) == (1024, 512)
    assert image_service.status(second["job"], svc)["position"] == 2


def test_enqueue_rejects_bad_requests_and_a_full_queue(svc):
    with pytest.raises(ValueError):
        image_service.enqueue("   ", data_dir=svc)
    with pytest.raises(ValueError):
        image_service.enqueue("x", service="image-nope", data_dir=svc)
    for i in range(image_service.QUEUE_CAP):
        image_service.enqueue(f"thing {i}", data_dir=svc)
    with pytest.raises(image_service.Busy):
        image_service.enqueue("one too many", data_dir=svc)


def test_whole_queue_in_one_swap_and_one_failure_doesnt_stop_it(svc):
    ids = [image_service.enqueue(p, data_dir=svc)["job"] for p in ("ok one", "broken", "ok two")]
    log = []

    async def render(client, url, model, job):
        assert model == "pixel-party-xl"
        if job["prompt"] == "broken":
            raise RuntimeError("the image model answered 500")
        return _png()

    out = asyncio.run(image_service.process(data_dir=svc, room=_room(log), render=render))
    assert out == {"done": 2, "failed": 1}
    assert log == [("swap", "image"), ("back", "image")]
    states = [image_service.status(i, svc)["state"] for i in ids]
    assert states == ["done", "failed", "done"]
    assert image_service.status(ids[1], svc)["error"] == "the image model answered 500"
    assert image_service.result_path(ids[0], svc).read_bytes()[:4] == b"\x89PNG"
    assert all("prompt" not in j for j in image_service._load(svc))   # dropped once finished


def test_a_failed_swap_fails_the_batch_with_a_reason(svc):
    job = image_service.enqueue("x", data_dir=svc)["job"]

    @asynccontextmanager
    async def room(service):
        raise RuntimeError("her model is still busy (1 call(s) running)")
        yield

    out = asyncio.run(image_service.process(data_dir=svc, room=room, render=None))
    assert out["failed"] == 1
    assert "couldn't get the GPU" in image_service.status(job, svc)["error"]


def test_nothing_runs_while_gaming(svc):
    (svc / "gpu_swap").mkdir()
    (svc / "gpu_swap" / "game_mode.json").write_text(json.dumps({"game": "Factorio"}))
    job = image_service.enqueue("x", data_dir=svc)["job"]
    log = []
    asyncio.run(image_service.process(data_dir=svc, room=_room(log), render=None))
    assert log == [] and image_service.status(job, svc)["state"] == "queued"

    async def try_now():
        image_service.start_now(data_dir=svc)
    with pytest.raises(image_service.Busy, match="Factorio"):
        asyncio.run(try_now())


def test_start_now_needs_something_queued(svc):
    async def try_now():
        image_service.start_now(data_dir=svc)
    with pytest.raises(image_service.Busy, match="nothing"):
        asyncio.run(try_now())


def test_gate_waits_for_idle_and_a_short_gathering(svc, monkeypatch):
    monkeypatch.setattr("src.gpu_swap.active", lambda: None)
    assert image_service.gate(_sig(), data_dir=svc) == ["nothing queued"]
    t0 = datetime.now(timezone.utc)
    image_service.enqueue("x", data_dir=svc, now=t0)
    assert "letting a few requests gather" in image_service.gate(_sig(), data_dir=svc, now=t0)
    later = t0 + timedelta(minutes=5)
    assert image_service.gate(_sig(), data_dir=svc, now=later) == []
    assert image_service.gate(_sig(chat_active=True, idle="active"), data_dir=svc, now=later)


def test_finished_jobs_are_forgotten_after_a_day(svc):
    job = image_service.enqueue("x", data_dir=svc)["job"]
    asyncio.run(image_service.process(data_dir=svc, room=_room([]), render=lambda *a: asyncio.sleep(0, _png())))
    assert image_service.result_path(job, svc)
    image_service.prune(svc, now=datetime.now(timezone.utc) + timedelta(days=2))
    assert image_service.status(job, svc) is None
    assert not list((svc / "image_service" / "results").iterdir())


def test_cut_out_removes_a_plain_background_but_keeps_the_object():
    out = image_service.cut_out(_png())
    alpha = np.asarray(Image.open(io.BytesIO(out)))[..., 3]
    assert alpha[0, 0] == 0 and alpha[32, 32] == 255


def test_cut_out_keeps_background_coloured_areas_inside_the_object():
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    img.paste((0, 0, 0), (10, 10, 54, 54))
    img.paste((255, 255, 255), (24, 24, 40, 40))       # a white hole, enclosed
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    alpha = np.asarray(Image.open(io.BytesIO(image_service.cut_out(buf.getvalue()))))[..., 3]
    assert alpha[0, 0] == 0 and alpha[32, 32] == 255


def test_cut_out_gives_up_on_a_busy_background():
    noise = (np.random.default_rng(1).integers(0, 255, (64, 64, 3))).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(noise).save(buf, format="PNG")
    assert image_service.cut_out(buf.getvalue()) is None


def test_transparent_job_asks_for_a_plain_background_and_records_the_cut(svc):
    job = image_service.enqueue("a lamp", transparent=True, data_dir=svc)["job"]
    seen = []

    async def render(client, url, model, j):
        seen.append(j["prompt"])
        return _png()

    asyncio.run(image_service.process(data_dir=svc, room=_room([]), render=render))
    assert image_service.status(job, svc)["background_removed"] is True
    assert Image.open(image_service.result_path(job, svc)).mode == "RGBA"


def test_render_adds_the_plain_background_and_model_preset(monkeypatch):
    sent = {}

    class Client:
        async def post(self, url, json):
            sent.update(json, url=url)
            import base64
            return SimpleNamespace(status_code=200, json=lambda: {"data": [{"b64_json": base64.b64encode(b"png").decode()}]})

    job = {"prompt": "a lamp", "width": 768, "height": 768, "transparent": True}
    assert asyncio.run(image_service._render(Client(), "http://x:1", "pixel-party-xl", job)) == b"png"
    assert sent["url"] == "http://x:1/v1/images/generations"
    assert "plain flat white background" in sent["prompt"] and "pixel art style" in sent["prompt"]
    assert sent["negative_prompt"] and sent["size"] == "768x768"

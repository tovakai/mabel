"""mabel-tui: the chat stream parser and Mabel's music commands (2026-11-17)."""
import asyncio
import json

import httpx

from mabel_tui.api import Mabel
from mabel_tui.player import Player


def _mabel(handler) -> Mabel:
    m = Mabel("http://mabel.test", "ody_test")
    m.http = httpx.AsyncClient(base_url=m.url, transport=httpx.MockTransport(handler))
    return m


def test_stream_yields_deltas_events_and_errors():
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in (
        {"type": "turn_mode", "mode": "chat"}, {"delta": "Hi"}, {"delta": " there"},
        {"type": "ui_control", "data": {"music_command": {"op": "pause"}}},
        {"error": "model hiccup", "status": 503})) + "data: [DONE]\n\ndata: {\"delta\": \"after done\"}\n\n"
    seen = {}

    def handler(request):
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, text=body)

    async def run():
        return [e async for e in _mabel(handler).stream("s1", "hello", approval=("ap1", "approve_once"))]

    events = asyncio.run(run())
    assert [e.get("delta") for e in events if "delta" in e] == ["Hi", " there"]          # nothing after [DONE]
    assert events[-1] == {"error": "model hiccup", "status": 503, "type": "error"}
    assert seen["tool_approval_id"] == "ap1" and seen["tool_approval_decision"] == "approve_once"
    assert seen["message"] == "" and seen["session"] == "s1"


def test_http_errors_become_an_error_event():
    events = asyncio.run(_collect(_mabel(lambda r: httpx.Response(403, text="missing scope")).stream("s", "x")))
    assert events == [{"type": "error", "error": "403: missing scope"}]


async def _collect(gen):
    return [e async for e in gen]


def test_mabels_music_commands_drive_the_player():
    calls = []
    player = Player(lambda i: i, "ody_test")

    async def record(name, *args):
        calls.append((name, args))
    for name in ("play", "enqueue", "play_next", "set_paused", "next", "previous"):
        setattr(player, name, (lambda n: lambda *a: record(n, *a))(name))
    songs = [{"id": "a"}, {"id": "b"}]
    for op in ("play", "queue", "play_next", "pause", "resume", "next", "previous"):
        asyncio.run(player.apply({"op": op, "songs": songs}))
    assert [c[0] for c in calls] == ["play", "enqueue", "play_next", "set_paused", "set_paused", "next", "previous"]
    assert calls[3][1] == (True,) and calls[4][1] == (False,)


def test_mpris_tells_the_desktop_what_is_playing(tmp_path, monkeypatch):
    from mabel_tui import mpris
    monkeypatch.setattr(mpris, "CACHE", tmp_path)
    (tmp_path / "cover-al1.jpg").write_bytes(b"jpg")
    player = Player(lambda i: i, "ody_test")
    iface = mpris.PlayerIface(player)
    assert iface.status() == "Stopped" and set(iface.metadata()) == {"mpris:trackid"}
    player.queue, player.index, player.duration = [{"id": "s-1", "title": "Roads", "artist": "Portishead",
                                                    "album": "Dummy", "coverArt": "al1"}], 0, 302.5
    meta = {k: v.value for k, v in iface.metadata().items()}
    assert meta["xesam:title"] == "Roads" and meta["xesam:artist"] == ["Portishead"]
    assert meta["mpris:length"] == 302_500_000 and meta["mpris:artUrl"].endswith("cover-al1.jpg")
    assert iface.status() == "Playing"
    player.paused = True
    assert iface.status() == "Paused"


def test_hjkl_moves_to_the_section_in_that_direction():
    from types import SimpleNamespace
    from textual.geometry import Region
    from mabel_tui import nav

    def sec(name, x, y, w, h):
        return SimpleNamespace(name=name, region=Region(x, y, w, h))
    search, results, deck = sec("search", 0, 0, 100, 3), sec("results", 0, 3, 100, 40), sec("deck", 100, 0, 70, 43)
    secs = [search, results, deck]
    assert nav.neighbour(search, secs, "j").name == "results"
    assert nav.neighbour(results, secs, "l").name == "deck"
    assert nav.neighbour(deck, secs, "h").name in ("search", "results")
    assert nav.neighbour(results, secs, "k").name == "search"
    assert nav.neighbour(search, secs, "k") is None

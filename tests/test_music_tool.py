"""control_music: resolve what to play against Navidrome, hand the browser one command."""
import asyncio
import json

import pytest

from src import navidrome as nd
from src.agent_tools.music_tool import MusicTool
from src.turn_contract import _MEDIA_REQUESTS, FAMILY_TOOLS, _REQUIRED_TOOLS

CFG = nd.Config(url="http://nd.test", user="mabel", password="pw")


def s(i, title=None, artist="Boards of Canada", album="Geogaddi"):
    return {"id": f"s{i}", "title": title or f"Song {i}", "artist": artist, "album": album, "albumId": "al1",
            "coverArt": "al1", "duration": 200, "path": "/secret/path.flac"}


LIBRARY = {
    "search3": lambda p: {"searchResult3": {
        "artist": [{"id": "ar1", "name": "Boards of Canada"}] if "boards" in p["query"].lower() else [],
        "album": [{"id": "al1", "name": "Geogaddi", "artist": "Boards of Canada"}] if "geogaddi" in p["query"].lower() else [],
        "song": [s(1, "Dayvan Cowboy")] if "dayvan" in p["query"].lower() else [],
    }},
    "getArtist": lambda p: {"artist": {"id": "ar1", "name": "Boards of Canada", "album": [{"id": "al1"}, {"id": "al2"}]}},
    "getAlbum": lambda p: {"album": {"id": p["id"], "song": [s(f"{p['id']}-{i}") for i in range(3)]}},
    "getGenres": lambda p: {"genres": {"genre": [{"value": "Ambient", "songCount": 40}, {"value": "IDM", "songCount": 12}]}},
    "getSongsByGenre": lambda p: {"songsByGenre": {"song": [s(i) for i in range(5)]}},
    "getRandomSongs": lambda p: {"randomSongs": {"song": [s(i) for i in range(int(p["size"]))]}},
    "getAlbumList2": lambda p: {"albumList2": {"album": [{"id": "new1", "name": "Fresh", "artist": "Someone"}]}},
    "getPlaylists": lambda p: {"playlists": {"playlist": [{"id": "pl1", "name": "Soldering"}]}},
    "getPlaylist": lambda p: {"playlist": {"entry": [s(7), s(8)]}},
    "getNowPlaying": lambda p: {"nowPlaying": {"entry": [{"username": "mabel", "title": "Roygbiv", "artist": "Boards of Canada"}]}},
}


@pytest.fixture
def tool():
    calls = []

    async def fake_call(cfg, method, pairs):
        calls.append(method)
        return LIBRARY[method](dict(pairs))

    t = MusicTool(cfg_loader=lambda: CFG, call=fake_call)
    t.calls = calls
    return t


def run(tool, **args):
    return asyncio.run(tool.execute(json.dumps(args), {}))


def test_play_artist_sends_shuffled_songs_once(tool):
    out = run(tool, action="play", query="Boards of Canada")
    assert out["exit_code"] == 0 and out["ui_event"] == "music"
    cmd = out["music_command"]
    assert cmd["op"] == "play" and len(cmd["nonce"]) == 32
    assert {x["id"] for x in cmd["songs"]} == {f"sal{a}-{i}" for a in (1, 2) for i in range(3)}
    assert "path" not in cmd["songs"][0]                      # only what the player needs
    assert "everything by Boards of Canada" in json.loads(out["output"])["status"]
    told = json.loads(out["output"])
    assert len(told["tracks"]) == 6 and all(" — Boards of Canada" in t for t in told["tracks"])  # she sees the real list


def test_album_keeps_order_and_queue_op(tool):
    out = run(tool, action="queue", query="Geogaddi")
    assert out["music_command"]["op"] == "queue"
    assert [x["id"] for x in out["music_command"]["songs"]] == ["sal1-0", "sal1-1", "sal1-2"]


def test_single_song_and_genre_and_playlist(tool):
    assert [x["id"] for x in run(tool, action="play_next", query="Dayvan Cowboy")["music_command"]["songs"]] == ["s1"]
    assert len(run(tool, action="play", query="ambient", kind="genre")["music_command"]["songs"]) == 5
    assert [x["id"] for x in run(tool, action="play", query="soldering", kind="playlist")["music_command"]["songs"]] == ["s7", "s8"]


def test_random_latest(tool):
    assert len(run(tool, action="play", kind="random", count=7)["music_command"]["songs"]) == 7
    out = run(tool, action="play", kind="latest")
    assert "Fresh" in json.loads(out["output"])["status"]


def test_nothing_found_is_an_error_without_command(tool):
    out = run(tool, action="play", query="zzz nothing")
    assert out["exit_code"] == 1 and "ui_event" not in out


def test_controls_need_no_navidrome(tool):
    out = run(tool, action="skip")
    assert out["music_command"]["op"] == "next" and tool.calls == []


def test_reads_have_no_command(tool):
    out = run(tool, action="now_playing")
    assert "ui_event" not in out and json.loads(out["output"])["now_playing"]["title"] == "Roygbiv"
    assert "ui_event" not in run(tool, action="genres")


def test_not_connected(tool):
    t = MusicTool(cfg_loader=lambda: nd.Config("", "", ""), call=None)
    assert "isn't connected" in asyncio.run(t.execute('{"action": "play", "query": "x"}', {}))["error"]


def test_music_family_is_wired():
    assert FAMILY_TOOLS["music"] == frozenset({"control_music"})
    assert _REQUIRED_TOOLS["music"] == "control_music"


MUSIC = dict(_MEDIA_REQUESTS)["music"]


@pytest.mark.parametrize("text", [
    "play Boards of Canada", "Mabel, can you play something chill?", "put on some jazz", "queue up the new album",
    "shuffle my starred songs", "pause the music", "skip", "skip this song", "next song", "what's playing?",
    "what song is this", "can you play some music for soldering", "resume the music",
])
def test_routes_music_requests(text):
    assert MUSIC.search(text)


@pytest.mark.parametrize("text", [
    "let's play a game", "play chess with me", "role-play as a pirate", "put the meeting on my calendar",
    "put on my calendar: dentist friday", "queue up a research job on batteries", "play it safe", "stop",
    "I like this album", "play around with the config", "shuffle the list order", "put on the kettle",
])
def test_does_not_route_other_requests(text):
    assert not MUSIC.search(text)

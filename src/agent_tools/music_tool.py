"""control_music: Anthon asks Mabel to play, queue or control music.

The music plays in Mabel's web UI (static/js/music.js) from Navidrome on the
other PC. This tool resolves what to play against Navidrome on the server
(src/navidrome.py) and hands the browser a command through the ui_control
event (ui_event "music"). The player in the tab he's chatting from applies it
once (a nonce guards against the event arriving twice).

Reads (now_playing, search, genres) only talk to Navidrome. Playback actions
change what's playing in his browser, nothing else.
"""
from __future__ import annotations

import json
import random
import re
import uuid

from src import navidrome as nd

PLAY_ACTIONS = ("play", "queue", "play_next")
CONTROL_ACTIONS = ("pause", "resume", "next", "previous")
READ_ACTIONS = ("now_playing", "search", "genres")
KINDS = ("auto", "song", "album", "artist", "playlist", "genre", "random", "starred", "latest")
MAX_SONGS = 200
ALIASES = {
    "add": "queue", "enqueue": "queue", "add_to_queue": "queue", "up_next": "play_next", "next_up": "play_next",
    "stop": "pause", "unpause": "resume", "continue": "resume", "skip": "next", "prev": "previous", "back": "previous",
    "what_is_playing": "now_playing", "current": "now_playing", "find": "search", "list_genres": "genres",
    "shuffle": "play",
}


def _fold(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").casefold()).strip()


def _song(raw: dict) -> dict:
    """The fields the browser player needs (static/js/music.js song())."""
    return {k: raw.get(k) for k in ("id", "title", "artist", "artistId", "album", "albumId", "coverArt", "duration", "track")
            if raw.get(k) not in (None, "")}


def _label(songs: list[dict]) -> str:
    if len(songs) == 1:
        s = songs[0]
        return f"“{s.get('title')}” by {s.get('artist') or 'unknown artist'}"
    return f"{len(songs)} songs"


class MusicTool:
    def __init__(self, cfg_loader=nd.load_config, call=nd.call):
        self._cfg = cfg_loader
        self._call = call

    async def _api(self, method: str, **params) -> dict:
        pairs = []
        for k, v in params.items():
            if v is None:
                continue
            pairs.append((k, str(v).lower() if isinstance(v, bool) else str(v)))
        return await self._call(self._cfg(), method, pairs)

    async def _album_songs(self, album_id: str) -> list[dict]:
        return (await self._api("getAlbum", id=album_id)).get("album", {}).get("song", []) or []

    async def _artist_songs(self, artist_id: str) -> list[dict]:
        albums = (await self._api("getArtist", id=artist_id)).get("artist", {}).get("album", []) or []
        songs: list[dict] = []
        for album in albums[:40]:
            songs += await self._album_songs(album["id"])
            if len(songs) >= MAX_SONGS:
                break
        return songs

    async def _genre(self, query: str) -> str | None:
        genres = (await self._api("getGenres")).get("genres", {}).get("genre", []) or []
        q = _fold(query)
        names = [g.get("value", "") for g in genres]
        return next((n for n in names if _fold(n) == q), None) or next((n for n in names if q and q in _fold(n)), None)

    async def resolve(self, kind: str, query: str, count: int) -> tuple[list[dict], str, bool]:
        """Songs to play, a human description, and whether shuffling suits it by default."""
        q = (query or "").strip()
        if kind == "random":
            genre = await self._genre(q) if q else None
            songs = (await self._api("getRandomSongs", size=count, genre=genre)).get("randomSongs", {}).get("song", []) or []
            return songs, f"a random mix{f' of {genre}' if genre else ''}", False
        if kind == "starred":
            songs = (await self._api("getStarred2")).get("starred2", {}).get("song", []) or []
            return songs, "his starred songs", True
        if kind == "latest":
            albums = (await self._api("getAlbumList2", type="newest", size=max(1, min(count // 10, 8)) or 3)).get("albumList2", {}).get("album", []) or []
            songs = []
            for a in albums:
                songs += await self._album_songs(a["id"])
            names = ", ".join(f"{a.get('name')} ({a.get('artist')})" for a in albums[:3])
            return songs, f"the latest added albums: {names}{'…' if len(albums) > 3 else ''}", False
        if kind == "playlist":
            lists = (await self._api("getPlaylists")).get("playlists", {}).get("playlist", []) or []
            fq = _fold(q)
            hit = next((p for p in lists if _fold(p.get("name")) == fq), None) \
                or next((p for p in lists if fq and fq in _fold(p.get("name"))), None)
            if not hit:
                return [], f"no playlist matching “{q}” (playlists: {', '.join(p.get('name', '') for p in lists[:15]) or 'none'})", False
            songs = (await self._api("getPlaylist", id=hit["id"])).get("playlist", {}).get("entry", []) or []
            return songs, f"the playlist “{hit.get('name')}”", False
        if kind == "genre":
            genre = await self._genre(q)
            if not genre:
                return [], f"no genre matching “{q}”", False
            songs = (await self._api("getSongsByGenre", genre=genre, count=min(MAX_SONGS, max(count, 50)))).get("songsByGenre", {}).get("song", []) or []
            return songs, f"{genre}", True
        if not q:
            return [], "nothing: say what to play (or use kind random/starred/latest)", False

        found = (await self._api("search3", query=q, artistCount=5, albumCount=5, songCount=25)).get("searchResult3", {}) or {}
        artists, albums, hits = found.get("artist", []) or [], found.get("album", []) or [], found.get("song", []) or []
        fq = _fold(q)
        if kind == "artist" or (kind == "auto" and artists and _fold(artists[0].get("name")) == fq):
            if not artists:
                return [], f"no artist matching “{q}”", False
            a = artists[0]
            return await self._artist_songs(a["id"]), f"everything by {a.get('name')}", True
        if kind == "album" or (kind == "auto" and albums and (_fold(albums[0].get("name")) == fq
                                                              or fq == _fold(f"{albums[0].get('name')} {albums[0].get('artist')}"))):
            if not albums:
                return [], f"no album matching “{q}”", False
            a = albums[0]
            return await self._album_songs(a["id"]), f"the album {a.get('name')} by {a.get('artist')}", False
        if kind == "song":
            return hits[:1], (_label(hits[:1]) if hits else f"no song matching “{q}”"), False
        if kind == "auto":
            if hits and (_fold(hits[0].get("title")) == fq or len(hits) == 1):
                return hits[:1], _label(hits[:1]), False
            if artists:
                a = artists[0]
                return await self._artist_songs(a["id"]), f"everything by {a.get('name')}", True
            if albums:
                a = albums[0]
                return await self._album_songs(a["id"]), f"the album {a.get('name')} by {a.get('artist')}", False
            if hits:
                return hits, f"{len(hits)} songs matching “{q}”", False
            genre = await self._genre(q)
            if genre:
                songs = (await self._api("getSongsByGenre", genre=genre, count=100)).get("songsByGenre", {}).get("song", []) or []
                return songs, genre, True
        return [], f"nothing in the library matches “{q}”", False

    @staticmethod
    def _command(op: str, songs: list[dict] | None = None) -> dict:
        cmd = {"op": op, "nonce": uuid.uuid4().hex}
        if songs is not None:
            cmd["songs"] = [_song(s) for s in songs]
        return cmd

    @staticmethod
    def _ok(text: dict, command: dict | None = None) -> dict:
        out = {"output": json.dumps(text, ensure_ascii=False), "exit_code": 0}
        if command:
            out.update(ui_event="music", music_command=command)
        return out

    async def execute(self, content: str, ctx: dict | None = None) -> dict:
        try:
            args = json.loads(content) if (content or "").strip().startswith("{") else {"action": "play", "query": content}
        except ValueError:
            args = {"action": "play", "query": content}
        if not isinstance(args, dict):
            return {"error": "control_music expects a JSON object with an action", "exit_code": 1}
        action = str(args.get("action") or "play").strip().lower().replace(" ", "_")
        action = ALIASES.get(action, action)
        kind = str(args.get("kind") or "auto").strip().lower()
        if kind not in KINDS:
            kind = "auto"
        query = str(args.get("query") or "").strip()[:200]
        try:
            count = max(1, min(int(args.get("count") or 50), MAX_SONGS))
        except (TypeError, ValueError):
            count = 50
        shuffle_arg = args.get("shuffle")

        if action not in PLAY_ACTIONS + CONTROL_ACTIONS + READ_ACTIONS:
            return {"error": f"Unknown action {action!r}. Use one of: {', '.join(PLAY_ACTIONS + CONTROL_ACTIONS + READ_ACTIONS)}.",
                    "exit_code": 1}

        if action in CONTROL_ACTIONS:
            done = {"pause": "paused", "resume": "resumed", "next": "skipped to the next song",
                    "previous": "went back a song"}[action]
            return self._ok({"status": f"{done} in his browser"}, self._command(action))

        if not self._cfg().configured:
            return {"error": "Music isn't connected to Navidrome yet (Music window → Connection).", "exit_code": 1}
        try:
            if action == "now_playing":
                entries = (await self._api("getNowPlaying")).get("nowPlaying", {}).get("entry", []) or []
                user = self._cfg().user
                mine = [e for e in entries if e.get("username") == user] or entries
                if not mine:
                    return self._ok({"now_playing": None, "note": "Nothing is playing right now (as far as Navidrome knows)."})
                e = mine[0]
                return self._ok({"now_playing": {"title": e.get("title"), "artist": e.get("artist"), "album": e.get("album"),
                                                 "year": e.get("year"), "minutes_ago": e.get("minutesAgo")}})
            if action == "genres":
                genres = (await self._api("getGenres")).get("genres", {}).get("genre", []) or []
                genres.sort(key=lambda g: -(g.get("songCount") or 0))
                return self._ok({"genres": [{"name": g.get("value"), "songs": g.get("songCount")} for g in genres[:40]]})
            if action == "search":
                if not query:
                    return {"error": "search needs a query", "exit_code": 1}
                r = (await self._api("search3", query=query, artistCount=8, albumCount=10, songCount=15)).get("searchResult3", {}) or {}
                return self._ok({
                    "artists": [a.get("name") for a in r.get("artist", []) or []],
                    "albums": [f"{a.get('name')} — {a.get('artist')} ({a.get('year') or '?'})" for a in r.get("album", []) or []],
                    "songs": [f"{s.get('title')} — {s.get('artist')}" for s in r.get("song", []) or []],
                })

            songs, described, shuffle_default = await self.resolve(kind, query, count)
        except nd.MusicError as exc:
            return {"error": str(exc), "exit_code": 1}
        if not songs:
            return {"error": f"Found {described}.", "exit_code": 1}
        shuffle = shuffle_default if shuffle_arg is None else bool(shuffle_arg)
        if shuffle:
            songs = list(songs)
            random.shuffle(songs)
        songs = songs[:MAX_SONGS]
        verb = {"play": "playing", "queue": "added to the queue", "play_next": "playing next"}[action]
        status = {
            "status": f"{verb}: {described}" + (" (shuffled)" if shuffle and len(songs) > 1 else ""),
            "songs": len(songs),
            # The actual songs, so she can talk about what's on without guessing.
            "tracks": [f"{x.get('title')} — {x.get('artist')}" for x in songs[:10]],
            "where": "in the Mabel tab he's chatting from",
        }
        if len(songs) > 10:
            status["more_after_these"] = len(songs) - 10
        return self._ok(status, self._command(action, songs))

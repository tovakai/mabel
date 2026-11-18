"""AUDIO: Navidrome (through Mabel's proxy, or directly on the deck), played locally by mpv.

Left: the library (newest albums first; / searches; enter opens an album or artist, or plays from
that song; a appends, n plays next; backspace goes back). Right: the deck (cover, now playing,
queue). space pause, [ ] previous/next, ← → seek, + - volume. O downloads for offline, o opens the
offline library (it opens by itself when Navidrome can't be reached).
"""
from __future__ import annotations

import re

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, ListItem, ListView, Label, OptionList, ProgressBar, Static
from textual.widgets.option_list import Option

from .config import CACHE
from .nav import mark
from .palette import P


def _time(seconds) -> str:
    seconds = int(float(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


class PickPlaylist(ModalScreen):
    """Which playlist? Returns its id, "+new", or None."""
    BINDINGS = [Binding("escape", "dismiss(None)", "cancel")]

    def __init__(self, playlists: list[dict]):
        super().__init__()
        self.playlists = playlists

    def compose(self) -> ComposeResult:
        options = [Option("＋ New playlist…", id="+new")] + \
                  [Option(f"♫ {p.get('name', '')}  ({p.get('songCount', 0)})", id=p["id"]) for p in self.playlists]
        box = OptionList(*options, id="pick-playlist", classes="panel")
        box.border_title = "ADD TO PLAYLIST"
        box.border_subtitle = "enter picks · esc cancels"
        yield box

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class Audio(Horizontal):
    BINDINGS = [
        Binding("slash", "search", "search"),
        Binding("space", "toggle", "pause"),
        Binding("right_square_bracket", "next", "next"),
        Binding("left_square_bracket", "previous", "prev"),
        Binding("right", "seek(10)", "+10s", show=False),
        Binding("left", "seek(-10)", "-10s", show=False),
        Binding("plus", "volume(5)", "vol+", show=False),
        Binding("minus", "volume(-5)", "vol-", show=False),
        Binding("a", "add", "append"),
        Binding("n", "add_next", "play next"),
        Binding("backspace", "back", "back"),
        Binding("p", "playlists", "playlists"),
        Binding("s", "to_playlist", "+ playlist"),
        Binding("S", "save_queue", "save queue", show=False),
        Binding("d", "remove_from_playlist", "remove", show=False),
        Binding("z", "shuffle", "shuffle"),
        Binding("R", "random_mix", "random mix", show=False),
        Binding("O", "download", "download"),
        Binding("o", "offline", "offline"),
    ]

    def __init__(self, **kw):
        super().__init__(id="audio-view", **kw)
        self.rows: list[tuple[str, dict]] = []      # (kind, item) per table row
        self.views: list[tuple[str, list]] = []     # back stack: (title, rows)
        self._cover_id = None
        self.playlist: dict | None = None            # the playlist on screen, for d (remove)
        self.context: tuple[str, dict] | None = None  # the album/playlist on screen, for O (download all)
        self._remove_armed: str | None = None
        self._dirty = True

    def compose(self) -> ComposeResult:
        with Vertical(id="browser", classes="panel") as browser:
            browser.border_title = "LIBRARY"
            yield mark(Input(placeholder="/ search artists, albums, songs", id="search"))
            yield mark(DataTable(id="results", cursor_type="row", zebra_stripes=False))
        with Vertical(id="deck", classes="panel section") as deck:
            deck.section_primary = "#queue"
            deck.border_title = "DECK"
            yield Container(id="cover-holder")
            yield Static(id="nowplaying")
            yield ProgressBar(total=1.0, show_eta=False, show_percentage=False, id="progress")
            yield ListView(id="queue")

    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        for label, width in ((" ", 2), ("TITLE", 32), ("ARTIST", 22), ("ALBUM · YEAR", 22), ("TIME", 6)):
            table.add_column(label, width=width)
        self.app.player.on_change = self._mark
        self.set_interval(0.5, self._refresh_deck)
        self.load_newest()

    @property
    def music(self):
        return self.app.music

    @property
    def offline(self):
        return self.app.offline

    @property
    def player(self):
        return self.app.player

    # ── the library ─────────────────────────────────────────────────────

    def _show(self, title: str, rows: list[tuple[str, dict]], remember: bool = True,
              playlist: dict | None = None, context: tuple[str, dict] | None = None) -> None:
        self.playlist = playlist
        if remember and self.rows:
            self.views.append((self.query_one("#browser").border_title, self.rows, self.context, self.playlist))
        self.context = context or (("playlist", playlist) if playlist else None)
        self.rows = rows
        self.query_one("#browser").border_title = title
        table = self.query_one("#results", DataTable)
        table.clear()
        for kind, item in rows:
            if kind == "song":
                table.add_row("↓" if self.offline.has(item["id"]) else "♪", item.get("title", ""),
                              item.get("artist", ""), item.get("album", ""), _time(item.get("duration")))
            elif kind == "offline":
                have, total = self.offline.complete(item["key"])
                table.add_row("⇩", item.get("name", ""), item.get("artist", ""),
                              f"{item.get('kind', '')} · {have}/{total}" if have < total else item.get("kind", ""),
                              f"{total} tr")
            elif kind == "album":
                table.add_row("◫", item.get("name", ""), item.get("artist", ""), str(item.get("year") or ""),
                              f"{item.get('songCount', '')} tr")
            elif kind == "playlist":
                table.add_row("♫", item.get("name", ""), item.get("owner", ""), f"{item.get('songCount', 0)} songs",
                              _time(item.get("duration")))
            else:
                table.add_row("◉", item.get("name", ""), "", f"{item.get('albumCount', '')} albums", "")
        if self.app.on_channel("audio"):
            table.focus()

    @work(exclusive=True, group="browse")
    async def load_newest(self) -> None:
        try:
            data = await self.music.music("getAlbumList2", type="newest", size=60)
        except Exception as exc:
            if self.offline.collections:
                self.app.notify("Navidrome isn't answering: here's what's downloaded", severity="warning")
                self.action_offline(remember=False)
            else:
                self.app.notify(f"Navidrome isn't answering: {exc}", severity="error")
            return
        albums = (data.get("albumList2") or {}).get("album") or []
        self._show("LIBRARY ░ NEWEST", [("album", a) for a in albums], remember=False)

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    @on(Input.Submitted, "#search")
    @work(exclusive=True, group="browse")
    async def search(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            return
        data = await self.music.music("search3", query=query, artistCount=8, albumCount=20, songCount=60)
        found = data.get("searchResult3") or {}
        rows = [("artist", a) for a in found.get("artist") or []] + \
               [("album", a) for a in found.get("album") or []] + [("song", s) for s in found.get("song") or []]
        self._show(f"LIBRARY ░ “{query.upper()[:30]}”", rows)

    @on(DataTable.RowSelected, "#results")
    @work(exclusive=True, group="browse")
    async def open_row(self, event: DataTable.RowSelected) -> None:
        if not 0 <= event.cursor_row < len(self.rows):
            return
        kind, item = self.rows[event.cursor_row]
        if kind == "song":
            songs = [i for k, i in self.rows if k == "song"]
            await self.player.play(songs, start=songs.index(item))
            self._report()
        elif kind == "album":
            data = await self.music.music("getAlbum", id=item["id"])
            songs = (data.get("album") or {}).get("song") or []
            self._show(f"◫ {item.get('name', '').upper()[:34]}", [("song", s) for s in songs], context=("album", item))
        elif kind == "playlist":
            await self._open_playlist(item)
        elif kind == "offline":
            songs = self.offline.collection_songs(item["key"])
            self._show(f"⇩ {item.get('name', '').upper()[:34]}", [("song", s) for s in songs])
        else:
            data = await self.music.music("getArtist", id=item["id"])
            albums = (data.get("artist") or {}).get("album") or []
            self._show(f"◉ {item.get('name', '').upper()[:34]}", [("album", a) for a in albums])

    async def _selected_songs(self) -> list[dict]:
        table = self.query_one("#results", DataTable)
        if not 0 <= table.cursor_row < len(self.rows):
            return []
        kind, item = self.rows[table.cursor_row]
        if kind == "song":
            return [item]
        if kind == "album":
            return (await self.music.music("getAlbum", id=item["id"])).get("album", {}).get("song") or []
        if kind == "playlist":
            return (await self.music.music("getPlaylist", id=item["id"])).get("playlist", {}).get("entry") or []
        if kind == "offline":
            return self.offline.collection_songs(item["key"])
        return []

    @work(group="queue")
    async def action_add(self) -> None:
        songs = await self._selected_songs()
        await self.player.enqueue(songs)
        self._report(f"+ {len(songs)} queued" if songs else "")

    @work(group="queue")
    async def action_add_next(self) -> None:
        songs = await self._selected_songs()
        await self.player.play_next(songs)
        self._report(f"+ {len(songs)} up next" if songs else "")

    # ── playlists ───────────────────────────────────────────────────────

    @work(exclusive=True, group="browse")
    async def action_playlists(self) -> None:
        data = await self.music.music("getPlaylists")
        lists = (data.get("playlists") or {}).get("playlist") or []
        self._show("LIBRARY ░ PLAYLISTS", [("playlist", p) for p in sorted(lists, key=lambda p: p.get("name", "").lower())])

    async def _open_playlist(self, playlist: dict, remember: bool = True) -> None:
        data = await self.music.music("getPlaylist", id=playlist["id"])
        entries = (data.get("playlist") or {}).get("entry") or []
        self._show(f"♫ {playlist.get('name', '').upper()[:34]}", [("song", e) for e in entries], remember=remember,
                   playlist=playlist)

    async def _playlists(self) -> list[dict]:
        data = await self.music.music("getPlaylists")
        return sorted((data.get("playlists") or {}).get("playlist") or [], key=lambda p: p.get("name", "").lower())

    def _pick_playlist(self, songs: list[dict], then) -> None:
        async def go():
            lists = await self._playlists()
            self.app.push_screen(PickPlaylist(lists), lambda choice: self.run_worker(then(choice, songs)))
        self.run_worker(go())

    @work(group="queue")
    async def action_to_playlist(self) -> None:
        songs = await self._selected_songs()
        if not songs:
            self.app.notify("Select a song, album or playlist first", severity="warning")
            return
        self._pick_playlist(songs, self._add_to)

    async def _add_to(self, choice, songs: list[dict]) -> None:
        if choice is None:
            return
        ids = [s["id"] for s in songs]
        if choice == "+new":
            def named(result):
                if result and result.get("name", "").strip():
                    self.run_worker(self._create(result["name"].strip(), ids))
            from .inventory import Ask
            self.app.push_screen(Ask("New playlist", [("name", "Name", "", "line")], ok="Create"), named)
            return
        await self.music.music_write("updatePlaylist", playlistId=choice, songIdToAdd=ids)
        self.app.notify(f"+ {len(ids)} song(s) added to the playlist", timeout=3)
        if self.playlist and self.playlist.get("id") == choice:
            await self._open_playlist(self.playlist, remember=False)

    async def _create(self, name: str, ids: list[str]) -> None:
        await self.music.music_write("createPlaylist", name=name, songId=ids)
        self.app.notify(f"♫ “{name}” saved with {len(ids)} song(s)", timeout=3)

    def action_save_queue(self) -> None:
        ids = [s["id"] for s in self.player.queue]
        if not ids:
            self.app.notify("The queue is empty", severity="warning")
            return

        def named(result):
            if result and result.get("name", "").strip():
                self.run_worker(self._create(result["name"].strip(), ids))
        from .inventory import Ask
        self.app.push_screen(Ask("Save the queue as a playlist", [("name", "Name", "", "line")], ok="Save"), named)

    @work(group="queue")
    async def action_remove_from_playlist(self) -> None:
        table = self.query_one("#results", DataTable)
        if 0 <= table.cursor_row < len(self.rows) and self.rows[table.cursor_row][0] == "offline":
            self._remove_download(self.rows[table.cursor_row][1])
            return
        if not self.playlist or not 0 <= table.cursor_row < len(self.rows):
            return
        kind, item = self.rows[table.cursor_row]
        if kind != "song":
            return
        await self.music.music_write("updatePlaylist", playlistId=self.playlist["id"], songIndexToRemove=table.cursor_row)
        self.app.notify(f"− {item.get('title', '')} removed from the playlist", timeout=3)
        await self._open_playlist(self.playlist, remember=False)
        table.move_cursor(row=min(table.cursor_row, max(0, len(self.rows) - 1)))

    def action_back(self) -> None:
        if self.views:
            title, rows, context, playlist = self.views.pop()
            self._show(title, rows, remember=False, playlist=playlist, context=context)

    # ── offline ─────────────────────────────────────────────────────────

    def action_offline(self, remember: bool = True) -> None:
        cols = sorted(self.offline.collections.items(), key=lambda kv: (kv[1].get("kind") != "album", kv[1].get("name", "").lower()))
        self._show(f"OFFLINE ░ {len(self.offline.songs)} SONGS · {self.offline.size_mb():.0f} MB",
                   [("offline", {**c, "key": k}) for k, c in cols], remember=remember)
        if not cols:
            self.app.notify("Nothing downloaded yet: O on an album, playlist or song", timeout=4)

    @work(group="download")
    async def action_download(self) -> None:
        table = self.query_one("#results", DataTable)
        row = self.rows[table.cursor_row] if 0 <= table.cursor_row < len(self.rows) else None
        if self.context:                                       # inside an album/playlist: all of it
            kind, item = self.context
        elif row and row[0] in ("song", "album", "playlist", "offline"):
            kind, item = row
        else:
            self.app.notify("Pick a song, album or playlist to download", severity="warning")
            return
        if kind == "offline":                                  # finish a half-done download
            key = item["key"]
            missing = [i for i in (self.offline.collections[key].get("song_ids") or []) if not self.offline.has(i)]
            if missing:
                self.app.notify("Finishing a download needs Navidrome: open the album or playlist and press O",
                                severity="warning")
                return
            self.app.notify(f"“{item.get('name', '')}” is all here", timeout=3)
            return
        try:
            if kind == "song":
                songs, name, artist, cover = [item], item.get("title", ""), item.get("artist", ""), item.get("coverArt")
            elif kind == "album":
                songs = (await self.music.music("getAlbum", id=item["id"])).get("album", {}).get("song") or []
                name, artist, cover = item.get("name", ""), item.get("artist", ""), item.get("coverArt")
            else:
                songs = (await self.music.music("getPlaylist", id=item["id"])).get("playlist", {}).get("entry") or []
                name, artist, cover = item.get("name", ""), item.get("owner", ""), item.get("coverArt")
        except Exception as exc:
            self.app.notify(f"Navidrome isn't answering: {exc}", severity="error")
            return
        browser = self.query_one("#browser")

        def progress(n, total, song):
            browser.border_subtitle = f"⇩ {n}/{total} · {song.get('title', '')[:28]}"
        self.app.notify(f"⇩ downloading “{name}” ({len(songs)} songs)", timeout=3)
        try:
            got, had = await self.offline.download(self.music, songs, key=f"{kind}:{item['id']}", kind=kind,
                                                   name=name, artist=artist, cover_id=cover,
                                                   max_bitrate=self.app.cfg.offline_bitrate, progress=progress)
        except Exception as exc:
            self.app.notify(f"Download stopped: {exc}. O again picks it up.", severity="error")
            return
        finally:
            browser.border_subtitle = ""
            self._redraw_marks()
        self.app.notify(f"⇩ “{name}” is offline: {got} new" + (f", {had} already here" if had else ""), timeout=4)

    def _redraw_marks(self) -> None:
        table = self.query_one("#results", DataTable)
        for i, (kind, item) in enumerate(self.rows):
            if kind == "song" and i < table.row_count:
                table.update_cell_at((i, 0), "↓" if self.offline.has(item["id"]) else "♪")

    def _remove_download(self, item: dict) -> None:
        key = item["key"]
        if self._remove_armed != key:                          # d twice: a download is too slow to lose by accident
            self._remove_armed = key
            self.set_timer(3, lambda: setattr(self, "_remove_armed", None))
            self.app.notify(f"d again to delete “{item.get('name', '')}” from this device", severity="warning", timeout=3)
            return
        self._remove_armed = None
        removed = self.offline.remove(key)
        self.app.notify(f"− “{item.get('name', '')}” deleted ({removed} files)", timeout=3)
        self.action_offline(remember=False)

    # ── playback ────────────────────────────────────────────────────────

    def _report(self, text: str = "") -> None:
        if self.player.error:
            self.app.notify(self.player.error, severity="error")
            self.player.error = ""
        elif text:
            self.app.notify(text, timeout=2)

    async def action_shuffle(self) -> None:
        await self.player.set_shuffle(not self.player.shuffle)
        self.app.notify("⤨ shuffle on: the rest of the queue is shuffled" if self.player.shuffle else "shuffle off",
                        timeout=2)

    @work(group="queue")
    async def action_random_mix(self) -> None:
        data = await self.music.music("getRandomSongs", size=50)
        songs = (data.get("randomSongs") or {}).get("song") or []
        if songs:
            await self.player.play(songs)
            self._report(f"⤨ a random mix of {len(songs)} songs")

    async def action_toggle(self) -> None:
        await self.player.toggle()

    async def action_next(self) -> None:
        await self.player.next()

    async def action_previous(self) -> None:
        await self.player.previous()

    async def action_seek(self, seconds: int) -> None:
        await self.player.seek(seconds)

    async def action_volume(self, delta: int) -> None:
        await self.player.change_volume(delta)

    @on(ListView.Selected, "#queue")
    async def jump(self, event: ListView.Selected) -> None:
        row = self.query_one("#queue", ListView).index or 0
        await self.player.jump(getattr(self, "_queue_offset", 0) + row)

    # ── the deck ────────────────────────────────────────────────────────

    def _mark(self) -> None:
        self._dirty = True
        self.app.refresh_statusline()
        self.app.mpris.changed()

    def _refresh_deck(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        player, song = self.player, self.player.current
        text = Text()
        if song:
            text.append(("❚❚ " if player.paused else "▶ ") + f"{song.get('title', '')}\n", style=f"bold {P.amber_hi}")
            text.append(f"{song.get('artist', '')}\n", style=P.amber)
            text.append(f"{song.get('album', '')}\n", style=P.amber_dim)
            length = player.duration or float(song.get("duration") or 0)
            text.append(f"{_time(player.position)} / {_time(length)}   vol {player.volume}"
                        + ("   ⤨ shuffle" if player.shuffle else ""), style=P.teal)
            self.query_one("#progress", ProgressBar).update(progress=(player.position / length) if length else 0)
        else:
            text.append("NO TAPE IN THE DECK\n", style=f"bold {P.amber_dim}")
            text.append("pick something on the left", style=P.amber_faint)
        self.query_one("#nowplaying", Static).update(text)
        self._sync_queue()
        cover = (song or {}).get("coverArt")
        if cover and cover != self._cover_id:
            self._cover_id = cover
            self.show_cover(cover)

    QUEUE_BEFORE, QUEUE_AFTER = 2, 40           # a window around the current song, not all 800 of them

    def _sync_queue(self) -> None:
        player = self.player
        start = max(0, player.index - self.QUEUE_BEFORE)
        window = list(enumerate(player.queue))[start: max(player.index, 0) + self.QUEUE_AFTER + 1]
        wanted = [f"{'▶' if i == player.index else ' '} {s.get('title', '')[:32]}  ·  {s.get('artist', '')[:18]}"
                  for i, s in window]
        rest = len(player.queue) - (window[-1][0] + 1 if window else 0)
        if rest > 0:
            wanted.append(f"  … {rest} more")
        # Compare with what we drew last time, not the widgets: freshly added rows may not be built yet.
        if wanted == getattr(self, "_queue_drawn", None):
            return
        self._queue_drawn, self._queue_offset = wanted, start
        view = self.query_one("#queue", ListView)
        view.clear()
        view.extend(ListItem(Label(w)) for w in wanted)

    @work(exclusive=True, group="cover")
    async def show_cover(self, cover_id: str) -> None:
        path = self.offline.cover_file(cover_id)
        if not path:
            try:
                picture = await self.music.cover(cover_id, 400)
            except Exception:
                picture = None
            if not picture:
                return
            CACHE.mkdir(parents=True, exist_ok=True)
            path = CACHE / f"cover-{re.sub(r'[^A-Za-z0-9_-]', '', cover_id)[:60]}.jpg"
            path.write_bytes(picture)
        from textual_image.widget import HalfcellImage as Image   # pixel art on purpose: 1×2 px per cell
        holder = self.query_one("#cover-holder", Container)
        await holder.remove_children()
        await holder.mount(Image(str(path), id="cover"))
        self.app.mpris.changed()                     # now the desktop can show the cover too

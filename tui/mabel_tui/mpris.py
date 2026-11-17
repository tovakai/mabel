"""MPRIS on the session bus, so the desktop (Quickshell, playerctl) sees and controls the AUDIO deck.

Publishes org.mpris.MediaPlayer2.mabel_os with title, artist, album, length and the cover (the
file the deck already caches), and maps play/pause/next/previous/seek back to the mpv player.
Without a session bus (e.g. over SSH) it quietly does nothing.
"""
from __future__ import annotations

import logging
import re

from dbus_fast import PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method

from .config import CACHE

log = logging.getLogger(__name__)
BUS_NAME = "org.mpris.MediaPlayer2.mabel_os"
PATH = "/org/mpris/MediaPlayer2"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"


def cover_file(cover_id: str | None):
    if not cover_id:
        return None
    path = CACHE / f"cover-{re.sub(r'[^A-Za-z0-9_-]', '', cover_id)[:60]}.jpg"
    return path if path.exists() else None


class Root(ServiceInterface):
    def __init__(self, app):
        super().__init__("org.mpris.MediaPlayer2")
        self.app = app

    @method()
    def Raise(self):
        pass

    @method()
    def Quit(self):
        self.app.exit()

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":
        return "MABEL-OS"

    @dbus_property(access=PropertyAccess.READ)
    def DesktopEntry(self) -> "s":
        return "mabel-os"

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":
        return []


class PlayerIface(ServiceInterface):
    def __init__(self, player):
        super().__init__("org.mpris.MediaPlayer2.Player")
        self.player = player

    # ── state ───────────────────────────────────────────────────────────

    def status(self) -> str:
        if not self.player.current:
            return "Stopped"
        return "Paused" if self.player.paused else "Playing"

    def metadata(self) -> dict:
        song = self.player.current
        if not song:
            return {"mpris:trackid": Variant("o", NO_TRACK)}
        meta = {
            "mpris:trackid": Variant("o", f"/org/mabel_os/track/{re.sub(r'[^A-Za-z0-9_]', '_', song['id'])}"),
            "xesam:title": Variant("s", song.get("title") or ""),
            "xesam:artist": Variant("as", [song.get("artist") or ""]),
            "xesam:album": Variant("s", song.get("album") or ""),
        }
        length = self.player.duration or float(song.get("duration") or 0)
        if length:
            meta["mpris:length"] = Variant("x", int(length * 1_000_000))
        art = cover_file(song.get("coverArt"))
        if art:
            meta["mpris:artUrl"] = Variant("s", art.as_uri())
        return meta

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":
        return self.status()

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self.metadata()

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":
        return int(self.player.position * 1_000_000)

    @dbus_property()
    def Volume(self) -> "d":
        return self.player.volume / 100

    @Volume.setter
    def Volume(self, value: "d"):
        import asyncio
        asyncio.create_task(self.player.change_volume(round(value * 100) - self.player.volume))

    @dbus_property(access=PropertyAccess.READ)
    def Rate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MinimumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MaximumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return self.player.index + 1 < len(self.player.queue)

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return bool(self.player.current)

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return bool(self.player.queue)

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return bool(self.player.current)

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":
        return bool(self.player.current)

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return True

    # ── controls ────────────────────────────────────────────────────────

    @method()
    async def Play(self):
        await self.player.set_paused(False)

    @method()
    async def Pause(self):
        await self.player.set_paused(True)

    @method()
    async def PlayPause(self):
        await self.player.toggle()

    @method()
    async def Stop(self):
        await self.player.set_paused(True)

    @method()
    async def Next(self):
        await self.player.next()

    @method()
    async def Previous(self):
        await self.player.previous()

    @method()
    async def Seek(self, offset: "x"):
        await self.player.seek(offset / 1_000_000)

    @method()
    async def SetPosition(self, track_id: "o", position: "x"):
        await self.player.seek(position / 1_000_000 - self.player.position)

    @method()
    def OpenUri(self, uri: "s"):
        pass


class Mpris:
    def __init__(self, app, player):
        self.app, self.player = app, player
        self.bus: MessageBus | None = None
        self.iface = PlayerIface(player)
        self._last: tuple | None = None

    async def start(self) -> bool:
        try:
            self.bus = await MessageBus().connect()
            self.bus.export(PATH, Root(self.app))
            self.bus.export(PATH, self.iface)
            await self.bus.request_name(BUS_NAME)
            return True
        except Exception as exc:                       # no session bus (SSH), or no dbus at all
            log.debug("MPRIS unavailable: %s", exc)
            self.bus = None
            return False

    def changed(self):
        """Tell listeners what changed (cheap to call often: sends only on a real change)."""
        if not self.bus:
            return
        song = self.player.current or {}
        state = (song.get("id"), self.iface.status(), bool(cover_file(song.get("coverArt"))),
                 int(self.player.duration), self.player.volume, len(self.player.queue), self.player.index)
        if state == self._last:
            return
        self._last = state
        self.iface.emit_properties_changed({
            "PlaybackStatus": self.iface.status(), "Metadata": self.iface.metadata(),
            "CanGoNext": self.iface.CanGoNext, "CanGoPrevious": self.iface.CanGoPrevious,
            "CanPlay": self.iface.CanPlay, "CanPause": self.iface.CanPause, "CanSeek": self.iface.CanSeek,
            "Volume": self.player.volume / 100})

    async def stop(self):
        if self.bus:
            self.bus.disconnect()

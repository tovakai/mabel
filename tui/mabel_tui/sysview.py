"""CH-08 SYS: the machine MABEL-OS runs on (backend: sysinfo.py).

On the deck (config `deck = true`) it's a suite: STATUS · NETWORK · BLUETOOTH · AUDIO · TIME.
Elsewhere just STATUS: a fastfetch-style card for the faux OS, plus restart / shut down MABEL-OS.

Every page is a Menu: j/k move · enter does the line · h/l (or -/+) adjust a value · r refresh ·
page keys are shown at the bottom. Slow things (Wi-Fi connect, pairing) run in the background.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from rich.style import Style
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Input

from . import sysinfo as si
from .palette import P
from .suite import Suite


@dataclass
class Item:
    label: str
    value: str = ""
    act: Callable[[], None] | None = None           # enter
    adjust: Callable[[int], None] | None = None     # h/l, -/+
    keys: dict[str, Callable[[], None]] = field(default_factory=dict)   # extra keys on this line
    style: str = ""
    header: bool = False


class Menu(Widget, can_focus=True):
    """A page: optional top lines, then items with a cursor; the page fills it in load()."""

    def __init__(self, page: "Page", **kw):
        super().__init__(**kw)
        self.page = page
        self.index = 0
        self.top = 0

    def render(self) -> Text:
        return self.page.draw(self.has_focus, self.size.height, self.size.width)

    async def _on_key(self, event: events.Key) -> None:
        if self.page.key(event):
            event.stop()
            event.prevent_default()

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()


class Page(Vertical):
    """One SYS app. Subclasses: build(), returning items (and top lines); actions call self.do(...)."""
    TITLE = ""
    HINT = ""
    REFRESH_S = 0.0                                  # reload this often while on screen (0 = only on demand)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.items: list[Item] = []
        self.top_lines: list[Text] = []
        self.message = ""
        self.busy = ""
        self.loaded = False

    def compose(self) -> ComposeResult:
        menu = Menu(self, classes="panel section sys-menu")
        menu.border_title = self.TITLE
        yield menu
        yield Input(classes="sys-input")

    def on_mount(self) -> None:
        self.query_one(".sys-input").display = False
        if self.REFRESH_S:
            self.set_interval(self.REFRESH_S, self._auto)

    @property
    def menu(self) -> Menu:
        return self.query_one(Menu)

    def _auto(self) -> None:
        if self.display and self.screen.is_current and not self.busy:
            self.reload()

    @work(thread=True, exclusive=True, group="sys-load")
    def reload(self) -> None:
        try:
            items, top = self.build()
        except Exception as exc:
            items, top = [Item(f"✖ {exc}", style=P.red)], []
        self.app.call_from_thread(self._loaded, items, top)

    def _loaded(self, items: list[Item], top: list[Text]) -> None:
        self.items, self.top_lines, self.loaded = items, top, True
        self._clamp()
        self.menu.refresh()

    def build(self) -> tuple[list[Item], list[Text]]:
        raise NotImplementedError

    def do(self, label: str, fn: Callable[[], tuple[bool, str]], then_reload: bool = True) -> None:
        """Run a (slow) action in the background, show what it said, then reload the page."""
        if self.busy:
            return
        self.busy, self.message = label, ""
        self.menu.refresh()

        def job() -> None:
            try:
                ok, msg = fn()
            except Exception as exc:
                ok, msg = False, str(exc)
            self.app.call_from_thread(self._done, ok, msg, then_reload)
        self.run_worker(job, thread=True, group="sys-do")

    def _done(self, ok: bool, msg: str, then_reload: bool) -> None:
        self.busy = ""
        self.message = ("" if ok else "✖ ") + (msg or ("done" if ok else "failed"))
        if then_reload:
            self.reload()
        self.menu.refresh()

    def ask(self, label: str, then: Callable[[str], None], password: bool = False, value: str = "") -> None:
        box = self.query_one(".sys-input", Input)
        box.placeholder, box.password, box.value, box.display = label, password, value, True
        self._then = then
        box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        event.input.display = False
        self.menu.focus()
        then, self._then = getattr(self, "_then", None), None
        if then:
            then(event.value)

    def on_key(self, event: events.Key) -> None:
        box = self.query_one(".sys-input", Input)
        if event.key == "escape" and box.has_focus:
            event.stop()
            box.display = False
            self.menu.focus()

    # cursor
    def _selectable(self) -> list[int]:
        return [i for i, it in enumerate(self.items) if not it.header and (it.act or it.adjust or it.keys)]

    def _clamp(self) -> None:
        sel = self._selectable()
        if sel and self.menu.index not in sel:
            self.menu.index = min(sel, key=lambda i: abs(i - self.menu.index))

    def key(self, event: events.Key) -> bool:
        k = event.key
        sel = self._selectable()
        cur = self.items[self.menu.index] if sel and self.menu.index < len(self.items) else None
        if k in ("down", "j", "up", "k") and sel:
            pos = sel.index(self.menu.index) if self.menu.index in sel else 0
            self.menu.index = sel[max(0, min(len(sel) - 1, pos + (1 if k in ("down", "j") else -1)))]
        elif k in ("enter", "space") and cur and cur.act:
            cur.act()
        elif k in ("left", "h", "minus", "right", "l", "plus") and cur and cur.adjust:
            cur.adjust(-1 if k in ("left", "h", "minus") else 1)
        elif cur and k in cur.keys:
            cur.keys[k]()
        elif k == "r":
            self.message = ""
            self.reload()
        else:
            return False
        self.menu.refresh()
        return True

    def draw(self, focused: bool, height: int, width: int) -> Text:
        t = Text()
        for line in self.top_lines:
            t.append_text(line)
            t.append("\n")
        if not self.loaded:
            t.append(" looking…\n", style=P.amber_dim)
        used = len(self.top_lines) + 3
        rows = max(1, height - used)
        idx = self.menu.index
        if idx < self.menu.top:
            self.menu.top = idx
        elif idx >= self.menu.top + rows:
            self.menu.top = idx - rows + 1
        for i in range(self.menu.top, min(len(self.items), self.menu.top + rows)):
            it = self.items[i]
            if it.header:
                t.append(f"\n{it.label}\n", style=f"bold {P.beige}")
                continue
            on = i == idx and focused and (it.act or it.adjust or it.keys)
            label_style = f"bold {P.bg} on {P.amber}" if on else (it.style or P.amber)
            t.append(f" {it.label:<26}", style=label_style)
            t.append(f" {it.value}\n", style=P.amber_hi if on else P.amber_dim)
        t.append("\n")
        if self.busy:
            t.append(f" ◌ {self.busy}…", style=f"bold {P.teal}")
        elif self.message:
            t.append(f" {self.message}", style=f"bold {P.red if self.message.startswith('✖') else P.sand}")
        else:
            cur = self.items[idx] if self.items and idx < len(self.items) else None
            extra = "  ".join(cur.keys) if cur and cur.keys else ""
            t.append(f" {self.HINT}" + (f" · {extra}" if extra else ""), style=P.amber_dim)
        return t


# --- STATUS ------------------------------------------------------------------------------------

def _logo(height: int) -> list[Text]:
    """Mabel's avatar, still frame, in half-blocks: the faux OS's logo."""
    try:
        from .avatar import DEFAULT, load_frames
        frame = load_frames(DEFAULT / "still.png")[0]
    except Exception:
        return []
    lines = []
    for y in range(0, len(frame), 2):
        t = Text()
        for x in range(len(frame[y])):
            top = frame[y][x]
            bottom = frame[y + 1][x] if y + 1 < len(frame) else None
            hx = lambda c: None if c is None else "#%02x%02x%02x" % c
            if top is None and bottom is None:
                t.append(" ")
            elif top is None:
                t.append("▄", style=Style(color=hx(bottom)))
            else:
                t.append("▀", style=Style(color=hx(top), bgcolor=hx(bottom)))
        lines.append(t)
    return lines


class StatusPage(Page):
    TITLE = "STATUS"
    HINT = "enter does it · r refresh"
    REFRESH_S = 5.0

    def __init__(self, deck: bool, **kw):
        super().__init__(**kw)
        self.deck = deck
        self._armed = ""                             # a machine power action waiting for its second enter

    def build(self):
        from . import update
        f = si.facts()
        app = self.app
        rows = [("mabel-os", f"2.6 · build {update.installed_build() or 'source'}"),
                ("link", app.cfg.url.split("//", 1)[-1])] + f.rows
        w = max(len(k) for k, _ in rows)
        info = []
        for i, (k, v) in enumerate(rows):
            line = Text()
            line.append(f"{k:<{w}}  ", style=f"bold {P.amber_hi}")        # fastfetch: labels left, a gap
            line.append(v, style=P.amber)
            info.append(line)
        for warn in f.warnings:
            info.append(Text(f"⚠ {warn}", style=f"bold {P.red}"))
        palette = Text()
        for c in (P.bg, P.rust, P.amber_dim, P.amber, P.amber_hi, P.beige, P.teal, P.red):
            palette.append("███", style=c)
        info += [Text(""), palette]
        logo = _logo(28)
        top = []
        for i in range(max(len(logo), len(info))):
            line = Text(" ")
            line.append_text(logo[i] if i < len(logo) else Text(" " * 28))
            line.append("   ")
            if i < len(info):
                line.append_text(info[i])
            top.append(line)
        items = [Item("MABEL-OS", header=True),
                 Item("restart MABEL-OS", "start it again (picks up changes)", act=self._restart),
                 Item("shut down MABEL-OS", "quit to the shell", act=lambda: self.app.exit())]
        if self.deck:
            items.append(Item("THE MACHINE", header=True))
            for action, label in (("suspend", "suspend"), ("reboot", "reboot"), ("poweroff", "power off")):
                armed = self._armed == action
                items.append(Item(label, "enter again to confirm" if armed else "",
                                  act=lambda a=action: self._power(a), style=f"bold {P.red}" if armed else ""))
        return items, top

    def _restart(self) -> None:
        self.app.exit(result="restart")

    def _power(self, action: str) -> None:
        if self._armed != action:
            self._armed = action
            self.reload()
            return
        self._armed = ""
        self.do(action, lambda: si.power(action))


# --- NETWORK ---------------------------------------------------------------------------------------

def _bars(signal: int) -> str:
    return "▂▄▆█"[: max(1, min(4, signal // 25 + 1))].ljust(4)


class NetworkPage(Page):
    TITLE = "NETWORK"
    HINT = "enter connect · r rescan"

    def build(self):
        hw, sw = si.wifi_radio()
        items = [Item("WIRED / TAILNET", header=True)]
        for dev, kind, state in si.wired():
            if kind != "wifi":
                items.append(Item(f"{dev} ({kind})", state))
        items.append(Item("WI-FI", header=True))
        if not hw:
            items.append(Item("Wi-Fi hardware", "off (a hardware switch or no Wi-Fi chip)"))
            return items, []
        items.append(Item("Wi-Fi", "on" if sw else "off: enter turns it on",
                          act=lambda: self.do("switching Wi-Fi", lambda: si.set_wifi_radio(not sw))))
        if sw:
            for net in si.wifi_networks(rescan=True):
                lock = "▣" if net.security not in ("open", "--", "") else "□"
                state = "connected" if net.active else ("known" if net.known else "")
                keys = {"x": (lambda n=net: self.do(f"forgetting {n.ssid}", lambda: si.wifi_forget(n.ssid)))} \
                    if net.known else {}
                items.append(Item(f"{'▸ ' if net.active else '  '}{net.ssid[:22]}",
                                  f"{_bars(net.signal)} {net.signal:>3}% {lock} {state}",
                                  act=lambda n=net: self._connect(n), keys=keys,
                                  style=f"bold {P.teal}" if net.active else ""))
        return items, []

    def _connect(self, net: si.Network) -> None:
        if net.active:
            self.message = f"already on {net.ssid}"
            return
        if net.known or net.security in ("open", "--", ""):
            self.do(f"connecting to {net.ssid}", lambda: si.wifi_connect(net.ssid))
        else:
            self.ask(f"password for {net.ssid}",
                     lambda pw: self.do(f"connecting to {net.ssid}", lambda: si.wifi_connect(net.ssid, pw)),
                     password=True)


# --- BLUETOOTH ---------------------------------------------------------------------------------------

class BluetoothPage(Page):
    TITLE = "BLUETOOTH"
    HINT = "enter connect / disconnect · r refresh"

    def __init__(self, **kw):
        super().__init__(**kw)
        self._armed = ""                             # an input device waiting for its second enter

    def _careful(self, d, action: str, fn) -> Callable[[], None]:
        """Disconnecting or removing the keyboard you type with leaves the deck without input: ask twice."""
        def go() -> None:
            if d.icon.startswith("input") and self._armed != f"{action}:{d.mac}":
                self._armed = f"{action}:{d.mac}"
                self.message = f"{d.name} is an input device: {action} it? enter again"
                self.menu.refresh()
                return
            self._armed = ""
            fn()
        return go

    def build(self):
        st = si.bt_state()
        if not st.get("present"):
            return [Item("no Bluetooth adapter found")], []
        state = "on" if st["powered"] else ("off (blocked: enter unblocks and turns it on)" if st["blocked"] else "off")
        items = [Item("ADAPTER", header=True),
                 Item(f"Bluetooth · {st['name']}", state,
                      act=lambda: self.do("switching Bluetooth", lambda: si.bt_power(not st["powered"])))]
        if st["powered"]:
            items.append(Item("look for devices", "8 s scan",
                              act=lambda: self.do("scanning", lambda: (si.bt_scan(8), (True, "scan done"))[1])))
            items.append(Item("DEVICES", header=True))
            for d in si.bt_devices():
                state = "connected" if d.connected else ("paired" if d.paired else "new: enter pairs")
                act = self._careful(d, "disconnect", lambda d=d: self.do(f"disconnecting {d.name}",
                                                                         lambda: si.bt_do("disconnect", d.mac))) \
                    if d.connected else \
                    (lambda d=d: self.do(f"{'connecting' if d.paired else 'pairing'} {d.name}",
                                         lambda: si.bt_do("connect" if d.paired else "pair", d.mac)))
                items.append(Item(f"{'▸ ' if d.connected else '  '}{d.name[:22]}", f"{state} {d.icon}", act=act,
                                  keys={"x": self._careful(d, "remove", lambda d=d: self.do(f"removing {d.name}",
                                                                                      lambda: si.bt_do("remove", d.mac)))},
                                  style=f"bold {P.teal}" if d.connected else ""))
        return items, []


# --- AUDIO + DISPLAY -----------------------------------------------------------------------------------

class AudioPage(Page):
    TITLE = "AUDIO + DISPLAY"
    HINT = "h/l volume · enter choose"

    def build(self):
        audio = si.Audio()
        vol, muted = audio.volume()
        bar = "" if vol is None else "█" * (vol // 10) + "░" * (10 - vol // 10)
        items = [Item(f"SOUND · {audio.backend}", header=True),
                 Item("volume", f"{bar} {vol if vol is not None else '?'}%{'  MUTED' if muted else ''}",
                      act=lambda: self.do("muting", audio.toggle_mute),
                      adjust=lambda s: self.do("volume", lambda: audio.change(5 * s)))]
        for out in audio.outputs():
            items.append(Item(f"{'▸ ' if out.default else '  '}{out.name[:24]}", "in use" if out.default else "",
                              act=lambda o=out: self.do(f"switching to {o.name}", lambda: audio.set_default(o)),
                              style=f"bold {P.teal}" if out.default else ""))
        items.append(Item("DISPLAY", header=True))
        level = si.brightness()
        if level is None:
            items.append(Item("brightness", "this screen has no brightness control"))
        else:
            items.append(Item("brightness", f"{level}%", adjust=lambda s: self.do("brightness", lambda: si.set_brightness(10 * s))))
        items.append(Item("colour tuner", "for screens that show colours their own way", act=self.app.action_tune))
        return items, []


# --- TIME ---------------------------------------------------------------------------------------------

class TimePage(Page):
    TITLE = "DATE, TIME, LOCALE"
    HINT = "enter change"
    REFRESH_S = 1.0

    def build(self):
        tm, loc = si.time_state(), si.locale_state()
        ntp = tm.get("NTP") == "yes"
        items = [Item("TIME", header=True),
                 Item("now", time.strftime("%a %d %b %Y  %H:%M:%S")),
                 Item("time zone", tm.get("Timezone", "?"), act=self._zone),
                 Item("network time", ("on" if ntp else "off") + (" · synchronised" if tm.get("NTPSynchronized") == "yes" else ""),
                      act=lambda: self.do("network time", lambda: si.set_ntp(not ntp))),
                 Item("LOCALE", header=True),
                 Item("language", loc["locale"].replace("LANG=", "")),
                 Item("keyboard layout", loc["layout"] or "?", act=self._layout)]
        return items, []

    def _zone(self) -> None:
        def chosen(text: str) -> None:
            text = text.strip()
            if not text:
                return
            zones = si.timezones()
            match = next((z for z in zones if z.lower() == text.lower()), None) or \
                next((z for z in zones if text.lower() in z.lower()), None)
            if match:
                self.do(f"time zone {match}", lambda: si.set_timezone(match))
            else:
                self.message = f"✖ no time zone like {text!r}"
                self.menu.refresh()
        self.ask("time zone (e.g. Stockholm, Tokyo, Europe/Berlin)", chosen)

    def _layout(self) -> None:
        self.ask("keyboard layout (se, us, de…)",
                 lambda text: text.strip() and self.do("keyboard", lambda: si.set_keyboard(text.strip())))


# --- the channel ----------------------------------------------------------------------------------------

class Sys(Suite):
    FKEY = "F8"
    DECK_APPS = [("status", "STATUS", "the machine and MABEL-OS"), ("network", "NETWORK", "Wi-Fi, wired, tailnet"),
                 ("bluetooth", "BLUETOOTH", "pair and connect"), ("audio", "AUDIO", "sound out, volume, screen"),
                 ("time", "TIME", "date, time zone, keyboard")]

    def __init__(self, deck: bool, **kw):
        self.deck = deck
        super().__init__(**kw)

    def apps(self):
        return self.DECK_APPS if self.deck else self.DECK_APPS[:1]

    def make(self):
        pages = {"status": StatusPage(self.deck, id="status"), "network": NetworkPage(id="network"),
                 "bluetooth": BluetoothPage(id="bluetooth"), "audio": AudioPage(id="audio"), "time": TimePage(id="time")}
        return [pages[k] for k, _, _ in self.apps()]

    def shown(self, key: str) -> None:
        self.query_one(f"#{key}", Page).reload()

    def focus_app(self, key: str) -> None:
        page = self.query_one(f"#{key}", Page)
        if not page.loaded:
            page.reload()
        page.menu.focus()

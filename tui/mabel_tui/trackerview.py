"""TRACKER (CREATIVE's second app): LSDJ's song → chain → phrase, all on one screen.

SONG (rows × tracks of chains) · CHAIN (16 phrases + transpose) · PHRASE (16 steps: note,
instrument, command) · INSTRUMENT · TABLE · WAVE. Each panel follows the cursor of the one
before it, so there is no menu to dive through: move in SONG and you see that chain, its
phrase, the instrument under the cursor and its table, all at once.

Keys (in any panel): hjkl move · H/L (shift+←/→) next panel · enter dive in · 0-9 a-f type hex ·
+/- nudge · </> ±16 (an octave on notes) · n new chain/phrase · N clone · x clear · y/p copy/paste
a cell · Y/P a whole chain/phrase/table · space play/stop what you're looking at · m mute · s solo ·
u undo · ctrl+r redo · ctrl+s save · : command. In PHRASE, i = piano mode (z s x d c… / q 2 w 3 e…,
+/- octave, = note off, esc back). In CMD columns type the letter (C D E H K L O P R T V W A).
"""
from __future__ import annotations

import copy
import math
import threading
import time
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Input

from . import tracker as tk
from .config import CACHE
from .palette import P

LAST_FILE = CACHE / "tracker-last"
PANELS = ["song", "chain", "phrase", "inst", "table", "wave"]
HEX = "0123456789abcdef"
LOWER = "zsxdcvgbhnjm,l.;/"                 # piano: C up to E of the next octave
UPPER = "q2w3er5t6y7ui9o0p"                 # one octave higher
IDLE_RELEASE_S = 30                         # close audio + MIDI after this long without sound
INST_FIELDS = [("kind", "kind"), ("volume", "volume"), ("attack", "attack"), ("decay", "decay"),
               ("sustain", "sustain"), ("release", "release"), ("width", "width/wave"),
               ("transpose", "transpose"), ("table", "table"), ("pan", "pan"), ("out", "out"),
               ("midi_channel", "midi ch"), ("midi_program", "program")]


def _cell(text: str, *, cursor: bool, focused: bool, play: bool = False, dim: bool = False) -> tuple[str, str]:
    if cursor:
        return text, (f"bold {P.bg} on {P.amber_hi}" if focused else f"bold underline {P.amber_hi}")
    if play:
        return text, f"bold {P.teal}"
    return text, (P.amber_dim if dim else P.amber)


def song_card(path: Path, width: int, height: int) -> Text:
    """The browser's preview of a song: its facts and the top of the song grid."""
    song = tk.load(path)
    last = song.last_row()
    steps = 0
    for t in range(song.tracks):                       # once through, on the longest track
        n, r = 0, 0
        while r <= last and song.rows[r][t] is not None:
            chain = song.chains.get(song.rows[r][t]) or []
            n += sum(1 for s in chain if s.phrase is not None) * tk.STEPS
            r += 1
        steps = max(steps, n)
    groove = song.groove or [6]
    seconds = steps * (sum(groove) / len(groove)) * 60 / (song.bpm * 24)
    used = lambda d, empty: sum(1 for v in d.values() if not empty(v))
    t = Text()
    t.append(f"\n {song.title}\n", style=f"bold {P.amber_hi}")
    t.append(f" {song.bpm} bpm · groove {'/'.join(map(str, groove))} · {song.tracks} tracks\n", style=P.amber)
    t.append(f" {used(song.chains, lambda c: all(s.phrase is None for s in c))} chains · "
             f"{used(song.phrases, lambda p: all(s.note is None for s in p))} phrases · "
             f"{len(song.instruments)} instruments\n", style=P.amber)
    t.append(f" about {int(seconds // 60)}:{int(seconds % 60):02d} once through\n\n", style=P.teal)
    for r in range(min(max(0, last + 1), max(1, height - 8))):
        t.append(f" {r:02X} ", style=P.amber_dim)
        t.append(" ".join(tk.hx(c) for c in song.rows[r][:song.tracks]) + "\n", style=P.amber)
    if last < 0:
        t.append(" (an empty song)\n", style=P.amber_dim)
    return t


class CmdLine(Input):
    BINDINGS = [Binding("escape", "close", show=False)]

    def action_close(self) -> None:
        self.display = False
        self.screen.query_one(TrackerView).focus_panel()


class Panel(Widget, can_focus=True):
    def __init__(self, tv: "TrackerView", kind: str, **kw):
        super().__init__(**kw)
        self.tv, self.kind = tv, kind

    def render(self) -> Text:
        return self.tv.draw(self.kind, self.has_focus, self.size.height)

    async def _on_key(self, event: events.Key) -> None:
        if self.tv.key(self.kind, event):
            event.stop()
            event.prevent_default()

    def on_focus(self) -> None:
        self.tv.panel = self.kind
        self.tv.refresh_all()

    def on_blur(self) -> None:
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        self.focus()
        self.tv.click(self.kind, event.x, event.y)


class Status(Widget):
    def __init__(self, tv: "TrackerView", **kw):
        super().__init__(**kw)
        self.tv = tv

    def render(self) -> Text:
        return self.tv.status_text()


class TrackerView(Vertical):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.song = tk.demo()
        self.undo_stack: list[tk.Song] = []
        self.redo_stack: list[tk.Song] = []
        self.cur = {k: [0, 0] for k in PANELS}
        self.song_top = 0
        self.panel = "song"
        self.insert = False
        self.octave = 4
        self.last_inst = 0
        self.clip: tuple[str, object] | None = None
        self.block: tuple[str, object] | None = None
        self.message = "space plays · ? lists the keys · :help the commands"
        self.dirty = False
        self.audio = None                   # tracksynth.AudioProcess (or LocalAudio), started on first sound
        self._audio_lock = threading.Lock()
        self._last_sound = 0.0
        self._typed: tuple[str, int, int, float] | None = None   # hex entry: (panel, row, col, when)

    def compose(self) -> ComposeResult:
        with Horizontal(id="trk-main"):
            for kind, title in (("song", "SONG"), ("chain", "CHAIN"), ("phrase", "PHRASE")):
                p = Panel(self, kind, id=f"trk-{kind}", classes="panel section")
                p.border_title = title
                yield p
            with Vertical(id="trk-right"):
                for kind, title in (("inst", "INSTRUMENT"), ("table", "TABLE"), ("wave", "WAVE")):
                    p = Panel(self, kind, id=f"trk-{kind}", classes="panel section")
                    p.border_title = title
                    yield p
        yield Status(self, id="trk-status")
        yield CmdLine(id="trk-cmd", placeholder=": command   (:w song.mtk · :bpm 140 · :export wav · :help)")

    def on_mount(self) -> None:
        self.query_one("#trk-cmd").display = False
        try:
            last = Path(LAST_FILE.read_text(encoding="utf-8").strip())
            if last.exists():
                self.song = tk.load(last)
        except (OSError, ValueError):
            pass
        self.set_interval(1 / 15, self._frame)

    def on_unmount(self) -> None:
        self._release_audio()

    # --- context: each panel follows the one before it -------------------------------------------

    @property
    def track(self) -> int:
        return min(self.cur["song"][1], self.song.tracks - 1)

    def chain_id(self) -> int | None:
        return self.song.rows[self.cur["song"][0]][self.track]

    def phrase_id(self) -> int | None:
        c = self.chain_id()
        chain = self.song.chains.get(c) if c is not None else None
        return chain[self.cur["chain"][0]].phrase if chain else None

    def inst_id(self) -> int:
        p = self.phrase_id()
        step = self.song.phrases.get(p)[self.cur["phrase"][0]] if p is not None and p in self.song.phrases else None
        if step is not None and step.inst is not None:
            return step.inst
        return self.last_inst

    def table_id(self) -> int | None:
        return self.song.instruments.get(self.inst_id(), tk.Instrument()).table

    def wave_id(self) -> int:
        inst = self.song.instruments.get(self.inst_id())
        return inst.width if inst and inst.kind == "wave" else 0

    def focus_panel(self, kind: str | None = None) -> None:
        self.query_one(f"#trk-{kind or self.panel}", Panel).focus()

    def refresh_all(self) -> None:
        for kind in PANELS:
            self.query_one(f"#trk-{kind}", Panel).refresh()
        self.query_one(Status).refresh()
        c, p, i, t = self.chain_id(), self.phrase_id(), self.inst_id(), self.table_id()
        inst = self.song.instruments.get(i)
        titles = {"song": f"SONG · {self.song.title}{' ●' if self.dirty else ''}",
                  "chain": f"CHAIN {tk.hx(c)}", "phrase": f"PHRASE {tk.hx(p)}",
                  "inst": f"INSTRUMENT {tk.hx(i)} {inst.name if inst else ''}",
                  "table": f"TABLE {tk.hx(t)}", "wave": f"WAVE {tk.hx(self.wave_id())}"}
        for kind, title in titles.items():
            self.query_one(f"#trk-{kind}", Panel).border_title = title
        self.query_one("#trk-phrase", Panel).border_subtitle = \
            f"PIANO · oct {self.octave}" if self.insert else ""

    def _frame(self) -> None:
        if self.audio and self.audio.playing:
            self.refresh_all()
        elif self.audio and time.monotonic() - self._last_sound > IDLE_RELEASE_S:
            self._release_audio()

    # --- drawing ---------------------------------------------------------------------------------

    def _playing(self) -> dict[int, tuple[int, int, int]]:
        if not (self.audio and self.audio.playing):
            return {}
        return {t: pos for t in range(self.song.tracks) if (pos := self.audio.position(t))}

    def draw(self, kind: str, focused: bool, height: int) -> Text:
        return getattr(self, f"_draw_{kind}")(focused, max(1, height))

    def _line(self, t: Text, parts) -> None:
        for text, style in parts:
            t.append(text, style=style)
        t.append("\n")

    def _draw_song(self, focused: bool, height: int) -> Text:
        r0, c0 = self.cur["song"]
        rows = max(1, height - 1)
        if r0 < self.song_top:
            self.song_top = r0
        elif r0 >= self.song_top + rows:
            self.song_top = r0 - rows + 1
        playing, eng = self._playing(), self.audio
        t = Text()
        head = [("  ", P.amber_dim)]
        for tr in range(self.song.tracks):
            mark = "M" if eng and tr in eng.mutes else ("S" if eng and eng.solo == tr else str(tr + 1))
            head.append((f"  {mark}", f"bold {P.red}" if mark in "MS" else f"bold {P.beige}"))
        self._line(t, head)
        for r in range(self.song_top, min(tk.ROWS, self.song_top + rows)):
            parts = [(f"{r:02X}", P.amber_hi if r % 4 == 0 else P.amber_dim)]
            for tr in range(self.song.tracks):
                on = playing.get(tr, (None,))[0] == r
                parts.append(("▸" if on else " ", f"bold {P.teal}"))
                parts.append(_cell(tk.hx(self.song.rows[r][tr]), cursor=(r, tr) == (r0, c0), focused=focused, play=on,
                                   dim=self.song.rows[r][tr] is None))
            self._line(t, parts)
        return t

    def _draw_chain(self, focused: bool, height: int) -> Text:
        c = self.chain_id()
        chain = self.song.chains.get(c) if c is not None else None
        r0, c0 = self.cur["chain"]
        pos = self._playing().get(self.track)
        live = pos and self.audio.mode in ("song", "chain") and self.song.rows[pos[0]][self.track] == c
        t = Text()
        self._line(t, [("   PH TR", P.amber_dim)])
        for s in range(tk.STEPS):
            step = chain[s] if chain else tk.ChainStep()
            on = bool(live and pos[1] == s)
            self._line(t, [(f"{s:X}", P.amber_dim), ("▸ " if on else "  ", f"bold {P.teal}"),
                           _cell(tk.hx(step.phrase), cursor=(s, 0) == (r0, c0), focused=focused, play=on,
                                 dim=step.phrase is None), (" ", ""),
                           _cell(tk.hx(step.transpose) if step.phrase is not None or step.transpose else "--",
                                 cursor=(s, 1) == (r0, c0), focused=focused, dim=not step.transpose)])
        if c is None:
            self._line(t, [("\n n: a new chain here", P.amber_dim)])
        return t

    def _draw_phrase(self, focused: bool, height: int) -> Text:
        p = self.phrase_id()
        phrase = self.song.phrases.get(p) if p is not None else None
        r0, c0 = self.cur["phrase"]
        pos = self._playing().get(self.track)
        live = False
        if pos and self.audio:
            if self.audio.mode == "phrase":
                live = self.audio.focus == (self.track, p)
            else:
                cid = self.audio.focus[1] if self.audio.mode == "chain" else self.song.rows[pos[0]][self.track]
                ch = self.song.chains.get(cid)
                live = bool(ch and ch[pos[1]].phrase == p)
        t = Text()
        self._line(t, [("   NOTE IN CMD", P.amber_dim)])
        for s in range(tk.STEPS):
            st = phrase[s] if phrase else tk.Step()
            on = bool(live and pos[2] == s)
            self._line(t, [(f"{s:X}", P.amber_hi if s % 4 == 0 else P.amber_dim),
                           ("▸ " if on else "  ", f"bold {P.teal}"),
                           _cell(tk.note_name(st.note), cursor=(s, 0) == (r0, c0), focused=focused, play=on,
                                 dim=st.note is None), (" ", ""),
                           _cell(tk.hx(st.inst), cursor=(s, 1) == (r0, c0), focused=focused, dim=st.inst is None),
                           (" ", ""),
                           _cell(st.cmd or "-", cursor=(s, 2) == (r0, c0), focused=focused, dim=not st.cmd),
                           _cell(tk.hx(st.val) if st.cmd else "--", cursor=(s, 3) == (r0, c0), focused=focused,
                                 dim=not st.cmd)])
        if p is None:
            self._line(t, [("\n n in CHAIN: a new phrase", P.amber_dim)])
        elif focused and c0 == 2:
            st = phrase[r0]
            self._line(t, [(f"\n{st.cmd}: {tk.COMMANDS[st.cmd]}" if st.cmd in tk.COMMANDS else
                            "\ntype a letter (+/- cycles): " + " ".join(tk.COMMANDS), P.sand)])
        return t

    def _inst_value(self, inst: tk.Instrument, field: str) -> str:
        v = getattr(inst, field)
        if field in ("kind", "out"):
            return v
        if field == "midi_channel":
            return f"{v:02d}"
        if field == "transpose":
            return f"{tk.hx(v)} ({tk.signed(v):+d})"
        if field == "pan":
            return {0x80: "80 centre", 0: "00 left", 0xFF: "FF right"}.get(v, tk.hx(v))
        return tk.hx(v)

    def _draw_inst(self, focused: bool, height: int) -> Text:
        inst = self.song.instruments.get(self.inst_id()) or tk.Instrument()
        r0 = self.cur["inst"][0]
        t = Text()
        for i, (field, label) in enumerate(INST_FIELDS):
            if field.startswith("midi") and inst.kind != "midi" and inst.out == "synth":
                dim = True
            else:
                dim = False
            self._line(t, [(f"{label:<10}", P.amber_dim),
                           _cell(self._inst_value(inst, field), cursor=i == r0, focused=focused, dim=dim)])
        return t

    def _draw_table(self, focused: bool, height: int) -> Text:
        tid = self.table_id()
        table = self.song.tables.get(tid) if tid is not None else None
        r0, c0 = self.cur["table"]
        t = Text()
        if tid is None:
            self._line(t, [("no table: set one in the", P.amber_dim)])
            self._line(t, [("instrument (table 00…)", P.amber_dim)])
            return t
        self._line(t, [("  VO TR CMD", P.amber_dim)])
        for s in range(min(tk.STEPS, height - 1)):
            row = table[s] if table else tk.TableRow()
            self._line(t, [(f"{s:X} ", P.amber_dim),
                           _cell(tk.hx(row.vol), cursor=(s, 0) == (r0, c0), focused=focused, dim=row.vol is None),
                           (" ", ""),
                           _cell(tk.hx(row.transpose), cursor=(s, 1) == (r0, c0), focused=focused,
                                 dim=not row.transpose), (" ", ""),
                           _cell(row.cmd or "-", cursor=(s, 2) == (r0, c0), focused=focused, dim=not row.cmd),
                           _cell(tk.hx(row.val) if row.cmd else "--", cursor=(s, 3) == (r0, c0), focused=focused,
                                 dim=not row.cmd)])
        return t

    def _draw_wave(self, focused: bool, height: int) -> Text:
        wave = self.song.waves.get(self.wave_id()) or [8] * 32
        c0 = self.cur["wave"][1]
        rows = max(2, min(8, height - 1))
        t = Text()
        for line in range(rows):
            top = (rows - line) * 16 / rows
            parts = []
            for i, v in enumerate(wave):
                full, half = v + 1 >= top, v + 1 >= top - 8 / rows
                ch = "█" if full else ("▄" if half else " ")
                parts.append((ch, f"{P.amber_hi} on {P.amber_faint}" if focused and i == c0 else P.amber))
            self._line(t, parts)
        self._line(t, [(f"sample {c0:02X} = {wave[c0]:X}  j/k or 0-f", P.amber_dim)])
        return t

    def status_text(self) -> Text:
        e = self.audio
        t = Text()
        if e and e.playing:
            t.append(f" ▶ {e.mode.upper():<6}", style=f"bold {P.teal}")
        else:
            t.append(" ■ STOP  ", style=f"bold {P.amber_dim}")
        t.append(f"BPM {e.bpm if e and e.playing else self.song.bpm}  GROOVE {'/'.join(map(str, self.song.groove))}  ",
                 style=P.amber)
        if e and e.playing:
            for tr in range(self.song.tracks):
                lvl = e.peak(tr)
                t.append(" ▁▂▃▄▅▆▇█"[min(8, int(lvl * 9))], style=P.teal if tr not in e.mutes else P.red)
            t.append("  ")
        t.append(f"♪ {e.how if e else 'idle'}", style=P.amber_dim)
        if e and e.dropouts:
            t.append(f" ⚠ {e.dropouts} dropouts", style=f"bold {P.red}")
        if e and e.midi_port:
            t.append(f" · MIDI {e.midi_port.rsplit('/', 1)[-1]}", style=P.amber_dim)
        if self.insert:
            t.append(f"  ◆ PIANO oct {self.octave}", style=f"bold {P.red}")
        if self.message:
            t.append(f"  │ {self.message}", style=f"bold {P.sand}")
        return t

    # --- audio -----------------------------------------------------------------------------------

    def _ensure_audio(self) -> None:
        from . import tracksynth as ts
        self._last_sound = time.monotonic()
        with self._audio_lock:                                      # warm() may be starting it right now
            if self.audio is None or not self.audio.alive:
                rate = int(getattr(self.app.cfg, "tracker_rate", 44100) or 44100)
                try:
                    self.audio = ts.AudioProcess(self.song, rate)
                except Exception as exc:
                    self.audio = ts.LocalAudio(self.song, rate)        # no second process: share ours
                    self.audio.how += " (shared: may stutter)"
                    self.message = f"audio runs inside the TUI ({type(exc).__name__}: {exc})"[:120]
                if self.audio.how == "none":
                    self.message = "no audio output (install PortAudio or alsa-utils); MIDI still works"
        self._sync()

    def warm(self) -> None:
        """Start the audio process in the background when TRACKER opens, so the first note isn't late."""
        if self.audio is None:
            self.run_worker(self._ensure_audio, thread=True, exclusive=True, group="trk-audio")

    def _sync(self) -> None:
        if self.audio:
            self.audio.set_song(self.song)

    def _release_audio(self) -> None:
        if self.audio:
            self.audio.close()
            self.audio = None

    def play(self) -> None:
        self._ensure_audio()
        e = self.audio
        if e.playing:
            e.stop()
            self.message = ""
        elif self.panel == "song":
            e.play_song(self.cur["song"][0])
        elif self.panel == "chain" and self.chain_id() is not None:
            e.play_chain(self.track, self.chain_id())
        elif self.phrase_id() is not None:
            e.play_phrase(self.track, self.phrase_id())
        else:
            e.play_song(self.cur["song"][0])
        self.refresh_all()

    def preview(self, note: int | None, inst: int | None) -> None:
        if note is None:
            return
        self._ensure_audio()
        if not self.audio.playing:
            self.audio.preview(self.track, note, inst)

    # --- editing ---------------------------------------------------------------------------------

    def push(self) -> None:
        self.undo_stack.append(copy.deepcopy(self.song))
        del self.undo_stack[:-100]
        self.redo_stack.clear()
        self.dirty = True

    def undo(self, back: bool) -> None:
        src, dst = (self.undo_stack, self.redo_stack) if back else (self.redo_stack, self.undo_stack)
        if not src:
            self.message = "nothing to " + ("undo" if back else "redo")
            return
        dst.append(copy.deepcopy(self.song))
        path = self.song.path
        self.song = src.pop()
        self.song.path = path
        self._sync()
        self.message = "undone" if back else "redone"
        self.dirty = True

    def _cols(self, kind: str) -> int:
        return {"song": self.song.tracks, "chain": 2, "phrase": 4, "inst": 1, "table": 4, "wave": 32}[kind]

    def _rows(self, kind: str) -> int:
        return {"song": tk.ROWS, "inst": len(INST_FIELDS), "wave": 1}.get(kind, tk.STEPS)

    def move(self, kind: str, dr: int, dc: int) -> None:
        r, c = self.cur[kind]
        self.cur[kind] = [max(0, min(self._rows(kind) - 1, r + dr)), max(0, min(self._cols(kind) - 1, c + dc))]
        self._typed = None

    def _target(self, kind: str):
        """(the object, attribute) the cursor edits, creating the chain/phrase/table if needed."""
        r, c = self.cur[kind]
        if kind == "song":
            return self.song.rows[r], c
        if kind == "chain":
            cid = self.chain_id()
            return (None, None) if cid is None else (self.song.chain(cid)[r], ("phrase", "transpose")[c])
        if kind == "phrase":
            pid = self.phrase_id()
            return (None, None) if pid is None else (self.song.phrase(pid)[r], ("note", "inst", "cmd", "val")[c])
        if kind == "inst":
            return self.song.instrument(self.inst_id()), INST_FIELDS[r][0]
        if kind == "table":
            tid = self.table_id()
            return (None, None) if tid is None else (self.song.table(tid)[r], ("vol", "transpose", "cmd", "val")[c])
        return self.song.wave(self.wave_id()), c

    def _get(self, obj, attr):
        return obj[attr] if isinstance(attr, int) else getattr(obj, attr)

    def _set(self, obj, attr, value) -> None:
        if isinstance(attr, int):
            obj[attr] = value
        else:
            setattr(obj, attr, value)

    def nudge(self, kind: str, step: int) -> None:
        obj, attr = self._target(kind)
        if obj is None:
            self.message = "nothing here yet: n makes one"
            return
        v = self._get(obj, attr)
        self.push()
        if attr == "note":
            v = 60 if v is None or v == tk.NOTE_OFF else v
            new = max(0, min(127, v + (12 if abs(step) == 16 and step > 0 else -12 if abs(step) == 16 else step)))
            self._set(obj, attr, new)
            if obj.inst is None:
                obj.inst = self.last_inst
            self.preview(new, obj.inst)
        elif attr in ("kind", "out"):
            choices = tk.KINDS if attr == "kind" else tk.OUTS
            self._set(obj, attr, choices[(choices.index(v) + (1 if step > 0 else -1)) % len(choices)])
        elif attr == "cmd":
            letters = sorted(tk.COMMANDS)
            i = letters.index(v) if v in letters else -1
            self._set(obj, attr, letters[(i + (1 if step > 0 else -1)) % len(letters)])
        elif kind == "wave":
            self._set(obj, attr, max(0, min(15, (v or 0) + (1 if step > 0 else -1) * (4 if abs(step) == 16 else 1))))
        elif attr == "midi_channel":
            self._set(obj, attr, max(1, min(16, v + (1 if step > 0 else -1))))
        else:
            base = 0 if v is None else v
            self._set(obj, attr, (base + step) & 0xFF)
            if attr == "inst":
                self.last_inst = obj.inst

    def type_hex(self, kind: str, digit: int) -> None:
        obj, attr = self._target(kind)
        if obj is None:
            self.message = "nothing here yet: n makes one"
            return
        if attr in ("note", "kind", "out", "cmd"):
            return
        if kind == "wave":
            self.push()
            self._set(obj, attr, digit)
            self.move(kind, 0, 1)
            return
        now = time.monotonic()
        here = (kind, *self.cur[kind])
        old = self._get(obj, attr)
        continuing = self._typed and self._typed[:3] == here and now - self._typed[3] < 2
        self.push()
        if attr == "midi_channel":
            value = ((old * 10 + digit) if continuing else digit)
            value = value if 1 <= value <= 16 else digit or 1
        else:
            value = (((old or 0) << 4 | digit) & 0xFF) if continuing else digit
        self._set(obj, attr, value)
        if attr == "inst":
            self.last_inst = value
        self._typed = (*here, now)

    def clear(self, kind: str) -> None:
        obj, attr = self._target(kind)
        if obj is None or attr in ("kind", "out", "midi_channel"):
            return
        self.push()
        if kind == "wave":
            self._set(obj, attr, 8)
        elif attr in ("transpose", "val", "volume", "attack", "decay", "sustain", "release", "width", "pan"):
            self._set(obj, attr, 0)
        elif attr == "cmd":
            obj.cmd, obj.val = "", 0
        elif attr == "note":
            obj.note, obj.inst, obj.cmd, obj.val = None, None, "", 0
        else:
            self._set(obj, attr, None)

    def new(self, kind: str, clone: bool) -> None:
        """n / N: a fresh (or cloned) chain in SONG, phrase in CHAIN, instrument in PHRASE."""
        if kind == "song":
            row, col = self.cur["song"]
            cur = self.song.rows[row][col]
            self.push()
            new = self.song.clone_chain(cur) if clone and cur is not None else self.song.new_chain()
            if new is None:
                self.message = "all 256 chains are used"
                return
            self.song.rows[row][col] = new
            self.message = f"chain {new:02X}" + (f" (a copy of {cur:02X})" if clone and cur is not None else "")
        elif kind == "chain":
            cid = self.chain_id()
            if cid is None:
                self.message = "no chain here: n in SONG first"
                return
            step = self.song.chain(cid)[self.cur["chain"][0]]
            self.push()
            new = self.song.clone_phrase(step.phrase) if clone and step.phrase is not None else self.song.new_phrase()
            if new is None:
                self.message = "all 256 phrases are used"
                return
            step.phrase = new
            self.message = f"phrase {new:02X}"
        elif kind == "phrase" and self.cur["phrase"][1] == 1:
            obj, _ = self._target("phrase")
            if obj is None:
                return
            used = set(self.song.instruments)
            new = next((i for i in range(64) if i not in used), None)
            if new is None:
                return
            self.push()
            src = self.song.instruments.get(self.inst_id())
            self.song.instruments[new] = copy.deepcopy(src) if clone and src else tk.Instrument(name=f"inst{new:02X}")
            obj.inst = self.last_inst = new
            self.message = f"instrument {new:02X}"
        elif kind == "inst" and INST_FIELDS[self.cur["inst"][0]][0] == "table":
            inst = self.song.instrument(self.inst_id())
            used = set(self.song.tables) | {i.table for i in self.song.instruments.values() if i.table is not None}
            new = next((i for i in range(32) if i not in used), None)
            if new is not None:
                self.push()
                inst.table = new
                self.song.table(new)
                self.message = f"table {new:02X}"

    def copy_cell(self, kind: str, whole: bool) -> None:
        if whole:
            src = {"chain": lambda: self.song.chains.get(self.chain_id()),
                   "phrase": lambda: self.song.phrases.get(self.phrase_id()),
                   "table": lambda: self.song.tables.get(self.table_id())}.get(kind)
            data = src() if src else None
            if data is None:
                self.message = "Y copies a whole chain, phrase or table"
                return
            self.block = (kind, copy.deepcopy(data))
            self.message = f"copied the {kind}"
            return
        obj, attr = self._target(kind)
        if obj is not None:
            self.clip = (str(attr), copy.deepcopy(obj if kind in ("phrase", "table") and attr in ("note", "vol")
                                                   else self._get(obj, attr)))
            self.message = "copied"

    def paste(self, kind: str, whole: bool) -> None:
        if whole:
            if not self.block or self.block[0] != kind:
                self.message = f"no {kind} copied (Y)"
                return
            ids = {"chain": self.chain_id, "phrase": self.phrase_id, "table": self.table_id}[kind]()
            if ids is None:
                self.message = f"no {kind} here: n makes one"
                return
            self.push()
            store = {"chain": self.song.chains, "phrase": self.song.phrases, "table": self.song.tables}[kind]
            store[ids] = copy.deepcopy(self.block[1])
            self.message = f"pasted into {kind} {ids:02X}"
            return
        obj, attr = self._target(kind)
        if obj is None or not self.clip:
            return
        what, value = self.clip
        self.push()
        if what in ("note", "vol") and not isinstance(value, (int, type(None))):
            for f in value.__dataclass_fields__:                 # a whole step
                setattr(obj, f, copy.deepcopy(getattr(value, f)))
        elif str(attr) == what or kind in ("song", "wave"):
            self._set(obj, attr, copy.deepcopy(value))
        self.message = "pasted"

    def dive(self, kind: str) -> None:
        order = {"song": "chain", "chain": "phrase", "inst": "table", "table": "wave"}
        if kind == "phrase":
            target = "inst" if self.cur["phrase"][1] == 1 else None
            if target is None:
                self.insert = not self.insert
                self.message = "piano: z s x d c… / q 2 w 3 e…  +/- octave  = off  esc back" if self.insert else ""
                return
        else:
            target = order.get(kind)
        if target:
            self.focus_panel(target)

    def step_panel(self, kind: str, step: int) -> None:
        i = (PANELS.index(kind) + step) % len(PANELS)
        self.focus_panel(PANELS[i])

    def piano(self, ch: str) -> bool:
        pid = self.phrase_id()
        if pid is None:
            self.message = "no phrase here: n in CHAIN makes one"
            return True
        ch = {"ö": ";", "Ö": ";"}.get(ch, ch)              # Swedish layout: ö sits where ; is (D#)
        if ch in LOWER:
            note = (self.octave + 1) * 12 + LOWER.index(ch)
        elif ch in UPPER:
            note = (self.octave + 2) * 12 + UPPER.index(ch)
        elif ch == "=":
            note = tk.NOTE_OFF
        else:
            return False
        self.push()
        row = self.cur["phrase"][0]
        step = self.song.phrase(pid)[row]
        step.note = max(0, min(127, note)) if note != tk.NOTE_OFF else note
        if note != tk.NOTE_OFF and step.inst is None:
            step.inst = self.last_inst
        self.preview(step.note, step.inst)
        self.cur["phrase"][0] = (row + 1) % tk.STEPS
        return True

    def key(self, kind: str, event: events.Key) -> bool:
        k, ch = event.key, event.character or ""
        handled = True
        if k == "escape":
            if self.insert:
                self.insert = False
                self.message = ""
            else:
                return False                                  # back out to the sections (app)
        elif self.insert and kind == "phrase" and k not in ("up", "down", "left", "right", "space", "colon"):
            if k in ("plus", "minus"):
                self.octave = max(0, min(8, self.octave + (1 if k == "plus" else -1)))
            elif k in ("backspace", "delete"):
                self.clear("phrase")
            elif not self.piano(ch):
                return False
        elif k in ("up", "k"):
            self.move(kind, -1, 0) if kind != "wave" else self.nudge(kind, 1)
        elif k in ("down", "j"):
            self.move(kind, 1, 0) if kind != "wave" else self.nudge(kind, -1)
        elif k in ("left", "h"):
            self.nudge(kind, -1) if kind == "inst" else self.move(kind, 0, -1)
        elif k in ("right", "l"):
            self.nudge(kind, 1) if kind == "inst" else self.move(kind, 0, 1)
        elif k in ("pageup", "pagedown"):
            self.move(kind, -16 if k == "pageup" else 16, 0)
        elif k in ("H", "shift+left"):
            self.step_panel(kind, -1)
        elif k in ("L", "shift+right"):
            self.step_panel(kind, 1)
        elif k == "enter":
            self.dive(kind)
        elif k == "space":
            self.play()
        elif k in ("plus", "minus"):
            self.nudge(kind, 1 if k == "plus" else -1)
        elif k in ("greater_than_sign", "less_than_sign"):
            self.nudge(kind, 16 if k == "greater_than_sign" else -16)
        elif k == "colon":
            self.open_command("")
        elif kind in ("phrase", "table") and self.cur[kind][1] == 2 and ch.upper() in tk.COMMANDS:
            obj, _ = self._target(kind)
            if obj is not None:
                self.push()
                obj.cmd = ch.upper()
                self.message = f"{obj.cmd}: {tk.COMMANDS[obj.cmd]}"
        elif k == "ctrl+s":
            self.run_command("w")
        elif k == "ctrl+o":
            self.browse()
        elif k == "u":
            self.undo(True)
        elif k == "ctrl+r":
            self.undo(False)
        elif k in ("x", "delete", "backspace"):
            self.clear(kind)
        elif k in ("n", "N"):
            self.new(kind, clone=k == "N")
        elif k in ("y", "Y"):
            self.copy_cell(kind, whole=k == "Y")
        elif k in ("p", "P"):
            self.paste(kind, whole=k == "P")
        elif k == "m":
            self._ensure_audio()
            self.audio.toggle_mute(self.track)
        elif k == "s":
            self._ensure_audio()
            self.audio.set_solo(None if self.audio.solo == self.track else self.track)
        elif k == "i" and kind == "phrase":
            self.insert = True
            self.message = "piano: z s x d c… / q 2 w 3 e…  +/- octave  = off  esc back"
        elif ch.lower() in HEX and len(ch) == 1 and not ch.isupper():
            self.type_hex(kind, HEX.index(ch.lower()))
        else:
            handled = False
        if handled:
            self._sync()
            if kind == "phrase":
                obj, _ = self._target("phrase")
                if obj is not None and obj.inst is not None:
                    self.last_inst = obj.inst
            self.refresh_all()
        return handled

    def click(self, kind: str, x: int, y: int) -> None:
        row = y - 1
        if kind == "song":
            self.cur["song"] = [max(0, min(tk.ROWS - 1, self.song_top + row)),
                                max(0, min(self.song.tracks - 1, (x - 2) // 3))]
        elif kind in ("chain", "phrase", "table") and 0 <= row < tk.STEPS:
            self.cur[kind][0] = row
        elif kind == "inst" and 0 <= y < len(INST_FIELDS):
            self.cur["inst"][0] = y
        self.refresh_all()

    # --- : commands ------------------------------------------------------------------------------

    def open_command(self, prefill: str) -> None:
        cmd = self.query_one("#trk-cmd", Input)
        cmd.display = True
        cmd.value = prefill
        cmd.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "trk-cmd":
            return
        event.stop()
        event.input.display = False
        self.focus_panel()
        self.run_command(event.value.strip().lstrip(":"))

    def _dir(self) -> Path:
        return Path(getattr(self.app.cfg, "tracker_dir", "~/Music/MABEL-OS")).expanduser()

    def _path(self, name: str, suffix: str = "") -> Path:
        p = Path(name).expanduser()
        p = p if p.is_absolute() else self._dir() / p
        return p.with_suffix(suffix) if suffix and not p.suffix else p

    def browse(self) -> None:
        """ctrl+o / :e: the file browser (browser.py) on tracker_dir; exports are listed, dimmed."""
        from .browser import Browser, BrowserSpec

        def renamed(old: Path, new: Path) -> None:
            if self.song.path == old:
                self.song.path = new
                self._remember()
        spec = BrowserSpec("TRACKER · SONGS", self._dir(), (".mtk",), also_show=(".wav", ".mid"),
                           preview=song_card, new_suffix=".mtk", dirty=self.dirty, current=self.song.path,
                           renamed=renamed)
        self.app.push_screen(Browser(spec), self._browsed)

    def _browsed(self, choice) -> None:
        if choice is not None:
            try:
                if choice.save_first:
                    if self.song.path is None:
                        self.message = "this song has no name yet: :w name.mtk first"
                        self.refresh_all()
                        return
                    tk.save(self.song)
                if choice.new:
                    song = tk.Song(title=choice.path.stem, tracks=self.song.tracks)
                    tk.save(song, choice.path)
                    self._load(song)
                    self.message = f"new song {choice.path.name}"
                else:
                    self._load(tk.load(choice.path))
                    self.message = f"opened {choice.path.name}"
                self._remember()
            except Exception as exc:
                self.message = f"✖ {exc}"
            self._sync()
        self.refresh_all()
        self.focus_panel()

    def _remember(self) -> None:
        try:
            if self.song.path:
                LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
                LAST_FILE.write_text(str(self.song.path), encoding="utf-8")
        except OSError:
            pass

    def run_command(self, line: str) -> None:
        try:
            self.message = self._command(line.split()) or ""
        except Exception as exc:
            self.message = f"✖ {exc}"
        self._sync()
        self.refresh_all()

    def _load(self, song: tk.Song) -> None:
        if self.audio:
            self.audio.stop()
        self.song = song
        self._sync()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.cur = {k: [0, 0] for k in PANELS}
        self.song_top, self.dirty = 0, False

    def _command(self, args: list[str]) -> str | None:
        from . import tracksynth as ts
        if not args:
            return None
        cmd, rest = args[0].rstrip("!"), args[1:]
        force = args[0].endswith("!")
        if cmd in ("new", "e", "demo") and self.dirty and not force:
            return f"unsaved changes: :w first, or :{cmd}! to drop them"
        if cmd == "w":
            target = self._path(" ".join(rest), ".mtk") if rest else None
            if target is None and self.song.path is None:
                return "name it: :w song.mtk"
            path = tk.save(self.song, target)
            self.dirty = False
            self._remember()
            return f"saved {path}"
        if cmd in ("files", "browse") or (cmd == "e" and not rest):
            self.browse()
            return None
        if cmd == "e":
            self._load(tk.load(self._path(" ".join(rest), ".mtk")))
            self._remember()
            return f"opened {self.song.path}"
        if cmd == "new":
            self._load(tk.Song(tracks=int(rest[0]) if rest else 4))
            return f"a new song, {self.song.tracks} tracks"
        if cmd == "demo":
            self._load(tk.demo())
            return "the demo song"
        if cmd == "bpm":
            self.push()
            self.song.bpm = max(20, min(400, int(rest[0])))
            if self.audio:
                self.audio.set_bpm(self.song.bpm)
            return f"{self.song.bpm} bpm"
        if cmd == "groove":
            self.push()
            self.song.groove = [max(1, min(32, int(g))) for g in rest] or [6, 6]
            return "groove " + "/".join(map(str, self.song.groove))
        if cmd == "tracks":
            self.push()
            self.song.tracks = max(1, min(tk.MAX_TRACKS, int(rest[0])))
            return f"{self.song.tracks} tracks"
        if cmd == "title":
            self.push()
            self.song.title = " ".join(rest) or self.song.title
            return None
        if cmd == "name":
            self.push()
            self.song.instrument(self.inst_id()).name = " ".join(rest)[:12] or "inst"
            return None
        if cmd == "wave":
            shapes = {"sine": lambda i: 7.5 + 7.5 * math.sin(i / 32 * 2 * math.pi),
                      "saw": lambda i: i / 31 * 15, "square": lambda i: 15 if i < 16 else 0,
                      "tri": lambda i: 15 - abs(i - 16) * 15 / 16}
            if not rest or rest[0] not in shapes:
                return "waves: " + " ".join(shapes)
            self.push()
            self.song.waves[self.wave_id()] = [int(round(shapes[rest[0]](i))) for i in range(32)]
            return f"wave {self.wave_id():02X} = {rest[0]}"
        if cmd == "export":
            how = rest[0] if rest and rest[0] in ("wav", "mid") else "wav"
            stem = self.song.path.stem if self.song.path else self.song.title.replace(" ", "-")
            path = self._path(rest[1] if len(rest) > 1 else f"{stem}.{how}", f".{how}")
            path.parent.mkdir(parents=True, exist_ok=True)
            if how == "wav":
                seconds = ts.export_wav(self.song, path)
                return f"exported {path} ({seconds:.0f} s)"
            notes = ts.export_midi(self.song, path)
            return f"exported {path} ({notes} notes)"
        if cmd == "midi":
            ports = ts.midi_ports()
            if not rest:
                now = self.song.midi_port or "auto"
                return f"MIDI {now} · ports: " + (" ".join(f"{i}:{p}" for i, p in enumerate(ports)) or "none")
            choice = rest[0]
            self.push()
            self.song.midi_port = ports[int(choice)] if choice.isdigit() and int(choice) < len(ports) else \
                ("" if choice == "auto" else choice)
            if self.audio:
                self.audio.set_midi(self.song.midi_port)
                self.audio.midi_port = self.song.midi_port or "auto"
            return f"MIDI {self.song.midi_port or 'auto'}"
        if cmd in ("help", "h"):
            self.app.notify(":w [file] · :e file · :new [tracks] · :demo · :bpm N · :groove 6 6 · :tracks N\n"
                            ":title … · :name … (instrument) · :wave sine|saw|square|tri · :midi [N|path|auto]\n"
                            ":export wav|mid [file]\nCommands: " +
                            "  ".join(f"{k} {v}" for k, v in tk.COMMANDS.items()), title="TRACKER", timeout=25)
            return None
        return f"unknown command :{cmd} (try :help)"

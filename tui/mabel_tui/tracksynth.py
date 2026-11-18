"""TRACKER's engine: a tick sequencer driving a small chip synth and MIDI out.

Time runs in ticks, 24 per beat (LSDJ's tempo): a step lasts groove[i] ticks (6 = a 16th).
`Engine.render(n)` makes n stereo samples and runs every tick that falls inside them, so the
same code plays live (Output: sounddevice, or an `aplay` pipe when PortAudio is missing) and
renders files offline (export_wav, export_midi) with no sound card at all.

MIDI goes straight to a rawmidi device (/dev/snd/midiC*D*), like Mabel's music job does for
the NSX-39 Pocket Miku; no MIDI library needed.
"""
from __future__ import annotations

import glob
import json
import math
import os
import struct
import subprocess
import threading
import time
import wave as wavefile

import numpy as np

from . import tracker as tk

TICKS_PER_BEAT = 24
NOISE = np.random.default_rng(7).uniform(-1, 1, 32768).astype(np.float32)
MASTER = 0.32
RAMP = 64                                    # samples a gain change takes (~1.5 ms at 44.1 kHz)


def note_hz(n: float) -> float:
    return 440.0 * 2 ** ((n - 69) / 12)


# --- MIDI -------------------------------------------------------------------------------------

def midi_ports() -> list[str]:
    return sorted(glob.glob("/dev/snd/midiC*D*"))


class Midi:
    """Writes raw MIDI bytes to a rawmidi device. Silent if there is none."""

    def __init__(self, port: str | None = ""):
        """port: a device path, "" = the first one found, None = no MIDI at all."""
        self.port = "" if port is None else (port or next(iter(midi_ports()), ""))
        self.fd: int | None = None
        self.programs: dict[int, int] = {}
        if self.port:
            try:
                self.fd = os.open(self.port, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                self.fd = None

    def send(self, *data: int) -> None:
        if self.fd is not None:
            try:
                os.write(self.fd, bytes(d & 0xFF for d in data))
            except OSError:
                pass

    def note_on(self, ch: int, note: int, vel: int) -> None:
        self.send(0x90 | (ch - 1) & 15, note & 127, max(1, min(127, vel)))

    def note_off(self, ch: int, note: int) -> None:
        self.send(0x80 | (ch - 1) & 15, note & 127, 0)

    def program(self, ch: int, prog: int) -> None:
        if self.programs.get(ch) != prog:
            self.programs[ch] = prog
            self.send(0xC0 | (ch - 1) & 15, prog & 127)

    def all_off(self) -> None:
        for ch in range(16):
            self.send(0xB0 | ch, 123, 0)

    def close(self) -> None:
        if self.fd is not None:
            self.all_off()
            os.close(self.fd)
            self.fd = None


# --- a voice per track ----------------------------------------------------------------------------

class Voice:
    def __init__(self, engine: "Engine", track: int):
        self.e, self.track = engine, track
        self.inst: tk.Instrument | None = None
        self.inst_id: int | None = None
        self.on = False
        self.level = 0.0
        self.stage = "off"
        self.note = 60.0
        self.target: float | None = None
        self.slide = 0.0
        self.phase = 0.0
        self.vol = 1.0                       # E command
        self.pan = 0.5
        self.width = 0.5
        self.wave = np.zeros(32, np.float32)
        self.table: list[tk.TableRow] | None = None
        self.table_row = 0
        self.table_vol = 1.0
        self.table_tr = 0
        self.chord: tuple[int, int] | None = None
        self.chord_i = 0
        self.bend = 0.0
        self.vib = (0, 0)
        self.vib_phase = 0.0
        self.retrig = self.retrig_n = 0
        self.kill = -1
        self.delay = -1
        self.pending: tuple[int, int | None] | None = None
        self.midi_note: tuple[int, int] | None = None
        self.peak = 0.0
        self.amp = 0.0                       # the gain last rendered, ramped from on every change

    # events
    def trigger(self, note: int, inst_id: int | None, slide: int = 0) -> None:
        if inst_id is None:
            inst_id = self.inst_id if self.inst_id is not None else 0
        inst = self.e.song.instruments.get(inst_id) or tk.Instrument()
        note = note + tk.signed(inst.transpose)
        if slide and self.on and self.inst_id == inst_id:
            self.target, self.slide = float(note), slide / 16
            self._midi_on(inst, note)
            return
        self.inst, self.inst_id = inst, inst_id
        self.note, self.target = float(note), None
        self.vol, self.pan = 1.0, inst.pan / 255
        self.width = min(0.95, max(0.05, inst.width / 256))
        self.wave = np.array(self.e.song.waves.get(inst.width if inst.kind == "wave" else 0)
                             or [8] * 32, np.float32) / 7.5 - 1
        self.table = self.e.song.tables.get(inst.table) if inst.table is not None else None
        self.table_row, self.table_vol, self.table_tr = 0, 1.0, 0
        self.chord, self.bend, self.vib, self.retrig, self.kill = None, 0.0, (0, 0), 0, -1
        self.on = inst.kind != "midi" and inst.out in ("synth", "both")
        self.stage = "a" if self.on else "off"
        self.level = 0.0 if (self.on and inst.attack) else (1.0 if self.on else 0.0)
        if self.on and not inst.attack:
            self.stage = "d"
        self._midi_on(inst, note)
        self.e.log("on", self.track, note, inst)

    def _midi_on(self, inst: tk.Instrument, note: int) -> None:
        self._midi_off()
        if inst.kind == "midi" or inst.out in ("midi", "both"):
            ch = max(1, min(16, inst.midi_channel))
            if inst.midi_program is not None:
                self.e.midi.program(ch, inst.midi_program)
            self.e.midi.note_on(ch, note, inst.volume // 2)
            self.midi_note = (ch, note)

    def _midi_off(self) -> None:
        if self.midi_note:
            self.e.midi.note_off(*self.midi_note)
            self.midi_note = None

    def release(self) -> None:
        sounding = self.stage not in ("off", "r") or self.midi_note is not None
        if self.stage not in ("off", "r"):
            self.stage = "r"
            self._rel_from = self.level
        self._midi_off()
        if sounding:
            self.e.log("off", self.track, int(self.note), self.inst)

    def stop(self) -> None:
        self.stage, self.level, self.on = "off", 0.0, False
        self._midi_off()

    def command(self, cmd: str, val: int) -> None:
        if cmd == "C":
            self.chord = (val >> 4, val & 15) if val else None
        elif cmd == "E":
            self.vol = val / 255
        elif cmd == "K":
            self.kill = val
        elif cmd == "O":
            self.pan = val / 255
        elif cmd == "P":
            self.bend = (val - 0x80) / 64
        elif cmd == "R":
            self.retrig = self.retrig_n = val
        elif cmd == "V":
            self.vib = (val >> 4, val & 15)
        elif cmd == "W":
            if self.inst and self.inst.kind == "wave":
                self.wave = np.array(self.e.song.waves.get(val) or [8] * 32, np.float32) / 7.5 - 1
            else:
                self.width = min(0.95, max(0.05, val / 256))
        elif cmd == "A":
            self.table, self.table_row = self.e.song.tables.get(val), 0

    # once per tick
    def tick(self) -> None:
        if self.delay >= 0:
            self.delay -= 1
            if self.delay < 0 and self.pending:
                self.trigger(*self.pending)
                self.pending = None
        inst = self.inst
        if inst is None:
            return
        if self.stage == "a":
            self.level += 1 / max(1, inst.attack)
            if self.level >= 1:
                self.level, self.stage = 1.0, "d"
        elif self.stage == "d":
            sus = inst.sustain / 255
            self.level -= (1 - sus) / max(1, inst.decay)
            if self.level <= sus:
                self.level, self.stage = sus, "s"
                if sus <= 0:
                    self.stage, self.on = "off", False
        elif self.stage == "r":
            self.level -= getattr(self, "_rel_from", 1.0) / max(1, inst.release)
            if self.level <= 0:
                self.level, self.stage, self.on = 0.0, "off", False
        if self.table:
            row = self.table[self.table_row % tk.STEPS]
            if row.vol is not None:
                self.table_vol = row.vol / 255
            self.table_tr = tk.signed(row.transpose)
            if row.cmd == "H":
                self.table_row = row.val % tk.STEPS
            else:
                if row.cmd:
                    self.command(row.cmd, row.val)
                self.table_row += 1
        if self.chord:
            self.chord_i = (self.chord_i + 1) % 3
        self.note += self.bend
        if self.target is not None:
            step = self.slide or 1
            self.note = min(self.target, self.note + step) if self.target > self.note else max(self.target, self.note - step)
            if self.note == self.target:
                self.target = None
        if self.vib[0]:
            self.vib_phase += self.vib[0] / 16
        if self.retrig:
            self.retrig_n -= 1
            if self.retrig_n <= 0:
                self.retrig_n, self.level, self.stage = self.retrig, 1.0, "d"
        if self.kill >= 0:
            self.kill -= 1
            if self.kill < 0:
                self.release()

    def pitch(self) -> float:
        n = self.note + self.table_tr
        if self.chord:
            n += (0, self.chord[0], self.chord[1])[self.chord_i]
        if self.vib[1]:
            n += math.sin(self.vib_phase * 2 * math.pi) * self.vib[1] / 16
        return n

    def render(self, n: int, out: np.ndarray) -> None:
        if not self.on or self.inst is None or self.level <= 0:
            self.peak *= 0.8
            if self.amp > 0 and self.inst is not None:      # fade out what was sounding instead of cutting it
                self.level = 0.0                             # (falls through and ramps to 0)
            else:
                self.amp = 0.0
                return
        hz = note_hz(self.pitch())
        kind = self.inst.kind
        rate = hz * (8 if kind == "noise" else 1) / self.e.sr
        phases = self.phase + rate * np.arange(1, n + 1, dtype=np.float64)
        self.phase = float(phases[-1] % 32768)
        frac = phases % 1.0
        if kind == "pulse":
            sig = np.where(frac < self.width, 1.0, -1.0)
        elif kind == "tri":
            sig = 4 * np.abs(frac - 0.5) - 1
        elif kind == "saw":
            sig = 2 * frac - 1
        elif kind == "wave":
            sig = self.wave[(frac * 32).astype(np.int64) % 32]
        else:
            sig = NOISE[np.floor(phases).astype(np.int64) % 32768]
        amp = self.level * (self.inst.volume / 255) * self.vol * self.table_vol
        if self.e.muted(self.track):
            amp = 0.0
        self.peak = max(self.peak * 0.8, amp)
        if amp != self.amp:                                  # ramp over ~1.5 ms: steps and note starts don't click
            ramp = min(n, RAMP)
            gain = np.full(n, amp)
            gain[:ramp] = np.linspace(self.amp, amp, ramp, endpoint=False)
            self.amp = amp
        else:
            gain = amp
        out[:n, 0] += sig * gain * math.cos(self.pan * math.pi / 2)
        out[:n, 1] += sig * gain * math.sin(self.pan * math.pi / 2)


# --- the sequencer --------------------------------------------------------------------------------

class Track:
    def __init__(self, i: int):
        self.i = i
        self.reset(0)
        self.running = False

    def reset(self, row: int) -> None:
        self.row, self.cstep, self.pstep = row, 0, 0
        self.ticks_left, self.groove_i, self.fresh, self.hop = 0, 0, True, False
        self.looped = False


class Engine:
    def __init__(self, song: tk.Song, sr: int = 44100, midi: Midi | None = None):
        self.song, self.sr = song, sr
        self.midi = midi or Midi(None)
        self.voices = [Voice(self, t) for t in range(tk.MAX_TRACKS)]
        self.tracks = [Track(t) for t in range(tk.MAX_TRACKS)]
        self.mode: str | None = None              # song | chain | phrase
        self.focus: tuple[int, int] = (0, 0)      # (track, chain or phrase id) for chain/phrase loops
        self.bpm = song.bpm
        self.tick_left = 0.0
        self.mutes: set[int] = set()
        self.solo: int | None = None
        self.lock = threading.RLock()
        self.events: list[tuple] | None = None    # offline export collects note events here
        self.samples = 0
        self.dropouts = 0                         # underruns the sound card reported (Output)
        self._preview_off: dict[int, int] = {}

    # control (from the UI thread)
    def play_song(self, row: int = 0) -> None:
        with self.lock:
            self._start("song")
            for t in self.tracks[:self.song.tracks]:
                t.reset(row)
                t.running = self._cell(t) is not None or self._block_start(t, row) is not None
                if t.running and self._cell(t) is None:
                    t.row = self._block_start(t, row)

    def play_chain(self, track: int, chain: int) -> None:
        with self.lock:
            self._start("chain", (track, chain))
            self.tracks[track].reset(0)
            self.tracks[track].running = True

    def play_phrase(self, track: int, phrase: int) -> None:
        with self.lock:
            self._start("phrase", (track, phrase))
            self.tracks[track].reset(0)
            self.tracks[track].running = True

    def _start(self, mode: str, focus=(0, 0)) -> None:
        self.stop_all()
        self.mode, self.focus, self.bpm = mode, focus, self.song.bpm
        self.tick_left = 0.0

    def stop(self) -> None:
        with self.lock:
            self.stop_all()

    def stop_all(self) -> None:
        self.mode = None
        for t in self.tracks:
            t.running = False
        for v in self.voices:
            v.release()
        self.midi.all_off()

    def preview(self, track: int, note: int, inst: int | None) -> None:
        """Play one note on a track's voice while editing (released after a beat)."""
        with self.lock:
            if note == tk.NOTE_OFF:
                self.voices[track].release()
                return
            self.voices[track].trigger(note, inst)
            self._preview_off[track] = TICKS_PER_BEAT // 2

    def toggle_mute(self, track: int) -> None:
        self.mutes ^= {track}

    def muted(self, track: int) -> bool:
        return track in self.mutes or (self.solo is not None and self.solo != track)

    @property
    def playing(self) -> bool:
        return self.mode is not None

    def log(self, kind: str, track: int, note: int, inst) -> None:
        if self.events is not None:
            self.events.append((self.samples, kind, track, int(note), inst))

    # positions for the UI
    def position(self, track: int) -> tuple[int, int, int] | None:
        t = self.tracks[track]
        return (t.row, t.cstep, t.pstep) if t.running else None

    # song walking
    def _cell(self, t: Track) -> int | None:
        if self.mode == "chain" and t.i == self.focus[0]:
            return self.focus[1]
        return self.song.rows[t.row][t.i] if 0 <= t.row < tk.ROWS else None

    def _block_start(self, t: Track, row: int) -> int | None:
        r = min(row, tk.ROWS - 1)
        while r >= 0 and self.song.rows[r][t.i] is None:
            r -= 1
        if r < 0:
            return None
        while r > 0 and self.song.rows[r - 1][t.i] is not None:
            r -= 1
        return r

    def _chain_step(self, t: Track) -> tk.ChainStep | None:
        if self.mode == "phrase":
            return tk.ChainStep(self.focus[1], 0)
        chain = self.song.chains.get(self._cell(t))
        return chain[t.cstep] if chain else None

    def _advance(self, t: Track) -> None:
        t.pstep += 1
        if t.pstep < tk.STEPS and not t.hop:
            return
        t.pstep, t.hop = 0, False
        if self.mode == "phrase":
            return
        t.cstep += 1
        chain = self.song.chains.get(self._cell(t))
        if t.cstep < tk.STEPS and chain and chain[t.cstep].phrase is not None:
            return
        t.cstep = 0
        if self.mode == "chain":
            return
        t.row += 1
        if t.row >= tk.ROWS or self.song.rows[t.row][t.i] is None:
            start = self._block_start(t, t.row - 1)
            if start is None:
                t.running = False
                return
            t.row, t.looped = start, True

    def _step(self, t: Track) -> None:
        cs = self._chain_step(t)
        phrase = self.song.phrases.get(cs.phrase) if cs and cs.phrase is not None else None
        if phrase is None:
            return
        step, v = phrase[t.pstep], self.voices[t.i]
        tr = tk.signed(cs.transpose)
        if step.cmd == "H":
            t.hop = True
        elif step.cmd == "T" and step.val:
            self.bpm = step.val
        if step.note is not None:
            if step.note == tk.NOTE_OFF:
                v.release()
            elif step.cmd == "D" and step.val:
                v.delay, v.pending = step.val, (step.note + tr, step.inst)
            else:
                v.trigger(step.note + tr, step.inst, slide=step.val if step.cmd == "L" else 0)
        if step.cmd and step.cmd not in ("D", "H", "T", "L"):
            v.command(step.cmd, step.val)

    def _tick(self) -> None:
        for t in self.tracks:
            if not t.running:
                continue
            if t.ticks_left <= 0:
                if not t.fresh:
                    self._advance(t)
                    if not t.running:
                        continue
                t.fresh = False
                self._step(t)
                groove = self.song.groove or [6]
                t.ticks_left = max(1, groove[t.groove_i % len(groove)])
                t.groove_i += 1
            t.ticks_left -= 1
        for track, left in list(self._preview_off.items()):
            if left <= 0:
                self.voices[track].release()
                del self._preview_off[track]
            else:
                self._preview_off[track] = left - 1
        for v in self.voices:
            v.tick()

    def render(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, 2), np.float64)
        with self.lock:
            pos = 0
            while pos < frames:
                if self.tick_left <= 0:
                    self._tick()
                    self.tick_left += self.sr * 60 / (max(20, self.bpm) * TICKS_PER_BEAT)
                n = min(frames - pos, max(1, int(math.ceil(self.tick_left))))
                for v in self.voices:
                    v.render(n, out[pos:])
                pos += n
                self.tick_left -= n
                self.samples += n
        return np.tanh(out * MASTER).astype(np.float32)         # soft limit: a full mix rounds off, no hard clip


# --- live output ----------------------------------------------------------------------------------

class Output:
    """Plays an Engine: sounddevice if PortAudio is there, else an `aplay` pipe, else nothing."""

    def __init__(self, engine: Engine, block: int = 512, after=None):
        self.engine, self.block, self.after = engine, block, after   # after(): runs after each block
        self.how = "none"
        self._stream = self._proc = self._thread = None
        self._run = False

    def start(self) -> str:
        try:
            import sounddevice as sd
            self._stream = sd.OutputStream(samplerate=self.engine.sr, channels=2, dtype="float32",
                                           blocksize=self.block, callback=self._callback, latency="low")
            self._stream.start()
            self.how = "portaudio"
            return self.how
        except Exception:
            self._stream = None
        for cmd in (["aplay", "-q", "-t", "raw", "-f", "FLOAT_LE", "-c", "2", "-r", str(self.engine.sr),
                     "--buffer-time=60000", "-"],):
            try:
                self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                continue
            self._run = True
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()
            self.how = "aplay"
            return self.how
        return self.how

    def _callback(self, outdata, frames, _time, status) -> None:
        if status.output_underflow:
            self.engine.dropouts += 1
        try:
            outdata[:] = self.engine.render(frames)
            if self.after:
                self.after()
        except Exception:
            outdata.fill(0)

    def _pump(self) -> None:
        while self._run and self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.write(self.engine.render(self.block).astype("<f4").tobytes())
                if self.after:
                    self.after()
            except (BrokenPipeError, OSError, ValueError):
                break

    def stop(self) -> None:
        self._run = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            self._proc.terminate()


# --- the audio behind the UI ----------------------------------------------------------------------
#
# The synth must not share a Python process with Textual: while the screen redraws, the GIL keeps
# the audio callback waiting past its deadline (measured 2026-11-18: 10 % of blocks underran with
# the tracker redrawing, 0 % alone). AudioProcess runs Engine + Output in a process of its own;
# the UI sends commands and the song, and reads positions and levels from shared memory.
# LocalAudio is the same interface in-process (tests, and a fallback if a process can't start).

MODES = {None: 0, "song": 1, "chain": 2, "phrase": 3}
_HEAD, _PER_TRACK = 6, 4                      # state: playing, mode, focus track, focus id, bpm, dropouts; per track


def _publish(e: Engine, state) -> None:
    state[0], state[1] = float(e.playing), MODES[e.mode]
    state[2], state[3], state[4] = e.focus[0], e.focus[1] if e.focus[1] is not None else -1, e.bpm
    state[5] = e.dropouts
    for t in range(tk.MAX_TRACKS):
        tr, base = e.tracks[t], _HEAD + t * _PER_TRACK
        state[base:base + 3] = (tr.row, tr.cstep, tr.pstep) if tr.running else (-1, -1, -1)
        state[base + 3] = e.voices[t].peak


def _apply(e: Engine, cmd: tuple) -> None:
    name, args = cmd[0], cmd[1:]
    if name == "song":
        bpm = e.bpm
        e.song = tk.from_json(args[0])
        e.bpm = bpm if e.playing else e.song.bpm
    elif name in ("play_song", "play_chain", "play_phrase", "stop", "preview", "toggle_mute"):
        getattr(e, name)(*args)
    elif name == "solo":
        e.solo = args[0]
    elif name == "bpm":
        e.bpm = args[0]
    elif name == "midi":
        e.midi.close()
        e.midi = Midi(args[0])


def _audio_main(cmds, status, state, song_json: dict, sr: int, port: str, block: int) -> None:
    null = os.open(os.devnull, os.O_WRONLY)              # ALSA/PortAudio chatter must not draw on the TUI
    os.dup2(null, 1)
    os.dup2(null, 2)
    e = Engine(tk.from_json(song_json), sr, Midi(port))
    out = Output(e, block, after=lambda: _publish(e, state))
    status.put((out.start(), e.midi.port if e.midi.fd is not None else ""))
    while True:
        cmd = cmds.get()
        if cmd[0] == "quit":
            break
        with e.lock:
            try:
                _apply(e, cmd)
            except Exception:
                pass
        if not e.playing:
            _publish(e, state)
    out.stop()
    e.midi.close()


class _AudioState:
    """What the UI reads back: the same attributes for both kinds."""
    mutes: set
    solo: int | None

    def _read(self, i: int) -> float:
        raise NotImplementedError

    @property
    def playing(self) -> bool:
        return bool(self._read(0))

    @property
    def mode(self) -> str | None:
        return {v: k for k, v in MODES.items()}[int(self._read(1))]

    @property
    def focus(self) -> tuple[int, int]:
        return int(self._read(2)), int(self._read(3))

    @property
    def bpm(self) -> int:
        return int(self._read(4))

    @property
    def dropouts(self) -> int:
        return int(self._read(5))

    def position(self, t: int) -> tuple[int, int, int] | None:
        base = _HEAD + t * _PER_TRACK
        row = int(self._read(base))
        return None if row < 0 else (row, int(self._read(base + 1)), int(self._read(base + 2)))

    def peak(self, t: int) -> float:
        return self._read(_HEAD + t * _PER_TRACK + 3)

    def toggle_mute(self, t: int) -> None:
        self.mutes ^= {t}
        self.send("toggle_mute", t)

    def set_solo(self, t: int | None) -> None:
        self.solo = t
        self.send("solo", t)

    def set_song(self, song: tk.Song) -> None:
        data = tk.to_json(song)
        key = json.dumps(data, sort_keys=True)
        if key != getattr(self, "_sent", None):
            self._sent = key
            self.send("song", data)

    def play_song(self, row: int) -> None: self.send("play_song", row)
    def play_chain(self, t: int, c: int) -> None: self.send("play_chain", t, c)
    def play_phrase(self, t: int, p: int) -> None: self.send("play_phrase", t, p)
    def stop(self) -> None: self.send("stop")
    def preview(self, t: int, note: int, inst: int | None) -> None: self.send("preview", t, note, inst)
    def set_bpm(self, bpm: int) -> None: self.send("bpm", bpm)
    def set_midi(self, port: str) -> None: self.send("midi", port)


class AudioProcess(_AudioState):
    def __init__(self, song: tk.Song, sr: int = 44100, block: int = 512):
        import multiprocessing as mp
        import multiprocessing.resource_tracker as tracker
        import sys
        # Locks and queues need multiprocessing's resource tracker, which is started with our stderr's
        # file descriptor. Under Textual sys.stderr has none ("bad value(s) in fds_to_keep"), so give
        # it /dev/null for that moment.
        saved, sys.stderr = sys.stderr, open(os.devnull, "w")
        try:
            tracker.ensure_running()
        finally:
            sys.stderr.close()
            sys.stderr = saved
        ctx = mp.get_context("spawn")                   # never fork Textual's state
        self.cmds, status = ctx.Queue(), ctx.Queue()
        self.state = ctx.Array("d", _HEAD + tk.MAX_TRACKS * _PER_TRACK, lock=False)
        self.mutes, self.solo = set(), None
        data = tk.to_json(song)
        self._sent = json.dumps(data, sort_keys=True)
        self.proc = ctx.Process(target=_audio_main, args=(self.cmds, status, self.state, data, sr,
                                                          song.midi_port, block), daemon=True, name="mabel-tracker-audio")
        self.proc.start()
        self.how, self.midi_port = "none", ""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.proc.is_alive():       # never wait on a dead child
            try:
                self.how, self.midi_port = status.get(timeout=0.1)
                break
            except Exception:
                continue
        if not self.proc.is_alive():
            raise RuntimeError("the audio process didn't start")

    def _read(self, i: int) -> float:
        return self.state[i]

    @property
    def alive(self) -> bool:
        return self.proc.is_alive()

    def send(self, *cmd) -> None:
        if self.proc.is_alive():
            self.cmds.put(cmd)

    def close(self) -> None:
        self.send("quit")
        self.proc.join(2)
        if self.proc.is_alive():
            self.proc.terminate()


class LocalAudio(_AudioState):
    """The same interface in-process: for tests (output=False) and as a fallback."""

    def __init__(self, song: tk.Song, sr: int = 44100, output: bool = True, midi: bool = True):
        self.engine = Engine(tk.from_json(tk.to_json(song)), sr, Midi(song.midi_port if midi else None))
        self.state = [0.0] * (_HEAD + tk.MAX_TRACKS * _PER_TRACK)
        self.mutes, self.solo = set(), None
        self._sent = json.dumps(tk.to_json(song), sort_keys=True)
        self.out = Output(self.engine, after=lambda: _publish(self.engine, self.state)) if output else None
        self.how = self.out.start() if self.out else "test"
        self.midi_port = self.engine.midi.port if self.engine.midi.fd is not None else ""
        self.alive = True

    def _read(self, i: int) -> float:
        return self.state[i]

    def send(self, *cmd) -> None:
        with self.engine.lock:
            _apply(self.engine, cmd)
        _publish(self.engine, self.state)

    def render(self, frames: int):
        audio = self.engine.render(frames)
        _publish(self.engine, self.state)
        return audio

    def close(self) -> None:
        if self.out:
            self.out.stop()
        self.engine.midi.close()
        self.alive = False


# --- offline ----------------------------------------------------------------------------------------

def _run_offline(song: tk.Song, sr: int, max_seconds: float, events: bool):
    import copy as _copy
    e = Engine(_copy.deepcopy(song), sr)
    e.events = [] if events else None
    e.play_song(0)
    chunks, limit = [], int(max_seconds * sr)
    while e.samples < limit:
        chunks.append(e.render(1024))
        if any(t.looped for t in e.tracks) or not any(t.running for t in e.tracks):
            break
    e.stop_all()
    for _ in range(sr // 1024):                   # a second for release tails
        chunks.append(e.render(1024))
    return e, np.concatenate(chunks) if chunks else np.zeros((0, 2), np.float32)


def export_wav(song: tk.Song, path, sr: int = 44100, max_seconds: float = 600) -> float:
    """The song once through (until a track loops) as 16-bit stereo. Returns seconds."""
    _, audio = _run_offline(song, sr, max_seconds, events=False)
    with wavefile.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((audio * 32767).astype("<i2").tobytes())
    return len(audio) / sr


def export_midi(song: tk.Song, path, sr: int = 44100, max_seconds: float = 600) -> int:
    """The song once through as a type-1 .mid, one MIDI track per tracker track. Returns notes."""
    e, _ = _run_offline(song, sr, max_seconds, events=True)
    ppq = 480
    per_sample = song.bpm / 60 * ppq / sr
    tracks: dict[int, list[tuple[int, bytes]]] = {}
    held: dict[int, tuple[int, int]] = {}
    notes = 0
    for samples, kind, track, note, inst in e.events or []:
        ch = (max(1, min(16, inst.midi_channel)) if inst and (inst.kind == "midi" or inst.out != "synth")
              else (track + 1 if track + 1 != 10 else 11)) - 1
        when = int(samples * per_sample)
        if kind != "on" and track not in held:
            continue
        evs = tracks.setdefault(track, [])
        if track in held:
            pch, pnote = held.pop(track)
            evs.append((when, bytes([0x80 | pch, pnote & 127, 0])))
        if kind == "on":
            evs.append((when, bytes([0x90 | ch, note & 127, max(1, (inst.volume if inst else 192) // 2)])))
            held[track] = (ch, note)
            notes += 1
    end = int(e.samples * per_sample)
    for track, (ch, note) in held.items():
        tracks[track].append((end, bytes([0x80 | ch, note & 127, 0])))

    def vlq(n: int) -> bytes:
        out = [n & 0x7F]
        while n > 0x7F:
            n >>= 7
            out.append((n & 0x7F) | 0x80)
        return bytes(reversed(out))

    def chunk(evs: list[tuple[int, bytes]]) -> bytes:
        body, last = b"", 0
        for when, data in sorted(evs, key=lambda x: x[0]):
            body += vlq(when - last) + data
            last = when
        body += b"\x00\xff\x2f\x00"
        return b"MTrk" + struct.pack(">I", len(body)) + body
    tempo = int(60_000_000 / song.bpm)
    meta = [(0, b"\xff\x51\x03" + tempo.to_bytes(3, "big")), (0, b"\xff\x03" + vlq(len(song.title.encode()))
                                                                + song.title.encode())]
    body = chunk(meta) + b"".join(chunk(evs) for _, evs in sorted(tracks.items()))
    with open(path, "wb") as f:
        f.write(b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks) + 1, ppq) + body)
    return notes

"""Composing music (just for fun).

Every couple of days, when she'd otherwise just be idle, Mabel is offered the
chance to compose a short piece. She can say no. If she wants to, she says what
it's about, then writes it as a score in a small text notation (below), which
is turned into a standard MIDI file in ``journal/music/`` with a journal note
next to it (her words, the score). Anthon plays it on one of his hardware
synths straight from the PC: the player writes to the synth's raw ALSA MIDI
port (``/dev/snd/midiC*D*``), so there are no extra dependencies.

The notation, one part per ``[name]`` header::

    title: Corridor Lights
    tempo: 84
    meter: 4/4
    about: walking somewhere at night with the lights humming

    [lead] program=80
    | A4:1 C5:1 E5:2 | D5:1.5 C5:0.5 A4:2 |
    { | F4:1 A4:1 C5:2 | E4+G4:4 | }x2

    [drums]
    { | kick:1 hat:0.5 hat:0.5 snare:1 hat:1 | }x4

A note is ``PITCH:BEATS`` (C4 is middle C, ``#``/``b`` for sharps/flats), a
chord joins pitches with ``+``, ``r:BEATS`` rests, an optional ``@VEL`` (1-127)
sets loudness, ``{ ... }xN`` repeats, ``|`` is only for her eyes, ``//`` comments.
``program`` is a General MIDI instrument number; ``[drums]`` is the GM drum
channel and takes drum names (kick, snare, hat, ...) or pitches. One optional
``[voice]`` part goes to MIDI channel 1, which on Anthon's NSX-39 (Pocket Miku)
is the singing voice; instruments use channels 2-16 so they never land there.

Model calls are plain, no tools. Settings: ``music_enabled`` (default on),
``music_midi_port`` (part of the synth's name; default: the first hardware port),
``music_tell_enabled`` (default on). After a piece she may choose to text Anthon
about it; that is only offered when his outreach limits would allow a message,
and it counts toward them like any other outreach.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
MUSIC_DIR = ("journal", "music")
EVERY = timedelta(days=2)
MIN_IDLE_MINUTES = 20
CHOICE_TOKENS = 2000
WRITE_TOKENS = 16000                 # her private thinking counts against this too
MAX_PARTS = 8
MAX_SECONDS = 300
MIN_BEATS = 8
MAX_EVENTS = 20000
PPQ = 480
MAX_HISTORY = 200

_running = False

# ── the notation ────────────────────────────────────────────────────────

DRUMS = {"kick": 36, "bd": 36, "rim": 37, "snare": 38, "sd": 38, "clap": 39, "hat": 42, "hh": 42, "closedhat": 42,
         "pedalhat": 44, "openhat": 46, "oh": 46, "lowtom": 45, "midtom": 47, "hightom": 50, "tom": 47,
         "crash": 49, "ride": 51, "ridebell": 53, "tambourine": 54, "cowbell": 56, "shaker": 70, "woodblock": 76}

INSTRUMENTS = ("0 acoustic piano, 4 electric piano, 6 harpsichord, 8 celesta, 11 vibraphone, 12 marimba, "
               "14 tubular bells, 19 church organ, 21 accordion, 24 nylon guitar, 26 jazz guitar, 29 overdriven guitar, "
               "32 acoustic bass, 33 finger bass, 38 synth bass, 40 violin, 42 cello, 45 pizzicato strings, 46 harp, "
               "48 string ensemble, 52 choir aahs, 56 trumpet, 57 trombone, 60 french horn, 65 alto sax, 68 oboe, "
               "71 clarinet, 73 flute, 75 pan flute, 79 ocarina, 80 square lead, 81 saw lead, 88 new age pad, "
               "89 warm pad, 91 choir pad, 92 bowed pad, 95 sweep pad, 98 crystal, 99 atmosphere, 101 goblins, "
               "102 echoes, 108 kalimba, 112 tinkle bell")

# MIDI channel 1 is the singing voice on his NSX-39 (Pocket Miku); other synths just play it as a piano.
VOICE_NAMES = ("voice", "miku", "vocal", "vocals", "singer")

_NOTE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d)$")
_SEMITONE = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


class ScoreError(ValueError):
    pass


@dataclass
class Part:
    name: str
    program: int = 0
    drums: bool = False
    voice: bool = False
    notes: list[tuple[Fraction, Fraction, tuple[int, ...], int]] = field(default_factory=list)  # start, beats, pitches, vel
    beats: Fraction = Fraction(0)


@dataclass
class Score:
    title: str
    tempo: int
    meter: tuple[int, int]
    about: str
    parts: list[Part]

    @property
    def beats(self) -> Fraction:
        return max((p.beats for p in self.parts), default=Fraction(0))

    @property
    def seconds(self) -> float:
        return float(self.beats) * 60 / self.tempo


def pitch(token: str, *, drums: bool = False) -> int:
    token = token.strip()
    if drums and token.casefold() in DRUMS:
        return DRUMS[token.casefold()]
    if token.isdigit():
        number = int(token)
    else:
        found = _NOTE.match(token)
        if not found:
            raise ScoreError(f"“{token}” isn't a note (like C4, F#3, Bb5" + (" or a drum name" if drums else "") + ")")
        letter, accidental, octave = found.groups()
        number = (int(octave) + 1) * 12 + _SEMITONE[letter.casefold()] + {"#": 1, "b": -1, "": 0}[accidental]
    if not 0 <= number <= 127:
        raise ScoreError(f"“{token}” is outside the MIDI range")
    return number


def _beats(text: str) -> Fraction:
    try:
        value = Fraction(text.strip())
    except (ValueError, ZeroDivisionError):
        raise ScoreError(f"“{text}” isn't a length in beats (like 1, 0.5, 1/3, 1.5)") from None
    if value <= 0 or value > 64:
        raise ScoreError(f"a length of {text} beats doesn't work")
    return value


def _expand(body: str) -> str:
    """``{ ... }xN`` repeats, innermost first."""
    pattern = re.compile(r"\{([^{}]*)\}\s*[x×]\s*(\d+)")
    for _ in range(20):
        new = pattern.sub(lambda m: " ".join([m.group(1)] * min(int(m.group(2)), 64)), body)
        if new == body:
            break
        body = new
    if "{" in body or "}" in body:
        raise ScoreError("a { ... } repeat isn't closed or has no xN after it")
    return body


def _strip_fences(text: str) -> str:
    text = re.sub(r"(?s)<think>.*?</think>", "", str(text or ""))
    fenced = re.findall(r"```[a-zA-Z]*\n(.*?)```", text, flags=re.S)
    return max(fenced, key=len) if fenced else text


def parse(text: str) -> Score:
    text = _strip_fences(text)
    head: dict[str, str] = {}
    parts: list[Part] = []
    bodies: list[list[str]] = []
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        header = re.match(r"^\[([^\]]+)\]\s*(.*)$", line)
        if header:
            name, rest = header.group(1).strip(), header.group(2)
            drums = name.casefold() in ("drums", "drum", "percussion", "kit") or "drums" in rest.casefold()
            voice = name.casefold() in VOICE_NAMES
            program = re.search(r"program\s*=\s*(\d+)", rest)
            prog = int(program.group(1)) if program else 0
            if not 0 <= prog <= 127:
                raise ScoreError(f"[{name}]: program must be 0-127")
            if voice and any(p.voice for p in parts):
                raise ScoreError("only one [voice] part")
            parts.append(Part(name[:40], prog, drums, voice and not drums))
            bodies.append([])
            continue
        meta = re.match(r"^(title|tempo|meter|about|key)\s*:\s*(.*)$", line, flags=re.I)
        if meta and not parts:
            head[meta.group(1).casefold()] = meta.group(2).strip()
            continue
        if not parts:
            if re.search(r"[A-Ga-g][#b]?-?\d\s*:", line):
                raise ScoreError("notes came before any [part] header")
            continue                                   # stray prose before the score
        bodies[-1].append(line)
    if not parts:
        raise ScoreError("no parts: each part starts with a header like [piano] program=0")
    if len(parts) > MAX_PARTS:
        raise ScoreError(f"{len(parts)} parts is too many (at most {MAX_PARTS})")
    try:
        tempo = int(float(head.get("tempo", "90").split()[0]))
    except ValueError:
        raise ScoreError("tempo should be a number of beats per minute") from None
    if not 30 <= tempo <= 260:
        raise ScoreError("tempo should be between 30 and 260")
    meter = re.match(r"^(\d+)\s*/\s*(\d+)$", head.get("meter", "4/4"))
    num, den = (int(meter.group(1)), int(meter.group(2))) if meter else (4, 4)
    if den not in (1, 2, 4, 8, 16) or not 1 <= num <= 32:
        num, den = 4, 4
    events = 0
    for part, lines in zip(parts, bodies):
        now = Fraction(0)
        for token in _expand(" ".join(lines)).replace("|", " ").split():
            if ":" not in token:
                raise ScoreError(f"[{part.name}]: “{token}” has no length (write it like D4:1)")
            what, _, rest = token.partition(":")
            length, _, vel = rest.partition("@")
            beats = _beats(length)
            if what.casefold() not in ("r", "rest", "-"):
                velocity = int(vel) if vel.isdigit() else (96 if part.drums else 84)
                pitches = tuple(pitch(p, drums=part.drums) for p in what.split("+") if p)
                if not pitches:
                    raise ScoreError(f"[{part.name}]: “{token}” has no pitch")
                part.notes.append((now, beats, pitches, max(1, min(127, velocity))))
                events += len(pitches)
            now += beats
        part.beats = now
        if events > MAX_EVENTS:
            raise ScoreError("too many notes")
    parts = [p for p in parts if p.notes]
    if not parts:
        raise ScoreError("there are parts but no notes")
    score = Score(" ".join(head.get("title", "").split())[:80] or "Untitled", tempo, (num, den),
                  " ".join(head.get("about", "").split())[:300], parts)
    if score.beats < MIN_BEATS:
        raise ScoreError(f"only {float(score.beats):g} beats long; make it at least {MIN_BEATS}")
    if score.seconds > MAX_SECONDS:
        raise ScoreError(f"{score.seconds / 60:.1f} minutes is too long (at most {MAX_SECONDS // 60})")
    return score


# ── standard MIDI files ─────────────────────────────────────────────────

def _vlq(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def _track(events: list[tuple[int, int, bytes]]) -> bytes:
    """events: (tick, order, message); note-offs sort before note-ons at the same tick."""
    data, last = bytearray(), 0
    for tick, _order, message in sorted(events, key=lambda e: (e[0], e[1])):
        data += _vlq(tick - last) + message
        last = tick
    data += b"\x00\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(data)) + bytes(data)


def _text(kind: int, text: str) -> bytes:
    raw = text.encode("utf-8")[:120]
    return bytes([0xFF, kind]) + _vlq(len(raw)) + raw


def to_midi(score: Score) -> bytes:
    micros = round(60_000_000 / score.tempo)
    num, den = score.meter
    conductor = [(0, 0, _text(0x03, score.title)),
                 (0, 0, b"\xff\x51\x03" + micros.to_bytes(3, "big")),
                 (0, 0, bytes([0xFF, 0x58, 0x04, num, den.bit_length() - 1, 24, 8]))]
    tracks = [_track(conductor)]
    channels = iter(c for c in range(1, 16) if c != 9)       # channel 1 belongs to the voice (NSX-39: Miku)
    for part in score.parts:
        channel = 9 if part.drums else 0 if part.voice else next(channels, 15)
        events = [(0, 0, _text(0x03, part.name))]
        if not part.drums and not part.voice:
            events.append((0, 1, bytes([0xC0 | channel, part.program])))
        for start, beats, pitches, velocity in part.notes:
            on = round(start * PPQ)
            off = max(on + 1, round((start + beats) * PPQ) - (0 if part.drums else 6))  # a hair of air between notes
            for p in pitches:
                events.append((on, 3, bytes([0x90 | channel, p, velocity])))
                events.append((off, 2, bytes([0x80 | channel, p, 0])))
        tracks.append(_track(events))
    return b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), PPQ) + b"".join(tracks)


def read_midi(data: bytes) -> list[tuple[float, bytes]]:
    """A MIDI file as (seconds, channel message) pairs, merged over tracks with the tempo map applied."""
    if data[:4] != b"MThd":
        raise ValueError("not a MIDI file")
    length, _fmt, count, division = struct.unpack(">IHHH", data[4:14])
    if division & 0x8000:
        raise ValueError("SMPTE-timed MIDI files aren't supported")
    pos, raw = 8 + length, []                   # (tick, track, seq, message or ("tempo", us))
    for track in range(count):
        if data[pos:pos + 4] != b"MTrk":
            break
        size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        chunk, pos = data[pos + 8:pos + 8 + size], pos + 8 + size
        i, tick, status, seq = 0, 0, 0, 0
        while i < len(chunk):
            delta = 0
            while True:
                byte = chunk[i]; i += 1
                delta = (delta << 7) | (byte & 0x7F)
                if not byte & 0x80:
                    break
            tick += delta
            if chunk[i] & 0x80:
                status = chunk[i]; i += 1
            if status == 0xFF:
                kind = chunk[i]; i += 1
                n = 0
                while True:
                    byte = chunk[i]; i += 1
                    n = (n << 7) | (byte & 0x7F)
                    if not byte & 0x80:
                        break
                if kind == 0x51:
                    raw.append((tick, track, seq, ("tempo", int.from_bytes(chunk[i:i + 3], "big"))))
                i += n
                if kind == 0x2F:
                    break
            elif status in (0xF0, 0xF7):
                n = 0
                while True:
                    byte = chunk[i]; i += 1
                    n = (n << 7) | (byte & 0x7F)
                    if not byte & 0x80:
                        break
                i += n
            else:
                size_ = 1 if status & 0xF0 in (0xC0, 0xD0) else 2
                raw.append((tick, track, seq, bytes([status]) + chunk[i:i + size_]))
                i += size_
            seq += 1
    raw.sort(key=lambda e: (e[0], e[1], e[2]))
    out, tempo, last_tick, seconds = [], 500_000, 0, 0.0
    for tick, _t, _s, message in raw:
        seconds += (tick - last_tick) * tempo / 1_000_000 / division
        last_tick = tick
        if isinstance(message, tuple):
            tempo = message[1]
        else:
            out.append((seconds, message))
    return out


# ── playing it on his synth ─────────────────────────────────────────────

def ports(root: Path = Path("/dev/snd"), proc: Path = Path("/proc/asound")) -> list[dict]:
    """Raw MIDI ports of connected hardware, e.g. a USB synth: [{"path", "name"}]."""
    out = []
    for path in sorted(root.glob("midiC*D*")):
        found = re.match(r"midiC(\d+)D(\d+)$", path.name)
        if not found:
            continue
        card, device = found.groups()
        name = ""
        for source in (proc / f"card{card}" / f"midi{device}", proc / f"card{card}" / "id"):
            try:
                name = source.read_text(encoding="utf-8").splitlines()[0].strip()
                break
            except (OSError, IndexError):
                continue
        out.append({"path": str(path), "name": name or path.name})
    return out


def pick_port(wanted: str | None = None, available: list[dict] | None = None) -> dict | None:
    available = ports() if available is None else available
    wanted = (wanted if wanted is not None else str(_setting("music_midi_port", "") or "")).casefold().strip()
    if wanted:
        return next((p for p in available if wanted in p["name"].casefold() or wanted == p["path"].casefold()), None)
    return available[0] if available else None


class Player:
    """One piece at a time, on a background thread; ``stop()`` silences the synth."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.now_playing: dict | None = None

    def playing(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def play(self, midi_path: Path, port: dict) -> dict:
        events = read_midi(Path(midi_path).read_bytes())
        self.stop()
        fd = os.open(port["path"], os.O_WRONLY)
        self._stop.clear()
        self.now_playing = {"file": Path(midi_path).name, "port": port["name"],
                            "seconds": round(events[-1][0] if events else 0, 1), "started": time.time()}
        self._thread = threading.Thread(target=self._run, args=(fd, events), name="mabel-midi", daemon=True)
        self._thread.start()
        return self.now_playing

    def _run(self, fd: int, events: list[tuple[float, bytes]]) -> None:
        start = time.monotonic()
        try:
            for at, message in events:
                if self._stop.wait(max(0.0, at - (time.monotonic() - start))):
                    break
                os.write(fd, message)
        except OSError:
            logger.warning("[music] the synth stopped taking notes", exc_info=True)
        finally:
            try:
                for channel in range(16):                       # all notes off, sustain off
                    os.write(fd, bytes([0xB0 | channel, 123, 0, 0xB0 | channel, 64, 0]))
            except OSError:
                pass
            os.close(fd)
            self.now_playing = None

    def stop(self) -> None:
        if self.playing():
            self._stop.set()
            self._thread.join(timeout=2)


player = Player()


# ── state ───────────────────────────────────────────────────────────────

def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def running() -> bool:
    return _running


def _state_path(data_dir: Path) -> Path:
    return data_dir / "persona" / "music.json"


def load_state(data_dir: Path) -> dict:
    try:
        state = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data_dir: Path, state: dict) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["history"] = (state.get("history") or [])[-MAX_HISTORY:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def pieces(mabel_vault: Path = _REPO) -> list[dict]:
    """Her compositions, newest first."""
    out = []
    for note in sorted(mabel_vault.joinpath(*MUSIC_DIR).glob("*.md"), reverse=True):
        try:
            text = note.read_text(encoding="utf-8")
            meta = yaml.safe_load(text.split("\n---\n", 1)[0].removeprefix("---\n")) if text.startswith("---\n") else {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(meta, dict) and meta.get("midi") and note.with_name(meta["midi"]).exists():
            out.append({"title": meta.get("title") or note.stem, "about": meta.get("about") or "",
                        "written": meta.get("written"), "midi": meta["midi"], "note": note.name,
                        "seconds": meta.get("seconds")})
    return out


def midi_path(name: str, mabel_vault: Path = _REPO) -> Path:
    """One of her pieces by file name, never anything outside journal/music."""
    folder = mabel_vault.joinpath(*MUSIC_DIR).resolve()
    path = (folder / Path(str(name)).name).resolve()
    if path.parent != folder or path.suffix.lower() not in (".mid", ".midi") or not path.is_file():
        raise FileNotFoundError(name)
    return path


# ── the activity ────────────────────────────────────────────────────────

def _identity() -> str:
    try:
        from src.persona import composer
        library = composer().modules.load()
        return (library.core.body if library.core else "") or "You are Mabel."
    except Exception:
        return "You are Mabel."


async def _ask(messages: list[dict], owner: str, max_tokens: int) -> str:
    from src import model_roles
    return await model_roles.call("default", messages, owner=owner, max_tokens=max_tokens, temperature=0.9, timeout=600)


def _sparks(data_dir: Path) -> list[str]:
    try:
        from . import recent
        return [line["text"] for line in recent.load(data_dir)[-3:] if line.get("text")]
    except Exception:
        return []


def _choice_prompt(label: str, earlier: list[dict], sparks: list[str]) -> str:
    before = "\n".join(f"- “{p['title']}”" + (f": {p['about']}" if p.get("about") else "") for p in earlier[:4])
    lately = "\n".join(f"- {s}" for s in sparks)
    return f"""It's {label} and you have a quiet moment to yourself. You could compose a short piece of music, if you feel like it. It would be saved as a MIDI file, and Anthon can play it on one of his synths.
{f"{chr(10)}Pieces you've written before:{chr(10)}{before}{chr(10)}" if before else ""}{f"{chr(10)}What's been on your mind lately:{chr(10)}{lately}{chr(10)}" if lately else ""}
Do you want to? If yes, reply with a line or two about what it would be: a mood, a picture, a feeling, anything that pulls at you. It doesn't have to be about any of the above. If you'd rather not right now, start your reply with: not now

Either way, if you want to, add a line starting with "Why:" saying why you're choosing as you do. You don't have to."""


def _compose_prompt(label: str, idea: str) -> str:
    return f"""It's {label}. You decided to compose a piece. What you had in mind:

{idea}

Write it as a score in this notation (it becomes a MIDI file exactly as written):

title: <a title>
tempo: <beats per minute>
meter: <like 4/4, 3/4 or 6/8>
about: <one line on what it is>

[<part name>] program=<General MIDI instrument number>
<notes>

[drums]
<drum hits>

Notes are PITCH:BEATS, e.g. D4:1 (C4 is middle C, F#3, Bb5), one beat = one quarter note, so 0.5 is an eighth and 1/3 a triplet. A chord joins pitches with +, like D3+F3+A3:2. A rest is r:BEATS. Add @VELOCITY (1-127) for loudness, like E5:1@110. Wrap a passage in {{ ... }}x4 to repeat it. Bar lines | are only for you. Each part runs from the start on its own, so keep every part's bars adding up to the meter. Lines starting with // are comments.
One more instrument: Anthon's synth is a Pocket Miku (NSX-39), and a part named [voice] (no program) is played by its little Vocaloid voice, singing wordless syllables. It works like any other instrument, at most one such part, best within about C4 to C6.
Drums use kick, snare, hat, openhat, clap, rim, lowtom, midtom, hightom, crash, ride, shaker, tambourine, cowbell (e.g. kick:1 hat:0.5 hat:0.5).
Some instruments: {INSTRUMENTS}.

One to eight parts, about one to three minutes. Write what you actually want to hear, not a demonstration. Give the score in a single ```score code block, then after it, a few lines for your own journal about the piece.

If you want, end with a part starting with "Why:" about why you made it the way you did: the instruments, the tempo, where it goes, what you left out, or why this piece today at all. Only if you feel like saying it."""


def _repair_prompt(error: str) -> str:
    return f"""That score didn't go through: {error}. Please fix it and send the whole score again in one ```score code block, then your few journal lines (and your "Why:" part, if you want one)."""


def _tell_prompt(title: str) -> str:
    return f"""You've finished “{title}”. If you'd like to, you could tell Anthon about it: a text from you, in your own voice, however long or short. It reaches his phone and opens a chat with you, and he can play the piece on his synth from your timeline.

This is entirely up to you. Keeping it to yourself for now is just as good; he'll find it in your journal either way. If you'd rather not, reply with just: no

If you want, add a line starting with "Why:" about why you're telling him or not."""


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:40]
    return slug or "untitled"


_WHY = re.compile(r"^[ \t>*_#-]*why\b[^:\n]{0,40}:[*_]*[ \t]*", flags=re.I | re.M)


def split_why(text: str) -> tuple[str, str]:
    """Her words and, if she chose to give one, her "Why:" part (empty if she didn't)."""
    found = _WHY.search(text or "")
    if not found:
        return (text or "").strip(), ""
    return text[:found.start()].strip(), text[found.end():].strip()[:2000]


def _journal_lines(reply: str) -> str:
    text = re.sub(r"(?s)<think>.*?</think>", "", str(reply or ""))
    after = text.rsplit("```", 1)[-1] if text.count("```") >= 2 else ""
    return after.strip()[:3000]


def _tell_gates(now: datetime) -> list[str]:
    """Anthon's own outreach limits (src/initiative/outreach.py) decide whether she may text him at all."""
    from src.initiative import outreach
    return outreach.gates(outreach.default_store().load(), now=now,
                          enabled=bool(_setting("initiative_enabled", True)),
                          paused=bool(_setting("initiative_paused", False)), model_free=True,
                          has_phone=bool(outreach.home_assistant()))


async def offer_to_tell(*, title: str, note_text: str, note_ref: str, owner: str, ask, system: dict,
                        now: datetime) -> dict:
    """She may tell him about the piece, or not. Only offered when his outreach limits allow a message."""
    from src.initiative import outreach
    if not _setting("music_tell_enabled", True):
        return {"told": "not_offered", "why": "switched off"}
    blocked = _tell_gates(now)
    if blocked:
        return {"told": "not_offered", "why": "; ".join(blocked)[:300]}
    reply = await ask([system, {"role": "user", "content": _tell_prompt(title)}], owner, CHOICE_TOKENS)
    message, why = split_why(re.sub(r"(?s)<think>.*?</think>", "", str(reply or "")).strip())
    message = message.strip().strip('"“”').strip()
    if len(message) < 12 or re.fullmatch(r"[\W_]*(no|nope|not now|no thanks?)[\W_]*", message, flags=re.I):
        return {"told": "kept_to_herself", **({"why": why} if why else {})}
    message = message[:outreach.MESSAGE_LIMIT]
    outreach_id = f"out-{uuid.uuid4().hex[:10]}"
    session_id = outreach.create_chat(owner, f"🎵 {title}", message, {
        "outreach_id": outreach_id, "outreach_source": "music",
        "background_tool_result": {"job_id": outreach_id, "query": title, "report": note_text[:12000],
                                   "sources": [], "rounds": 0, "error": False}})
    link = outreach.chat_link(session_id)
    preview = message if len(message) <= outreach.PUSH_LIMIT else \
        message[:outreach.PUSH_LIMIT].rsplit(" ", 1)[0] + "…"
    delivered = outreach.send_notification(preview, link, outreach_id)
    store = outreach.default_store()
    data = store.load()
    data["sent"].append({"id": outreach_id, "at": now.isoformat(), "key": f"music:{note_ref}", "source": "music",
                         "subject": title, "message": message, "why": why, "session_id": session_id,
                         "status": "sent" if delivered else "in_app_only", "answered_at": None})
    store.save(data)
    outreach._ledger("outreach.sent" if delivered else "outreach.held", outreach_id,
                     decision="sent" if delivered else "in_app_only", code="music")
    return {"told": "sent" if delivered else "in_app_only", "message": message, "session_id": session_id,
            **({"why": why} if why else {})}


async def compose(*, owner: str = "tovakai", data_dir: Path | None = None, mabel_vault: Path = _REPO,
                  ask=None, now: datetime | None = None) -> dict:
    """Offer her the chance to compose; if she takes it, write the score, the .mid and her note."""
    global _running
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    ask = ask or _ask
    state = load_state(data_dir)
    history = state.get("history") or []
    _running = True
    try:
        label = now.astimezone().strftime("%A %-d %B, %H:%M")
        identity = _identity()
        system = {"role": "system", "content": identity}
        reply = await ask([system, {"role": "user", "content": _choice_prompt(label, pieces(mabel_vault),
                                                                              _sparks(data_dir))}],
                          owner, CHOICE_TOKENS)
        idea = re.sub(r"(?s)<think>.*?</think>", "", str(reply or "")).strip()
        state["last"] = now.isoformat(timespec="seconds")
        idea, why_choose = split_why(idea)
        declined = re.match(r"^[\W_]*not now\b[\W_]*(.*)$", idea, flags=re.I | re.S)
        if not idea or declined or len(idea) < 4:
            why_choose = why_choose or (declined.group(1).strip() if declined else "")
            state["history"] = history + [{"at": state["last"], "outcome": "not_now",
                                           **({"why": why_choose[:500]} if why_choose else {})}]
            _save_state(data_dir, state)
            return {"status": "not_now", **({"why": why_choose[:500]} if why_choose else {})}
        idea = idea[:1200]
        messages = [system, {"role": "user", "content": _compose_prompt(label, idea)}]
        reply = await ask(messages, owner, WRITE_TOKENS)
        try:
            score = parse(reply)
        except ScoreError as exc:
            logger.info("[music] her score didn't parse (%s); asking her to fix it", exc)
            messages += [{"role": "assistant", "content": re.sub(r"(?s)<think>.*?</think>", "", str(reply or ""))},
                         {"role": "user", "content": _repair_prompt(str(exc))}]
            reply = await ask(messages, owner, WRITE_TOKENS)
            try:
                score = parse(reply)
            except ScoreError as exc2:
                state["history"] = history + [{"at": state["last"], "outcome": "failed", "idea": idea[:200],
                                               "error": str(exc2)[:200]}]
                _save_state(data_dir, state)
                return {"status": "failed", "error": str(exc2)}
        folder = mabel_vault.joinpath(*MUSIC_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        stem, n = f"{now.astimezone():%Y-%m-%d}-{_slug(score.title)}", 2
        while (folder / f"{stem}.mid").exists() or (folder / f"{stem}.md").exists():
            stem, n = f"{now.astimezone():%Y-%m-%d}-{_slug(score.title)}-{n}", n + 1
        (folder / f"{stem}.mid").write_bytes(to_midi(score))
        notes, why_made = split_why(_journal_lines(reply))
        front = {"type": "composition", "date": now.astimezone().strftime("%Y-%m-%d"),
                 "written": now.isoformat(timespec="seconds"), "title": score.title, "about": score.about,
                 "tempo": score.tempo, "meter": f"{score.meter[0]}/{score.meter[1]}",
                 "parts": [f"{p.name} ({'drums' if p.drums else f'program {p.program}'})" for p in score.parts],
                 "seconds": round(score.seconds), "midi": f"{stem}.mid", "provenance": "experienced"}
        body = (f"# {score.title}\n\n" + (f"*{score.about}*\n\n" if score.about else "")
                + f"What I had in mind: {idea}\n\n"
                + (f"Why I chose to compose: {why_choose}\n\n" if why_choose else "")
                + (f"{notes}\n\n" if notes else "")
                + (f"## Why I made it this way\n\n{why_made}\n\n" if why_made else "")
                + f"## Score\n\n```score\n{_strip_fences(reply).strip()}\n```\n")
        (folder / f"{stem}.md").write_text("---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True,
                                                                       width=1000) + "---\n" + body, encoding="utf-8")
        rel = str((folder / f"{stem}.md").relative_to(mabel_vault))
        try:
            told = await offer_to_tell(title=score.title, note_text=body, note_ref=rel, owner=owner, ask=ask,
                                       system=system, now=now)
        except Exception as exc:
            logger.warning("[music] telling Anthon didn't work", exc_info=True)
            told = {"told": "failed", "why": str(exc)[:200]}
        if told.get("message") or told["told"] == "kept_to_herself":
            section = (f"## What I told Anthon\n\n{told['message']}\n" if told.get("message")
                       else "*I kept this one to myself for now.*\n")
            if told.get("why"):
                section += f"\nWhy: {told['why']}\n"
            note = folder / f"{stem}.md"
            note.write_text(note.read_text(encoding="utf-8").replace("## Score\n", section + "\n## Score\n", 1),
                            encoding="utf-8")
        state["history"] = history + [{"at": state["last"], "outcome": "written", "title": score.title, "note": rel,
                                       "told": told["told"], **({"told_why": told["why"][:300]} if told.get("why") else {})}]
        _save_state(data_dir, state)
        from . import recent
        await recent.remember("composing music", "experienced",
                              f"I composed “{score.title}”. {score.about}\n\n{idea}\n\n{notes}\n\n{why_made}",
                              fallback=f"I wrote a little piece of music, “{score.title}”.", owner=owner, ref=rel,
                              now=now, data_dir=data_dir)
        return {"status": "written", "path": rel, "midi": f"{stem}.mid", "title": score.title,
                "seconds": round(score.seconds), "told": told["told"]}
    finally:
        _running = False


def gate(sig, *, data_dir: Path, now: datetime, research_running: bool) -> list[str]:
    """Reasons not to offer her composing right now (empty = go)."""
    reasons = []
    if not _setting("music_enabled", True):
        reasons.append("composing is switched off")
    if _running:
        reasons.append("already composing")
    last = load_state(data_dir).get("last")
    if last and now - datetime.fromisoformat(last) < EVERY:
        reasons.append("composed (or was offered to) recently")
    if research_running:
        reasons.append("a research session is running")
    if sig.chat_active is not False:
        reasons.append("a chat is running (or unknown)")
    if sig.idle != "idle" or (sig.idle_minutes or 0) < MIN_IDLE_MINUTES:
        reasons.append(f"Anthon hasn't been away {MIN_IDLE_MINUTES} min")
    if sig.gpu != "free":
        reasons.append(f"GPU not free ({sig.gpu_reason or sig.gpu})")
    if not sig.strata:
        reasons.append("her model isn't up")
    return reasons


async def maybe_compose(sig, *, owner: str = "tovakai", data_dir: Path | None = None,
                        now: datetime | None = None, research_running: bool = False) -> dict | None:
    """Called from the heartbeat: offer composing if the gate is clear."""
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    if gate(sig, data_dir=data_dir, now=now, research_running=research_running):
        return None
    result = await compose(owner=owner, data_dir=data_dir, now=now)
    logger.info("[music] %s %s", result.get("status"), result.get("path") or result.get("error") or "")
    return result


if __name__ == "__main__":
    import argparse
    import asyncio
    cli = argparse.ArgumentParser(prog="python -m src.persona.music")
    cli.add_argument("--ports", action="store_true", help="list connected MIDI synths")
    cli.add_argument("--list", action="store_true", help="list her pieces")
    cli.add_argument("--play", nargs="?", const="latest", help="play a piece (file name; default: the latest)")
    cli.add_argument("--port", help="part of the synth's name")
    cli.add_argument("--render", help="turn a score text file into <file>.mid (no model)")
    cli.add_argument("--compose", action="store_true", help="offer her composing now (uses her model)")
    args = cli.parse_args()
    if args.ports:
        for p in ports():
            print(f"{p['path']}  {p['name']}")
    elif args.list:
        for p in pieces():
            print(f"{p['midi']}  {p['seconds']}s  {p['title']}")
    elif args.render:
        source = Path(args.render)
        source.with_suffix(".mid").write_bytes(to_midi(parse(source.read_text(encoding="utf-8"))))
        print(source.with_suffix(".mid"))
    elif args.play:
        name = pieces()[0]["midi"] if args.play == "latest" and pieces() else args.play
        port = pick_port(args.port)
        if not port:
            raise SystemExit("no MIDI synth connected (python -m src.persona.music --ports)")
        path = Path(name) if Path(name).is_file() else midi_path(name)
        print("playing", player.play(path, port))
        try:
            while player.playing():
                time.sleep(0.2)
        except KeyboardInterrupt:
            player.stop()
    elif args.compose:
        print(asyncio.run(compose()))
    else:
        cli.print_help()

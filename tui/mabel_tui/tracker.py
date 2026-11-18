"""TRACKER's model: an LSDJ-style song (song → chains → phrases), instruments, tables and waves.

Our own code. LSDJ's structure, without its menu-diving: trackerview.py shows song, chain,
phrase, instrument and table at once. rtrack (github.com/shakfu/rtrack, GPL-3.0) inspired
some ideas (effect set, vim keys), none of its code.

- Song: rows × tracks of chain ids (None = empty: the track loops back to the top of its block).
- Chain: 16 steps of (phrase id, transpose). A chain ends at its first empty step.
- Phrase: 16 steps of (note, instrument, command, value). note -1 = note off.
- Instrument: a chip voice (pulse, tri, saw, wave, noise) or MIDI out, with an ADSR in ticks,
  an optional table, and where it plays (synth, midi, both).
- Table: 16 rows run one per tick under every note of its instrument: (volume, transpose, cmd, val).
- Wave: 32 samples of 0-15 for the "wave" kind.

Values are shown in hex like LSDJ. Saved as .mtk (JSON).
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

STEPS = 16
ROWS = 256
MAX_ID = 0xFF
MAX_TRACKS = 8
NOTE_OFF = -1
KINDS = ["pulse", "tri", "saw", "wave", "noise", "midi"]
OUTS = ["synth", "midi", "both"]
NAMES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]

# Commands (phrase and table): letter → what the value means. Kept small and LSDJ-flavoured.
COMMANDS = {
    "A": "start table XX on this track",
    "C": "chord/arpeggio: x and y semitones, cycled per tick",
    "D": "delay the note XX ticks",
    "E": "volume XX (00-FF) for the playing note",
    "H": "hop: end the phrase after this step",
    "K": "kill the note after XX ticks",
    "L": "slide (portamento) to the note, speed XX",
    "O": "pan: 00 left, 80 centre, FF right",
    "P": "pitch bend XX per tick (80 = none, below = down)",
    "R": "retrigger every XX ticks",
    "T": "tempo XX bpm",
    "V": "vibrato: x speed, y depth",
    "W": "pulse width / wave number XX",
}


def note_name(n: int | None) -> str:
    if n is None:
        return "---"
    if n == NOTE_OFF:
        return "OFF"
    return f"{NAMES[n % 12]}{n // 12 - 1}" if n >= 12 else f"{NAMES[n % 12]}0"


def hx(v: int | None, width: int = 2) -> str:
    return "-" * width if v is None else f"{v & (16 ** width - 1):0{width}X}"


def signed(v: int) -> int:
    """A byte as LSDJ shows transpose: 00-7F up, 80-FF down (FF = -1)."""
    v &= 0xFF
    return v - 256 if v >= 0x80 else v


@dataclass
class Step:
    note: int | None = None
    inst: int | None = None
    cmd: str = ""
    val: int = 0


@dataclass
class ChainStep:
    phrase: int | None = None
    transpose: int = 0                 # a byte; signed() reads it


@dataclass
class Instrument:
    name: str = "inst"
    kind: str = "pulse"
    volume: int = 0xC0
    attack: int = 0                    # ticks to full volume
    decay: int = 8                     # ticks from full to sustain
    sustain: int = 0x80                # level while held
    release: int = 6                   # ticks to silence after note off
    width: int = 0x80                  # pulse width (80 = square) / wave number for "wave"
    transpose: int = 0                 # byte, signed
    table: int | None = None
    pan: int = 0x80
    out: str = "synth"                 # synth | midi | both
    midi_channel: int = 2              # 1-16 (on Pocket Miku 1 is Miku's voice)
    midi_program: int | None = None


@dataclass
class TableRow:
    vol: int | None = None             # None = leave the volume
    transpose: int = 0                 # byte, signed, added to the note
    cmd: str = ""
    val: int = 0


@dataclass
class Song:
    title: str = "untitled"
    tracks: int = 4
    bpm: int = 128
    groove: list[int] = field(default_factory=lambda: [6, 6])     # ticks per step, cycled
    rows: list[list[int | None]] = field(default_factory=list)
    chains: dict[int, list[ChainStep]] = field(default_factory=dict)
    phrases: dict[int, list[Step]] = field(default_factory=dict)
    instruments: dict[int, Instrument] = field(default_factory=dict)
    tables: dict[int, list[TableRow]] = field(default_factory=dict)
    waves: dict[int, list[int]] = field(default_factory=dict)
    midi_port: str = ""                # a rawmidi device (/dev/snd/midiC2D0); "" = find one
    path: Path | None = None

    def __post_init__(self):
        if not self.rows:
            self.rows = [[None] * MAX_TRACKS for _ in range(ROWS)]
        if not self.instruments:
            self.instruments = default_instruments()
        if not self.waves:
            self.waves = {0: [round(7.5 + 7.5 * math.sin(i / 32 * 2 * math.pi)) for i in range(32)]}

    # --- lookup that creates on demand (editing an empty id makes it) ---------------------------

    def chain(self, i: int) -> list[ChainStep]:
        return self.chains.setdefault(i, [ChainStep() for _ in range(STEPS)])

    def phrase(self, i: int) -> list[Step]:
        return self.phrases.setdefault(i, [Step() for _ in range(STEPS)])

    def instrument(self, i: int) -> Instrument:
        return self.instruments.setdefault(i, Instrument(name=f"inst{i:02X}"))

    def table(self, i: int) -> list[TableRow]:
        return self.tables.setdefault(i, [TableRow() for _ in range(STEPS)])

    def wave(self, i: int) -> list[int]:
        return self.waves.setdefault(i, [8] * 32)

    # --- ids --------------------------------------------------------------------------------------

    @staticmethod
    def _used(d: dict, empty) -> set[int]:
        return {k for k, v in d.items() if not empty(v)}

    def used_chains(self) -> set[int]:
        return self._used(self.chains, lambda c: all(s.phrase is None for s in c)) | \
            {c for row in self.rows for c in row if c is not None}

    def used_phrases(self) -> set[int]:
        return self._used(self.phrases, lambda p: all(s.note is None and not s.cmd for s in p)) | \
            {s.phrase for c in self.chains.values() for s in c if s.phrase is not None}

    def free_id(self, used: set[int]) -> int | None:
        return next((i for i in range(MAX_ID + 1) if i not in used), None)

    def new_chain(self) -> int | None:
        i = self.free_id(self.used_chains())
        if i is not None:
            self.chains[i] = [ChainStep() for _ in range(STEPS)]
        return i

    def new_phrase(self) -> int | None:
        i = self.free_id(self.used_phrases())
        if i is not None:
            self.phrases[i] = [Step() for _ in range(STEPS)]
        return i

    def clone_chain(self, i: int) -> int | None:
        j = self.new_chain()
        if j is not None:
            self.chains[j] = copy.deepcopy(self.chain(i))
        return j

    def clone_phrase(self, i: int) -> int | None:
        j = self.new_phrase()
        if j is not None:
            self.phrases[j] = copy.deepcopy(self.phrase(i))
        return j

    def last_row(self) -> int:
        """The last song row that has anything in it (-1 if none)."""
        return max((r for r, row in enumerate(self.rows) if any(c is not None for c in row[:self.tracks])),
                   default=-1)


def default_instruments() -> dict[int, Instrument]:
    return {
        0: Instrument("lead", "pulse", 0xC0, 0, 10, 0x90, 8, 0x80),
        1: Instrument("thin", "pulse", 0xA0, 0, 6, 0x60, 6, 0x30),
        2: Instrument("bass", "tri", 0xF0, 0, 4, 0xE0, 4),
        3: Instrument("wave", "wave", 0xB0, 1, 12, 0x80, 10, 0),
        4: Instrument("kick", "tri", 0xFF, 0, 5, 0x00, 1, table=0),
        5: Instrument("hat", "noise", 0x70, 0, 2, 0x00, 1),
        6: Instrument("snare", "noise", 0xC0, 0, 6, 0x00, 2),
        7: Instrument("miku", "midi", 0xC0, out="midi", midi_channel=2, midi_program=None),
    }


def demo() -> Song:
    """A tiny starter so there's something to press play on."""
    song = Song(title="first light")
    song.tables[0] = [TableRow(None, t) for t in (24, 12, 5, 0)] + [TableRow() for _ in range(12)]
    lead = song.phrase(0)
    for i, n in enumerate([69, None, 72, None, 76, None, 72, None, 74, None, 71, None, 67, None, None, NOTE_OFF]):
        if n is not None:
            lead[i] = Step(n, 0 if n != NOTE_OFF else None)
    bass = song.phrase(1)
    for i in range(0, 16, 4):
        bass[i] = Step(45 if i < 8 else 43, 2)
    drums = song.phrase(2)
    for i in range(16):
        if i % 8 == 0:
            drums[i] = Step(36, 4)
        elif i % 8 == 4:
            drums[i] = Step(60, 6)
        elif i % 2 == 0:
            drums[i] = Step(84, 5)
    for c, p in ((0, 0), (1, 1), (2, 2)):
        song.chain(c)[0] = ChainStep(p)
        song.chain(c)[1] = ChainStep(p, 5 if c < 2 else 0)
    song.rows[0][:3] = [0, 1, 2]
    return song


# --- files ------------------------------------------------------------------------------------

def to_json(song: Song) -> dict:
    def dense(d, empty):
        return {f"{k:02X}": v for k, v in sorted(d.items()) if not empty(v)}
    return {
        "version": 1, "title": song.title, "tracks": song.tracks, "bpm": song.bpm, "groove": song.groove,
        "midi_port": song.midi_port,
        "rows": {f"{r:02X}": row[:song.tracks] for r, row in enumerate(song.rows) if any(c is not None for c in row)},
        "chains": dense({k: [asdict(s) for s in v] for k, v in song.chains.items()},
                        lambda c: all(s["phrase"] is None for s in c)),
        "phrases": dense({k: [asdict(s) for s in v] for k, v in song.phrases.items()},
                         lambda p: all(s["note"] is None and not s["cmd"] for s in p)),
        "instruments": {f"{k:02X}": asdict(v) for k, v in sorted(song.instruments.items())},
        "tables": dense({k: [asdict(s) for s in v] for k, v in song.tables.items()},
                        lambda t: all(r["vol"] is None and not r["transpose"] and not r["cmd"] for r in t)),
        "waves": {f"{k:02X}": v for k, v in sorted(song.waves.items())},
    }


def from_json(data: dict, path: Path | None = None) -> Song:
    def ids(d):
        return {int(k, 16): v for k, v in (d or {}).items()}

    def fill(items, cls):
        items = [cls(**{k: v for k, v in s.items() if k in cls.__dataclass_fields__}) for s in items][:STEPS]
        return items + [cls() for _ in range(STEPS - len(items))]
    song = Song(title=data.get("title", "untitled"), tracks=max(1, min(MAX_TRACKS, int(data.get("tracks", 4)))),
                bpm=int(data.get("bpm", 128)), groove=[int(g) for g in data.get("groove") or [6, 6]],
                midi_port=str(data.get("midi_port") or ""), path=path)
    for r, row in ids(data.get("rows")).items():
        if 0 <= r < ROWS:
            song.rows[r][:len(row)] = row[:MAX_TRACKS]
    song.chains = {k: fill(v, ChainStep) for k, v in ids(data.get("chains")).items()}
    song.phrases = {k: fill(v, Step) for k, v in ids(data.get("phrases")).items()}
    if data.get("instruments"):
        song.instruments = {k: Instrument(**{a: b for a, b in v.items() if a in Instrument.__dataclass_fields__})
                            for k, v in ids(data["instruments"]).items()}
    song.tables = {k: fill(v, TableRow) for k, v in ids(data.get("tables")).items()}
    if data.get("waves"):
        song.waves = {k: ([int(x) & 15 for x in v] + [8] * 32)[:32] for k, v in ids(data["waves"]).items()}
    return song


def save(song: Song, path: Path | None = None) -> Path:
    path = Path(path).expanduser() if path else song.path
    if path is None:
        raise ValueError("no file name yet: :w name.mtk")
    if not path.suffix:
        path = path.with_suffix(".mtk")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(to_json(song), separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    song.path = path
    return path


def load(path: Path) -> Song:
    path = Path(path).expanduser()
    return from_json(json.loads(path.read_text(encoding="utf-8")), path)

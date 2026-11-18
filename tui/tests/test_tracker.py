"""TRACKER's model and engine (mabel_tui/tracker.py, tracksynth.py), offline: no sound card."""
import wave

import numpy as np

from mabel_tui import tracker as tk
from mabel_tui import tracksynth as ts

SR = 8000


def _ticks_per_step(song):                 # samples in one 6-tick step
    return SR * 60 / (song.bpm * ts.TICKS_PER_BEAT) * 6


def test_names_and_bytes():
    assert tk.note_name(60) == "C-4" and tk.note_name(61) == "C#4" and tk.note_name(tk.NOTE_OFF) == "OFF"
    assert tk.signed(0xFF) == -1 and tk.signed(0x0C) == 12 and tk.hx(None) == "--" and tk.hx(10) == "0A"


def test_round_trip(tmp_path):
    song = tk.demo()
    song.instrument(9).kind = "saw"
    back = tk.load(tk.save(song, tmp_path / "s"))
    assert back.path.suffix == ".mtk" and tk.to_json(back) == tk.to_json(song)


def test_new_and_clone_ids():
    song = tk.demo()
    c = song.new_chain()
    assert c == 3, "0-2 are used by the demo"
    p = song.clone_phrase(0)
    assert p == 3 and song.phrases[3][0].note == 69 and song.phrases[3] is not song.phrases[0]


def test_the_demo_makes_sound_and_stops_cleanly():
    e = ts.Engine(tk.demo(), SR)
    e.play_song(0)
    audio = e.render(SR)
    assert audio.shape == (SR, 2) and np.abs(audio).max() > 0.05
    e.stop()
    e.render(SR)                           # release tails
    assert np.abs(e.render(1024)).max() < 1e-3


def test_song_walks_chain_then_loops_its_block():
    song = tk.Song(tracks=1)
    for p in (0, 1):
        song.phrase(p)[0] = tk.Step(60 + p, 0)
    song.chain(0)[0] = tk.ChainStep(0)
    song.chain(1)[0] = tk.ChainStep(1, 0x0C)             # +12
    song.rows[0][0], song.rows[1][0] = 0, 1
    e = ts.Engine(song, SR)
    e.events = []
    e.play_song(0)
    e.render(int(_ticks_per_step(song) * 16 * 3) + 10)    # three phrases' worth
    ons = [n for _, kind, _, n, _ in e.events if kind == "on"]
    assert ons[:3] == [60, 61 + 12, 60], "row 0, row 1 (transposed), then back to the top of the block"
    assert e.tracks[0].looped


def test_phrase_loop_and_commands():
    song = tk.Song(tracks=1)
    ph = song.phrase(0)
    ph[0] = tk.Step(60, 0, "K", 2)                        # killed after 2 ticks
    ph[1] = tk.Step(None, None, "H", 0)                   # hop: phrase ends after step 1
    e = ts.Engine(song, SR)
    e.events = []
    e.play_phrase(0, 0)
    e.render(int(_ticks_per_step(song) * 4))              # 4 steps = 2 loops of the hopped phrase
    kinds = [k for _, k, *_ in e.events]
    assert kinds.count("on") == 2 and "off" in kinds


def test_exports(tmp_path):
    song = tk.demo()
    seconds = ts.export_wav(song, tmp_path / "s.wav", sr=SR)
    with wave.open(str(tmp_path / "s.wav")) as w:
        assert w.getnchannels() == 2 and w.getframerate() == SR and w.getnframes() > SR
    assert 2 < seconds < 30
    notes = ts.export_midi(song, tmp_path / "s.mid", sr=SR)
    data = (tmp_path / "s.mid").read_bytes()
    assert data[:4] == b"MThd" and data.count(b"MTrk") == 4 and notes > 20


def test_midi_out_writes_raw_bytes(tmp_path):
    port = tmp_path / "fake-midi"
    port.write_bytes(b"")
    midi = ts.Midi(str(port))
    song = tk.Song(tracks=1)
    song.instruments[0] = tk.Instrument("miku", "midi", 0xC0, out="midi", midi_channel=2, midi_program=5)
    e = ts.Engine(song, SR, midi)
    e.preview(0, 64, 0)
    midi.close()
    sent = port.read_bytes()
    assert sent[:2] == bytes([0xC1, 5]) and sent[2:5] == bytes([0x91, 64, 0x60])

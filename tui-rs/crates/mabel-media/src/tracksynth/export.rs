//! Files without a sound card: the song rendered once through to WAV, or its notes to MIDI.

use super::*;

/// Play the song once through without a sound card (until a track loops, or `max_seconds`),
/// plus a second for release tails. With `events`, notes are logged for MIDI export.
pub(super) fn run_offline(song: &Song, sr: u32, max_seconds: f64, events: bool) -> (Engine, Vec<[f32; 2]>) {
    let mut e = Engine::new(song.clone(), sr, Midi::open(None));
    if events {
        e.events = Some(vec![]);
    }
    e.play_song(0);
    let limit = (max_seconds * sr as f64) as u64;
    let mut audio = vec![];
    while e.samples < limit {
        audio.extend(e.render(1024));
        if e.tracks.iter().any(|t| t.looped) || !e.tracks.iter().any(|t| t.running) {
            break;
        }
    }
    e.stop_all();
    for _ in 0..(sr as usize / 1024) {
        audio.extend(e.render(1024)); // a second for release tails
    }
    (e, audio)
}

/// The song once through (until a track loops) as 16-bit stereo. Returns seconds.
pub fn export_wav(song: &Song, path: &Path, sr: u32) -> Result<f64> {
    let (_, audio) = run_offline(song, sr, 600.0, false);
    let data_len = (audio.len() * 4) as u32;
    let mut out = Vec::with_capacity(44 + data_len as usize);
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + data_len).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes()); // PCM
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&sr.to_le_bytes());
    out.extend_from_slice(&(sr * 4).to_le_bytes());
    out.extend_from_slice(&4u16.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&data_len.to_le_bytes());
    for [l, r] in &audio {
        out.extend_from_slice(&((l * 32767.0) as i16).to_le_bytes());
        out.extend_from_slice(&((r * 32767.0) as i16).to_le_bytes());
    }
    std::fs::write(path, out)?;
    Ok(audio.len() as f64 / sr as f64)
}

/// A number in MIDI files' variable-length format (7 bits a byte).
fn vlq(mut n: u64) -> Vec<u8> {
    let mut out = vec![(n & 0x7F) as u8];
    while n > 0x7F {
        n >>= 7;
        out.push(((n & 0x7F) | 0x80) as u8);
    }
    out.reverse();
    out
}

/// One MIDI file track: events sorted by time, each with its delta time, then the end marker.
fn chunk(mut evs: Vec<(u64, Vec<u8>)>) -> Vec<u8> {
    evs.sort_by_key(|(w, _)| *w);
    let mut body = vec![];
    let mut last = 0;
    for (when, data) in evs {
        body.extend(vlq(when - last));
        body.extend(data);
        last = when;
    }
    body.extend([0x00, 0xFF, 0x2F, 0x00]);
    let mut out = b"MTrk".to_vec();
    out.extend((body.len() as u32).to_be_bytes());
    out.extend(body);
    out
}

/// The song once through as a type-1 .mid, one MIDI track per tracker track. Returns notes.
pub fn export_midi(song: &Song, path: &Path, sr: u32) -> Result<usize> {
    let (e, _) = run_offline(song, sr, 600.0, true);
    let ppq = 480.0;
    let per_sample = song.bpm as f64 / 60.0 * ppq / sr as f64;
    let mut tracks: std::collections::BTreeMap<usize, Vec<(u64, Vec<u8>)>> = Default::default();
    let mut held: HashMap<usize, (u8, i64)> = HashMap::new();
    let mut notes = 0;
    for (samples, on, track, note, inst) in e.events.clone().unwrap_or_default() {
        let midi_inst = inst.as_ref().is_some_and(|i| i.kind == "midi" || i.out != "synth");
        let ch = if midi_inst {
            inst.as_ref().map_or(1, |i| i.midi_channel.clamp(1, 16))
        } else if track + 1 != 10 {
            track as i64 + 1
        } else {
            11
        } - 1;
        let when = (samples as f64 * per_sample) as u64;
        if !on && !held.contains_key(&track) {
            continue;
        }
        let evs = tracks.entry(track).or_default();
        if let Some((pch, pnote)) = held.remove(&track) {
            evs.push((when, vec![0x80 | pch, (pnote & 127) as u8, 0]));
        }
        if on {
            let vel = (inst.as_ref().map_or(192, |i| i.volume) / 2).max(1) as u8;
            evs.push((when, vec![0x90 | ch as u8, (note & 127) as u8, vel]));
            held.insert(track, (ch as u8, note));
            notes += 1;
        }
    }
    let end = (e.samples as f64 * per_sample) as u64;
    for (track, (ch, note)) in held {
        tracks.entry(track).or_default().push((end, vec![0x80 | ch, (note & 127) as u8, 0]));
    }
    let tempo = (60_000_000 / song.bpm.max(1)) as u32;
    let title = song.title.as_bytes();
    let mut meta_title = vec![0xFF, 0x03];
    meta_title.extend(vlq(title.len() as u64));
    meta_title.extend(title);
    let meta = vec![(0, vec![0xFF, 0x51, 0x03, (tempo >> 16) as u8, (tempo >> 8) as u8, tempo as u8]), (0, meta_title)];
    let mut file = b"MThd".to_vec();
    file.extend(6u32.to_be_bytes());
    file.extend(1u16.to_be_bytes());
    file.extend(((tracks.len() + 1) as u16).to_be_bytes());
    file.extend((ppq as u16).to_be_bytes());
    file.extend(chunk(meta));
    for (_, evs) in tracks {
        file.extend(chunk(evs));
    }
    std::fs::write(path, file)?;
    Ok(notes)
}

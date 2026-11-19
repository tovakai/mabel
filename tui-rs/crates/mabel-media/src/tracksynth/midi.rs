//! MIDI out: straight to a rawmidi device file, no MIDI library.

use super::*;

/// The MIDI devices plugged in (/dev/snd/midiC*D*).
pub fn midi_ports() -> Vec<String> {
    let mut ports: Vec<String> = std::fs::read_dir("/dev/snd")
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path().to_string_lossy().into_owned())
        .filter(|p| p.rsplit('/').next().is_some_and(|n| n.starts_with("midiC")))
        .collect();
    ports.sort();
    ports
}

/// Writes raw MIDI bytes to a rawmidi device. Silent if there is none.
pub struct Midi {
    pub port: String,
    file: Option<std::fs::File>,
    programs: HashMap<i64, i64>,
}

impl Midi {
    /// `port`: a device path, "" = the first one found, None = no MIDI at all.
    /// Open MIDI output (see `port`); no device just means MIDI is silent.
    pub fn open(port: Option<&str>) -> Self {
        let port = match port {
            None => String::new(),
            Some("") => midi_ports().into_iter().next().unwrap_or_default(),
            Some(p) => p.to_string(),
        };
        let file = (!port.is_empty())
            .then(|| std::fs::OpenOptions::new().write(true).custom_flags(libc::O_NONBLOCK).open(&port).ok())
            .flatten();
        Midi { port, file, programs: HashMap::new() }
    }

    /// Is a device open?
    pub fn connected(&self) -> bool {
        self.file.is_some()
    }

    /// Write raw MIDI bytes (errors ignored: a device can vanish mid-song).
    pub(super) fn send(&mut self, data: &[u8]) {
        if let Some(f) = &mut self.file {
            let _ = f.write_all(data);
        }
    }

    /// Note on, on channel 1-16.
    pub(super) fn note_on(&mut self, ch: i64, note: i64, vel: i64) {
        self.send(&[0x90 | ((ch - 1) & 15) as u8, (note & 127) as u8, vel.clamp(1, 127) as u8]);
    }

    /// Note off.
    pub(super) fn note_off(&mut self, ch: i64, note: i64) {
        self.send(&[0x80 | ((ch - 1) & 15) as u8, (note & 127) as u8, 0]);
    }

    /// Program change (only sent when it actually changes).
    pub(super) fn program(&mut self, ch: i64, prog: i64) {
        if self.programs.get(&ch) != Some(&prog) {
            self.programs.insert(ch, prog);
            self.send(&[0xC0 | ((ch - 1) & 15) as u8, (prog & 127) as u8]);
        }
    }

    /// "All notes off" on every channel (stop, and before closing).
    pub fn all_off(&mut self) {
        for ch in 0..16u8 {
            self.send(&[0xB0 | ch, 123, 0]);
        }
    }
}

impl Drop for Midi {
    fn drop(&mut self) {
        self.all_off();
    }
}

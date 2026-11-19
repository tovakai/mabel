//! Live sound: a thread feeding samples to `aplay` (the deck) or `pw-cat` (PipeWire).

use super::*;

/// Anything Output can play: an Engine, or a mix of several (DreamTapes: music + sound effects).
pub trait Source: Send {
    fn sample_rate(&self) -> u32;
    /// `frames` stereo samples in -1..1.
    fn render(&mut self, frames: usize) -> Vec<[f32; 2]>;
}

impl Source for Engine {
    fn sample_rate(&self) -> u32 {
        self.sr
    }
    fn render(&mut self, frames: usize) -> Vec<[f32; 2]> {
        Engine::render(self, frames)
    }
}

/// Plays an Engine: a thread renders blocks and pipes them to `aplay` (the deck) or `pw-cat`
/// (PipeWire). The pipe is kept small, so a note you play is heard within a few blocks.
pub struct Output {
    pub how: String,
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl Output {
    /// Start playing a source on a thread, through `aplay` (the deck) or `pw-cat` (PipeWire),
    /// `block` samples at a time. Neither installed: silent, and `how` says "none".
    pub fn start<S: Source + 'static>(engine: Arc<Mutex<S>>, block: usize) -> Self {
        let sr = engine.lock().map(|e| e.sample_rate()).unwrap_or(44100);
        let players: [(&str, Vec<String>); 2] = [
            (
                "aplay",
                vec![
                    "-q".into(),
                    "-f".into(),
                    "S16_LE".into(),
                    "-r".into(),
                    sr.to_string(),
                    "-c".into(),
                    "2".into(),
                    "--buffer-time=80000".into(),
                    "-".into(),
                ],
            ),
            (
                "pw-cat",
                vec![
                    "--playback".into(),
                    // Raw samples on stdin: without it, pw-cat (PipeWire 1.6) reads "-" as a sound
                    // file and quits with "Format not recognised", so nothing played on the PC.
                    "--raw".into(),
                    "--format".into(),
                    "s16".into(),
                    "--rate".into(),
                    sr.to_string(),
                    "--channels".into(),
                    "2".into(),
                    "--latency".into(),
                    format!("{block}"),
                    "-".into(),
                ],
            ),
        ];
        let Some((name, args)) = players.into_iter().find(|(n, _)| crate::have(n)) else {
            return Output { how: "none".into(), stop: Arc::new(AtomicBool::new(true)), thread: None };
        };
        let child = std::process::Command::new(name)
            .args(&args)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
        let Ok(mut child) = child else {
            return Output { how: "none".into(), stop: Arc::new(AtomicBool::new(true)), thread: None };
        };
        let mut stdin = child.stdin.take().expect("piped");
        {
            use std::os::fd::AsRawFd;
            // A small pipe: the latency a note has is the pipe plus the player's buffer.
            // SAFETY: fcntl on our own pipe's file descriptor.
            unsafe {
                libc::fcntl(stdin.as_raw_fd(), libc::F_SETPIPE_SZ, 4096);
            }
        }
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let thread = std::thread::spawn(move || {
            let mut bytes = Vec::with_capacity(block * 4);
            while !flag.load(Ordering::Relaxed) {
                let audio = match engine.lock() {
                    Ok(mut e) => e.render(block),
                    Err(_) => break,
                };
                bytes.clear();
                for [l, r] in audio {
                    bytes.extend_from_slice(&((l * 32767.0) as i16).to_le_bytes());
                    bytes.extend_from_slice(&((r * 32767.0) as i16).to_le_bytes());
                }
                // Blocks while the player is full: that's what paces the engine.
                if stdin.write_all(&bytes).is_err() {
                    break;
                }
            }
            drop(stdin);
            let _ = child.kill();
            let _ = child.wait();
        });
        Output { how: name.to_string(), stop, thread: Some(thread) }
    }
}

impl Drop for Output {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

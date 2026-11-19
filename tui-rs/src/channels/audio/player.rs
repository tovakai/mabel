//! Local playback through mpv's JSON IPC. The queue lives here; mpv's playlist mirrors it.
//!
//! mpv starts on the first play. URLs and the token (Mabel's proxy wants a bearer header) go to
//! mpv over its IPC socket, never on its command line, so they don't show in the process list.
//! Playing a song tells the music source "now playing"; past half the song (or 4 minutes) it
//! scrobbles.

use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender, unbounded_channel};

use crate::channels::audio::music::Music;
use crate::os::channel::Msg;

/// Where a song plays from: a downloaded file, else the stream.
pub type Locate = Arc<dyn Fn(&str) -> String + Send + Sync>;

/// What's playing, as mpv last reported it (AUDIO, the status line and MPRIS read this).
#[derive(Default)]
pub struct State {
    /// The queue (Navidrome song JSON) and which song is playing.
    pub queue: Vec<Value>,
    pub index: Option<usize>,
    pub paused: bool,
    /// Seconds into the song, and its length (0 until mpv knows).
    pub position: f64,
    pub duration: f64,
    /// 0-100.
    pub volume: i64,
    /// New plays start with the chosen song, the rest shuffled.
    pub shuffle: bool,
    /// The last error (mpv missing, a song that won't play), shown once.
    pub error: String,
    /// Songs already scrobbled this time through (each counts once).
    scrobbled: HashSet<String>,
}

impl State {
    pub fn current(&self) -> Option<&Value> {
        self.index.and_then(|i| self.queue.get(i))
    }

    /// The length of the song on now (mpv's word, else the library's).
    /// The song's length: mpv's, else Navidrome's (mpv doesn't know a stream's length at first).
    pub fn length(&self) -> f64 {
        if self.duration > 0.0 {
            self.duration
        } else {
            self.current().and_then(|s| s["duration"].as_f64()).unwrap_or(0.0)
        }
    }
}

#[derive(Clone)]
pub struct Player {
    state: Arc<Mutex<State>>,
    music: Music,
    locate: Locate,
    /// Commands for mpv; None until it's started.
    mpv: Arc<Mutex<Option<UnboundedSender<Value>>>>,
    /// The app hears about changes (it redraws on any message).
    app: UnboundedSender<Msg>,
}

/// Tiny shuffle without a dependency: xorshift seeded from the clock.
/// Shuffle in place (a small random source; no extra crate needed).
fn shuffle<T>(items: &mut [T]) {
    let mut x =
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(1, |d| d.as_nanos() as u64) | 1;
    for i in (1..items.len()).rev() {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        items.swap(i, (x % (i as u64 + 1)) as usize);
    }
}

/// A song's id.
fn id(song: &Value) -> &str {
    song["id"].as_str().unwrap_or("")
}

impl Player {
    /// A player; mpv starts with the first song. `app` hears about changes (Msg "player").
    pub fn new(music: Music, locate: Locate, volume: i64, app: UnboundedSender<Msg>) -> Self {
        let state = State { volume, ..Default::default() };
        Player { state: Arc::new(Mutex::new(state)), music, locate, mpv: Arc::new(Mutex::new(None)), app }
    }

    pub fn available() -> bool {
        crate::channels::sys::sysinfo::have("mpv")
    }

    /// Read the state (the queue, what's on, the position…).
    /// Look at the state for a moment.
    pub fn with<R>(&self, f: impl FnOnce(&State) -> R) -> R {
        f(&self.state.lock().expect("player state"))
    }

    /// Change the state and tell the app.
    fn update(&self, f: impl FnOnce(&mut State)) {
        f(&mut self.state.lock().expect("player state"));
        self.changed();
    }

    fn changed(&self) {
        let _ = self.app.send(Msg::For("player", Box::new(())));
    }

    /// Take (and clear) the last error, for a toast.
    /// The last error, once (it's cleared).
    pub fn take_error(&self) -> Option<String> {
        let mut s = self.state.lock().expect("player state");
        (!s.error.is_empty()).then(|| std::mem::take(&mut s.error))
    }

    // ── mpv ──────────────────────────────────────────────────────────────

    fn cmd(&self, args: Value) {
        if let Some(tx) = self.mpv.lock().expect("mpv").as_ref() {
            let _ = tx.send(args);
        }
    }

    /// Start mpv if it isn't running. Commands sent meanwhile wait in the channel.
    /// Start mpv if it isn't running (false: mpv isn't installed).
    fn ensure(&self) -> bool {
        let mut slot = self.mpv.lock().expect("mpv");
        if slot.as_ref().is_some_and(|tx| !tx.is_closed()) {
            return true;
        }
        if !Self::available() {
            drop(slot);
            self.update(|s| s.error = "mpv isn't installed (sudo pacman -S mpv)".into());
            return false;
        }
        let (tx, rx) = unbounded_channel();
        *slot = Some(tx);
        drop(slot);
        let me = self.clone();
        tokio::spawn(async move {
            if let Err(e) = me.run_mpv(rx).await {
                *me.mpv.lock().expect("mpv") = None;
                me.update(|s| s.error = format!("mpv: {e}"));
            }
        });
        true
    }

    /// mpv's life: start it idle with a control socket, send it the queued commands, and turn
    /// its property changes into `State` until it exits.
    async fn run_mpv(&self, mut rx: UnboundedReceiver<Value>) -> anyhow::Result<()> {
        let dir = std::env::var_os("XDG_RUNTIME_DIR").map(PathBuf::from).unwrap_or_else(crate::os::config::cache_dir);
        std::fs::create_dir_all(&dir)?;
        let socket = dir.join(format!("mabel-os-mpv-{}.sock", std::process::id()));
        let _ = std::fs::remove_file(&socket);
        let volume = self.with(|s| s.volume);
        let mut child = tokio::process::Command::new("mpv");
        child
            .args(["--idle=yes", "--no-video", "--no-terminal", "--audio-display=no", "--force-window=no"])
            .arg(format!("--input-ipc-server={}", socket.display()))
            .arg(format!("--volume={volume}"))
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .kill_on_drop(true);
        // If MABEL-OS dies (killed, crashed, its terminal closed), the kernel stops mpv too:
        // no music left playing with nothing to stop it.
        // SAFETY: prctl is async-signal-safe and only touches the child being started.
        unsafe {
            child.pre_exec(|| {
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
                Ok(())
            });
        }
        let mut child = child.spawn()?;
        let mut stream = None;
        for _ in 0..50 {
            if let Ok(s) = UnixStream::connect(&socket).await {
                stream = Some(s);
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let stream = stream.ok_or_else(|| anyhow::anyhow!("couldn't talk to mpv"))?;
        let (read, mut write) = stream.into_split();
        let send = |v: Value| format!("{}\n", json!({ "command": v }));

        let mut first = vec![];
        if let Some(token) = self.music.bearer() {
            first.push(json!(["set_property", "http-header-fields", [format!("Authorization: Bearer {token}")]]));
        }
        for (i, prop) in ["pause", "time-pos", "duration", "playlist-pos", "idle-active"].iter().enumerate() {
            first.push(json!(["observe_property", i + 1, prop]));
        }
        for c in first {
            write.write_all(send(c).as_bytes()).await?;
        }

        let me = self.clone();
        let reader = tokio::spawn(async move {
            let mut lines = BufReader::new(read).lines();
            let mut last_tick = Instant::now() - Duration::from_secs(1);
            while let Ok(Some(line)) = lines.next_line().await {
                let Ok(msg) = serde_json::from_str::<Value>(&line) else { continue };
                match msg["event"].as_str() {
                    Some("property-change") => {
                        let name = msg["name"].as_str().unwrap_or("");
                        let quiet = name == "time-pos" && last_tick.elapsed() < Duration::from_millis(250);
                        me.property(name, &msg["data"], !quiet);
                        if name == "time-pos" && !quiet {
                            last_tick = Instant::now();
                        }
                    }
                    Some("end-file") if msg["reason"] == "error" => {
                        me.update(|s| s.error = "couldn't play that song".into())
                    }
                    _ => {}
                }
            }
        });
        while let Some(c) = rx.recv().await {
            if write.write_all(send(c).as_bytes()).await.is_err() {
                break;
            }
        }
        reader.abort();
        let _ = child.kill().await;
        let _ = std::fs::remove_file(&socket);
        Ok(())
    }

    /// One property change from mpv: pause, position (scrobbling a song at half its length or
    /// 4 minutes), duration, which song, idle. Tells Navidrome "now playing" on a new song.
    fn property(&self, name: &str, value: &Value, notify: bool) {
        let mut now_playing = None;
        let mut scrobble = None;
        {
            let mut s = self.state.lock().expect("player state");
            match name {
                "pause" => s.paused = value.as_bool().unwrap_or(false),
                "time-pos" => {
                    s.position = value.as_f64().unwrap_or(0.0);
                    let length = s.length();
                    if let Some(song) = s.current().map(|c| id(c).to_string()) {
                        let due = length > 0.0 && (s.position >= length / 2.0 || s.position >= 240.0);
                        if due && !s.scrobbled.contains(&song) {
                            s.scrobbled.insert(song.clone());
                            scrobble = Some(song);
                        }
                    }
                }
                "duration" => s.duration = value.as_f64().unwrap_or(0.0),
                "playlist-pos" => {
                    s.index = value.as_i64().and_then(|i| usize::try_from(i).ok());
                    s.position = 0.0;
                    s.duration = 0.0;
                    now_playing = s.current().map(|c| id(c).to_string());
                }
                "idle-active" if value.as_bool() == Some(true) => s.position = 0.0,
                _ => {}
            }
        }
        for (song, submission) in [(now_playing, false), (scrobble, true)] {
            if let Some(song) = song {
                let music = self.music.clone();
                tokio::spawn(async move {
                    let _ = music.music_write("scrobble", json!({"id": song, "submission": submission})).await;
                });
            }
        }
        if notify {
            self.changed();
        }
    }

    /// Give mpv a song: from disk if downloaded, else the stream (`how`: replace or append).
    fn load(&self, song: &Value, how: &str) {
        self.cmd(json!(["loadfile", (self.locate)(id(song)), how]));
    }

    // ── the queue ────────────────────────────────────────────────────────

    /// Play `songs` from `start` (shuffled after it, if shuffle is on), replacing the queue.
    pub fn play(&self, songs: Vec<Value>, start: usize) {
        if songs.is_empty() || !self.ensure() {
            return;
        }
        let mut songs = songs;
        let mut start = start.min(songs.len() - 1);
        if self.with(|s| s.shuffle) && songs.len() > 1 {
            let first = songs.remove(start);
            shuffle(&mut songs);
            songs.insert(0, first);
            start = 0;
        }
        self.cmd(json!(["playlist-clear"]));
        for (i, song) in songs.iter().enumerate() {
            self.load(song, if i == 0 { "replace" } else { "append" });
        }
        if start > 0 {
            self.cmd(json!(["set_property", "playlist-pos", start]));
        }
        self.cmd(json!(["set_property", "pause", false]));
        self.update(|s| {
            s.queue = songs;
            s.error.clear();
            s.scrobbled.clear();
        });
    }

    /// Add songs to the end of the queue.
    pub fn enqueue(&self, songs: Vec<Value>) {
        if self.with(|s| s.queue.is_empty()) {
            return self.play(songs, 0);
        }
        for song in &songs {
            self.load(song, "append");
        }
        self.update(|s| s.queue.extend(songs));
    }

    pub fn play_next(&self, songs: Vec<Value>) {
        if self.with(|s| s.queue.is_empty()) {
            return self.play(songs, 0);
        }
        let (at, len) = self.with(|s| (s.index.map_or(0, |i| i + 1), s.queue.len()));
        for (offset, song) in songs.iter().enumerate() {
            self.load(song, "append");
            self.cmd(json!(["playlist-move", len + offset, at + offset]));
        }
        self.update(|s| {
            for (offset, song) in songs.into_iter().enumerate() {
                s.queue.insert(at + offset, song);
            }
        });
    }

    /// Shuffle on: everything after the current song is shuffled now (and new plays start shuffled).
    /// Shuffle on or off (on: the rest of the queue is shuffled now).
    pub fn set_shuffle(&self, on: bool) {
        let (index, len) = self.with(|s| (s.index, s.queue.len()));
        let after = index.map_or(0, |i| i + 1);
        if on && after < len {
            let mut rest: Vec<Value> = self.with(|s| s.queue[after..].to_vec());
            shuffle(&mut rest);
            for i in (after..len).rev() {
                self.cmd(json!(["playlist-remove", i]));
            }
            for song in &rest {
                self.load(song, "append");
            }
            self.update(|s| {
                s.queue.truncate(after);
                s.queue.extend(rest);
            });
        }
        self.update(|s| s.shuffle = on);
    }

    /// Play the queue's song at `index`.
    pub fn jump(&self, index: usize) {
        if index < self.with(|s| s.queue.len()) {
            self.cmd(json!(["set_property", "playlist-pos", index]));
            self.cmd(json!(["set_property", "pause", false]));
        }
    }

    /// Pause or play.
    pub fn toggle(&self) {
        self.cmd(json!(["cycle", "pause"]));
    }

    /// Pause or play, as told.
    pub fn set_paused(&self, paused: bool) {
        self.cmd(json!(["set_property", "pause", paused]));
    }

    /// The next song.
    pub fn next(&self) {
        self.cmd(json!(["playlist-next"]));
    }

    /// Back to the start of the song, or (in its first 5 seconds) the song before.
    pub fn previous(&self) {
        if self.with(|s| s.position) > 5.0 {
            self.cmd(json!(["seek", 0, "absolute"]));
        } else {
            self.cmd(json!(["playlist-prev"]));
        }
    }

    /// Jump `seconds` forward (or back, negative).
    pub fn seek(&self, seconds: f64) {
        self.cmd(json!(["seek", seconds, "relative"]));
    }

    pub fn change_volume(&self, delta: i64) {
        let volume = self.with(|s| (s.volume + delta).clamp(0, 130));
        self.cmd(json!(["set_property", "volume", volume]));
        self.update(|s| s.volume = volume);
    }

    /// A control_music command from Mabel (the same one the web player gets).
    pub fn apply(&self, command: &Value) {
        let songs = command["songs"].as_array().cloned().unwrap_or_default();
        match command["op"].as_str() {
            Some("play") => self.play(songs, 0),
            Some("queue") => self.enqueue(songs),
            Some("play_next") => self.play_next(songs),
            Some("pause") => self.set_paused(true),
            Some("resume") => self.set_paused(false),
            Some("next") => self.next(),
            Some("previous") => self.previous(),
            _ => {}
        }
    }

    /// Quit mpv (MABEL-OS is closing).
    pub fn stop(&self) {
        self.cmd(json!(["quit"]));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shuffle_keeps_every_item() {
        let mut v: Vec<u32> = (0..50).collect();
        shuffle(&mut v);
        let mut sorted = v.clone();
        sorted.sort();
        assert_eq!(sorted, (0..50).collect::<Vec<_>>());
    }
}

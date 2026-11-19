//! MEDIA ▸ AUDIO: Navidrome (through Mabel's proxy, or directly), played locally by mpv.
//!
//! Left: the library (newest albums first; / searches; enter opens an album or artist, or plays
//! from that song; a appends, n plays next; backspace goes back; p playlists). Right: the deck
//! (cover, now playing, queue). space pause, [ ] or < > previous/next, ← → seek, + - volume,
//! z shuffle, R a random mix.

/// the desktop's media keys and widget
pub mod mpris;
/// where music comes from: Mabel's proxy or Navidrome
pub mod music;
/// downloaded music
pub mod offline;
/// mpv, playing the queue
pub mod player;

use std::time::Instant;

use crossterm::event::{KeyCode, KeyEvent, MouseButton, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Row, Table, TableState};
use serde_json::{Value, json};

use crate::channels::media::audio::music::Music;
use crate::os::channel::{Channel, Ctx, Outcome, View};
use crate::os::keys;

use crate::channels::media::audio::player::Player;
use crate::os::ui::{self, PanelState, bold};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::picture::Picture;
use crate::widgets::popup::{Answer, Ask, Pick};
use crate::widgets::wrap::width;

// AUDIO's screen is split by job; every piece adds to `impl Audio`.
/// keys and popups
mod controls;
/// drawing
mod draw;
/// screens, search, playlists, downloads
mod library;

/// The id messages and help use.
const ID: &str = "audio";
// The three sections, in the order `render` returns them.
const SEARCH: usize = 0;
const RESULTS: usize = 1;
const DECK: usize = 2;
/// The deck's queue shows this many songs before the one playing, and up to this many after.
const QUEUE_BEFORE: usize = 2;
const QUEUE_AFTER: usize = 40;

/// What a library row is.
#[derive(Clone, Copy, PartialEq, Debug)]
enum Kind {
    Song,
    Album,
    Artist,
    Playlist,
    /// A downloaded collection: {"key", "name", "artist", "kind", "have", "total"}.
    Offline,
}

/// A screen's rows: each one's kind and its data (Navidrome's JSON for it).
type Rows = Vec<(Kind, Value)>;

/// One screen of the library, kept for backspace.
#[derive(Clone)]
struct Screen {
    title: String,
    rows: Rows,
    /// The album or playlist on screen (for "download all of it").
    context: Option<(Kind, Value)>,
    /// The playlist on screen (for d: remove from it).
    playlist: Option<Value>,
    cursor: usize,
}

/// What to do with songs once they're fetched.
#[derive(Clone, Copy)]
enum Then {
    /// a: to the end of the queue.
    Append,
    /// n: right after the song playing.
    Next,
    /// A popup asks which playlist.
    ToPlaylist,
}

/// Background work reporting back.
enum Got {
    /// A library screen loaded (ignored if a newer one was asked for meanwhile).
    Screen {
        generation: u64,
        screen: Screen,
        remember: bool,
    },
    /// Songs fetched for a/n/playlist.
    Songs {
        then: Then,
        songs: Vec<Value>,
    },
    /// The playlists to pick from, for these song ids.
    Playlists {
        songs: Vec<String>,
        lists: Vec<Value>,
    },
    /// Play these songs, from the one with this id.
    Play(Vec<Value>, String),
    /// The cover art for the song playing.
    Cover {
        id: String,
        bytes: Option<Vec<u8>>,
    },
    /// The newest albums, or (Navidrome down) why not.
    Newest(Result<Screen, String>),
    /// Download progress for the library's bottom line ("" when done).
    Progress(String),
    /// A download finished: (songs got, songs in it), or why not.
    Downloaded {
        name: String,
        result: Result<(usize, usize), String>,
    },
    /// Something to say (a toast), or to say went wrong.
    Note(String),
    Failed(String),
}

/// A box over AUDIO that takes the keys while it's open.
enum Popup {
    /// Which playlist gets these songs.
    Pick(Pick, Vec<String>),
    /// The name of a new playlist with these songs.
    Name(Ask, Vec<String>),
}

/// A text field of Navidrome's JSON ("" if missing).
fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}

/// Seconds as m:ss.
fn time(seconds: f64) -> String {
    let t = seconds.max(0.0) as u64;
    format!("{}:{:02}", t / 60, t % 60)
}

/// `text` cut or padded to exactly `w` cells.
fn fit(text: &str, w: usize) -> String {
    let mut out = String::new();
    for c in text.chars() {
        if width(&out) + width(c.encode_utf8(&mut [0; 4])) > w {
            break;
        }
        out.push(c);
    }
    let pad = w.saturating_sub(width(&out));
    out + &" ".repeat(pad)
}

/// A list nested two levels down in an answer, like the songs in `album.song`.
fn sub(data: &Value, outer: &str, inner: &str) -> Vec<Value> {
    data[outer][inner].as_array().cloned().unwrap_or_default()
}

/// The songs inside one library row (fetching albums and playlists).
async fn songs_of(music: &Music, kind: Kind, item: &Value) -> Vec<Value> {
    match kind {
        Kind::Song => vec![item.clone()],
        Kind::Album => music
            .music("getAlbum", json!({"id": s(item, "id")}))
            .await
            .map(|d| sub(&d, "album", "song"))
            .unwrap_or_default(),
        Kind::Playlist => music
            .music("getPlaylist", json!({"id": s(item, "id")}))
            .await
            .map(|d| sub(&d, "playlist", "entry"))
            .unwrap_or_default(),
        Kind::Artist | Kind::Offline => vec![],
    }
}

/// The AUDIO screen. (Playing itself is `Player`, shared by the whole app: music keeps
/// playing on any channel.)
pub struct Audio {
    /// The library screen showing, and the ones before it (backspace).
    screen: Screen,
    back: Vec<Screen>,
    /// The results table's selection and scroll.
    table: TableState,
    /// The search field.
    search: Input,
    /// The deck's queue: cursor and first line shown.
    queue_cursor: usize,
    queue_offset: usize,
    /// The playing song's cover, and whose it is.
    cover: Option<Picture>,
    cover_id: Option<String>,
    popup: Option<Popup>,
    /// Counts library requests, so a slow old answer can't replace a newer screen.
    generation: u64,
    /// Where the three sections were drawn (mouse clicks use them).
    rects: Vec<Rect>,
    /// How many rows the results showed last time (PgUp/PgDn move by it).
    page: usize,
    /// The first load (newest albums) has been asked for.
    started: bool,
    /// Songs on this device (for the ↓ marks), refreshed after downloads.
    downloaded: std::collections::HashSet<String>,
    /// A download running: shown under the library.
    progress: String,
    /// d on a download: pressed once, waiting for the second press (key, when).
    remove_armed: Option<(String, Instant)>,
}

impl Audio {
    /// An empty library; `start` loads the newest albums.
    pub fn new() -> Self {
        Audio {
            screen: Screen { title: "LIBRARY".into(), rows: vec![], context: None, playlist: None, cursor: 0 },
            back: vec![],
            table: TableState::default(),
            search: Input::new("/ search artists, albums, songs"),
            queue_cursor: 0,
            queue_offset: 0,
            cover: None,
            cover_id: None,
            popup: None,
            generation: 0,
            rects: vec![Rect::default(); 3],
            page: 10,
            started: false,
            downloaded: Default::default(),
            progress: String::new(),
            remove_armed: None,
        }
    }

    /// The row under the cursor.
    fn selected(&self) -> Option<(Kind, Value)> {
        self.table.selected().and_then(|i| self.screen.rows.get(i)).cloned()
    }
}

impl Channel for Audio {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "AUDIO"
    }

    fn start(&mut self, ctx: &mut Ctx) {
        self.started = true;
        self.refresh_marks(ctx);
        self.load_newest(ctx);
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let (browser, deck) = if view.narrow {
            let [b, d] = Layout::vertical([Constraint::Fill(1), Constraint::Length(13)]).areas(area);
            (b, d)
        } else {
            let [b, d] = Layout::horizontal([Constraint::Fill(3), Constraint::Fill(2)]).areas(area);
            (b, d)
        };
        let (search, results) = self.render_browser(frame, browser, view);
        self.render_deck(frame, deck, view, view.player);
        if let Some(popup) = &mut self.popup {
            match popup {
                Popup::Pick(pick, _) => pick.render(frame, area, view.pal),
                Popup::Name(ask, _) => ask.render(frame, area, view.pal),
            }
        }
        self.rects = vec![search, results, deck];
        self.rects.clone()
    }

    fn captures_keys(&self) -> bool {
        self.popup.is_some()
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if self.popup.is_some() {
            self.popup_key(&key, ctx);
            return Outcome::Used;
        }
        match section {
            SEARCH => match self.search.key(&key) {
                InputResult::Submit => {
                    let q = self.search.value.trim().to_string();
                    self.search(q, ctx);
                    ctx.focus(RESULTS);
                    Outcome::Used
                }
                InputResult::Used => Outcome::Used,
                InputResult::Ignored => Outcome::Ignored,
            },
            RESULTS => {
                let n = self.screen.rows.len();
                let at = self.table.selected().unwrap_or(0);
                match key.code {
                    KeyCode::Down if n > 0 => self.table.select(Some((at + 1).min(n - 1))),
                    KeyCode::Up => self.table.select(Some(at.saturating_sub(1))),
                    KeyCode::PageDown if n > 0 => self.table.select(Some((at + self.page).min(n - 1))),
                    KeyCode::PageUp => self.table.select(Some(at.saturating_sub(self.page))),
                    KeyCode::Home => self.table.select(Some(0)),
                    KeyCode::End if n > 0 => self.table.select(Some(n - 1)),
                    KeyCode::Enter => self.open_row(ctx),
                    _ => return self.common(&key, ctx),
                }
                Outcome::Used
            }
            DECK => match key.code {
                KeyCode::Down => {
                    self.queue_cursor += 1;
                    Outcome::Used
                }
                KeyCode::Up => {
                    self.queue_cursor = self.queue_cursor.saturating_sub(1);
                    Outcome::Used
                }
                KeyCode::Enter => {
                    ctx.player.jump(self.queue_offset + self.queue_cursor);
                    Outcome::Used
                }
                _ => self.common(&key, ctx),
            },
            _ => Outcome::Ignored,
        }
    }

    fn nav_key(&mut self, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        self.common(&key, ctx)
    }

    fn paste(&mut self, section: usize, text: &str, _ctx: &mut Ctx) {
        match &mut self.popup {
            Some(Popup::Name(ask, _)) => ask.input.insert(text),
            _ if section == SEARCH => self.search.insert(text),
            _ => {}
        }
    }

    fn mouse(&mut self, event: MouseEvent, section: Option<usize>, ctx: &mut Ctx) -> Outcome {
        let n = self.screen.rows.len();
        match (event.kind, section) {
            (MouseEventKind::Down(MouseButton::Left), Some(RESULTS)) => {
                let top = self.rects[RESULTS].y + 1; // under the header
                let row = event.row.saturating_sub(top) as usize + self.table.offset();
                if event.row >= top && row < n {
                    if self.table.selected() == Some(row) {
                        self.open_row(ctx); // a second click opens
                    }
                    self.table.select(Some(row));
                }
            }
            (MouseEventKind::ScrollDown, Some(RESULTS)) if n > 0 => {
                self.table.select(Some((self.table.selected().unwrap_or(0) + 3).min(n - 1)));
            }
            (MouseEventKind::ScrollUp, Some(RESULTS)) => {
                self.table.select(Some(self.table.selected().unwrap_or(0).saturating_sub(3)));
            }
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn on_msg(&mut self, _id: &str, msg: Box<dyn std::any::Any + Send>, ctx: &mut Ctx) {
        let Ok(got) = msg.downcast::<Got>() else { return };
        match *got {
            Got::Screen { generation, screen, remember } if generation == self.generation => {
                self.show(screen, remember)
            }
            Got::Screen { .. } => {}
            Got::Songs { then, songs } => {
                let n = songs.len();
                match then {
                    Then::Append => {
                        ctx.player.enqueue(songs);
                        if n > 0 {
                            ctx.toast("AUDIO", &format!("+ {n} queued"));
                        }
                    }
                    Then::Next => {
                        ctx.player.play_next(songs);
                        if n > 0 {
                            ctx.toast("AUDIO", &format!("+ {n} up next"));
                        }
                    }
                    Then::ToPlaylist => {
                        let ids: Vec<String> = songs.iter().map(|x| s(x, "id").to_string()).collect();
                        if ids.is_empty() {
                            ctx.toast("AUDIO", "Select a song, album or playlist first");
                            return;
                        }
                        let music = ctx.music.clone();
                        ctx.run(ID, async move {
                            let mut lists = music
                                .music("getPlaylists", json!({}))
                                .await
                                .map(|d| sub(&d, "playlists", "playlist"))
                                .unwrap_or_default();
                            lists.sort_by_key(|p| s(p, "name").to_lowercase());
                            Got::Playlists { songs: ids, lists }
                        });
                    }
                }
            }
            Got::Playlists { songs, lists } => {
                let mut options = vec![("+new".to_string(), "＋ New playlist…".to_string())];
                options.extend(lists.iter().map(|p| {
                    (s(p, "id").to_string(), format!("♫ {}  ({})", s(p, "name"), p["songCount"].as_u64().unwrap_or(0)))
                }));
                self.popup = Some(Popup::Pick(Pick::new("ADD TO PLAYLIST", options), songs));
            }
            Got::Play(songs, note) => {
                ctx.player.play(songs, 0);
                ctx.toast("AUDIO", &note);
            }
            Got::Cover { id, bytes } => {
                if self.cover_id.as_deref() == Some(id.as_str()) {
                    self.cover = bytes.as_deref().and_then(Picture::decode);
                    if let Some(b) = bytes {
                        // Kept on disk for the desktop's media widget (MPRIS art).
                        let dir = crate::os::config::cache_dir();
                        let _ = std::fs::create_dir_all(&dir);
                        let safe: String = id
                            .chars()
                            .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
                            .take(60)
                            .collect();
                        let _ = std::fs::write(dir.join(format!("cover-{safe}.jpg")), b);
                    }
                }
            }
            Got::Note(text) => ctx.toast("AUDIO", &text),
            Got::Failed(e) => ctx.error("AUDIO", &e),
            Got::Newest(Ok(screen)) => {
                if self.screen.rows.is_empty() {
                    self.show(screen, false);
                }
            }
            Got::Newest(Err(e)) => {
                if ctx.offline.lock().expect("offline").collections().is_empty() {
                    ctx.error("AUDIO", &e);
                } else {
                    // No Navidrome (on a train, the lifebook asleep): what's downloaded.
                    ctx.error("AUDIO", "Navidrome isn't answering: here's what's downloaded");
                    self.offline_screen(ctx, false);
                }
            }
            Got::Progress(line) => self.progress = line,
            Got::Downloaded { name, result } => {
                self.progress.clear();
                self.refresh_marks(ctx);
                match result {
                    Ok((got, had)) => {
                        let also = if had > 0 { format!(", {had} already here") } else { String::new() };
                        ctx.toast("AUDIO", &format!("⇩ “{name}” is offline: {got} new{also}"));
                    }
                    Err(e) => ctx.error("AUDIO", &format!("Download stopped: {e}. O again picks it up.")),
                }
            }
        }
    }

    fn tick(&mut self, _now: Instant, _visible: bool, ctx: &mut Ctx) {
        // A new song with a new cover: fetch it (even off screen, so it's there when you look).
        let cover = ctx.player.with(|st| st.current().map(|x| s(x, "coverArt").to_string())).filter(|c| !c.is_empty());
        if cover.is_some() && cover != self.cover_id {
            self.cover_id = cover.clone();
            let id = cover.unwrap_or_default();
            let saved = ctx.offline.lock().expect("offline").cover_file(&id);
            let music = ctx.music.clone();
            ctx.run(ID, async move {
                let bytes = match saved {
                    Some(path) => tokio::fs::read(path).await.ok(),
                    None => music.cover(&id, 400).await,
                };
                Got::Cover { bytes, id }
            });
        }
        if let Some(e) = ctx.player.take_error() {
            ctx.error("AUDIO", &e);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn times_and_cells() {
        assert_eq!(time(65.4), "1:05");
        assert_eq!(fit("abc", 5), "abc  ");
        assert_eq!(fit("日本語", 4), "日本");
    }
}

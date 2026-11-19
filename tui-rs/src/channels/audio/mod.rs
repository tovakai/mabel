//! CH-03 AUDIO: Navidrome (through Mabel's proxy, or directly), played locally by mpv.
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

use crate::channels::audio::music::Music;
use crate::os::channel::{Channel, Ctx, Outcome, View};
use crate::os::keys;

use crate::channels::audio::player::Player;
use crate::os::ui::{self, PanelState, bold};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::picture::Picture;
use crate::widgets::popup::{Answer, Ask, Pick};
use crate::widgets::wrap::width;

const ID: &str = "audio";
const SEARCH: usize = 0;
const RESULTS: usize = 1;
const DECK: usize = 2;
const QUEUE_BEFORE: usize = 2;
const QUEUE_AFTER: usize = 40;

#[derive(Clone, Copy, PartialEq, Debug)]
enum Kind {
    Song,
    Album,
    Artist,
    Playlist,
    /// A downloaded collection: {"key", "name", "artist", "kind", "have", "total"}.
    Offline,
}

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
    Append,
    Next,
    ToPlaylist,
}

enum Got {
    Screen {
        generation: u64,
        screen: Screen,
        remember: bool,
    },
    Songs {
        then: Then,
        songs: Vec<Value>,
    },
    Playlists {
        songs: Vec<String>,
        lists: Vec<Value>,
    },
    Play(Vec<Value>, String),
    Cover {
        id: String,
        bytes: Option<Vec<u8>>,
    },
    /// The newest albums, or (Navidrome down) why not.
    Newest(Result<Screen, String>),
    /// Download progress for the library's bottom line ("" when done).
    Progress(String),
    Downloaded {
        name: String,
        result: Result<(usize, usize), String>,
    },
    Note(String),
    Failed(String),
}

enum Popup {
    /// Which playlist gets these songs.
    Pick(Pick, Vec<String>),
    /// The name of a new playlist with these songs.
    Name(Ask, Vec<String>),
}

fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}

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

pub struct Audio {
    screen: Screen,
    back: Vec<Screen>,
    table: TableState,
    search: Input,
    queue_cursor: usize,
    queue_offset: usize,
    cover: Option<Picture>,
    cover_id: Option<String>,
    popup: Option<Popup>,
    generation: u64,
    rects: Vec<Rect>,
    page: usize,
    started: bool,
    /// Songs on this device (for the ↓ marks), refreshed after downloads.
    downloaded: std::collections::HashSet<String>,
    /// A download running: shown under the library.
    progress: String,
    /// d on a download: pressed once, waiting for the second press (key, when).
    remove_armed: Option<(String, Instant)>,
}

impl Audio {
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

    fn selected(&self) -> Option<(Kind, Value)> {
        self.table.selected().and_then(|i| self.screen.rows.get(i)).cloned()
    }

    /// Fetch a screen in the background; a newer browse wins.
    fn browse<F>(&mut self, ctx: &Ctx, remember: bool, work: impl FnOnce(Music) -> F + Send + 'static)
    where
        F: std::future::Future<Output = Result<Screen, String>> + Send + 'static,
    {
        self.generation += 1;
        let generation = self.generation;
        let fut = work(ctx.music.clone());
        ctx.run(ID, async move {
            match fut.await {
                Ok(screen) => Got::Screen { generation, screen, remember },
                Err(e) => Got::Failed(e),
            }
        });
    }

    fn load_newest(&mut self, ctx: &Ctx) {
        let music = ctx.music.clone();
        ctx.run(ID, async move {
            let data = music.music("getAlbumList2", json!({"type": "newest", "size": 60})).await;
            Got::Newest(data.map_err(|e| format!("Navidrome isn't answering: {e}")).map(|data| {
                let rows = sub(&data, "albumList2", "album").into_iter().map(|a| (Kind::Album, a)).collect();
                Screen { title: "LIBRARY ░ NEWEST".into(), rows, context: None, playlist: None, cursor: 0 }
            }))
        });
    }

    fn refresh_marks(&mut self, ctx: &Ctx) {
        let off = ctx.offline.lock().expect("offline");
        self.downloaded = self.downloaded_ids(&off);
    }

    fn downloaded_ids(&self, off: &crate::channels::audio::offline::Offline) -> std::collections::HashSet<String> {
        off.collections().iter().flat_map(|(_, c)| c.song_ids.clone()).filter(|i| off.has(i)).collect()
    }

    /// o: what's downloaded, as collections.
    fn offline_screen(&mut self, ctx: &mut Ctx, remember: bool) {
        let (rows, title) = {
            let off = ctx.offline.lock().expect("offline");
            let rows: Rows = off
                .collections()
                .into_iter()
                .map(|(key, c)| {
                    let (have, total) = off.complete(&key);
                    let item = json!({"key": key, "name": c.name, "artist": c.artist, "kind": c.kind, "have": have, "total": total});
                    (Kind::Offline, item)
                })
                .collect();
            (rows, format!("OFFLINE ░ {} SONGS · {:.0} MB", off.song_count(), off.size_mb()))
        };
        if rows.is_empty() {
            ctx.toast("AUDIO", "Nothing downloaded yet: O on an album, playlist or song");
        }
        self.show(Screen { title, rows, context: None, playlist: None, cursor: 0 }, remember);
    }

    /// O: download the album or playlist on screen, else the selected row.
    fn download(&mut self, ctx: &mut Ctx) {
        let target = self.screen.context.clone().or_else(|| self.selected());
        let Some((kind, item)) = target.filter(|(k, _)| *k != Kind::Artist) else {
            ctx.toast("AUDIO", "Pick a song, album or playlist to download");
            return;
        };
        if kind == Kind::Offline {
            let (have, total) = (item["have"].as_u64().unwrap_or(0), item["total"].as_u64().unwrap_or(0));
            if have < total {
                ctx.toast("AUDIO", "Finishing a download needs Navidrome: open the album or playlist and press O");
            } else {
                ctx.toast("AUDIO", &format!("“{}” is all here", s(&item, "name")));
            }
            return;
        }
        let (music, offline, tx) = (ctx.music.clone(), ctx.offline.clone(), ctx.tx.clone());
        let max_bitrate = ctx.cfg.offline_bitrate;
        let (name, artist, cover) = match kind {
            Kind::Song => (s(&item, "title"), s(&item, "artist"), s(&item, "coverArt")),
            Kind::Playlist => (s(&item, "name"), s(&item, "owner"), s(&item, "coverArt")),
            _ => (s(&item, "name"), s(&item, "artist"), s(&item, "coverArt")),
        };
        let (name, artist, cover) = (name.to_string(), artist.to_string(), cover.to_string());
        let key = format!("{}:{}", format!("{kind:?}").to_lowercase(), s(&item, "id"));
        let kind_name = format!("{kind:?}").to_lowercase();
        ctx.toast("AUDIO", &format!("⇩ downloading “{name}”"));
        ctx.run(ID, async move {
            let songs = songs_of(&music, kind, &item).await;
            if songs.is_empty() {
                return Got::Downloaded { name, result: Err("Navidrome isn't answering, or it's empty".into()) };
            }
            let progress = |n: usize, total: usize, title: &str| {
                let line = format!("⇩ {n}/{total} · {}", title.chars().take(28).collect::<String>());
                let _ = tx.send(crate::os::channel::Msg::For(ID, Box::new(Got::Progress(line))));
            };
            let cover = (!cover.is_empty()).then_some(cover.as_str());
            let result = crate::channels::audio::offline::download(
                &offline,
                &music,
                &songs,
                &key,
                &kind_name,
                &name,
                &artist,
                cover,
                max_bitrate,
                progress,
            )
            .await
            .map_err(|e| e.to_string());
            Got::Downloaded { name, result }
        });
    }

    /// d on a download: twice within 3 s deletes it from this device.
    fn remove_download(&mut self, item: &Value, ctx: &mut Ctx) {
        let key = s(item, "key").to_string();
        let armed = self.remove_armed.as_ref().is_some_and(|(k, at)| *k == key && at.elapsed().as_secs() < 3);
        if !armed {
            self.remove_armed = Some((key, Instant::now()));
            ctx.toast("AUDIO", &format!("d again to delete “{}” from this device", s(item, "name")));
            return;
        }
        self.remove_armed = None;
        let removed = ctx.offline.lock().expect("offline").remove(&key);
        ctx.toast("AUDIO", &format!("− “{}” deleted ({removed} files)", s(item, "name")));
        self.refresh_marks(ctx);
        self.offline_screen(ctx, false);
    }

    fn search(&mut self, query: String, ctx: &Ctx) {
        if query.is_empty() {
            return;
        }
        self.browse(ctx, true, move |music| async move {
            let params = json!({"query": query, "artistCount": 8, "albumCount": 20, "songCount": 60});
            let data = music.music("search3", params).await.map_err(|e| e.to_string())?;
            let mut rows: Rows = sub(&data, "searchResult3", "artist").into_iter().map(|a| (Kind::Artist, a)).collect();
            rows.extend(sub(&data, "searchResult3", "album").into_iter().map(|a| (Kind::Album, a)));
            rows.extend(sub(&data, "searchResult3", "song").into_iter().map(|a| (Kind::Song, a)));
            let title = format!("LIBRARY ░ “{}”", query.to_uppercase().chars().take(30).collect::<String>());
            Ok(Screen { title, rows, context: None, playlist: None, cursor: 0 })
        });
    }

    fn open_playlist(&mut self, playlist: Value, remember: bool, cursor: usize, ctx: &Ctx) {
        self.browse(ctx, remember, move |music| async move {
            let data =
                music.music("getPlaylist", json!({"id": s(&playlist, "id")})).await.map_err(|e| e.to_string())?;
            let rows = sub(&data, "playlist", "entry").into_iter().map(|e| (Kind::Song, e)).collect();
            let title = format!("♫ {}", s(&playlist, "name").to_uppercase().chars().take(34).collect::<String>());
            Ok(Screen {
                title,
                rows,
                context: Some((Kind::Playlist, playlist.clone())),
                playlist: Some(playlist),
                cursor,
            })
        });
    }

    fn playlists(&mut self, ctx: &Ctx) {
        self.browse(ctx, true, |music| async move {
            let data = music.music("getPlaylists", json!({})).await.map_err(|e| e.to_string())?;
            let mut lists = sub(&data, "playlists", "playlist");
            lists.sort_by_key(|p| s(p, "name").to_lowercase());
            let rows = lists.into_iter().map(|p| (Kind::Playlist, p)).collect();
            Ok(Screen { title: "LIBRARY ░ PLAYLISTS".into(), rows, context: None, playlist: None, cursor: 0 })
        });
    }

    fn open_row(&mut self, ctx: &mut Ctx) {
        let Some((kind, item)) = self.selected() else { return };
        match kind {
            Kind::Song => {
                let songs: Vec<Value> =
                    self.screen.rows.iter().filter(|(k, _)| *k == Kind::Song).map(|(_, i)| i.clone()).collect();
                let start = songs.iter().position(|x| x == &item).unwrap_or(0);
                ctx.player.play(songs, start);
            }
            Kind::Album => self.browse(ctx, true, move |music| async move {
                let data = music.music("getAlbum", json!({"id": s(&item, "id")})).await.map_err(|e| e.to_string())?;
                let rows = sub(&data, "album", "song").into_iter().map(|x| (Kind::Song, x)).collect();
                let title = format!("◫ {}", s(&item, "name").to_uppercase().chars().take(34).collect::<String>());
                Ok(Screen { title, rows, context: Some((Kind::Album, item)), playlist: None, cursor: 0 })
            }),
            Kind::Playlist => self.open_playlist(item, true, 0, ctx),
            Kind::Offline => {
                let songs = ctx.offline.lock().expect("offline").collection_songs(s(&item, "key"));
                let title = format!("⇩ {}", s(&item, "name").to_uppercase().chars().take(34).collect::<String>());
                let rows = songs.into_iter().map(|x| (Kind::Song, x)).collect();
                self.show(Screen { title, rows, context: None, playlist: None, cursor: 0 }, true);
            }
            Kind::Artist => self.browse(ctx, true, move |music| async move {
                let data = music.music("getArtist", json!({"id": s(&item, "id")})).await.map_err(|e| e.to_string())?;
                let rows = sub(&data, "artist", "album").into_iter().map(|x| (Kind::Album, x)).collect();
                let title = format!("◉ {}", s(&item, "name").to_uppercase().chars().take(34).collect::<String>());
                Ok(Screen { title, rows, context: None, playlist: None, cursor: 0 })
            }),
        }
    }

    /// The selected row's songs, fetched, then `then`.
    fn with_songs(&self, then: Then, ctx: &mut Ctx) {
        let Some((kind, item)) = self.selected() else {
            ctx.toast("AUDIO", "Select a song, album or playlist first");
            return;
        };
        if kind == Kind::Offline {
            let songs = ctx.offline.lock().expect("offline").collection_songs(s(&item, "key"));
            ctx.run(ID, async move { Got::Songs { then, songs } });
            return;
        }
        let music = ctx.music.clone();
        ctx.run(ID, async move { Got::Songs { then, songs: songs_of(&music, kind, &item).await } });
    }

    fn show(&mut self, screen: Screen, remember: bool) {
        if remember && !self.screen.rows.is_empty() {
            let mut old = self.screen.clone();
            old.cursor = self.table.selected().unwrap_or(0);
            self.back.push(old);
        }
        let cursor = screen.cursor.min(screen.rows.len().saturating_sub(1));
        self.screen = screen;
        self.table.select((!self.screen.rows.is_empty()).then_some(cursor));
    }

    fn back(&mut self) {
        if let Some(screen) = self.back.pop() {
            self.show(screen, false);
        }
    }

    fn remove_from_playlist(&mut self, ctx: &mut Ctx) {
        let (Some(playlist), Some(row)) = (self.screen.playlist.clone(), self.table.selected()) else { return };
        let Some((Kind::Song, item)) = self.screen.rows.get(row).cloned() else { return };
        let music = ctx.music.clone();
        let title = s(&item, "title").to_string();
        ctx.run(ID, async move {
            let params = json!({"playlistId": s(&playlist, "id"), "songIndexToRemove": row});
            match music.music_write("updatePlaylist", params).await {
                Ok(()) => Got::Note(format!("− {title} removed from the playlist")),
                Err(e) => Got::Failed(e.to_string()),
            }
        });
        self.open_playlist(self.screen.playlist.clone().unwrap_or_default(), false, row, ctx);
    }

    fn create_playlist(&self, name: String, ids: Vec<String>, ctx: &Ctx) {
        let music = ctx.music.clone();
        ctx.run(ID, async move {
            match music.music_write("createPlaylist", json!({"name": name, "songId": ids})).await {
                Ok(()) => Got::Note(format!("♫ “{name}” saved with {} song(s)", ids.len())),
                Err(e) => Got::Failed(e.to_string()),
            }
        });
    }

    fn add_to_playlist(&mut self, list: String, ids: Vec<String>, ctx: &mut Ctx) {
        let reopen = self.screen.playlist.clone().filter(|p| s(p, "id") == list);
        let music = ctx.music.clone();
        ctx.run(ID, async move {
            match music.music_write("updatePlaylist", json!({"playlistId": list, "songIdToAdd": ids})).await {
                Ok(()) => Got::Note(format!("+ {} song(s) added to the playlist", ids.len())),
                Err(e) => Got::Failed(e.to_string()),
            }
        });
        if let Some(p) = reopen {
            let cursor = self.table.selected().unwrap_or(0);
            self.open_playlist(p, false, cursor, ctx);
        }
    }

    // ── keys ─────────────────────────────────────────────────────────────

    /// AUDIO's own keys: in nav mode and inside the library and the deck.
    fn common(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        let player = ctx.player.clone();
        match (keys::ch(key), key.code) {
            (Some('/'), _) => ctx.focus(SEARCH),
            (Some(' '), _) => player.toggle(),
            (Some(']' | '>'), _) => player.next(),
            (Some('[' | '<'), _) => player.previous(),
            (Some('+'), _) => player.change_volume(5),
            (Some('-'), _) => player.change_volume(-5),
            (_, KeyCode::Right) => player.seek(10.0),
            (_, KeyCode::Left) => player.seek(-10.0),
            (Some('a'), _) => self.with_songs(Then::Append, ctx),
            (Some('n'), _) => self.with_songs(Then::Next, ctx),
            (Some('s'), _) => self.with_songs(Then::ToPlaylist, ctx),
            (Some('S'), _) => {
                let ids: Vec<String> = player.with(|st| st.queue.iter().map(|x| s(x, "id").to_string()).collect());
                if ids.is_empty() {
                    ctx.toast("AUDIO", "The queue is empty");
                } else {
                    self.popup = Some(Popup::Name(Ask::new("SAVE THE QUEUE AS A PLAYLIST", "name"), ids));
                }
            }
            (Some('p'), _) => self.playlists(ctx),
            (Some('d'), _) => match self.selected() {
                Some((Kind::Offline, item)) => self.remove_download(&item, ctx),
                _ => self.remove_from_playlist(ctx),
            },
            (Some('z'), _) => {
                let on = !player.with(|st| st.shuffle);
                player.set_shuffle(on);
                ctx.toast("AUDIO", if on { "⤨ shuffle on: the rest of the queue is shuffled" } else { "shuffle off" });
            }
            (Some('R'), _) => {
                let music = ctx.music.clone();
                ctx.run(ID, async move {
                    let songs = music
                        .music("getRandomSongs", json!({"size": 50}))
                        .await
                        .map(|d| sub(&d, "randomSongs", "song"));
                    match songs {
                        Ok(songs) if !songs.is_empty() => {
                            let note = format!("⤨ a random mix of {} songs", songs.len());
                            Got::Play(songs, note)
                        }
                        Ok(_) => Got::Note("no songs came back".into()),
                        Err(e) => Got::Failed(e.to_string()),
                    }
                });
            }
            (Some('O'), _) => self.download(ctx),
            (Some('o'), _) => self.offline_screen(ctx, true),
            (_, KeyCode::Backspace) => self.back(),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn popup_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) {
        let Some(popup) = self.popup.as_mut() else { return };
        match popup {
            Popup::Pick(pick, ids) => match pick.key(key) {
                Answer::Open => {}
                Answer::Cancel => self.popup = None,
                Answer::Chosen(choice) => {
                    let ids = std::mem::take(ids);
                    self.popup = None;
                    if choice == "+new" {
                        self.popup = Some(Popup::Name(Ask::new("NEW PLAYLIST", "name"), ids));
                    } else {
                        self.add_to_playlist(choice, ids, ctx);
                    }
                }
            },
            Popup::Name(ask, ids) => match ask.key(key) {
                Answer::Open => {}
                Answer::Cancel => self.popup = None,
                Answer::Chosen(name) => {
                    let ids = std::mem::take(ids);
                    self.popup = None;
                    if !name.is_empty() {
                        self.create_playlist(name, ids, ctx);
                    }
                }
            },
        }
    }

    // ── drawing ──────────────────────────────────────────────────────────

    fn row_cells(&self, kind: Kind, item: &Value, widths: &[usize; 5]) -> [String; 5] {
        let cells: [String; 5] = match kind {
            Kind::Song => [
                if self.downloaded.contains(s(item, "id")) { "↓" } else { "♪" }.into(),
                s(item, "title").into(),
                s(item, "artist").into(),
                s(item, "album").into(),
                time(item["duration"].as_f64().unwrap_or(0.0)),
            ],
            Kind::Album => [
                "◫".into(),
                s(item, "name").into(),
                s(item, "artist").into(),
                item["year"].as_u64().map(|y| y.to_string()).unwrap_or_default(),
                item["songCount"].as_u64().map(|n| format!("{n} tr")).unwrap_or_default(),
            ],
            Kind::Playlist => [
                "♫".into(),
                s(item, "name").into(),
                s(item, "owner").into(),
                format!("{} songs", item["songCount"].as_u64().unwrap_or(0)),
                time(item["duration"].as_f64().unwrap_or(0.0)),
            ],
            Kind::Artist => [
                "◉".into(),
                s(item, "name").into(),
                String::new(),
                item["albumCount"].as_u64().map(|n| format!("{n} albums")).unwrap_or_default(),
                String::new(),
            ],
            Kind::Offline => {
                let (have, total) = (item["have"].as_u64().unwrap_or(0), item["total"].as_u64().unwrap_or(0));
                let kind = s(item, "kind");
                [
                    "⇩".into(),
                    s(item, "name").into(),
                    s(item, "artist").into(),
                    if have < total { format!("{kind} · {have}/{total}") } else { kind.to_string() },
                    format!("{total} tr"),
                ]
            }
        };
        let mut i = 0;
        cells.map(|c| {
            let out = fit(&c, widths[i]);
            i += 1;
            out
        })
    }

    fn render_browser(&mut self, frame: &mut Frame, area: Rect, view: &View) -> (Rect, Rect) {
        let pal = view.pal;
        // The search box has its own border; the library's border stands for the results.
        let lit = view.panel_state(RESULTS) == PanelState::Lit;
        let inside = view.inside(SEARCH) || view.inside(RESULTS);
        let state = if lit {
            PanelState::Lit
        } else if inside {
            PanelState::Inside
        } else {
            PanelState::Idle
        };
        let mut block = ui::panel(pal, &self.screen.title, state);
        if !self.progress.is_empty() {
            block = block.title_bottom(Line::styled(format!(" {} ", self.progress), bold(pal.teal)).right_aligned());
        }
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let [search, _, results] =
            Layout::vertical([Constraint::Length(3), Constraint::Length(1), Constraint::Fill(1)]).areas(inner);

        let sblock = ui::panel(pal, "", view.panel_state(SEARCH));
        let field = sblock.inner(search);
        frame.render_widget(sblock, search);
        let placeholder = if view.panel_state(SEARCH) == PanelState::Lit { pal.amber_dim } else { pal.amber_faint };
        self.search.render(
            frame,
            field,
            Style::new().fg(pal.amber_hi),
            Style::new().fg(placeholder),
            view.inside(SEARCH),
        );

        // Columns: mark, title, artist, album · year, time; the title takes what's left.
        let w = results.width as usize;
        let fixed = 2 + 22 + 22 + 6 + 4;
        let title_w = w.saturating_sub(fixed).clamp(8, 48);
        let widths = [2, title_w, 22, 22, 6];
        let header =
            Row::new(["", "TITLE", "ARTIST", "ALBUM · YEAR", "TIME"].map(|h| Line::styled(h, bold(pal.beige))))
                .style(Style::new().bg(pal.amber_faint));
        let rows: Vec<Row> =
            self.screen.rows.iter().map(|(k, item)| Row::new(self.row_cells(*k, item, &widths))).collect();
        let cursor = if view.inside(RESULTS) {
            Style::new().bg(pal.amber).fg(pal.bg)
        } else {
            Style::new().bg(pal.amber_faint).fg(pal.amber_hi)
        };
        let table = Table::new(rows, widths.map(|w| Constraint::Length(w as u16)))
            .header(header)
            .column_spacing(1)
            .style(Style::new().fg(pal.amber))
            .row_highlight_style(cursor);
        self.page = (results.height as usize).saturating_sub(2).max(1);
        frame.render_stateful_widget(table, results, &mut self.table);
        if self.screen.rows.is_empty() {
            let note = Rect { y: results.y + 1, height: 1, ..results };
            frame.render_widget(Paragraph::new(Span::styled(" loading…", Style::new().fg(pal.amber_dim))), note);
        }
        (search, results)
    }

    fn render_deck(&mut self, frame: &mut Frame, area: Rect, view: &View, player: &Player) {
        let pal = view.pal;
        let block = ui::panel(pal, "DECK", view.panel_state(DECK));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        // The cover takes room only when there is one.
        let has_cover = self.cover.is_some();
        let (cover_area, info_area) = if view.narrow {
            let [c, rest] =
                Layout::horizontal([Constraint::Length(if has_cover { 24 } else { 0 }), Constraint::Fill(1)])
                    .areas(inner);
            (c, rest)
        } else {
            let [c, rest] = Layout::vertical([Constraint::Length(if has_cover { 15 } else { 0 }), Constraint::Fill(1)])
                .areas(inner);
            (c, rest)
        };
        if let Some(p) = &mut self.cover {
            let max_h = if view.narrow { 11 } else { 14 };
            let w = if view.narrow { 22 } else { 30 };
            let spot = Rect { width: w.min(cover_area.width), ..cover_area };
            p.render(spot, frame.buffer_mut(), max_h, 0);
        }

        let (now, queue_lines) = player.with(|st| {
            let mut now: Vec<Line> = vec![];
            match st.current() {
                Some(song) => {
                    let mark = if st.paused { "❚❚ " } else { "▶ " };
                    now.push(Line::styled(format!("{mark}{}", s(song, "title")), bold(pal.amber_hi)));
                    now.push(Line::styled(s(song, "artist").to_string(), Style::new().fg(pal.amber)));
                    now.push(Line::styled(s(song, "album").to_string(), Style::new().fg(pal.amber_dim)));
                    let shuffle = if st.shuffle { "   ⤨ shuffle" } else { "" };
                    now.push(Line::styled(
                        format!("{} / {}   vol {}{shuffle}", time(st.position), time(st.length()), st.volume),
                        Style::new().fg(pal.teal),
                    ));
                }
                None => {
                    now.push(Line::styled("NO TAPE IN THE DECK", bold(pal.amber_dim)));
                    now.push(Line::styled("pick something on the left", Style::new().fg(pal.amber_faint)));
                }
            }
            let progress = if st.length() > 0.0 { (st.position / st.length()).clamp(0.0, 1.0) } else { 0.0 };
            // The queue: a window around the current song, not all 800 of them.
            let at = st.index.unwrap_or(0);
            let start = at.saturating_sub(QUEUE_BEFORE);
            let end = (at + QUEUE_AFTER + 1).min(st.queue.len());
            let mut q: Vec<(Option<usize>, String, bool)> = (start..end)
                .map(|i| {
                    let song = &st.queue[i];
                    let on = st.index == Some(i);
                    let artist: String = s(song, "artist").chars().take(18).collect();
                    let title: String = s(song, "title").chars().take(32).collect();
                    (Some(i), format!("{} {title}  ·  {artist}", if on { "▶" } else { " " }), on)
                })
                .collect();
            if end < st.queue.len() {
                q.push((None, format!("  … {} more", st.queue.len() - end), false));
            }
            ((now, progress), q)
        });
        let (now_lines, progress) = now;
        let [np, _, bar, _, queue] = Layout::vertical([
            Constraint::Length(now_lines.len() as u16),
            Constraint::Length(if view.narrow { 0 } else { 1 }),
            Constraint::Length(1),
            Constraint::Length(if view.narrow { 0 } else { 1 }),
            Constraint::Fill(1),
        ])
        .areas(info_area);
        frame.render_widget(Paragraph::new(now_lines), np);
        let filled = (progress * bar.width as f64).round() as usize;
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled("━".repeat(filled), Style::new().fg(pal.amber)),
                Span::styled("━".repeat((bar.width as usize).saturating_sub(filled)), Style::new().fg(pal.amber_faint)),
            ])),
            bar,
        );

        self.queue_offset = queue_lines.first().and_then(|(i, _, _)| *i).unwrap_or(0);
        let shown = queue_lines.len();
        self.queue_cursor = self.queue_cursor.min(shown.saturating_sub(1));
        let inside = view.inside(DECK);
        let rows = queue.height as usize;
        let skip = (self.queue_cursor + 1).saturating_sub(rows);
        let lines: Vec<Line> = queue_lines
            .into_iter()
            .enumerate()
            .skip(skip)
            .take(rows)
            .map(|(n, (_, text, on))| {
                let style = if inside && n == self.queue_cursor {
                    Style::new().bg(pal.amber_faint).fg(pal.amber_hi)
                } else if on {
                    bold(pal.amber_hi)
                } else {
                    Style::new().fg(pal.amber)
                };
                Line::styled(fit(&text, queue.width as usize), style)
            })
            .collect();
        frame.render_widget(Paragraph::new(lines), queue);
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

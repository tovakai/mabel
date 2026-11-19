//! MPRIS on the session bus, so the desktop (Quickshell, playerctl) sees and controls the AUDIO
//! deck. Publishes org.mpris.MediaPlayer2.mabel_os with title, artist, album, length and the
//! cover (the file AUDIO already caches), and maps play/pause/next/previous/seek to the player.
//! Without a session bus (over SSH, a bare console) it quietly does nothing.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use tokio::sync::mpsc::UnboundedSender;
use zbus::interface;
use zbus::object_server::SignalEmitter;
use zbus::{ObjectPath, OwnedVariant as OwnedValue, Variant as Value};

use crate::channels::audio::player::Player;
use crate::os::channel::Msg;

const BUS_NAME: &str = "org.mpris.MediaPlayer2.mabel_os";
const PATH: &str = "/org/mpris/MediaPlayer2";
const NO_TRACK: &str = "/org/mpris/MediaPlayer2/TrackList/NoTrack";

fn owned<'a>(v: impl Into<Value<'a>>) -> OwnedValue {
    v.into().try_to_owned().expect("plain values convert")
}

/// The cover AUDIO saved for this song (its cache, else the offline folder).
fn cover_file(cover_id: &str) -> Option<PathBuf> {
    if cover_id.is_empty() {
        return None;
    }
    let safe: String =
        cover_id.chars().filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_').take(60).collect();
    let cached = crate::os::config::cache_dir().join(format!("cover-{safe}.jpg"));
    let offline = crate::channels::audio::offline::root()
        .join("covers")
        .join(format!("{}.jpg", crate::channels::audio::offline::safe(cover_id, 60)));
    [cached, offline].into_iter().find(|p| p.exists())
}

struct Root {
    app: UnboundedSender<Msg>,
}

#[interface(name = "org.mpris.MediaPlayer2")]
impl Root {
    fn raise(&self) {}

    fn quit(&self) {
        let _ = self.app.send(Msg::Quit);
    }

    #[zbus(property)]
    fn can_quit(&self) -> bool {
        true
    }

    #[zbus(property)]
    fn can_raise(&self) -> bool {
        false
    }

    #[zbus(property)]
    fn has_track_list(&self) -> bool {
        false
    }

    #[zbus(property)]
    fn identity(&self) -> &str {
        "MABEL-OS"
    }

    #[zbus(property)]
    fn desktop_entry(&self) -> &str {
        "mabel-os"
    }

    #[zbus(property)]
    fn supported_uri_schemes(&self) -> Vec<String> {
        vec![]
    }

    #[zbus(property)]
    fn supported_mime_types(&self) -> Vec<String> {
        vec![]
    }
}

struct PlayerIface {
    player: Player,
}

impl PlayerIface {
    fn status(&self) -> &'static str {
        self.player.with(|st| match (st.current(), st.paused) {
            (None, _) => "Stopped",
            (Some(_), true) => "Paused",
            (Some(_), false) => "Playing",
        })
    }
}

#[interface(name = "org.mpris.MediaPlayer2.Player")]
impl PlayerIface {
    #[zbus(property)]
    fn playback_status(&self) -> &str {
        self.status()
    }

    #[zbus(property)]
    fn metadata(&self) -> HashMap<String, OwnedValue> {
        self.player.with(|st| {
            let mut meta = HashMap::new();
            let Some(song) = st.current() else {
                meta.insert("mpris:trackid".into(), owned(ObjectPath::from_static_str_unchecked(NO_TRACK)));
                return meta;
            };
            let s = |k: &str| song[k].as_str().unwrap_or("").to_string();
            let id: String =
                s("id").chars().map(|c| if c.is_ascii_alphanumeric() || c == '_' { c } else { '_' }).collect();
            if let Ok(path) = ObjectPath::try_from(format!("/org/mabel_os/track/{id}")) {
                meta.insert("mpris:trackid".into(), owned(path));
            }
            meta.insert("xesam:title".into(), owned(s("title")));
            meta.insert("xesam:artist".into(), owned(vec![s("artist")]));
            meta.insert("xesam:album".into(), owned(s("album")));
            if st.length() > 0.0 {
                meta.insert("mpris:length".into(), owned((st.length() * 1e6) as i64));
            }
            if let Some(art) = cover_file(&s("coverArt")) {
                meta.insert("mpris:artUrl".into(), owned(format!("file://{}", art.display())));
            }
            meta
        })
    }

    #[zbus(property)]
    fn position(&self) -> i64 {
        self.player.with(|st| (st.position * 1e6) as i64)
    }

    #[zbus(property)]
    fn volume(&self) -> f64 {
        self.player.with(|st| st.volume as f64 / 100.0)
    }

    #[zbus(property)]
    fn set_volume(&self, value: f64) {
        let now = self.player.with(|st| st.volume);
        self.player.change_volume((value * 100.0).round() as i64 - now);
    }

    #[zbus(property)]
    fn rate(&self) -> f64 {
        1.0
    }

    #[zbus(property)]
    fn minimum_rate(&self) -> f64 {
        1.0
    }

    #[zbus(property)]
    fn maximum_rate(&self) -> f64 {
        1.0
    }

    #[zbus(property)]
    fn can_go_next(&self) -> bool {
        self.player.with(|st| st.index.is_some_and(|i| i + 1 < st.queue.len()))
    }

    #[zbus(property)]
    fn can_go_previous(&self) -> bool {
        self.player.with(|st| st.current().is_some())
    }

    #[zbus(property)]
    fn can_play(&self) -> bool {
        self.player.with(|st| !st.queue.is_empty())
    }

    #[zbus(property)]
    fn can_pause(&self) -> bool {
        self.player.with(|st| st.current().is_some())
    }

    #[zbus(property)]
    fn can_seek(&self) -> bool {
        self.player.with(|st| st.current().is_some())
    }

    #[zbus(property)]
    fn can_control(&self) -> bool {
        true
    }

    fn play(&self) {
        self.player.set_paused(false);
    }

    fn pause(&self) {
        self.player.set_paused(true);
    }

    fn play_pause(&self) {
        self.player.toggle();
    }

    fn stop(&self) {
        self.player.set_paused(true);
    }

    fn next(&self) {
        self.player.next();
    }

    fn previous(&self) {
        self.player.previous();
    }

    fn seek(&self, offset: i64) {
        self.player.seek(offset as f64 / 1e6);
    }

    fn set_position(&self, _track_id: ObjectPath<'_>, position: i64) {
        let now = self.player.with(|st| st.position);
        self.player.seek(position as f64 / 1e6 - now);
    }

    fn open_uri(&self, _uri: &str) {}
}

/// What listeners last heard: only a real change is sent.
type Seen = (String, &'static str, bool, i64, i64, usize, Option<usize>);

#[derive(Clone, Default)]
pub struct Mpris {
    conn: Arc<Mutex<Option<zbus::Connection>>>,
    last: Arc<Mutex<Option<Seen>>>,
}

impl Mpris {
    /// Connect in the background; until then (or without a bus) `changed` does nothing.
    pub fn start(player: Player, app: UnboundedSender<Msg>) -> Self {
        let me = Mpris::default();
        let slot = me.conn.clone();
        tokio::spawn(async move {
            // Another MABEL-OS (the Python one, during the port) may hold the name: then this one
            // shows up as a second player, the way MPRIS players with several copies do.
            let names = [BUS_NAME.to_string(), format!("{BUS_NAME}.instance{}", std::process::id())];
            for name in names {
                let built = zbus::connection::Builder::session()
                    .name(name)
                    .serve_at(PATH, Root { app: app.clone() })
                    .serve_at(PATH, PlayerIface { player: player.clone() })
                    .build()
                    .await;
                if let Ok(conn) = built {
                    *slot.lock().expect("mpris") = Some(conn);
                    return;
                }
            }
        });
        me
    }

    /// Tell listeners what changed (cheap to call often: it sends only on a real change).
    pub fn changed(&self, player: &Player) {
        let Some(conn) = self.conn.lock().expect("mpris").clone() else { return };
        let seen: Seen = player.with(|st| {
            let song = st.current();
            let id = song.map(|s| s["id"].as_str().unwrap_or("").to_string()).unwrap_or_default();
            let status = match (song, st.paused) {
                (None, _) => "Stopped",
                (Some(_), true) => "Paused",
                (Some(_), false) => "Playing",
            };
            let art = song.and_then(|s| cover_file(s["coverArt"].as_str().unwrap_or(""))).is_some();
            (id, status, art, st.length() as i64, st.volume, st.queue.len(), st.index)
        });
        {
            let mut last = self.last.lock().expect("mpris");
            if last.as_ref() == Some(&seen) {
                return;
            }
            *last = Some(seen);
        }
        tokio::spawn(async move {
            let Ok(iface) = conn.object_server().interface::<_, PlayerIface>(PATH).await else { return };
            let emitter: &SignalEmitter<'_> = iface.signal_emitter();
            let me = iface.get().await;
            let _ = me.playback_status_changed(emitter).await;
            let _ = me.metadata_changed(emitter).await;
            let _ = me.can_go_next_changed(emitter).await;
            let _ = me.can_go_previous_changed(emitter).await;
            let _ = me.can_play_changed(emitter).await;
            let _ = me.can_pause_changed(emitter).await;
            let _ = me.can_seek_changed(emitter).await;
            let _ = me.volume_changed(emitter).await;
        });
    }
}

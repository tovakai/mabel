//! Offline music: songs downloaded to this machine, played from disk when they're here.
//!
//! Files live in ~/Music/mabel-os-offline/<artist>/<album>/<track> - <title>.<ext> with an index
//! (index.json) of the song metadata, so the library browses with no network at all, and of the
//! collections you downloaded (an album, a playlist, a single song). Covers are saved beside
//! them. The same layout and index as the Python client, so both share one download folder.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use anyhow::{Result, anyhow};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::music::Music;

#[derive(Serialize, Deserialize, Clone)]
pub struct Entry {
    pub song: Value,
    pub path: String,
    #[serde(default)]
    pub size: u64,
}

#[derive(Serialize, Deserialize, Clone, Default)]
pub struct Collection {
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub artist: String,
    #[serde(default)]
    pub song_ids: Vec<String>,
    #[serde(default)]
    pub cover: Option<String>,
}

#[derive(Serialize, Deserialize, Default)]
struct Index {
    #[serde(default)]
    songs: BTreeMap<String, Entry>,
    #[serde(default)]
    collections: BTreeMap<String, Collection>,
}

pub fn root() -> PathBuf {
    match std::env::var_os("MABEL_OFFLINE_DIR") {
        Some(dir) if !dir.is_empty() => PathBuf::from(dir),
        _ => crate::config::home().join("Music").join("mabel-os-offline"),
    }
}

/// A file or folder name that's safe on any filesystem.
pub fn safe(text: &str, limit: usize) -> String {
    let text = if text.is_empty() { "unknown" } else { text };
    let cleaned: String =
        text.chars().map(|c| if "\\/:*?\"<>|".contains(c) || (c as u32) < 0x20 { '_' } else { c }).collect();
    let trimmed: String = cleaned.trim_matches(|c| c == ' ' || c == '.').chars().take(limit).collect();
    if trimmed.is_empty() { "unknown".into() } else { trimmed }
}

fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}

pub struct Offline {
    root: PathBuf,
    index: Index,
}

/// Shared between AUDIO (browsing, downloads) and the player (what plays from disk).
pub type Shared = Arc<Mutex<Offline>>;

impl Offline {
    pub fn load(root: PathBuf) -> Self {
        let mut index: Index = std::fs::read_to_string(root.join("index.json"))
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or_default();
        index.songs.retain(|_, e| root.join(&e.path).exists());
        Offline { root, index }
    }

    pub fn save(&self) -> Result<()> {
        std::fs::create_dir_all(&self.root)?;
        let tmp = self.root.join("index.tmp");
        std::fs::write(&tmp, serde_json::to_string_pretty(&self.index)?)?;
        std::fs::rename(tmp, self.root.join("index.json"))?;
        Ok(())
    }

    pub fn has(&self, song_id: &str) -> bool {
        self.index.songs.contains_key(song_id)
    }

    /// The file a song plays from, if it's downloaded.
    pub fn local(&self, song_id: &str) -> Option<String> {
        self.index.songs.get(song_id).map(|e| self.root.join(&e.path).to_string_lossy().into_owned())
    }

    pub fn collections(&self) -> Vec<(String, Collection)> {
        let mut out: Vec<(String, Collection)> =
            self.index.collections.iter().map(|(k, c)| (k.clone(), c.clone())).collect();
        out.sort_by_key(|(_, c)| (c.kind != "album", c.name.to_lowercase()));
        out
    }

    pub fn collection(&self, key: &str) -> Option<&Collection> {
        self.index.collections.get(key)
    }

    pub fn song_count(&self) -> usize {
        self.index.songs.len()
    }

    pub fn size_mb(&self) -> f64 {
        self.index.songs.values().map(|e| e.size).sum::<u64>() as f64 / 1e6
    }

    /// (downloaded, total) for a collection.
    pub fn complete(&self, key: &str) -> (usize, usize) {
        let ids = self.collection(key).map(|c| c.song_ids.as_slice()).unwrap_or_default();
        (ids.iter().filter(|i| self.has(i)).count(), ids.len())
    }

    pub fn collection_songs(&self, key: &str) -> Vec<Value> {
        let ids = self.collection(key).map(|c| c.song_ids.clone()).unwrap_or_default();
        ids.iter().filter_map(|i| self.index.songs.get(i)).map(|e| e.song.clone()).collect()
    }

    pub fn cover_path(&self, cover_id: &str) -> PathBuf {
        self.root.join("covers").join(format!("{}.jpg", safe(cover_id, 60)))
    }

    pub fn cover_file(&self, cover_id: &str) -> Option<PathBuf> {
        let p = self.cover_path(cover_id);
        p.exists().then_some(p)
    }

    fn path_for(song: &Value, transcoded: bool) -> PathBuf {
        let ext = Some(s(song, "transcodedSuffix"))
            .filter(|e| transcoded && !e.is_empty())
            .or(Some(s(song, "suffix")).filter(|e| !e.is_empty()))
            .unwrap_or("mp3");
        let track = match &song["track"] {
            Value::Number(n) => n.as_u64().map(|t| format!("{t:02} - ")),
            Value::String(t) if !t.is_empty() && t.chars().all(|c| c.is_ascii_digit()) => {
                t.parse::<u64>().ok().map(|t| format!("{t:02} - "))
            }
            _ => None,
        }
        .unwrap_or_default();
        PathBuf::from(safe(s(song, "artist"), 80))
            .join(safe(s(song, "album"), 80))
            .join(format!("{track}{}.{ext}", safe(s(song, "title"), 120)))
    }

    /// Forget a collection; delete the files no other collection still uses. Returns files removed.
    pub fn remove(&mut self, key: &str) -> usize {
        let Some(gone) = self.index.collections.remove(key) else { return 0 };
        let still: std::collections::HashSet<&String> =
            self.index.collections.values().flat_map(|c| &c.song_ids).collect();
        let doomed: Vec<String> = gone.song_ids.into_iter().filter(|i| !still.contains(i)).collect();
        let mut removed = 0;
        for id in doomed {
            let Some(entry) = self.index.songs.remove(&id) else { continue };
            let path = self.root.join(&entry.path);
            let _ = std::fs::remove_file(&path);
            removed += 1;
            // Tidy empty album and artist folders.
            for parent in path.ancestors().skip(1).take(2) {
                if std::fs::remove_dir(parent).is_err() {
                    break;
                }
            }
        }
        let _ = self.save();
        removed
    }
}

/// What the file really is, by its first bytes (a transcode may differ from the original).
fn true_suffix(head: &[u8]) -> Option<&'static str> {
    if head.get(4..8) == Some(b"ftyp") {
        return Some("m4a");
    }
    let magic: [(&[u8], &str); 6] = [
        (b"fLaC", "flac"),
        (b"OggS", "opus"),
        (b"ID3", "mp3"),
        (b"\xff\xfb", "mp3"),
        (b"\xff\xf3", "mp3"),
        (b"RIFF", "wav"),
    ];
    let ext = magic.iter().find(|(m, _)| head.starts_with(m)).map(|(_, e)| *e)?;
    if ext == "opus" && !head.windows(8).any(|w| w == b"OpusHead") {
        return Some("ogg");
    }
    Some(ext)
}

/// Rename a download after what arrived; returns the (possibly new) relative path.
fn fix_suffix(root: &Path, rel: PathBuf) -> PathBuf {
    let path = root.join(&rel);
    let mut head = [0u8; 64];
    let n = std::fs::File::open(&path).and_then(|mut f| std::io::Read::read(&mut f, &mut head)).unwrap_or(0);
    let Some(ext) = true_suffix(&head[..n]) else { return rel };
    if rel.extension().is_some_and(|e| e == ext) {
        return rel;
    }
    let new = rel.with_extension(ext);
    match std::fs::rename(&path, root.join(&new)) {
        Ok(()) => new,
        Err(_) => rel,
    }
}

/// Download a set of songs as one collection: what isn't here yet is fetched. `progress` hears
/// (n, total, title). Returns (downloaded, already here). Recorded first with every song it
/// should have, so a broken download shows up half-done and O again finishes it.
#[allow(clippy::too_many_arguments)]
pub async fn download(
    offline: &Shared,
    music: &Music,
    songs: &[Value],
    key: &str,
    kind: &str,
    name: &str,
    artist: &str,
    cover_id: Option<&str>,
    max_bitrate: i64,
    progress: impl Fn(usize, usize, &str),
) -> Result<(usize, usize)> {
    let root = {
        let mut off = offline.lock().expect("offline");
        let old = off.index.collections.get(key).cloned().unwrap_or_default();
        let mut ids = old.song_ids.clone();
        for song in songs {
            let id = s(song, "id").to_string();
            if !ids.contains(&id) {
                ids.push(id);
            }
        }
        let cover = cover_id.map(String::from).or(old.cover);
        off.index.collections.insert(
            key.into(),
            Collection { kind: kind.into(), name: name.into(), artist: artist.into(), song_ids: ids, cover },
        );
        off.save()?;
        off.root.clone()
    };
    // Covers too, per album, so the deck shows them offline.
    let mut covers: Vec<String> =
        songs.iter().map(|x| s(x, "coverArt").to_string()).filter(|c| !c.is_empty()).collect();
    covers.extend(cover_id.map(String::from));
    covers.dedup();
    for cover in covers {
        let path = offline.lock().expect("offline").cover_path(&cover);
        if !path.exists()
            && let Some(bytes) = music.cover(&cover, 400).await
        {
            let _ = std::fs::create_dir_all(path.parent().unwrap_or(&root));
            let _ = std::fs::write(&path, bytes);
        }
    }
    let (mut got, mut had) = (0, 0);
    for (n, song) in songs.iter().enumerate() {
        progress(n + 1, songs.len(), s(song, "title"));
        let id = s(song, "id");
        if offline.lock().expect("offline").has(id) {
            had += 1;
            continue;
        }
        let rel = Offline::path_for(song, max_bitrate > 0);
        let size = music
            .download(id, &root.join(&rel), max_bitrate)
            .await
            .map_err(|e| anyhow!("{}: {e}", s(song, "title")))?;
        let rel = fix_suffix(&root, rel);
        let mut off = offline.lock().expect("offline");
        off.index.songs.insert(id.into(), Entry { song: song.clone(), path: rel.to_string_lossy().into_owned(), size });
        off.save()?;
        got += 1;
    }
    Ok((got, had))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn names_paths_and_magic() {
        assert_eq!(safe("AC/DC: Live?", 80), "AC_DC_ Live_");
        assert_eq!(safe(" ..", 80), "unknown");
        let song = json!({"artist": "ヒグチアイ", "album": "未成線上", "title": "祈り", "track": 2, "suffix": "flac", "transcodedSuffix": "opus"});
        assert_eq!(Offline::path_for(&song, false), PathBuf::from("ヒグチアイ/未成線上/02 - 祈り.flac"));
        assert_eq!(Offline::path_for(&song, true), PathBuf::from("ヒグチアイ/未成線上/02 - 祈り.opus"));
        assert_eq!(true_suffix(b"ID3\x04"), Some("mp3"));
        assert_eq!(true_suffix(b"OggS\x00\x02....OpusHead"), Some("opus"));
        assert_eq!(true_suffix(b"OggS\x00\x02....vorbis.."), Some("ogg"));
        assert_eq!(true_suffix(b"\x00\x00\x00\x20ftypM4A "), Some("m4a"));
    }

    #[test]
    fn index_round_trip_and_remove() {
        let dir = std::env::temp_dir().join(format!("mabel-offline-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let mut off = Offline::load(dir.clone());
        let rel = "A/B/01 - x.mp3";
        std::fs::create_dir_all(dir.join("A/B")).unwrap();
        std::fs::write(dir.join(rel), b"ID3").unwrap();
        off.index
            .songs
            .insert("s1".into(), Entry { song: json!({"id": "s1", "title": "x"}), path: rel.into(), size: 3 });
        off.index.collections.insert(
            "album:a".into(),
            Collection { kind: "album".into(), name: "B".into(), song_ids: vec!["s1".into()], ..Default::default() },
        );
        off.save().unwrap();
        let again = Offline::load(dir.clone());
        assert!(again.has("s1") && again.complete("album:a") == (1, 1));
        assert_eq!(again.collection_songs("album:a")[0]["title"], "x");
        let mut again = again;
        assert_eq!(again.remove("album:a"), 1);
        assert!(!dir.join("A").exists(), "empty folders tidied");
        let _ = std::fs::remove_dir_all(&dir);
    }
}

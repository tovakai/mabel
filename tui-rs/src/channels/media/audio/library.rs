//! AUDIO's library: loading screens of albums, artists, songs and playlists (from Mabel's
//! proxy or Navidrome), search, the offline library and downloads, and playlist editing.

use super::*;

impl Audio {
    /// Fetch a screen in the background; a newer browse wins.
    pub(super) fn browse<F>(&mut self, ctx: &Ctx, remember: bool, work: impl FnOnce(Music) -> F + Send + 'static)
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

    /// The first screen: the newest albums (or, if Navidrome isn't answering, the offline library).
    pub(super) fn load_newest(&mut self, ctx: &Ctx) {
        let music = ctx.music.clone();
        ctx.run(ID, async move {
            let data = music.music("getAlbumList2", json!({"type": "newest", "size": 60})).await;
            Got::Newest(data.map_err(|e| format!("Navidrome isn't answering: {e}")).map(|data| {
                let rows = sub(&data, "albumList2", "album").into_iter().map(|a| (Kind::Album, a)).collect();
                Screen { title: "LIBRARY ░ NEWEST".into(), rows, context: None, playlist: None, cursor: 0 }
            }))
        });
    }

    /// Re-read which songs are downloaded (the ↓ marks).
    pub(super) fn refresh_marks(&mut self, ctx: &Ctx) {
        let off = ctx.offline.lock().expect("offline");
        self.downloaded = self.downloaded_ids(&off);
    }

    /// The ids of every downloaded song.
    pub(super) fn downloaded_ids(
        &self,
        off: &crate::channels::media::audio::offline::Offline,
    ) -> std::collections::HashSet<String> {
        off.collections().iter().flat_map(|(_, c)| c.song_ids.clone()).filter(|i| off.has(i)).collect()
    }

    /// o: what's downloaded, as collections.
    pub(super) fn offline_screen(&mut self, ctx: &mut Ctx, remember: bool) {
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
    pub(super) fn download(&mut self, ctx: &mut Ctx) {
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
            let result = crate::channels::media::audio::offline::download(
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
    pub(super) fn remove_download(&mut self, item: &Value, ctx: &mut Ctx) {
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

    /// /: search artists, albums and songs, as one screen.
    pub(super) fn search(&mut self, query: String, ctx: &Ctx) {
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

    /// A playlist's songs as a screen (`cursor`: where to put the cursor, after an edit).
    pub(super) fn open_playlist(&mut self, playlist: Value, remember: bool, cursor: usize, ctx: &Ctx) {
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

    /// p: every playlist, as a screen.
    pub(super) fn playlists(&mut self, ctx: &Ctx) {
        self.browse(ctx, true, |music| async move {
            let data = music.music("getPlaylists", json!({})).await.map_err(|e| e.to_string())?;
            let mut lists = sub(&data, "playlists", "playlist");
            lists.sort_by_key(|p| s(p, "name").to_lowercase());
            let rows = lists.into_iter().map(|p| (Kind::Playlist, p)).collect();
            Ok(Screen { title: "LIBRARY ░ PLAYLISTS".into(), rows, context: None, playlist: None, cursor: 0 })
        });
    }

    /// enter on a row: a song plays the screen's songs from it; an album, artist, playlist or
    /// downloaded collection opens as a new screen.
    pub(super) fn open_row(&mut self, ctx: &mut Ctx) {
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
    pub(super) fn with_songs(&self, then: Then, ctx: &mut Ctx) {
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

    /// Put `screen` up; `remember` keeps the current one (and its cursor) for backspace.
    pub(super) fn show(&mut self, screen: Screen, remember: bool) {
        if remember && !self.screen.rows.is_empty() {
            let mut old = self.screen.clone();
            old.cursor = self.table.selected().unwrap_or(0);
            self.back.push(old);
        }
        let cursor = screen.cursor.min(screen.rows.len().saturating_sub(1));
        self.screen = screen;
        self.table.select((!self.screen.rows.is_empty()).then_some(cursor));
    }

    /// backspace: the screen before.
    pub(super) fn back(&mut self) {
        if let Some(screen) = self.back.pop() {
            self.show(screen, false);
        }
    }

    /// d in a playlist: take the song out of it.
    pub(super) fn remove_from_playlist(&mut self, ctx: &mut Ctx) {
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

    /// A new playlist called `name` with these songs.
    pub(super) fn create_playlist(&self, name: String, ids: Vec<String>, ctx: &Ctx) {
        let music = ctx.music.clone();
        ctx.run(ID, async move {
            match music.music_write("createPlaylist", json!({"name": name, "songId": ids})).await {
                Ok(()) => Got::Note(format!("♫ “{name}” saved with {} song(s)", ids.len())),
                Err(e) => Got::Failed(e.to_string()),
            }
        });
    }

    /// Add these songs to an existing playlist.
    pub(super) fn add_to_playlist(&mut self, list: String, ids: Vec<String>, ctx: &mut Ctx) {
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
}

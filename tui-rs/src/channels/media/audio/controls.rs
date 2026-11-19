//! AUDIO's keys: the ones every section shares, and the popups (pick a playlist, name one).

use super::*;

impl Audio {
    // ── keys ─────────────────────────────────────────────────────────────

    /// AUDIO's own keys: in nav mode and inside the library and the deck.
    pub(super) fn common(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
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

    /// Keys while a popup is open: pick a playlist (or "new…"), or type a new one's name.
    pub(super) fn popup_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) {
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
}

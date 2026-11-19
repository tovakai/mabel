//! Drawing the whole screen, one frame.

use super::*;

impl App {
    /// Draw everything: casing, tabs, the channel, status line, footer, toasts, and the help
    /// card or tuner on top. A running tape gets everything under the casing.
    pub fn render(&mut self, frame: &mut Frame) {
        let area = frame.area();
        let pal = self.pal;
        self.narrow = area.width < NARROW_BELOW;
        frame.render_widget(Block::new().style(Style::new().bg(pal.bg).fg(pal.amber)), area);
        let [casing, tabs, body, status, footer] = Layout::vertical([
            Constraint::Length(1),
            Constraint::Length(2),
            Constraint::Fill(1),
            Constraint::Length(1),
            Constraint::Length(1),
        ])
        .areas(area);

        let clock = chrono::Local::now().format("%a %d %b %Y  %H:%M:%S").to_string().to_uppercase();
        let badge = if self.updating {
            Some("▲ UPDATING…")
        } else if self.update_ready.is_some() {
            Some("▲ UPDATE F12")
        } else {
            None
        };
        ui::casing(frame, casing, &pal, self.host(), self.link, badge, &clock);
        // A running tape: everything under the casing bar is the game.
        if self.channels[self.active].takes_screen() {
            let game = Rect { y: area.y + 1, height: area.height.saturating_sub(1), ..area };
            let view =
                View { pal: &pal, narrow: self.narrow, focus: self.focus, now: Instant::now(), player: &self.player };
            self.sections = self.channels[self.active].render(frame, game, &view);
            if self.settle {
                self.settle_focus();
            }
            ui::toasts(frame, game, &pal, &self.toasts);
            if let Some((id, scroll)) = &mut self.help {
                ui::help_card(frame, area, &pal, id, scroll);
            }
            if let Some(tuner) = &mut self.tuner {
                tuner.render(frame, area, &pal);
            }
            return;
        }
        let labels = self.tab_labels();
        ui::tabs(frame, tabs, &pal, &labels, self.active);
        self.tab_row = Rect { height: 1, ..tabs };

        let view =
            View { pal: &pal, narrow: self.narrow, focus: self.focus, now: Instant::now(), player: &self.player };
        self.sections = self.channels[self.active].render(frame, body, &view);
        // A resize can hide the section the keyboard was in (wide → narrow): back to nav mode.
        let current = match self.focus {
            Focus::Inside(i) | Focus::Nav(Some(i)) => Some(i),
            Focus::Nav(None) => None,
        };
        if self.settle {
            self.settle_focus();
        } else if current.is_some_and(|i| !self.visible(i)) {
            self.start_nav();
            self.redraw = true;
        }

        let playing = self.player.with(|st| {
            st.current().map(|song| {
                let title = song["title"].as_str().unwrap_or("");
                (format!("{title} · {}", song["artist"].as_str().unwrap_or("")), st.paused)
            })
        });
        ui::statusline(frame, status, &pal, playing.as_ref().map(|(t, p)| (t.as_str(), *p)), &self.status);
        let mut footer_keys = vec![("F1", "HOME"), ("F2", "COMMS"), ("F3", "LIFE"), ("F4", "MEDIA")];
        if self.narrow {
            footer_keys = vec![("alt+0–9", "channels")];
        }
        footer_keys.extend([("?", "keys"), ("^p", "play/pause"), ("^q", "power off")]);
        ui::footer(frame, footer, &pal, &footer_keys);

        ui::toasts(frame, body, &pal, &self.toasts);
        if let Some((id, scroll)) = &mut self.help {
            ui::help_card(frame, area, &pal, id, scroll);
        }
        if let Some(tuner) = &mut self.tuner {
            tuner.render(frame, area, &pal);
        }
    }
}

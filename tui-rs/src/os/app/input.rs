//! Keys, mouse and paste, in the order they're tried: the keys that work everywhere (channel
//! keys, power off, help, tuner), then the section you're inside, then nav mode.

use super::*;

impl App {
    /// A terminal event: keys (releases only to a running tape), mouse, pasted text.
    pub fn on_event(&mut self, event: Event) {
        if matches!(event, Event::Key(_) | Event::Mouse(_)) {
            self.last_input = Instant::now();
        }
        match event {
            Event::Key(key) if key.kind != KeyEventKind::Release => self.on_key(key),
            // Releases only go to a running tape (the terminal sends them only while it runs).
            Event::Key(key) => {
                if let Focus::Inside(section) = self.focus
                    && self.channels[self.active].key_releases()
                    && self.channels[self.active].raw_keys(section)
                {
                    let active = self.active;
                    self.with_channel(active, |ch, ctx| ch.key(section, key, ctx));
                }
            }
            Event::Mouse(mouse) => self.on_mouse(mouse),
            Event::Paste(text) => {
                if let Focus::Inside(section) = self.focus {
                    let active = self.active;
                    self.with_channel(active, |ch, ctx| ch.paste(section, &text, ctx));
                }
            }
            _ => {}
        }
    }

    /// Where a key goes, first match wins (docs/ARCHITECTURE.md lists the order).
    pub(super) fn on_key(&mut self, key: KeyEvent) {
        // Inside a TERM shell or a running tape, the program gets every key but alt+1–9 and ctrl+].
        if let Focus::Inside(section) = self.focus
            && self.help.is_none()
            && self.channels[self.active].raw_keys(section)
        {
            if let Some(n) = keys::alt_digit(&key).filter(|n| (1..=self.channels.len()).contains(n)) {
                self.switch(n - 1);
            } else if keys::ctrl_bracket(&key) {
                let active = self.active;
                if !self.with_channel(active, |ch, ctx| ch.escape(section, ctx)) {
                    self.focus = Focus::Nav(Some(section));
                }
            } else {
                let active = self.active;
                self.with_channel(active, |ch, ctx| ch.key(section, key, ctx));
            }
            return;
        }
        if keys::ctrl(&key, 'q') {
            self.quit = true;
            return;
        }
        if let Some(tuner) = &mut self.tuner {
            match tuner.key(&key, &mut self.pal) {
                crate::os::tuner::Outcome::Open => {}
                crate::os::tuner::Outcome::Close => self.tuner = None,
                crate::os::tuner::Outcome::Save => self.save_colours(),
                crate::os::tuner::Outcome::Reset => {
                    let raw =
                        std::fs::read_to_string(crate::os::config::config_path()).ok().and_then(|t| t.parse().ok());
                    self.pal = Palette::from_config(raw.as_ref().unwrap_or(&self.cfg.raw));
                }
            }
            return;
        }
        if let Some((_, scroll)) = &mut self.help {
            match (keys::ch(&key), key.code) {
                (Some('?' | 'q'), _) | (_, KeyCode::Esc) => self.help = None,
                (Some('j'), _) | (_, KeyCode::Down) => *scroll += 1,
                (Some('k'), _) | (_, KeyCode::Up) => *scroll = scroll.saturating_sub(1),
                _ => {}
            }
            return;
        }
        // A popup in the channel (pick a playlist, name it…) takes the keys first.
        if self.channels[self.active].captures_keys() {
            let (active, section) = (
                self.active,
                match self.focus {
                    Focus::Inside(i) | Focus::Nav(Some(i)) => i,
                    Focus::Nav(None) => 0,
                },
            );
            if self.with_channel(active, |ch, ctx| ch.key(section, key, ctx)) == Outcome::Used {
                return;
            }
        }
        if keys::fkey(&key) == Some(12) {
            self.update_key();
            return;
        }
        // The channel keys work everywhere: F1–F9, and alt+1–9 for keyboards without F-keys.
        let channel = keys::alt_digit(&key)
            .or(keys::fkey(&key).map(usize::from))
            .filter(|n| (1..=self.channels.len()).contains(n));
        if let Some(n) = channel {
            self.switch(n - 1);
            return;
        }
        if keys::ctrl(&key, 'z') {
            self.shell = true;
            return;
        }
        if keys::ctrl(&key, 'p') {
            self.player.toggle();
            return;
        }
        if keys::ctrl(&key, 't') {
            self.tuner = Some(crate::os::tuner::Tuner::new());
            return;
        }
        match self.focus {
            Focus::Inside(section) => self.key_inside(section, key),
            Focus::Nav(lit) => self.key_nav(lit, key),
        }
    }

    /// A key while inside a section: the channel first, then h/j/k/l as arrows, then esc/?.
    pub(super) fn key_inside(&mut self, section: usize, key: KeyEvent) {
        let active = self.active;
        if self.with_channel(active, |ch, ctx| ch.key(section, key, ctx)) == Outcome::Used {
            return;
        }
        // Inside, h j k l are the arrows wherever the section doesn't use the letter.
        if let Some(arrow) = keys::ch(&key).and_then(keys::arrow_for) {
            self.with_channel(active, |ch, ctx| ch.key(section, arrow, ctx));
            return;
        }
        if key.code == KeyCode::Esc || keys::ctrl_bracket(&key) {
            self.focus = Focus::Nav(Some(section));
        } else if keys::is(&key, '?') {
            self.open_help();
        }
    }

    /// A key in nav mode: h/j/k/l move the light, i/a/enter go in, esc, ?; others to the channel.
    pub(super) fn key_nav(&mut self, lit: Option<usize>, key: KeyEvent) {
        // Navigation's own keys always mean navigation: h j k l move, i / a / enter go in, esc,
        // ?. Every other key goes to the channel (AUDIO's space pauses, / searches…).
        match (keys::ch(&key), key.code) {
            (Some(k @ ('h' | 'j' | 'k' | 'l')), _) => match lit.filter(|&i| self.visible(i)) {
                None => self.start_nav(),
                Some(current) => {
                    if let Some(next) = nav::neighbour(current, &self.sections, k) {
                        self.focus = Focus::Nav(Some(next));
                    }
                }
            },
            (Some('i' | 'a'), _) | (_, KeyCode::Enter) => {
                if let Some(i) = lit.filter(|&i| self.visible(i)) {
                    self.focus = Focus::Inside(i);
                }
            }
            (_, KeyCode::Esc) => {
                if lit.is_none() {
                    self.start_nav();
                }
            }
            (Some('?'), _) => self.open_help(),
            _ => {
                let active = self.active;
                self.with_channel(active, |ch, ctx| ch.nav_key(key, ctx));
            }
        }
    }

    /// A mouse event: tabs switch channel, a click goes inside a section, the rest to the channel.
    pub(super) fn on_mouse(&mut self, mouse: MouseEvent) {
        let (x, y) = (mouse.column, mouse.row);
        let left = mouse.kind == MouseEventKind::Down(MouseButton::Left);
        if let Some(tuner) = &mut self.tuner {
            if left {
                tuner.click(y);
            }
            return;
        }
        if self.help.is_some() {
            if left {
                self.help = None;
            }
            return;
        }
        if left && self.tab_row.contains((x, y).into()) {
            let labels: Vec<&str> = self.channels.iter().map(|c| c.tab()).collect();
            let places = ui::tab_places(self.tab_row, &labels, self.active);
            if let Some(i) = places.iter().position(|(_, r)| r.contains((x, y).into())) {
                self.switch(i);
            }
            return;
        }
        let section = nav::hit(&self.sections, x, y);
        if left && let Some(i) = section {
            self.focus = Focus::Inside(i); // clicked into it: inside now
        }
        let active = self.active;
        let before = self.help_id_now();
        let used = self.with_channel(active, |ch, ctx| ch.mouse(mouse, section, ctx));
        if used == Outcome::Used && self.help_id_now() != before {
            self.settle = true; // a suite's bar was clicked: another app is on screen
        }
    }
}

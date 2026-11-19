//! A channel that holds several apps (COMMS: MABEL, INBOX · CREATIVE: PIXEL, TRACKER · SYS:
//! STATUS, NETWORK…): a bar of apps over the one on screen. The channel's key pressed again
//! moves to the next app; the bar is clickable too. Each app is a channel of its own.

use std::any::Any;
use std::time::Instant;

use crossterm::event::{KeyEvent, MouseButton, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;

use crate::os::channel::{Channel, Ctx, Outcome, View};
use crate::os::palette::Palette;

pub struct SuiteApp {
    pub key: &'static str,
    pub label: &'static str,
    pub hint: &'static str,
}

pub struct Suite {
    id: &'static str,
    tab: &'static str,
    /// Shown in the bar: "F7: next app".
    fkey: &'static str,
    apps: Vec<(SuiteApp, Box<dyn Channel>)>,
    current: usize,
    bar: Rect,
}

impl Suite {
    pub fn new(
        id: &'static str,
        tab: &'static str,
        fkey: &'static str,
        apps: Vec<(SuiteApp, Box<dyn Channel>)>,
    ) -> Self {
        Suite { id, tab, fkey, apps, current: 0, bar: Rect::default() }
    }

    fn app(&mut self) -> &mut Box<dyn Channel> {
        &mut self.apps[self.current].1
    }
}

/// The bar over a suite's apps (only drawn when it has more than one).
fn bar(frame: &mut Frame, area: Rect, pal: &Palette, apps: &[&SuiteApp], current: usize, fkey: &str) {
    let mut spans = vec![];
    for (i, app) in apps.iter().enumerate() {
        if i == current {
            spans.push(Span::styled(
                format!(" ◆ {} ", app.label),
                Style::new().fg(pal.bg).bg(pal.amber).add_modifier(Modifier::BOLD),
            ));
        } else {
            spans.push(Span::styled(
                format!("   {} ", app.label),
                Style::new().fg(pal.amber_dim).add_modifier(Modifier::BOLD),
            ));
        }
        spans.push(Span::raw("  "));
    }
    let tail = if fkey.is_empty() { String::new() } else { format!("   {fkey}: next app") };
    spans.push(Span::styled(format!("  {}{tail}", apps[current].hint), Style::new().fg(pal.amber_dim)));
    frame.render_widget(Paragraph::new(Line::from(spans)).style(Style::new().bg(pal.panel)), area);
}

/// Which app a click on the bar at column `x` (relative to the bar) hits.
fn bar_hit(apps: &[&SuiteApp], x: u16) -> Option<usize> {
    let mut left = 0u16;
    for (i, app) in apps.iter().enumerate() {
        let width = app.label.chars().count() as u16 + 6;
        if x >= left && x < left + width {
            return Some(i);
        }
        left += width;
    }
    None
}

impl Channel for Suite {
    fn id(&self) -> &'static str {
        self.id
    }

    fn tab(&self) -> &'static str {
        self.tab
    }

    fn start(&mut self, ctx: &mut Ctx) {
        for (_, app) in &mut self.apps {
            app.start(ctx);
        }
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let mut body = area;
        self.bar = Rect::default();
        if self.apps.len() > 1 {
            let [top, rest] = Layout::vertical([Constraint::Length(1), Constraint::Fill(1)]).areas(area);
            let labels: Vec<&SuiteApp> = self.apps.iter().map(|(a, _)| a).collect();
            bar(frame, top, view.pal, &labels, self.current, self.fkey);
            self.bar = top;
            body = rest;
        }
        self.app().render(frame, body, view)
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        self.app().key(section, key, ctx)
    }

    fn nav_key(&mut self, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        self.app().nav_key(key, ctx)
    }

    fn paste(&mut self, section: usize, text: &str, ctx: &mut Ctx) {
        self.app().paste(section, text, ctx);
    }

    fn mouse(&mut self, event: MouseEvent, section: Option<usize>, ctx: &mut Ctx) -> Outcome {
        let on_bar = self.bar.contains((event.column, event.row).into());
        if on_bar && event.kind == MouseEventKind::Down(MouseButton::Left) && self.apps.len() > 1 {
            let labels: Vec<&SuiteApp> = self.apps.iter().map(|(a, _)| a).collect();
            if let Some(i) = bar_hit(&labels, event.column - self.bar.x) {
                self.current = i;
                return Outcome::Used;
            }
        }
        self.app().mouse(event, section, ctx)
    }

    fn lands_inside(&self, narrow: bool) -> Option<usize> {
        self.apps[self.current].1.lands_inside(narrow)
    }

    fn cycle(&mut self) -> bool {
        if self.apps.len() < 2 {
            return false;
        }
        self.current = (self.current + 1) % self.apps.len();
        true
    }

    fn help_id(&self) -> &'static str {
        self.apps[self.current].0.key
    }

    fn accepts(&self, id: &str) -> bool {
        id == self.id || self.apps.iter().any(|(_, app)| app.accepts(id))
    }

    fn on_msg(&mut self, id: &str, msg: Box<dyn Any + Send>, ctx: &mut Ctx) {
        // Apps that aren't on screen still get their results (a reply keeps streaming in).
        if let Some(i) = self.apps.iter().position(|(_, app)| app.accepts(id)) {
            let before = ctx.requests.len();
            self.apps[i].1.on_msg(id, msg, ctx);
            if i != self.current {
                // An app off screen doesn't move the keyboard.
                let mut n = 0;
                ctx.requests.retain(|r| {
                    n += 1;
                    n <= before || !matches!(r, crate::os::channel::Request::Focus(_))
                });
            }
        }
    }

    fn show_app(&mut self, id: &str) {
        if let Some(i) = self.apps.iter().position(|(_, app)| app.accepts(id)) {
            self.current = i;
        }
    }

    fn next_wake(&self) -> Option<Instant> {
        self.apps[self.current].1.next_wake()
    }

    fn captures_keys(&self) -> bool {
        self.apps[self.current].1.captures_keys()
    }

    fn tick(&mut self, now: Instant, visible: bool, ctx: &mut Ctx) {
        let current = self.current;
        for (i, (_, app)) in self.apps.iter_mut().enumerate() {
            app.tick(now, visible && i == current, ctx);
        }
    }
}

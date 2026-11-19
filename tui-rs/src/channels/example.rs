//! An example channel to copy when you make your own. It isn't in the channel list, so it
//! never shows up; it's compiled with everything else, so it can't quietly go out of date.
//!
//! To try it: in `src/os/channel.rs`, add `boxed(crate::channels::example::Example::new()),`
//! to the list in `all()` just before SYS, then `scripts/sandbox.sh`. It becomes CH-08: F8 or
//! alt+8 opens it. (A real program usually belongs in a suite instead: see HOWTO.)
//!
//! What it shows:
//! - two sections (panels) you move between with h/l in nav mode, and go into with i/enter;
//! - keys inside a section (`key`) and keys that work in nav mode too (`nav_key`);
//! - asking Mabel's API for something in the background and getting the answer (`on_msg`);
//! - a little animation (`next_wake`).
//!
//! Read `docs/ARCHITECTURE.md` first: it explains sections, nav mode, Ctx and messages.

#![allow(dead_code)] // nothing constructs Example until you add it to the list

use std::any::Any;
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::Line;
use ratatui::widgets::Paragraph;

use crate::os::channel::{Channel, Ctx, Outcome, View};
use crate::os::keys;
use crate::os::ui::{self, bold};

/// The id messages and help use. Must be unique among channels.
const ID: &str = "example";

/// Section numbers: the order of the rects `render` returns.
const COUNTER: usize = 0;
const MABEL: usize = 1;

/// What comes back from background work (see `on_msg`).
/// What background work sends back (here: LIFE's answer, or an error).
enum Got {
    Life(Result<String, String>),
}

/// The channel's state: everything it remembers between frames.
pub struct Example {
    /// A counter, changed with + and -.
    count: i32,
    /// What Mabel said she's doing, once asked.
    doing: Option<String>,
    /// When it was made (the spinner turns from this).
    started: Instant,
}

impl Example {
    /// A fresh channel: count 0, nothing asked yet.
    pub fn new() -> Self {
        Example { count: 0, doing: None, started: Instant::now() }
    }
}

impl Default for Example {
    fn default() -> Self {
        Self::new()
    }
}

impl Channel for Example {
    fn id(&self) -> &'static str {
        ID
    }

    /// The tab's name.
    fn tab(&self) -> &'static str {
        "EXAMPLE"
    }

    /// Once at start-up. Ask for slow things here, in the background.
    fn start(&mut self, ctx: &mut Ctx) {
        ask_mabel(ctx);
    }

    /// Draw the channel into `area` (everything between the tabs and the status line) and
    /// return where each section is, in section order. The app uses those rects for nav
    /// (h/j/k/l), clicks and the lit border.
    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let pal = view.pal;
        // The deck (`view.narrow`) stacks panels; the PC puts them side by side.
        let [left, right] = if view.narrow {
            Layout::vertical([Constraint::Fill(1), Constraint::Fill(1)]).areas(area)
        } else {
            Layout::horizontal([Constraint::Fill(1), Constraint::Fill(1)]).areas(area)
        };

        // `panel_state` says whether this section is lit (nav mode) or the keys are inside it.
        let block = ui::panel(pal, "COUNTER", view.panel_state(COUNTER));
        let inner = block.inner(left);
        frame.render_widget(block, left);
        // A spinner that turns once a second, to show next_wake.
        let spin = ["◐", "◓", "◑", "◒"][(self.started.elapsed().as_millis() / 250 % 4) as usize];
        let lines = vec![
            Line::styled(format!(" {spin} count: {}", self.count), bold(pal.beige)),
            Line::raw(""),
            Line::styled(" inside: + / - change it, 0 resets", Style::new().fg(pal.amber_dim)),
        ];
        frame.render_widget(Paragraph::new(lines), inner);

        let block = ui::panel(pal, "MABEL", view.panel_state(MABEL));
        let inner = block.inner(right);
        frame.render_widget(block, right);
        let doing = self.doing.as_deref().unwrap_or("asking…");
        let lines = vec![
            Line::styled(format!(" she's: {doing}"), Style::new().fg(pal.amber)),
            Line::raw(""),
            Line::styled(" r (anywhere in this channel): ask again", Style::new().fg(pal.amber_dim)),
        ];
        frame.render_widget(Paragraph::new(lines), inner);

        vec![left, right]
    }

    /// A key while the keys are inside `section`. Return `Used` if you did something with it;
    /// `Ignored` lets the app try (h/j/k/l become arrows, esc goes back to nav mode).
    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        match (section, keys::ch(&key), key.code) {
            (COUNTER, Some('+'), _) | (COUNTER, _, KeyCode::Up) => self.count += 1,
            (COUNTER, Some('-'), _) | (COUNTER, _, KeyCode::Down) => self.count -= 1,
            (COUNTER, Some('0'), _) => {
                self.count = 0;
                ctx.toast("EXAMPLE", "counter reset"); // a little card for 5 seconds
            }
            (_, Some('r'), _) => ask_mabel(ctx),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    /// A key in nav mode that the app doesn't use itself (it keeps h j k l i a enter esc ?).
    fn nav_key(&mut self, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if keys::is(&key, 'r') {
            ask_mabel(ctx);
            return Outcome::Used;
        }
        Outcome::Ignored
    }

    /// Answers from background work started with `ctx.spawn(ID, …)` arrive here.
    fn on_msg(&mut self, _id: &str, msg: Box<dyn Any + Send>, ctx: &mut Ctx) {
        let Ok(got) = msg.downcast::<Got>() else { return };
        match *got {
            Got::Life(Ok(doing)) => self.doing = Some(doing),
            Got::Life(Err(e)) => {
                self.doing = Some("(Mabel isn't answering)".into());
                ctx.status(&format!("example: {e}")); // the status line above the footer
            }
        }
    }

    /// Ask to be drawn again at this time even without input: here, four times a second.
    fn next_wake(&self) -> Option<Instant> {
        Some(Instant::now() + Duration::from_millis(250))
    }
}

/// Ask Mabel what she's doing, without freezing the screen: `spawn` runs it in the background
/// and sends the result to this channel's `on_msg`.
fn ask_mabel(ctx: &mut Ctx) {
    ctx.spawn(ID, |api| async move {
        let life = api.life().await.map_err(|e| format!("{e:#}"));
        // /api/tui/life answers {"doing": ["…", "…"], …}: a list of what she's busy with.
        Got::Life(life.map(|v| {
            let doing: Vec<&str> = v["doing"].as_array().into_iter().flatten().filter_map(|d| d.as_str()).collect();
            if doing.is_empty() { "resting".to_string() } else { doing.join(", ") }
        }))
    });
}

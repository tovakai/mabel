//! Running a tape: starting it, stepping its frames, pausing, the pause menu (reset,
//! screenshot, console, quit), and drawing it with its cards.

use super::*;

/// Where screenshots go: ~/Pictures/mabel-os if there's a Pictures folder, else next to the tapes.
pub(super) fn screenshot_dir() -> PathBuf {
    let pictures = crate::os::config::home().join("Pictures");
    if pictures.is_dir() { pictures.join("mabel-os") } else { data_dir().join("screenshots") }
}

/// The tape's screen as a PNG, `SHOT_SCALE` times bigger.
pub(super) fn screenshot(px: &Pixels, name: &str) -> anyhow::Result<PathBuf> {
    let (w, h) = (px.w as u32, px.h as u32);
    let img = image::RgbImage::from_fn(w * SHOT_SCALE, h * SHOT_SCALE, |x, y| {
        let c = px.px[(y / SHOT_SCALE * w + x / SHOT_SCALE) as usize];
        image::Rgb([(c >> 16) as u8, (c >> 8) as u8, c as u8])
    });
    let dir = screenshot_dir();
    std::fs::create_dir_all(&dir)?;
    let path = dir.join(format!("{name}-{}.png", chrono::Local::now().format("%Y%m%d-%H%M%S")));
    img.save(&path)?;
    Ok(path)
}

impl Tapes {
    /// enter on a tape: install it first if it's only on Mabel; otherwise load its wasm and run
    /// it (errors become a toast and a console line, not a crash).
    pub(super) fn start_tape(&mut self, ctx: &mut Ctx) {
        let Some(entry) = self.entries.get(self.sel) else { return };
        if entry.kind == Kind::Remote {
            let name = entry.key();
            self.download(ctx, name, true);
            return;
        }
        let info = match &entry.info {
            Ok(i) => i.clone(),
            Err(e) => {
                ctx.error("TAPES", e);
                return;
            }
        };
        let wasm = match std::fs::read(&entry.wasm) {
            Ok(w) => w,
            Err(_) if entry.kind == Kind::Dev => {
                ctx.error("TAPES", "Not built yet: cargo tape (in the tape's folder)");
                return;
            }
            Err(e) => {
                ctx.error("TAPES", &format!("{}: {e}", entry.wasm.display()));
                return;
            }
        };
        self.last_run = Some(entry.dir.clone());
        let place = mabel_console::place(info.screen, self.area);
        let opts = Options { w: place.w, h: place.h, root: entry.dir.clone(), rate: self.rate, save_key: entry.key() };
        self.console.clear();
        match Tape::load(&wasm, opts) {
            Ok(mut tape) => {
                self.console.extend(tape.take_log());
                let now = Instant::now();
                let period = Duration::from_secs_f64(1.0 / info.fps as f64);
                self.played.insert(entry.key(), chrono::Utc::now().timestamp());
                if let Ok(text) = serde_json::to_string_pretty(&self.played) {
                    let _ = std::fs::create_dir_all(data_dir());
                    let _ = std::fs::write(played_file(), text);
                }
                self.run = Some(Running {
                    tape,
                    entry: self.sel,
                    keymap: Keymap::new(&info.keys),
                    screen: info.screen,
                    period,
                    next_frame: now,
                    last_frame: now,
                    built: mtime(&entry.wasm),
                    menu: None,
                    console: false,
                    crashed: None,
                });
                ctx.focus(LIST);
            }
            Err(e) => {
                self.console.push(format!("couldn't start: {e}"));
                ctx.error("TAPE CRASHED", &e);
            }
        }
    }

    /// Stop the running tape, keeping its log (and how it ended) for the card's console.
    pub(super) fn stop(&mut self) {
        if let Some(mut run) = self.run.take() {
            self.console.extend(run.tape.take_log());
            if let Some(e) = run.crashed {
                self.console.push(format!("crashed: {e}"));
            }
        }
    }

    /// Open the pause menu (and silence the tape).
    pub(super) fn pause(&mut self) {
        if let Some(run) = &mut self.run
            && run.menu.is_none()
            && run.crashed.is_none()
        {
            run.menu = Some(0);
            run.tape.pause(true);
        }
    }

    /// Close the pause menu and carry on, without the tape seeing the paused time as one huge `dt`.
    pub(super) fn resume(run: &mut Running) {
        run.menu = None;
        run.console = false;
        run.tape.pause(false);
        let now = Instant::now();
        (run.next_frame, run.last_frame) = (now, now); // no catch-up jump after the pause
    }

    /// Step the tape if a frame is due.
    pub(super) fn advance(&mut self, now: Instant) {
        let Some(run) = &mut self.run else { return };
        if run.menu.is_some() || run.crashed.is_some() || now < run.next_frame {
            return;
        }
        let dt = now.duration_since(run.last_frame).as_secs_f32().min(MAX_DT);
        run.last_frame = now;
        run.next_frame += run.period;
        if run.next_frame < now {
            run.next_frame = now + run.period; // fell behind: don't try to catch up
        }
        if let Err(e) = run.tape.step(dt, now) {
            run.tape.pause(true);
            run.crashed = Some(e);
        }
        let log = run.tape.take_log();
        self.console.extend(log);
        let excess = self.console.len().saturating_sub(200);
        self.console.drain(..excess);
    }

    /// enter in the pause menu.
    pub(super) fn menu_choice(&mut self, choice: usize, ctx: &mut Ctx) {
        let Some(run) = &mut self.run else { return };
        match MENU[choice] {
            "resume" => Self::resume(run),
            "reset" => {
                let entry = run.entry;
                self.stop();
                self.sel = entry;
                self.start_tape(ctx);
            }
            "screenshot" => {
                let name = self.entries.get(run.entry).map(Entry::key).unwrap_or_else(|| "tape".into());
                match screenshot(run.tape.frame(), &name) {
                    Ok(path) => ctx.toast("SCREENSHOT", &format!("saved {}", path.display())),
                    Err(e) => ctx.error("SCREENSHOT", &format!("{e:#}")),
                }
            }
            "console" => run.console = true,
            _ => self.stop(),
        }
    }

    /// The running tape over the whole body (stepping it first if a frame is due), with the
    /// pause menu, console or crash card on top when they're open.
    pub(super) fn render_run(&mut self, frame: &mut Frame, area: Rect, pal: &Palette) {
        self.advance(Instant::now());
        let Some(run) = &mut self.run else { return };
        frame.render_widget(Block::new().style(Style::new().bg(pal.bg)), area);
        let place = mabel_console::place(run.screen, area);
        if run.screen == Screen::Fill {
            run.tape.resize(place.w, place.h);
        }
        mabel_console::render(run.tape.frame(), run.tape.texts(), frame.buffer_mut(), place.cells, place.scale);

        if run.console {
            let room = area.height.saturating_sub(6) as usize;
            let mut lines: Vec<Line> = if self.console.is_empty() {
                vec![Line::styled(
                    " (nothing logged yet: log(\"…\") in the tape writes here)",
                    Style::new().fg(pal.amber_dim),
                )]
            } else {
                self.console
                    .iter()
                    .rev()
                    .take(room)
                    .rev()
                    .map(|l| Line::styled(format!(" {l}"), Style::new().fg(pal.amber)))
                    .collect()
            };
            lines.push(Line::raw(""));
            lines.push(Line::styled(" esc: back to the menu", Style::new().fg(pal.amber_dim)));
            card(frame, area, pal, "CONSOLE", lines, false, 90);
        } else if let Some(sel) = run.menu {
            let title = self.entries.get(run.entry).map(Entry::title).unwrap_or_default();
            let mut lines = vec![Line::styled(format!(" {title}"), bold(pal.beige)), Line::raw("")];
            for (i, item) in MENU.iter().enumerate() {
                let style = if i == sel { bold(pal.bg).bg(pal.amber) } else { Style::new().fg(pal.amber) };
                lines.push(Line::styled(format!("  {item}  "), style));
            }
            lines.push(Line::raw(""));
            lines.push(Line::styled(" ctrl+] or esc: resume", Style::new().fg(pal.amber_dim)));
            card(frame, area, pal, "▌▌ PAUSED", lines, false, 46);
        } else if let Some(e) = &run.crashed {
            let lines = vec![
                Line::styled(" The tape stopped:", bold(pal.beige)),
                Line::raw(""),
                Line::styled(format!(" {e}"), Style::new().fg(pal.amber)),
                Line::raw(""),
                Line::styled(
                    " enter: back to the library (the console is on its card)",
                    Style::new().fg(pal.amber_dim),
                ),
            ];
            card(frame, area, pal, "TAPE CRASHED", lines, true, 46);
        }
    }
}

/// A centred card over the game (the pause menu, the console, the crash), `width` wide at most.
pub(super) fn card(
    frame: &mut Frame,
    area: Rect,
    pal: &Palette,
    title: &str,
    lines: Vec<Line>,
    error: bool,
    width: u16,
) {
    let w = width.min(area.width);
    let h = (lines.len() as u16 + 2).min(area.height);
    let r = Rect { x: area.x + (area.width - w) / 2, y: area.y + (area.height - h) / 2, width: w, height: h };
    frame.render_widget(Clear, r);
    let colour = if error { pal.red } else { pal.amber };
    let block = Block::bordered()
        .title(Span::styled(format!(" {title} "), bold(colour)))
        .border_style(Style::new().fg(colour))
        .style(Style::new().bg(pal.panel));
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }).block(block), r);
}

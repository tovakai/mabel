//! MABEL-OS: Mabel in a terminal, the Rust port of tui/ (Python + Textual).
//!
//! Run: `cargo run --release` in tui-rs/ (reads the same ~/.config/mabel-tui/config.toml).

// The API client and config are ahead of the channels that will use them (phase 2 on).
#![allow(dead_code)]

mod api;
mod app;
mod channel;
mod config;
mod help;
mod keys;
mod nav;
mod palette;
mod ui;

use std::io::stdout;
use std::time::Duration;

use anyhow::Result;
use crossterm::event::{
    DisableMouseCapture, EnableMouseCapture, EventStream, KeyboardEnhancementFlags, PopKeyboardEnhancementFlags,
    PushKeyboardEnhancementFlags,
};
use crossterm::execute;
use futures::StreamExt;
use ratatui::DefaultTerminal;
use tokio::sync::mpsc;

use app::App;

#[tokio::main]
async fn main() -> Result<()> {
    let cfg = match config::load(&config::config_path()) {
        Ok(cfg) => cfg,
        Err(e) => {
            eprintln!("{e:#}");
            std::process::exit(1);
        }
    };
    let (tx, rx) = mpsc::unbounded_channel();
    let mut app = App::new(cfg, tx)?;
    app.start();

    let mut terminal = enter();
    let result = run(&mut terminal, &mut app, rx).await;
    leave();
    result
}

/// Raw mode, the alternate screen, mouse, and (where the terminal has it) the kitty keyboard
/// protocol, so esc is instant and ctrl+i isn't tab. The Linux console on the deck has no such
/// protocol and gets plain keys.
fn enter() -> DefaultTerminal {
    let terminal = ratatui::init(); // also restores the terminal if we panic
    let _ = execute!(stdout(), EnableMouseCapture);
    if crossterm::terminal::supports_keyboard_enhancement().unwrap_or(false) {
        let _ = execute!(stdout(), PushKeyboardEnhancementFlags(KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES));
    }
    let hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let _ = execute!(stdout(), PopKeyboardEnhancementFlags, DisableMouseCapture);
        hook(info);
    }));
    terminal
}

fn leave() {
    let _ = execute!(stdout(), PopKeyboardEnhancementFlags, DisableMouseCapture);
    ratatui::restore();
}

async fn run(
    terminal: &mut DefaultTerminal,
    app: &mut App,
    mut rx: mpsc::UnboundedReceiver<channel::Msg>,
) -> Result<()> {
    let mut events = EventStream::new();
    let mut tick = tokio::time::interval(Duration::from_millis(500)); // the clock, toasts running out
    loop {
        terminal.draw(|f| app.render(f))?;
        if std::mem::take(&mut app.redraw) {
            terminal.draw(|f| app.render(f))?;
        }
        if app.quit {
            return Ok(());
        }
        if std::mem::take(&mut app.shell) {
            drop(events); // its reader would steal the shell's keystrokes
            shell();
            *terminal = enter();
            events = EventStream::new();
            continue;
        }
        tokio::select! {
            Some(Ok(event)) = events.next() => app.on_event(event),
            _ = tick.tick() => app.tick(),
            Some(msg) = rx.recv() => app.on_msg(msg),
        }
    }
}

/// ctrl+z: the whole screen becomes a plain shell; exit comes back to MABEL-OS.
fn shell() {
    leave();
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/bash".into());
    println!("\x1b[33mMABEL-OS is paused — type exit to come back\x1b[0m");
    let _ = std::process::Command::new(shell).current_dir(config::home()).status();
}

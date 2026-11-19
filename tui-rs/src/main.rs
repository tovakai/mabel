//! MABEL-OS: Mabel in a terminal, the Rust port of tui/ (Python + Textual).
//!
//! Run: `cargo run --release` in tui-rs/ (reads the same ~/.config/mabel-tui/config.toml).

// The API client and config are ahead of the channels that will use them (until phase 5).
#![allow(dead_code)]

use std::path::PathBuf;

mod api;
mod app;
mod audio;
mod avatar;
mod browser;
mod channel;
mod comms;
mod config;
mod dtape;
mod help;
mod housekeeping;
mod inbox;
mod inventory;
mod keys;
mod life;
mod mpris;
mod music;
mod nav;
mod newtape;
mod offline;
mod palette;
mod pixelview;
pub use mabel_media::{pixel, tracker, tracksynth};
mod player;
mod printserver;
mod suite;
mod sysinfo;
mod sysview;
mod tapecli;
mod tapes;
mod term;
mod timefmt;
mod tkindex;
mod trackerview;
mod tuner;
mod ui;
mod update;
mod vault;
mod vaultview;
mod widgets;

use std::io::stdout;
use std::time::Duration;

use anyhow::Result;
use crossterm::event::{
    DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture, EventStream,
    KeyboardEnhancementFlags, PopKeyboardEnhancementFlags, PushKeyboardEnhancementFlags,
};
use crossterm::execute;
use futures::StreamExt;
use ratatui::DefaultTerminal;
use tokio::sync::mpsc;

use app::App;

#[tokio::main]
async fn main() -> Result<()> {
    // `mabel-os print-server`: the deck's print server (mabel-print.service), not the TUI.
    if std::env::args().nth(1).as_deref() == Some("print-server") {
        return tokio::task::spawn_blocking(printserver::run).await?;
    }
    // `mabel-os tape publish|pack|list|install|remove|unpublish`: the DreamTape repo.
    if std::env::args().nth(1).as_deref() == Some("tape") {
        let args: Vec<String> = std::env::args().skip(2).collect();
        return tapecli::run(&args).await;
    }
    // `mabel-os new-tape snake`: a new DreamTape project in the current folder.
    if std::env::args().nth(1).as_deref() == Some("new-tape") {
        let args: Vec<String> = std::env::args().skip(2).collect();
        return newtape::run(&args);
    }
    if let Ok(pid) = std::env::var(PAUSED_ENV) {
        eprintln!(
            "This shell belongs to a paused MABEL-OS (pid {pid}).\nType exit to go back to it, instead of starting a second one."
        );
        std::process::exit(1);
    }
    if std::env::var_os(term::INSIDE_ENV).is_some() {
        eprintln!("You're in a shell inside MABEL-OS's TERM. ctrl+] steps out of it; alt+1–8 switch channels.");
        std::process::exit(1);
    }
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
    app.player.stop();
    leave();
    if app.restart && result.is_ok() {
        // The same program from disk again: a freshly copied build takes over here.
        use std::os::unix::process::CommandExt;
        // After an update the old file was renamed over: Linux then reports "<path> (deleted)",
        // and the path itself holds the new build.
        let exe = std::env::current_exe()?;
        let exe = exe.to_str().and_then(|p| p.strip_suffix(" (deleted)")).map_or(exe.clone(), PathBuf::from);
        let err = std::process::Command::new(exe).args(std::env::args_os().skip(1)).exec();
        return Err(err.into());
    }
    result
}

/// Raw mode, the alternate screen, mouse, and (where the terminal has it) the kitty keyboard
/// protocol, so esc is instant and ctrl+i isn't tab. The Linux console on the deck has no such
/// protocol and gets plain keys.
fn enter() -> DefaultTerminal {
    let terminal = ratatui::init(); // also restores the terminal if we panic
    let _ = execute!(stdout(), EnableMouseCapture, EnableBracketedPaste);
    if crossterm::terminal::supports_keyboard_enhancement().unwrap_or(false) {
        KITTY_KEYS.store(true, std::sync::atomic::Ordering::Relaxed);
        let _ = execute!(stdout(), PushKeyboardEnhancementFlags(KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES));
    }
    let hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let _ = execute!(stdout(), PopKeyboardEnhancementFlags, DisableMouseCapture, DisableBracketedPaste);
        hook(info);
    }));
    terminal
}

/// The terminal speaks the kitty keyboard protocol (kitty, foot): key releases can be had.
static KITTY_KEYS: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Ask for key releases (a running tape's held buttons) or stop asking. Returns the new state.
fn key_releases(on: bool, now: bool) -> bool {
    if on == now || !KITTY_KEYS.load(std::sync::atomic::Ordering::Relaxed) {
        return now && KITTY_KEYS.load(std::sync::atomic::Ordering::Relaxed);
    }
    let _ = if on {
        execute!(
            stdout(),
            PushKeyboardEnhancementFlags(
                KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES | KeyboardEnhancementFlags::REPORT_EVENT_TYPES
            )
        )
    } else {
        execute!(stdout(), PopKeyboardEnhancementFlags)
    };
    on
}

fn leave() {
    let _ = execute!(stdout(), PopKeyboardEnhancementFlags, DisableMouseCapture, DisableBracketedPaste);
    ratatui::restore();
}

async fn run(
    terminal: &mut DefaultTerminal,
    app: &mut App,
    mut rx: mpsc::UnboundedReceiver<channel::Msg>,
) -> Result<()> {
    let mut events = EventStream::new();
    let mut tick = tokio::time::interval(Duration::from_millis(500)); // the clock, toasts running out
    let mut releases = false;
    loop {
        terminal.draw(|f| app.render(f))?;
        if std::mem::take(&mut app.redraw) {
            terminal.draw(|f| app.render(f))?;
        }
        if app.quit {
            key_releases(false, releases);
            return Ok(());
        }
        releases = key_releases(app.wants_key_releases() && !app.shell, releases);
        if std::mem::take(&mut app.shell) {
            drop(events); // its reader would steal the shell's keystrokes
            shell();
            *terminal = enter();
            events = EventStream::new();
            continue;
        }
        // An animation frame or spinner wants drawing at this time, input or not.
        let wake = app.next_wake().map(tokio::time::Instant::from_std);
        tokio::select! {
            Some(Ok(event)) = events.next() => app.on_event(event),
            _ = tick.tick() => app.tick(),
            Some(msg) = rx.recv() => app.on_msg(msg),
            _ = sleep_until(wake) => {}
        }
        // A reply streams in as many small chunks: take all that are waiting, then draw once.
        while let Ok(msg) = rx.try_recv() {
            app.on_msg(msg);
        }
    }
}

async fn sleep_until(when: Option<tokio::time::Instant>) {
    match when {
        Some(t) => tokio::time::sleep_until(t).await,
        None => std::future::pending().await,
    }
}

/// Set in the ctrl+z shell: MABEL-OS started from there would be a second one inside the first
/// (two players, two songs at once), so it says where to go instead.
const PAUSED_ENV: &str = "MABEL_OS_PAUSED";

/// ctrl+z: the whole screen becomes a plain shell; exit comes back to MABEL-OS.
fn shell() {
    leave();
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/bash".into());
    println!("\x1b[33mMABEL-OS is paused — type exit to come back\x1b[0m");
    let _ = std::process::Command::new(shell)
        .current_dir(config::home())
        .env(PAUSED_ENV, std::process::id().to_string())
        .status();
}

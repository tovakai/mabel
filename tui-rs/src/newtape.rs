//! `mabel-os new-tape <name> [--kit <path>]`: a new DreamTape project in the current folder.
//!
//! It writes a starter game that already walks a sprite around to music, the sprite
//! (`art/hero.mpx`, open it in PIXEL) and two songs (`music/theme.mtk` and
//! `music/blips.mtk`, open them in TRACKER), then adds the folder to `tapes_dev` so
//! TAPES lists it. kit comes from GitHub, pinned to a tag; `--kit` points at a local
//! checkout instead (for working on kit itself).

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};

use crate::pixel;
use crate::tracker::{self, NOTE_OFF, Song, Step};

/// The kit release new tapes are made with.
pub const KIT_TAG: &str = "kit-v0.2";
const KIT_GIT: &str = "https://github.com/tovakai/mabel";

pub fn run(args: &[String]) -> Result<()> {
    let mut name = None;
    let mut kit_path = None;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--kit" => kit_path = Some(it.next().context("--kit needs a path to tui-rs/crates/mabel-kit")?.clone()),
            "-h" | "--help" => {
                println!("mabel-os new-tape <name> [--kit <path to mabel-kit>]");
                return Ok(());
            }
            other if name.is_none() => name = Some(other.to_string()),
            other => bail!("unexpected argument {other:?}"),
        }
    }
    let name = name.context("usage: mabel-os new-tape <name>")?;
    if name.is_empty() || !name.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        bail!("a tape name is letters, digits, - and _ (it becomes the folder and the Rust package name)");
    }
    let dir = std::env::current_dir()?.join(&name);
    if dir.exists() {
        bail!("{} already exists", dir.display());
    }
    let kit = match kit_path {
        Some(p) => {
            let p = std::fs::canonicalize(crate::config::expand(&p)).with_context(|| format!("--kit {p}"))?;
            format!("mabel-kit = {{ path = \"{}\" }}", p.display())
        }
        None => format!("mabel-kit = {{ git = \"{KIT_GIT}\", tag = \"{KIT_TAG}\" }}"),
    };
    create(&dir, &name, &kit)?;
    let registered = register(&dir)?;

    println!("\x1b[33m◘ DreamTape \"{name}\" is ready in {}\x1b[0m\n", dir.display());
    println!("  cd {name}");
    println!(
        "  cargo tape            # builds it (an alias for: cargo build --release --target wasm32-unknown-unknown)"
    );
    println!();
    println!("  Then open TAPES (F9 / alt+9) in MABEL-OS and press enter on it.");
    println!("  Leave it running: every `cargo tape` restarts it with your new code.");
    println!("  `cargo run --release` plays it on its own, in any terminal (ctrl+q quits).");
    println!("  `mabel-os tape publish` puts it on Mabel, so the deck can install it.");
    println!("  art/hero.mpx opens in PIXEL, music/*.mtk in TRACKER.");
    match registered {
        Registered::Added(cfg) => println!("\n  (Added to tapes_dev in {}.)", cfg.display()),
        Registered::Already => {}
        Registered::Manual(cfg) => println!(
            "\n  Add it to tapes_dev in {} so TAPES lists it:\n    tapes_dev = [..., \"{}\"]",
            cfg.display(),
            dir.display()
        ),
    }
    Ok(())
}

fn create(dir: &Path, name: &str, kit: &str) -> Result<()> {
    std::fs::create_dir_all(dir.join("src"))?;
    std::fs::create_dir_all(dir.join(".cargo"))?;
    std::fs::write(
        dir.join("Cargo.toml"),
        format!(
            r#"[package]
name = "{name}"
version = "0.1.0"
edition = "2024"

[dependencies]
{kit}

# Small, fast tapes.
[profile.release]
opt-level = "s"
lto = true
"#
        ),
    )?;
    std::fs::write(
        dir.join(".cargo/config.toml"),
        "# `cargo tape` builds the DreamTape (TAPES runs target/wasm32-unknown-unknown/release/*.wasm).\n\
         [alias]\ntape = \"build --release --target wasm32-unknown-unknown\"\n",
    )?;
    std::fs::write(dir.join(".gitignore"), "/target\n")?;
    std::fs::write(
        dir.join("tape.toml"),
        format!(
            r#"title = "{name}"
author = ""
description = "A brand new DreamTape."
cover = "art/hero.mpx"
screen = "fill"     # or "128x72"
fps = 30
"#
        ),
    )?;
    std::fs::write(dir.join("src/main.rs"), STARTER)?;
    hero().save_to(&dir.join("art/hero.mpx"))?;
    let mut theme = tracker::demo();
    theme.title = format!("{name} theme");
    tracker::save(&mut theme, Some(&dir.join("music/theme.mtk")))?;
    tracker::save(&mut blips(), Some(&dir.join("music/blips.mtk")))?;
    Ok(())
}

/// The starter game: read it top to bottom, it's meant to be changed.
const STARTER: &str = r#"//! A DreamTape. TAPES calls `init` once, then `update` and `draw` every frame.
//! Build it with `cargo tape`; TAPES restarts it whenever you rebuild.

use mabel_kit::*;

/// Everything the game remembers between frames.
struct Game {
    x: f32,
    y: f32,
    facing_left: bool,
    walking: bool,
    hero: Sprite,
    /// How many times this tape has been started (kept with save/load).
    plays: u32,
}

/// How fast the hero walks, in pixels per second.
const SPEED: f32 = 30.0;

impl Tape for Game {
    fn init() -> Self {
        music("theme"); // music/theme.mtk loops in the background

        // `load` gives back text (or None the very first time); `parse` turns it into a number.
        let plays: u32 = load("plays").and_then(|s| s.parse().ok()).unwrap_or(0) + 1;
        save("plays", &plays.to_string());

        Game {
            x: 20.0,
            y: 20.0,
            facing_left: false,
            walking: false,
            hero: sprite("hero"), // art/hero.mpx
            plays,
        }
    }

    // `dt` is the time since the last frame in seconds, so the speed is the same at any fps.
    fn update(&mut self, dt: f32) {
        let (mut dx, mut dy) = (0.0, 0.0);
        if btn(Btn::Left) {
            dx -= 1.0;
        }
        if btn(Btn::Right) {
            dx += 1.0;
        }
        if btn(Btn::Up) {
            dy -= 1.0;
        }
        if btn(Btn::Down) {
            dy += 1.0;
        }
        self.walking = dx != 0.0 || dy != 0.0;
        if dx != 0.0 {
            self.facing_left = dx < 0.0;
        }
        self.x += dx * SPEED * dt;
        self.y += dy * SPEED * dt;

        // Stay on the screen. `clamp` keeps a number between a lowest and a highest value.
        self.x = self.x.clamp(0.0, (width() - self.hero.w) as f32);
        self.y = self.y.clamp(0.0, (height() - self.hero.h) as f32);

        if btnp(Btn::A) {
            sfx("blips", 0); // phrase 00 of music/blips.mtk, played once
        }
    }

    fn draw(&self) {
        cls(BG);
        // A floor line along the bottom.
        line(0, height() - 1, width() - 1, height() - 1, AMBER_DIM);

        // Two animation frames, swapped five times a second while walking.
        let frame = if self.walking { (time() * 5.0) as i32 % 2 } else { 0 };
        spr_ex(&self.hero, frame, self.x as i32, self.y as i32, self.facing_left, false);

        print(1, 0, "◘ my first DreamTape", AMBER);
        print(1, 1, "arrows / wasd: walk   z: blip   ctrl+]: pause", AMBER_DIM);

        // The pixel font can go anywhere, even right under the hero's feet.
        let label = format!("play {}", self.plays);
        pprint(self.x as i32 + 4 - text_width(&label) / 2, self.y as i32 + 10, &label, AMBER_DIM);
    }
}

tape!(Game);
"#;

/// The starter sprite: a little cassette robot, two walking frames, 8×8.
fn hero() -> Hero {
    const FRAMES: [[&str; 8]; 2] = [
        ["..aaaa..", ".adddda.", ".aw..wa.", ".adddda.", ".aaaaaa.", "..a..a..", "..l..l..", ".ll..ll."],
        ["..aaaa..", ".adddda.", ".aw..wa.", ".adddda.", ".aaaaaa.", "..a..a..", ".l....l.", ".l....ll"],
    ];
    Hero(FRAMES)
}

struct Hero([[&'static str; 8]; 2]);

impl Hero {
    fn save_to(&self, path: &Path) -> Result<PathBuf> {
        let mut doc = pixel::Doc::new(8, 8, "db32");
        let colour = |c: char| -> pixel::Px {
            match c {
                'a' => Some([0xff, 0xb0, 0x00]),
                'd' => Some([0x3a, 0x2a, 0x1a]),
                'w' => Some([0xe8, 0xd8, 0xb0]),
                'l' => Some([0x8a, 0x5e, 0x00]),
                _ => None,
            }
        };
        for (f, rows) in self.0.iter().enumerate() {
            if f > 0 {
                doc.add_frame(f - 1, false);
            }
            for (y, row) in rows.iter().enumerate() {
                for (x, c) in row.chars().enumerate() {
                    doc.frames[f].cels[0][y][x] = colour(c);
                }
            }
        }
        doc.adopt_colors();
        pixel::save(&mut doc, Some(path))
    }
}

/// Sound effects: phrase 00 a rising blip, phrase 01 a low bonk. Fast, so they're short.
fn blips() -> Song {
    let mut song = Song { title: "blips".into(), bpm: 240, ..Default::default() };
    for (i, note) in [72, 79, 84, NOTE_OFF].into_iter().enumerate() {
        song.phrase(0)[i] = Step { note: Some(note), inst: (note != NOTE_OFF).then_some(0), ..Default::default() };
    }
    for (i, note) in [43, 36, NOTE_OFF].into_iter().enumerate() {
        song.phrase(1)[i * 2] = Step { note: Some(note), inst: (note != NOTE_OFF).then_some(2), ..Default::default() };
    }
    song
}

enum Registered {
    Added(PathBuf),
    Already,
    Manual(PathBuf),
}

/// Add `dir` to `tapes_dev` in config.toml. Only when the key isn't there yet (a new line at
/// the top, before any [section]), so the file's comments and layout stay as they are.
fn register(dir: &Path) -> Result<Registered> {
    let path = crate::config::config_path();
    let Ok(text) = std::fs::read_to_string(&path) else { return Ok(Registered::Manual(path)) };
    let table: toml::Table = text.parse().unwrap_or_default();
    let shown = dir.display().to_string();
    match table.get("tapes_dev") {
        Some(v) if v.as_array().is_some_and(|a| a.iter().any(|x| x.as_str() == Some(shown.as_str()))) => {
            Ok(Registered::Already)
        }
        Some(_) => Ok(Registered::Manual(path)),
        None => {
            let line = format!(
                "# DreamTapes I'm making: TAPES lists them and restarts one when it's rebuilt.\ntapes_dev = [{}]\n\n",
                toml::Value::from(shown)
            );
            std::fs::write(&path, format!("{line}{text}"))?;
            Ok(Registered::Added(path))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_new_tape_has_everything_it_needs() {
        let dir = std::env::temp_dir().join(format!("mabel-new-tape-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        create(&dir, "snake", "mabel-kit = { path = \"/nowhere\" }").unwrap();
        for f in [
            "Cargo.toml",
            "tape.toml",
            "src/main.rs",
            ".cargo/config.toml",
            "art/hero.mpx",
            "music/theme.mtk",
            "music/blips.mtk",
        ] {
            assert!(dir.join(f).exists(), "{f} missing");
        }
        let doc = pixel::open_any(&dir.join("art/hero.mpx")).unwrap();
        assert_eq!((doc.w, doc.h, doc.frames.len()), (8, 8, 2));
        let blips = tracker::load(&dir.join("music/blips.mtk")).unwrap();
        assert!(blips.phrases.contains_key(&0) && blips.phrases.contains_key(&1));
        let info = mabel_console::tapefile::TapeInfo::read(&dir).unwrap();
        assert_eq!(info.cover.as_deref(), Some("art/hero.mpx"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}

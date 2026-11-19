//! `mabel-os tape …`: my DreamTape repo from the command line.
//!
//! ```text
//! mabel-os tape publish [folder]     pack the tape (built with `cargo tape`) and put it on Mabel
//! mabel-os tape pack [folder]        just make <name>.dtape here (one file to copy anywhere)
//! mabel-os tape list                 the tapes on Mabel, and which are installed here
//! mabel-os tape install <name>       from Mabel (or a .dtape file) into ~/.local/share/mabel-os/tapes
//! mabel-os tape remove <name>        uninstall it from this device
//! mabel-os tape unpublish <name>     take it off Mabel
//! ```

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};

use crate::api::Mabel;
use crate::tapes::{Remote, dev_wasm, install, installed_dir};

const HELP: &str = "mabel-os tape publish [folder]   pack the tape (cargo tape first) and put it on Mabel
mabel-os tape pack [folder]      make <name>.dtape here (one file you can copy anywhere)
mabel-os tape list               the tapes on Mabel, and which are installed here
mabel-os tape install <name>     install from Mabel, or from a .dtape file
mabel-os tape remove <name>      uninstall it from this device
mabel-os tape unpublish <name>   take it off Mabel";

fn api() -> Result<Mabel> {
    let cfg = crate::config::load(&crate::config::config_path())?;
    Mabel::new(&cfg.url, &cfg.token, &cfg.preset)
}

/// The tape folder, its name, and the wasm to pack (a dev tape's cargo build, or tape.wasm).
fn tape_dir(arg: Option<&String>) -> Result<(PathBuf, String, PathBuf)> {
    let dir = match arg {
        Some(p) => std::fs::canonicalize(crate::config::expand(p)).with_context(|| p.clone())?,
        None => std::env::current_dir()?,
    };
    if !dir.join("tape.toml").is_file() {
        bail!("{} isn't a tape folder (no tape.toml)", dir.display());
    }
    let name = dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let wasm = if dir.join("Cargo.toml").is_file() { dev_wasm(&dir) } else { dir.join("tape.wasm") };
    Ok((dir, name, wasm))
}

fn installed_sha(name: &str) -> Option<String> {
    std::fs::read_to_string(installed_dir().join(name).join(".dtape-sha")).ok().map(|s| s.trim().to_string())
}

pub async fn run(args: &[String]) -> Result<()> {
    let arg = args.get(1);
    match args.first().map(String::as_str) {
        Some("publish") => {
            let (dir, name, wasm) = tape_dir(arg)?;
            let bytes = crate::dtape::pack(&dir, &wasm)?;
            let size = bytes.len();
            let out = api()?.tape_publish(&name, bytes).await?;
            println!(
                "\x1b[33m◘ published {name}\x1b[0m ({} KB, version {}). Every device's TAPES lists it now.",
                size / 1024,
                out["sha"].as_str().unwrap_or("?")
            );
        }
        Some("pack") => {
            let (dir, name, wasm) = tape_dir(arg)?;
            let bytes = crate::dtape::pack(&dir, &wasm)?;
            let path = std::env::current_dir()?.join(format!("{name}.dtape"));
            std::fs::write(&path, &bytes)?;
            println!("◘ {} ({} KB)", path.display(), bytes.len() / 1024);
        }
        Some("list") => {
            let tapes: Vec<Remote> = api()?.tapes().await?.iter().filter_map(Remote::from_json).collect();
            if tapes.is_empty() {
                println!("No tapes on Mabel yet. In a tape's folder: mabel-os tape publish");
            }
            for t in tapes {
                let here = match installed_sha(&t.name) {
                    Some(sha) if sha == t.sha => "installed",
                    Some(_) => "installed, newer on Mabel",
                    None => "",
                };
                println!("  ◘ {:<20} {:<32} {here}", t.name, t.info.title);
            }
        }
        Some("install") => {
            let what = arg.context("mabel-os tape install <name | file.dtape>")?;
            let file = Path::new(what);
            let (name, bytes) = if what.ends_with(".dtape") && file.is_file() {
                let name = file.file_stem().unwrap_or_default().to_string_lossy().into_owned();
                (name, std::fs::read(file)?)
            } else {
                (what.clone(), api()?.tape_download(what).await?)
            };
            let dir = install(&name, &bytes)?;
            println!("\x1b[33m◘ {name} installed\x1b[0m in {}. It's in TAPES (r refreshes).", dir.display());
        }
        Some("remove") => {
            let name = arg.context("mabel-os tape remove <name>")?;
            let dir = installed_dir().join(name);
            if name.contains('/') || name.starts_with('.') || !dir.is_dir() {
                bail!("{name} isn't installed here");
            }
            std::fs::remove_dir_all(&dir)?;
            println!("◘ {name} removed from this device (Mabel still has it, if it was published).");
        }
        Some("unpublish") => {
            let name = arg.context("mabel-os tape unpublish <name>")?;
            api()?.tape_unpublish(name).await?;
            println!("◘ {name} is off Mabel (devices that installed it keep their copy).");
        }
        _ => println!("{HELP}"),
    }
    Ok(())
}

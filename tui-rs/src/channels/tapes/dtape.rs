//! `.dtape`: a whole DreamTape in one file, a plain tar archive.
//!
//! ```text
//! tape.toml
//! tape.wasm        (a dev tape's cargo build, renamed)
//! art/*.mpx|png
//! music/*.mtk
//! ```
//!
//! Nothing else goes in, and unpacking refuses anything else (Mabel checks the same on publish).

use std::io::Read;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};

pub const MAX_BYTES: usize = 32 * 1024 * 1024;

/// May `name` be in a tape? (tape.toml, tape.wasm, `art/<file>.mpx|png`, `music/<file>.mtk`)
pub fn allowed(name: &str) -> bool {
    let plain = |f: &str| !f.is_empty() && f.chars().all(|c| c.is_ascii_alphanumeric() || "_.-".contains(c));
    match name.split_once('/') {
        None => name == "tape.toml" || name == "tape.wasm",
        Some(("art", f)) => plain(f) && (f.ends_with(".mpx") || f.ends_with(".png")),
        Some(("music", f)) => plain(f) && f.ends_with(".mtk"),
        _ => false,
    }
}

/// Pack the tape in `dir`. `wasm` is its built wasm (a dev tape's cargo output, or tape.wasm).
pub fn pack(dir: &Path, wasm: &Path) -> Result<Vec<u8>> {
    if !dir.join("tape.toml").is_file() {
        bail!("{} has no tape.toml", dir.display());
    }
    let wasm_bytes = std::fs::read(wasm).with_context(|| format!("{}: not built yet? (cargo tape)", wasm.display()))?;
    let mut tar = tar::Builder::new(Vec::new());
    let mut add = |name: &str, data: &[u8]| -> Result<()> {
        let mut head = tar::Header::new_ustar();
        head.set_size(data.len() as u64);
        head.set_mode(0o644);
        head.set_mtime(0); // the same tape packs to the same bytes
        head.set_cksum();
        tar.append_data(&mut head, name, data)?;
        Ok(())
    };
    add("tape.toml", &std::fs::read(dir.join("tape.toml"))?)?;
    add("tape.wasm", &wasm_bytes)?;
    for folder in ["art", "music"] {
        let mut files: Vec<PathBuf> = std::fs::read_dir(dir.join(folder))
            .map(|rd| rd.flatten().map(|e| e.path()).filter(|p| p.is_file()).collect())
            .unwrap_or_default();
        files.sort();
        for path in files {
            let name = format!("{folder}/{}", path.file_name().unwrap_or_default().to_string_lossy());
            if allowed(&name) {
                add(&name, &std::fs::read(&path)?)?;
            }
        }
    }
    let bytes = tar.into_inner()?;
    if bytes.len() > MAX_BYTES {
        bail!("the tape is {} MB; a tape is at most {} MB", bytes.len() / (1024 * 1024), MAX_BYTES / (1024 * 1024));
    }
    Ok(bytes)
}

/// Unpack a .dtape into `dest` (made fresh: an old copy is replaced in one rename at the end).
pub fn unpack(bytes: &[u8], dest: &Path) -> Result<()> {
    if bytes.len() > MAX_BYTES {
        bail!("a tape is at most {} MB", MAX_BYTES / (1024 * 1024));
    }
    let parent = dest.parent().context("no folder to install into")?;
    std::fs::create_dir_all(parent)?;
    let tmp = parent.join(format!(".{}.unpacking", dest.file_name().unwrap_or_default().to_string_lossy()));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp)?;
    let result = (|| -> Result<()> {
        let mut archive = tar::Archive::new(bytes);
        let (mut toml, mut wasm) = (false, false);
        for entry in archive.entries()? {
            let mut entry = entry?;
            let name = entry.path()?.to_string_lossy().into_owned();
            let kind = entry.header().entry_type();
            if kind.is_dir() && matches!(name.trim_end_matches('/'), "art" | "music") {
                continue;
            }
            if !kind.is_file() || !allowed(&name) {
                bail!("not allowed in a tape: {name}");
            }
            let mut data = Vec::new();
            entry.read_to_end(&mut data)?;
            let path = tmp.join(&name);
            if let Some(dir) = path.parent() {
                std::fs::create_dir_all(dir)?;
            }
            std::fs::write(path, data)?;
            toml |= name == "tape.toml";
            wasm |= name == "tape.wasm";
        }
        if !toml || !wasm {
            bail!("the tape is missing {}", if toml { "tape.wasm" } else { "tape.toml" });
        }
        Ok(())
    })();
    if let Err(e) = result {
        let _ = std::fs::remove_dir_all(&tmp);
        return Err(e);
    }
    let _ = std::fs::remove_dir_all(dest);
    std::fs::rename(&tmp, dest)?;
    Ok(())
}

/// The first 16 hex digits of the tape's SHA-256 (what Mabel lists; tells versions apart).
pub fn sha16(bytes: &[u8]) -> String {
    use sha2::Digest;
    sha2::Sha256::digest(bytes).iter().take(8).map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_tape_files_are_allowed() {
        for ok in ["tape.toml", "tape.wasm", "art/hero.mpx", "art/cover.png", "music/theme.mtk"] {
            assert!(allowed(ok), "{ok}");
        }
        for bad in ["../x", "/etc/passwd", "art/../x.mpx", "art/x.sh", "music/a/b.mtk", "src/main.rs", "art/"] {
            assert!(!allowed(bad), "{bad}");
        }
    }

    #[test]
    fn pack_and_unpack_round_trip() {
        let base = std::env::temp_dir().join(format!("mabel-dtape-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let src = base.join("snake");
        std::fs::create_dir_all(src.join("art")).unwrap();
        std::fs::create_dir_all(src.join("music")).unwrap();
        std::fs::write(src.join("tape.toml"), "title = \"Snake\"\n").unwrap();
        std::fs::write(src.join("built.wasm"), b"\0asm").unwrap();
        std::fs::write(src.join("art/hero.mpx"), "{}").unwrap();
        std::fs::write(src.join("art/notes.txt"), "left out").unwrap();
        std::fs::write(src.join("music/theme.mtk"), "{}").unwrap();
        let bytes = pack(&src, &src.join("built.wasm")).unwrap();
        assert_eq!(bytes, pack(&src, &src.join("built.wasm")).unwrap()); // the same bytes every time
        let dest = base.join("installed/snake");
        unpack(&bytes, &dest).unwrap();
        assert_eq!(std::fs::read(dest.join("tape.wasm")).unwrap(), b"\0asm");
        assert!(dest.join("art/hero.mpx").exists() && dest.join("music/theme.mtk").exists());
        assert!(!dest.join("art/notes.txt").exists());
        assert!(unpack(b"rubbish", &base.join("installed/bad")).is_err());
        assert!(!base.join("installed/bad").exists());
        let _ = std::fs::remove_dir_all(&base);
    }
}

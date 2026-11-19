//! Updates pushed from the PC: MABEL-OS asks Mabel which build she serves and offers F12.
//!
//! Mabel's installer (curl …/tui/install.sh | sh) puts this binary in ~/.local/share/mabel-tui
//! and writes the build it got to `build-rs` there. Mabel's /tui/version says which Rust build
//! she serves for each architecture now (a hash of the binary in tui-rs/dist, made by
//! scripts/tui_rs_dist.sh on the PC). A copy run from the source tree has no build file and
//! never asks. F12 runs the same installer (it keeps the config), then restarts in place.

use std::path::PathBuf;
use std::time::Duration;

pub const CHECK_EVERY: Duration = Duration::from_secs(300);
pub const FIRST_CHECK: Duration = Duration::from_secs(10);
/// auto_update waits this long without a key press.
pub const IDLE_BEFORE_AUTO: Duration = Duration::from_secs(300);

/// What a check or an install came back with (sent as Msg::For("update", …)).
pub enum Note {
    /// The build Mabel serves for this architecture (None: she didn't say); `quiet` = a timed check.
    Served { build: Option<String>, quiet: bool },
    /// The installer finished: ok, and the last lines it printed.
    Installed { ok: bool, tail: String },
}

/// Where the installer puts MABEL-OS: ~/.local/share/mabel-tui.
pub fn app_dir() -> PathBuf {
    std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| crate::os::config::home().join(".local/share"))
        .join("mabel-tui")
}

/// The build this install came from; None for a copy run from source (no updates offered).
pub fn installed() -> Option<String> {
    let text = std::fs::read_to_string(app_dir().join("build-rs")).ok()?;
    Some(text.trim().to_string()).filter(|t| !t.is_empty())
}

/// Is an update on offer? Any difference counts (builds are names, not numbers); never for a
/// copy run from source.
pub fn newer(installed: Option<&str>, served: Option<&str>) -> bool {
    matches!((installed, served), (Some(i), Some(s)) if i != s)
}

/// /tui/version's answer for this machine: `{"build": …, "rs": {"x86_64": …, "aarch64": …}}`.
pub fn build_for(version: &serde_json::Value, arch: &str) -> Option<String> {
    version.get("rs")?.get(arch)?.as_str().filter(|s| !s.is_empty()).map(str::to_string)
}

/// Ask Mabel which build she serves for this machine's CPU (None if she can't be reached).
pub async fn served(base_url: String) -> Option<String> {
    let client = reqwest::Client::builder().timeout(Duration::from_secs(15)).build().ok()?;
    let resp = client.get(format!("{}/tui/version", base_url.trim_end_matches('/'))).send().await.ok()?;
    if !resp.status().is_success() {
        return None;
    }
    build_for(&resp.json().await.ok()?, std::env::consts::ARCH)
}

/// Run Mabel's installer again. Returns (ok, the last lines it printed).
pub async fn apply(base_url: String) -> (bool, String) {
    let url = format!("{}/tui/install.sh", base_url.trim_end_matches('/'));
    let fetch = if crate::channels::sys::sysinfo::have("curl") {
        format!("curl -fsSL '{url}'")
    } else {
        format!("wget -qO- '{url}'")
    };
    let run = tokio::process::Command::new("sh")
        .args(["-c", &format!("{fetch} | sh")])
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true)
        .output();
    match tokio::time::timeout(Duration::from_secs(900), run).await {
        Ok(Ok(out)) => {
            let text = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
            let lines: Vec<&str> = text.trim().lines().collect();
            let tail = lines[lines.len().saturating_sub(4)..].join("\n");
            (out.status.success(), strip_ansi(&tail))
        }
        Ok(Err(e)) => (false, e.to_string()),
        Err(_) => (false, "the installer took over 15 minutes".into()),
    }
}

/// The installer colours its lines when it has a terminal; a toast wants them plain.
fn strip_ansi(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        if c == '\x1b' {
            for c in chars.by_ref() {
                if c.is_ascii_alphabetic() {
                    break;
                }
            }
        } else {
            out.push(c);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_an_installed_copy_with_another_build_is_behind() {
        assert!(newer(Some("a"), Some("b")));
        assert!(!newer(Some("a"), Some("a")));
        assert!(!newer(None, Some("b")), "run from source: never asks");
        assert!(!newer(Some("a"), None), "Mabel didn't say");
    }

    #[test]
    fn the_build_is_picked_by_architecture() {
        let v = serde_json::json!({"build": "py", "rs": {"x86_64": "x1", "aarch64": "a1"}});
        assert_eq!(build_for(&v, "aarch64").as_deref(), Some("a1"));
        assert_eq!(build_for(&v, "riscv64"), None);
        assert_eq!(build_for(&serde_json::json!({"build": "py"}), "x86_64"), None, "an older Mabel");
    }

    #[test]
    fn colours_are_stripped() {
        assert_eq!(strip_ansi("\x1b[38;2;255;176;0m▌\x1b[0m done"), "▌ done");
    }
}

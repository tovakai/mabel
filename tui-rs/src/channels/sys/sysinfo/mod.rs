//! SYS's backend: the machine MABEL-OS runs on, through the usual Linux tools.
//!
//! Everything acts on *this* machine (the deck, the laptop, the PC) via NetworkManager (nmcli),
//! BlueZ (bluetoothctl), PipeWire (wpctl) or ALSA (amixer), systemd (timedatectl, localectl,
//! systemctl) and /proc and /sys. Commands that need root are tried with `sudo -n` (the deck has
//! passwordless sudo); where that isn't allowed the caller gets an honest error to show.
//! These functions block: SYS runs them on a background thread. Parsers take command output as
//! text, so they're tested against real output.

use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

// One file per subject; everything is re-exported, so callers just use `sysinfo::…`.
/// sound and brightness
mod audio;
/// Bluetooth
mod bluetooth;
/// STATUS's facts
mod status;
/// date, time, locale
mod time;
/// Wi-Fi and wired networks
mod wifi;

pub use audio::*;
pub use bluetooth::*;
pub use status::*;
pub use time::*;
pub use wifi::*;

const TIMEOUT: Duration = Duration::from_secs(8);

/// (exit code, stdout+stderr). Never fails: a missing tool is 127, a timeout 124.
pub fn run(args: &[&str], timeout: Duration, sudo: bool, input: Option<&str>) -> (i32, String) {
    let mut argv: Vec<&str> = vec![];
    if sudo && !is_root() {
        argv.extend(["sudo", "-n"]);
    }
    argv.extend(args);
    let mut cmd = Command::new(argv[0]);
    cmd.args(&argv[1..]).env("LC_ALL", "C").stdout(Stdio::piped()).stderr(Stdio::piped());
    cmd.stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() });
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(_) => return (127, format!("{} isn't installed", argv[0])),
    };
    if let (Some(text), Some(mut stdin)) = (input, child.stdin.take()) {
        let _ = stdin.write_all(text.as_bytes());
    }
    // Read both pipes on threads, so a chatty command can't fill a pipe and stall.
    let pipe = |r: Option<Box<dyn Read + Send>>| {
        std::thread::spawn(move || {
            let mut s = String::new();
            if let Some(mut r) = r {
                let _ = r.read_to_string(&mut s);
            }
            s
        })
    };
    let out = pipe(child.stdout.take().map(|p| Box::new(p) as Box<dyn Read + Send>));
    let err = pipe(child.stderr.take().map(|p| Box::new(p) as Box<dyn Read + Send>));
    let started = Instant::now();
    let code = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status.code().unwrap_or(1),
            Ok(None) if started.elapsed() > timeout => {
                let _ = child.kill();
                let _ = child.wait();
                return (124, format!("{} took too long", argv[0]));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(e) => return (1, e.to_string()),
        }
    };
    let text = format!("{}{}", out.join().unwrap_or_default(), err.join().unwrap_or_default());
    (code, text.trim().to_string())
}

/// Run a command with the usual time limit, as ourselves: (exit code, output).
fn sh(args: &[&str]) -> (i32, String) {
    run(args, TIMEOUT, false, None)
}

/// Are we running as root? (Then sudo isn't needed.)
fn is_root() -> bool {
    read("/proc/self/status").lines().find(|l| l.starts_with("Uid:")).and_then(|l| l.split_whitespace().nth(1))
        == Some("0")
}

/// Is `tool` installed (on PATH)?
pub fn have(tool: &str) -> bool {
    std::env::var_os("PATH").map(|p| std::env::split_paths(&p).any(|d| d.join(tool).is_file())).unwrap_or(false)
}

/// A small file's text, trimmed ("" if it can't be read): /proc and /sys values.
fn read(path: impl AsRef<Path>) -> String {
    std::fs::read_to_string(path).map(|s| s.trim().to_string()).unwrap_or_default()
}

/// A command's failure in one short line (its first line; "needs root" for sudo refusals).
pub fn short_error(out: &str) -> String {
    let line = out.lines().find(|l| !l.trim().is_empty()).unwrap_or(out);
    if line.contains("a password is required") || line.contains("sudo:") {
        return "needs root (no passwordless sudo here)".into();
    }
    line.chars().take(120).collect()
}

/// The value after `key:` in a command's `key: value` output ("" if it isn't there).
fn field<'a>(out: &'a str, key: &str) -> &'a str {
    out.lines().find_map(|l| l.trim().strip_prefix(key)?.strip_prefix(':').map(str::trim)).unwrap_or("")
}

/// Try as ourselves, then with sudo -n.
fn as_admin(args: &[&str], ok: String) -> Done {
    let (mut code, mut out) = sh(args);
    if code != 0 {
        (code, out) = run(args, TIMEOUT, true, None);
    }
    done(code, ok, &out)
}

/// What an action reports: Ok(message) or Err(message), for the page to show.
pub type Done = Result<String, String>;

/// Exit code 0: Ok(`ok`). Anything else: Err(the output's short error).
fn done(code: i32, ok: impl Into<String>, out: &str) -> Done {
    if code == 0 { Ok(ok.into()) } else { Err(short_error(out)) }
}

// ── power ───────────────────────────────────────────────────────────────

/// suspend · reboot · poweroff of the whole machine.
pub fn power(action: &str) -> Done {
    as_admin(&["systemctl", action], action.into())
}

#[cfg(test)]
mod tests {
    use super::bluetooth::strip_ansi;
    use super::*;

    const WPCTL: &str = "Audio
 ├─ Devices:
 │      48. GA102 High Definition Audio Controller [alsa]
 │      49. Built-in Audio                      [alsa]
 │
 ├─ Sinks:
 │  *   55. Built-in Audio Analog Stereo        [vol: 0.45]
 │      61. HDMI Out                            [vol: 1.00]
 │
 ├─ Sources:
 │      56. Built-in Audio Analog Stereo        [vol: 1.00]
";
    const DECK_BT: &str = "Controller B8:27:EB:26:28:94 (public)\n\tName: mabel-deck\n\tPowered: no\n\tPowerState: off-blocked\n\tPairable: no\n";
    const APLAY: &str = "card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
card 1: vc4hdmi [vc4-hdmi], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
";

    #[test]
    fn audio_parsers() {
        let sinks = parse_wpctl_sinks(WPCTL);
        let got: Vec<(&str, &str, bool)> = sinks.iter().map(|o| (o.id.as_str(), o.name.as_str(), o.default)).collect();
        assert_eq!(got, vec![("55", "Built-in Audio Analog Stereo", true), ("61", "HDMI Out", false)]);
        let cards: Vec<(String, String)> = parse_alsa_cards(APLAY).into_iter().map(|c| (c.id, c.name)).collect();
        assert_eq!(cards, vec![("0".into(), "bcm2835 Headphones".into()), ("1".into(), "vc4-hdmi".into())]);
        assert_eq!(parse_amixer("  Mono: Playback 0 [96%] [0.00dB] [on]"), (Some(96), false));
        assert_eq!(parse_amixer("  Front Left: Playback 0 [40%] [-12dB] [off]"), (Some(40), true));
    }

    #[test]
    fn bluetooth_and_power_parsers() {
        let st = parse_bt_show(DECK_BT);
        assert!(st.name == "mabel-deck" && !st.powered && st.blocked && st.present);
        let devs = parse_bt_devices("Device AA:BB:CC:DD:EE:FF Keychron K2\nDevice 11:22:33:44:55:66 Buds\n");
        assert_eq!(
            devs,
            vec![("AA:BB:CC:DD:EE:FF".into(), "Keychron K2".into()), ("11:22:33:44:55:66".into(), "Buds".into())]
        );
        let d = parse_bt_info(
            "AA",
            "K2",
            "\tPaired: yes\n\tBonded: no\n\tTrusted: yes\n\tConnected: no\n\tIcon: input-keyboard\n",
        );
        assert!(d.paired && !d.bonded && d.trusted && !d.connected && d.icon == "input-keyboard");
        assert_eq!(
            decode_throttled("throttled=0x50005"),
            vec!["under-voltage now", "throttled now", "under-voltage since boot", "throttled since boot"]
        );
        assert!(decode_throttled("throttled=0x0").is_empty());
        assert_eq!(
            strip_ansi("\x1b[0;94m[bluetoothctl]\x1b[0m> [agent] PIN code: 123456\r"),
            "[bluetoothctl]> [agent] PIN code: 123456"
        );
    }

    #[test]
    fn wifi_parsers() {
        assert_eq!(split_terse(r"*:Café\:5G:72:WPA2"), vec!["*", "Café:5G", "72", "WPA2"]);
        let out =
            [" :Home:60:WPA2", "*:Home:80:WPA2", " :Open Cafe:90:", " ::40:WPA2", " :Neighbour:95:WPA3"].join("\n");
        let nets = parse_wifi_list(&out, &["Home".to_string()]);
        let names: Vec<&str> = nets.iter().map(|n| n.ssid.as_str()).collect();
        assert_eq!(names, vec!["Home", "Neighbour", "Open Cafe"], "active first, then known, then signal");
        assert!(nets[0].active && nets[0].signal == 80 && nets[0].known && nets[2].security == "open");
    }
}

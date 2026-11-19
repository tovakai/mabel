//! Date, time zone, network time, language and keyboard layout (`timedatectl`, `localectl`).

use super::*;

/// `timedatectl show`-style `Key=Value` lines → pairs.
pub fn parse_keyvalues(out: &str) -> Vec<(String, String)> {
    out.lines().filter_map(|l| l.split_once('=').map(|(k, v)| (k.into(), v.into()))).collect()
}

/// The clock's settings (time zone, NTP, synced…).
pub fn time_state() -> Vec<(String, String)> {
    parse_keyvalues(&sh(&["timedatectl", "show"]).1)
}

/// Every time zone name (Europe/Stockholm…).
pub fn timezones() -> Vec<String> {
    let (code, out) = sh(&["timedatectl", "list-timezones"]);
    if code == 0 { out.lines().map(String::from).collect() } else { vec![] }
}

/// Set the time zone (needs admin: tries sudo).
pub fn set_timezone(tz: &str) -> Done {
    as_admin(&["timedatectl", "set-timezone", tz], format!("time zone {tz}"))
}

/// Network time on or off.
pub fn set_ntp(on: bool) -> Done {
    let label = if on { "network time on" } else { "network time off" };
    as_admin(&["timedatectl", "set-ntp", if on { "true" } else { "false" }], label.into())
}

/// (locale, keyboard layout).
pub fn locale_state() -> (String, String) {
    let out = sh(&["localectl", "status"]).1;
    (field(&out, "System Locale").replace("LANG=", ""), field(&out, "X11 Layout").into())
}

/// Set the console/X keyboard layout (se, us…).
pub fn set_keyboard(layout: &str) -> Done {
    let (code, out) = run(&["localectl", "set-x11-keymap", layout], TIMEOUT, true, None);
    done(code, format!("keyboard {layout} (from the next start of MABEL-OS)"), &out)
}

//! Wi-Fi and wired networks, through NetworkManager's `nmcli`.

use super::*;

/// nmcli -t fields: ':' separates, '\:' is a literal colon (in SSIDs and MACs).
pub fn split_terse(line: &str) -> Vec<String> {
    let mut out = vec![];
    let mut cur = String::new();
    let mut esc = false;
    for c in line.chars() {
        if esc {
            cur.push(c);
            esc = false;
        } else if c == '\\' {
            esc = true;
        } else if c == ':' {
            out.push(std::mem::take(&mut cur));
        } else {
            cur.push(c);
        }
    }
    out.push(cur);
    out
}

#[derive(Debug, Clone, PartialEq)]
/// A Wi-Fi network in range.
pub struct Network {
    pub ssid: String,
    pub signal: u32,
    pub security: String,
    pub active: bool,
    pub known: bool,
}

impl Network {
    /// No password needed?
    pub fn open(&self) -> bool {
        matches!(self.security.as_str(), "open" | "--" | "")
    }
}

/// nmcli's Wi-Fi list → networks (each name once): the connected one first, then saved
/// ones (`known`), then by signal strength.
pub fn parse_wifi_list(out: &str, known: &[String]) -> Vec<Network> {
    let mut best: Vec<Network> = vec![];
    for line in out.lines() {
        let p = split_terse(line);
        if p.len() < 4 || p[1].is_empty() {
            continue;
        }
        let net = Network {
            ssid: p[1].clone(),
            signal: p[2].parse().unwrap_or(0),
            security: if p[3].is_empty() { "open".into() } else { p[3].clone() },
            active: p[0] == "*",
            known: known.contains(&p[1]),
        };
        match best.iter_mut().find(|n| n.ssid == net.ssid) {
            Some(old) if net.active || net.signal > old.signal => *old = net,
            Some(_) => {}
            None => best.push(net),
        }
    }
    best.sort_by_key(|n| (!n.active, !n.known, std::cmp::Reverse(n.signal)));
    best
}

/// (hardware enabled, software enabled).
pub fn wifi_radio() -> (bool, bool) {
    let (code, out) = sh(&["nmcli", "-t", "-f", "WIFI-HW,WIFI", "radio"]);
    let parts = if code == 0 { split_terse(out.lines().next().unwrap_or("")) } else { vec![] };
    (parts.first().is_some_and(|p| p == "enabled"), parts.get(1).is_some_and(|p| p == "enabled"))
}

/// Turn the Wi-Fi radio on or off.
pub fn set_wifi_radio(on: bool) -> Done {
    let (code, out) = sh(&["nmcli", "radio", "wifi", if on { "on" } else { "off" }]);
    done(code, if on { "Wi-Fi on" } else { "Wi-Fi off" }, &out)
}

/// The Wi-Fi networks NetworkManager has saved.
fn known_wifi() -> Vec<String> {
    let (code, out) = sh(&["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"]);
    if code != 0 {
        return vec![];
    }
    out.lines().filter(|l| l.ends_with(":802-11-wireless")).map(|l| split_terse(l).remove(0)).collect()
}

/// The networks in range now.
pub fn wifi_networks() -> Vec<Network> {
    let args = ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes"];
    let (code, out) = run(&args, Duration::from_secs(20), false, None);
    if code == 0 { parse_wifi_list(&out, &known_wifi()) } else { vec![] }
}

/// Connect to a network (a saved one needs no password).
pub fn wifi_connect(ssid: &str, password: Option<&str>) -> Done {
    let mut args = vec!["nmcli", "device", "wifi", "connect", ssid];
    if let Some(pw) = password {
        args.extend(["password", pw]);
    }
    let (code, out) = run(&args, Duration::from_secs(45), false, None);
    done(code, format!("connected to {ssid}"), &out)
}

/// Delete a saved network.
pub fn wifi_forget(ssid: &str) -> Done {
    let (code, out) = sh(&["nmcli", "connection", "delete", "id", ssid]);
    done(code, format!("forgot {ssid}"), &out)
}

/// (device, type, state) for ethernet, Wi-Fi and the tailnet.
pub fn wired() -> Vec<(String, String, String)> {
    let (code, out) = sh(&["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"]);
    if code != 0 {
        return vec![];
    }
    out.lines()
        .map(split_terse)
        .filter(|p| p.len() >= 3 && matches!(p[1].as_str(), "ethernet" | "tun" | "wifi") && p[2] != "unmanaged")
        .map(|p| (p[0].clone(), p[1].clone(), p[2].clone()))
        .collect()
}

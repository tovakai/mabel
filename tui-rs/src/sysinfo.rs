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

fn sh(args: &[&str]) -> (i32, String) {
    run(args, TIMEOUT, false, None)
}

fn is_root() -> bool {
    read("/proc/self/status").lines().find(|l| l.starts_with("Uid:")).and_then(|l| l.split_whitespace().nth(1))
        == Some("0")
}

pub fn have(tool: &str) -> bool {
    std::env::var_os("PATH").map(|p| std::env::split_paths(&p).any(|d| d.join(tool).is_file())).unwrap_or(false)
}

fn read(path: impl AsRef<Path>) -> String {
    std::fs::read_to_string(path).map(|s| s.trim().to_string()).unwrap_or_default()
}

pub fn short_error(out: &str) -> String {
    let line = out.lines().find(|l| !l.trim().is_empty()).unwrap_or(out);
    if line.contains("a password is required") || line.contains("sudo:") {
        return "needs root (no passwordless sudo here)".into();
    }
    line.chars().take(120).collect()
}

/// Ok(message) or Err(message), for the page to show.
pub type Done = Result<String, String>;

fn done(code: i32, ok: impl Into<String>, out: &str) -> Done {
    if code == 0 { Ok(ok.into()) } else { Err(short_error(out)) }
}

// ── status: a fastfetch for MABEL-OS ────────────────────────────────────

const THROTTLE_BITS: [(u32, &str); 8] = [
    (0, "under-voltage now"),
    (1, "CPU capped now"),
    (2, "throttled now"),
    (3, "too hot now"),
    (16, "under-voltage since boot"),
    (17, "capped since boot"),
    (18, "throttled since boot"),
    (19, "too hot since boot"),
];

/// `vcgencmd get_throttled` → plain words (the deck's 0x50005: under-voltage now and since boot).
pub fn decode_throttled(value: &str) -> Vec<&'static str> {
    let Some(hex) = value.split("0x").nth(1) else { return vec![] };
    let digits: String = hex.chars().take_while(|c| c.is_ascii_hexdigit()).collect();
    let Ok(bits) = u32::from_str_radix(&digits, 16) else { return vec![] };
    THROTTLE_BITS.iter().filter(|(bit, _)| bits & (1 << bit) != 0).map(|(_, t)| *t).collect()
}

fn os_name() -> String {
    read("/etc/os-release")
        .lines()
        .find_map(|l| l.strip_prefix("PRETTY_NAME=").map(|v| v.trim_matches('"').to_string()))
        .unwrap_or_else(|| "Linux".into())
}

fn cpu() -> String {
    let info = read("/proc/cpuinfo");
    for key in ["model name", "Model", "Hardware"] {
        let found = info.lines().find_map(|l| {
            let (k, v) = l.split_once(':')?;
            (k.trim() == key).then(|| v.trim().to_string())
        });
        if let Some(v) = found {
            return v.replace("(R)", "").replace("(TM)", "").split_whitespace().collect::<Vec<_>>().join(" ");
        }
    }
    std::env::consts::ARCH.into()
}

fn uptime() -> String {
    let secs = read("/proc/uptime").split_whitespace().next().and_then(|s| s.parse::<f64>().ok()).unwrap_or(0.0) as u64;
    let (d, h, m) = (secs / 86400, secs % 86400 / 3600, secs % 3600 / 60);
    format!("{}{h} h {m} min", if d > 0 { format!("{d} d ") } else { String::new() })
}

/// (used, total) in MB.
fn memory() -> (u64, u64) {
    let info = read("/proc/meminfo");
    let get = |key: &str| {
        info.lines()
            .find_map(|l| l.strip_prefix(key)?.strip_prefix(':')?.split_whitespace().next()?.parse::<u64>().ok())
            .unwrap_or(0)
    };
    let total = get("MemTotal");
    ((total - get("MemAvailable").min(total)) / 1024, total / 1024)
}

fn temperature() -> Option<f64> {
    let mut zones: Vec<PathBuf> = std::fs::read_dir("/sys/class/thermal")
        .ok()?
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.file_name().is_some_and(|n| n.to_string_lossy().starts_with("thermal_zone")))
        .collect();
    zones.sort();
    let mut best = None;
    for zone in zones {
        let kind = read(zone.join("type"));
        let Ok(t) = read(zone.join("temp")).parse::<f64>() else { continue };
        let cpu = matches!(kind.as_str(), "x86_pkg_temp" | "cpu-thermal" | "Tctl");
        if best.is_none() || cpu {
            best = Some(t / 1000.0);
            if cpu {
                break;
            }
        }
    }
    best
}

fn battery() -> Option<String> {
    let mut bats: Vec<PathBuf> = std::fs::read_dir("/sys/class/power_supply")
        .ok()?
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.file_name().is_some_and(|n| n.to_string_lossy().starts_with("BAT")))
        .collect();
    bats.sort();
    bats.into_iter().find_map(|b| {
        let cap = read(b.join("capacity"));
        let status = read(b.join("status")).to_lowercase();
        (!cap.is_empty()).then(|| format!("{cap}% ({})", if status.is_empty() { "?" } else { &status }))
    })
}

fn addresses() -> Vec<(String, String)> {
    let (code, out) = sh(&["ip", "-4", "-br", "addr"]);
    if code != 0 {
        return vec![];
    }
    out.lines()
        .filter_map(|l| {
            let parts: Vec<&str> = l.split_whitespace().collect();
            let dev = *parts.first()?;
            let skip = dev == "lo" || ["docker", "br-", "veth"].iter().any(|p| dev.starts_with(p));
            (parts.len() >= 3 && !skip).then(|| (dev.to_string(), parts[2].split('/').next().unwrap_or("").to_string()))
        })
        .collect()
}

fn gpu() -> Option<String> {
    if !have("nvidia-smi") {
        return None;
    }
    let (code, out) = sh(&["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"]);
    let parts: Vec<String> = out.lines().next()?.split(',').map(|p| p.trim().to_string()).collect();
    if code != 0 || parts.len() < 3 {
        return None;
    }
    let (used, total) = (parts[1].parse::<f64>().ok()?, parts[2].parse::<f64>().ok()?);
    Some(format!("{} · {:.1}/{:.0} GB", parts[0].replace("NVIDIA ", ""), used / 1024.0, total / 1024.0))
}

fn tailnet() -> Option<String> {
    if !have("tailscale") {
        return None;
    }
    let (_, out) = run(&["tailscale", "status", "--self", "--json"], Duration::from_secs(4), false, None);
    let v: serde_json::Value = serde_json::from_str(&out).ok()?;
    let me = &v["Self"];
    let name = me["DNSName"].as_str()?.trim_end_matches('.').split('.').next()?.to_string();
    Some(format!("{name} · {}", me["TailscaleIPs"][0].as_str()?))
}

fn disk() -> String {
    let (code, out) = sh(&["df", "-P", "-B1", "/"]);
    let line = out.lines().nth(1).unwrap_or("");
    let cols: Vec<u64> = line.split_whitespace().skip(1).take(3).filter_map(|c| c.parse().ok()).collect();
    if code != 0 || cols.len() < 3 {
        return "?".into();
    }
    format!("{:.0} / {:.0} GB", cols[1] as f64 / 1e9, cols[0] as f64 / 1e9)
}

pub struct Facts {
    pub rows: Vec<(String, String)>,
    pub warnings: Vec<String>,
}

/// The status card: one (label, value) per line, plus warnings worth a red line.
pub fn facts() -> Facts {
    let mut rows = vec![];
    let mut warnings = vec![];
    let user = std::env::var("USER").unwrap_or_else(|_| "?".into());
    let host = read("/proc/sys/kernel/hostname");
    let load = read("/proc/loadavg").split_whitespace().next().unwrap_or("?").to_string();
    let cores = std::thread::available_parallelism().map_or(1, |n| n.get());
    let (used, total) = memory();
    rows.push(("host".into(), format!("{user}@{host}")));
    rows.push(("os".into(), os_name()));
    rows.push(("kernel".into(), read("/proc/sys/kernel/osrelease")));
    rows.push(("uptime".into(), uptime()));
    rows.push(("cpu".into(), format!("{} · {cores} cores · load {load}", cpu())));
    rows.push(("memory".into(), format!("{used} / {total} MB")));
    rows.push(("disk".into(), disk()));
    if let Some(t) = temperature() {
        rows.push(("temp".into(), format!("{t:.0} °C")));
        if t >= 80.0 {
            warnings.push(format!("running hot: {t:.0} °C"));
        }
    }
    if let Some(g) = gpu() {
        rows.push(("gpu".into(), g));
    }
    if let Some(b) = battery() {
        rows.push(("battery".into(), b));
    }
    let tail = tailnet();
    for (dev, addr) in addresses() {
        if dev == "tailscale0" && tail.is_some() {
            continue; // the tailnet line below says it with the name
        }
        let label = if dev == "tailscale0" { "tailnet" } else { "net" };
        rows.push((label.into(), format!("{addr} ({dev})")));
    }
    if let Some(t) = tail {
        rows.push(("tailnet".into(), t));
    }
    if have("vcgencmd") {
        let problems = decode_throttled(&sh(&["vcgencmd", "get_throttled"]).1);
        if !problems.is_empty() {
            warnings.push(format!("power: {} (a thicker cable or the official supply helps)", problems.join(", ")));
        }
    }
    Facts { rows, warnings }
}

// ── Wi-Fi (NetworkManager) ─────────────────────────────────────────────

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
pub struct Network {
    pub ssid: String,
    pub signal: u32,
    pub security: String,
    pub active: bool,
    pub known: bool,
}

impl Network {
    pub fn open(&self) -> bool {
        matches!(self.security.as_str(), "open" | "--" | "")
    }
}

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

pub fn set_wifi_radio(on: bool) -> Done {
    let (code, out) = sh(&["nmcli", "radio", "wifi", if on { "on" } else { "off" }]);
    done(code, if on { "Wi-Fi on" } else { "Wi-Fi off" }, &out)
}

fn known_wifi() -> Vec<String> {
    let (code, out) = sh(&["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"]);
    if code != 0 {
        return vec![];
    }
    out.lines().filter(|l| l.ends_with(":802-11-wireless")).map(|l| split_terse(l).remove(0)).collect()
}

pub fn wifi_networks() -> Vec<Network> {
    let args = ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes"];
    let (code, out) = run(&args, Duration::from_secs(20), false, None);
    if code == 0 { parse_wifi_list(&out, &known_wifi()) } else { vec![] }
}

pub fn wifi_connect(ssid: &str, password: Option<&str>) -> Done {
    let mut args = vec!["nmcli", "device", "wifi", "connect", ssid];
    if let Some(pw) = password {
        args.extend(["password", pw]);
    }
    let (code, out) = run(&args, Duration::from_secs(45), false, None);
    done(code, format!("connected to {ssid}"), &out)
}

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

// ── Bluetooth (BlueZ) ──────────────────────────────────────────────────

#[derive(Debug, Clone, Default, PartialEq)]
pub struct BtState {
    pub name: String,
    pub powered: bool,
    pub blocked: bool,
    pub present: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Device {
    pub mac: String,
    pub name: String,
    pub paired: bool,
    pub bonded: bool,
    pub trusted: bool,
    pub connected: bool,
    pub icon: String,
}

fn field<'a>(out: &'a str, key: &str) -> &'a str {
    out.lines().find_map(|l| l.trim().strip_prefix(key)?.strip_prefix(':').map(str::trim)).unwrap_or("")
}

pub fn parse_bt_show(out: &str) -> BtState {
    BtState {
        name: field(out, "Name").into(),
        powered: field(out, "Powered") == "yes",
        blocked: field(out, "PowerState").contains("blocked"),
        present: !field(out, "Name").is_empty(),
    }
}

pub fn parse_bt_devices(out: &str) -> Vec<(String, String)> {
    out.lines()
        .filter_map(|l| {
            let rest = l.strip_prefix("Device ")?;
            let (mac, name) = rest.split_at_checked(17)?;
            (mac.len() == 17 && mac.chars().all(|c| c.is_ascii_hexdigit() || c == ':'))
                .then(|| (mac.into(), name.trim().into()))
        })
        .collect()
}

pub fn parse_bt_info(mac: &str, name: &str, out: &str) -> Device {
    let yes = |k: &str| field(out, k) == "yes";
    Device {
        mac: mac.into(),
        name: name.into(),
        paired: yes("Paired"),
        bonded: yes("Bonded"),
        trusted: yes("Trusted"),
        connected: yes("Connected"),
        icon: field(out, "Icon").into(),
    }
}

pub fn bt_state() -> BtState {
    parse_bt_show(&sh(&["bluetoothctl", "show"]).1)
}

pub fn bt_devices() -> Vec<Device> {
    let mut devs: Vec<Device> = parse_bt_devices(&sh(&["bluetoothctl", "devices"]).1)
        .into_iter()
        .map(|(mac, name)| parse_bt_info(&mac, &name, &sh(&["bluetoothctl", "info", &mac]).1))
        .collect();
    devs.sort_by_key(|d| (!d.connected, !d.paired, d.name.to_lowercase()));
    devs
}

/// A soft rfkill block (the deck's "off-blocked"): clear it through /sys, as root.
fn bt_unblock() -> Done {
    let Ok(entries) = std::fs::read_dir("/sys/class/rfkill") else { return Ok("not blocked".into()) };
    let mut any = false;
    for p in entries.flatten().map(|e| e.path()) {
        if read(p.join("type")) == "bluetooth" && read(p.join("soft")) == "1" {
            any = true;
            let soft = p.join("soft");
            let (code, out) = run(&["tee", &soft.to_string_lossy()], TIMEOUT, true, Some("0"));
            if code != 0 {
                return Err(short_error(&out));
            }
        }
    }
    Ok(if any { "unblocked" } else { "not blocked" }.into())
}

pub fn bt_power(on: bool) -> Done {
    if on && bt_state().blocked {
        bt_unblock()?;
        std::thread::sleep(Duration::from_millis(500));
    }
    let (code, out) = sh(&["bluetoothctl", "power", if on { "on" } else { "off" }]);
    let ok = code == 0 && !out.to_lowercase().contains("fail");
    if ok { Ok(if on { "Bluetooth on" } else { "Bluetooth off" }.into()) } else { Err(short_error(&out)) }
}

pub fn bt_scan(seconds: u64) -> Done {
    run(
        &["bluetoothctl", "--timeout", &seconds.to_string(), "scan", "on"],
        Duration::from_secs(seconds + 5),
        false,
        None,
    );
    Ok("scan done".into())
}

/// connect · disconnect · remove.
pub fn bt_do(action: &str, mac: &str) -> Done {
    let (code, out) = run(&["bluetoothctl", action, mac], Duration::from_secs(25), false, None);
    let ok = code == 0 && !out.to_lowercase().contains("fail");
    if ok { Ok(format!("{action}: done")) } else { Err(short_error(&out)) }
}

fn strip_ansi(s: &str) -> String {
    let mut out = String::new();
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\x1b' {
            // CSI: ESC [ … final byte in @–~
            if chars.peek() == Some(&'[') {
                chars.next();
                for f in chars.by_ref() {
                    if ('@'..='~').contains(&f) {
                        break;
                    }
                }
            }
        } else if c != '\r' && c != '\x01' && c != '\x02' {
            out.push(c);
        }
    }
    out
}

/// What a pairing says while it runs.
pub enum PairNews {
    /// Type this on the device (a keyboard), then Enter.
    Code(String),
    Step(String),
}

/// Pair the right way: a bluetoothctl session with a registered agent (so the link key is stored
/// and the device is *bonded*), scanning until the device shows, then trust and connect.
/// A PIN or passkey the device must type is sent to `news` as it appears.
pub fn bt_pair(mac: &str, news: impl Fn(PairNews)) -> Done {
    let mut child = match Command::new("bluetoothctl")
        .env("LC_ALL", "C")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(c) => c,
        Err(_) => return Err("bluetoothctl isn't installed".into()),
    };
    let mut stdin = child.stdin.take().expect("piped stdin");
    let stdout = child.stdout.take().expect("piped stdout");
    let (tx, rx) = mpsc::channel::<String>();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).split(b'\n').map_while(Result::ok) {
            for part in String::from_utf8_lossy(&line).split('\r') {
                if tx.send(strip_ansi(part)).is_err() {
                    return;
                }
            }
        }
    });
    let mut say = |cmd: &str| {
        let _ = writeln!(stdin, "{cmd}");
        let _ = stdin.flush();
    };
    // Wait for any of `wanted` (or a failure) in the output, up to `secs`.
    let wait = |wanted: &[&str], secs: u64, news: &dyn Fn(PairNews), say: &mut dyn FnMut(&str)| -> Option<String> {
        let until = Instant::now() + Duration::from_secs(secs);
        while let Some(left) = until.checked_duration_since(Instant::now()) {
            let Ok(line) = rx.recv_timeout(left) else { break };
            let line = line.trim().to_string();
            // The agent's questions: show the code, accept confirmations.
            if let Some(code) = ["PIN code: ", "Passkey: ", "Confirm passkey "].iter().find_map(|k| {
                let at = line.find(k)?;
                Some(line[at + k.len()..].split_whitespace().next()?.to_string())
            }) {
                news(PairNews::Code(code));
            }
            if line.contains("(yes/no)") {
                say("yes");
            }
            if let Some(hit) = wanted.iter().find(|w| line.contains(**w)) {
                return Some((*hit).to_string());
            }
            if line.contains("Failed to pair")
                || (line.contains("not available") && wanted.contains(&"Pairing successful"))
            {
                return Some(line);
            }
        }
        None
    };
    say("agent KeyboardDisplay");
    say("default-agent");
    say("pairable on");
    news(PairNews::Step("looking for it".into()));
    say("scan on");
    let seen = format!("Device {mac}");
    // Known already, or appears in the scan: either way the pair can start.
    let _ = wait(&[&seen], 10, &news, &mut say);
    news(PairNews::Step("pairing".into()));
    say(&format!("pair {mac}"));
    let result = wait(&["Pairing successful", "AlreadyExists"], 60, &news, &mut say);
    let outcome = match result.as_deref() {
        Some("Pairing successful") | Some("AlreadyExists") => {
            news(PairNews::Step("trusting and connecting".into()));
            say("scan off");
            say(&format!("trust {mac}"));
            say(&format!("connect {mac}"));
            match wait(&["Connection successful"], 15, &news, &mut say) {
                Some(l) if l == "Connection successful" => Ok("paired, bonded and connected".into()),
                _ => Ok("paired (connect it when it's awake)".into()),
            }
        }
        Some(other) => Err(short_error(other)),
        None => Err("pairing timed out (is it still in pairing mode?)".into()),
    };
    say("scan off");
    say("pairable off");
    say("quit");
    drop(stdin);
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline && matches!(child.try_wait(), Ok(None)) {
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = child.kill();
    let _ = child.wait();
    outcome
}

// ── audio ───────────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq)]
pub struct Output {
    pub id: String,
    pub name: String,
    pub default: bool,
}

/// The first Sinks block of `wpctl status`: ' │  *   55. Built-in Audio Analog Stereo  [vol: 0.45]'.
pub fn parse_wpctl_sinks(status: &str) -> Vec<Output> {
    let mut sinks = vec![];
    let mut inside = false;
    for line in status.lines() {
        let clean: String = line.chars().map(|c| if "│├└─".contains(c) { ' ' } else { c }).collect();
        let clean = clean.trim();
        if !inside {
            inside = clean == "Sinks:";
            continue;
        }
        if clean.is_empty() {
            break;
        }
        let (default, rest) = match clean.strip_prefix('*') {
            Some(r) => (true, r.trim_start()),
            None => (false, clean),
        };
        let Some((id, name)) = rest.split_once('.') else { continue };
        if id.is_empty() || !id.chars().all(|c| c.is_ascii_digit()) {
            continue;
        }
        let name = name.split("[vol:").next().unwrap_or("").trim();
        sinks.push(Output { id: id.into(), name: name.into(), default });
    }
    sinks
}

pub fn parse_alsa_cards(aplay_l: &str) -> Vec<Output> {
    let mut cards: Vec<Output> = vec![];
    for line in aplay_l.lines() {
        let Some(rest) = line.strip_prefix("card ") else { continue };
        let Some((id, rest)) = rest.split_once(':') else { continue };
        let name = rest.split_once('[').and_then(|(_, r)| r.split_once(']')).map(|(n, _)| n);
        if let Some(name) = name
            && !cards.iter().any(|c| c.id == id)
        {
            cards.push(Output { id: id.into(), name: name.into(), default: false });
        }
    }
    cards
}

/// (volume %, muted) from `amixer sget`.
pub fn parse_amixer(out: &str) -> (Option<u32>, bool) {
    for line in out.lines() {
        let Some(pct) = line.split('[').find_map(|p| p.strip_suffix("%] ").or(p.strip_suffix("%]"))) else { continue };
        if let Ok(v) = pct.parse() {
            return (Some(v), line.contains("[off]"));
        }
    }
    (None, false)
}

fn asoundrc() -> PathBuf {
    crate::config::home().join(".asoundrc")
}

/// PipeWire (wpctl) where it runs, else plain ALSA (amixer, ~/.asoundrc for the default card).
pub struct Audio {
    pub pipewire: bool,
    pub backend: &'static str,
}

impl Audio {
    pub fn detect() -> Self {
        let pipewire = have("wpctl") && run(&["wpctl", "status"], Duration::from_secs(3), false, None).0 == 0;
        let backend = if pipewire {
            "pipewire"
        } else if have("amixer") {
            "alsa"
        } else {
            "none"
        };
        Audio { pipewire, backend }
    }

    pub fn outputs(&self) -> Vec<Output> {
        if self.pipewire {
            return parse_wpctl_sinks(&sh(&["wpctl", "status"]).1);
        }
        let current = self.alsa_default();
        let mut cards = parse_alsa_cards(&sh(&["aplay", "-l"]).1);
        for c in &mut cards {
            c.default = c.id == current;
        }
        cards
    }

    fn alsa_default(&self) -> String {
        read(asoundrc())
            .lines()
            .find_map(|l| l.trim().strip_prefix("defaults.pcm.card").map(|v| v.trim().to_string()))
            .unwrap_or_else(|| "0".into())
    }

    fn alsa_control(&self, card: &str) -> Option<String> {
        let out = sh(&["amixer", "-c", card, "scontrols"]).1;
        let names: Vec<String> = out.lines().filter_map(|l| l.split('\'').nth(1).map(String::from)).collect();
        ["Master", "PCM", "Speaker", "Headphone", "HDMI"]
            .iter()
            .find(|n| names.iter().any(|x| x == *n))
            .map(|n| n.to_string())
            .or_else(|| names.first().cloned())
    }

    pub fn volume(&self) -> (Option<u32>, bool) {
        if self.pipewire {
            let out = sh(&["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]).1;
            let v = out
                .split("Volume:")
                .nth(1)
                .and_then(|r| r.split_whitespace().next())
                .and_then(|v| v.parse::<f64>().ok());
            return (v.map(|v| (v * 100.0).round() as u32), out.contains("MUTED"));
        }
        let card = self.alsa_default();
        match self.alsa_control(&card) {
            Some(ctl) => parse_amixer(&sh(&["amixer", "-c", &card, "sget", &ctl]).1),
            None => (None, false),
        }
    }

    pub fn change(&self, step: i32) -> Done {
        let amount = format!("{}%{}", step.abs(), if step > 0 { "+" } else { "-" });
        let (code, out) = if self.pipewire {
            sh(&["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", &amount])
        } else {
            let card = self.alsa_default();
            let Some(ctl) = self.alsa_control(&card) else { return Err("this card has no volume control".into()) };
            sh(&["amixer", "-c", &card, "sset", &ctl, &amount])
        };
        done(code, "", &out)
    }

    pub fn toggle_mute(&self) -> Done {
        let (code, out) = if self.pipewire {
            sh(&["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
        } else {
            let card = self.alsa_default();
            match self.alsa_control(&card) {
                Some(ctl) => sh(&["amixer", "-c", &card, "sset", &ctl, "toggle"]),
                None => (1, "no control".into()),
            }
        };
        done(code, "", &out)
    }

    pub fn set_default(&self, output: &Output) -> Done {
        if self.pipewire {
            let (code, out) = sh(&["wpctl", "set-default", &output.id]);
            return done(code, format!("sound out: {}", output.name), &out);
        }
        let text = format!("# written by MABEL-OS SYS\ndefaults.pcm.card {0}\ndefaults.ctl.card {0}\n", output.id);
        std::fs::write(asoundrc(), text).map_err(|e| e.to_string())?;
        Ok(format!("sound out: {} (programs started from now on)", output.name))
    }
}

// ── display ─────────────────────────────────────────────────────────────

fn backlight() -> Option<PathBuf> {
    let mut lights: Vec<PathBuf> =
        std::fs::read_dir("/sys/class/backlight").ok()?.flatten().map(|e| e.path()).collect();
    lights.sort();
    lights.into_iter().next()
}

pub fn brightness() -> Option<u32> {
    let light = backlight()?;
    let cur: f64 = read(light.join("brightness")).parse().ok()?;
    let top: f64 = read(light.join("max_brightness")).parse().ok()?;
    (top > 0.0).then(|| (cur * 100.0 / top).round() as u32)
}

pub fn set_brightness(step: i32) -> Done {
    if backlight().is_none() {
        return Err("this screen has no brightness control".into());
    }
    if !have("brightnessctl") {
        return Err("brightnessctl isn't installed".into());
    }
    let (code, out) = sh(&["brightnessctl", "set", &format!("{}%{}", step.abs(), if step > 0 { "+" } else { "-" })]);
    done(code, "", &out)
}

// ── time and locale ─────────────────────────────────────────────────────

pub fn parse_keyvalues(out: &str) -> Vec<(String, String)> {
    out.lines().filter_map(|l| l.split_once('=').map(|(k, v)| (k.into(), v.into()))).collect()
}

pub fn time_state() -> Vec<(String, String)> {
    parse_keyvalues(&sh(&["timedatectl", "show"]).1)
}

pub fn timezones() -> Vec<String> {
    let (code, out) = sh(&["timedatectl", "list-timezones"]);
    if code == 0 { out.lines().map(String::from).collect() } else { vec![] }
}

/// Try as ourselves, then with sudo -n.
fn as_admin(args: &[&str], ok: String) -> Done {
    let (mut code, mut out) = sh(args);
    if code != 0 {
        (code, out) = run(args, TIMEOUT, true, None);
    }
    done(code, ok, &out)
}

pub fn set_timezone(tz: &str) -> Done {
    as_admin(&["timedatectl", "set-timezone", tz], format!("time zone {tz}"))
}

pub fn set_ntp(on: bool) -> Done {
    let label = if on { "network time on" } else { "network time off" };
    as_admin(&["timedatectl", "set-ntp", if on { "true" } else { "false" }], label.into())
}

/// (locale, keyboard layout).
pub fn locale_state() -> (String, String) {
    let out = sh(&["localectl", "status"]).1;
    (field(&out, "System Locale").replace("LANG=", ""), field(&out, "X11 Layout").into())
}

pub fn set_keyboard(layout: &str) -> Done {
    let (code, out) = run(&["localectl", "set-x11-keymap", layout], TIMEOUT, true, None);
    done(code, format!("keyboard {layout} (from the next start of MABEL-OS)"), &out)
}

// ── power ───────────────────────────────────────────────────────────────

/// suspend · reboot · poweroff of the whole machine.
pub fn power(action: &str) -> Done {
    as_admin(&["systemctl", action], action.into())
}

#[cfg(test)]
mod tests {
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

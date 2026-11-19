//! STATUS's facts, fastfetch-style: OS, CPU, memory, temperature, battery, addresses, GPU,
//! tailnet, disk, and the Pi's throttling flags.

use super::*;

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

/// The distribution's name (/etc/os-release).
fn os_name() -> String {
    read("/etc/os-release")
        .lines()
        .find_map(|l| l.strip_prefix("PRETTY_NAME=").map(|v| v.trim_matches('"').to_string()))
        .unwrap_or_else(|| "Linux".into())
}

/// The CPU's model and core count.
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

/// How long the machine has been on ("3 d 4 h 12 min").
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

/// The CPU temperature in °C, if a sensor says.
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

/// Battery level and charging, if there's a battery.
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

/// Each network interface's IP address.
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

/// The graphics card, if one can be named.
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

/// This machine's tailnet (Tailscale) name and address.
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

/// Space used and free on /.
fn disk() -> String {
    let (code, out) = sh(&["df", "-P", "-B1", "/"]);
    let line = out.lines().nth(1).unwrap_or("");
    let cols: Vec<u64> = line.split_whitespace().skip(1).take(3).filter_map(|c| c.parse().ok()).collect();
    if code != 0 || cols.len() < 3 {
        return "?".into();
    }
    format!("{:.0} / {:.0} GB", cols[1] as f64 / 1e9, cols[0] as f64 / 1e9)
}

/// Everything STATUS's card shows.
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

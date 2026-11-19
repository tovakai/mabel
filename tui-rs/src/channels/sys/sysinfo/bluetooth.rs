//! Bluetooth through BlueZ's `bluetoothctl`: the adapter, devices, scanning, and pairing
//! (with the PIN or passkey reported as it happens).

use super::*;

#[derive(Debug, Clone, Default, PartialEq)]
/// The adapter: its name, and whether it's powered, discoverable…
pub struct BtState {
    pub name: String,
    pub powered: bool,
    pub blocked: bool,
    pub present: bool,
}

#[derive(Debug, Clone, PartialEq)]
/// A device BlueZ knows (paired, or found by a scan).
pub struct Device {
    pub mac: String,
    pub name: String,
    pub paired: bool,
    pub bonded: bool,
    pub trusted: bool,
    pub connected: bool,
    pub icon: String,
}

/// `bluetoothctl show` → the adapter's state.
pub fn parse_bt_show(out: &str) -> BtState {
    BtState {
        name: field(out, "Name").into(),
        powered: field(out, "Powered") == "yes",
        blocked: field(out, "PowerState").contains("blocked"),
        present: !field(out, "Name").is_empty(),
    }
}

/// `bluetoothctl devices` → (MAC, name) pairs.
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

/// `bluetoothctl info MAC` → one device's details (paired, bonded, connected, icon).
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

/// The adapter now.
pub fn bt_state() -> BtState {
    parse_bt_show(&sh(&["bluetoothctl", "show"]).1)
}

/// Every known device, with details.
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

/// Adapter on or off (on also unblocks it if rfkill had it blocked).
pub fn bt_power(on: bool) -> Done {
    if on && bt_state().blocked {
        bt_unblock()?;
        std::thread::sleep(Duration::from_millis(500));
    }
    let (code, out) = sh(&["bluetoothctl", "power", if on { "on" } else { "off" }]);
    let ok = code == 0 && !out.to_lowercase().contains("fail");
    if ok { Ok(if on { "Bluetooth on" } else { "Bluetooth off" }.into()) } else { Err(short_error(&out)) }
}

/// Look for devices for `seconds` (they need to be in pairing mode).
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

/// bluetoothctl's output without its colour codes and prompts, so it can be read.
pub(super) fn strip_ansi(s: &str) -> String {
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

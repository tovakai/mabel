//! `mabel-os print-server`: the deck as print server for the desk's thermal printer (and, later,
//! the one in its casing). The Rust port of tui/mabel_tui/printserver.py (`mabel-print`).
//!
//! The deck never sleeps, so it owns the printer: it keeps it awake (a status request every 20 min;
//! the printer's own auto-off is about an hour and only its button brings it back) and prints
//! what Mabel sends. Mabel's src/persona/printer.py POSTs finished ESC/POS bytes here instead of
//! opening Bluetooth itself; if the deck or the printer can't be reached, her queue keeps the job.
//!
//!   POST /print    raw ESC/POS bytes → {"route": "usb" | "bluetooth", "bytes": n} (503 if the printer is away)
//!   GET  /status   {"printer", "usb", "keepalive_minutes", "last_ok", "last_error", "last_route"}
//!
//! Both need "Authorization: Bearer <token>". It listens on the tailnet address only
//! (MABEL_PRINT_HOST overrides). Config: ~/.config/mabel-tui/printserver.toml, the same file as
//! the Python version's:
//!
//!   token = "…"                          # shared with Mabel (setting printer_server_token)
//!   bluetooth = "86:67:7A:53:65:6F"      # the printer (RFCOMM channel 1, no pairing)
//!   usb = "/dev/usb/lp0"                 # used first when it exists (the casing)
//!   port = 8631
//!   keepalive_minutes = 20
//!
//! Runs as the systemd user service mabel-print.service on the deck. Plain threads and std::net:
//! a request or two a day doesn't want a web framework.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::fd::{FromRawFd, OwnedFd};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Context, Result};
use serde_json::{Value, json};

const STATUS: &[u8] = b"\x10\x04\x01"; // DLE EOT 1: a real-time status request, prints nothing
const CHUNK: usize = 512;
const MAX_JOB: usize = 4 * 1024 * 1024;
const AF_BLUETOOTH: libc::c_int = 31;
const BTPROTO_RFCOMM: libc::c_int = 3;

#[derive(Clone, Debug, PartialEq)]
pub struct PrintConfig {
    pub token: String,
    pub bluetooth: String,
    pub usb: String,
    pub port: u16,
    pub keepalive_minutes: f64,
}

pub fn config_path() -> PathBuf {
    crate::os::config::config_path().with_file_name("printserver.toml")
}

fn token() -> Result<String> {
    let mut raw = [0u8; 24];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut raw)?;
    const ABC: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::new();
    for c in raw.chunks(3) {
        let n = (c[0] as u32) << 16 | (c[1] as u32) << 8 | c[2] as u32;
        for shift in [18, 12, 6, 0] {
            out.push(ABC[(n >> shift & 63) as usize] as char);
        }
    }
    Ok(out)
}

/// The config, creating it (with a fresh token, readable only by us) the first time.
pub fn load_config(path: &Path) -> Result<PrintConfig> {
    let table: toml::Table = match std::fs::read_to_string(path) {
        Ok(text) => text.parse().with_context(|| format!("{}: not valid TOML", path.display()))?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            use std::os::unix::fs::OpenOptionsExt;
            let token = token()?;
            if let Some(dir) = path.parent() {
                std::fs::create_dir_all(dir)?;
            }
            let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(path)?;
            writeln!(f, "token = \"{token}\"")?;
            let mut t = toml::Table::new();
            t.insert("token".into(), token.into());
            t
        }
        Err(e) => return Err(e.into()),
    };
    let text = |k: &str, d: &str| table.get(k).and_then(|v| v.as_str()).unwrap_or(d).to_string();
    let num = |k: &str, d: f64| {
        table.get(k).and_then(|v| v.as_float().or_else(|| v.as_integer().map(|i| i as f64))).unwrap_or(d)
    };
    Ok(PrintConfig {
        token: text("token", ""),
        bluetooth: text("bluetooth", "86:67:7A:53:65:6F"),
        usb: text("usb", "/dev/usb/lp0"),
        port: num("port", 8631.0) as u16,
        keepalive_minutes: num("keepalive_minutes", 20.0),
    })
}

// ── the printer ──────────────────────────────────────────────────────────

fn send_usb(device: &str, data: &[u8]) -> std::io::Result<()> {
    let mut f = std::fs::OpenOptions::new().write(true).open(device)?;
    for chunk in data.chunks(CHUNK) {
        f.write_all(chunk)?;
    }
    Ok(())
}

/// "86:67:7A:53:65:6F" → the bytes as the kernel wants them (reversed).
fn bdaddr(address: &str) -> Option<[u8; 6]> {
    let parts: Vec<u8> = address.split(':').map(|p| u8::from_str_radix(p, 16).ok()).collect::<Option<_>>()?;
    let mut out: [u8; 6] = parts.try_into().ok()?;
    out.reverse();
    Some(out)
}

#[repr(C)]
struct SockaddrRc {
    family: libc::sa_family_t,
    bdaddr: [u8; 6],
    channel: u8,
}

fn send_bt(address: &str, data: &[u8]) -> std::io::Result<()> {
    let bd = bdaddr(address).ok_or_else(|| std::io::Error::other(format!("not a Bluetooth address: {address}")))?;
    // SAFETY: plain socket calls on a descriptor we own; the address struct matches sockaddr_rc.
    let fd = unsafe { libc::socket(AF_BLUETOOTH, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, BTPROTO_RFCOMM) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    let timeout = libc::timeval { tv_sec: 20, tv_usec: 0 };
    for opt in [libc::SO_SNDTIMEO, libc::SO_RCVTIMEO] {
        unsafe {
            libc::setsockopt(
                std::os::fd::AsRawFd::as_raw_fd(&fd),
                libc::SOL_SOCKET,
                opt,
                &timeout as *const _ as *const libc::c_void,
                std::mem::size_of::<libc::timeval>() as libc::socklen_t,
            );
        }
    }
    let addr = SockaddrRc { family: AF_BLUETOOTH as libc::sa_family_t, bdaddr: bd, channel: 1 };
    let rc = unsafe {
        libc::connect(
            std::os::fd::AsRawFd::as_raw_fd(&fd),
            &addr as *const SockaddrRc as *const libc::sockaddr,
            std::mem::size_of::<SockaddrRc>() as libc::socklen_t,
        )
    };
    if rc < 0 {
        return Err(std::io::Error::last_os_error());
    }
    let mut sock = std::fs::File::from(fd);
    for chunk in data.chunks(CHUNK) {
        sock.write_all(chunk)?; // the printer's buffer is small; let it keep up
        std::thread::sleep(Duration::from_millis(40));
    }
    // Closing early can cut the tail off.
    std::thread::sleep(Duration::from_secs_f64((1.0 + data.len() as f64 / 6000.0).min(8.0)));
    Ok(())
}

/// USB if it's plugged in, else Bluetooth (twice: a dozing module often refuses the first).
pub fn send(cfg: &PrintConfig, data: &[u8]) -> Result<&'static str, String> {
    let mut errors = vec![];
    if !cfg.usb.is_empty() && Path::new(&cfg.usb).exists() {
        match send_usb(&cfg.usb, data) {
            Ok(()) => return Ok("usb"),
            Err(e) => errors.push(format!("usb: {e}")),
        }
    }
    if !cfg.bluetooth.is_empty() {
        for attempt in 0..2 {
            match send_bt(&cfg.bluetooth, data) {
                Ok(()) => return Ok("bluetooth"),
                Err(e) if attempt == 1 => errors.push(format!("bluetooth: {e}")),
                Err(_) => std::thread::sleep(Duration::from_millis(1500)),
            }
        }
    }
    Err(if errors.is_empty() { "no printer configured".into() } else { errors.join("; ") })
}

// ── the server ───────────────────────────────────────────────────────────

pub type SendFn = Arc<dyn Fn(&PrintConfig, &[u8]) -> Result<&'static str, String> + Send + Sync>;

#[derive(Default)]
struct State {
    last_ok: Option<String>,
    last_error: Option<String>,
    last_route: Option<String>,
}

pub struct Server {
    cfg: PrintConfig,
    send: SendFn,
    /// One job on the printer at a time.
    wire: Mutex<()>,
    state: Mutex<State>,
}

fn log(msg: &str) {
    eprintln!("mabel-print INFO {msg}");
}

fn now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
}

impl Server {
    pub fn new(cfg: PrintConfig, send: SendFn) -> Arc<Self> {
        Arc::new(Server { cfg, send, wire: Mutex::new(()), state: Mutex::new(State::default()) })
    }

    fn print(&self, data: &[u8]) -> Result<&'static str, String> {
        let result = {
            let _wire = self.wire.lock().unwrap_or_else(|e| e.into_inner());
            (self.send)(&self.cfg, data)
        };
        let mut s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        match &result {
            Ok(route) => (s.last_ok, s.last_route) = (Some(now()), Some(route.to_string())),
            Err(e) => s.last_error = Some(format!("{} {e}", now())),
        }
        result
    }

    fn status(&self) -> Value {
        let s = self.state.lock().unwrap_or_else(|e| e.into_inner());
        json!({
            "printer": self.cfg.bluetooth,
            "usb": !self.cfg.usb.is_empty() && Path::new(&self.cfg.usb).exists(),
            "keepalive_minutes": self.cfg.keepalive_minutes,
            "last_ok": s.last_ok,
            "last_error": s.last_error,
            "last_route": s.last_route,
        })
    }

    fn keepalive(self: Arc<Self>) {
        let minutes = self.cfg.keepalive_minutes;
        if minutes <= 0.0 {
            return;
        }
        loop {
            if let Err(e) = self.print(STATUS) {
                log(&format!("keep-alive: printer not reachable ({e})"));
            }
            std::thread::sleep(Duration::from_secs_f64(minutes * 60.0));
        }
    }

    pub fn serve(self: Arc<Self>, listener: TcpListener) {
        for stream in listener.incoming().flatten() {
            let me = self.clone();
            std::thread::spawn(move || me.handle(stream));
        }
    }

    fn handle(&self, stream: TcpStream) {
        let peer = stream.peer_addr().map_or("?".into(), |a| a.ip().to_string());
        let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
        let Ok(mut out) = stream.try_clone() else { return };
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        if reader.read_line(&mut line).is_err() {
            return;
        }
        let mut parts = line.split_whitespace();
        let (method, path) = (parts.next().unwrap_or("").to_string(), parts.next().unwrap_or("").to_string());
        let (mut length, mut auth) = (0usize, String::new());
        loop {
            let mut h = String::new();
            if reader.read_line(&mut h).unwrap_or(0) == 0 || h.trim().is_empty() {
                break;
            }
            if let Some((k, v)) = h.split_once(':') {
                match k.trim().to_ascii_lowercase().as_str() {
                    "content-length" => length = v.trim().parse().unwrap_or(0),
                    "authorization" => auth = v.trim().to_string(),
                    _ => {}
                }
            }
        }
        // Read the whole body before answering, even a refused one: closing a socket with unread
        // data resets the connection, and the client would see an error instead of our reply.
        let mut data = vec![];
        if length <= MAX_JOB && (&mut reader).take(length as u64).read_to_end(&mut data).is_err() {
            return;
        }
        let (code, body) = self.route(&method, &path, &auth, length, &data);
        log(&format!("{peer} \"{method} {path}\" {code}"));
        let raw = body.to_string();
        let reason = match code {
            200 => "OK",
            401 => "Unauthorized",
            404 => "Not Found",
            413 => "Payload Too Large",
            _ => "Service Unavailable",
        };
        let _ = write!(
            out,
            "HTTP/1.1 {code} {reason}\r\nServer: mabel-print\r\nContent-Type: application/json\r\n\
             Content-Length: {}\r\nConnection: close\r\n\r\n{raw}",
            raw.len()
        );
    }

    fn authorized(&self, auth: &str) -> bool {
        let sent = auth.strip_prefix("Bearer ").unwrap_or("").as_bytes();
        let want = self.cfg.token.as_bytes();
        // Constant-time compare (the length isn't secret).
        !want.is_empty() && sent.len() == want.len() && sent.iter().zip(want).fold(0, |acc, (a, b)| acc | (a ^ b)) == 0
    }

    fn route(&self, method: &str, path: &str, auth: &str, length: usize, data: &[u8]) -> (u16, Value) {
        let unauthorized = (401, json!({"error": "unauthorized"}));
        match (method, path) {
            ("GET", "/status") if !self.authorized(auth) => unauthorized,
            ("GET", "/status") => (200, self.status()),
            ("POST", "/print") if !self.authorized(auth) => unauthorized,
            ("POST", "/print") if length == 0 || length > MAX_JOB => (413, json!({"error": "empty or too big"})),
            ("POST", "/print") if data.len() < length => (413, json!({"error": "the body ended early"})),
            ("POST", "/print") => match self.print(data) {
                Ok(route) => (200, json!({"route": route, "bytes": length})),
                Err(e) => (503, json!({"error": e})),
            },
            _ => (404, json!({"error": "not found"})),
        }
    }
}

fn tailnet_address() -> Option<String> {
    let out = std::process::Command::new("tailscale").args(["ip", "-4"]).output().ok()?;
    String::from_utf8_lossy(&out.stdout).split_whitespace().next().map(str::to_string)
}

/// `mabel-os print-server`: runs until killed.
pub fn run() -> Result<()> {
    let cfg = load_config(&config_path())?;
    let host = match std::env::var("MABEL_PRINT_HOST") {
        Ok(h) if !h.is_empty() => h,
        _ => loop {
            if let Some(h) = tailnet_address() {
                break h;
            }
            log("waiting for the tailnet address"); // at boot the tailnet may come up after us
            std::thread::sleep(Duration::from_secs(10));
        },
    };
    let listener =
        TcpListener::bind((host.as_str(), cfg.port)).with_context(|| format!("listen on {host}:{}", cfg.port))?;
    log(&format!("printing for Mabel on {host}:{} (printer {}, usb {})", cfg.port, cfg.bluetooth, cfg.usb));
    let server = Server::new(cfg, Arc::new(send));
    let keep = server.clone();
    std::thread::spawn(move || keep.keepalive());
    server.serve(listener);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call(port: u16, method: &str, path: &str, token: &str, body: &[u8]) -> (u16, Value) {
        let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
        write!(
            s,
            "{method} {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .unwrap();
        s.write_all(body).unwrap();
        let mut reply = String::new();
        s.read_to_string(&mut reply).unwrap();
        let code = reply[9..12].parse().unwrap();
        let json = reply.split("\r\n\r\n").nth(1).unwrap();
        (code, serde_json::from_str(json).unwrap())
    }

    fn cfg() -> PrintConfig {
        PrintConfig {
            token: "s3cret".into(),
            bluetooth: "86:67:7A:53:65:6F".into(),
            usb: "/nonexistent/lp0".into(),
            port: 0,
            keepalive_minutes: 0.0,
        }
    }

    #[test]
    fn config_is_created_with_a_private_token() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("mabel-print-{}", std::process::id()));
        let path = dir.join("printserver.toml");
        let c = load_config(&path).unwrap();
        assert_eq!((c.token.len(), c.port, c.keepalive_minutes), (32, 8631, 20.0));
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert_eq!(load_config(&path).unwrap().token, c.token, "kept, not regenerated");
        std::fs::write(&path, "token = \"t\"\nport = 9000\nkeepalive_minutes = 0.5\nusb = \"\"\n").unwrap();
        let c = load_config(&path).unwrap();
        assert_eq!((c.port, c.keepalive_minutes, c.usb.as_str()), (9000, 0.5, ""));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn printing_needs_the_token_and_reaches_the_printer() {
        let sent = Arc::new(Mutex::new(Vec::<Vec<u8>>::new()));
        let away = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let (s2, a2) = (sent.clone(), away.clone());
        let fake: SendFn = Arc::new(move |_, data| {
            if a2.load(std::sync::atomic::Ordering::Relaxed) {
                return Err("bluetooth: Host is down (os error 112)".into());
            }
            s2.lock().unwrap().push(data.to_vec());
            Ok("bluetooth")
        });
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = Server::new(cfg(), fake);
        std::thread::spawn(move || server.serve(listener));

        assert_eq!(call(port, "POST", "/print", "wrong", b"x").0, 401);
        assert!(sent.lock().unwrap().is_empty());
        let (code, body) = call(port, "POST", "/print", "s3cret", b"\x1b@hello\n");
        assert_eq!((code, body), (200, json!({"route": "bluetooth", "bytes": 8})));
        assert_eq!(*sent.lock().unwrap(), vec![b"\x1b@hello\n".to_vec()]);
        let (code, body) = call(port, "GET", "/status", "s3cret", b"");
        assert_eq!(
            (code, body["last_route"].as_str(), body["printer"].as_str()),
            (200, Some("bluetooth"), Some("86:67:7A:53:65:6F"))
        );
        assert_eq!(call(port, "POST", "/print", "s3cret", b"").0, 413);
        assert_eq!(call(port, "GET", "/nope", "s3cret", b"").0, 404);
        away.store(true, std::sync::atomic::Ordering::Relaxed);
        let (code, body) = call(port, "POST", "/print", "s3cret", b"x");
        assert!(code == 503 && body["error"].as_str().unwrap().contains("Host is down"));
        assert!(call(port, "GET", "/status", "s3cret", b"").1["last_error"].as_str().unwrap().contains("Host is down"));
    }

    #[test]
    fn usb_goes_first_when_plugged_in() {
        let lp = std::env::temp_dir().join(format!("mabel-lp0-{}", std::process::id()));
        std::fs::write(&lp, b"").unwrap();
        let c = PrintConfig { usb: lp.display().to_string(), bluetooth: "not-an-address".into(), ..cfg() };
        assert_eq!(send(&c, b"hi"), Ok("usb"));
        assert_eq!(std::fs::read(&lp).unwrap(), b"hi");
        let _ = std::fs::remove_file(lp);
    }

    #[test]
    fn bluetooth_addresses_are_reversed_for_the_kernel() {
        assert_eq!(bdaddr("86:67:7A:53:65:6F"), Some([0x6F, 0x65, 0x53, 0x7A, 0x67, 0x86]));
        assert_eq!(bdaddr("86:67"), None);
        assert_eq!(std::mem::size_of::<SockaddrRc>(), 10, "the kernel's sockaddr_rc");
    }
}

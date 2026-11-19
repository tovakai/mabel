//! Sound (PipeWire's `wpctl`, or plain ALSA with `amixer` on the deck) and the screen's
//! brightness (the backlight in /sys).

use super::*;

#[derive(Debug, Clone, PartialEq)]
/// A sound output (speakers, headphones, HDMI…).
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

/// `aplay -l` → the ALSA sound cards (the deck has no PipeWire).
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

/// ~/.asoundrc: on plain ALSA, which card is the default is written here.
fn asoundrc() -> PathBuf {
    crate::os::config::home().join(".asoundrc")
}

/// PipeWire (wpctl) where it runs, else plain ALSA (amixer, ~/.asoundrc for the default card).
pub struct Audio {
    pub pipewire: bool,
    pub backend: &'static str,
}

impl Audio {
    /// Which sound system this machine has.
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

    /// The outputs, the default one marked.
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

    /// (volume in %, muted?) of the default output.
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

    /// Volume up or down by `step` %.
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

    /// Mute on/off.
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

    /// Make `output` the one sound goes to.
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

/// The screen's backlight in /sys/class/backlight, if it has one.
fn backlight() -> Option<PathBuf> {
    let mut lights: Vec<PathBuf> =
        std::fs::read_dir("/sys/class/backlight").ok()?.flatten().map(|e| e.path()).collect();
    lights.sort();
    lights.into_iter().next()
}

/// Brightness in % (None: no backlight control).
pub fn brightness() -> Option<u32> {
    let light = backlight()?;
    let cur: f64 = read(light.join("brightness")).parse().ok()?;
    let top: f64 = read(light.join("max_brightness")).parse().ok()?;
    (top > 0.0).then(|| (cur * 100.0 / top).round() as u32)
}

/// Brightness up or down by `step` %.
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

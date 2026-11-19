//! Building SYS's pages from what the system says (sysinfo.rs). This runs on a background
//! thread, because the commands it asks (nmcli, bluetoothctl, wpctl…) can take a moment.

use super::*;

/// Build page `kind`: its menu, and (STATUS only) the lines above it.
pub(super) fn build(kind: Kind, bc: &BuildCtx) -> (Vec<Item>, Vec<Line<'static>>) {
    match kind {
        Kind::Status => build_status(bc),
        Kind::Network => (build_network(), vec![]),
        Kind::Bluetooth => (build_bluetooth(), vec![]),
        Kind::Audio => (build_audio(), vec![]),
        Kind::Time => (build_time(), vec![]),
    }
}

/// STATUS: a fastfetch-style card (Mabel's face, the machine, the link to Mabel), then
/// restart / shut down MABEL-OS, and on the deck the machine's power.
pub(super) fn build_status(bc: &BuildCtx) -> (Vec<Item>, Vec<Line<'static>>) {
    let pal = &bc.pal;
    let facts = si::facts();
    let mut rows = vec![
        ("mabel-os".to_string(), format!("2.6 · rust {}", env!("CARGO_PKG_VERSION"))),
        ("link".to_string(), bc.link.clone()),
    ];
    rows.extend(facts.rows);
    let w = rows.iter().map(|(k, _)| width(k)).max().unwrap_or(0);
    let mut info: Vec<Line<'static>> = rows
        .into_iter()
        .map(|(k, v)| {
            Line::from(vec![
                Span::styled(format!("{k}{}  ", " ".repeat(w - width(&k))), bold(pal.amber_hi)),
                Span::styled(v, Style::new().fg(pal.amber)),
            ])
        })
        .collect();
    for warn in facts.warnings {
        info.push(Line::styled(format!("⚠ {warn}"), bold(pal.red)));
    }
    info.push(Line::raw(""));
    let swatches: Vec<Color> =
        vec![pal.bg, pal.rust, pal.amber_dim, pal.amber, pal.amber_hi, pal.beige, pal.teal, pal.red];
    info.push(Line::from(swatches.into_iter().map(|c| Span::styled("███", Style::new().fg(c))).collect::<Vec<_>>()));

    let logo = crate::os::avatar::still_lines();
    let logo_w = logo.first().map_or(0, |l| l.width());
    let mut top = vec![];
    for i in 0..logo.len().max(info.len()) {
        let mut spans = vec![Span::raw(" ")];
        match logo.get(i) {
            Some(l) => spans.extend(l.spans.clone()),
            None => spans.push(Span::raw(" ".repeat(logo_w))),
        }
        spans.push(Span::raw("   "));
        if let Some(l) = info.get(i) {
            spans.extend(l.spans.clone());
        }
        top.push(Line::from(spans));
    }

    let mut items = vec![
        Item::header("MABEL-OS"),
        Item::new("restart MABEL-OS", "start it again (picks up changes)").act(Action::Restart),
        Item::new("shut down MABEL-OS", "quit to the shell").act(Action::Quit),
    ];
    if bc.deck {
        items.push(Item::header("THE MACHINE"));
        for (action, label) in [("suspend", "suspend"), ("reboot", "reboot"), ("poweroff", "power off")] {
            let armed = bc.armed == action;
            let item = Item::new(label, if armed { "enter again to confirm" } else { "" }).act(Action::Power(action));
            items.push(if armed { item.look(Look::Danger) } else { item });
        }
    }
    (items, top)
}

/// Wi-Fi strength (0-100) as 1-4 bars.
pub(super) fn bars(signal: u32) -> String {
    let n = (signal / 25 + 1).clamp(1, 4) as usize;
    format!("{:<4}", "▂▄▆█".chars().take(n).collect::<String>())
}

/// NETWORK: wired and tailnet connections, the Wi-Fi radio, and the networks in range
/// (enter connects, x forgets a saved one).
pub(super) fn build_network() -> Vec<Item> {
    let (hw, sw) = si::wifi_radio();
    let mut items = vec![Item::header("WIRED / TAILNET")];
    for (dev, kind, state) in si::wired() {
        if kind != "wifi" {
            items.push(Item::new(format!("{dev} ({kind})"), state));
        }
    }
    items.push(Item::header("WI-FI"));
    if !hw {
        items.push(Item::new("Wi-Fi hardware", "off (a hardware switch or no Wi-Fi chip)"));
        return items;
    }
    items.push(Item::new("Wi-Fi", if sw { "on" } else { "off: enter turns it on" }).act(Action::WifiRadio(!sw)));
    if sw {
        for net in si::wifi_networks() {
            let lock = if net.open() { "□" } else { "▣" };
            let state = if net.active {
                "connected"
            } else if net.known {
                "known"
            } else {
                ""
            };
            let ssid: String = net.ssid.chars().take(22).collect();
            let label = format!("{}{ssid}", if net.active { "▸ " } else { "  " });
            let mut item = Item::new(label, format!("{} {:>3}% {lock} {state}", bars(net.signal), net.signal))
                .act(Action::WifiConnect(net.clone()));
            if net.known {
                item = item.key('x', Action::WifiForget(net.ssid.clone()));
            }
            items.push(if net.active { item.look(Look::Live) } else { item });
        }
    }
    items
}

/// BLUETOOTH: the radio, scanning, and devices (connect, disconnect, pair, remove).
pub(super) fn build_bluetooth() -> Vec<Item> {
    let st = si::bt_state();
    if !st.present {
        return vec![Item::new("no Bluetooth adapter found", "")];
    }
    let state = if st.powered {
        "on"
    } else if st.blocked {
        "off (blocked: enter unblocks and turns it on)"
    } else {
        "off"
    };
    let mut items = vec![
        Item::header("ADAPTER"),
        Item::new(format!("Bluetooth · {}", st.name), state).act(Action::BtPower(!st.powered)),
    ];
    if st.powered {
        items
            .push(Item::new("look for devices", "8 s scan · put the device in pairing mode first").act(Action::BtScan));
        items.push(Item::header("DEVICES"));
        for d in si::bt_devices() {
            // Paired but not bonded: BlueZ refuses its input (the 2026-11-19 keyboard).
            let half = d.paired && !d.bonded && d.icon.starts_with("input");
            let (state, act) = if d.connected && !half {
                ("connected", Action::BtDisconnect(d.clone()))
            } else if half {
                ("paired, not bonded: enter re-pairs", Action::BtRepair(d.clone()))
            } else if d.paired {
                ("paired", Action::BtConnect(d.clone()))
            } else {
                ("new: enter pairs", Action::BtPair(d.clone()))
            };
            let name: String = d.name.chars().take(22).collect();
            let label = format!("{}{name}", if d.connected { "▸ " } else { "  " });
            let item = Item::new(label, format!("{state} {}", d.icon)).act(act).key('x', Action::BtRemove(d.clone()));
            items.push(match (d.connected, half) {
                (_, true) => item.look(Look::Danger),
                (true, _) => item.look(Look::Live),
                _ => item,
            });
        }
    }
    items
}

/// AUDIO + DISPLAY: volume, mute, the output device, brightness, the colour tuner.
pub(super) fn build_audio() -> Vec<Item> {
    let audio = si::Audio::detect();
    let (vol, muted) = audio.volume();
    let bar = vol.map_or(String::new(), |v| {
        let n = (v / 10).min(10) as usize;
        format!("{}{}", "█".repeat(n), "░".repeat(10 - n))
    });
    let value = format!("{bar} {}%{}", vol.map_or("?".into(), |v| v.to_string()), if muted { "  MUTED" } else { "" });
    let mut items = vec![
        Item::header(&format!("SOUND · {}", audio.backend)),
        Item::new("volume", value).act(Action::Mute).adjust(Adjust::Volume),
    ];
    for out in audio.outputs() {
        let name: String = out.name.chars().take(24).collect();
        let label = format!("{}{name}", if out.default { "▸ " } else { "  " });
        let item = Item::new(label, if out.default { "in use" } else { "" }).act(Action::Output(out.clone()));
        items.push(if out.default { item.look(Look::Live) } else { item });
    }
    items.push(Item::header("DISPLAY"));
    match si::brightness() {
        None => items.push(Item::new("brightness", "this screen has no brightness control")),
        Some(level) => items.push(Item::new("brightness", format!("{level}%")).adjust(Adjust::Brightness)),
    }
    items.push(Item::new("colour tuner", "for screens that show colours their own way").act(Action::Tuner));
    items
}

/// DATE, TIME, LOCALE: the clock, time zone, network time, language, keyboard layout.
pub(super) fn build_time() -> Vec<Item> {
    let tm = si::time_state();
    let get = |k: &str| tm.iter().find(|(key, _)| key == k).map(|(_, v)| v.as_str()).unwrap_or("");
    let ntp = get("NTP") == "yes";
    let (locale, layout) = si::locale_state();
    let synced = if get("NTPSynchronized") == "yes" { " · synchronised" } else { "" };
    vec![
        Item::header("TIME"),
        Item::new("now", chrono::Local::now().format("%a %d %b %Y  %H:%M:%S").to_string()),
        Item::new(
            "time zone",
            match get("Timezone") {
                "" => "?",
                z => z,
            },
        )
        .act(Action::TimeZone),
        Item::new("network time", format!("{}{synced}", if ntp { "on" } else { "off" })).act(Action::Ntp(!ntp)),
        Item::header("LOCALE"),
        Item::new("language", locale),
        Item::new("keyboard layout", if layout.is_empty() { "?".into() } else { layout }).act(Action::Layout),
    ]
}

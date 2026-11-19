//! What SYS's lines do: running a command in the background, pairing Bluetooth (with its
//! PIN on screen), asking twice before touching an input device, each action, adjusting a
//! value, and the questions asked in the line under the menu.

use super::*;

impl Page {
    /// Run a slow action in the background, show what it said, then reload the page.
    pub(super) fn run(&mut self, label: &str, ctx: &Ctx, reload: bool, f: impl FnOnce() -> Done + Send + 'static) {
        if !self.busy.is_empty() {
            return;
        }
        self.busy = label.into();
        self.message.clear();
        let (id, tx) = (self.kind.id(), ctx.tx.clone());
        tokio::task::spawn_blocking(move || {
            let result = f();
            let _ = tx.send(Msg::For(id, Box::new(Got::Done { result, reload })));
        });
    }

    /// Pair a Bluetooth device in the background (`repair`: remove it first). Its progress
    /// arrives as `Got::Pairing`, so a PIN can be shown while it waits for you to type it.
    pub(super) fn pair(&mut self, d: si::Device, repair: bool, ctx: &Ctx) {
        if !self.busy.is_empty() {
            return;
        }
        self.busy = format!("pairing {}", d.name);
        self.pair_code = None;
        self.message.clear();
        let (id, tx) = (self.kind.id(), ctx.tx.clone());
        tokio::task::spawn_blocking(move || {
            if repair {
                let _ = si::bt_do("remove", &d.mac);
            }
            let news_tx = tx.clone();
            let result = si::bt_pair(&d.mac, move |news| {
                let _ = news_tx.send(Msg::For(id, Box::new(Got::Pairing(news))));
            });
            let _ = tx.send(Msg::For(id, Box::new(Got::Done { result, reload: true })));
        });
    }

    /// Input devices: the keyboard you type with may be this one. Ask twice.
    pub(super) fn careful(&mut self, d: &si::Device, what: &str) -> bool {
        let key = format!("{what}:{}", d.mac);
        if d.icon.starts_with("input") && self.armed != key {
            self.armed = key;
            self.message = format!("{} is an input device: {what} it? enter again", d.name);
            return false;
        }
        self.armed.clear();
        true
    }

    /// Do a line's action. Slow ones run in the background (`run`); machine power and
    /// input devices ask for a second enter first.
    pub(super) fn perform(&mut self, action: Action, ctx: &mut Ctx) {
        match action {
            Action::Restart => ctx.requests.push(Request::Quit { restart: true }),
            Action::Quit => ctx.requests.push(Request::Quit { restart: false }),
            Action::Power(what) => {
                if self.armed != what {
                    self.armed = what.into();
                    self.reload(ctx.tx);
                    return;
                }
                self.armed.clear();
                self.run(what, ctx, true, move || si::power(what));
            }
            Action::WifiRadio(on) => self.run("switching Wi-Fi", ctx, true, move || si::set_wifi_radio(on)),
            Action::WifiConnect(net) => {
                if net.active {
                    self.message = format!("already on {}", net.ssid);
                } else if net.known || net.open() {
                    let label = format!("connecting to {}", net.ssid);
                    self.run(&label, ctx, true, move || si::wifi_connect(&net.ssid, None));
                } else {
                    self.ask(Ask::WifiPassword(net.ssid.clone()), &format!("password for {}", net.ssid), "");
                }
            }
            Action::WifiForget(ssid) => {
                let label = format!("forgetting {ssid}");
                self.run(&label, ctx, true, move || si::wifi_forget(&ssid));
            }
            Action::BtPower(on) => self.run("switching Bluetooth", ctx, true, move || si::bt_power(on)),
            Action::BtScan => self.run("scanning", ctx, true, || si::bt_scan(8)),
            Action::BtPair(d) => self.pair(d, false, ctx),
            Action::BtRepair(d) => self.pair(d, true, ctx),
            Action::BtConnect(d) => {
                let label = format!("connecting {}", d.name);
                self.run(&label, ctx, true, move || si::bt_do("connect", &d.mac));
            }
            Action::BtDisconnect(d) => {
                if self.careful(&d, "disconnect") {
                    let label = format!("disconnecting {}", d.name);
                    self.run(&label, ctx, true, move || si::bt_do("disconnect", &d.mac));
                }
            }
            Action::BtRemove(d) => {
                if self.careful(&d, "remove") {
                    let label = format!("removing {}", d.name);
                    self.run(&label, ctx, true, move || si::bt_do("remove", &d.mac));
                }
            }
            Action::Mute => self.run("muting", ctx, true, || si::Audio::detect().toggle_mute()),
            Action::Output(o) => {
                let label = format!("switching to {}", o.name);
                self.run(&label, ctx, true, move || si::Audio::detect().set_default(&o));
            }
            Action::Tuner => ctx.requests.push(Request::Tuner),
            Action::TimeZone => self.ask(Ask::TimeZone, "time zone (e.g. Stockholm, Tokyo, Europe/Berlin)", ""),
            Action::Ntp(on) => self.run("network time", ctx, true, move || si::set_ntp(on)),
            Action::Layout => self.ask(Ask::Layout, "keyboard layout (se, us, de…)", ""),
        }
    }

    /// h/l on volume (5% a step) or brightness (10%).
    pub(super) fn adjust(&mut self, a: Adjust, step: i32, ctx: &Ctx) {
        match a {
            Adjust::Volume => self.run("volume", ctx, true, move || si::Audio::detect().change(5 * step)),
            Adjust::Brightness => self.run("brightness", ctx, true, move || si::set_brightness(10 * step)),
        }
    }

    /// Ask a question in the line under the menu (a Wi-Fi password is typed as dots).
    pub(super) fn ask(&mut self, ask: Ask, label: &str, value: &str) {
        self.input = Input::new(label);
        self.input.mask = matches!(ask, Ask::WifiPassword(_));
        self.input.set(value);
        self.ask = Some(ask);
    }

    /// enter on a question: act on the answer (connect with the password, set the zone…).
    pub(super) fn answered(&mut self, ctx: &mut Ctx) {
        let text = self.input.value.trim().to_string();
        let Some(ask) = self.ask.take() else { return };
        match ask {
            Ask::WifiPassword(ssid) => {
                let label = format!("connecting to {ssid}");
                self.run(&label, ctx, true, move || si::wifi_connect(&ssid, Some(&text)));
            }
            Ask::TimeZone if !text.is_empty() => {
                self.run("time zone", ctx, true, move || {
                    let zones = si::timezones();
                    let low = text.to_lowercase();
                    let hit = zones
                        .iter()
                        .find(|z| z.to_lowercase() == low)
                        .or_else(|| zones.iter().find(|z| z.to_lowercase().contains(&low)));
                    match hit {
                        Some(z) => si::set_timezone(z),
                        None => Err(format!("no time zone like '{text}'")),
                    }
                });
            }
            Ask::Layout if !text.is_empty() => self.run("keyboard", ctx, true, move || si::set_keyboard(&text)),
            _ => {}
        }
    }
}

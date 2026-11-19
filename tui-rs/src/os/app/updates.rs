//! Updates pushed from the PC: asking Mabel for a newer build, u to install it, and her
//! answers coming back.

use super::*;

impl App {
    /// Ask Mabel which build she serves (`quiet`: no toast if it's the same).
    pub(super) fn check_update(&mut self, quiet: bool) {
        if quiet && crate::os::update::installed().is_none() {
            return; // run from the source tree: always current
        }
        let (tx, url) = (self.tx.clone(), self.cfg.url.clone());
        tokio::spawn(async move {
            let build = crate::os::update::served(url).await;
            let _ = tx.send(Msg::For("update", Box::new(crate::os::update::Note::Served { build, quiet })));
        });
    }

    /// F12: install the waiting update, or check for one.
    pub(super) fn update_key(&mut self) {
        if self.updating {
            return;
        }
        if crate::os::update::installed().is_none() {
            self.toast("UPDATE", "This copy runs from the source tree, so it's always current.", false);
        } else if self.update_ready.is_none() {
            self.check_update(false);
        } else {
            self.install_update();
        }
    }

    /// Run the installer in the background (it replaces the binary; then MABEL-OS restarts).
    pub(super) fn install_update(&mut self) {
        self.updating = true;
        let (tx, url) = (self.tx.clone(), self.cfg.url.clone());
        tokio::spawn(async move {
            let (ok, tail) = crate::os::update::apply(url).await;
            let _ = tx.send(Msg::For("update", Box::new(crate::os::update::Note::Installed { ok, tail })));
        });
    }

    /// What the update check or install said: show ▲ UPDATE F12, install by itself when
    /// `auto_update` is on and nobody's using it, or restart into the new build.
    pub(super) fn on_update(&mut self, note: crate::os::update::Note) {
        match note {
            crate::os::update::Note::Served { build, quiet } => {
                if crate::os::update::newer(crate::os::update::installed().as_deref(), build.as_deref()) {
                    if self.update_ready != build {
                        self.update_ready = build;
                        self.toast("UPDATE", "A new MABEL-OS is ready. F12 installs it and restarts.", false);
                    }
                    let idle = self.last_input.elapsed() >= crate::os::update::IDLE_BEFORE_AUTO;
                    let playing = self.player.with(|s| s.current().is_some() && !s.paused);
                    if self.cfg.auto_update && idle && !playing && !self.updating {
                        self.install_update();
                    }
                } else if !quiet {
                    let text = if build.is_some() {
                        "Already the newest build."
                    } else {
                        "Couldn't ask Mabel for the newest build."
                    };
                    self.toast("UPDATE", text, false);
                }
            }
            crate::os::update::Note::Installed { ok, tail } => {
                self.updating = false;
                if ok {
                    self.restart = true; // main() starts the new build in this terminal
                    self.quit = true;
                } else {
                    let text = if tail.is_empty() { "the installer failed".to_string() } else { tail };
                    self.toast("UPDATE FAILED", &text, true);
                }
            }
        }
    }
}

//! TRACKER's `:` command line (`:w`, `:e`, `:bpm`, `:export`, `:midi` …).

use super::*;

impl TrackerView {
    /// Run a `:` line and show its answer (or its error, with ✖) on the status line.
    pub(super) fn command(&mut self, line: &str, ctx: &mut Ctx) {
        let args: Vec<&str> = line.trim().trim_start_matches(':').split_whitespace().collect();
        self.message = match self.run(&args, ctx) {
            Ok(m) => m,
            Err(e) => format!("✖ {e}"),
        };
        self.sync();
    }

    /// One command, split into words. Returns the message to show.
    ///
    /// | command | does |
    /// |---|---|
    /// | `:w [name]` | save (as `name` in `tracker_dir`) |
    /// | `:e [name]` | open a song (no name: the browser); `:files` too |
    /// | `:new`, `:demo` | an empty song, the demo song |
    /// | `:bpm N`, `:groove A B…`, `:tracks N` | tempo, ticks per step, how many tracks |
    /// | `:title …`, `:name …` | the song's title, the instrument's name |
    /// | `:wave sine\|saw\|square\|tri` | fill the current wave with that shape |
    /// | `:export wav\|mid [name]` | render the song to a file in `tracker_dir` (named after it) |
    /// | `:midi [port]` | pick the MIDI output (Pocket Miku) |
    /// | `:help` | list the commands |
    ///
    /// A `!` after `:new`, `:e` or `:demo` drops unsaved changes.
    pub(super) fn run(&mut self, args: &[&str], ctx: &mut Ctx) -> anyhow::Result<String> {
        let Some((first, rest)) = args.split_first() else { return Ok(String::new()) };
        let force = first.ends_with('!');
        let cmd = first.trim_end_matches('!');
        let num = |i: usize| -> anyhow::Result<i64> {
            Ok(rest.get(i).ok_or_else(|| anyhow::anyhow!("a number is missing"))?.parse()?)
        };
        if matches!(cmd, "new" | "e" | "demo") && self.dirty && !force && !(cmd == "e" && rest.is_empty()) {
            return Ok(format!("unsaved changes: :w first, or :{cmd}! to drop them"));
        }
        Ok(match cmd {
            "w" => {
                let target = (!rest.is_empty()).then(|| self.path(&rest.join(" "), "mtk"));
                if target.is_none() && self.song.path.is_none() {
                    return Ok("name it: :w song.mtk".into());
                }
                let path = tk::save(&mut self.song, target.as_deref())?;
                self.dirty = false;
                self.remember();
                format!("saved {}", path.display())
            }
            "files" | "browse" | "e" if rest.is_empty() => {
                self.browse();
                String::new()
            }
            "e" => {
                let song = tk::load(&self.path(&rest.join(" "), "mtk"))?;
                self.load(song);
                self.remember();
                format!("opened {}", self.song.path.as_ref().map_or(String::new(), |p| p.display().to_string()))
            }
            "new" => {
                let tracks = if rest.is_empty() { 4 } else { num(0)?.clamp(1, tk::MAX_TRACKS as i64) as usize };
                self.load(Song { tracks, ..Default::default() });
                format!("a new song, {tracks} tracks")
            }
            "demo" => {
                self.load(tk::demo());
                "the demo song".into()
            }
            "bpm" => {
                let bpm = num(0)?.clamp(20, 400);
                self.push();
                self.song.bpm = bpm;
                self.with_engine(|e| e.bpm = bpm);
                format!("{bpm} bpm")
            }
            "groove" => {
                let groove: Vec<i64> =
                    rest.iter().map(|g| g.parse::<i64>().map(|g| g.clamp(1, 32))).collect::<Result<_, _>>()?;
                self.push();
                self.song.groove = if groove.is_empty() { vec![6, 6] } else { groove };
                format!("groove {}", self.song.groove.iter().map(|g| g.to_string()).collect::<Vec<_>>().join("/"))
            }
            "tracks" => {
                let n = num(0)?.clamp(1, tk::MAX_TRACKS as i64) as usize;
                self.push();
                self.song.tracks = n;
                format!("{n} tracks")
            }
            "title" => {
                self.push();
                if !rest.is_empty() {
                    self.song.title = rest.join(" ");
                }
                String::new()
            }
            "name" => {
                self.push();
                let name: String = rest.join(" ").chars().take(12).collect();
                let id = self.inst_id();
                self.song.instrument(id).name = if name.is_empty() { "inst".into() } else { name };
                String::new()
            }
            "wave" => {
                let shape: fn(f64) -> f64 = match rest.first().copied() {
                    Some("sine") => |i| 7.5 + 7.5 * (i / 32.0 * std::f64::consts::TAU).sin(),
                    Some("saw") => |i| i / 31.0 * 15.0,
                    Some("square") => |i| if i < 16.0 { 15.0 } else { 0.0 },
                    Some("tri") => |i| 15.0 - (i - 16.0).abs() * 15.0 / 16.0,
                    _ => return Ok("waves: sine saw square tri".into()),
                };
                self.push();
                let id = self.wave_id();
                self.song.waves.insert(id, (0..32).map(|i| shape(i as f64).round() as i64).collect());
                format!("wave {id:02X} = {}", rest[0])
            }
            "export" => {
                let how = rest.first().copied().filter(|h| matches!(*h, "wav" | "mid")).unwrap_or("wav");
                let stem = self
                    .song
                    .path
                    .as_ref()
                    .and_then(|p| p.file_stem())
                    .map_or(self.song.title.replace(' ', "-"), |s| s.to_string_lossy().into_owned());
                let name = rest.get(1).map_or(format!("{stem}.{how}"), |n| n.to_string());
                let path = self.path(&name, how);
                if let Some(dir) = path.parent() {
                    std::fs::create_dir_all(dir)?;
                }
                if how == "wav" {
                    let seconds = ts::export_wav(&self.song, &path, self.rate)?;
                    format!("exported {} ({seconds:.0} s)", path.display())
                } else {
                    let notes = ts::export_midi(&self.song, &path, self.rate)?;
                    format!("exported {} ({notes} notes)", path.display())
                }
            }
            "midi" => {
                let ports = ts::midi_ports();
                let Some(choice) = rest.first() else {
                    let now = if self.song.midi_port.is_empty() { "auto" } else { &self.song.midi_port };
                    let list: Vec<String> = ports.iter().enumerate().map(|(i, p)| format!("{i}:{p}")).collect();
                    return Ok(format!(
                        "MIDI {now} · ports: {}",
                        if list.is_empty() { "none".into() } else { list.join(" ") }
                    ));
                };
                self.push();
                self.song.midi_port = match choice.parse::<usize>() {
                    Ok(i) if i < ports.len() => ports[i].clone(),
                    _ if *choice == "auto" => String::new(),
                    _ => choice.to_string(),
                };
                let port = self.song.midi_port.clone();
                self.with_engine(|e| {
                    e.midi.all_off();
                    e.midi = Midi::open(Some(&port));
                });
                format!("MIDI {}", if port.is_empty() { "auto" } else { &port })
            }
            "help" | "h" => {
                let cmds: Vec<String> = tk::COMMANDS.iter().map(|(k, v)| format!("{k} {v}")).collect();
                ctx.toast(
                    "TRACKER",
                    &format!(
                        ":w [file] · :e file · :new [tracks] · :demo · :bpm N · :groove 6 6 · :tracks N\n\
                         :title … · :name … (instrument) · :wave sine|saw|square|tri · :midi [N|path|auto]\n\
                         :export wav|mid [file]\nCommands: {}",
                        cmds.join("  ")
                    ),
                );
                String::new()
            }
            _ => format!("unknown command :{cmd} (try :help)"),
        })
    }

    // ── drawing ──────────────────────────────────────────────────────────
}

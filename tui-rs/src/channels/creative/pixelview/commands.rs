//! PIXEL's `:` command line (`:new`, `:w`, `:e`, `:avatar`, `:export`, `:palette` …).

use super::*;

impl PixelView {
    /// Run a `:` line and show its answer (or its error, with ✖) on the status line.
    pub(super) fn command(&mut self, line: &str, ctx: &mut Ctx) {
        let args: Vec<&str> = line.trim().trim_start_matches(':').split_whitespace().collect();
        self.message = match self.run(&args, ctx) {
            Ok(m) => m,
            Err(e) => format!("✖ {e}"),
        };
    }

    /// One command, split into words; returns the message to show. The full list is
    /// `COMMANDS` in `mod.rs` (also what `:help` shows). A `!` after `:new` or `:e` drops
    /// unsaved changes.
    pub(super) fn run(&mut self, args: &[&str], ctx: &mut Ctx) -> anyhow::Result<String> {
        let Some((first, rest)) = args.split_first() else { return Ok(String::new()) };
        let force = first.ends_with('!');
        let cmd = first.trim_end_matches('!');
        let num = |i: usize| -> anyhow::Result<i64> {
            Ok(rest.get(i).ok_or_else(|| anyhow::anyhow!("a number is missing"))?.parse()?)
        };
        if matches!(cmd, "new" | "e" | "avatar") && self.dirty && !force && !(cmd == "e" && rest.is_empty()) {
            return Ok(format!("unsaved changes: :w first, or :{cmd}! to drop them"));
        }
        Ok(match cmd {
            "new" => {
                let (w, h) = if rest.len() >= 2 { (num(0)?, num(1)?) } else { (32, 32) };
                self.doc = Doc::new(w, h, rest.get(2).copied().unwrap_or("db32"));
                self.hist = History::default();
                (self.frame, self.layer, self.dirty, self.cx, self.cy) = (0, 0, false, 0, 0);
                self.fg = self.doc.palette.get(4.min(self.doc.palette.len().saturating_sub(1))).copied();
                self.zoom = 0;
                format!("new {}×{}", self.doc.w, self.doc.h)
            }
            "w" => {
                let target = rest.first().map(|n| self.path(n));
                if target.is_none() && self.doc.path.is_none() {
                    return Ok("name it: :w sprite.mpx (or .png)".into());
                }
                let p = px::save(&mut self.doc, target.as_deref())?;
                self.dirty = false;
                self.remember();
                format!("saved {}", p.display())
            }
            "files" | "browse" => {
                self.browse();
                String::new()
            }
            "e" if rest.is_empty() => {
                self.browse();
                String::new()
            }
            "e" => {
                let p = self.path(&rest.join(" "));
                self.open(&p)?;
                format!("opened {}", p.display())
            }
            "avatar" => {
                let dir = avatar_dir(&self.avatar);
                crate::os::avatar::ensure_user_copy(&dir)?;
                self.open(&dir)?;
                let tags: Vec<&str> = self.doc.tags.iter().map(|t| t.name.as_str()).collect();
                format!("Mabel's avatar ({}) · :w writes the strips back", tags.join(", "))
            }
            "export" => {
                let how = rest.first().copied().filter(|h| matches!(*h, "png" | "strip" | "sheet")).unwrap_or("png");
                let nums: Vec<usize> = rest.iter().skip(1).filter_map(|a| a.parse().ok()).collect();
                let names: Vec<&&str> = rest.iter().skip(1).filter(|a| a.parse::<usize>().is_err()).collect();
                let scale = nums.first().copied().unwrap_or(1);
                let columns = nums.get(1).copied().unwrap_or(4);
                let stem = self.doc.path.as_ref().map_or("sprite".into(), |p| {
                    p.file_stem().map_or("sprite".into(), |s| s.to_string_lossy().into_owned())
                });
                let path = match names.first() {
                    Some(n) => self.path(n),
                    None => self.dir.join(format!(
                        "{stem}-{how}{}.png",
                        if scale == 1 { String::new() } else { format!("@{scale}x") }
                    )),
                };
                let what = match how {
                    "strip" => px::Export::Strip,
                    "sheet" => px::Export::Sheet(columns),
                    _ => px::Export::Png(self.frame),
                };
                px::export(&self.doc, &path, what, scale)?;
                format!("exported {}", path.display())
            }
            "palette" => {
                let Some(name) = rest.first().filter(|n| px::PALETTES.iter().any(|(p, _)| p == *n)) else {
                    return Ok(format!(
                        "palettes: {}",
                        px::PALETTES.iter().map(|(n, _)| *n).collect::<Vec<_>>().join(" ")
                    ));
                };
                self.hist.push(&self.doc);
                self.doc.palette = px::palette(name);
                self.doc.adopt_colors();
                self.dirty = true;
                format!("palette {name}")
            }
            "hex" => {
                let Some(c) = rest.first().and_then(|h| px::parse_color(h)) else {
                    return Ok("a colour like :hex #ff8800".into());
                };
                if !self.doc.palette.contains(&c) && self.doc.palette.len() < px::MAX_PALETTE {
                    self.doc.palette.push(c);
                }
                self.fg = Some(c);
                format!("colour {}", px::hex(c))
            }
            "flip" | "rotate" | "resize" => {
                self.hist.push(&self.doc);
                match cmd {
                    "flip" => self.doc.flip(rest.first() != Some(&"v")),
                    "rotate" => self.doc.rotate(rest.first() != Some(&"ccw")),
                    _ => self.doc.resize(num(0)?, num(1)?),
                }
                self.dirty = true;
                format!("{cmd} {}", rest.join(" ")).trim().to_string()
            }
            "ms" => {
                if rest.first() == Some(&"all") {
                    let ms = num(1)? as u32;
                    self.doc.frames.iter_mut().for_each(|f| f.ms = ms);
                } else {
                    self.doc.frames[self.frame].ms = num(0)? as u32;
                }
                self.dirty = true;
                String::new()
            }
            "tag" => {
                let name = rest.first().ok_or_else(|| anyhow::anyhow!(":tag NAME FROM TO"))?.to_string();
                let (a, b) = (num(1)? - 1, num(2)? - 1);
                self.doc.tags.retain(|t| t.name != name);
                self.doc.tags.push(px::Tag {
                    name: name.clone(),
                    start: a.min(b).max(0) as usize,
                    end: a.max(b).max(0) as usize,
                    extra: Default::default(),
                });
                self.dirty = true;
                format!("tag {name} {}–{}", a.min(b) + 1, a.max(b) + 1)
            }
            "untag" => {
                let name = rest.first().copied().unwrap_or("");
                self.doc.tags.retain(|t| t.name != name);
                self.dirty = true;
                String::new()
            }
            "layer" | "frame" => {
                let what = rest.first().copied().unwrap_or("");
                self.hist.push(&self.doc);
                self.dirty = true;
                match (cmd, what) {
                    ("layer", "name") => {
                        let name = rest[1..].join(" ");
                        if !name.is_empty() {
                            self.doc.layers[self.layer].name = name;
                        }
                    }
                    ("layer", "del") => self.layer = self.doc.remove_layer(self.layer),
                    ("layer", "up") => self.layer = self.doc.move_layer(self.layer, 1),
                    ("layer", "down") => self.layer = self.doc.move_layer(self.layer, -1),
                    ("frame", "del") => self.frame = self.doc.remove_frame(self.frame),
                    ("frame", "right") => self.frame = self.doc.move_frame(self.frame, 1),
                    ("frame", "left") => self.frame = self.doc.move_frame(self.frame, -1),
                    _ => {}
                }
                String::new()
            }
            "help" | "h" => {
                ctx.toast("PIXEL COMMANDS", COMMANDS);
                String::new()
            }
            other => format!("unknown command :{other} (try :help)"),
        })
    }
}

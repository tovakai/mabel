//! Courses in the vault (`<vault>/Courses/<id>/`), in the layout `Courses/COURSE_FORMAT.md`
//! describes: course.md, tutor.md, NN-module/NN-lesson.md + .tutor.md + .files/, progress.md.
//!
//! Nothing here knows what a course teaches. COURSE (course.rs) shows the lessons, runs their
//! checks, and sends questions to Mabel with the tutor's material attached (`context`).

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::vault::split_frontmatter;

/// The tutor's material goes along with the first question of a lesson, then every this many.
pub const FULL_CONTEXT_EVERY: u32 = 8;
/// A file sent to the tutor is cut at this many characters.
const FILE_MAX: usize = 12_000;
/// What separates the attached material from his question (also used to show only the question).
pub const QUESTION_MARK: &str = "=== His question ===";

#[derive(Clone, Debug)]
pub struct Lesson {
    /// "01-first-light/01-hello-tape": how progress refers to it.
    pub id: String,
    pub path: PathBuf,
    pub title: String,
    pub goal: String,
    pub minutes: Option<u32>,
    pub project: Option<String>,
    pub files: Vec<String>,
    pub check: Option<String>,
    pub body: String,
}

impl Lesson {
    pub fn tutor_path(&self) -> PathBuf {
        self.path.with_extension("tutor.md")
    }

    pub fn starter_dir(&self) -> PathBuf {
        self.path.with_extension("files")
    }
}

#[derive(Clone, Debug)]
pub struct Module {
    pub title: String,
    pub intro: String,
    pub lessons: Vec<Lesson>,
}

#[derive(Clone, Debug)]
pub struct Course {
    pub dir: PathBuf,
    pub title: String,
    pub description: String,
    pub author: String,
    pub workspace: PathBuf,
    pub intro: String,
    pub modules: Vec<Module>,
}

impl Course {
    pub fn lessons(&self) -> impl Iterator<Item = &Lesson> {
        self.modules.iter().flat_map(|m| m.lessons.iter())
    }

    pub fn lesson(&self, id: &str) -> Option<&Lesson> {
        self.lessons().find(|l| l.id == id)
    }

    /// Where a lesson's work happens: `<workspace>/<project>` (or the workspace).
    pub fn project_dir(&self, lesson: &Lesson) -> PathBuf {
        match &lesson.project {
            Some(p) => self.workspace.join(p),
            None => self.workspace.clone(),
        }
    }

    pub fn progress_path(&self) -> PathBuf {
        self.dir.join("progress.md")
    }
}

fn get<'a>(pairs: &'a [(String, Vec<String>)], key: &str) -> Option<&'a Vec<String>> {
    pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v)
}

fn one(pairs: &[(String, Vec<String>)], key: &str) -> String {
    get(pairs, key).and_then(|v| v.first()).cloned().unwrap_or_default()
}

/// "01-first-light" → "First light" (when a module or lesson has no title).
fn from_slug(slug: &str) -> String {
    let words = slug.split_once('-').map_or(slug, |(_, rest)| rest).replace('-', " ");
    let mut c = words.chars();
    c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default()
}

/// Folders or files whose names start with two digits and a dash, in order.
fn numbered(dir: &Path, want_dir: bool) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> =
        std::fs::read_dir(dir).map(|rd| rd.flatten().map(|e| e.path()).collect()).unwrap_or_default();
    out.retain(|p| {
        let name = p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let b = name.as_bytes();
        let numbered = b.len() > 3 && b[0].is_ascii_digit() && b[1].is_ascii_digit() && b[2] == b'-';
        numbered
            && if want_dir { p.is_dir() } else { p.is_file() && name.ends_with(".md") && !name.ends_with(".tutor.md") }
    });
    out.sort();
    out
}

fn read_lesson(module: &str, path: &Path) -> Option<Lesson> {
    let text = std::fs::read_to_string(path).ok()?;
    let (pairs, body, _) = split_frontmatter(&text);
    let stem = path.file_stem()?.to_string_lossy().into_owned();
    let title = one(&pairs, "title");
    Some(Lesson {
        id: format!("{module}/{stem}"),
        path: path.to_path_buf(),
        title: if title.is_empty() { from_slug(&stem) } else { title },
        goal: one(&pairs, "goal"),
        minutes: one(&pairs, "minutes").parse().ok(),
        project: Some(one(&pairs, "project")).filter(|p| !p.is_empty()),
        files: get(&pairs, "files").cloned().unwrap_or_default(),
        check: Some(one(&pairs, "check")).filter(|c| !c.is_empty()),
        body: body.to_string(),
    })
}

fn read_course(dir: &Path) -> Option<Course> {
    let text = std::fs::read_to_string(dir.join("course.md")).ok()?;
    let (pairs, body, _) = split_frontmatter(&text);
    let id = dir.file_name()?.to_string_lossy().into_owned(); // the folder name
    let workspace = one(&pairs, "workspace");
    let modules = numbered(dir, true)
        .iter()
        .map(|m| {
            let slug = m.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            let (title, intro) = match std::fs::read_to_string(m.join("module.md")) {
                Ok(t) => {
                    let (p, b, _) = split_frontmatter(&t);
                    (one(&p, "title"), b.trim().to_string())
                }
                Err(_) => (String::new(), String::new()),
            };
            Module {
                title: if title.is_empty() { from_slug(&slug) } else { title },
                intro,
                lessons: numbered(m, false).iter().filter_map(|l| read_lesson(&slug, l)).collect(),
            }
        })
        .collect();
    let title = one(&pairs, "title");
    Some(Course {
        title: if title.is_empty() { id.clone() } else { title },
        description: one(&pairs, "description"),
        author: one(&pairs, "author"),
        workspace: if workspace.is_empty() {
            crate::config::home().join("code").join("courses").join(&id)
        } else {
            crate::config::expand(&workspace)
        },
        intro: body.trim().to_string(),
        modules,
        dir: dir.to_path_buf(),
    })
}

/// Every course in `<vault>/Courses/`, by folder name.
pub fn load_all(vault: &Path) -> Vec<Course> {
    let mut dirs: Vec<PathBuf> = std::fs::read_dir(vault.join("Courses"))
        .map(|rd| rd.flatten().map(|e| e.path()).filter(|p| p.join("course.md").is_file()).collect())
        .unwrap_or_default();
    dirs.sort();
    dirs.iter().filter_map(|d| read_course(d)).collect()
}

/// The student's progress in one course (progress.md: a checklist for people, JSON for MABEL-OS).
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Progress {
    pub done: BTreeSet<String>,
    pub current: Option<String>,
    /// Lesson id → Mabel's chat session for it.
    pub sessions: BTreeMap<String, String>,
    /// Lesson id → questions asked so far (when to send the tutor's material again).
    pub asked: BTreeMap<String, u32>,
}

impl Progress {
    pub fn load(course: &Course) -> Progress {
        let text = std::fs::read_to_string(course.progress_path()).unwrap_or_default();
        let json = text.split("```json").nth(1).and_then(|rest| rest.split("```").next());
        json.and_then(|j| serde_json::from_str(j).ok()).unwrap_or_default()
    }

    pub fn save(&self, course: &Course) -> std::io::Result<()> {
        let mut out = format!(
            "---\nmabel-os: course progress\n---\n\n# {}: progress\n\nWritten by MABEL-OS's COURSE app. Tick lessons there (`d`), not here.\n\n",
            course.title
        );
        for l in course.lessons() {
            let tick = if self.done.contains(&l.id) { "x" } else { " " };
            out.push_str(&format!("- [{tick}] {}: {}\n", l.id, l.title));
        }
        out.push_str(&format!("\n```json\n{}\n```\n", serde_json::to_string_pretty(self).unwrap_or_default()));
        let tmp = course.progress_path().with_extension("md.tmp");
        std::fs::write(&tmp, out)?;
        std::fs::rename(&tmp, course.progress_path())
    }
}

fn read_capped(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    Some(if text.chars().count() > FILE_MAX {
        text.chars().take(FILE_MAX).collect::<String>() + "\n… (cut here)"
    } else {
        text
    })
}

/// The message Mabel gets: (when `full`) the course's tutor guide, the lesson he's reading and
/// her manual for it; always his current files; then his question.
pub fn context(course: &Course, lesson: &Lesson, question: &str, full: bool) -> String {
    let mut out = String::from(
        "[COURSE: you are tutoring Anthon through a course in MABEL-OS's COURSE app. The material below is \
         for you; he only sees his question and your answer. Follow your tutor manual: hints before answers.]\n\n",
    );
    if full {
        if let Some(t) = read_capped(&course.dir.join("tutor.md")) {
            out.push_str(&format!("=== Tutor guide for the course “{}” ===\n{t}\n\n", course.title));
        }
        out.push_str(&format!("=== The lesson he is on: {} ===\n{}\n\n", lesson.title, lesson.body.trim()));
        if let Some(t) = read_capped(&lesson.tutor_path()) {
            out.push_str(&format!("=== Your manual for this lesson ===\n{t}\n\n"));
        }
    } else {
        out.push_str(&format!(
            "(Still the lesson “{}”; your tutor guide and manual for it came earlier in this chat.)\n\n",
            lesson.title
        ));
    }
    let dir = course.project_dir(lesson);
    for f in &lesson.files {
        match read_capped(&dir.join(f)) {
            Some(text) => out.push_str(&format!("=== His code: {f} ===\n```\n{text}\n```\n\n")),
            None => out.push_str(&format!("=== His code: {f} === (doesn't exist yet)\n\n")),
        }
    }
    out.push_str(&format!("{QUESTION_MARK}\n{question}"));
    out
}

/// Just his question, from a message `context` built (for showing old chats).
pub fn question_of(message: &str) -> &str {
    message.split_once(QUESTION_MARK).map_or(message, |(_, q)| q.trim_start())
}

/// Copy a lesson's starter files into its project. Existing files are never touched.
/// Returns how many files were copied.
pub fn start_files(course: &Course, lesson: &Lesson) -> std::io::Result<usize> {
    fn copy(from: &Path, to: &Path, n: &mut usize) -> std::io::Result<()> {
        for entry in std::fs::read_dir(from)?.flatten() {
            let (src, dst) = (entry.path(), to.join(entry.file_name()));
            if src.is_dir() {
                std::fs::create_dir_all(&dst)?;
                copy(&src, &dst, n)?;
            } else if !dst.exists() {
                std::fs::copy(&src, &dst)?;
                *n += 1;
            }
        }
        Ok(())
    }
    let from = lesson.starter_dir();
    let mut n = 0;
    if from.is_dir() {
        let to = course.project_dir(lesson);
        std::fs::create_dir_all(&to)?;
        copy(&from, &to, &mut n)?;
    }
    Ok(n)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(path: &Path, text: &str) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }

    #[test]
    fn a_course_is_read_in_order_with_progress_and_context() {
        let vault = std::env::temp_dir().join(format!("mabel-courses-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&vault);
        let c = vault.join("Courses/demo");
        write(&c.join("course.md"), "---\ntitle: Demo\nworkspace: /tmp/demo-ws\n---\nHello.\n");
        write(&c.join("tutor.md"), "Be kind.");
        write(&c.join("02-later/01-x.md"), "---\ntitle: Later\n---\nLater body.");
        write(&c.join("01-first-steps/02-two.md"), "---\ntitle: Two\nfiles: [src/main.rs]\ncheck: true\n---\nTwo.");
        write(&c.join("01-first-steps/01-one.md"), "---\ntitle: One\nproject: p\n---\nOne body.");
        write(&c.join("01-first-steps/01-one.tutor.md"), "Secret solution.");
        write(&c.join("01-first-steps/01-one.files/src/main.rs"), "fn main() {}");
        write(&vault.join("Courses/COURSE_FORMAT.md"), "not a course");

        let courses = load_all(&vault);
        assert_eq!(courses.len(), 1);
        let course = &courses[0];
        assert_eq!(course.modules[0].title, "First steps"); // from the folder name
        let ids: Vec<&str> = course.lessons().map(|l| l.id.as_str()).collect();
        assert_eq!(ids, ["01-first-steps/01-one", "01-first-steps/02-two", "02-later/01-x"]);
        let two = course.lesson("01-first-steps/02-two").unwrap();
        assert_eq!((two.files.clone(), two.check.as_deref()), (vec!["src/main.rs".to_string()], Some("true")));

        let one = course.lesson("01-first-steps/01-one").unwrap();
        let full = context(course, one, "why?", true);
        assert!(full.contains("Be kind.") && full.contains("Secret solution.") && full.contains("One body."));
        assert_eq!(question_of(&full), "why?");
        let short = context(course, one, "and now?", false);
        assert!(!short.contains("Secret solution.") && short.ends_with("and now?"));

        let mut p = Progress::default();
        p.done.insert(one.id.clone());
        p.sessions.insert(one.id.clone(), "abc".into());
        p.save(course).unwrap();
        let back = Progress::load(course);
        assert!(back.done.contains(&one.id) && back.sessions[&one.id] == "abc");
        assert!(std::fs::read_to_string(course.progress_path()).unwrap().contains("- [x] 01-first-steps/01-one: One"));
        let _ = std::fs::remove_dir_all(&vault);
    }

    #[test]
    fn starter_files_never_overwrite() {
        let base = std::env::temp_dir().join(format!("mabel-course-start-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let lesson_path = base.join("Courses/demo/01-m/01-l.md");
        write(&lesson_path, "---\nproject: p\n---\n");
        write(&base.join("Courses/demo/01-m/01-l.files/a.txt"), "new");
        write(&base.join("Courses/demo/01-m/01-l.files/sub/b.txt"), "new");
        let course = Course {
            dir: base.join("Courses/demo"),
            title: "Demo".into(),
            description: String::new(),
            author: String::new(),
            workspace: base.join("ws"),
            intro: String::new(),
            modules: vec![],
        };
        let lesson = read_lesson("01-m", &lesson_path).unwrap();
        write(&base.join("ws/p/a.txt"), "mine");
        assert_eq!(start_files(&course, &lesson).unwrap(), 1);
        assert_eq!(std::fs::read_to_string(base.join("ws/p/a.txt")).unwrap(), "mine");
        assert_eq!(std::fs::read_to_string(base.join("ws/p/sub/b.txt")).unwrap(), "new");
        let _ = std::fs::remove_dir_all(&base);
    }
}

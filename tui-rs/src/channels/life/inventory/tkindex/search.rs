//! Sorting things the way the web UI does (articles and Swedish endings ignored) and searching
//! (names, nicknames, spec ids, synonyms, Swedish word endings).

use super::*;

/// Words sorting skips at the start of a name (English and Swedish), and Swedish endings
/// search ignores so "lådan" finds "låda".
pub(super) const ARTICLE_WORDS: [&str; 7] = ["the", "a", "an", "den", "det", "de", "en"];
pub(super) const ENDINGS: [&str; 12] = ["erna", "arna", "orna", "na", "en", "et", "er", "ar", "or", "n", "t", "s"];

impl World {
    /// The title things sort by: the sort key, else the name without a leading article.
    pub fn sort_title(i: &Item) -> String {
        if let Some(k) = i.sort_key.as_deref().filter(|k| !k.trim().is_empty()) {
            return k.trim().to_string();
        }
        let name = i.name.trim();
        let mut words = name.splitn(2, char::is_whitespace);
        let first = words.next().unwrap_or("");
        match words.next() {
            Some(rest)
                if ARTICLE_WORDS.contains(&first.to_lowercase().as_str()) || first.eq_ignore_ascii_case("ett") =>
            {
                rest.trim().to_string()
            }
            _ => name.to_string(),
        }
    }

    /// Swedish order (å ä ö after z), numbers by value.
    pub fn in_order(mut items: Vec<Item>) -> Vec<Item> {
        let collate = |t: &str| {
            let t: String = t
                .to_lowercase()
                .chars()
                .map(|c| match c {
                    'å' => "{1".to_string(),
                    'ä' | 'æ' => "{2".to_string(),
                    'ö' | 'ø' => "{3".to_string(),
                    c => c.to_string(),
                })
                .collect();
            let mut out = String::new();
            let mut digits = String::new();
            for c in t.chars().chain(std::iter::once('\0')) {
                if c.is_ascii_digit() {
                    digits.push(c);
                    continue;
                }
                if !digits.is_empty() {
                    out.push_str(&format!("{digits:0>8}"));
                    digits.clear();
                }
                if c != '\0' {
                    out.push(c);
                }
            }
            out
        };
        items.sort_by_key(|i| (collate(&Self::sort_title(i)), i.id.clone()));
        items
    }

    /// Other words for a search word (Swedish endings stripped, synonyms).
    /// A search word and the words that mean the same (synonyms, and without its ending).
    fn equivalents(&self, t: &str) -> Vec<String> {
        let mut found: HashSet<String> = HashSet::new();
        let mut forms = vec![t.to_string()];
        for e in ENDINGS {
            if t.ends_with(e) && t.chars().count() >= e.chars().count() + 3 {
                forms.push(t[..t.len() - e.len()].to_string());
            }
        }
        for w in &forms {
            if let Some(others) = self.synonyms.get(w) {
                found.insert(w.clone());
                found.extend(others.iter().cloned());
            }
        }
        if found.is_empty() && t.chars().count() >= 4 {
            for (w, others) in &self.synonyms {
                if w.starts_with(t) {
                    found.insert(w.clone());
                    found.extend(others.iter().cloned());
                }
            }
        }
        found.remove(t);
        found.into_iter().collect()
    }

    /// Does item `i` match every word of `q` (in its name, nicknames, spec id, search words)?
    fn matches(&self, i: &Item, q: &str) -> bool {
        if q.trim().is_empty() {
            return true;
        }
        let fields = [
            Some(i.name.as_str()),
            Some(i.id.as_str()),
            i.address.as_deref(),
            i.spec_code.as_deref(),
            i.category.as_deref(),
            i.sort_key.as_deref(),
            i.search_words.as_deref(),
        ];
        let mut hay: String = fields.iter().flatten().copied().collect::<Vec<_>>().join(" ");
        if let Some(n) = self.nick.get(&i.id) {
            hay.push(' ');
            hay.push_str(&n.join(" "));
        }
        let hay = hay.to_lowercase();
        let starts: String = format!(
            " {}",
            hay.chars()
                .map(|c| if c.is_ascii_lowercase() || c.is_ascii_digit() || "åäöé".contains(c) { c } else { ' ' })
                .collect::<String>()
        );
        q.to_lowercase()
            .split_whitespace()
            .all(|t| hay.contains(t) || self.equivalents(t).iter().any(|o| starts.contains(&format!(" {o}"))))
    }

    /// Items matching `q` (and `status`, if given), in sort order.
    pub fn search(&self, q: &str, status: Option<&str>) -> Vec<Item> {
        let live: Vec<Item> = self
            .items
            .iter()
            .filter(|i| i.status() != "retired")
            .filter(|i| status.is_none_or(|s| i.status() == s))
            .filter(|i| self.matches(i, q))
            .cloned()
            .collect();
        Self::in_order(live).into_iter().take(200).collect()
    }

    /// How many items have each status.
    pub fn status_counts(&self) -> HashMap<String, usize> {
        let mut out = HashMap::new();
        for i in &self.items {
            *out.entry(i.status().to_string()).or_default() += 1;
        }
        out
    }
}

//! Mabel's timestamps: ISO 8601, either with a zone ("…Z", "+01:00") or naive (local time).

use chrono::{DateTime, Local, NaiveDateTime, TimeZone};

pub fn parse_local(value: &str) -> Option<DateTime<Local>> {
    if let Ok(t) = DateTime::parse_from_rfc3339(value) {
        return Some(t.with_timezone(&Local));
    }
    let naive = NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
        .or_else(|_| NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f"))
        .ok()?;
    Local.from_local_datetime(&naive).earliest()
}

/// "14:05", or "--:--" when there's no (readable) time.
pub fn hm(value: Option<&str>) -> String {
    value.and_then(parse_local).map_or_else(|| "--:--".into(), |t| t.format("%H:%M").to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn naive_and_zoned() {
        assert_eq!(hm(Some("2026-11-18T10:05:00")), "10:05");
        assert_eq!(hm(Some("2026-11-18T10:05:00.123456")), "10:05");
        assert_eq!(hm(Some("2026-11-18T10:05:00+00:00")).len(), 5);
        assert_eq!(hm(None), "--:--");
        assert_eq!(hm(Some("soon")), "--:--");
    }
}

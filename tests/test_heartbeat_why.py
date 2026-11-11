"""The "why didn't she…?" trace: changes only, outcomes, and when the heartbeat wasn't running."""
from datetime import datetime, timedelta, timezone

from src.heartbeat import why

T0 = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)


def _at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_only_changes_are_kept(tmp_path):
    for m in range(0, 30):
        why.record("news", ["Anthon hasn't been away 5 min"], now=_at(m), data_dir=tmp_path)
    why.record("news", ["GPU not free (SteamVR)", "Anthon hasn't been away 5 min"], now=_at(30), data_dir=tmp_path)
    assert len(why.load(since=T0, data_dir=tmp_path)) == 2


def test_explain_reads_as_periods_with_outcomes_and_sleep(tmp_path):
    for m in (0, 5, 10):
        why.pulse(now=_at(m), data_dir=tmp_path)
    why.record("news", ["Anthon hasn't been away 5 min"], now=_at(0), data_dir=tmp_path)
    why.record("news", [], now=_at(10), data_dir=tmp_path)
    why.done("news", "read", now=_at(11), data_dir=tmp_path)
    why.pulse(now=_at(120), data_dir=tmp_path)                    # nothing for ~2 h: asleep
    report = why.explain("news", hours=3, now=_at(125), data_dir=tmp_path)
    news = report["jobs"]["news"]
    assert news[0]["reasons"] == ["Anthon hasn't been away 5 min"] and news[0]["to"] == _at(10).isoformat()
    assert news[1].get("clear") and news[2]["ran"] == "read"
    assert len(report["asleep"]) == 1
    text = why.render(report)
    assert "held back: Anthon hasn't been away 5 min" in text and "ran → read" in text and "not running" in text


def test_a_run_makes_the_next_block_news_again(tmp_path):
    why.record("dream", ["already dreamt today"], now=_at(0), data_dir=tmp_path)
    why.done("dream", "dreamt", now=_at(1), data_dir=tmp_path)
    why.record("dream", ["already dreamt today"], now=_at(2), data_dir=tmp_path)
    assert len(why.load(since=T0, data_dir=tmp_path)) == 3


def test_trim_keeps_two_weeks(tmp_path):
    why.record("news", ["old"], now=_at(0) - timedelta(days=20), data_dir=tmp_path)
    why.record("news", ["new"], now=_at(0), data_dir=tmp_path)
    why.trim(now=_at(1), data_dir=tmp_path)
    assert [e["reasons"] for e in why.load(since=T0 - timedelta(days=30), data_dir=tmp_path)] == [["new"]]


def test_counter_only_changes_read_as_one_span(tmp_path):
    from datetime import datetime, timedelta, timezone
    from src.heartbeat import why
    why._last.clear()
    t0 = datetime(2026, 11, 11, 0, 5, tzinfo=timezone.utc)
    for i in range(3):
        why.record("research", [f"last session started {5 + i} min ago (gap 3 h)"], now=t0 + timedelta(minutes=i), data_dir=tmp_path)
    why.record("research", ["already 2 sessions today"], now=t0 + timedelta(minutes=3), data_dir=tmp_path)
    periods = why.explain("research", hours=1, now=t0 + timedelta(minutes=10), data_dir=tmp_path)["jobs"]["research"]
    assert [p["reasons"] for p in periods] == [["last session started 7 min ago (gap 3 h)"], ["already 2 sessions today"]]
    assert periods[0]["to"] == (t0 + timedelta(minutes=3)).isoformat(timespec="seconds")

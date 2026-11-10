"""Noticing slow replies: relative to his own usual, a higher bar when she wrote first, plain facts, kept."""
import json
from datetime import datetime, timedelta, timezone

from src.persona import pace

NOW = datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc)
OPENER = json.dumps({"news_day": "2026-11-10"})


def _wait(minutes, mid="m1", first=False):
    return {"message_id": mid, "since": NOW - timedelta(minutes=minutes), "until": NOW, "minutes": minutes,
            "first": first}


def test_baselines_split_conversation_from_her_openers():
    rows, t = [], NOW - timedelta(days=1)
    for gap in [1, 2, 1, 3, 2, 1, 2, 1, 2, 1, 600]:
        rows += [("s", "assistant", t, "{}"), ("s", "user", t + timedelta(minutes=gap), "{}")]
        t += timedelta(minutes=gap + 1)
    for i, gap in enumerate([20, 40, 50, 90, 200]):
        rows += [(f"o{i}", "assistant", t, OPENER), (f"o{i}", "user", t + timedelta(minutes=gap), "{}")]
    rows += [("smoke", "assistant", t, json.dumps({"cancelled": True})), ("smoke", "user", t + timedelta(minutes=2), "{}")]
    rows.sort(key=lambda r: (r[0], r[2]))
    # the cancelled smoke chat is no opener: its reply counts as conversation (1,1,1,1,1,2,2,2,2,2,3)
    assert pace.baselines(rows=rows) == {"conversation": 2, "first": 50}


def test_reply_wait_knows_when_she_wrote_first():
    opener = [("a1", "assistant", NOW - timedelta(hours=4), OPENER), ("u1", "user", NOW - timedelta(seconds=5), "{}")]
    wait = pace.reply_wait("s", now=NOW, messages=opener)
    assert wait["first"] and 239 < wait["minutes"] < 240
    chat = [("u0", "user", NOW - timedelta(hours=1), "{}"), ("a1", "assistant", NOW - timedelta(minutes=50), "{}"),
            ("u1", "user", NOW - timedelta(seconds=5), "{}")]
    assert not pace.reply_wait("s", now=NOW, messages=chat)["first"]
    old = [("a1", "assistant", NOW - timedelta(hours=2), "{}"), ("u1", "user", NOW - timedelta(hours=1), "{}")]
    assert pace.reply_wait("s", now=NOW, messages=old) is None


def test_quick_replies_say_nothing(tmp_path):
    assert pace.note("s", now=NOW, data_dir=tmp_path, wait=_wait(8), usual=1.5, doing=[]) is None
    assert pace.note("s", now=NOW, data_dir=tmp_path, wait=_wait(400), usual=1.5, doing=[]) is None   # gap note's job
    assert pace.note("s", now=NOW, data_dir=tmp_path, wait=_wait(90, first=True), usual=50, doing=[]) is None


def test_slow_reply_is_a_plain_observation_and_kept(tmp_path):
    out = pace.note("s", now=NOW, data_dir=tmp_path, wait=_wait(47), usual=1.5,
                    doing=["watching a cooking show (25 min)"])
    assert not out["first"]
    assert "waited 47 min" in out["text"] and "within about 2 min" in out["text"] and "cooking show" in out["text"]
    again = pace.note("s", now=NOW, data_dir=tmp_path, wait=_wait(30, "m2"), usual=1.5, doing=[])
    assert "1 other time this week" in again["text"]
    assert [e["message_id"] for e in pace.history(tmp_path)] == ["m1", "m2"]


def test_her_first_message_has_its_own_bar_and_no_six_hour_cap(tmp_path):
    out = pace.note("s", now=NOW, data_dir=tmp_path, wait=_wait(271, first=True), usual=50, doing=[])
    assert out["first"] and "You wrote to him first" in out["text"] and "4.5 h later" in out["text"]
    late = pace.note("s", now=NOW, data_dir=tmp_path, wait=_wait(30 * 60, "m3", first=True), usual=50, doing=[])
    assert late and "30 h later" in late["text"]

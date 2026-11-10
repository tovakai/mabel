"""Her recent lines: kept with provenance, served coarse-timed, social turns only, shadow by default."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.persona import recent


def _local(*args):
    return datetime(*args).astimezone()


def test_clean_line_keeps_one_short_line():
    assert recent.clean_line("<think>hm</think>\n\n- “Dreamt of a hallway again.”\n\nsecond para") == "Dreamt of a hallway again."
    long = "word " * 100
    assert len(recent.clean_line(long)) <= recent.LINE_LIMIT + 1
    assert recent.clean_line("```code```") == ""


def test_remember_falls_back_and_keeps_source(tmp_path):
    async def broken(*_a):
        raise RuntimeError("model down")
    line = asyncio.run(recent.remember("a dream", "dreamt", "entry", fallback="I dreamt.", data_dir=tmp_path, ask=broken))
    assert line["text"] == "I dreamt." and line["source"] == "dreamt"

    async def voice(*_a):
        return "still thinking about that hallway."
    asyncio.run(recent.remember("a dream", "dreamt", "entry", fallback="x", data_dir=tmp_path, ask=voice))
    assert [l["text"] for l in recent.load(tmp_path)] == ["I dreamt.", "still thinking about that hallway."]


def test_unknown_source_is_refused(tmp_path):
    with pytest.raises(ValueError):
        recent.add("x", "made_up", "text", data_dir=tmp_path)


def test_when_is_coarse():
    now = _local(2026, 11, 10, 16, 0)
    assert recent.when(now - timedelta(hours=1), now) == "a little while ago"
    assert recent.when(_local(2026, 11, 10, 3, 0), now) == "last night"
    assert recent.when(_local(2026, 11, 9, 23, 0), now) == "last night"
    assert recent.when(_local(2026, 11, 10, 9, 0), now) == "this morning"
    assert recent.when(_local(2026, 11, 10, 12, 30), now) == "earlier today"
    assert recent.when(_local(2026, 11, 9, 14, 0), now) == "yesterday"


def test_servable_keeps_last_few_from_two_days(tmp_path):
    now = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    for hours in (72, 30, 20, 10, 5, 1):
        recent.add("k", "experienced", f"{hours}h ago", now=now - timedelta(hours=hours), data_dir=tmp_path)
    lines = recent.servable(now, tmp_path)
    assert [l["text"] for l in lines] == ["20h ago", "10h ago", "5h ago", "1h ago"]
    assert recent.fragment(lines).count("\n- ") == 4


def _compose(monkeypatch, tmp_path, mode, message="hey, how are you?"):
    from src.persona.composer import Composer
    monkeypatch.setattr(recent, "_path", lambda data_dir=None: tmp_path / "recent.json")
    monkeypatch.setattr(recent, "mode", lambda: mode)
    recent.add("a dream", "dreamt", "a hallway full of water.", now=datetime.now(timezone.utc))
    return Composer().compose(message, set(), None, local_now=datetime.now().astimezone(), mode="on")


def test_composer_serves_only_when_on(monkeypatch, tmp_path):
    shadow = _compose(monkeypatch, tmp_path, "shadow")
    if shadow.fallback == "no_valid_core":
        pytest.skip("no identity core in this checkout")
    assert shadow.recent == 1 and not shadow.recent_served
    assert "hallway" not in (shadow.fragment or "")
    on = _compose(monkeypatch, tmp_path, "on")
    if on.function == "social":
        assert on.recent_served and "hallway full of water" in on.fragment

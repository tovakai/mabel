"""Revisiting her own past: what's offered, her choice (or none), the note, and the gate."""
import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.persona import revisit

NOW = datetime(2026, 11, 20, 14, 0, tzinfo=timezone.utc)
PROSE = "I walked a wet corridor and the doors kept counting backwards, which felt rude but honest. " * 2


def _vault(tmp_path):
    def put(rel, text):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    put("journal/dreams/2026-11-05.md", "---\ntype: dream\ndate: '2026-11-05'\n---\n![[2026-11-05.png]]\n\n# Dream\n\n" + PROSE)
    put("journal/self/2026-11-06.md", "---\ndate: '2026-11-06'\nwritten: '2026-11-06T01:00:00+00:00'\n---\n# Reflection\n\n" + PROSE)
    put("journal/letters/Mabel-to-Tova-2026-11-07.md", "# Letter\n\n" + PROSE)
    put("journal/letters/Tova-to-Mabel-2026-11-07.md", "# Tova's letter\n\n" + PROSE)          # never hers to revisit
    put("journal/revisits/2026-11-08.md", "---\ndate: '2026-11-08'\n---\n# Revisiting\n\n" + PROSE)  # never feeds on itself
    put("journal/dreams/2026-11-19.md", "---\ndate: '2026-11-19'\n---\n# Dream\n\n" + PROSE)    # too recent
    put("knowledge/research/Some Studio.md", "---\ncreated: '2026-11-06T23:05:45+00:00'\n---\n# Some Studio\n\n" + PROSE)
    return tmp_path


def _sig(**kw):
    base = dict(chat_active=False, idle="idle", idle_minutes=45, gpu="free", gpu_reason="", strata=True)
    return SimpleNamespace(**{**base, **kw})


def test_pool_is_her_own_older_writing_only(tmp_path):
    items = revisit.pool(_vault(tmp_path), NOW)
    assert sorted(i["ref"] for i in items) == [
        "journal/dreams/2026-11-05.md", "journal/letters/Mabel-to-Tova-2026-11-07.md",
        "journal/self/2026-11-06.md", "knowledge/research/Some Studio.md"]
    dream = next(i for i in items if i["kind"] == "dream")
    assert dream["label"] == "your dream from Thursday 5 November"
    assert "![[" not in dream["snippet"] and dream["snippet"].startswith("# Dream")


def test_recently_revisited_things_are_left_out_for_a_while(tmp_path):
    vault = _vault(tmp_path)
    history = [{"at": (NOW - timedelta(days=3)).isoformat(), "outcome": "written", "ref": "journal/dreams/2026-11-05.md"}]
    assert "journal/dreams/2026-11-05.md" not in {i["ref"] for i in revisit.pool(vault, NOW, history)}
    history[0]["at"] = (NOW - timedelta(days=40)).isoformat()
    back = {i["ref"]: i for i in revisit.pool(vault, NOW, history)}
    assert back["journal/dreams/2026-11-05.md"]["looked_before"] is True


def test_offer_is_a_draw_not_a_ranking(tmp_path):
    items = revisit.pool(_vault(tmp_path), NOW)
    draws = {tuple(i["ref"] for i in revisit.offer(items, NOW, count=2, rng=random.Random(seed))) for seed in range(30)}
    assert len(draws) > 3
    assert len(revisit.offer(items, NOW, count=10, rng=random.Random(1))) == len(items)


def test_parse_choice():
    assert revisit.parse_choice("<think>maybe 4?</think> 2", 5) == 1
    assert revisit.parse_choice("0", 5) is None
    assert revisit.parse_choice("9", 5) is None
    assert revisit.parse_choice("not today", 5) is None


def test_she_can_choose_not_to_look_back(tmp_path):
    vault, data = _vault(tmp_path / "v"), tmp_path / "d"

    async def ask(messages, owner, max_tokens):
        return "0"

    result = asyncio.run(revisit.revisit(data_dir=data, mabel_vault=vault, ask=ask, now=NOW, rng=random.Random(2)))
    assert result["status"] == "not_now"
    assert not (vault / "journal/revisits/2026-11-20.md").exists()
    assert revisit.load_state(data)["history"][-1]["outcome"] == "not_now"
    assert revisit.gate(_sig(), data_dir=data, now=NOW + timedelta(hours=1), research_running=False,
                        mabel_vault=vault) == ["looked back recently"]


def test_she_rereads_and_writes_a_note(tmp_path):
    vault, data = _vault(tmp_path / "v"), tmp_path / "d"
    prompts = []

    async def ask(messages, owner, max_tokens):
        prompts.append(messages[-1]["content"])
        if len(prompts) == 1:
            return "1"
        return "<think>hm</think>Huh. I was so sure of that corridor. I'd argue with her now, but gently; the doors were right."

    result = asyncio.run(revisit.revisit(data_dir=data, mabel_vault=vault, ask=ask, now=NOW, rng=random.Random(2)))
    assert result["status"] == "written"
    note = (vault / result["path"]).read_text(encoding="utf-8")
    assert note.startswith("---\ntype: revisit")
    assert "provenance: reflected" in note and f"source: {result['source']}" in note
    assert "# Revisiting: your" in note and "<think>" not in note
    assert "doors kept counting backwards" in prompts[1]          # she rereads the real text
    assert "![[" not in prompts[1]
    history = revisit.load_state(data)["history"]
    assert history[-1]["ref"] == result["source"] and history[-1]["outcome"] == "written"
    from src.persona import recent
    assert recent.load(data)[-1]["kind"].startswith("looking back at my ")


def test_gate(tmp_path):
    vault, data = _vault(tmp_path / "v"), tmp_path / "d"
    assert revisit.gate(_sig(), data_dir=data, now=NOW, research_running=False, mabel_vault=vault) == []
    reasons = revisit.gate(_sig(chat_active=True, idle="active", gpu="busy", strata=False), data_dir=data, now=NOW,
                           research_running=True, mabel_vault=vault)
    assert len(reasons) == 5
    empty = tmp_path / "empty"
    empty.mkdir()
    assert revisit.gate(_sig(), data_dir=data, now=NOW, research_running=False,
                        mabel_vault=empty) == ["nothing old enough to look back at"]


def test_switched_off(tmp_path, monkeypatch):
    monkeypatch.setattr(revisit, "_setting", lambda key, default: False if key == "revisit_enabled" else default)
    assert "looking back is switched off" in revisit.gate(_sig(), data_dir=tmp_path, now=NOW, research_running=False,
                                                          mabel_vault=_vault(tmp_path / "v"))

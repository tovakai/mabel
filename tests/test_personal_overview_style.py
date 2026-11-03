"""Personal-overview formatting never changes tools, grants, or inventories."""
from types import SimpleNamespace

from src.agent_runtime.personal_overview_style import (
    PERSONAL_OVERVIEW_PRESENTATION, apply_personal_overview_style,
)
from src.chat_processor import ChatProcessor


def test_personal_overview_prefers_prose_in_chat_context():
    text = "Tell me what you know about me."
    assert ChatProcessor._requests_personal_overview(text)
    original = [{"role": "system", "content": "Personal CORE"}]
    context = SimpleNamespace(messages=original)
    assert apply_personal_overview_style(context, text, is_overview=True)
    assert context.messages[0]["role"] == "system"
    assert context.messages[0]["content"] == PERSONAL_OVERVIEW_PRESENTATION
    assert context.messages[1]["content"] == "Personal CORE"


def test_personal_overview_style_reaches_separate_foreground_context():
    text = "What do you remember about me?"
    assert ChatProcessor._requests_personal_overview(text)
    ctx = SimpleNamespace(messages=[], route_messages=[])
    assert apply_personal_overview_style(ctx, text, is_overview=True)
    assert len(ctx.messages) == len(ctx.route_messages) == 1
    assert ctx.messages[0] is not ctx.route_messages[0]
    assert ctx.messages[0] == ctx.route_messages[0]


def test_explicit_inventory_and_other_questions_keep_their_format():
    for text in (
        "List everything you know about me in bullet points.",
        "Tell me what you know about me as a table.",
        "Tell me everything you remember about me as JSON.",
    ):
        ctx = SimpleNamespace(messages=[])
        assert not apply_personal_overview_style(ctx, text, is_overview=True)
        assert ctx.messages == []
    ctx = SimpleNamespace(messages=[])
    assert not apply_personal_overview_style(ctx, "Explain my systemd service", is_overview=False)


def test_personal_overview_cue_is_not_an_authority_grant():
    assert "tool access" in PERSONAL_OVERVIEW_PRESENTATION
    assert "without inventing" in PERSONAL_OVERVIEW_PRESENTATION


def test_personal_overview_does_not_encourage_explaining_its_own_format():
    cue = PERSONAL_OVERVIEW_PRESENTATION.lower()
    assert "do not explain why you are using prose" in cue
    assert "important gaps briefly" in cue
    assert "not tool access" in cue

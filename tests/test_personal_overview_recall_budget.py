"""Personal overview should see a diverse bounded sample; ordinary chat stays small."""
from types import SimpleNamespace

from src.chat_processor import ChatProcessor


class _Memory:
    def __init__(self, entries):
        self.entries = entries

    def load(self, owner=None):
        assert owner in (None, "alice")
        return list(self.entries)

    def increment_uses(self, ids):
        pass


def _processor(entries):
    return ChatProcessor(
        memory_manager=_Memory(entries),
        personal_docs_manager=SimpleNamespace(rag_manager=None),
    )


def _entry(index, category, pinned=False, text=None):
    return {
        "id": str(index),
        "text": text or f"Anthon's {category} memory number {index}.",
        "category": category,
        "pinned": pinned,
        "timestamp": index,
    }


def test_personal_overview_crosses_pinned_boundary_and_covers_categories():
    entries = [_entry(i, "identity", pinned=True) for i in range(12)]
    entries += [_entry(20 + i, category) for i, category in enumerate(
        ["project", "preference", "goal", "fact", "contact", "project", "preference"],
    )]
    processor = _processor(entries)
    processor.build_context_preface(
        "Tell me what you know about me.", SimpleNamespace(),
        owner="alice", use_rag=False, use_memory=True,
    )
    recalled = processor._last_used_memories
    assert len(recalled) > 5
    assert len(recalled) <= processor.PERSONAL_OVERVIEW_MEMORY_LIMIT
    assert {m["category"] for m in recalled} >= {
        "identity", "project", "preference", "goal", "fact",
    }
    assert sum(len(m["text"]) for m in recalled) <= processor.PERSONAL_OVERVIEW_CHAR_LIMIT


def test_long_memory_overview_remains_bounded_and_normal_chat_stays_five():
    entries = [_entry(i, "project", text=f"Memory {i} " + "x" * 1100)
               for i in range(40)]
    processor = _processor(entries)
    processor.build_context_preface(
        "What do you remember about me?", SimpleNamespace(),
        use_rag=False, use_memory=True,
    )
    assert len(processor._last_used_memories) <= 24
    assert sum(len(m["text"]) for m in processor._last_used_memories) <= 6500

    processor.build_context_preface(
        "Tell me about decorators in Python", SimpleNamespace(),
        use_rag=False, use_memory=True,
    )
    assert len(processor._last_used_memories) <= 5


def test_explicit_management_and_disabled_memory_do_not_preload_private_entries():
    processor = _processor([_entry(1, "identity", pinned=True)])
    for question, no_memory in (
        ("List my saved memories", False),
        ("Tell me what you know about me", True),
    ):
        processor.build_context_preface(
            question, SimpleNamespace(),
            use_rag=False, use_memory=not no_memory,
        )
        assert processor._last_used_memories == []

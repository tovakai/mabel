"""Phase 2.0 provenance: one vocabulary, honest recall, reflections and dreams never become facts."""
import pytest

from src import provenance
from src.episodic_memory.candidates import CandidateAdmissionError, CandidateDraft
from src.episodic_memory.chat_recall import recall_message


def test_existing_stores_map_onto_the_vocabulary():
    assert provenance.of_candidate(origin="completed_persisted_turn", source_kind="conversation") == "experienced"
    assert provenance.of_candidate(origin="letter_from_tova", source_kind="tool_observation") == "told"
    assert provenance.of_candidate(origin="daily_life_account", source_kind="tool_observation") == "observed"
    assert provenance.of_whisper("self") == "reflected" and provenance.of_whisper("manual") == "told"
    assert provenance.of_inbox("dreamer") == "inferred"
    assert provenance.of_backlog({"origin": "self_generated", "from_article": "journal/self/2026-11-08.md"}) == "reflected"
    assert provenance.of_backlog({"origin": "self_generated", "from_article": "journal/letters/notes/x.md"}) == "told"
    assert provenance.of_backlog({"origin": "self_generated", "from_article": "knowledge/research/A.md"}) == "experienced"


@pytest.mark.parametrize("origin", ["self_reflection", "dream"])
def test_reflections_and_dreams_cannot_become_memories(origin):
    draft = CandidateDraft(kind="experience", actor="mabel", source_kind="tool_observation",
                           source_ref="note:x", source_event_id="e1", summary="s", origin=origin)
    with pytest.raises(CandidateAdmissionError):
        draft.validated()


def test_recall_says_where_a_memory_came_from():
    text = recall_message([
        {"occurred_at": "2026-11-07", "user": "hi", "assistant": "hey", "provenance": "experienced", "moment": ""},
        {"occurred_at": "2026-11-07", "user": "(from your letters…)", "assistant": "", "provenance": "told",
         "moment": "Tova told me she thinks maintenance can become a cult."},
        {"occurred_at": "2026-11-06", "user": "(from his day…)", "assistant": "", "provenance": "observed",
         "moment": "He went to a gig downtown."},
    ])["content"]
    assert "in a conversation: The user said: \"hi\"" in text
    assert "from your letters with Tova (her word, not a fact): \"Tova told me" in text
    assert "something you noticed in his day: \"He went to a gig" in text
    assert "The user said: \"(from your letters" not in text

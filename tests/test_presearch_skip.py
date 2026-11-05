"""Agent turns with web search on skip the pre-search for plain remarks, not for requests."""
import pytest

from routes.chat_helpers import _conversational_statement


@pytest.mark.parametrize("message", [
    "Bit of everything really. But ESP32s are def cluttering my workshop.",
    "Haha yeah, the concert last night was insane",
    "ugh, work was so long today",
    "hey how are you",
    "How was your day? Mine was long",
    "what are you up to tonight",
])
def test_remarks_skip_the_presearch(message):
    assert _conversational_statement(message)


@pytest.mark.parametrize("message", [
    "tell me about the ESP32-S3",
    "I wonder who won the world cup",
    "explain how ESP-NOW works",
    "who won the 2026 world cup?",
    "whats the weather like in Gothenburg tomorrow",
    "look up the price of an ESP32",
    "recommend me an anime based on my ratings",
    "hey how are you, also who won the world cup?",
    "",
])
def test_requests_keep_the_presearch(message):
    assert not _conversational_statement(message)

"""Focused compatibility tests for Obsidian Companion form flags."""
import pytest

from src.companion_compat import form_flag_enabled, include_agent_skills_index


@pytest.mark.parametrize("value", [True, "true", "TRUE", " true ", "1", "yes", "on"])
def test_form_flag_enabled(value):
    assert form_flag_enabled(value) is True


@pytest.mark.parametrize("value", [False, None, "", "false", "FALSE", "0", "off", "no", "garbage"])
def test_form_flag_disabled(value):
    assert form_flag_enabled(value) is False


@pytest.mark.parametrize(
    ("mode", "no_skills", "expected"),
    [
        ("agent", None, True),
        ("agent", "false", True),
        ("agent", "true", False),
        ("agent", True, False),
        ("agent", " TRUE ", False),
        ("chat", None, False),
        ("chat", "false", False),
        ("chat", "true", False),
    ],
)
def test_skills_index_respects_companion_flag(mode, no_skills, expected):
    assert include_agent_skills_index(mode, no_skills) is expected

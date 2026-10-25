"""Keep the visible M.A.B.E.L. branding and shipped personas in sync."""
import json
from pathlib import Path

from src.reminder_personas import PERSONAS, synthesis_system_prompt


ROOT = Path(__file__).resolve().parents[1]


def test_visible_branding_and_acronym():
    index = (ROOT / "static/index.html").read_text(encoding="utf-8")
    login = (ROOT / "static/login.html").read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "static/manifest.json").read_text(encoding="utf-8"))
    assert 'id="welcome-sub">Mildly Autistic But Extremely Loyal</div>' in index
    assert "M.A.B.E.L." in index and "A voyage begins." not in index
    assert "<title>M.A.B.E.L. Chat</title>" in index
    assert "<title>M.A.B.E.L. — Login</title>" in login
    assert manifest["name"] == manifest["short_name"] == "M.A.B.E.L."
    assert "Odysseus" not in index and "Odysseus" not in login


def test_builtin_personas_are_available_to_chat_and_reminders():
    presets = (ROOT / "static/js/presets.js").read_text(encoding="utf-8")
    tasks = (ROOT / "static/js/tasks.js").read_text(encoding="utf-8")
    for persona_id, name in (("mabel", "M.A.B.E.L."), ("tova", "Tova"), ("dottie", "Dottie")):
        assert f"id: '{persona_id}'" in presets
        assert f"name: '{name}'" in presets
        assert f"['{persona_id}', '{name}']" in tasks
        assert persona_id in PERSONAS
        assert "under 10 words" in synthesis_system_prompt(persona_id)
    # Existing tasks with the legacy persona ID remain functional.
    assert synthesis_system_prompt("odysseus") == synthesis_system_prompt("mabel")

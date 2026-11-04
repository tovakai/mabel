"""vault_read_range/traverse_graph locate notes by note_id; a stray path must not fail the call."""
import json
from unittest.mock import patch

from src.agent_runtime.authority import ExactOperation
from src.agent_runtime.resource_binding import resolve_filesystem_operation
from src.agent_runtime.resources import FilesystemRoot
from src.knowledge_engine.tools import schemas


def _bind(vault, tool, args):
    roots = (FilesystemRoot.seal(str(vault), owner="alice"),)
    with patch("src.agent_runtime.vault_target.require_configured_vault", return_value=str(vault)):
        return resolve_filesystem_operation(ExactOperation.normalize(tool, json.dumps(args)),
                                            roots=roots, workspace=str(vault), request_id="")


def test_note_path_in_directory_field_binds_to_the_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Library" / "Games").mkdir(parents=True)
    (vault / "Library" / "Games" / "Lessons In Love.md").write_text("x")
    for tool in ("vault_read_range", "vault_traverse_graph"):
        stray = _bind(vault, tool, {"note_id": "abc", "path": "Library/Games/Lessons In Love.md"})
        plain = _bind(vault, tool, {"note_id": "abc"})
        assert json.loads(stray.execution_input) == json.loads(plain.execution_input)


def test_search_keeps_its_directory_scope(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    scoped = _bind(vault, "vault_knowledge_search", {"query": "x", "path": "Library"})
    assert json.loads(scoped.execution_input)["path"].endswith("Library")


def test_note_id_tools_no_longer_advertise_a_directory_field():
    by_name = {s["function"]["name"]: s["function"]["parameters"]["properties"] for s in schemas()}
    assert "path" not in by_name["vault_read_range"]
    assert "path" not in by_name["vault_traverse_graph"]
    assert "path" in by_name["vault_knowledge_search"]

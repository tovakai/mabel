"""Vault note rewrite should stay on file tools, not documents/bash/web."""
from __future__ import annotations

import json

from src.agent_loop import (
    _API_AGENT_RULES,
    _classify_agent_request,
    _looks_like_vault_note_rewrite,
)
from src.tool_policy import ToolPolicy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import (
    VAULT_NOTE_REWRITE_TOOLS,
    looks_like_vault_note_rewrite,
    requested_capabilities,
    resolve_turn_contract,
    selected_tools_for_request,
)


REWRITE = (
    "Good catch, the visual novel genre page is an experiment made with a... "
    "Lesser LLM. So if you want you can pretty much rewrite that whole note for me."
)
CLARIFY = "Yeah, we only want to edit the visual novels genre note."


def test_vault_note_rewrite_detector():
    assert _looks_like_vault_note_rewrite(REWRITE)
    assert looks_like_vault_note_rewrite(CLARIFY)
    assert _looks_like_vault_note_rewrite(
        "Please rewrite the genre page in my vault"
    )
    assert not _looks_like_vault_note_rewrite("What is Fruit of Grisaia about?")
    assert not _looks_like_vault_note_rewrite("Rewrite this Python module")


def test_vault_note_rewrite_routes_to_files_not_documents():
    intent = _classify_agent_request(
        [{"role": "user", "content": REWRITE}],
        REWRITE,
    )
    domains = set(intent.get("domains") or ())
    assert "files" in domains
    assert "documents" not in domains
    assert "notes_calendar_tasks" not in domains


def test_vault_note_rewrite_contract_offers_writers_not_bulk_or_shell():
    for message in (REWRITE, CLARIFY):
        assert selected_tools_for_request(message) == VAULT_NOTE_REWRITE_TOOLS
        assert requested_capabilities(message, workspace=True) == frozenset()
        contract = resolve_turn_contract(
            capabilities=requested_capabilities(message, workspace=True),
            schemas=list(FUNCTION_TOOL_SCHEMAS),
            policy=ToolPolicy(),
            selected_tools=selected_tools_for_request(message),
            # Prior vault inventory must not reopen bulk/shell on a rewrite seal.
            warm_tools={
                "vault_batch_edit",
                "vault_knowledge_search",
                "bash",
                "web_search",
            },
            message=message,
        )
        offered = {
            json.loads(blob)["function"]["name"] for blob in contract.schema_json
        }
        assert "write_file" in offered
        assert "edit_file" in offered
        assert "read_file" in offered
        assert "vault_batch_edit" not in offered
        assert "bash" not in offered
        assert "research_parallel" not in offered


def test_vault_note_rewrite_writers_survive_workspace_read_family():
    """Default-vault workspace_read must not intersect away write_file."""
    message = REWRITE
    contract = resolve_turn_contract(
        capabilities=frozenset({"workspace_read"}),
        schemas=list(FUNCTION_TOOL_SCHEMAS),
        policy=ToolPolicy(),
        selected_tools=selected_tools_for_request(message),
        required_capabilities=frozenset({"workspace_read"}),
        message=message,
    )
    offered = {
        json.loads(blob)["function"]["name"] for blob in contract.schema_json
    }
    assert "write_file" in offered
    assert "edit_file" in offered
    assert "vault_batch_edit" not in offered


def test_rewrite_prompt_steers_off_bash_and_bulk():
    assert "Rewriting a vault/Obsidian note" in _API_AGENT_RULES
    assert "never `vault_batch_edit`" in _API_AGENT_RULES


def test_agent_loop_retries_degenerate_streams_unconditionally():
    """Regression: WebUI agent must not gate degenerate retry on terminal_agent."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("src/agent_loop.py").read_text(
        encoding="utf-8",
    )
    assert "retry_degenerate_stream_once=True," in src
    assert "retry_degenerate_stream_once=_terminal_completion_contract," not in src

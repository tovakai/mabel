"""Model tool discovery is schema selection, never an execution grant."""
import json

import pytest

from src.agent_runtime.conversational_catalog import conversational_catalog_contract
from src.agent_runtime.tool_catalog import active_catalog, bind_catalog, discover_tools
from src.tool_policy import ToolPolicy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS


def _names(contract):
    return {schema["function"]["name"] for schema in contract.schemas()}


@pytest.mark.asyncio
async def test_explicit_discovery_changes_only_request_local_schema_selection():
    policy = ToolPolicy(disabled_tools=frozenset({"bash", "write_file", "read_file"}))
    contract = conversational_catalog_contract(FUNCTION_TOOL_SCHEMAS, policy)
    before = contract
    assert contract.required == frozenset()
    assert "discover_tools" in _names(contract)
    assert "manage_notes" not in _names(contract)
    assert "manage_notes" in contract.executable

    with bind_catalog(contract, True):
        result = await discover_tools('{"names":["manage_notes"]}', {})
        assert result["exit_code"] == 0, result
        assert "manage_notes" in _names(contract)
        assert "bash" not in _names(contract)
        assert "write_file" not in _names(contract)
        denied = await discover_tools('{"names":["write_file"]}', {})
        assert denied["exit_code"] != 0
        assert "write_file" not in active_catalog(contract).inventory

    assert contract is before
    assert "manage_notes" not in _names(contract)
    assert "manage_notes" not in contract.offered


@pytest.mark.asyncio
async def test_catalog_query_falls_back_to_local_metadata_without_semantic_service(monkeypatch):
    from src import tool_index
    monkeypatch.setattr(tool_index, "get_ready_tool_index", lambda: None)
    monkeypatch.setattr(tool_index, "get_tool_index_status", lambda: {"state": "warming"})
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: pytest.fail("no index startup on warming"))
    policy = ToolPolicy(disabled_tools=frozenset({"bash", "write_file"}))
    contract = conversational_catalog_contract(FUNCTION_TOOL_SCHEMAS, policy)
    with bind_catalog(contract, True):
        result = await discover_tools('{"query":"manage notes"}', {})
        assert result["exit_code"] == 0, result
        payload = json.loads(result["output"])
        assert payload["retrieval"] == "catalog"
        assert "manage_notes" in payload["selected"]
        assert "manage_notes" in _names(contract)
        assert "write_file" not in _names(contract)


def test_all_tools_blocked_has_no_bootstrap_or_discovery_inventory():
    contract = conversational_catalog_contract(
        FUNCTION_TOOL_SCHEMAS, ToolPolicy(block_all_tool_calls=True),
    )
    assert contract.offered == frozenset()
    assert contract.executable == frozenset()
    assert contract.catalog_json == ()


def test_mcp_disabled_stays_out_of_catalog_and_scope_does_not_mint_a_grant():
    mock_schema = {
        "type": "function",
        "function": {"name": "mcp__secret__read", "description": "Read secret",
                     "parameters": {"type": "object", "properties": {}}},
    }
    contract = conversational_catalog_contract(
        [mock_schema, *FUNCTION_TOOL_SCHEMAS],
        ToolPolicy(disable_mcp=True),
        workspace_read=True,
    )
    assert not any("mcp__secret__read" in s for s in contract.catalog_json)
    assert "workspace_read" in contract.capabilities
    assert contract.required == frozenset()
    assert "read_file" not in contract.offered

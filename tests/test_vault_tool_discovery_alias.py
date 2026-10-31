"""Discovery resolves known legacy names to visible schemas, not permissions."""
import json

import pytest

from src.agent_runtime.conversational_catalog import conversational_catalog_contract
from src.agent_runtime.tool_catalog import bind_catalog, discover_tools
from src.tool_policy import ToolPolicy


def _schema(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Search indexed Obsidian vault notes",
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.mark.asyncio
async def test_legacy_vault_discovery_name_returns_canonical_schema_without_execution():
    contract = conversational_catalog_contract(
        [_schema("discover_tools"), _schema("vault_knowledge_search")],
        ToolPolicy(),
    )
    with bind_catalog(contract, True):
        response = await discover_tools(
            json.dumps({"names": ["vault_search"]}), {},
        )
        assert response["exit_code"] == 0, response
        payload = json.loads(response["output"])
        assert payload["selected"] == ["vault_knowledge_search"]
        assert payload["resolved_aliases"] == {"vault_search": "vault_knowledge_search"}
        assert payload["authorization"].startswith("Selection exposes schemas only")
        assert "vault_search" not in contract.executable


@pytest.mark.asyncio
async def test_alias_cannot_discover_disabled_or_nonexistent_vault_tool():
    for policy in (ToolPolicy(disabled_tools=frozenset({"vault_knowledge_search"})), ToolPolicy()):
        schemas = [_schema("discover_tools")]
        if not policy.disabled_tools:
            # No matching registered tool is as restrictive as a denied one.
            pass
        else:
            schemas.append(_schema("vault_knowledge_search"))
        contract = conversational_catalog_contract(schemas, policy)
        with bind_catalog(contract, True):
            result = await discover_tools('{"names":["vault_search"]}', {})
            assert result["exit_code"] != 0
            assert result["failure_kind"] == "unknown_tool"

"""Policy-filtered, request-local discovery seed for conversational Agent turns.

The seed exposes only small control tools initially. Discovering a tool
adds its schema to the current turn, never grants authority to execute it.
Owner approval and path containment are enforced independently downstream.
"""
import json

from src.turn_contract import TurnContract, canonical_tool


_CONVERSATION_BOOTSTRAP = frozenset({"discover_tools", "ask_user", "update_plan"})


def conversational_catalog_contract(schemas, policy, *, workspace_read=False):
    """Build an immutable bootstrap contract without lexical tool-family routing.

    Schema inventory reflects only the caller's policy-filtered candidates.
    A tool becomes visible after discover_tools selects it, but cannot become
    executable unless RequestAuthority and the execution gate also admit it.
    """
    denied = {canonical_tool(n) for n in policy.all_disabled_names()}
    inventory = {}
    if not policy.block_all_tool_calls:
        for schema in schemas:
            function = schema.get("function") if isinstance(schema, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(name, str) or not name:
                continue
            if (
                policy.blocks(name) or canonical_tool(name) in denied
                or (policy.disable_mcp and name.startswith("mcp__"))
            ):
                continue
            inventory[name] = schema

    executable = frozenset(inventory)
    offered = executable & _CONVERSATION_BOOTSTRAP
    # This does not imply user intent for filesystem access. The route only
    # sets workspace_read after validating an existing server-bound root.
    capabilities = frozenset({"workspace_read"}) if workspace_read else frozenset()
    return TurnContract(
        capabilities=capabilities,
        required=frozenset(),
        offered=offered,
        executable=executable,
        unavailable=frozenset(),
        schema_json=tuple(json.dumps(inventory[n], sort_keys=True) for n in sorted(offered)),
        selection_mode="conversational_discovery",
        catalog_json=tuple(json.dumps(inventory[n], sort_keys=True) for n in sorted(executable)),
    )

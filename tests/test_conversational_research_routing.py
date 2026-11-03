"""Focused regression: conversational grounding vs research_parallel visibility.

Covers the empty-cap / compact-web failure class without hardcoding a media
title: Gyro-S must see research_parallel when it is executable; casual
reactions must not require research; factual / vault / parallel intents must
select the right model-facing schemas; discovery recovers when the
conversational bootstrap omits a registered tool.
"""
from __future__ import annotations

import json

import pytest

from src.agent_loop import (
    TOOL_SECTIONS,
    _API_AGENT_RULES,
    _DOMAIN_RULES,
    _apply_notes_only_tool_clamp,
    _compact_native_route_tools,
    _looks_like_explicit_notes_only_turn,
    _notes_only_clamp_allowed,
)
from src.agent_runtime.conversational_catalog import conversational_catalog_contract
from src.agent_runtime.tool_catalog import active_catalog, bind_catalog, discover_tools
from src.tool_policy import WEB_TOOL_NAMES, ToolPolicy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.agent_runtime import parallel_research as pr
from src.turn_contract import (
    CONTRACT_CORE_TOOLS,
    conversational_retrieval_intent,
    media_progress_boundary,
    requested_capabilities,
    resolve_turn_contract,
    selected_tools_for_request,
)


def _contract(
    message: str,
    *,
    workspace: bool = True,
    policy: ToolPolicy | None = None,
    drop_tools: set[str] | None = None,
):
    schemas = list(FUNCTION_TOOL_SCHEMAS)
    if drop_tools:
        schemas = [
            s for s in schemas
            if s.get("function", {}).get("name") not in drop_tools
        ]
    return resolve_turn_contract(
        capabilities=requested_capabilities(message, [], workspace=workspace),
        schemas=schemas,
        policy=policy or ToolPolicy(),
        selected_tools=selected_tools_for_request(message),
        message=message,
    )


def _offered_names(contract) -> set[str]:
    names = set()
    for blob in contract.schema_json:
        payload = json.loads(blob)
        name = payload.get("function", {}).get("name")
        if name:
            names.add(name)
    return names


# Generic titles — never pin routing to one media franchise.
_CASUAL = "So, I just finished chapter 1 in Aurora Protocol, and just wow."
_FACTUAL = "What happens at the end of chapter 1 of Aurora Protocol?"
_PARALLEL = "Compare my notes about Aurora Protocol with what people say online."
_VAULT = "What do my notes say about Aurora Protocol?"
_PUBLIC = "What is Aurora Protocol?"


def test_a_casual_reaction_offers_tools_but_intent_is_casual():
    assert conversational_retrieval_intent(_CASUAL, workspace=True) == "casual"
    contract = _contract(_CASUAL)
    assert "research_parallel" in CONTRACT_CORE_TOOLS
    assert "research_parallel" in contract.offered
    assert "research_parallel" in _offered_names(contract)
    assert "web_search" in contract.offered
    assert "Named works" in _API_AGENT_RULES
    assert "inventing plot" in _API_AGENT_RULES
    assert "emotional reactions" in TOOL_SECTIONS["research_parallel"]


def test_b_factual_chapter_question_is_factual_public_with_web_surface():
    assert conversational_retrieval_intent(_FACTUAL, workspace=True) == "factual_public"
    offered = _offered_names(_contract(_FACTUAL))
    assert "web_search" in offered
    assert "web_fetch" in offered
    # Lean public lookup: no vault/parallel fan-out for a simple chapter/plot ask.
    assert "research_parallel" not in offered
    assert "vault_knowledge_search" not in offered
    assert "Do not invent plot" in TOOL_SECTIONS["web_search"]


def test_c_compare_notes_online_selects_parallel_and_exposes_research_parallel():
    assert conversational_retrieval_intent(_PARALLEL, workspace=True) == "parallel"
    caps = requested_capabilities(_PARALLEL, [], workspace=True)
    assert "workspace_read" in caps
    assert "search_browser" in caps
    offered = _offered_names(_contract(_PARALLEL))
    assert "research_parallel" in offered
    assert "web_search" in offered
    assert "vault_knowledge_search" in offered or "dispatch_dottie" in offered
    desc = TOOL_SECTIONS["research_parallel"]
    assert "mode=both" in desc and "vault_strategy=fast" in desc


def test_d_vault_notes_question_is_vault_only_not_web_family():
    assert conversational_retrieval_intent(_VAULT, workspace=True) == "vault"
    caps = requested_capabilities(_VAULT, [], workspace=True)
    assert caps == frozenset({"workspace_read"})
    offered = _offered_names(_contract(_VAULT))
    assert "vault_knowledge_search" in offered or "dispatch_dottie" in offered
    assert "research_parallel" in offered
    assert "web_search" not in offered


def test_e_public_what_is_offers_web_lookup():
    assert conversational_retrieval_intent(_PUBLIC, workspace=True) == "factual_public"
    assert conversational_retrieval_intent(
        "What is Fruit of Grisaia about?", workspace=True,
    ) == "factual_public"
    offered = _offered_names(_contract(_PUBLIC))
    assert "web_search" in offered
    assert "web_fetch" in offered
    assert "research_parallel" not in offered
    assert "vault_knowledge_search" not in offered
    assert selected_tools_for_request(_PUBLIC) == frozenset({
        "web_search", "web_fetch", "ask_user", "update_plan",
    })
    assert "single `web_search`" in _API_AGENT_RULES


def test_f_disabled_web_or_missing_compound_blocks_unauthorized_surface():
    no_web = _contract(
        _PARALLEL,
        policy=ToolPolicy(disabled_tools=frozenset(WEB_TOOL_NAMES)),
    )
    assert "web_search" not in _offered_names(no_web)

    no_compound = _contract(
        _PARALLEL,
        drop_tools={
            "vault_knowledge_search",
            "dispatch_dottie",
            "vault_traverse_graph",
            "vault_read_range",
            "vault_retrieve_batch",
            "research_parallel",
        },
    )
    assert "research_parallel" not in _offered_names(no_compound)


@pytest.mark.asyncio
async def test_g_registered_but_unselected_recovers_via_bounded_discover():
    seed = conversational_catalog_contract(
        FUNCTION_TOOL_SCHEMAS, ToolPolicy(), workspace_read=True,
    )
    assert "research_parallel" in seed.executable
    assert "research_parallel" not in seed.offered
    with bind_catalog(seed, True):
        result = await discover_tools(
            json.dumps({"names": ["research_parallel"]}),
            {},
        )
        assert result.get("exit_code") == 0
        catalog = active_catalog()
        assert catalog is not None
        assert "research_parallel" in catalog.selected
        schema_names = {s["function"]["name"] for s in catalog.schemas()}
        assert "research_parallel" in schema_names


def test_compact_web_route_preserves_research_parallel_when_already_admitted():
    original = {"web_search", "web_fetch", "research_parallel", "ask_user", "bash"}
    compact = _compact_native_route_tools(
        original,
        "Compare my notes with what people say online about a series.",
        {"web"},
    )
    assert compact is not None
    assert "research_parallel" in compact
    assert "web_search" in compact
    assert "prefer `research_parallel`" in _DOMAIN_RULES["web"]


def test_casual_named_media_does_not_force_parallel_intent():
    assert conversational_retrieval_intent(_CASUAL, workspace=True) == "casual"
    assert conversational_retrieval_intent(_CASUAL, workspace=True) != "parallel"
    # Core may offer the tool; intent must not classify every named mention as research.
    assert "do not force `research_parallel`" in _API_AGENT_RULES


def test_spoiler_boundary_from_expressed_progress():
    boundary = media_progress_boundary(_CASUAL)
    assert boundary == {"unit": "chapter", "reached": 1}
    assert media_progress_boundary(_FACTUAL) is None or media_progress_boundary(_FACTUAL)["reached"] == 1
    assert "later-unit spoilers" in _API_AGENT_RULES or "later-chapter spoilers" in TOOL_SECTIONS["research_parallel"]
    assert "finished/reached a numbered chapter" in _API_AGENT_RULES


def test_empty_web_notice_is_not_substantive_source():
    notice = (
        "No search results found. Tried: duckduckgo:empty. "
        "All providers returned empty — possibly a niche query."
    )
    assert pr._looks_like_empty_search_message(notice)
    filtered = pr._substantive_web_results([{
        "title": "", "url": "", "snippet": notice, "engine": "",
    }])
    assert filtered == []
    # A real hit still counts.
    assert pr._substantive_web_results([{
        "title": "Wiki", "url": "https://example.test/x", "snippet": "ok", "engine": "ddg",
    }])


def test_compare_and_vault_notes_are_not_personal_notes_only_clamp():
    """Regression: 'my notes' + public/compare must not collapse to manage_notes."""
    assert not _looks_like_explicit_notes_only_turn(_PARALLEL)
    assert not _notes_only_clamp_allowed(_PARALLEL, workspace=True)
    assert not _notes_only_clamp_allowed(_PARALLEL, workspace=False)
    assert not _notes_only_clamp_allowed(_VAULT, workspace=True)
    # Pure personal-notes create still clamps.
    assert _looks_like_explicit_notes_only_turn("Add a note to buy milk tomorrow")
    assert _notes_only_clamp_allowed("Add a note to buy milk tomorrow", workspace=False)


def test_notes_clamp_preserves_sealed_retrieval_tools_on_resume():
    sealed = {
        "ask_user", "manage_notes", "research_parallel",
        "vault_knowledge_search", "web_fetch", "web_search",
    }
    assert _apply_notes_only_tool_clamp(sealed) == sealed
    personal = {"manage_notes", "manage_calendar", "web_search"}  # web present → keep
    assert _apply_notes_only_tool_clamp(personal) == personal
    notes_only = {"manage_notes", "discover_tools", "ui_control"}
    clamped = _apply_notes_only_tool_clamp(notes_only)
    assert clamped == {
        "manage_notes", "manage_calendar", "manage_tasks",
        "ask_user", "update_plan",
    }

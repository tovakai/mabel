"""Regression: native web_fetch arguments, link hubs and browser gating."""
import json

import pytest

from src.agent_tools.web_tools import WebFetchTool
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, normalize_native_function_args


def _schema(name):
    return next(s["function"] for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == name)


def test_fetch_contract_uses_extraction_goal_not_ambiguous_query():
    properties=_schema("web_fetch")["parameters"]["properties"]
    assert "extraction_goal" in properties
    assert "query" not in properties
    assert "urls" in properties


def test_singular_url_array_is_normalized_in_native_tool_transport():
    name,args=normalize_native_function_args("web_fetch", {
        "url":["https://docs.example.org/a","https://docs.example.org/b"],
        "extraction_goal":"install steps",
    })
    assert name=="web_fetch"
    assert args.get("urls")==["https://docs.example.org/a","https://docs.example.org/b"]
    assert "url" not in args


@pytest.mark.parametrize("raw,expected",[
    ("https://wiki.example.org/Ch1 https://wiki.example.org/Ch2",
     ["https://wiki.example.org/Ch1","https://wiki.example.org/Ch2"]),
    ("https://wiki.example.org/Ch1,https://wiki.example.org/Ch2",
     ["https://wiki.example.org/Ch1","https://wiki.example.org/Ch2"]),
    ("wiki.example.org/Ch1, wiki.example.org/Ch2",
     ["wiki.example.org/Ch1","wiki.example.org/Ch2"]),
])
def test_unambiguous_two_url_strings_are_split(raw,expected):
    assert WebFetchTool._coerce_url_list(raw)==expected


@pytest.mark.parametrize("value",[
    "https://wiki.example.org/index.php?title=Walkthrough",
    "https://wiki.example.org/wiki/Chapter,Part_1",
    "https://wiki.example.org/wiki/A%20B",
    "https://wiki.example.org/Chapter?section=One,Two",
    "https://wiki.example.org/Chapter query=two",
    "https://wiki.example.org/\tevil.example.org",
])
def test_single_urls_and_bad_ambiguous_input_are_not_split(value):
    assert WebFetchTool._coerce_url_list(value) is None


@pytest.mark.asyncio
async def test_url_list_and_delimited_string_run_through_normal_batch(monkeypatch):
    calls=[]
    def mock_fetch(url,timeout=10,max_bytes=None):
        calls.append(url)
        return {
            "title":"Example",
            "content":"A normal page.",
            "linked_content":"A normal page.",
            "page_entries":[],
        }
    from src.search import content as search_content
    monkeypatch.setattr(search_content,"fetch_webpage_content",mock_fetch)
    tool=WebFetchTool()
    first=await tool.execute(json.dumps({
        "url":["https://wiki.example.org/a","https://wiki.example.org/b"],
        "extraction_goal":"chapter",
    }),{})
    assert first["exit_code"]==0
    assert first["requested"]==2
    assert first["successful"]==2
    assert "## URL 1:" in first["output"]
    second=await tool.execute(json.dumps({
        "url":"https://wiki.example.org/c, https://wiki.example.org/d",
        "extraction_goal":"chapter",
    }),{})
    assert second["exit_code"]==0
    assert second["requested"]==2
    assert len(calls)==4


@pytest.mark.asyncio
async def test_short_focused_wiki_extraction_preserves_navigation(monkeypatch):
    index="\n".join(
        f"- [Chapter {i}](<https://lessonsinlove.wiki/index.php?title=Chapter_{i}>)"
        for i in range(1,31)
    )
    text="Walkthrough index\n" + index + "\nNo spoilers below."
    def mock_fetch(url,timeout=10,max_bytes=None):
        return {"title":"Walkthrough","content":text,
                "linked_content":text,"page_entries":[]}
    from src.search import content as search_content
    monkeypatch.setattr(search_content,"fetch_webpage_content",mock_fetch)
    result=await WebFetchTool().execute(json.dumps({
        "url":"https://lessonsinlove.wiki/index.php?title=Walkthrough",
        "extraction_goal":"chapter 1 chapter 2 events endings flowchart",
    }),{})
    assert result["exit_code"]==0
    assert "[Navigation/index page:" in result["output"]
    assert "Chapter 20" in result["output"]


@pytest.mark.asyncio
async def test_regular_prose_keeps_focused_extraction(monkeypatch):
    from src.search import content as search_content
    def mock_fetch(url,timeout=10,max_bytes=None):
        body="Title\n" + " " .join(["background"]*800) + "\nGoal: Verify target result."
        return {"title":"Technical paper","content":body,
                "linked_content":body,"page_entries":[]}
    monkeypatch.setattr(search_content,"fetch_webpage_content",mock_fetch)
    result=await WebFetchTool().execute(json.dumps({
        "url":"https://example.org/paper","extraction_goal":"Verify target result",
    }),{})
    assert result["exit_code"]==0
    assert "[Navigation/index page:" not in result["output"]
    assert "Goal: Verify target result" in result["output"]


@pytest.mark.asyncio
async def test_invalid_multi_url_entry_still_fails_per_url_validation(monkeypatch):
    tool=WebFetchTool()
    result=await tool.execute(json.dumps({"url":["https://example.org","file:///etc/passwd"]}),{})
    assert result["requested"]==2
    assert result["successful"]<=1
    assert any(r["exit_code"]==1 for r in result["batch_results"])


def test_disabled_page_commands_are_not_advertised_as_available():
    from src.browser_identity import PAGE_COMMANDS_ENABLED, PAGE_ACTIONS
    assert PAGE_COMMANDS_ENABLED is False
    assert "open" in PAGE_ACTIONS  # recognized syntax != implemented capability

"""media_lookup: structured AniList / VNDB / fan-wiki lookups under the web toggle."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.agent_runtime.authority import ExactOperation
from src.agent_tools import TOOL_HANDLERS
from src.agent_tools import media_lookup_tool as media
from src.tool_capabilities import ToolRunSecurityContext
from src.tool_execution import execute_tool_block
from src.tool_policy import WEB_EGRESS_MARKER, WEB_EGRESS_TOOL_NAMES, ToolPolicy, is_web_egress_operation
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.tool_types import ToolBlock
from src.turn_contract import FAMILY_TOOLS, requested_capabilities
from tests.test_research_parallel_turn_authority import _authority


def _run(args):
    return asyncio.run(media.MediaLookupTool().execute(json.dumps(args), {}))


def test_registered_as_a_web_tool():
    assert "media_lookup" in TOOL_HANDLERS
    assert "media_lookup" in WEB_EGRESS_TOOL_NAMES
    assert is_web_egress_operation("media_lookup", '{"source": "anilist"}')
    assert "media_lookup" in FAMILY_TOOLS["search_browser"]
    assert any(s["function"]["name"] == "media_lookup" for s in FUNCTION_TOOL_SCHEMAS)


def test_recommendations_offer_vault_and_web_families():
    assert requested_capabilities("Recommend me an anime based on my preferences.", vault_scope=True) == \
        frozenset({"workspace_read", "search_browser"})
    assert requested_capabilities("What did I rate Berserk?", vault_scope=True) == frozenset({"workspace_read"})


@pytest.mark.asyncio
async def test_explicit_web_off_refuses_media_lookup(tmp_path):
    authority = _authority(tmp_path, capabilities={"workspace_read", "search_browser"},
                           disabled=set(WEB_EGRESS_TOOL_NAMES) | {WEB_EGRESS_MARKER})
    _, result = await execute_tool_block(
        ToolBlock("media_lookup", json.dumps({"source": "anilist", "query": "Pluto"})),
        owner="alice", session_id="sess-parallel-auth", workspace=str(tmp_path),
        request_authority=authority, security_context=ToolRunSecurityContext(), tool_policy=ToolPolicy())
    assert result["failure_reason"] == "web_disabled" and result["executed"] is False


def test_read_only_lookup_needs_no_approval_when_web_is_allowed(tmp_path):
    authority = _authority(tmp_path, capabilities={"workspace_read", "search_browser"})
    assert authority.permits(ExactOperation.normalize("media_lookup", json.dumps({"source": "vndb", "query": "x"})))


@pytest.mark.parametrize("host", ["localhost", "evil.internal", "http://10.0.0.5/", "not a host", "a..b"])
def test_private_or_invalid_wiki_hosts_are_refused(host, monkeypatch):
    monkeypatch.setattr("src.outbound_fetch._resolve_hostname_ips",
                        lambda h: [__import__("ipaddress").ip_address("10.0.0.5")])
    result = _run({"source": "wiki", "query": "x", "wiki": host})
    assert result["exit_code"] == 1


def test_wiki_host_resolving_to_a_private_address_is_refused(monkeypatch):
    import ipaddress
    monkeypatch.setattr("src.outbound_fetch._resolve_hostname_ips", lambda h: [ipaddress.ip_address("127.0.0.1")])
    result = _run({"source": "wiki", "query": "Guts", "wiki": "rebind.example.com"})
    assert result["exit_code"] == 1


def test_bad_arguments_are_explained():
    assert "source must be" in _run({"source": "imdb", "query": "x"})["error"]
    assert "action must be" in _run({"source": "anilist", "action": "delete", "query": "x"})["error"]
    assert "VNDB id" in _run({"source": "vndb", "action": "details", "id": "abc"})["error"]


def _mock_transport(handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)
    return factory


def test_anilist_search_is_compact_and_structured(monkeypatch):
    def handler(request):
        assert request.url.host == "graphql.anilist.co"
        return httpx.Response(200, json={"data": {"Page": {"media": [{
            "id": 30745, "idMal": 745, "title": {"romaji": "PLUTO", "english": "Pluto"}, "format": "MANGA",
            "averageScore": 85, "genres": ["Mystery"], "tags": [{"name": "Robots", "rank": 90, "isMediaSpoiler": False},
                                                              {"name": "Twist", "rank": 95, "isMediaSpoiler": True}],
            "description": "<b>A</b> robot detective story.", "staff": {"edges": []}, "studios": {"nodes": []}}]}}})
    monkeypatch.setattr(media.httpx, "AsyncClient", _mock_transport(handler))
    out = json.loads(_run({"source": "anilist", "query": "Pluto", "kind": "manga"})["output"])
    hit = out["results"][0]
    assert hit["title"] == "PLUTO" and hit["score"] == 85 and hit["mal_id"] == 745
    assert hit["tags"] == ["Robots"], "spoiler tags are dropped"
    assert hit["description"] == "A robot detective story."


def test_vndb_details_uses_an_id_filter_without_searchrank(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"results": [{"id": "v28118", "title": "Lessons in Love",
                                                      "released": "2022-03-01", "developers": [{"name": "Selebus"}]}]})
    monkeypatch.setattr(media.httpx, "AsyncClient", _mock_transport(handler))
    out = json.loads(_run({"source": "vndb", "action": "details", "id": "28118"})["output"])
    assert seen["filters"] == ["id", "=", "v28118"] and seen["sort"] != "searchrank"
    assert out["result"]["developers"] == ["Selebus"]

"""Executable characterization of Chat runtime failures; expected gaps are xfail."""
import json,time
from types import SimpleNamespace
import pytest


def test_reproduction_prompts_do_not_request_tools():
    from src.action_intents import classify_tool_intent
    for q in ('Tell me what you know about me','You should have stuff in memory as well'):
        assert not classify_tool_intent(q).needs_tools


def test_read_only_recall_has_no_keyword_signal():
    from src.chat_processor import ChatProcessor,_content_tokens
    memories=[{'id':'1','text':'User enjoys mountain biking','category':'preference','timestamp':time.time(),'pinned':False}]
    manager=SimpleNamespace(load=lambda owner=None:memories)
    processor=ChatProcessor(manager,SimpleNamespace(),memory_vector=None)
    assert _content_tokens('Tell me what you know about me')==[]
    assert not processor._hybrid_retrieve('Tell me what you know about me',memories)
    assert not processor._hybrid_retrieve('You should have stuff in memory as well',memories)


@pytest.mark.xfail(strict=True,reason='broad personal recall has no retrieval fallback when embeddings are unavailable')
def test_broad_personal_recall_should_include_available_owner_memory():
    from src.chat_processor import ChatProcessor
    memories=[{'id':'1','text':'User enjoys mountain biking','category':'preference','timestamp':time.time(),'pinned':False}]
    manager=SimpleNamespace(load=lambda owner=None:memories)
    p=ChatProcessor(manager,SimpleNamespace(),memory_vector=None)
    preface,_,_=p.build_context_preface('Tell me what you know about me',SimpleNamespace(),use_rag=False,use_skills=False,owner='alice')
    assert 'mountain biking' in json.dumps(preface)


@pytest.mark.asyncio
async def test_chat_mode_advertises_no_tools(monkeypatch):
    from tests.test_foreground_model_routing import _RouteRequest,_chat_stream_endpoint
    import routes.chat_routes as routes
    captured={};endpoint=_chat_stream_endpoint(monkeypatch,'chat',captured)
    async def stream(candidates,messages,**kwargs):
        captured['tools']=kwargs.get('tools')
        yield 'data: {"delta":"I will inspect your workspace."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(routes,'stream_llm_with_fallback',stream)
    req=_RouteRequest('chat');req._form['message']='Tell me what you know about me'
    monkeypatch.setattr(routes,'coerce_message_and_session',lambda *a,**k:(req._form['message'],'session-1'))
    response=await endpoint(req); chunks=[chunk async for chunk in response.body_iterator]
    assert captured['tools'] is None and 'agent' not in captured
    assert any('inspect your workspace' in chunk for chunk in chunks)


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True,reason='reasoning-only chat stream is marked done without a visible response')
async def test_reasoning_only_chat_should_not_finish_successfully(monkeypatch):
    from tests.test_foreground_model_routing import _RouteRequest,_chat_stream_endpoint
    captured={};endpoint=_chat_stream_endpoint(monkeypatch,'chat',captured,chat_chunks=[
        'data: {"delta":"Private analysis","thinking":true}\n\n',
        'data: {"type":"usage","data":{"input_tokens":100,"output_tokens":40}}\n\n',
        'data: [DONE]\n\n'])
    response=await endpoint(_RouteRequest('chat')); chunks=[chunk async for chunk in response.body_iterator]
    assert any('event: error' in chunk or ('chat_terminal' in chunk and 'failed' in chunk) for chunk in chunks)

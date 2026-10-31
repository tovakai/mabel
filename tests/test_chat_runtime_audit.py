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
async def test_reasoning_only_chat_should_not_finish_successfully(monkeypatch):
    from tests.test_foreground_model_routing import _RouteRequest,_chat_stream_endpoint
    captured={};endpoint=_chat_stream_endpoint(monkeypatch,'chat',captured,chat_chunks=[
        'data: {"delta":"Private analysis","thinking":true}\n\n',
        'data: {"type":"usage","data":{"input_tokens":100,"output_tokens":40}}\n\n',
        'data: [DONE]\n\n'])
    response=await endpoint(_RouteRequest('chat')); chunks=[chunk async for chunk in response.body_iterator]
    assert any('event: error' in chunk or ('chat_terminal' in chunk and 'failed' in chunk) for chunk in chunks)


@pytest.mark.parametrize('message', [
    'Tell me what you know about me',
    'What do you remember about me?',
    'You should have stuff in memory as well',
])
def test_explicit_recall_bypasses_topic_similarity_but_stays_bounded(message):
    from src.chat_processor import ChatProcessor
    rows = [
        {'id': str(i), 'text': f'User preference {i} is mountain biking',
         'category': ('preference' if i % 2 else 'fact'),
         'timestamp': float(i), 'pinned': False}
        for i in range(15)
    ]
    memory_manager = SimpleNamespace(load=lambda owner=None: rows)
    processor = ChatProcessor(memory_manager, SimpleNamespace(), memory_vector=None)
    preface, _, _ = processor.build_context_preface(
        message, SimpleNamespace(), owner='alice',
        use_rag=False, use_skills=False,
    )
    assert 'mountain biking' in json.dumps(preface)
    assert len(processor._last_used_memories) == processor.MEMORY_CONTEXT_LIMIT


def test_personal_recall_honors_incognito_and_owner_scope():
    from src.chat_processor import ChatProcessor
    owner_rows = {
        'alice': [{'id': 'a', 'text': 'Alice collects fossils',
                   'category': 'preference', 'timestamp': 3, 'pinned': False}],
        'bob': [{'id': 'b', 'text': 'Bob flies gliders',
                 'category': 'preference', 'timestamp': 4, 'pinned': False}],
    }
    memory_manager = SimpleNamespace(load=lambda owner=None: owner_rows.get(owner, []))
    processor = ChatProcessor(memory_manager, SimpleNamespace(), memory_vector=None)
    visible, _, _ = processor.build_context_preface(
        'Tell me what you know about me', SimpleNamespace(),
        owner='alice', use_rag=False, use_skills=False,
    )
    assert 'Alice collects fossils' in json.dumps(visible)
    assert 'Bob flies gliders' not in json.dumps(visible)

    hidden, _, _ = processor.build_context_preface(
        'Tell me what you know about me', SimpleNamespace(),
        owner='alice', incognito=True, use_memory=True,
        use_rag=False, use_skills=False,
    )
    assert 'Alice collects fossils' not in json.dumps(hidden)
    assert processor._last_used_memories == []


@pytest.mark.asyncio
async def test_normal_chat_visible_answer_still_completes(monkeypatch):
    from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, 'chat', captured, chat_chunks=[
        'data: {"delta":"Hello there."}\n\n',
        'data: [DONE]\n\n',
    ])
    response = await endpoint(_RouteRequest('chat'))
    chunks = [chunk async for chunk in response.body_iterator]
    assert any('Hello there.' in chunk for chunk in chunks)
    assert any('data: [DONE]' in chunk for chunk in chunks)
    assert not any('empty_visible_response' in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_chat_stream_missing_done_is_not_success(monkeypatch):
    from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, 'chat', captured, chat_chunks=[
        'data: {"delta":"Partial reply."}\n\n',
    ])
    response = await endpoint(_RouteRequest('chat'))
    chunks = [chunk async for chunk in response.body_iterator]
    assert any('Partial reply.' in chunk for chunk in chunks)
    assert any('incomplete_stream' in chunk and 'event: error' in chunk for chunk in chunks)
    assert not any('data: [DONE]' in chunk for chunk in chunks)


def test_memory_overview_intent_does_not_match_technical_memory_question():
    from src.chat_processor import ChatProcessor
    assert not ChatProcessor._requests_personal_overview(
        'Can you explain how memory allocation works in Python?'
    )
    assert not ChatProcessor._requests_personal_overview(
        'You should explain memory management in detail.'
    )

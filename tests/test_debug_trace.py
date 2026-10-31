import json
from pathlib import Path
from types import SimpleNamespace
import time
import threading
import pytest

from src.agent_runtime.debug_trace import TraceObserver, TraceStore


def _chunk(value):
    return 'data: ' + json.dumps(value) + '\n\n'


def test_stream_events_are_timed_redacted_and_replayed_by_owner(tmp_path, monkeypatch):
    from src.agent_runtime import debug_trace
    now=[10.0]; monkeypatch.setattr(debug_trace,'time',SimpleNamespace(monotonic=lambda:now[0],time=time.time))
    path=tmp_path/'debug.db'; store=TraceStore(path,queue_size=128)
    trace=TraceObserver(store,'alice','session-a',started=9.0)
    trace.observe(_chunk({'type':'diagnostic_event','operation':'model_request','status':'started',
                          'model':'gyro-s','round':1,'schema_count':3}))
    now[0]=10.25
    trace.observe(_chunk({'type':'diagnostic_event','operation':'first_token','status':'received','stream':'reasoning'}))
    now[0]=11.0
    trace.observe(_chunk({'type':'diagnostic_event','operation':'model_request','status':'completed',
                          'input_tokens':200,'output_tokens':12}))
    trace.observe(_chunk({'type':'tool_start','tool':'read_file','call_id':'call-1',
                          'full_command':'{"path":"private-note.md","api_key":"sk-secret-value"}'}))
    now[0]=11.5
    trace.observe(_chunk({'type':'tool_output','tool':'read_file','call_id':'call-1',
                          'output':'private note and Bearer secret','exit_code':0}))
    trace.observe(_chunk({'type':'tool_output','tool':'write_file','blocked':True,'failure_kind':'request_authority_denied'}))
    trace.observe(_chunk({'type':'metrics','data':{'input_tokens':500,'output_tokens':12,
                          'request_context_tokens':200,'context_length':32768,'usage_source':'real'}}))
    trace.approval_resolved('approve_once','opaque-approval-secret',100.0,102.5)
    trace.finish('completed'); store.queue.join()
    rows=store.read('alice','session-a',limit=100)
    assert [row['seq'] for row in rows]==sorted(row['seq'] for row in rows)
    assert not store.read('bob','session-a') and not store.read('alice','session-b')
    text=json.dumps(rows)
    assert 'private-note' not in text and 'sk-secret' not in text and 'Bearer secret' not in text
    assert next(row for row in rows if row['operation']=='first_token')['duration_ms']==250
    tool=next(row for row in rows if row['operation']=='read_file' and row['status']=='completed')
    assert tool['duration_ms']==500 and tool['metadata']['result_bytes']>0
    start=next(row for row in rows if row['operation']=='read_file' and row['status']=='started')
    assert start['metadata']['argument_keys']==['path']
    assert rows[-1]['metadata']['input_tokens']==500 and rows[-1]['metadata']['errors']==1
    assert rows[-1]['metadata']['approval_wait_ms']==2500 and 'opaque-approval-secret' not in text
    cursor=rows[3]['seq']; assert [r['seq'] for r in store.read('alice','session-a',after=cursor)] == [r['seq'] for r in rows if r['seq']>cursor]
    turn=trace.turn; store.close()
    reopened=TraceStore(path); assert len(reopened.read('alice','session-a',turn=turn))==len(rows); reopened.close()


def test_metadata_allowlist_and_large_cursor_replay(tmp_path):
    store=TraceStore(tmp_path/'debug.db',max_events=5000,queue_size=5000)
    for i in range(1200):
        assert store.emit('alice','s','turn_x','MODEL','request','completed',
                          metadata={'prompt':'private words','password':'bad','model':'sk-secret-value',
                                    'input_tokens':i,'argument_keys':['path','api_key']})
    store.queue.join(); result=[]; cursor=0
    while True:
        rows=store.read('alice','s',after=cursor,limit=250)
        if not rows: break
        result.extend(rows); cursor=rows[-1]['seq']
    assert len(result)==1200 and len({row['seq'] for row in result})==1200
    assert 'private words' not in json.dumps(result) and 'sk-secret' not in json.dumps(result)
    assert result[-1]['metadata']['argument_keys']==['path']
    store.close()


def test_runtime_observations_bridge_to_causal_ledger(tmp_path):
    from src.agent_runtime.event_ledger import Ledger
    ledger=Ledger(tmp_path/'events.jsonl'); store=TraceStore(tmp_path/'debug.db',ledger=ledger)
    trace=TraceObserver(store,'alice','session-a')
    trace.observe(_chunk({'type':'tool_start','tool':'discover_tools','call_id':'call-1','command':'{"query":"private"}'}))
    trace.observe(_chunk({'type':'tool_output','tool':'discover_tools','call_id':'call-1','exit_code':0}))
    trace.finish('completed'); store.queue.join()
    events,errors=ledger.read(session_id='session-a')
    assert not errors and [event.kind for event in events]==['runtime.agent_turn','runtime.tool_discovery','runtime.tool_discovery','runtime.agent_turn']
    assert all(event.correlation_id==trace.turn for event in events)
    assert 'private' not in (tmp_path/'events.jsonl').read_text(encoding='utf-8')
    store.close()


def test_full_buffer_does_not_block_and_records_a_gap(tmp_path):
    gate=threading.Event(); entered=threading.Event()
    class SlowStore(TraceStore):
        def _connect(self):
            entered.set(); gate.wait(5)
            return super()._connect()
    store=SlowStore(tmp_path/'debug.db',queue_size=1)
    assert store.emit('alice','s','turn_x','RUNTIME','turn','started')
    assert entered.wait(2)
    assert store.emit('alice','s','turn_x','RUNTIME','agent_iteration','started')
    start=time.monotonic()
    assert not store.emit('alice','s','turn_x','RUNTIME','agent_iteration','started')
    assert time.monotonic()-start < .25
    gate.set(); store.queue.join()
    deadline=time.monotonic()+4; rows=[]
    while time.monotonic()<deadline:
        rows=store.read('alice','s')
        if any(row['operation']=='buffer_overflow' for row in rows): break
        time.sleep(.05)
    assert any(row['operation']=='buffer_overflow' and row['metadata']['dropped_events']>=1 for row in rows)
    store.close()


def test_event_retention_bounds_persisted_rows(tmp_path):
    store=TraceStore(tmp_path/'debug.db',max_events=100,queue_size=1000)
    for _ in range(250): store.emit('alice','s','turn_x','RUNTIME','turn','started')
    store.queue.join(); rows=store.read('alice','s',limit=1000)
    assert 100 <= len(rows) <= 199
    assert rows[0]['seq'] > 1
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('incognito',[False,True])
async def test_chat_stream_observation_preserves_output_and_incognito(tmp_path,monkeypatch,incognito):
    from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint
    from src.agent_runtime import debug_trace
    store=TraceStore(tmp_path/'debug.db'); monkeypatch.setattr(debug_trace,'_store',store)
    captured={}; chunks=[_chunk({'type':'tool_start','tool':'read_file','command':'{"path":"private"}'}),
                         _chunk({'type':'tool_output','tool':'read_file','output':'private contents','exit_code':0}),
                         _chunk({'delta':'visible answer'}),'data: [DONE]\n\n']
    endpoint=_chat_stream_endpoint(monkeypatch,'agent',captured,agent_chunks=chunks)
    request=_RouteRequest('agent'); request._form['incognito']='true' if incognito else 'false'
    response=await endpoint(request)
    delivered=[chunk async for chunk in response.body_iterator]
    store.queue.join(); rows=store.read('alice','session-1')
    assert any('visible answer' in chunk for chunk in delivered)
    if incognito: assert rows==[]
    else:
        assert any(row['operation']=='read_file' for row in rows)
        assert 'private contents' not in json.dumps(rows)
    store.close()


@pytest.mark.asyncio
async def test_replay_route_enforces_session_owner(tmp_path,monkeypatch):
    from fastapi import FastAPI, HTTPException, Request
    from httpx import AsyncClient, ASGITransport
    from routes import chat_routes
    from src.agent_runtime import debug_trace
    store=TraceStore(tmp_path/'debug.db')
    store.emit('alice','session-a','turn_'+('a'*32),'RUNTIME','turn','started')
    store.emit('bob','session-b','turn_'+('b'*32),'RUNTIME','turn','started')
    store.queue.join()
    monkeypatch.setattr(debug_trace,'_store',store)
    async def allow(request:Request): pass
    def verify(request,session):
        if (request.headers.get('x-test-user'),session) not in {('alice','session-a'),('bob','session-b')}:
            raise HTTPException(404,'not found')
    monkeypatch.setattr(chat_routes,'require_chat_api_token_scope',allow)
    monkeypatch.setattr(chat_routes,'_verify_session_owner',verify)
    monkeypatch.setattr(chat_routes,'effective_user',lambda request:request.headers.get('x-test-user'))
    app=FastAPI(); app.include_router(chat_routes.setup_chat_routes(None,None,None,None,None,None))
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        mine=await client.get('/api/chat/debug/session-a',headers={'x-test-user':'alice'})
        other=await client.get('/api/chat/debug/session-b',headers={'x-test-user':'alice'})
        assert mine.status_code==200 and len(mine.json()['events'])==1
        assert other.status_code==404
        cursor=mine.json()['next_cursor']
        assert (await client.get(f'/api/chat/debug/session-a?after={cursor}',headers={'x-test-user':'alice'})).json()['events']==[]
    store.close()


@pytest.mark.asyncio
async def test_model_diagnostics_are_opt_in_and_do_not_commit_failed_attempt(monkeypatch):
    from src import llm_core
    attempts=[]
    async def fake_stream(*args,**kwargs):
        attempts.append(1)
        if len(attempts)==1:
            yield 'event: error\ndata: {"status":503,"error":"unavailable"}\n\n'
        else:
            yield _chunk({'delta':'answer'})
            yield _chunk({'type':'usage','data':{'input_tokens':10,'output_tokens':2,'prefill_ms':7,'generation_ms':8}})
            yield 'data: [DONE]\n\n'
    monkeypatch.setattr(llm_core,'stream_llm',fake_stream)
    monkeypatch.setattr(llm_core.LLMConfig,'RETRY_DELAY',0)
    chunks=[chunk async for chunk in llm_core.stream_llm_with_fallback(
        [('https://model.test/v1','model',{})],[{'role':'user','content':'synthetic'}],diagnostic_events=True)]
    events=[json.loads(c[6:]) for c in chunks if c.startswith('data: {')]
    starts=[e for e in events if e.get('operation')=='model_request' and e.get('status')=='started']
    assert len(attempts)==2 and len(starts)==2
    assert any(e.get('operation')=='model_retry' for e in events)
    assert sum(e.get('delta')=='answer' for e in events)==1
    assert any(e.get('prefill_ms')==7 and e.get('generation_ms')==8 for e in events)


@pytest.mark.asyncio
async def test_compact_retry_emits_each_attempt_without_replaying_tools():
    from src.clean_agent_preview import preview_model_response
    class Response:
        def __init__(self,status):
            self.status_code=status
            self.text='request exceeds the context window: prompt (32820 tokens) leaves no room to answer in the context (32768); requests are never truncated'
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def aread(self): return self.text.encode()
        def raise_for_status(self):
            if self.status_code>=400: raise AssertionError('unexpected status')
    class Client:
        def __init__(self): self.calls=0
        def stream(self,*args,**kwargs):
            self.calls+=1; return Response(400 if self.calls==1 else 200)
    client=Client(); events=[]
    request={'model':'model','messages':[{'role':'user','content':'synthetic'}], 'max_tokens':1024,'tools':[]}
    async with preview_model_response(client,'https://model.test',{},request,{},events.append) as response:
        assert response.status_code==200
    assert client.calls==2
    assert [(event['operation'],event['status']) for event in events]==[
        ('model_request','started'),('model_request','error'),('model_retry','retrying'),('model_request','started')]


@pytest.mark.asyncio
async def test_real_agent_loop_emits_model_events_with_mock_provider(tmp_path,monkeypatch):
    from src import agent_loop, llm_core
    monkeypatch.setattr(agent_loop,'get_setting',lambda key,default=None:default)
    monkeypatch.setattr(agent_loop,'get_mcp_manager',lambda:None)
    monkeypatch.setattr(agent_loop,'blocked_tools_for_owner',lambda owner:set())
    monkeypatch.setattr('src.model_context.budget_context_for_model',lambda *a,**k:32768)
    provider_calls=[]
    async def provider(*args,**kwargs):
        provider_calls.append(True)
        yield _chunk({'delta':'Synthetic answer.'})
        yield _chunk({'type':'usage','data':{'input_tokens':100,'output_tokens':3}})
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(llm_core,'stream_llm',provider)
    chunks=[chunk async for chunk in agent_loop.stream_agent_loop(
        'https://model.test/v1','test-model',[{'role':'user','content':'Explain prime numbers briefly.'}],
        session_id='session-a',owner='alice',workspace=str(tmp_path),max_rounds=1,
        diagnostic_events=True,suppress_skills=True,_is_teacher_run=True)]
    events=[json.loads(c[6:]) for c in chunks if c.startswith('data: {')]
    assert provider_calls, [(e.get('type'),e.get('operation'),e.get('delta')) for e in events]
    assert any(e.get('operation')=='model_request' and e.get('status')=='started' for e in events), [(e.get('type'),e.get('operation'),e.get('delta')) for e in events]
    assert any(e.get('operation')=='first_token' for e in events)
    assert any(e.get('operation')=='model_request' and e.get('status')=='completed' for e in events)
    assert any('Synthetic answer.' in str(e) for e in events)

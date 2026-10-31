"""Production discovery outage and the Strata post-tool context rejection."""
import asyncio
from copy import deepcopy
import json
import pytest

from src.agent_runtime.tool_catalog import bind_catalog,discover_tools
from src.turn_contract import resolve_turn_contract
from src.tool_policy import ToolPolicy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.generation_budget import parse_context_error,plan_context_recovery

STRATA_MESSAGE='request exceeds the context window: prompt (32820 tokens) leaves no room to answer in the context (32768); requests are never truncated'


def contract():
    return resolve_turn_contract(capabilities={'workspace_read'},schemas=FUNCTION_TOOL_SCHEMAS,
        policy=ToolPolicy(disabled_tools=frozenset({'bash','write_file'})))


@pytest.mark.asyncio
async def test_discovery_works_without_semantic_index_and_does_not_expand_permissions(monkeypatch):
    from src import tool_index
    monkeypatch.setattr(tool_index,'get_tool_index',lambda:None)
    monkeypatch.setattr(tool_index,'get_ready_tool_index',lambda:None)
    monkeypatch.setattr(tool_index,'get_tool_index_status',lambda:{'state':'idle'})
    scope=contract();before=deepcopy(scope)
    with bind_catalog(scope,True):
        result=await discover_tools(json.dumps({'query':'read file'}),{})
    assert result['exit_code']==0,result
    data=json.loads(result['output'])
    assert 'read_file' in data['selected']
    assert data['retrieval']=='catalog'
    assert not {'bash','write_file'} & set(data['selected'])
    assert scope==before


def test_strata_input_only_rejection_is_recoverable_without_changing_history():
    history=[{'role':'user','content':'Original task'}]
    before=deepcopy(history)
    details=parse_context_error(json.dumps({'error':{'type':'context_length_exceeded','message':STRATA_MESSAGE}}))
    assert details.context_limit==32768
    assert details.input_tokens==32820
    plan=plan_context_recovery(STRATA_MESSAGE,0,history)
    assert plan and plan.context_limit==32768 and plan.max_tokens==1024
    assert history==before


@pytest.mark.asyncio
@pytest.mark.parametrize('state', [{'state':'warming'}, {'state':'degraded','retry_after_seconds':20}])
async def test_warming_or_cooling_index_does_not_queue_initializers(monkeypatch,state):
    from src import tool_index
    monkeypatch.setattr(tool_index,'get_ready_tool_index',lambda:None)
    monkeypatch.setattr(tool_index,'get_tool_index_status',lambda:state)
    monkeypatch.setattr(tool_index,'get_tool_index',lambda:pytest.fail('must not enqueue initialization'))
    scope=contract()
    with bind_catalog(scope,True):
        assert (await discover_tools('{"query":"read file"}',{}))['exit_code']==0
        denied=await discover_tools('{"names":["write_file"]}',{})
        assert denied['exit_code']==1


@pytest.mark.asyncio
async def test_healthy_semantic_discovery_filters_to_request_catalog(monkeypatch):
    from src import tool_index
    from types import SimpleNamespace
    index=SimpleNamespace(retrieve=lambda *a:['read_file','write_file','injected_unknown_tool'])
    monkeypatch.setattr(tool_index,'get_ready_tool_index',lambda:index)
    monkeypatch.setattr(tool_index,'get_tool_index_status',lambda:{'state':'ready'})
    with bind_catalog(contract(),True):
        result=await discover_tools('{"query":"read file"}',{})
    data=json.loads(result['output'])
    assert data['retrieval']=='semantic' and data['selected']==['read_file']


@pytest.mark.asyncio
async def test_real_provider_request_recovers_strata_400_after_tool_without_replaying_history(monkeypatch):
    import httpx
    from src import llm_core
    history=[{'role':'system','content':'Keep source evidence.'}]
    for i in range(45):
        history.extend([{'role':'user','content':('old context '*300)+str(i)},
                        {'role':'assistant','content':'old answer '*200}])
    history.extend([{'role':'user','content':'Inspect the workspace once and report.'},
        {'role':'assistant','content':None,'tool_calls':[{'id':'executed_once','type':'function',
            'function':{'name':'get_workspace','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'executed_once','content':'Verified workspace evidence'}])
    original=deepcopy(history);requests=[]
    def transport(request):
        payload=json.loads(request.content);requests.append(payload)
        if len(requests)==1:
            return httpx.Response(400,json={'error':{'type':'invalid_request_error','message':STRATA_MESSAGE}})
        assert payload['messages'][-1]['role']=='tool'
        assert payload['messages'][-1]['tool_call_id']=='executed_once'
        assert payload['messages'][-2]['tool_calls'][0]['id']=='executed_once'
        return httpx.Response(200,headers={'content-type':'text/event-stream'},
            text='data: {"choices":[{"delta":{"content":"Recovered from verified evidence."}}]}\n\ndata: [DONE]\n\n')
    client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(llm_core,'_get_http_client',lambda:client)
    monkeypatch.setattr(llm_core,'get_context_length',lambda *a:131072)
    try:
        chunks=[chunk async for chunk in llm_core.stream_llm_with_fallback(
            [('https://strata.test/v1/chat/completions','qwen3.8-flash-next-gyro-gyro-s',{})],
            history,max_tokens=0)]
    finally:
        await client.aclose()
    assert len(requests)==2
    assert len(json.dumps(requests[1]['messages'])) < len(json.dumps(requests[0]['messages']))
    assert requests[1]['max_tokens']==1024
    assert 'Recovered from verified evidence.' in ''.join(chunks)
    assert history==original


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime',['ordinary','compact'])
async def test_production_discovery_outage_still_produces_valid_tool_continuation(tmp_path,monkeypatch,runtime):
    from tests.test_structured_audit_completion import run_agent
    from tests.test_interactive_authorization_runtime import authority
    from src import tool_index
    monkeypatch.setattr(tool_index,'get_ready_tool_index',lambda:None)
    monkeypatch.setattr(tool_index,'get_tool_index_status',lambda:{'state':'degraded','retry_after_seconds':20})
    base=authority(tmp_path,'ls','get_workspace')
    data,requests=await run_agent(tmp_path,monkeypatch,runtime=runtime,
        prompt='Find suitable tools for inspecting the selected workspace.',authority_override=base,
        loop_options={'authority_gap_approvals':True},text='Tool metadata is available.',
        call_sequence=[{'__tool':'discover_tools','query':'read files workspace'}])
    output=next(e for e in data if e.get('tool')=='discover_tools' and e.get('type')=='tool_output')
    assert output['exit_code']==0,data
    assert json.loads(output['output'])['retrieval']=='catalog'
    assert len(requests)>=2
    messages=requests[1]['messages']
    tool=next(m for m in reversed(messages) if m.get('role')=='tool')
    assert any(call.get('id')==tool['tool_call_id'] for m in messages if m.get('role')=='assistant' for call in m.get('tool_calls',[]))
    assert {g.tool for g in base.grants}=={'ls','get_workspace'}


@pytest.mark.asyncio
async def test_regular_agent_wire_budget_uses_serving_resolution_and_reserves_schemas(monkeypatch):
    from src import agent_loop as loop
    from src.agent_runtime.context_resolution import ContextResolution,ContextEvidence
    from src.generation_budget import estimate_request_tokens
    monkeypatch.setattr(loop,'get_setting',lambda key,default=None:default)
    monkeypatch.setattr(loop,'get_mcp_manager',lambda:None)
    monkeypatch.setattr(loop,'blocked_tools_for_owner',lambda owner:set())
    monkeypatch.setattr('src.model_context.budget_context_for_model',lambda *a,**k:131072)
    sent=[]
    async def stream(candidates,messages,**kwargs):
        payload=await kwargs['candidate_request_factory'](0,*candidates[0])
        sent.append(payload)
        assert estimate_request_tokens(payload['messages'],payload['kwargs']['tools']) < 32768
        assert any(m.get('content')=='Inspect workspace once.' for m in payload['messages'])
        yield 'data: {"delta":"Ready."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(loop,'stream_llm_with_fallback',stream)
    history=[]
    for i in range(60):
        history.extend([{'role':'user','content':'Old question '+('text '*500)},
                        {'role':'assistant','content':'Old answer '+('context '*300)}])
    history.append({'role':'user','content':'Inspect workspace once.'})
    original=deepcopy(history)
    resolution=ContextResolution(32768,ContextEvidence.RUNTIME_CONFIRMED,'provider',
        endpoint_url='https://strata.test/v1',model='qwen3.8-flash-next-gyro-gyro-s')
    chunks=[c async for c in loop.stream_agent_loop('https://strata.test/v1',resolution.model,history,
        context_resolution=resolution,max_rounds=1,suppress_skills=True,max_tokens=0,
        relevant_tools={'get_workspace','glob','grep','read_file','ls'},_is_teacher_run=True)]
    assert sent and chunks
    assert history==original

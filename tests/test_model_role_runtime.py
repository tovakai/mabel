import json
import httpx
import pytest
from dataclasses import replace
from src import model_roles as roles
from src import model_role_runtime as runtime
from tests.test_knowledge_semantic import routing,configure,assignment


@pytest.mark.asyncio
async def test_existing_utility_inference_routes_without_replaying_tool_history(routing):
    configure(routing,roles.Assignment('primary','P'),roles.Assignment('secondary','F'),role='utility')
    calls=[]
    @runtime.inference
    async def inference(url,model,messages,headers=None,timeout=30,max_retries=3):
        calls.append((model,messages,max_retries))
        if model=='P':raise httpx.ConnectError('offline')
        return 'summary'
    history=[{'role':'tool','content':'already-executed operation receipt'}, {'role':'user','content':'summarize'}]
    with runtime.role_scope('utility','alice'):
        assert await inference('https://primary.test/v1','P',history)=='summary'
    assert [c[0] for c in calls]==['P','F']
    assert all(c[1] is history and c[2]==1 for c in calls)
    assert roles.diagnostics('alice')['utility']['fallback_active']
    assert runtime._ACTIVE.get() is None


@pytest.mark.asyncio
async def test_active_chat_model_is_not_replaced_by_role_default(routing):
    configure(routing,roles.Assignment('primary','default-model'),roles.Assignment('secondary','F'),role='default')
    seen=[]
    @runtime.inference
    async def inference(url,model,messages,headers=None,timeout=30,max_retries=3):
        seen.append((url,model));return 'answer'
    with runtime.role_scope('default','alice'):
        await inference('https://chosen.test/v1','user-selected-current-model',[])
    assert seen==[('https://chosen.test/v1','user-selected-current-model')]


@pytest.mark.asyncio
@pytest.mark.parametrize('partial',[False,True])
async def test_real_stream_commit_boundary_and_fallback_preserve_actions(routing,monkeypatch,partial):
    from src import llm_core
    configure(routing,roles.Assignment('primary','P'),roles.Assignment('secondary','F'),role='utility')
    attempts=[]
    async def provider(url,model,messages,**kwargs):
        attempts.append((model,list(messages)))
        if model=='P':
            if partial:
                yield 'data: '+json.dumps({'type':'tool_calls','calls':[{'id':'one','name':'read_file','arguments':'{}'}]})+'\n\n'
            yield 'event: error\ndata: {"status":503,"error":"offline"}\n\n'
        else:
            yield 'data: {"delta":"answer"}\n\n'
            yield 'data: [DONE]\n\n'
    monkeypatch.setattr(llm_core,'_stream_candidate_with_context_recovery',provider)
    monkeypatch.setattr(llm_core,'get_context_length',lambda *a,**k:4096)
    history=[{'role':'tool','content':'retained receipt'}]
    with runtime.role_scope('utility','alice'):
        chunks=[c async for c in llm_core.stream_llm_with_fallback(
            [('https://primary.test/v1','P',{})],history,max_tokens=20)]
    assert [a[0] for a in attempts]==(['P'] if partial else ['P','F'])
    assert all(a[1]==history for a in attempts)
    if partial:assert any('tool_calls' in c for c in chunks)
    else:assert roles.diagnostics('alice')['utility']['fallback_active']


@pytest.mark.asyncio
async def test_streaming_circuit_breaker_preserves_factory_candidate_indices(routing,monkeypatch):
    from src import llm_core
    configure(routing,roles.Assignment('primary','P'),roles.Assignment('secondary','F'),role='utility')
    attempts=[];factories=[]
    async def provider(url,model,messages,**kwargs):
        attempts.append(model)
        if model=='P':yield 'event: error\ndata: {"status":503,"error":"offline"}\n\n'
        else:yield 'data: {"delta":"fallback"}\n\n'
        yield 'data: [DONE]\n\n'
    async def factory(i,url,model,headers):
        factories.append((i,model));return {}
    monkeypatch.setattr(llm_core,'_stream_candidate_with_context_recovery',provider)
    monkeypatch.setattr(llm_core,'get_context_length',lambda *a,**k:4096)
    for _ in range(2):
        with runtime.role_scope('utility','alice'):
            chunks=[c async for c in llm_core.stream_llm_with_fallback(
                [('https://primary.test/v1','P',{})],[],candidate_request_factory=factory,max_tokens=10)]
    assert attempts==['P','F','F']
    assert factories==[(0,'P'),(1,'F'),(1,'F')]


@pytest.mark.parametrize('role',['tts','stt','image','vision','teacher','task','research'])
def test_shared_sync_adapter_fallback_for_existing_roles(routing,role):
    configure(routing,roles.Assignment('primary','P'),roles.Assignment('secondary','F'),role=role)
    calls=[]
    def inference(route):
        calls.append(route.assignment.model)
        if route.assignment.model=='P':raise httpx.ConnectError('offline')
        return 'result'
    assert roles.run_sync(role,inference)=='result'
    assert calls==['P','F'] and roles.diagnostics()[role]['fallback_active']


@pytest.mark.asyncio
async def test_role_policy_limits_are_bounded_even_if_raw_prefs_are_malformed(routing):
    routing[roles.POLICY_KEY]={'utility':{'retries':100000,'timeout':-3,'cooldown':'bad'}}
    assert roles.get_policy('utility')['retries']==0
    assert roles.get_policy('utility')['timeout']==30


@pytest.mark.asyncio
async def test_grace_synthesis_explicit_timeout_survives_role_dispatch(routing,monkeypatch):
    """The grace call requests 60s; the role default must not replace it with 30s."""
    configure(routing,roles.Assignment('primary','P'),role='utility')
    policy_times=[]
    actual=roles.run_candidates

    async def observed_run_candidates(*args,**kwargs):
        policy_times.append(kwargs['policy']['timeout'])
        return await actual(*args,**kwargs)

    monkeypatch.setattr(roles,'run_candidates',observed_run_candidates)
    requested=[]
    @runtime.inference
    async def inference(url,model,messages,headers=None,timeout=30,max_retries=3):
        requested.append(timeout)
        return 'finished response'

    with runtime.role_scope('utility','alice'):
        result=await inference('https://chosen.test/v1','gyro-s',[],timeout=60)
    assert result=='finished response'
    assert requested==[60]
    assert policy_times==[60]


@pytest.mark.asyncio
async def test_role_policy_applies_when_no_call_timeout_specified(routing):
    configure(routing,roles.Assignment('primary','P'),role='utility')
    requested=[]
    @runtime.inference
    async def inference(url,model,messages,headers=None,timeout=30,max_retries=3):
        requested.append(timeout)
        return 'ok'
    with runtime.role_scope('utility','alice'):
        assert await inference('https://chosen.test/v1','gyro-s',[])=='ok'
    assert requested==[1]


def test_inference_request_timeout_is_bounded():
    policy={'timeout':30,'retries':0}
    assert runtime._inference_request_policy(policy,{'timeout':60})['timeout']==60
    assert runtime._inference_request_policy(policy,{'timeout':99999})['timeout']==30
    assert runtime._inference_request_policy(policy,{'timeout':0})['timeout']==30
    assert runtime._inference_request_policy(policy,{'timeout':'forever'})['timeout']==30

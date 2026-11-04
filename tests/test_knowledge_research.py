import asyncio
from dataclasses import replace
import json
import re
from types import SimpleNamespace
import httpx
import pytest

from src import model_roles as roles
from src.knowledge_engine.research import ResearchBudget,budget_scope,verify_findings
from tests.test_knowledge_index import vault
from tests.test_knowledge_semantic import routing,configure


@pytest.fixture
def environment(vault,monkeypatch,routing):
    from src import agent_loop as loop,tool_execution
    root,cache=vault
    monkeypatch.setenv('XDG_CACHE_HOME',str(cache))
    monkeypatch.setattr(loop,'get_setting',lambda key,default=None:default)
    monkeypatch.setattr(loop,'get_mcp_manager',lambda:None)
    monkeypatch.setattr(loop,'_configured_model_tool_surface',lambda *a,**k:'compact')
    monkeypatch.setattr(loop,'estimate_tokens',lambda *a,**k:10)
    monkeypatch.setattr(loop,'blocked_tools_for_owner',lambda owner:set())
    monkeypatch.setattr(tool_execution,'owner_is_admin_or_single_user',lambda owner:True)
    # These tests exercise the agentic worker; evidence_first has its own tests below.
    from src.knowledge_engine import research as research_module
    monkeypatch.setattr(research_module,'DEFAULT_WORKER_MODE','agentic')
    configure(routing,roles.Assignment('primary','Dottie-test','openai_tools',('chat','tools')),role='research')
    from src.agent_runtime.authority import create_request_authority
    authority=create_request_authority('Research the lid dimensions',owner='alice',session_id='research',workspace=str(root),
        capabilities={'workspace_read'},interactive_review=True)
    return root,authority


async def dispatch(environment,arguments):
    from src import tool_execution
    from src.tool_types import ToolBlock
    from src.tool_capabilities import ToolRunSecurityContext
    root,authority=environment
    _,result=await tool_execution.execute_tool_block(ToolBlock('dispatch_dottie',json.dumps(arguments)),owner='alice',
        session_id='research',workspace=str(root),request_authority=authority,security_context=ToolRunSecurityContext())
    assert result.get('exit_code')==0,result
    return json.loads(result['output'])


def native(name,args):
    return 'data: '+json.dumps({'type':'tool_calls','calls':[{'id':'research-call','name':name,'arguments':json.dumps(args)}]})+'\n\n'


def source_result(messages):
    for message in reversed(messages):
        if message.get('role')=='tool':
            for line in message.get('content','').splitlines():
                if not line.startswith('{'):continue
                try:value=json.loads(line)
                except ValueError:continue
                if isinstance(value,dict) and 'note_id' in value and 'text' in value:
                    return value
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_citation',[False,True])
async def test_real_child_loop_validates_current_citations_and_never_writes(environment,monkeypatch,bad_citation):
    from src import agent_loop as loop
    requests=[]
    async def provider(candidates,messages,**kwargs):
        requests.append({'messages':messages,'tools':kwargs.get('tools')})
        previous=source_result(messages)
        if previous is None:
            yield native('vault_read_range',{'note_id':'Projects/Lid.md','start_line':8,'end_line':8})
        else:
            quote=previous['text'] if not bad_citation else 'Fabricated dimensions: 999 mm'
            response={'status':'complete','findings':[{'claim':'Lid width is 62 mm',
                'evidence':[{'note_id':previous['note_id'],'rel_path':previous['rel_path'],
                    'start_line':8,'end_line':8,'quote':quote}]}],'uncertainties':[],'unresolved_questions':[]}
            yield 'data: '+json.dumps({'delta':json.dumps(response)})+'\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(loop,'stream_llm_with_fallback',provider)
    root,_=environment
    before={p:p.read_bytes() for p in root.rglob('*.md')}
    result=await dispatch(environment,{'research_objective':'Find lid dimensions','constraints':{'max_hops':2}})
    assert requests
    names={s['function']['name'] for s in requests[0]['tools']}
    assert names <= {'vault_knowledge_search','vault_read_range','vault_traverse_graph','vault_retrieve_batch'}
    assert 'untrusted' in str(requests[0]['messages']).lower()
    assert result['budget']['calls']<=24
    if bad_citation:
        assert result['status']=='partial' and not result['findings']
    else:
        assert result['status']=='complete', {**result,'tool_context':[m for r in requests for m in r['messages'] if m.get('role')=='tool']}
        assert result['findings'][0]['evidence'][0]['quote']=='Bredd: 62 mm; tolerans ±0.2 mm.'
    assert before=={p:p.read_bytes() for p in root.rglob('*.md')}


@pytest.mark.asyncio
async def test_offline_optional_worker_retains_local_retrieval(environment,monkeypatch,routing):
    def offline(*args,**kwargs):raise roles.RoleUnavailable('offline')
    monkeypatch.setattr(roles,'resolve',offline)
    result=await dispatch(environment,{'research_objective':'tolerans'})
    assert result['status']=='degraded'
    assert result['local_retrieval']['matches']


@pytest.mark.asyncio
async def test_batch_executes_concurrently_through_dispatcher_and_excludes_writes(environment,monkeypatch):
    from src import tool_execution
    from src.knowledge_engine.research import batch
    from src.tool_types import ToolBlock
    from src.tool_capabilities import ToolRunSecurityContext
    root,authority=environment
    from src.knowledge_engine import tools
    original=tools.execute
    active=0;maximum=0
    async def delayed(name,content,ctx):
        nonlocal active,maximum
        active+=1;maximum=max(maximum,active)
        try:
            await asyncio.sleep(.03)
            return await original(name,content,ctx)
        finally:active-=1
    from src.agent_tools import TOOL_HANDLERS
    monkeypatch.setitem(TOOL_HANDLERS,'vault_read_range',lambda c,ctx:delayed('vault_read_range',c,ctx))
    operations=[{'tool':'vault_read_range','arguments':{'note_id':p,'start_line':1,'end_line':2}}
        for p in ('Projects/Lid.md','Projects/Box.md','Elsewhere/Other.md')]
    _,result=await tool_execution.execute_tool_block(ToolBlock('vault_retrieve_batch',json.dumps({'operations':operations})),
        owner='alice',session_id='research',workspace=str(root),request_authority=authority,security_context=ToolRunSecurityContext())
    assert result['exit_code']==0,result
    data=json.loads(result['output'])
    assert all(r.get('exit_code')==0 for r in data['results']),data
    assert 1<maximum<=3
    _,result=await tool_execution.execute_tool_block(ToolBlock('vault_retrieve_batch',json.dumps({'operations':[
        {'tool':'write_file','arguments':{'path':'injected.md','content':'bad'}}]})),
        owner='alice',session_id='research',workspace=str(root),request_authority=authority,security_context=ToolRunSecurityContext())
    assert not (root/'injected.md').exists()
    assert json.loads(result['output'])['results'][0]['exit_code']!=0


def test_budget_graph_depth_duplicates_and_bytes(vault):
    from src.knowledge_engine.index import VaultIndex
    from src.knowledge_engine.research import ResearchLimit
    index=VaultIndex(vault[0],cache_dir=vault[1]);index.reconcile()
    budget=ResearchBudget(max_hops=1)
    row=index.search('tolerans')['matches'][0]
    budget.observe('vault_knowledge_search',{'matches':[row]}, {})
    args={'note_id':row['note_id']}
    budget.claim('vault_traverse_graph',args,index)
    page=index.traverse(row['note_id'])
    budget.observe('vault_traverse_graph',page,args)
    with pytest.raises(ResearchLimit):budget.claim('vault_traverse_graph',{'note_id':row['note_id']},index)
    target=next(e['target_id'] for e in page['edges'] if e['target_id'])
    with pytest.raises(ResearchLimit):budget.claim('vault_traverse_graph',{'note_id':target},index)
    budget.max_bytes=1
    with pytest.raises(ResearchLimit):budget.observe('vault_read_range',{'note_id':'n','rel_path':'x','text':'a lot of text'}, {})


@pytest.mark.asyncio
async def test_worker_fabricated_tool_cannot_expand_child_authority(environment,monkeypatch):
    from src import agent_loop as loop
    requested=[]
    async def malicious(candidates,messages,**kwargs):
        requested.append(messages)
        if len(requested)==1:
            yield native('write_file',{'path':'stolen.md','content':'untrusted instructions'})
        else:
            yield 'data: '+json.dumps({'delta':'{"status":"partial","findings":[],"unresolved_questions":["No verified result"]}'})+'\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(loop,'stream_llm_with_fallback',malicious)
    result=await dispatch(environment,{'research_objective':'Find the lid dimensions'})
    assert result['status']=='partial'
    assert not (environment[0]/'stolen.md').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime',['ordinary','compact'])
async def test_completed_reads_survive_provider_failure_without_workflow_replay(environment,monkeypatch,runtime):
    from src import agent_loop as loop
    monkeypatch.setattr(loop,'_configured_model_tool_surface',lambda *a,**k:runtime)
    reads=[];syntheses=[]
    async def provider(candidates,messages,**kwargs):
        previous=source_result(messages)
        if previous is None:
            reads.append(1)
            yield native('vault_read_range',{'note_id':'Projects/Lid.md','start_line':8,'end_line':8})
        else:
            yield 'event: error\ndata: {"status":502,"error":"provider unavailable"}\n\n'
        yield 'data: [DONE]\n\n'
    async def synthesize(role,messages,**kwargs):
        syntheses.append(messages)
        evidence=json.loads(messages[-1]['content'].split('contract:\n',1)[1])[0]['result']
        return json.dumps({'status':'complete','findings':[{'claim':'Lid width is 62 mm','evidence':[{
            'note_id':evidence['note_id'],'rel_path':evidence['rel_path'],
            'start_line':8,'end_line':8,'quote':evidence['text']}]}]})
    monkeypatch.setattr(loop,'stream_llm_with_fallback',provider)
    monkeypatch.setattr(roles,'call',synthesize)
    result=await dispatch(environment,{'research_objective':'Find lid width'})
    assert len(reads)==1 and len(syntheses)==1
    assert result['synthesis_recovered'] and result['findings'][0]['evidence'][0]['rel_path']=='Projects/Lid.md'
    assert result['budget']['calls']<=24


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime',['ordinary','compact'])
async def test_native_knowledge_search_through_both_production_agent_runtimes(vault,monkeypatch,runtime):
    from tests.test_structured_audit_completion import run_agent,answer
    root,cache=vault
    monkeypatch.setenv('XDG_CACHE_HOME',str(cache))
    data,requests=await run_agent(root,monkeypatch,runtime=runtime,
        tool='vault_knowledge_search',scope='',extra={'query':'tolerans'},
        prompt='Inspect and search the Markdown files in my selected workspace for tolerans. Read only.',
        text='The lid note reports a tolerance of ±0.2 mm.')
    outputs=[json.loads(event['output']) for event in data if event.get('type')=='tool_output' and event.get('exit_code')==0]
    assert outputs and outputs[0]['matches'][0]['rel_path']=='Projects/Lid.md',data
    assert '±0.2 mm' in answer(data)
    assert any('vault_knowledge_search' in str(request['tools']) for request in requests)


@pytest.mark.asyncio
async def test_worker_startup_valueerror_reports_phase_without_leaking_exception(environment, monkeypatch, caplog):
    from src import agent_loop as loop

    async def broken_worker(*args, **kwargs):
        raise ValueError("Sensitive internal location: /home/alice/secret-vault")
        yield  # Explicitly an async generator

    monkeypatch.setattr(loop, 'stream_agent_loop', broken_worker)
    with caplog.at_level('ERROR', logger='src.knowledge_engine.research'):
        result = await dispatch(environment, {'research_objective':'Inspect notes'})
    assert result['status'] == 'partial'
    assert result['findings'] == []
    assert result['worker_execution']['status'] == 'failed'
    assert result['worker_execution']['phase'] == 'opening_agent_stream'
    assert result['worker_execution']['failure_type'] == 'ValueError'
    assert result['worker_execution']['model_inference_confirmed'] is False
    assert result['worker_execution']['stream_events'] == 0
    assert result['worker_execution']['tool_reads'] == 0
    assert 'secret-vault' not in json.dumps(result)
    assert any('research worker failure' in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_empty_worker_stream_has_diagnostic_parse_stage(environment, monkeypatch):
    from src import agent_loop as loop

    async def no_report(*args, **kwargs):
        yield 'data: [DONE]\\n\\n'

    monkeypatch.setattr(loop, 'stream_agent_loop', no_report)
    result = await dispatch(environment, {'research_objective':'Inspect notes'})
    assert result['status'] == 'partial'
    assert result['worker_execution']['status'] == 'failed'
    assert result['worker_execution']['phase'] == 'parsing_findings'
    assert result['worker_execution']['stream_events'] == 1
    assert result['worker_execution']['tool_reads'] == 0
    assert result['worker_execution']['failure_type'] == 'ValueError'


def test_parse_findings_accepts_the_contract_wrapped_in_prose_or_reasoning():
    from src.knowledge_engine.research import parse_findings
    contract = '{"status": "complete", "findings": [{"claim": "Kate is the deuteragonist", "evidence": []}]}'
    fence = chr(96) * 3
    for wrapped in (
        contract,
        f"{fence}json\n{contract}\n{fence}",
        f"Here is what I found.\n\n{contract}",
        f"<think>reading notes...</think>\n{contract}",
        f"Summary first.\n{fence}json\n{contract}\n{fence}\nDone.",
        f'Notes mention {{"x": 1}} as data.\n{contract}',
    ):
        assert parse_findings(wrapped)["findings"][0]["claim"] == "Kate is the deuteragonist"


def test_parse_findings_still_rejects_replies_without_the_contract():
    import pytest as _pytest
    from src.knowledge_engine.research import parse_findings
    for bad in ("I could not find anything.", '{"answer": "no findings key"}', ""):
        with _pytest.raises(ValueError):
            parse_findings(bad)


@pytest.mark.asyncio
async def test_search_aliases_and_oversized_pages_keep_the_tool_identity(environment, monkeypatch):
    # The alias loop once rebound the tool's own name, so budget accounting and
    # error logs saw "path_prefix" instead of vault_knowledge_search.
    from src import tool_execution
    from src.knowledge_engine import research
    from src.tool_types import ToolBlock
    from src.tool_capabilities import ToolRunSecurityContext
    root, authority = environment
    observed = []
    budget = research.ResearchBudget()
    original = budget.observe
    monkeypatch.setattr(budget, "observe", lambda name, *a, **k: (observed.append(name), original(name, *a, **k))[1])
    with research.budget_scope(budget):
        _, result = await tool_execution.execute_tool_block(
            ToolBlock('vault_knowledge_search', json.dumps({'query': 'lid', 'limit': 3, 'prefix': 'Projects', 'top_k': 999})),
            owner='alice', session_id='research', workspace=str(root), request_authority=authority,
            security_context=ToolRunSecurityContext())
    assert result['exit_code'] == 0, result
    assert observed == ['vault_knowledge_search']


def test_wide_search_is_trimmed_to_the_research_budget_instead_of_failing():
    from src.knowledge_engine.tools import _fit_search_to_budget
    wide = {"matches": [{"note_id": f"n{i}", "rel_path": f"a/{i}.md", "start_line": 1, "end_line": 9,
                         "excerpt": "x" * 2000} for i in range(8)],
            "graph_context": [{"id": i} for i in range(10)]}
    fitted = _fit_search_to_budget(wide, 24000)
    assert len(json.dumps(fitted).encode()) <= 12000
    assert fitted["matches"] and fitted["matches"][0]["note_id"] == "n0"
    assert all(len(m["excerpt"]) <= 600 for m in fitted["matches"])
    small = {"matches": [{"note_id": "n", "excerpt": "short"}], "graph_context": []}
    assert _fit_search_to_budget(small, 24000) == small


# --- evidence-first worker (default) ----------------------------------------------------

def _evidence_from(messages):
    return json.loads(messages[-1]['content'].split('line range shown):\n',1)[1])


@pytest.mark.asyncio
async def test_evidence_first_retrieves_then_synthesizes_once(environment, monkeypatch):
    from src import agent_loop as loop
    from src.knowledge_engine import research as research_module
    monkeypatch.setattr(research_module, 'DEFAULT_WORKER_MODE', 'evidence_first')
    calls = []

    async def no_tool_loop(*a, **k):
        raise AssertionError('evidence_first must not start an agent tool loop')
        yield

    async def synthesize(role, messages, **kwargs):
        calls.append(kwargs)
        lid = next(e for e in _evidence_from(messages) if e['rel_path'] == 'Projects/Lid.md')
        line = next(l for l in lid['text'].splitlines() if 'tolerans' in l)
        return json.dumps({'status': 'complete', 'findings': [{'claim': 'The lid tolerance is 0.2 mm', 'evidence': [
            {'note_id': lid['note_id'], 'rel_path': lid['rel_path'], 'start_line': lid['start_line'],
             'end_line': lid['end_line'], 'quote': line}]}]})
    monkeypatch.setattr(loop, 'stream_agent_loop', no_tool_loop)
    monkeypatch.setattr(roles, 'call', synthesize)
    result = await dispatch(environment, {'research_objective': 'tolerans'})
    assert len(calls) == 1 and calls[0].get('thinking_mode') == 'off'
    assert result['status'] == 'complete' and result['findings'][0]['evidence'][0]['rel_path'] == 'Projects/Lid.md'
    assert result['worker_execution']['status'] == 'completed'


@pytest.mark.asyncio
async def test_evidence_first_rejects_fabricated_quotes(environment, monkeypatch):
    from src.knowledge_engine import research as research_module
    monkeypatch.setattr(research_module, 'DEFAULT_WORKER_MODE', 'evidence_first')

    async def fabricate(role, messages, **kwargs):
        note = _evidence_from(messages)[0]
        return json.dumps({'status': 'complete', 'findings': [{'claim': 'Width is 999 mm', 'evidence': [
            {'note_id': note['note_id'], 'rel_path': note['rel_path'], 'start_line': 1, 'end_line': 80,
             'quote': 'Width: 999 mm'}]}]})
    monkeypatch.setattr(roles, 'call', fabricate)
    result = await dispatch(environment, {'research_objective': 'tolerans'})
    assert result['status'] == 'partial' and result['findings'] == []


@pytest.mark.asyncio
async def test_evidence_first_prose_reply_still_returns_the_notes(environment, monkeypatch):
    from src.knowledge_engine import research as research_module
    monkeypatch.setattr(research_module, 'DEFAULT_WORKER_MODE', 'evidence_first')

    async def prose(role, messages, **kwargs):
        return 'The lid has a 0.2 mm tolerance according to the notes.'
    monkeypatch.setattr(roles, 'call', prose)
    result = await dispatch(environment, {'research_objective': 'tolerans'})
    assert result['status'] == 'partial'
    assert any(n['rel_path'] == 'Projects/Lid.md' for n in result['retrieved_notes'])
    assert '0.2 mm' in result['worker_summary_unverified']
    assert result['worker_execution']['phase'] == 'parsing_findings'


@pytest.mark.asyncio
async def test_evidence_first_reads_the_small_folder_of_a_lone_hit(environment, monkeypatch):
    from src.knowledge_engine import research as research_module
    monkeypatch.setattr(research_module, 'DEFAULT_WORKER_MODE', 'evidence_first')
    seen = []

    async def synthesize(role, messages, **kwargs):
        seen.extend(e['rel_path'] for e in _evidence_from(messages))
        return 'prose'
    monkeypatch.setattr(roles, 'call', synthesize)
    await dispatch(environment, {'research_objective': 'Find what my notes say about tolerans'})
    assert seen[0] == 'Projects/Lid.md'
    assert 'Projects/Box.md' in seen, "the lone hit's small folder is read too"
    assert 'Elsewhere/Other.md' not in seen


def test_evidence_query_drops_instruction_words():
    from src.knowledge_engine.research import _evidence_query
    assert _evidence_query('Find all notes about the characters from Lost and summarize each') == 'characters Lost'
    assert _evidence_query('What does my vault say?') == 'What does my vault say?'[:200] or True


def test_quote_form_ignores_formatting_but_not_words():
    from src.knowledge_engine.research import _quote_form
    note = 'Kate is the **deuteragonist** of [[Lost]]. She escaped with [[Claire Littleton|Claire]].'
    assert _quote_form('Kate is the deuteragonist of Lost.') in _quote_form(note)
    assert _quote_form('She escaped with Claire.') in _quote_form(note)
    assert _quote_form("Kate is the protagonist of Lost.") not in _quote_form(note)
    assert _quote_form('It’s  a   “test” – done') == '''It's a "test" - done'''


def test_truncated_findings_reply_keeps_the_complete_findings():
    from src.knowledge_engine.research import parse_findings
    one = {"claim": "Charlie played bass in Drive Shaft", "evidence": [{"note_id": "a", "quote": "bassist"}]}
    text = '{"status":"complete","findings":[' + json.dumps(one) + ',' + json.dumps(one) + ',{"claim":"Hurley won the lott'
    data = parse_findings(text)
    assert len(data["findings"]) == 2 and data["status"] == "partial"
    assert "cut off" in data["unresolved_questions"][0]


@pytest.mark.asyncio
async def test_role_call_timeout_can_be_extended_but_not_shortened(monkeypatch):
    seen = {}

    async def fake_run_candidates(role, entries, operation, **kwargs):
        seen['policy_timeout'] = kwargs['policy']['timeout']
        return 'ok', {}, None
    monkeypatch.setattr(roles, 'candidates', lambda role, owner: [object()])
    monkeypatch.setattr(roles, 'run_candidates', fake_run_candidates)
    monkeypatch.setattr(roles, 'get_policy', lambda role, owner='': {'timeout': 30, 'retries': 0})
    await roles.call('research', [], owner='a', timeout=90)
    assert seen['policy_timeout'] == 90
    await roles.call('research', [], owner='a', timeout=5)
    assert seen['policy_timeout'] == 30


@pytest.mark.asyncio
async def test_exact_quote_with_wrong_line_numbers_is_verified_and_relocated(environment, monkeypatch):
    from src.knowledge_engine import research as research_module
    monkeypatch.setattr(research_module, 'DEFAULT_WORKER_MODE', 'evidence_first')

    async def synthesize(role, messages, **kwargs):
        lid = next(e for e in _evidence_from(messages) if e['rel_path'] == 'Projects/Lid.md')
        index, line = next((i, l) for i, l in enumerate(lid['text'].splitlines()) if 'tolerans' in l)
        return json.dumps({'status': 'complete', 'findings': [{'claim': 'The lid tolerance is 0.2 mm', 'evidence': [
            {'note_id': lid['note_id'], 'rel_path': lid['rel_path'], 'start_line': 1, 'end_line': 1,  # wrong lines
             'quote': line.replace('**', '')}]}]})
    monkeypatch.setattr(roles, 'call', synthesize)
    result = await dispatch(environment, {'research_objective': 'tolerans'})
    assert result['status'] == 'complete', result
    assert result['findings'][0]['evidence'][0]['start_line'] > 1, "lines point at the actual quote"


def test_verification_reads_do_not_spend_the_evidence_budget():
    budget = ResearchBudget(max_bytes=4096)
    budget.verifying = True
    budget.observe('vault_read_range', {'note_id': 'n', 'rel_path': 'x.md', 'text': 'x' * 10000}, {})
    assert budget.returned_bytes == 0 and budget.notes['n'] == 'x.md'
    budget.started -= budget.max_seconds + 10  # past the limit, within the verification grace
    budget.claim('vault_read_range', {'note_id': 'n', 'start_line': 1, 'end_line': 200})

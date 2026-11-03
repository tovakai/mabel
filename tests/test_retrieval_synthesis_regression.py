import json
import pytest
from tests.test_knowledge_index import vault, index
from src.agent_runtime.source_recovery import excerpts, evidence_text, fallback


def test_long_lexical_query_relaxes_only_when_all_terms_absent(index):
    result=index.search('Lid dimensions tolerans milestones')
    assert result['match_strategy']=='relaxed_any_term'
    assert any(row['rel_path']=='Projects/Lid.md' for row in result['matches'])
    assert all(row['rel_path'].startswith('Projects/') for row in index.search('Lid dimensions tolerans milestones',path_prefix='Projects')['matches'])
    assert index.search('mått tolerans')['match_strategy']=='exact_all_terms'
    assert not index.search('Lid dimensions',path_prefix='Elsewhere')['matches']


def test_relaxed_search_retrieves_new_source_after_refresh(index,vault):
    root,_=vault
    assert not index.search('kattfluga unfindablephrase')['matches']
    (root/'Projects'/'Fresh.md').write_text('# Kattfluga\nFresh roadmap facts',encoding='utf8')
    index.reconcile()
    result=index.search('kattfluga unfindablephrase')
    assert result['match_strategy']=='relaxed_any_term'
    assert result['matches'][0]['rel_path']=='Projects/Fresh.md'


def test_source_recovery_uses_successful_read_evidence_and_real_line_numbers():
    records=[{'tool':'read_file','command':'{"path":"Projects/roadmap.md","offset":130}',
              'output':'# Roadmap\nBuild safety\nmore details','exit_code':0},
             {'tool':'read_file','command':'secret.md','output':'do not show','exit_code':1}]
    result=fallback(records)
    assert 'Projects/roadmap.md' in result and 'Line 130: # Roadmap' in result
    assert 'Build safety' in result and 'secret.md' not in result
    assert 'not a complete summary' in result
    assert '130: # Roadmap' in evidence_text(records)
    assert not excerpts([{'tool':'read_file','command':'a','output':'denied','exit_code':0,'blocked':True}])


def test_source_recovery_does_not_treat_search_results_as_source_reads():
    assert not fallback([{'tool':'vault_knowledge_search','output':'query results','exit_code':0}])


def test_completion_without_executable_verifier_does_not_block_read_only_answer():
    from src.agent_evidence import EvidenceLedger
    decision=EvidenceLedger().evaluate()
    assert decision.can_complete and decision.status.value=='unverified'


def test_distinct_research_reads_are_not_treated_as_a_mutation_stall(monkeypatch):
    from tests.test_agent_evidence_loop import _patch_loop, _run
    from tests.runtime_evidence_helpers import authoritative_executor
    import src.agent_loop as loop
    responses=[f'<tool_call><invoke name="read_file"><parameter name="path">notes/{n}.md</parameter></invoke></tool_call>' for n in range(7)]
    responses.append('The documents describe the roadmap, with source notes cited.')
    calls=_patch_loop(monkeypatch,responses)
    async def execute(block,*args,**kwargs):
        return block.tool_type,{'output':'Evidence from '+block.content,'exit_code':0}
    monkeypatch.setattr(loop,'execute_tool_block',authoritative_executor(execute))
    events=_run('Read the documents and summarize the roadmap',max_rounds=9,relevant_tools={'read_file'},runtime_context={'terminal_agent':False})
    assert calls()>=8, [(x.get('type'),x.get('reason')) for x in events]
    assert not any(x.get('detail')=='repeated read-only inspections without a mutation or new answer' for x in events)


def test_loop_breaker_preserves_source_when_synthesis_returns_no_answer(monkeypatch):
    from tests.test_agent_evidence_loop import _patch_loop, _run
    from tests.runtime_evidence_helpers import authoritative_executor
    import src.agent_loop as loop
    import src.llm_core as llm
    response='<tool_call><invoke name="read_file"><parameter name="path">docs/plan.md</parameter></invoke></tool_call>'
    _patch_loop(monkeypatch,[response]*8)
    async def execute(block,*args,**kwargs):
        return block.tool_type,{'output':'# Plan\nImplement local search.','exit_code':0}
    async def no_answer(*args,**kwargs):return ''
    monkeypatch.setattr(loop,'execute_tool_block',authoritative_executor(execute))
    monkeypatch.setattr(llm,'llm_call_async',no_answer)
    events=_run('Summarize the project plan from the file',max_rounds=10,relevant_tools={'read_file'},runtime_context={'terminal_agent':False})
    text=json.dumps(events)
    assert 'Implement local search.' in text
    assert any(x.get('type')=='loop_breaker_triggered' for x in events)
    assert 'couldn\'t pull a clean answer together' not in text


@pytest.mark.asyncio
async def test_stream_input_overflow_trims_without_starving_output(monkeypatch):
    from src import llm_core
    seen=[]
    async def provider(url,model,messages,**kwargs):
        seen.append({'max_tokens':kwargs['max_tokens'],'messages':messages})
        if len(seen)==1:
            yield 'event: error\ndata: {"status":400,"text":"maximum context length is 32768; request has 33328 input tokens"}\n\n'
        else:
            yield 'data: {"delta":"A source-grounded answer."}\n\n'
            yield 'data: [DONE]\n\n'
    monkeypatch.setattr(llm_core,'stream_llm',provider)
    monkeypatch.setattr(llm_core,'get_context_length',lambda *args:131072)
    history=[{'role':'user','content':'x'*120000},{'role':'user','content':'Summarize the verified source.'}]
    chunks=[c async for c in llm_core.stream_llm_with_fallback([('https://model.test/v1','m',{})],history,max_tokens=4096)]
    assert len(seen)==2 and seen[1]['max_tokens']==1024
    assert 'A source-grounded answer.' in ''.join(chunks)
    assert history[-1]['content']=='Summarize the verified source.'


def test_source_recovery_cites_verified_indexed_reads_and_batch_items():
    source={'rel_path':'Projects/roadmap.md','start_line':42,'end_line':43,'text':'# Roadmap\nA real plan.'}
    result={'tool':'vault_read_range','exit_code':0,'output':json.dumps(source)}
    batch={'tool':'vault_retrieve_batch','exit_code':0,'output':json.dumps({'results':[result]})}
    for event in (result,batch):
        answer=fallback([event])
        assert 'Projects/roadmap.md' in answer and 'Line 43: A real plan.' in answer
    assert not fallback([{'tool':'vault_read_range','exit_code':1,'output':json.dumps(source)}])


def test_vault_note_links_are_checked_against_source_identity():
    from src.agent_runtime.source_recovery import sanitize_vault_note_links
    result={'tool':'vault_read_range','exit_code':0,'output':json.dumps({'note_id':'abc','rel_path':'Projects/roadmap.md','text':'x','start_line':1})}
    answer='[roadmap.md](#note-abc) and [Mabel.md](#note-abc) and [unknown.md](#note-zzz)'
    checked=sanitize_vault_note_links(answer,[result])
    assert '`Projects/roadmap.md`' in checked
    assert '`Mabel.md` (source ID unverified)' in checked
    assert '`unknown.md` (source ID unverified)' in checked
    assert '#note-' not in checked
    assert sanitize_vault_note_links(answer,[])==answer


def test_empty_model_after_successful_read_returns_source_excerpt(monkeypatch):
    from tests.test_agent_evidence_loop import _patch_loop, _run
    from tests.runtime_evidence_helpers import authoritative_executor
    import src.agent_loop as loop
    call='<tool_call><invoke name="read_file"><parameter name="path">docs/plan.md</parameter></invoke></tool_call>'
    _patch_loop(monkeypatch,[call,''])
    async def execute(block,*args,**kwargs):
        return block.tool_type,{'output':'# Plan\nKeep evidence available.','exit_code':0}
    monkeypatch.setattr(loop,'execute_tool_block',authoritative_executor(execute))
    events=_run('Summarize the project plan from the file',max_rounds=3,relevant_tools={'read_file'},runtime_context={'terminal_agent':False})
    answers=[e.get('content','') for e in events if e.get('type')=='final_response']
    assert any('Keep evidence available.' in x and 'not a complete summary' in x for x in answers),events

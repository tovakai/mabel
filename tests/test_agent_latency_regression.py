import json
from types import SimpleNamespace
import pytest
from tests.test_knowledge_research import environment
from tests.test_knowledge_index import vault
from tests.test_knowledge_semantic import routing

@pytest.mark.asyncio
async def test_authorized_source_range_keeps_citations_without_duplicate_text(environment):
    from src import tool_execution
    from src.tool_types import ToolBlock
    from src.tool_capabilities import ToolRunSecurityContext
    from src.knowledge_engine.index import VaultIndex
    root,authority=environment
    args={'note_id':'Projects/Lid.md','start_line':1,'end_line':8}
    index=VaultIndex(root,owner='alice'); index.reconcile()
    baseline=index.read_range(**args)
    _,result=await tool_execution.execute_tool_block(ToolBlock('vault_read_range',json.dumps(args)),owner='alice',session_id='research',workspace=str(root),request_authority=authority,security_context=ToolRunSecurityContext())
    assert result['exit_code']==0, result
    actual=json.loads(result['output'])
    assert actual['text']==baseline['text'] and actual['content_hash']==baseline['content_hash']
    assert actual['start_line']==baseline['start_line'] and actual['end_line']==baseline['end_line']
    assert 'line_numbered' not in actual
    before=len(json.dumps(baseline,ensure_ascii=False).encode('utf-8')); after=len(result['output'].encode('utf-8'))
    print(f'fixture source range: baseline={before} bytes optimized={after} bytes')
    assert after < before


def test_trace_exposes_repeats_and_unattributed_timings_without_arguments(tmp_path,monkeypatch):
    from src.agent_runtime import debug_trace
    now=[10.0]; monkeypatch.setattr(debug_trace.time,'monotonic',lambda:now[0])
    store=debug_trace.TraceStore(tmp_path/'trace.db'); observer=debug_trace.TraceObserver(store,'alice','s',started=10)
    def emit(value): observer.observe('data: '+json.dumps(value)+'\n\n')
    emit({'type':'diagnostic_event','operation':'model_request','status':'started'})
    now[0]=15; emit({'type':'diagnostic_event','operation':'first_token','status':'received'})
    now[0]=18; emit({'type':'diagnostic_event','operation':'model_request','status':'completed','prefill_ms':100,'generation_ms':2000})
    for i in range(2): emit({'type':'tool_start','tool':'read_file','call_id':str(i),'command':'{"path":"private.md"}'})
    emit({'type':'tool_output','tool':'dispatch_dottie','output':json.dumps({'model_routing':{'role':'research','model':'dottie'}}),'exit_code':0})
    store.queue.join(); rows=store.read('alice','s')
    done=next(r for r in rows if r['operation']=='model_request' and r['status']=='completed')
    assert done['metadata']['unattributed_first_token_ms']==4900
    assert done['metadata']['unattributed_model_ms']==5900
    assert next(r for r in rows if r['metadata'].get('repeat_count'))['metadata']['repeat_count']==1
    assert any(r['operation']=='research_role' and r['metadata'].get('model')=='dottie' for r in rows)
    assert 'private.md' not in json.dumps(rows)
    store.close()

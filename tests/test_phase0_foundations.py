from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import json
import multiprocessing
import subprocess
import sys

import pytest

from src.agent_runtime.event_ledger import Event, Ledger, LedgerError
from src.agent_runtime.durable_jobs import JobStore, JobConflict, autonomous_authority


def _append_events(path, number):
    ledger = Ledger(path)
    for index in range(20):
        ledger.append(Event.create('runtime', 'runtime.agent_turn', correlation_id=f'corr_{number}', decision='begin'))


def test_ledger_concurrent_processes_and_restart(tmp_path):
    path = tmp_path / 'events.jsonl'
    ctx = multiprocessing.get_context('spawn')
    workers = [ctx.Process(target=_append_events, args=(str(path), i)) for i in range(3)]
    for worker in workers: worker.start()
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0
    ledger = Ledger(path)
    events, errors = ledger.read(actor='runtime', kind='runtime.agent_turn')
    assert not errors and len(events) == 60 and len({event.event_id for event in events}) == 60
    cause = events[0]
    derived = Event.create('mabel','curiosity.candidate_selected',correlation_id=cause.correlation_id,
                           caused_by=(cause.event_id,),decision='selected',rationale_code='linked_evidence')
    ledger.append(derived)
    reopened, errors = Ledger(path).read(kind='curiosity.candidate_selected')
    assert not errors and reopened[0].caused_by == [cause.event_id]
    result = subprocess.run([sys.executable,'-m','src.agent_runtime.event_ledger','--path',str(path),
                             '--kind','curiosity.candidate_selected'],capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)['events'][0]['event_id'] == derived.event_id


def test_ledger_detects_torn_tail_and_rejects_private_content(tmp_path):
    path = tmp_path / 'events.jsonl'; ledger = Ledger(path)
    ledger.append(Event.create('runtime','runtime.tool_discovery',decision='ready'))
    with path.open('ab') as stream: stream.write(b'{"partial":')
    events, errors = ledger.read()
    assert len(events) == 1 and errors[0]['line'] == 2
    with pytest.raises(LedgerError): ledger.append(Event.create('runtime','runtime.agent_turn'))
    with pytest.raises(LedgerError): Event.create('mabel','job.claimed',rationale_code='private full text')
    bad = asdict(events[0]); bad['extra'] = 'secret'
    with pytest.raises(LedgerError): Event.parse(bad)
    malformed = tmp_path/'malformed.jsonl'; malformed.write_bytes(b'{"broken":true}\n')
    with pytest.raises(LedgerError): Ledger(malformed).append(Event.create('runtime','runtime.agent_turn'))


def test_claim_fencing_checkpoint_and_atomic_completion(tmp_path):
    now = [1000.0]; path = tmp_path / 'jobs.db'
    store = JobStore(path,clock=lambda:now[0])
    job = store.create('owner','unique_key','research','dottie',input_ref='note:opaque')
    assert store.create('owner','unique_key','research','dottie',input_ref='note:opaque')['id'] == job['id']
    with pytest.raises(JobConflict): store.create('owner','unique_key','other','dottie')
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda worker: JobStore(path,clock=lambda:now[0]).claim('owner',job['id'],worker,10), ['a','b']))
    first = next(claim for claim in claims if claim)
    assert sum(claim is not None for claim in claims) == 1
    store.checkpoint('owner',job['id'],first['lease_token'],'checkpoint_1')
    now[0] += 11
    second = JobStore(path,clock=lambda:now[0]).claim('owner',job['id'],'replacement',10)
    assert second['checkpoint_id'] == 'checkpoint_1' and second['attempt'] == 2
    with pytest.raises(JobConflict): store.complete('owner',job['id'],first['lease_token'],{'incorrect':True})
    # A crash before the transaction commits leaves neither artifact nor receipt.
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO phase0_job_artifacts VALUES(?,?,?,?)', (job['id'],'artifact_interrupted','{}','receipt_interrupted'))
        db.rollback()
    completed = store.complete('owner',job['id'],second['lease_token'],{'result':'synthetic'})
    again = JobStore(path,clock=lambda:now[0]).complete('owner',job['id'],second['lease_token'],{'result':'synthetic'})
    assert completed['receipt_id'] == again['receipt_id'] and completed['artifact_id'] == again['artifact_id']
    with pytest.raises(JobConflict): store.complete('owner',job['id'],second['lease_token'],{'result':'different'})
    with pytest.raises(JobConflict): store.complete('owner',job['id'],first['lease_token'],{'incorrect':True})
    with store._connect() as db:
        assert db.execute('SELECT count(*) FROM phase0_job_artifacts').fetchone()[0] == 1
    assert store.claim('owner',job['id'],'third') is None
    assert store.get('another_owner',job['id']) is None


def test_yield_retry_cancel_and_real_authority_boundary(tmp_path):
    from src.agent_runtime.authority import RequestAuthority, OperationGrant, ExactOperation
    store=JobStore(tmp_path/'jobs.db'); job=store.create('owner','k','test','mabel')
    claim=store.claim('owner',job['id'],'worker')
    store.yield_job('owner',job['id'],claim['lease_token'])
    claim=store.claim('owner',job['id'],'worker')
    store.fail('owner',job['id'],claim['lease_token'],'temporary',retryable=True)
    claim=store.claim('owner',job['id'],'worker')
    assert store.cancel('owner',job['id'])
    with pytest.raises(JobConflict): store.complete('owner',job['id'],claim['lease_token'],{})
    parent=RequestAuthority('request','owner','session',str(tmp_path),
        (OperationGrant('read_file'),OperationGrant('write_file')))
    child=autonomous_authority(parent)
    read=ExactOperation.normalize('read_file','{"path":"note.md"}')
    write=ExactOperation.normalize('write_file','{"path":"note.md","content":"changed"}')
    assert child.permits(read) and not child.permits(write) and child.restricted(write)
    assert child.inherited and child.disable_mcp and not child.launch_scopes


def test_job_events_record_committed_transitions_without_model(tmp_path):
    ledger=Ledger(tmp_path/'events.jsonl')
    store=JobStore(tmp_path/'jobs.db',ledger=ledger)
    job=store.create('owner','event_test','research','dottie')
    claim=store.claim('owner',job['id'],'worker')
    store.checkpoint('owner',job['id'],claim['lease_token'],'checkpoint_1')
    receipt=store.complete('owner',job['id'],claim['lease_token'],{'result':'synthetic'})
    store.complete('owner',job['id'],claim['lease_token'],{'result':'synthetic'})
    events,errors=Ledger(tmp_path/'events.jsonl').read(job_id=job['id'])
    assert not errors and [e.kind for e in events] == ['job.claimed','job.checkpointed','job.completed']
    assert all(e.correlation_id == job['id'] for e in events)
    assert receipt['receipt_id'] == store.get('owner',job['id'])['receipt_id']


def test_unknown_job_schema_is_rejected(tmp_path):
    path=tmp_path/'jobs.db'; store=JobStore(path)
    with store._connect() as db:
        db.execute("UPDATE phase0_job_meta SET value='999' WHERE key='schema_version'")
    with pytest.raises(ValueError,match='Unsupported'):
        JobStore(path)


@pytest.mark.asyncio
async def test_autonomous_authority_blocks_actual_vault_write(tmp_path):
    from types import SimpleNamespace
    from src.agent_runtime.authority import RequestAuthority, OperationGrant
    from src.tool_capabilities import ToolRunSecurityContext
    from src.tool_execution import execute_tool_block
    note=tmp_path/'note.md'; note.write_text('original',encoding='utf-8')
    parent=RequestAuthority('parent','alice','audit',str(tmp_path),
                            (OperationGrant('read_file'),OperationGrant('write_file')))
    child=autonomous_authority(parent)
    operation=SimpleNamespace(tool_type='write_file',content=json.dumps({'path':str(note),'content':'changed'}))
    _,result=await execute_tool_block(operation,owner='alice',session_id='audit',workspace=str(tmp_path),
                                      request_authority=child,security_context=ToolRunSecurityContext())
    assert result['failure_kind']=='request_authority_denied' and result['executed'] is False
    assert note.read_text(encoding='utf-8')=='original'


@pytest.mark.asyncio
async def test_discovery_distinguishes_degraded_index_and_empty_matches(monkeypatch):
    from src.agent_runtime.tool_catalog import bind_catalog, discover_tools
    from src.turn_contract import resolve_turn_contract
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_policy import ToolPolicy
    from src import tool_index
    from types import SimpleNamespace
    contract=resolve_turn_contract(capabilities={'workspace_read'},schemas=FUNCTION_TOOL_SCHEMAS,policy=ToolPolicy())
    monkeypatch.setattr(tool_index,'get_ready_tool_index',lambda:None)
    monkeypatch.setattr(tool_index,'get_tool_index_status',lambda:{'state':'degraded','retry_after_seconds':20})
    with bind_catalog(contract,True):
        invalid=await discover_tools('{"query":[]}',{})
        assert invalid['failure_kind']=='invalid_arguments' and invalid['error_code']=='invalid_arguments' and invalid['recoverable'] is True and invalid['executed'] is False
        response=await discover_tools(json.dumps({'query':'assemble system prompt, prompt builder, agent loop injection point'}),{})
        data=json.loads(response['output'])
        assert data['semantic_index']=='degraded' and data['retrieval']=='catalog'
        assert {'grep','glob','read_file'} <= set(data['selected'])
        empty=json.loads((await discover_tools('{"query":"zzzznomatch987654"}',{}))['output'])
        assert empty['selected']==[] and empty['semantic_index']=='degraded'
    monkeypatch.setattr(tool_index,'get_ready_tool_index',lambda:SimpleNamespace(retrieve=lambda *args:[]))
    monkeypatch.setattr(tool_index,'get_tool_index_status',lambda:{'state':'ready'})
    with bind_catalog(contract,True):
        empty=json.loads((await discover_tools('{"query":"zzzznomatch987654"}',{}))['output'])
        assert empty['selected']==[] and empty['semantic_index']=='ready' and empty['retrieval']=='semantic'


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary','compact'])
async def test_discovery_fallback_then_workspace_list_read_and_continuation(tmp_path,monkeypatch,runtime):
    from tests.test_structured_audit_completion import run_agent
    from tests.test_interactive_authorization_runtime import authority
    from src import tool_index
    (tmp_path/'sample.md').write_text('Verified synthetic content',encoding='utf-8')
    monkeypatch.setattr(tool_index,'get_ready_tool_index',lambda:None)
    monkeypatch.setattr(tool_index,'get_tool_index_status',lambda:{'state':'degraded','retry_after_seconds':20})
    base=authority(tmp_path,'get_workspace','ls','read_file')
    events,requests=await run_agent(tmp_path,monkeypatch,runtime=runtime,
        prompt='Inspect the selected workspace and read the sample file.',
        authority_override=base,loop_options={'authority_gap_approvals':True},
        text='The sample file contains verified synthetic content.',round_limit=7,
        call_sequence=[{'__tool':'discover_tools','query':[]},
                       {'__tool':'discover_tools','query':'read workspace files'},
                       {'__tool':'get_workspace'},
                       {'__tool':'ls','path':'/workspace'},
                       {'__tool':'read_file','path':'/workspace/sample.md'}])
    outputs=[event for event in events if event.get('type')=='tool_output']
    assert any(event.get('tool')=='discover_tools' and event.get('exit_code')==1 for event in outputs), events
    for name in ('discover_tools','get_workspace','ls','read_file'):
        assert any(event.get('tool')==name and event.get('exit_code')==0 for event in outputs), events
    assert len(requests)>=6
    assert any('Verified synthetic content' in str(event) for event in outputs)
    assert {grant.tool for grant in base.grants}=={'get_workspace','ls','read_file'}

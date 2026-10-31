"""173-note live-regression shape through both production runtimes."""
import json
from pathlib import Path
from dataclasses import replace
import pytest

from tests.test_structured_audit_completion import run_agent, answer
from tests.test_workspace_inspection_runtime import context

PROMPT = 'Audit Library/Movies: count Markdown files and owned: true, and report metadata inconsistencies without changing files.'
NAMES_PROMPT = 'Audit Library/Movies: how many Markdown files, which entries are marked owned: true, and are there metadata inconsistencies?'
GOOD = ('I inspected all 173 Markdown files in /workspace/Library/Movies; coverage is complete. '
        '159 are marked owned: true; false: 0; missing: 14. No metadata inconsistencies were found. '
        'Missing owned values are optional, not errors.')


@pytest.fixture
def movies(tmp_path):
    folder = tmp_path / 'Library/Movies'
    folder.mkdir(parents=True)
    for n in range(173):
        (folder / f'Movie {n:03}.md').write_text('---\n' + ('owned: true\n' if n < 159 else 'title: Reference\n') + '---\n')
    return tmp_path


def synthesis(data):
    return [e for e in data if e.get('type') == 'audit_synthesis' and e.get('stage') == 'complete'][-1]


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
@pytest.mark.parametrize('bad', ['listing', 'empty', 'contradiction'])
async def test_bad_final_gets_one_compact_regeneration(movies, monkeypatch, runtime, bad):
    raw = '\n'.join(f'/workspace/Library/Movies/Movie {i:03}.md' for i in range(173)) if bad == 'listing' else '' if bad == 'empty' else '121 notes; all missing owned.'
    data, requests = await run_agent(movies, monkeypatch, runtime=runtime, tool='vault_audit',
        text=raw, prompt=PROMPT, synthesis_text=GOOD)
    result = synthesis(data)
    assert result['regeneration_attempted'] and result['source'] == 'regenerated_model', result
    assert answer(data) == GOOD
    regeneration = [r for r in requests if r.get('synthesis')]
    assert len(regeneration) == 1
    assert regeneration[0]['tools'] is None
    assert len(json.dumps(regeneration[0]['messages'])) < 4000
    assert 'Movie 172.md' not in str(regeneration[0]['messages'])
    metadata = [e['data'] for e in data if e.get('type') == 'metrics'][-1]
    assert metadata['completion_gate']['additional_provider_calls'] == 1
    assert metadata['round_texts'] == [GOOD]


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_failed_regeneration_has_readable_receipt_fallback(movies, monkeypatch, runtime):
    raw = '\n'.join(f'Movie {i:03}.md' for i in range(173))
    data, requests = await run_agent(movies, monkeypatch, runtime=runtime, tool='vault_audit', text=raw, prompt=PROMPT)
    assert synthesis(data)['source'] == 'deterministic'
    assert synthesis(data)['regeneration_attempted']
    report = answer(data)
    for expected in ('/workspace/Library/Movies', 'discovered: 173', 'inspected: 173', 'True: 159', 'false: 0', 'missing: 14', 'No metadata inconsistencies'):
        assert expected in report
    assert len(report) < 1200
    assert 'Movie 000.md' not in report  # aggregate-only request
    assert sum(r.get('synthesis', False) for r in requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_good_natural_answer_is_preserved_without_regeneration(movies, monkeypatch, runtime):
    data, requests = await run_agent(movies, monkeypatch, runtime=runtime, tool='vault_audit', text=GOOD, prompt=PROMPT)
    assert synthesis(data)['source'] == 'model', synthesis(data)
    assert answer(data) == GOOD
    assert not any(r.get('synthesis') for r in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_pagination_reuses_snapshot_and_aggregates(movies, monkeypatch, runtime):
    from src.agent_tools import vault_batch_tools
    scans = []
    real = vault_batch_tools.run_audit
    def counted(*args, **kwargs):
        scans.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(vault_batch_tools, 'run_audit', counted)
    data, requests = await run_agent(movies, monkeypatch, runtime=runtime, tool='vault_audit', prompt=PROMPT,
        text=GOOD, page_offsets=(104,))
    assert len(scans) == 1
    outputs = [json.loads(e['output']) for e in data if e.get('type') == 'tool_output' and e.get('tool') == 'vault_audit']
    assert len(outputs) == 2
    assert outputs[0]['audit_id'] == outputs[1]['audit_id']
    assert 'entries' not in outputs[0] and outputs[0]['sample_only']
    assert len(outputs[0]['sample_entries']) <= 3
    assert outputs[1]['report_offset'] == 104 and outputs[1]['entries']
    for output in outputs:
        assert (output['discovered'], output['inspected'], output['property_counts']['true']) == (173, 173, 159)
        assert len(json.dumps(output)) < 3000
    assert answer(data) == GOOD


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact', 'direct_compact'])
async def test_authorized_native_receipt_activates_gate_after_missed_intent(movies, monkeypatch, runtime):
    from src.agent_runtime.structured_audits import audit_requirement
    prompt = 'Explore Library/Movies and examine owned: true in these Markdown files.'
    assert audit_requirement(prompt) is None
    data, _ = await run_agent(movies, monkeypatch, runtime=runtime, tool='vault_audit', prompt=prompt,
        text='\n'.join(f'Movie {i:03}.md' for i in range(173)), synthesis_text=GOOD)
    assert answer(data) == GOOD
    assert [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]['complete']


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_filename_request_has_real_references_and_full_receipt(movies, monkeypatch, runtime):
    data, _ = await run_agent(movies, monkeypatch, runtime=runtime, tool='vault_audit', prompt=NAMES_PROMPT, text='Done.')
    report = answer(data)
    assert 'Movie 000.md' in report and '25 of 159' in report
    assert 'paginated' in report and 'audit receipt' in report
    assert sum(line.startswith('- Movie ') for line in report.splitlines()) <= 25
    assert 'True: 159' in report


@pytest.mark.asyncio
async def test_receipt_pages_cannot_cross_owner_session_or_directory(movies, monkeypatch):
    from src import tool_execution as execution
    from src.turn_contract import bind_turn_contract
    from src.tool_types import ToolBlock
    monkeypatch.setattr(execution, 'owner_is_admin_or_single_user', lambda _: True)
    authority, contract, policy = context(PROMPT, movies)
    async def call(authority, args):
        with bind_turn_contract(contract):
            return (await execution.execute_tool_block(ToolBlock('vault_audit', json.dumps(args)), owner=authority.owner,
                session_id=authority.session_id, workspace=authority.workspace, request_authority=authority,
                tool_policy=policy, security_context=execution.NO_TOOL_SECURITY_CONTEXT))[1]
    initial = await call(authority, {'path':'Library/Movies'})
    audit_id = initial['result']['audit_id']
    for changed in (replace(authority, owner='bob', resource_roots=()), replace(authority, session_id='other')):
        result = await call(changed, {'path':'Library/Movies', 'audit_id':audit_id, 'report_offset':104})
        assert result['exit_code'] != 0
    (movies / 'Other').mkdir()
    result = await call(authority, {'path':'Other', 'audit_id':audit_id, 'report_offset':0})
    assert result['exit_code'] != 0


@pytest.mark.asyncio
async def test_workspace_tool_describes_enforced_containment(movies, monkeypatch):
    from src.agent_tools.filesystem_tools import GetWorkspaceTool
    from src import tool_execution
    monkeypatch.setattr(tool_execution, 'get_active_workspace', lambda: str(movies))
    output = (await GetWorkspaceTool().execute('', {}))['output']
    assert 'enforced containment' in output
    assert 'not sandboxed' not in output.lower()


@pytest.mark.asyncio
async def test_canonical_audit_final_replaces_the_whole_turn(movies, monkeypatch):
    data, _ = await run_agent(movies, monkeypatch, tool='vault_audit', text='\n'.join(f'Movie {i:03}.md' for i in range(173)), prompt=PROMPT)
    final = [event for event in data if event.get('type') == 'final_response'][-1]
    assert final['replacement_scope'] == 'turn' and final['render_owner'] == 'streamed'
    assert 'True: 159' in final['content']


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_audit_synthesis_preserves_existing_execution_claim_guards(movies, monkeypatch, runtime):
    unsupported = GOOD + ' I ran pytest and all 173 tests passed.'
    data, _ = await run_agent(movies, monkeypatch, runtime=runtime, tool='vault_audit', prompt=PROMPT, text=unsupported)
    assert synthesis(data)['source'] == 'deterministic'
    assert synthesis(data)['initial_problem'] == 'unsupported_execution_claims'
    assert 'tests passed' not in answer(data)
    assert 'True: 159' in answer(data)

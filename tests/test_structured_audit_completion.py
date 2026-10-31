"""Audit completion is native evidence, never model arithmetic or listings."""
import json
from dataclasses import replace
import pytest

from tests.test_workspace_inspection_runtime import vault, context
from src.tool_policy import ToolPolicy

PROMPT = "Audit the Markdown notes in Library/Movies and count owned: true, false and missing properties. Don't change anything."


def events(chunks):
    return [json.loads(c[6:]) for c in chunks if c.startswith('data: ') and not c.startswith('data: [DONE]')]


def answer(data):
    finals = [e['content'] for e in data if e.get('type') == 'final_response']
    return finals[-1] if finals else ''.join(e.get('delta', '') for e in data if e.get('thinking') is not True)


async def run_agent(vault, monkeypatch, *, runtime='ordinary', tool='ls', text='121 Markdown files, zero owned, and every file lacks owned.', denied=False, scope='Library/Movies', extra=None, missing_schema=False, prompt=PROMPT, synthesis_text=None, page_offsets=(), call_sequence=None, owner="alice", session_id="audit", provider_failure=False, loop_options=None, authority_override=None, extra_schemas=(), round_limit=3, contract_override=None):
    from src import agent_loop as loop, tool_execution as execution
    monkeypatch.setattr(loop, 'get_setting', lambda key, default=None: default)
    monkeypatch.setattr(loop, 'get_mcp_manager', lambda: None)
    monkeypatch.setattr(loop, '_configured_model_tool_surface', lambda *a, **k: 'compact')
    monkeypatch.setattr(loop, 'estimate_tokens', lambda *a, **k: 10)
    monkeypatch.setattr(loop, 'blocked_tools_for_owner', lambda _: set())
    monkeypatch.setattr(execution, 'owner_is_admin_or_single_user', lambda _: True)
    policy = ToolPolicy(disabled_tools=frozenset({'vault_audit'}) if denied else frozenset())
    authority, contract, policy = context(prompt, vault, policy=policy, owner=owner, session_id=session_id, extra_schemas=extra_schemas)
    if authority_override is not None:
        authority = authority_override
    if contract_override is not None:
        contract = contract_override
    if missing_schema:
        contract = replace(contract, required=contract.required - {'vault_audit'},
            offered=contract.offered - {'vault_audit'}, executable=contract.executable - {'vault_audit'},
            schema_json=tuple(s for s in contract.schema_json if json.loads(s)['function']['name'] != 'vault_audit'))
    if runtime in {'compact', 'direct_compact'}:
        from src.agent_runtime.runtime_selection import COMPACT_PREVIEW_MODE
        contract = replace(contract, selection_mode=COMPACT_PREVIEW_MODE)
    seen = []
    native_args = dict(extra or {}) if tool == 'task_results' else {'path': scope, **(extra or {})}
    if tool == 'glob':
        native_args['pattern'] = '**/*.md'
    def requested_args(index, messages):
        if call_sequence is None:
            return {**native_args, **({'report_offset': page_offsets[index - 2]} if index > 1 else {})}
        result = dict(call_sequence[index - 1])
        if result.get('audit_id') == '$receipt':
            import re
            matches = re.findall(r'"audit_id"\s*:\s*"([a-f0-9]{32})"', str(messages))
            # Python repr may preserve double quotes in tool JSON, or escape them.
            if not matches:
                matches = re.findall(r'audit_id[^a-f0-9]{1,12}([a-f0-9]{32})', str(messages))
            assert matches, 'fixture must copy a real native receipt from tool context'
            result['audit_id'] = matches[-1]
        return result
    tool_rounds = len(call_sequence) if call_sequence is not None else 1 + len(page_offsets)
    async def provider(candidates, messages, **kwargs):
        seen.append({'tools': kwargs.get('tools'), 'messages': list(messages), 'synthesis': bool(kwargs.get('tool_choice_none'))})
        if provider_failure and not kwargs.get('tool_choice_none') and len(seen) > tool_rounds:
            import httpx
            raise httpx.HTTPStatusError('synthetic HTTP 502', request=httpx.Request('POST', 'https://provider.invalid'), response=httpx.Response(502))
        if kwargs.get('tool_choice_none'):
            yield 'data: ' + json.dumps({'delta': text if synthesis_text is None else synthesis_text}) + '\n\n'
        elif len(seen) <= tool_rounds and tool:
            call_args = requested_args(len(seen), messages)
            call_tool = (call_sequence[len(seen) - 1].get('__tool', tool) if call_sequence is not None else tool)
            call_args.pop('__tool', None)
            yield 'data: ' + json.dumps({'type':'tool_calls','calls':[{'id':'qwen-call', 'name':call_tool, 'arguments':json.dumps(call_args)}]}) + '\n\n'
        else:
            yield 'data: ' + json.dumps({'delta':text}) + '\n\n'
        yield 'data: [DONE]\n\n'
    if runtime == 'ordinary':
        monkeypatch.setattr(loop, 'stream_llm_with_fallback', provider)
    else:
        from src import clean_agent_preview as preview
        monkeypatch.setattr(loop, 'stream_llm_with_fallback', provider)
        class Response:
            def __init__(self, request): self.request = request
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def raise_for_status(self):
                if provider_failure and len(seen) >= tool_rounds:
                    import httpx
                    raise httpx.HTTPStatusError('synthetic HTTP 502', request=httpx.Request('POST', 'https://provider.invalid'), response=httpx.Response(502))
            async def aiter_lines(self):
                seen.append(self.request)
                call_args = requested_args(len(seen), self.request['messages']) if len(seen) <= tool_rounds else native_args
                call_tool = call_args.pop('__tool', tool)
                delta = {'tool_calls':[{'index':0, 'id':'qwen-call-' + str(len(seen)), 'type':'function','function':{'name':call_tool,'arguments':json.dumps(call_args)}}]} if len(seen) <= tool_rounds and tool else {'content':text}
                yield 'data: ' + json.dumps({'choices':[{'delta':delta}]})
                yield 'data: [DONE]'
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def stream(self, *args, **kwargs): return Response(kwargs['json'])
        monkeypatch.setattr(preview.httpx, 'AsyncClient', Client)
    if runtime == 'direct_compact':
        stream = preview.stream_preview(endpoint_url='https://provider.invalid/v1', model='qwen3.5-4b',
            headers={}, messages=[{'role':'user','content':prompt}], owner=owner, session_id=session_id, workspace=str(vault),
            turn_contract=contract, request_authority=authority, tool_policy=policy, disabled_tools=set(policy.disabled_tools), max_rounds=round_limit, **(loop_options or {}))
    else:
        stream = loop.stream_agent_loop('https://provider.invalid/v1', 'qwen3.5-4b',
            [{'role':'user','content':prompt}], owner=owner, session_id=session_id, workspace=str(vault),
            turn_contract=contract, request_authority=authority, tool_policy=policy, disabled_tools=set(policy.disabled_tools),
            relevant_tools=set(contract.offered), forced_tools=set(contract.offered), max_rounds=round_limit,
            suppress_skills=True, fallbacks=[], fallback_on_empty=False, **(loop_options or {}))
    chunks = [c async for c in stream]
    return events(chunks), seen


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_listing_and_fabricated_counts_never_verify_audit(vault, monkeypatch, runtime):
    data, _ = await run_agent(vault, monkeypatch, runtime=runtime, denied=True)
    decisions = [e['data'] for e in data if e.get('type') == 'completion_decision']
    assert decisions and decisions[-1]['can_complete'] is False
    assert '121 Markdown files' not in answer(data)
    assert 'incomplete' in answer(data).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_coincidentally_correct_counts_without_native_audit_are_not_verified(vault, monkeypatch, runtime):
    data, _ = await run_agent(vault, monkeypatch, runtime=runtime, denied=True,
        text='All 208 notes: 26 owned true, 26 false, 26 missing and 130 invalid. Audit complete.')
    assert [e['data'] for e in data if e.get('type') == 'completion_decision'][-1]['can_complete'] is False
    assert 'All 208 notes' not in answer(data)
    assert [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]['audit_executed'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
@pytest.mark.parametrize('text', ['Audit done.', '121 notes, zero owned, all missing.'])
async def test_model_selected_audit_is_authoritative_even_if_summary_contradicts(vault, monkeypatch, runtime, text):
    data, requests = await run_agent(vault, monkeypatch, runtime=runtime, tool='vault_audit', text=text)
    evidence = [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]
    assert evidence['complete'] and evidence['audit_executed']
    assert evidence['coverage'] == {'discovered':208, 'inspected':208}
    assert evidence['property_counts'] == {'true':26, 'false':26, 'missing':26, 'invalid':78, 'malformed':52, 'unreadable':0}
    assert '121 notes' not in answer(data)
    assert 'True: 26' in answer(data) and 'successfully inspected: 208' in answer(data)
    assert 'not inconsistencies' in answer(data)
    assert [e['data'] for e in data if e.get('type') == 'completion_decision'][-1]['status'] == 'verified'
    assert 'vault_audit' in {s['function']['name'] for s in requests[0]['tools']}
    assert any('property_counts' in str(request['messages']) and '208' in str(request['messages']) for request in requests[1:])
    assert not any(e.get('type') == 'audit_recovery' for e in data)


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
@pytest.mark.parametrize('tool', ['ls', 'glob'])
async def test_truncated_listing_recovers_with_one_authorized_audit(vault, monkeypatch, runtime, tool):
    from src.agent_tools import filesystem_tools
    from src.agent_tools import vault_batch_tools
    monkeypatch.setattr(filesystem_tools, '_CODENAV_MAX_HITS', 20)
    calls = []
    run = vault_batch_tools.run_audit
    def counted(*args, **kwargs):
        calls.append(args)
        return run(*args, **kwargs)
    monkeypatch.setattr(vault_batch_tools, 'run_audit', counted)
    before = {p:(p.read_bytes(), p.stat().st_mtime_ns) for p in vault.rglob('*.md')}
    data, requests = await run_agent(vault, monkeypatch, runtime=runtime, tool=tool)
    assert len(calls) == 1
    evidence = [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]
    listing = evidence['listings'][-1]
    assert listing['truncated'] and listing['files_inspected'] == 0
    assert listing['coverage_complete'] is False
    assert evidence['complete'] and evidence['coverage']['inspected'] == 208
    assert len([e for e in data if e.get('type') == 'audit_recovery']) == 1
    assert 'True: 26' in answer(data) and '121 Markdown' not in answer(data)
    assert {p:(p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before
    assert len(requests) <= 3


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_partial_native_audit_never_claims_full_collection(vault, monkeypatch, runtime):
    (vault / 'Library/Movies/unreadable.md').write_bytes(b'\xff\xff')
    data, _ = await run_agent(vault, monkeypatch, runtime=runtime, tool='vault_audit', text='All 209 notes verified; no issues.')
    evidence = [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]
    assert evidence['audit_executed'] and not evidence['complete']
    assert evidence['property_counts']['unreadable'] == 1
    assert evidence['coverage'] == {'discovered':209, 'inspected':208}
    assert [e['data'] for e in data if e.get('type') == 'completion_decision'][-1]['can_complete'] is False
    assert 'partial counts' in answer(data) and 'All 209 notes verified' not in answer(data)


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_failed_audit_is_not_retried_or_replaced_by_listing_counts(vault, monkeypatch, runtime):
    from src.agent_tools import TOOL_HANDLERS
    calls = []
    async def failed(*args, **kwargs):
        calls.append(args)
        return {'exit_code':1, 'failure_kind':'missing_optional_dependency', 'executed':False, 'error':'YAML parser unavailable'}
    monkeypatch.setitem(TOOL_HANDLERS, 'vault_audit', failed)
    data, _ = await run_agent(vault, monkeypatch, runtime=runtime, tool='vault_audit')
    assert len(calls) == 1
    assert [e['data'] for e in data if e.get('type') == 'completion_decision'][-1]['can_complete'] is False
    assert 'incomplete' in answer(data).lower()
    assert '121 Markdown' not in answer(data)


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
@pytest.mark.parametrize('extra', [{'glob':'Movie 0*.md'}, {'property':'watched'}])
async def test_wrong_selection_or_property_cannot_attest_requested_audit(vault, monkeypatch, runtime, extra):
    data, _ = await run_agent(vault, monkeypatch, runtime=runtime, tool='vault_audit', extra=extra)
    assert [e['data'] for e in data if e.get('type') == 'completion_decision'][-1]['can_complete'] is True
    assert any(e.get('type') == 'tool_output' and e.get('exit_code') == 1 for e in data)
    assert sum(e.get('type') == 'audit_recovery' for e in data) == 1
    assert 'True: 26' in answer(data)


def test_native_report_partition_and_validation(vault):
    from src.agent_tools.vault_batch_tools import run_audit, audit_report_page
    from src.agent_runtime.structured_audits import validate_report
    r = run_audit(vault, {})
    assert validate_report(r)
    assert r['complete'] and sum(r['property_counts'].values()) == r['discovered'] == 208
    assert r['counts']['invalid'] == r['property_counts']['invalid'] + r['property_counts']['malformed']
    assert len(r['errors']) == 130
    assert audit_report_page(r)['errors_truncated'] is True
    assert not validate_report({**r, 'property_counts':{**r['property_counts'], 'invalid':130, 'malformed':0}})
    assert not validate_report({**r, 'errors':[]})
    assert not validate_report({**r, 'inspected':121})
    assert not validate_report({**r, 'complete':False})
    assert not validate_report({**r, 'property_counts':{**r['property_counts'],'missing':208}})


@pytest.mark.parametrize('prompt, prop', [
    ('Count Markdown notes with owned: true in Library/Movies.', 'owned'),
    ('Compare the YAML property watched across my movie collection.', 'watched'),
    ('Which entries have I marked as owned in my movie library?', 'owned'),
])
def test_natural_property_audit_is_selected_without_tool_name(vault, prompt, prop):
    from src.agent_runtime.structured_audits import audit_requirement
    authority, contract, _ = context(prompt, vault)
    assert audit_requirement(prompt).property == prop
    assert contract.permits('vault_audit')
    from src.agent_runtime.authority import ExactOperation
    assert authority.permits(ExactOperation.normalize('vault_audit', '{}'))


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_unadvertised_audit_cannot_be_substituted_from_listing(vault, monkeypatch, runtime):
    data, requests = await run_agent(vault, monkeypatch, runtime=runtime, missing_schema=True)
    assert 'vault_audit' not in {s['function']['name'] for s in requests[0]['tools']}
    assert not [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]['audit_executed']
    assert [e['data'] for e in data if e.get('type') == 'completion_decision'][-1]['can_complete'] is False
    assert 'incomplete' in answer(data).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_success_shaped_output_without_native_producer_is_not_attestation(vault, monkeypatch, runtime):
    from src.agent_tools import TOOL_HANDLERS
    from src.agent_tools.vault_batch_tools import run_audit
    report = run_audit(vault / 'Library/Movies', {})
    async def fabricated(*args, **kwargs):
        return {'exit_code':0, 'result':report, 'output':json.dumps(report)}
    monkeypatch.setitem(TOOL_HANDLERS, 'vault_audit', fabricated)
    data, _ = await run_agent(vault, monkeypatch, runtime=runtime, tool='vault_audit', text='The audit succeeded.')
    assert [e['data'] for e in data if e.get('type') == 'completion_decision'][-1]['can_complete'] is False
    assert 'no validated producer result' in answer(data)


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_named_collection_can_be_located_by_listing_but_still_needs_audit(vault, monkeypatch, runtime):
    data, _ = await run_agent(vault, monkeypatch, runtime=runtime,
        prompt='Look through my movie library and tell me which notes are marked owned.')
    evidence = [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]
    assert evidence['complete'] and evidence['scope'] == '/workspace/Library/Movies'
    assert evidence['listings'][-1]['files_inspected'] == 0
    assert 'True: 26' in answer(data)


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_unresolved_collection_does_not_verify_a_whole_vault_subset(vault, monkeypatch, runtime):
    data, _ = await run_agent(vault, monkeypatch, runtime=runtime, scope='.',
        prompt='Look through my movie library and tell me which notes are marked owned.')
    assert [e['data'] for e in data if e.get('type') == 'completion_decision'][-1]['can_complete'] is False
    assert 'Which directory' in answer(data)


@pytest.mark.parametrize('prompt', [
    'How many apples are in this arithmetic example?',
    'Write a Python script to audit owned: true in Markdown files.',
    'Tell me the number 121.',
])
def test_unrelated_numbers_and_script_requests_are_not_audit_obligations(prompt):
    from src.agent_runtime.structured_audits import audit_requirement
    assert audit_requirement(prompt) is None


@pytest.mark.parametrize('prompt', [
    'Inspect the owned property across Markdown files in Library/Movies.',
    'Count the property owned in my movie collection.',
])
def test_property_name_is_not_a_preposition(prompt):
    from src.agent_runtime.structured_audits import audit_requirement
    assert audit_requirement(prompt).property == 'owned'


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_long_title_listing_is_byte_truncated_and_never_property_evidence(vault, monkeypatch, runtime):
    for index, file in enumerate(sorted((vault / 'Library/Movies').glob('*.md'))):
        file.rename(file.with_name(f'Movie {index:03} - ' + 'A long descriptive movie title ' * 2 + '.md'))
    data, requests = await run_agent(vault, monkeypatch, runtime=runtime)
    evidence = [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]
    listing = evidence['listings'][-1]
    assert listing['entries_discovered'] == 208
    assert listing['truncated'] and listing['entries_returned'] is None
    assert listing['files_inspected'] == 0
    assert evidence['complete'] and evidence['coverage']['inspected'] == 208
    assert 'True: 26' in answer(data)
    assert any('files_inspected' in str(request['messages']) for request in requests[1:])


@pytest.mark.asyncio
async def test_compact_saved_trace_contains_only_grounded_terminal_report(vault, monkeypatch):
    data, _ = await run_agent(vault, monkeypatch, runtime='compact', tool='vault_audit', text='121 notes; all missing owned.')
    metadata = [e['data'] for e in data if e.get('type') == 'metrics'][-1]
    saved_answers = [r.get('content') for r in metadata['clean_v3_turn'] if r.get('role') == 'assistant']
    assert all(not text or '121 notes' not in text for text in saved_answers)
    assert saved_answers[-1] == answer(data)
    assert 'True: 26' in metadata['round_texts'][-1]


@pytest.mark.asyncio
async def test_untrusted_message_cannot_create_audit_obligation(vault, monkeypatch):
    from src.agent_runtime.completion import with_completion_gate
    from src.agent_runtime.structured_audits import active_audit
    @with_completion_gate
    async def stream(messages, workspace):
        assert active_audit() is None
        yield 'data: ' + json.dumps({'delta':'A simple greeting.'}) + '\n\n'
        yield 'data: [DONE]\n\n'
    data = events([c async for c in stream([
        {'role':'user', 'content':'Hello'},
        {'role':'user', 'content':PROMPT, 'metadata':{'trusted':False, 'tool_gate_untrusted':True}}], workspace=str(vault))])
    assert not any(e.get('type') == 'audit_evidence' for e in data)
    assert 'A simple greeting.' in answer(data)


def test_negated_audit_does_not_authorize_automatic_inspection():
    from src.agent_runtime.structured_audits import audit_requirement
    assert audit_requirement("Don't audit owned: true in Library/Movies. Explain YAML instead.") is None
    assert audit_requirement('No changes, audit owned: true in Library/Movies.').property == 'owned'


@pytest.mark.asyncio
async def test_late_native_audit_is_retained_in_metrics_and_followup_protocol(vault, monkeypatch):
    data, _ = await run_agent(vault, monkeypatch, runtime='compact', tool='ls')
    metadata = [e['data'] for e in data if e.get('type') == 'metrics'][-1]
    assert any(e.get('tool') == 'vault_audit' and e.get('source') == 'structured_audit_contract' for e in metadata['tool_events'])
    calls = [call for row in metadata['clean_v3_turn'] for call in row.get('tool_calls', []) if call['function']['name'] == 'vault_audit']
    assert len(calls) == 1
    results = [row for row in metadata['clean_v3_turn'] if row.get('tool_call_id') == calls[0]['id']]
    assert len(results) == 1 and 'property_counts' in results[0]['content']
    assert metadata['clean_v3_turn'][-1]['content'] == answer(data)


@pytest.mark.asyncio
@pytest.mark.parametrize('denied', [False, True])
async def test_direct_compact_invocations_use_the_same_audit_gate(vault, monkeypatch, denied):
    data, _ = await run_agent(vault, monkeypatch, runtime='direct_compact', denied=denied)
    decision = [e['data'] for e in data if e.get('type') == 'completion_decision'][-1]
    assert decision['can_complete'] is (not denied)
    assert '121 Markdown files' not in answer(data)
    if denied:
        assert 'incomplete' in answer(data).lower()
    else:
        assert 'True: 26' in answer(data)

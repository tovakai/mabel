"""Human permission, model-selected discovery, and exact resumed execution."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from src.agent_runtime.authority import RequestAuthority, OperationGrant, ExactOperation
from src.tool_approvals import ToolApprovalStore, bind_approval_history, scoped_exact_approval
from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action
from src.tool_approval_scopes import ToolApprovalScope
from src.tool_policy import ToolPolicy
from tests.test_structured_audit_completion import run_agent, answer
from tests.test_remote_resource_identity import manager, connect


@pytest.fixture
def approvals(monkeypatch):
    from src import tool_approvals, agent_loop
    from routes import chat_routes
    store = ToolApprovalStore()
    monkeypatch.setattr(tool_approvals, 'tool_approval_store', store)
    monkeypatch.setattr(agent_loop, 'tool_approval_store', store)
    monkeypatch.setattr(chat_routes, 'tool_approval_store', store)
    return store


def authority(root, *tools):
    return RequestAuthority('interactive-task', 'alice', 'audit', str(root), tuple(OperationGrant(t) for t in tools))


def cards(data):
    return [e['ask_user'] for e in data if e.get('type') == 'tool_output' and e.get('ask_user')]


MCP_SCHEMA = {'type':'function', 'function':{'name':'mcp__alpha__read', 'description':'Read a record from the configured service',
    'parameters':{'type':'object', 'properties':{'record':{'type':'string'}}}}}


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_discover_custom_mcp_approve_once_resume_and_prevent_replay(tmp_path, monkeypatch, approvals, manager, runtime):
    remote = connect(manager, tools=('read',))
    base = authority(tmp_path)
    prompt = 'Bitte prüfe den Eintrag im verbundenen Dienst.'
    data, requests = await run_agent(tmp_path, monkeypatch, runtime=runtime, prompt=prompt,
        authority_override=base, extra_schemas=[MCP_SCHEMA], loop_options={'authority_gap_approvals':True},
        call_sequence=[{'__tool':'discover_tools', 'names':['mcp__alpha__read']},
                       {'__tool':'mcp__alpha__read', 'record':'one'}])
    remote.call_tool.assert_not_awaited()
    assert cards(data), json.dumps(data, ensure_ascii=False)
    pending = approvals.peek(cards(data)[-1]['approval_id'])
    assert pending and pending.backend_operation and pending.authority_gap
    public = pending.public_payload()['action']
    assert public['resources'] == [{'path':'https://example.test', 'role':'mcp:alpha'}]
    assert 'SECRET' not in json.dumps(public)
    assert not base.permits(ExactOperation.normalize('mcp__alpha__read', '{"record":"one"}'))
    # Disclosure is metadata only; a later round receives the selected schema.
    assert any('mcp__alpha__read' in str(r.get('tools')) for r in requests[1:])
    approved = approvals.consume(pending.approval_id, decision='approve_once', owner='alice', session_id='audit')
    resumed, _ = await run_agent(tmp_path, monkeypatch, runtime=runtime, tool=None, prompt=prompt,
        text='The verified record was retrieved.', authority_override=pending.request_authority,
        extra_schemas=[MCP_SCHEMA], loop_options={'authority_gap_approvals':True, 'exact_approval':approved})
    remote.call_tool.assert_awaited_once_with('read', {'record':'one'})
    assert any(e.get('type') == 'tool_output' and e.get('approved') for e in resumed)
    assert approvals.consume(pending.approval_id, decision='approve_once', owner='alice', session_id='audit') is None
    assert approved._claimed


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary','compact'])
@pytest.mark.parametrize('prompt', ['Show my meetings.', 'Zeig mir meine Termine.', 'Visa mina möten.',
                                   'Montre mes rendez-vous.', 'Mostre meus compromissos.', 'Δείξε τις συναντήσεις μου.'])
async def test_model_discovers_calendar_without_english_routing_or_new_grants(tmp_path, monkeypatch, approvals, runtime, prompt):
    base = authority(tmp_path)
    data, requests = await run_agent(tmp_path, monkeypatch, runtime=runtime, prompt=prompt,
        authority_override=base, loop_options={'authority_gap_approvals':True},
        call_sequence=[{'__tool':'discover_tools', 'names':['manage_calendar']},
                       {'__tool':'manage_calendar', 'action':'list_events'}])
    assert cards(data), json.dumps(data, ensure_ascii=False)
    pending = approvals.peek(cards(data)[-1]['approval_id'])
    assert pending and pending.backend_operation is not None
    assert pending.tool_name == 'manage_calendar'
    assert not base.permits(ExactOperation.normalize('manage_calendar', '{"action":"list_events"}'))
    assert len(requests) == 2
    assert set(pending.public_payload()['action']['effects'])


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary','compact'])
async def test_user_denial_is_seen_by_model_and_alternative_executes(tmp_path, monkeypatch, approvals, runtime):
    from src import tool_execution
    base = authority(tmp_path, 'ls', 'get_workspace')
    pending = approvals.create(owner='alice', session_id='audit', origin_run_id='run', tool_name='bash', content='pwd',
        workspace=str(tmp_path), request_authority=base, continuation_query='Inspect the selected workspace.',
        external_untrusted_context_seen=False, capabilities=capabilities_for_action('bash','pwd'), authority_gap=True)
    approvals.consume(pending.approval_id, decision='deny', owner='alice', session_id='audit')
    data, requests = await run_agent(tmp_path, monkeypatch, runtime=runtime, prompt='Inspect the selected workspace.',
        tool='ls', scope='.', authority_override=base, text='The native listing succeeded.',
        loop_options={'authority_gap_approvals':True, 'rejected_tool_approval':pending})
    assert any('user_denied' in str(r['messages']) for r in requests)
    assert any(e.get('tool') == 'ls' and e.get('exit_code') == 0 for e in data)
    assert not cards(data)
    _, denied = await tool_execution.execute_tool_block(SimpleNamespace(tool_type='bash', content='pwd'), owner='alice',
        session_id='audit', workspace=str(tmp_path), request_authority=base, security_context=ToolRunSecurityContext())
    assert denied['failure_kind'] == 'user_denied'


def pending_read(store, root, **kwargs):
    base = authority(root)
    (root / 'note.md').write_text('evidence')
    return store.create(owner='alice', session_id='audit', origin_run_id='run', tool_name='read_file', content='{"path":"note.md"}',
        workspace=str(root), request_authority=base, external_untrusted_context_seen=True,
        capabilities=capabilities_for_action('read_file','{"path":"note.md"}'), authority_gap=True, **kwargs)


@pytest.mark.parametrize('scope', ['approve_task','approve'])
def test_scoped_consent_reuses_only_exact_operation_and_sealed_resources(tmp_path, approvals, scope):
    pending = pending_read(approvals, tmp_path)
    grant = approvals.consume(pending.approval_id, decision=scope, owner='alice', session_id='audit')
    assert not grant.allow_remaining_actions
    proof = approvals.scope_proof(pending.approval_id)
    history = SimpleNamespace(history=[{'role':'assistant', 'metadata':{'tool_events':[{'ask_user':{
        'approval_id':pending.approval_id, 'resolved':scope, '_scope_grant':proof}}]}}])
    base = pending.request_authority
    current = replace(base, request_id='next-turn') if scope == 'approve' else base
    with bind_approval_history(history, True):
        exact = scoped_exact_approval(current, 'read_file', pending.content, security_context=ToolRunSecurityContext(external_untrusted_context_seen=True))
        assert exact and exact.matches(owner='alice',session_id='audit',workspace=str(tmp_path),tool_name='read_file',content=pending.content)
        assert scoped_exact_approval(current,'read_file','{"path":"other.md"}',security_context=ToolRunSecurityContext()) is None
        assert scoped_exact_approval(RequestAuthority.empty(owner='bob',session_id='audit',workspace=str(tmp_path)),'read_file',pending.content,security_context=ToolRunSecurityContext()) is None
        assert scoped_exact_approval(current.restrict(ToolPolicy(disabled_tools=frozenset({'read_file'}))), 'read_file',pending.content,security_context=ToolRunSecurityContext()) is None
        # Changing the target identity cannot inherit an old consent.
        (tmp_path / 'note.md').rename(tmp_path / 'previous.md')
        (tmp_path / 'note.md').write_text('replacement')
        assert scoped_exact_approval(current,'read_file',pending.content,security_context=ToolRunSecurityContext()) is None


def test_chat_scope_requires_original_server_card_and_expires(tmp_path, approvals, monkeypatch):
    from src import tool_approvals
    pending = pending_read(approvals, tmp_path)
    approvals.consume(pending.approval_id, decision='approve', owner='alice', session_id='audit')
    base = replace(pending.request_authority, request_id='next')
    with bind_approval_history(SimpleNamespace(history=[]), True):
        assert scoped_exact_approval(base,'read_file',pending.content,security_context=ToolRunSecurityContext()) is None
    now = tool_approvals.time.time()
    monkeypatch.setattr(tool_approvals.time, 'time', lambda:now + 601)
    with bind_approval_history(None, True):
        assert scoped_exact_approval(base,'read_file',pending.content,security_context=ToolRunSecurityContext()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary','compact'])
async def test_discovery_and_approval_cannot_override_disabled_tool(tmp_path, monkeypatch, approvals, runtime):
    base = authority(tmp_path).restrict(ToolPolicy(disabled_tools=frozenset({'bash'})))
    data, _ = await run_agent(tmp_path, monkeypatch, runtime=runtime, prompt='Inspect this workspace.',
        authority_override=base, loop_options={'authority_gap_approvals':True},
        call_sequence=[{'__tool':'discover_tools','names':['bash']}, {'__tool':'bash','command':'pwd'}])
    assert not cards(data)
    assert not any(e.get('tool') == 'bash' and e.get('exit_code') == 0 for e in data)


@pytest.mark.parametrize('prompt', ['Show my meetings.', 'Zeig mir meine Termine.', 'Visa mina möten.',
                                   'Montre mes rendez-vous.', 'Mostre meus compromissos.', 'Δείξε τις συναντήσεις μου.'])
def test_interactive_review_automatic_grants_come_from_ui_scopes_not_language(tmp_path, prompt):
    from src.agent_runtime.authority import create_request_authority
    profiles = [create_request_authority(text, owner='alice', session_id='audit', workspace=str(tmp_path),
        capabilities={'workspace_read','search_browser'}, interactive_review=True)
        for text in ('Show my meetings.', prompt)]
    assert profiles[0].grants == profiles[1].grants
    assert profiles[1].permits(ExactOperation.normalize('ls','{"path":"."}'))
    assert profiles[1].permits(ExactOperation.normalize('web_search','news'))
    # Named owners always receive personal pure-read grants (calendar list,
    # notes search, …) independent of request language. UI scopes still do
    # not mint writes, shell, or python.
    assert profiles[1].permits(ExactOperation.normalize(
        'manage_calendar', '{"action":"list_events"}'))
    assert profiles[1].permits(ExactOperation.normalize(
        'manage_notes', '{"action":"search","query":"x"}'))
    for name, content in (
        ('manage_calendar', '{"action":"create_event","title":"x"}'),
        ('write_file', '{"path":"a","content":"b"}'),
        ('bash', 'pwd'),
        ('python', 'print(1)'),
    ):
        assert not profiles[1].permits(ExactOperation.normalize(name, content))


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary','compact'])
async def test_containment_unavailable_does_not_offer_ineffective_approval_and_native_listing_survives(tmp_path, monkeypatch, approvals, runtime):
    from src import containment
    monkeypatch.setattr(containment, 'probe', lambda spec:containment.ContainmentProbe(
        mechanism='none', enforced=frozenset(), degraded=(), unenforced_required=('filesystem','process_tree'),
        mode=containment.MODE_ENFORCING))
    (tmp_path / 'active.md').write_text('safe native data')
    base = authority(tmp_path, 'ls')
    data, _ = await run_agent(tmp_path, monkeypatch, runtime=runtime, prompt='Inspect this workspace.',
        authority_override=base, loop_options={'authority_gap_approvals':True}, round_limit=4,
        call_sequence=[{'__tool':'discover_tools','names':['bash']},
                       {'__tool':'bash','command':'pwd'}, {'__tool':'ls','path':'.'}])
    assert not cards(data)
    assert any(e.get('tool') == 'bash' and e.get('exit_code') != 0 for e in data)
    assert any(e.get('tool') == 'ls' and e.get('exit_code') == 0 for e in data)
    assert (tmp_path / 'active.md').read_text() == 'safe native data'


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['approve_task','approve'])
async def test_scoped_read_permission_executes_through_dispatch_without_widening(tmp_path, monkeypatch, approvals, scope):
    from src import tool_execution
    pending = pending_read(approvals, tmp_path)
    approvals.consume(pending.approval_id, decision=scope, owner='alice', session_id='audit')
    proof = approvals.scope_proof(pending.approval_id)
    session = SimpleNamespace(history=[{'role':'assistant','metadata':{'tool_events':[{'ask_user':{
        'approval_id':pending.approval_id, 'resolved':scope, '_scope_grant':proof}}]}}])
    current = pending.request_authority if scope == 'approve_task' else replace(pending.request_authority, request_id='next-turn')
    monkeypatch.setattr(tool_execution,'owner_is_admin_or_single_user',lambda _:True)
    with bind_approval_history(session, True):
        for _ in range(2):
            _, result = await tool_execution.execute_tool_block(SimpleNamespace(tool_type='read_file',content=pending.content),
                owner='alice',session_id='audit',workspace=str(tmp_path),request_authority=current,
                security_context=ToolRunSecurityContext(external_untrusted_context_seen=True))
            assert result['exit_code'] == 0, result
            assert 'evidence' in result['output']
        _, denied = await tool_execution.execute_tool_block(SimpleNamespace(tool_type='read_file',content='{"path":"other.md"}'),
            owner='alice',session_id='audit',workspace=str(tmp_path),request_authority=current,
            security_context=ToolRunSecurityContext(external_untrusted_context_seen=True))
        assert denied['failure_kind'] == 'request_authority_denied'


def test_pending_approval_survives_disconnect_and_invalid_decision_does_not_destroy_it(tmp_path, approvals):
    pending = pending_read(approvals,tmp_path)
    assert approvals.peek(pending.approval_id) is pending
    assert approvals.consume(pending.approval_id,decision='approve_once',owner='bob',session_id='audit') is None
    assert approvals.peek(pending.approval_id) is pending
    assert approvals.consume(pending.approval_id,decision='nonsense',owner='alice',session_id='audit') is None
    assert approvals.peek(pending.approval_id) is pending
    grant = approvals.consume(pending.approval_id,decision='approve_once',owner='alice',session_id='audit')
    assert grant and grant.scope is ToolApprovalScope.SINGLE_ACTION


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary','compact'])
async def test_native_discovery_does_not_leak_into_other_turns(tmp_path, monkeypatch, approvals, runtime):
    from tests.test_workspace_inspection_runtime import context
    from src.agent_runtime.tool_catalog import active_catalog
    base = authority(tmp_path)
    data, requests = await run_agent(tmp_path,monkeypatch,runtime=runtime,tool='discover_tools',prompt='Help with my appointments.',
        authority_override=base, loop_options={'authority_gap_approvals':True},
        call_sequence=[{'names':['manage_calendar']}],text='The calendar tool is available for review.')
    assert active_catalog() is None
    assert requests and any(e.get('tool') == 'discover_tools' and e.get('exit_code') == 0 for e in data)
    _, contract, _ = context('Unrelated new task.', tmp_path)
    assert 'manage_calendar' not in {s['function']['name'] for s in contract.schemas()}


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_registered_http_route_discovers_reviews_and_resumes_mcp(
        tmp_path, monkeypatch, approvals, manager, runtime):
    """Exercise the registered endpoint, contract selection and real dispatcher."""
    from routes import chat_routes
    from src import agent_loop as loop, tool_execution, clean_agent_preview as preview
    from src.agent_runtime import context_resolution
    from tests.test_foreground_model_routing import _chat_stream_endpoint, _RouteRequest

    remote = connect(manager, tools=('read',))
    manager._tools['alpha'][0].update(description='Read the configured service record',
        input_schema=MCP_SCHEMA['function']['parameters'])
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, 'agent', captured, session_model='qwen3.5-4b')
    monkeypatch.setattr(chat_routes, 'stream_agent_loop', loop.stream_agent_loop)
    from src import tool_utils
    monkeypatch.setattr(tool_utils, 'get_mcp_manager', lambda:manager)
    monkeypatch.setattr(chat_routes, 'uses_compact_preview_runtime', lambda **kwargs:runtime == 'compact')
    monkeypatch.setattr(chat_routes, 'coerce_message_and_session',
        lambda body, message, session, *a, **k:(message, session))
    monkeypatch.setattr(loop, 'get_setting', lambda key, default=None:default)
    monkeypatch.setattr(loop, 'get_mcp_manager', lambda:manager)
    monkeypatch.setattr(loop, '_configured_model_tool_surface', lambda *a, **k:'compact')
    monkeypatch.setattr(loop, 'estimate_tokens', lambda *a, **k:10)
    monkeypatch.setattr(loop, 'blocked_tools_for_owner', lambda owner:set())
    monkeypatch.setattr(tool_execution, 'owner_is_admin_or_single_user', lambda owner:True)
    from src import tool_security
    monkeypatch.setattr(tool_security, 'blocked_tools_for_owner', lambda owner:set())
    monkeypatch.setattr(tool_security, 'owner_is_admin_or_single_user', lambda owner:True)

    async def resolution(*a, **k):
        return context_resolution.ContextResolution(8192, context_resolution.ContextEvidence.OPERATOR_DECLARED,
            'test provider')
    monkeypatch.setattr(context_resolution, 'resolve_effective_context', resolution)
    observed = []

    def model_response(messages, tools):
        observed.append({'messages':messages, 'tools':tools})
        text = str(messages)
        if 'remote result' in text:
            return None, 'The reviewed record was retrieved from the configured service.'
        if '"selected"' in text or "'selected'" in text:
            return ('mcp__alpha__read', {'record':'one'}), None
        return ('discover_tools', {'names':['mcp__alpha__read']}), None

    async def provider(candidates, messages, **kwargs):
        call, text = model_response(messages, kwargs.get('tools'))
        event = {'delta':text} if text else {'type':'tool_calls', 'calls':[{
            'id':'http-qwen', 'name':call[0], 'arguments':json.dumps(call[1])}]}
        yield 'data: ' + json.dumps(event) + '\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(loop, 'stream_llm_with_fallback', provider)

    class Response:
        def __init__(self, body): self.body = body
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def raise_for_status(self): pass
        async def aiter_lines(self):
            call, text = model_response(self.body['messages'], self.body.get('tools'))
            delta = {'content':text} if text else {'tool_calls':[{
                'index':0, 'id':'http-qwen', 'type':'function',
                'function':{'name':call[0], 'arguments':json.dumps(call[1])}}]}
            yield 'data: ' + json.dumps({'choices':[{'delta':delta}]})
            yield 'data: [DONE]'
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def stream(self, *args, **kwargs): return Response(kwargs['json'])
    monkeypatch.setattr(preview.httpx, 'AsyncClient', Client)

    async def invoke(**updates):
        request = _RouteRequest('agent')
        request._form.update(message='Bitte prüfe den Eintrag im verbundenen Dienst.',
            compare_mode='false', workspace=str(tmp_path), **updates)
        response = await endpoint(request)
        chunks = [c async for c in response.body_iterator]
        from tests.test_structured_audit_completion import events
        return events(chunks)

    first = await invoke()
    assert any('clean_v3_turn' in e.get('data', {}) for e in first if e.get('type') == 'metrics') == (runtime == 'compact')
    remote.call_tool.assert_not_awaited()
    assert cards(first), json.dumps(first, ensure_ascii=False)[-12000:]
    pending = approvals.peek(cards(first)[-1]['approval_id'])
    assert pending and pending.authority_gap
    assert pending.backend_operation
    assert any('mcp__alpha__read' in str(r['tools']) for r in observed[1:])
    second = await invoke(tool_approval_id=pending.approval_id, tool_approval_decision='approve_once')
    remote.call_tool.assert_awaited_once_with('read', {'record':'one'})
    assert any(e.get('type') == 'tool_output' and e.get('approved') for e in second), second
    assert approvals.peek(pending.approval_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_missing_lexical_family_can_discover_an_authorized_alternative(tmp_path, monkeypatch, approvals, runtime):
    from tests.test_workspace_inspection_runtime import context
    prompt = 'Inspect the selected workspace.'
    _, contract, _ = context(prompt, tmp_path)
    contract = replace(contract, capabilities=frozenset({'calendar'}), required=frozenset(),
        offered=frozenset(), schema_json=(), unavailable=frozenset({'manage_calendar'}))
    data, requests = await run_agent(tmp_path, monkeypatch, runtime=runtime, prompt=prompt,
        authority_override=authority(tmp_path, 'ls', 'get_workspace'), contract_override=contract,
        loop_options={'authority_gap_approvals':True}, text='The native workspace listing succeeded.',
        call_sequence=[{'__tool':'discover_tools','names':['ls']}, {'__tool':'ls','path':'.'}])
    assert requests, data
    assert any(e.get('tool') == 'ls' and e.get('exit_code') == 0 for e in data), data


def test_server_derived_editor_contract_preserves_discovery_without_granting_tools(tmp_path):
    from tests.test_workspace_inspection_runtime import context
    from src.agent_runtime.authority import bind_request_authority
    from src.agent_runtime.tool_catalog import active_catalog, bind_catalog, derived_catalog
    _, original, _ = context('Inspect this workspace.', tmp_path)
    derived = replace(original, required=frozenset(), offered=frozenset(), schema_json=())
    base = authority(tmp_path)
    with bind_request_authority(base), bind_catalog(original, True):
        catalog = active_catalog(original)
        catalog.selected.add('manage_calendar')
        assert not derived.permits('manage_calendar')
        derived_catalog(original, derived)
        assert derived.permits('manage_calendar') and original.permits('manage_calendar')
        assert not base.permits(ExactOperation.normalize('manage_calendar', '{"action":"list_events"}'))
    assert active_catalog() is None


def test_interactive_browser_mutations_require_review_in_every_language(tmp_path):
    from src.agent_runtime.authority import create_request_authority
    for prompt in ('Use the browser to click the button.', 'Klicka på knappen i webbläsaren.'):
        current = create_request_authority(prompt, owner='alice', session_id='audit', workspace=str(tmp_path),
            capabilities={'search_browser'}, interactive_review=True)
        assert not current.permits(ExactOperation.normalize('private_browser', '{"action":"click","selector":"button"}'))
        assert current.permits(ExactOperation.normalize('private_browser', '{"action":"snapshot"}'))


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_english_calendar_shortcut_does_not_skip_model_or_human_review(tmp_path, monkeypatch, approvals, runtime):
    data, requests = await run_agent(tmp_path, monkeypatch, runtime=runtime,
        prompt='List my calendar events.', authority_override=authority(tmp_path),
        loop_options={'authority_gap_approvals':True},
        call_sequence=[{'__tool':'manage_calendar','action':'list_events'}])
    assert requests, 'A lexical fast path must not run instead of the model proposal'
    assert cards(data), data
    pending = approvals.peek(cards(data)[-1]['approval_id'])
    assert pending.authority_gap and pending.tool_name == 'manage_calendar'


@pytest.mark.asyncio
async def test_single_tool_legacy_fastpath_cannot_skip_interactive_review(tmp_path, monkeypatch, approvals):
    from tests.test_workspace_inspection_runtime import context
    prompt = 'List my API tokens.'
    _, contract, _ = context(prompt, tmp_path)
    schemas = [s for s in map(json.loads, contract.catalog_json) if s['function']['name'] == 'manage_tokens']
    assert schemas
    contract = replace(contract, capabilities=frozenset({'cookbook_admin'}),
        required=frozenset(), required_read_operation=None, unavailable=frozenset(),
        offered=frozenset({'manage_tokens'}), schema_json=tuple(json.dumps(s) for s in schemas))
    data, requests = await run_agent(tmp_path, monkeypatch, runtime='ordinary', prompt=prompt,
        contract_override=contract, authority_override=authority(tmp_path),
        loop_options={'authority_gap_approvals':True},
        call_sequence=[{'__tool':'manage_tokens','action':'list'}])
    assert requests, data
    assert cards(data), data
    assert approvals.peek(cards(data)[-1]['approval_id']).tool_name == 'manage_tokens'

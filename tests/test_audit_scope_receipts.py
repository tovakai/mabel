"""Canonical directory scope, hidden history, and receipt continuation."""
import json
from dataclasses import replace
from pathlib import Path
import pytest

from tests.test_audit_final_synthesis import PROMPT, GOOD
from tests.test_structured_audit_completion import run_agent, answer
from tests.test_workspace_inspection_runtime import context


@pytest.fixture
def collection(tmp_path):
    root = tmp_path / 'Library/Movies'
    root.mkdir(parents=True)
    for n in range(173):
        parent = root if n < 169 else root / 'Sequels (Local)' / 'Nested'
        parent.mkdir(parents=True, exist_ok=True)
        (parent / f'Movie {n:03}.md').write_text('---\n' + ('owned: true\n' if n < 159 else 'title: Reference\n') + '---\n')
    history = tmp_path / '.stversions/Library/Movies'
    history.mkdir(parents=True)
    (history / 'Old owned.md').write_text('---\nowned: true\n---\n')
    (history / 'Old reference.md').write_text('---\ntitle: Old\n---\n')
    other = tmp_path / 'Elsewhere/Library/Movies'
    other.mkdir(parents=True)
    (other / 'Unrelated.md').write_text('---\nowned: true\n---\n')
    return tmp_path


@pytest.fixture
def dispatch(collection, monkeypatch):
    from src import tool_execution as execution
    from src.tool_types import ToolBlock
    from src.turn_contract import bind_turn_contract
    monkeypatch.setattr(execution, 'owner_is_admin_or_single_user', lambda _: True)
    authority, contract, policy = context(PROMPT, collection)
    async def call(args, *, grant=None):
        active = grant or authority
        with bind_turn_contract(contract):
            return (await execution.execute_tool_block(ToolBlock('vault_audit', json.dumps(args)),
                owner=active.owner, session_id=active.session_id, workspace=active.workspace,
                request_authority=active, tool_policy=policy,
                security_context=execution.NO_TOOL_SECURITY_CONTEXT))[1]
    return call, authority


@pytest.mark.asyncio
async def test_real_receipt_continues_with_omitted_scope_fields(dispatch, monkeypatch):
    call, _ = dispatch
    initial = await call({'path':'Library/Movies'})
    assert initial['exit_code'] == 0
    from src.agent_tools import vault_batch_tools
    def no_rescan(*args, **kwargs):
        raise AssertionError('pagination reread the collection')
    monkeypatch.setattr(vault_batch_tools, 'run_audit', no_rescan)
    page = await call({'audit_id':initial['result']['audit_id'], 'report_offset':104})
    assert page['exit_code'] == 0, page
    assert page['result']['discovered'] == 173


@pytest.mark.asyncio
async def test_invented_receipt_has_specific_actionable_code(dispatch):
    call, _ = dispatch
    result = await call({'path':'Library/Movies', 'audit_id':'initial'})
    assert result['exit_code'] == 1
    assert result['failure_kind'] == 'audit_receipt_invalid_id'


def test_default_discovery_excludes_syncthing_history(collection):
    from src.agent_tools.vault_batch_tools import run_audit
    report = run_audit(collection, {'glob':'**/Library/Movies/*.md'})
    assert not any('.stversions' in path for rows in report['files'].values() for path in [row if isinstance(row, str) else row['path'] for row in rows])


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
@pytest.mark.parametrize('bad_args', [
    {'path':'/workspace', 'glob':'**/Library/Movies/*.md', 'property':'owned'},
    {'path':'/workspace/Library/Movies', 'audit_id':'initial'},
    {'path':'/workspace/Library/Movies', 'audit_id':'f' * 32},
    {'path':'/workspace'},
    {'path':'/workspace/Library/Movies', 'include_control_dirs':True},
])
async def test_bad_model_operation_is_corrected_once(collection, monkeypatch, runtime, bad_args):
    from src.agent_tools import vault_batch_tools
    scans = []
    real = vault_batch_tools.run_audit
    def counted(root, args, *rest):
        scans.append((str(root), dict(args)))
        return real(root, args, *rest)
    monkeypatch.setattr(vault_batch_tools, 'run_audit', counted)
    data, _ = await run_agent(collection, monkeypatch, runtime=runtime, tool='vault_audit', prompt=PROMPT,
        text='175 notes, 160 true and 15 missing.', synthesis_text=GOOD, call_sequence=[bad_args])
    assert len(scans) == 1
    root, args = scans[0]
    assert root == str(collection / 'Library/Movies')
    assert args.get('glob', '**/*.md') == '**/*.md'
    evidence = [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]
    assert evidence['complete'] and evidence['coverage'] == {'discovered':173, 'inspected':173}
    assert evidence['property_counts']['true'] == 159 and evidence['property_counts']['missing'] == 14
    assert sum(e.get('type') == 'audit_recovery' for e in data) == 1
    assert '175 notes' not in answer(data)
    assert answer(data) == GOOD


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
@pytest.mark.parametrize('second', [
    {'audit_id':'$receipt', 'report_offset':104},
    {'audit_id':'$receipt'},
    {'report_offset':104},
    {},
    {'audit_id':'initial', 'report_offset':104},
    {'audit_id':'$receipt', 'path':'/workspace', 'glob':'**/Library/Movies/*.md', 'report_offset':104},
])
async def test_pages_preserve_canonical_snapshot_without_rescan(collection, monkeypatch, runtime, second):
    from src.agent_tools import vault_batch_tools
    count = []
    real = vault_batch_tools.run_audit
    def scan(*args, **kwargs):
        count.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(vault_batch_tools, 'run_audit', scan)
    data, _ = await run_agent(collection, monkeypatch, runtime=runtime, tool='vault_audit', prompt=PROMPT,
        text=GOOD, call_sequence=[{'path':'Library/Movies'}, second])
    assert len(count) == 1
    good_pages = [json.loads(e['output']) for e in data if e.get('type') == 'tool_output' and e.get('tool') == 'vault_audit' and e.get('exit_code') == 0]
    assert len(good_pages) >= 2
    assert len({p['audit_id'] for p in good_pages}) == 1
    assert all(p['discovered'] == p['inspected'] == 173 and p['counts']['true'] == 159 for p in good_pages)
    assert answer(data) == GOOD


@pytest.mark.asyncio
async def test_receipt_expiry_missing_and_scope_have_distinct_codes(dispatch, monkeypatch):
    from src.agent_runtime import audit_receipts
    call, authority = dispatch
    initial = await call({'path':'Library/Movies'})
    token = initial['result']['audit_id']
    changed = await call({'path':'Library/Movies', 'audit_id':token, 'property':'watched', 'report_offset':0})
    assert changed['failure_kind'] == 'audit_receipt_scope_mismatch'
    missing = await call({'path':'Library/Movies', 'audit_id':'e' * 32, 'report_offset':0})
    assert missing['failure_kind'] == 'audit_receipt_missing'
    now = audit_receipts.monotonic()
    monkeypatch.setattr(audit_receipts, 'monotonic', lambda: now + 601)
    expired = await call({'audit_id':token, 'report_offset':0})
    assert expired['failure_kind'] == 'audit_receipt_expired'
    foreign = await call({'audit_id':token, 'report_offset':0}, grant=replace(authority, session_id='other'))
    assert foreign['failure_kind'] == 'audit_receipt_missing'


def test_unusual_directories_are_active_but_history_is_opt_in(collection):
    from src.agent_tools.vault_batch_tools import run_audit
    unusual = collection / '.my-legitimate-notes'
    unusual.mkdir()
    (unusual / 'Active.md').write_text('---\nowned: true\n---\n')
    report = run_audit(collection, {})
    assert report['discovered'] == 175  # 173 selected notes + unrelated active note + legitimate hidden note
    assert report['counts']['true'] == 161
    historical = run_audit(collection, {'include_control_dirs':True})
    assert historical['discovered'] == 177
    assert historical['counts']['true'] == 162


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_explicit_authorized_history_audit_is_possible(collection, monkeypatch, runtime):
    prompt = 'Audit owned: true in .stversions/Library/Movies, including historical directories.'
    data, _ = await run_agent(collection, monkeypatch, runtime=runtime, tool='vault_audit', prompt=prompt,
        scope='.stversions/Library/Movies', extra={'include_control_dirs':True}, text='Done.')
    report = [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]
    assert report['complete'] and report['coverage']['discovered'] == 2
    assert report['property_counts']['true'] == report['property_counts']['missing'] == 1


@pytest.mark.asyncio
async def test_page_zero_can_start_a_snapshot(dispatch):
    call, _ = dispatch
    first = await call({'path':'Library/Movies', 'report_offset':0})
    assert first['exit_code'] == 0
    assert first['result']['discovered'] == 173


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_expired_page_recovers_with_one_fresh_scoped_audit(collection, monkeypatch, runtime):
    from src.agent_runtime import audit_receipts
    from src.agent_tools import vault_batch_tools
    real_resolve, real_scan = audit_receipts.resolve_arguments, vault_batch_tools.run_audit
    now = audit_receipts.monotonic()
    scans = []
    def resolve(authority, args):
        if args.get('audit_id') and not scans[1:]:
            expires_after = audit_receipts.monotonic() + 601
            monkeypatch.setattr(audit_receipts, 'monotonic', lambda: expires_after)
        return real_resolve(authority, args)
    def scan(*args, **kwargs):
        scans.append(str(args[0]))
        return real_scan(*args, **kwargs)
    monkeypatch.setattr(audit_receipts, 'resolve_arguments', resolve)
    monkeypatch.setattr(vault_batch_tools, 'run_audit', scan)
    data, _ = await run_agent(collection, monkeypatch, runtime=runtime, tool='vault_audit', prompt=PROMPT,
        text=GOOD, call_sequence=[{'path':'Library/Movies'}, {'audit_id':'$receipt', 'report_offset':104}])
    assert scans == [str(collection / 'Library/Movies')] * 2
    assert sum(e.get('type') == 'audit_recovery' for e in data) == 1
    assert any('audit_receipt_expired' in str(e) for e in data)
    assert answer(data) == GOOD


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
@pytest.mark.parametrize('denied,path', [(False, '/etc'), (True, '/workspace')])
async def test_security_denials_never_become_scope_corrections(collection, monkeypatch, runtime, denied, path):
    from src.agent_tools import vault_batch_tools
    def no_scan(*args, **kwargs):
        raise AssertionError('denied operation was retried')
    monkeypatch.setattr(vault_batch_tools, 'run_audit', no_scan)
    data, _ = await run_agent(collection, monkeypatch, runtime=runtime, tool='vault_audit', prompt=PROMPT,
        denied=denied, text=GOOD, call_sequence=[{'path':path}])
    assert not any(e.get('type') == 'audit_recovery' for e in data)
    evidence = [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]
    assert not evidence['complete'] and not evidence['audit_executed']
    assert 'incomplete' in answer(data).lower()


@pytest.mark.asyncio
async def test_receipt_rebinding_preserves_symlink_and_scope_guards(collection, dispatch):
    call, authority = dispatch
    initial = await call({'path':'Library/Movies'})
    token = initial['result']['audit_id']
    # A different request in the same authenticated session can page the snapshot.
    followup = await call({'audit_id':token, 'report_offset':104}, grant=replace(authority, request_id='followup'))
    assert followup['exit_code'] == 0
    # Metadata defaults must not override an explicitly supplied forbidden path.
    outside = await call({'path':'/etc', 'audit_id':token, 'report_offset':0})
    assert outside['failure_kind'] == 'resource_identity_denied'
    original = collection / 'Library/Movies'
    original.rename(collection / 'Library/Previous Movies')
    original.symlink_to('/etc', target_is_directory=True)
    replaced = await call({'audit_id':token, 'report_offset':104})
    assert replaced['failure_kind'] == 'resource_identity_denied'


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_general_quoted_directory_is_canonical(collection, monkeypatch, runtime):
    target = collection / 'Collections/Films (Personal)'
    target.parent.mkdir()
    (collection / 'Library/Movies').rename(target)
    prompt = 'Audit owned: true in `Collections/Films (Personal)` without changing files.'
    data, _ = await run_agent(collection, monkeypatch, runtime=runtime, tool='vault_audit', prompt=prompt,
        text='Done.', call_sequence=[{'path':'/workspace', 'glob':'**/*.md'}])
    report = [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]
    assert report['complete'] and report['coverage'] == {'discovered':173, 'inspected':173}
    assert report['scope'] == '/workspace/Collections/Films (Personal)'


def test_recursive_pattern_retains_directory_and_matching_rules():
    from src.agent_runtime.structured_audits import audit_requirement
    requirement = audit_requirement('Audit owned: true in `Collections/Films/**/*.md`.')
    assert requirement.path == 'Collections/Films'
    assert requirement.pattern == '**/*.md'


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime', ['ordinary', 'compact'])
async def test_failed_correction_is_bounded_and_reports_actual_blocker(collection, monkeypatch, runtime):
    from src.agent_tools import vault_batch_tools
    attempts = []
    def unavailable(*args, **kwargs):
        attempts.append(1)
        raise ImportError('synthetic missing parser')
    monkeypatch.setattr(vault_batch_tools, 'run_audit', unavailable)
    data, _ = await run_agent(collection, monkeypatch, runtime=runtime, tool='vault_audit', prompt=PROMPT,
        text=GOOD, call_sequence=[{'path':'/workspace', 'glob':'**/Library/Movies/*.md'}])
    assert len(attempts) == 1
    assert sum(e.get('type') == 'audit_recovery' for e in data) == 1
    evidence = [e['data'] for e in data if e.get('type') == 'audit_evidence'][-1]
    assert not evidence['complete'] and evidence['correction_used']
    assert 'YAML parser unavailable' in answer(data)
    assert [e['data'] for e in data if e.get('type') == 'completion_decision'][-1]['can_complete'] is False

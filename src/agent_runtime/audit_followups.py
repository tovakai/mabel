"""Scoped, deterministic questions about a server-attested completed audit.

This parser selects an evidence query, never a resource. Only the native receipt
store can supply the resource, identifier, classifications and filenames.
"""
import json
import re


def followup_kind(text):
    if not isinstance(text, str) or len(text) > 350:
        return None
    text = text.strip().lower().replace('’', "'")
    if re.search(r'\b(?:inspect|check|explore|compare|frontmatter)\b|\bmy\s+\w+\s+(?:library|collection|folder)\b', text):
        return None
    if re.search(r'\baudit\b', text) and not re.fullmatch(r'(?:the )?audit (?:counts|totals|results)[.!?]*', text):
        return None
    # Compound actions and newly named resources need ordinary intent handling.
    if re.search(r'[/\\]|https?:|\b(?:delete|remove|write|edit|change|move|rename|run|execute|bash|python|email|calendar|export|save|send|instead|another|other directory)\b', text):
        return None
    if re.fullmatch(r'(?:please\s+)?(?:show\s+)?(?:the\s+)?(?:next(?:\s+page)?|more)(?:\s+(?:please|results|files))?[.!?]*', text):
        return 'next'
    if not re.search(r'\b(?:which|list|show|tell|counts?|how many)\b', text):
        return None
    if re.search(r"\b(?:don'?t|do not|not)\s+(?:i\s+)?own\b|\bnot (?:marked as )?owned\b", text):
        return 'not_true'
    if re.search(r'\b(?:missing|unmarked)\b', text):
        return 'missing'
    if re.search(r'\binvalid\b|\bmalformed\b', text):
        return 'invalid'
    if re.search(r'\b(?:those|these)\s+\d+\b', text):
        return 'number'
    if re.fullmatch(r"(?:can you |could you |please )?(?:(?:tell|show|give) me )?(?:what (?:are|were) )?(?:the |those |audit )?(?:counts|totals|results)(?: again)?[.!?]*", text):
        return 'counts'
    return None


def plan_for_request(text, *, owner, session_id, workspace):
    kind = followup_kind(text)
    if kind is None:
        return None
    from .task_artifacts import select
    latest = select(owner, session_id, workspace)
    if latest is not None and latest['kind'] != 'markdown_property_audit':
        return None  # Let the model interpret references to other result kinds.
    from .audit_receipts import task_reference
    reference = task_reference(owner, session_id, workspace)
    plan = {'kind':kind, 'reference':reference, 'operations':[]}
    if reference is None:
        return plan
    classification, offset = kind, 0
    if kind == 'number':
        number = int(re.search(r'\b(?:those|these)\s+(\d+)\b', text, re.I)[1])
        matches = [k for k, value in reference['counts'].items() if value == number]
        if len(matches) != 1:
            plan['error'] = 'Which classification do you mean: missing, false, or invalid?'
            return plan
        classification = matches[0]
    if kind == 'counts':
        classification = None
    if kind == 'next':
        classification = reference['last_filter']
        offset = reference['next_offset']
        if offset is None:
            plan['error'] = 'There are no more entries in the previous result page.'
            return plan
    root, pattern, key, include_controls = reference['selection']
    fresh = dict(path=root, glob=pattern, property=key, include_control_dirs=include_controls,
                 require_current=True)
    if classification is not None:
        fresh.update(classification=classification, report_offset=offset)
    saved = dict(fresh, audit_id=reference['audit_id'])
    plan.update(classification=classification, offset=offset, operations=[saved, fresh])
    return plan


def operation_input(args):
    return json.dumps(args, sort_keys=True, separators=(',', ':'))


def readable_report(report, page, classification, scope):
    c = report['counts']
    lines = [f"Audit of {scope}: {report['discovered']} Markdown notes discovered; {report['inspected']} inspected. Coverage is {'complete' if report['complete'] else 'incomplete'}."]
    if classification is None:
        lines.append(f"`{report['property']}` — true: {c['true']}; false: {c['false']}; missing: {c['missing']}; invalid or malformed: {c['invalid']}; unreadable: {c['uninspected']}.")
    else:
        label = {'not_true':'not marked true', 'missing':'missing the property (unmarked)',
                 'false':'explicitly false', 'invalid':'invalid or malformed',
                 'uninspected':'unreadable', 'true':'marked true'}[classification]
        lines.append(f"{page['matching']} entries are {label} for `{report['property']}`.")
        if page['entries']:
            start = page['report_offset'] + 1
            lines.append(f"Showing {start}–{start + len(page['entries']) - 1}:")
            for entry in page['entries']:
                name = entry['path'].replace('`', '\\`')
                lines.append(f"- `{scope}/{name}`")
        if page['next_offset'] is not None:
            lines.append('Ask for the next page to see more filenames.')
    if report['property'] == 'owned' and (classification in {None, 'missing', 'not_true'}):
        lines.append('Missing `owned` means unmarked, not proof that you do not physically own the item. Missing and false are not metadata inconsistencies.')
    lines.append(f"Metadata issues: {c['invalid']} invalid or malformed; {c['uninspected']} unreadable. No files modified.")
    return '\n\n'.join(lines[:3]) + ('\n' + '\n'.join(lines[3:]) if len(lines) > 3 else '')


async def answer_followup(plan, bound):
    """Execute exact newly authorized reads through the production dispatcher."""
    from .authority import active_request_authority
    from .structured_audits import StructuredAudit, AuditRequirement, bind_audit
    from src.turn_contract import bind_turn_contract
    from src.tool_execution import execute_tool_block, ToolRunSecurityContext
    from src.tool_types import ToolBlock
    from .audit_receipts import remember_page
    authority = active_request_authority()
    reference = plan['reference']
    events = []
    if reference is None:
        return 'No accessible completed audit is available in this session and workspace. Which directory and property should I audit?', events, False
    if plan.get('error'):
        return plan['error'], events, False
    root, pattern, key, controls = reference['selection']
    try:
        reference['directory'].validate()
    except (ValueError, OSError, RuntimeError):
        return 'The saved audit directory identity is no longer valid. Select and explicitly audit the intended directory again.', events, False
    state = StructuredAudit(AuditRequirement(key, root, pattern, include_control_dirs=controls), bound['workspace'])
    security = ToolRunSecurityContext(external_untrusted_context_seen=bool(bound.get('external_untrusted_context_seen')),
                                      delegated_credential=bool(bound.get('delegated_credential')))
    security.observe_messages(bound.get('messages') or [])
    result = None
    with bind_audit(state), bind_turn_contract(bound.get('turn_contract')):
        for index, args in enumerate(plan['operations']):
            content = operation_input(args)
            _, result = await execute_tool_block(ToolBlock('vault_audit', content), owner=bound.get('owner'),
                session_id=bound.get('session_id'), workspace=bound.get('workspace'), request_authority=authority,
                tool_policy=bound.get('tool_policy'), disabled_tools=bound.get('disabled_tools'),
                security_context=security, client_runtime_context=bound.get('client_runtime_context'))
            events.append(dict(type='tool_output', tool='vault_audit', command=content,
                               output=result.get('output') or result.get('error', ''), exit_code=result.get('exit_code'),
                               source='structured_audit_followup', failure_kind=result.get('failure_kind')))
            if result.get('exit_code') == 0:
                report = result.get('result')
                if not state.report or not report or not report.get('complete'):
                    return 'The refreshed audit has incomplete coverage. ' + state.answer(), events, False
                page = json.loads(result['output'])
                remember_page(authority, report['audit_id'], plan['classification'], page.get('next_offset'))
                return readable_report(report, page, plan['classification'], state.report_path), events, True
            if index or result.get('failure_kind') not in {'audit_receipt_expired', 'audit_receipt_missing', 'audit_receipt_changed'} or result.get('blocked'):
                break
    return 'The audit follow-up is incomplete: ' + str((result or {}).get('error', 'No authorized audit evidence is available.')) + ' Which authorized directory should I use?', events, False

"""Scoped Markdown audit obligations and native producer attestations.

Only the native producer records property evidence. Listings, model prose,
transport events, and prior-session results never attest to an audit.
"""
from contextlib import contextmanager
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import re


@dataclass(frozen=True)
class AuditRequirement:
    property: str
    path: str | None
    pattern: str = '**/*.md'
    collection: str = ''
    unresolved: str = ''
    include_entries: bool = False
    include_control_dirs: bool = False


def audit_requirement(text):
    if not isinstance(text, str):
        return None
    positive = re.sub(r"\b(?:do not|don't|never|without|no)\b[^.;,\n]*", '', text, flags=re.I)
    if re.search(r'\b(?:manage_notes|manage_documents|manage_calendar|manage_tasks)\b|\b(?:use|run|execute)\b[^.;\n]{0,30}\b(?:bash|python|host_shell)\b', positive, re.I):
        return None
    if re.search(r'\b(?:write|create|generate|design)\b[^.;\n]*\b(?:script|code|function|tests?|plan)\b', positive, re.I):
        return None
    text = positive
    if not re.search(r'\b(?:audit|count|inspect|check|compare|review|look|tell|which|how many)\b', text, re.I):
        return None
    if not re.search(r'\b(?:markdown|notes?|files?|library|collection|vault|folder|directory|frontmatter|metadata)\b|\w+/\w+', text, re.I):
        return None
    literal = re.search(r'\b([A-Za-z_][\w-]*)\s*:\s*(?:true|false)\b', text, re.I)
    named = re.search(r'\b(?:property|field|key)\s+(?:(?:named|called)\s+)?[`"\']?([A-Za-z_][\w-]*)', text, re.I)
    preceding = re.search(r'[`"\']?([A-Za-z_][\w-]*)[`"\']?\s+(?:property|field|key)\b', text, re.I)
    stopwords = {'across', 'in', 'of', 'for', 'and', 'values', 'is', 'has', 'the', 'yaml', 'frontmatter'}
    key = literal[1] if literal else named[1] if named and named[1].casefold() not in stopwords else preceding[1] if preceding and preceding[1].casefold() not in stopwords else 'owned' if re.search(r'\bowned\b|\bownership\b', text, re.I) else None
    if not key:
        return None  # Do not turn general prose/counting into numerical validation.
    path = re.search(r'(?:/workspace(?:/[^\s`"\',;!?]+)?|(?:[\w.-]+/)+[^\s`"\',;!?]+)', text)
    selector = path[0].rstrip('.:)]}') if path else None
    # Quoted directory names can contain spaces or closing punctuation. Keep
    # those literal rather than truncating the server-owned target.
    quoted = next((m[2] for m in re.finditer(r'([`"\'])([^`"\'\n]+)\1', text)
                   if '/' in m[2] and '://' not in m[2]), None)
    if quoted is not None:
        selector = quoted
    pattern = '**/*.md'
    if selector and any(mark in selector for mark in '*?['):
        parts = selector.split('/')
        start = next(i for i, part in enumerate(parts) if any(mark in part for mark in '*?['))
        selector, pattern = '/'.join(parts[:start]) or '.', '/'.join(parts[start:])
    collection = re.search(r'\b([A-Za-z][\w-]*)\s+(?:library|collection|folder|directory)\b', text, re.I)
    hint = collection[1].casefold() if collection and collection[1].casefold() not in {'my', 'the', 'a', 'this', 'active', 'selected', 'current', 'workspace', 'markdown'} else ''
    if selector is None and not hint and re.search(r'\b(?:workspace|vault)\b', text, re.I):
        selector = '.'
    keys = set(re.findall(r'\b([A-Za-z_][\w-]*)\s*:\s*(?:true|false)\b', text, re.I))
    unresolved = 'Several properties were requested. Which single boolean property should this audit verify first?' if len(keys) > 1 else ''
    include_entries = bool(re.search(r"\bwhich\b[^.;\n]{0,100}\b(?:entries|files|notes|movies)\b|\b(?:filenames?|file names?)\b|\b(?:list|identify|show|name)\b[^.;\n]{0,100}\b(?:owned|matches|true)\b", text, re.I))
    from .workspace_discovery import is_control_directory
    explicit_control = selector is not None and any(is_control_directory(p) for p in Path(selector).parts)
    include_controls = explicit_control or bool(re.search(r"\b(?:include|including|inspect|audit)\b[^.;\n]{0,70}\b(?:hidden|history|historical|version history|control directories)\b", text, re.I))
    return AuditRequirement(key, selector, pattern.replace('\\', '/'), hint, unresolved, include_entries, include_controls)


_ACTIVE = ContextVar('structured_audit', default=None)
_OBSERVED = ContextVar('native_audit_observations', default=None)


def active_audit():
    return _ACTIVE.get()


def observing_native_audits():
    return _OBSERVED.get() is not None


@contextmanager
def bind_audit(state):
    token = _ACTIVE.set(state)
    try:
        yield state
    finally:
        _ACTIVE.reset(token)


@contextmanager
def observe_native_audits(records):
    token = _OBSERVED.set(records)
    try:
        yield
    finally:
        _OBSERVED.reset(token)


def record_native_audit(report, root, payload):
    """Called only by the admitted native producer, never by transport text."""
    audit = active_audit()
    if audit is not None:
        audit.record(report, root, payload)
    observed = _OBSERVED.get()
    if observed is not None and validate_report(report):
        key = (str(root), payload.get('glob', '**/*.md'), report['property'])
        if key not in observed and len(observed) >= 8:
            observed.pop(next(iter(observed)))
        observed[key] = (deepcopy(report), str(root), dict(payload))


def validate_report(report):
    """Validate the native schema, partition, coverage and unique file inventory."""
    if not isinstance(report, dict) or type(report.get('schema_version')) is not int or report['schema_version'] != 1:
        return False
    counts = report.get('property_counts', {})
    names = {'true', 'false', 'missing', 'invalid', 'malformed', 'unreadable'}
    if not isinstance(counts, dict) or set(counts) != names or any(type(n) is not int or n < 0 for n in counts.values()):
        return False
    discovered, inspected = report.get('discovered'), report.get('inspected')
    if type(discovered) is not int or type(inspected) is not int or not 0 <= inspected <= discovered <= 1000:
        return False
    if sum(counts.values()) != discovered or inspected != discovered - counts['unreadable']:
        return False
    complete = inspected == discovered
    if type(report.get('complete')) is not bool or report['complete'] != complete or report.get('status') != ('complete' if complete else 'partial'):
        return False
    legacy = report.get('counts', {})
    if not isinstance(legacy, dict) or legacy != {**{k: counts[k] for k in ('true', 'false', 'missing')}, 'invalid': counts['invalid'] + counts['malformed'], 'uninspected': counts['unreadable']}:
        return False
    groups = report.get('files', {})
    if not isinstance(groups, dict) or set(groups) != set(legacy) or any(not isinstance(groups[k], list) or len(groups[k]) != legacy[k] for k in groups):
        return False
    if any(not isinstance(row, str) for k in ('true', 'false', 'missing') for row in groups[k]):
        return False
    if any(not isinstance(row, dict) or row.get('kind') not in {'invalid', 'malformed'} or not isinstance(row.get('reason'), str) for row in groups['invalid']):
        return False
    if sum(row['kind'] == 'malformed' for row in groups['invalid']) != counts['malformed']:
        return False
    if any(not isinstance(row, dict) or row.get('kind') != 'unreadable' or not isinstance(row.get('reason'), str) for row in groups['uninspected']):
        return False
    if report.get('errors') != groups['invalid'] + groups['uninspected']:
        return False
    paths = [row if isinstance(row, str) else row.get('path') if isinstance(row, dict) else None for rows in groups.values() for row in rows]
    if any(not isinstance(p, str) or not p or Path(p).is_absolute() or '..' in Path(p).parts for p in paths) or len(set(paths)) != discovered:
        return False
    return type(report.get('modified')) is int and report['modified'] == 0


class AuditScopeError(ValueError):
    code = 'audit_scope_mismatch'


_RECOVERABLE_AUDIT_ERRORS = frozenset({
    'audit_scope_mismatch', 'audit_receipt_invalid_id', 'audit_receipt_expired',
    'audit_receipt_missing', 'audit_receipt_scope_mismatch', 'invalid_arguments',
})


@dataclass
class StructuredAudit:
    requirement: AuditRequirement
    workspace: str
    report: dict | None = None
    report_path: str = ''
    candidate: str | None = None
    attempted: bool = False
    blocker: str = ''
    listings: list = field(default_factory=list)
    correction_pending: bool = False
    correction_used: bool = False
    correction_reason: str = ''
    pending_offset: int | None = None
    canonical_target: str | None = None
    target_identity: object = None

    def __post_init__(self):
        if self.requirement.path is not None:
            try:
                self.target()
            except (ValueError, OSError, RuntimeError):
                self.blocker = 'The requested directory could not be resolved inside the authorized workspace.'

    def canonical(self, selector):
        if not self.workspace:
            raise ValueError('No bound workspace')
        root = Path(self.workspace).resolve()
        value = str(selector)
        if value == '/workspace' or value.startswith('/workspace/'):
            value = value[len('/workspace'):].lstrip('/') or '.'
        target = (root / value).resolve()
        target.relative_to(root)
        return target

    def target(self):
        selector = self.requirement.path or self.candidate
        if self.canonical_target is None and selector is not None:
            from src.agent_runtime.resources import FileObjectIdentity
            target = self.canonical(selector)
            self.canonical_target = str(target)
            try:
                self.target_identity = FileObjectIdentity.observe(target)
            except FileNotFoundError:
                pass
        return Path(self.canonical_target) if self.canonical_target is not None else None

    def observe_listing(self, path, listing):
        self.listings.append(dict(listing))
        if self.requirement.path is None and self.requirement.collection:
            # A listing can locate the named directory; it proves no properties.
            stem = Path(path).name.casefold().rstrip('s')
            if stem == self.requirement.collection.rstrip('s'):
                self.candidate = str(path)

    def check_operation(self, root, payload):
        """Use the same trusted selection in the executor and evidence gate."""
        target = self.target()
        if target is None and self.requirement.collection and Path(root).name.casefold().rstrip('s') == self.requirement.collection.rstrip('s'):
            self.candidate = str(root)
            target = self.target()
        if target is None:
            raise AuditScopeError('The requested collection directory is unresolved; locate it before auditing')
        if (self.requirement.unresolved or self.canonical(root) != target
                or payload.get('glob', '**/*.md').replace('\\', '/') != self.requirement.pattern
                or payload.get('property', 'owned') != self.requirement.property
                or payload.get('include_control_dirs', False) != self.requirement.include_control_dirs):
            raise AuditScopeError('Audit directory, recursive glob, property or discovery policy differs from the trusted request; use the canonical target')
        from src.agent_runtime.resources import FileObjectIdentity, ResourceIdentityError
        if self.target_identity is not None and FileObjectIdentity.observe(target) != self.target_identity:
            raise ResourceIdentityError('The canonical audit directory identity changed')

    def observe_result(self, result, content=''):
        self.attempted = True
        if result.get('exit_code') not in (None, 0):
            code = result.get('failure_kind') or result.get('failure_reason')
            if code in _RECOVERABLE_AUDIT_ERRORS and result.get('executed') is False and not result.get('blocked'):
                self.correction_pending = not self.correction_used
                self.correction_reason = code
                try:
                    import json
                    offset = json.loads(content).get('report_offset')
                    if type(offset) is int and 0 <= offset <= 1000:
                        self.pending_offset = offset
                except (ValueError, TypeError, AttributeError):
                    pass
            else:
                self.correction_pending = False
            if self.report is None:
                self.blocker = str(result.get('error') or 'Native audit failed: ' + (code or 'unknown failure'))

    def record(self, report, root, payload):
        self.attempted = True
        try:
            self.check_operation(root, payload)
            target = self.target()
            if not validate_report(report) or report.get('property') != self.requirement.property:
                raise AuditScopeError('The native report did not match the audit coverage contract')
            self.report = deepcopy(report)
            relative = target.relative_to(Path(self.workspace).resolve()).as_posix()
            self.report_path = '/workspace' if relative == '.' else '/workspace/' + relative
            self.blocker = ''
            self.correction_pending = False
        except AuditScopeError as error:
            self.correction_pending = not self.correction_used
            self.correction_reason = 'audit_scope_mismatch'
            self.blocker = str(error)
        except (ValueError, OSError, RuntimeError):
            self.blocker = 'The requested audit scope could not be verified.'

    def fallback_arguments(self):
        if not self.workspace:
            self.blocker = 'Select an authorized workspace before auditing its Markdown properties.'
            return None
        if self.requirement.unresolved:
            self.blocker = self.requirement.unresolved
            return None
        if self.correction_used or (self.attempted and not self.correction_pending):
            return None
        try:
            target = self.target()
        except (ValueError, OSError, RuntimeError):
            target = None
        if target is None:
            self.blocker = 'The collection directory is unresolved. Which directory in the selected workspace should be audited?'
            return None
        self.correction_used = True
        result = {'path': str(target), 'glob': self.requirement.pattern, 'property': self.requirement.property}
        if self.requirement.include_control_dirs:
            result['include_control_dirs'] = True
        if self.correction_pending and self.report is not None and self.report.get('audit_id') and self.correction_reason not in {'audit_receipt_expired', 'audit_receipt_missing'}:
            result.update(audit_id=self.report['audit_id'], report_offset=self.pending_offset or 0)
        return result

    def facts(self):
        complete = self.report is not None and self.report['complete']
        return {'required': True, 'property': self.requirement.property, 'complete': complete,
                'audit_executed': self.report is not None, 'attempted': self.attempted, 'scope': self.report_path,
                'coverage': {k: self.report[k] for k in ('discovered', 'inspected')} if self.report else None,
                'property_counts': self.report['property_counts'] if self.report else None,
                'audit_id': self.report.get('audit_id') if self.report else None,
                'correction_used': self.correction_used, 'correction_reason': self.correction_reason,
                'listings': self.listings[-5:], 'blocker': self.blocker}

    def answer(self):
        if self.report is None:
            text = 'The property audit is incomplete: ' + (self.blocker or 'No validated native frontmatter audit was recorded. Directory listings do not establish property values.')
            return text if '?' in text else text + ' Would you like a listing-only report until the native audit is available?'
        r, c = self.report, self.report['property_counts']
        prefix = 'Verified property audit' if r['complete'] else 'Incomplete property audit'
        lines = [f"{prefix} of {self.report_path} ({self.requirement.pattern}), property `{r['property']}`.",
                 f"Markdown files discovered: {r['discovered']}; successfully inspected: {r['inspected']}.",
                 f"True: {c['true']}; false: {c['false']}; missing: {c['missing']}; invalid values: {c['invalid']}; malformed frontmatter: {c['malformed']}; unreadable: {c['unreadable']}.",
                 'Missing or false values are not inconsistencies for an optional reference-library property.']
        lines.append('Coverage: complete.' if r['complete'] else 'Coverage: partial; uninspected property values remain unknown.')
        owned = r['files']['true']
        if owned and self.requirement.include_entries:
            sample = []
            budget = 4000
            for path in owned[:25]:
                if len(path) > budget:
                    break
                sample.append(path)
                budget -= len(path)
            lines.append('Marked true (bounded sample):\n' + '\n'.join('- ' + p for p in sample))
            if len(owned) > len(sample):
                lines.append(f'Showing {len(sample)} of {len(owned)} matches; the remaining filenames are available in the paginated audit details.')
        errors = []
        budget = 2000
        for error in (r['files']['invalid'] + r['files']['uninspected'])[:5]:
            size = len(error['path']) + len(error['reason'])
            if size > budget:
                break
            errors.append(error)
            budget -= size
        if errors:
            lines.append('Metadata/read issues (bounded sample):\n' + '\n'.join('- ' + e['path'] + ': ' + e['reason'] for e in errors))
        if not errors and not any(c[k] for k in ('invalid', 'malformed', 'unreadable')):
            lines.append('No metadata inconsistencies were found for this property.')
        if self.requirement.include_entries and r.get('audit_id'):
            lines.append('Full filename details: audit receipt ' + r['audit_id'] + ' (paginated snapshot).')
        if not r['complete']:
            lines.append('These are partial counts; unreadable files have unknown property values. Resolve the reported read errors before claiming full coverage. Would you like a report limited to the inspected subset?')
        return '\n\n'.join(lines)


def synthesis_evidence(audit):
    """Small immutable facts for one prose repair; no listing/history tail."""
    r = audit.report
    samples = [p for p in r['files']['true'] if len(p) <= 240][:3] if audit.requirement.include_entries else []
    issues = [e for e in r['errors'] if len(e['path']) <= 240][:3]
    return {'directory': audit.report_path, 'property': r['property'],
            'discovered': r['discovered'], 'inspected': r['inspected'],
            'counts': r['property_counts'], 'coverage_complete': r['complete'],
            'issues': issues, 'matching_filename_sample': samples,
            'audit_id': r.get('audit_id'), 'filename_details_requested': audit.requirement.include_entries,
            'optional_missing_is_error': False}


def audit_answer_problem(text, audit):
    """Validate only this audit's answer slots, never arbitrary conversation."""
    if not isinstance(text, str) or not text.strip():
        return 'empty_answer'
    if len(text) > 6000 or text.lstrip().startswith(('{', '```')):
        return 'raw_or_oversized_answer'
    lines = text.splitlines()
    if sum('.md' in line.casefold() for line in lines) > (25 if audit.requirement.include_entries else 5):
        return 'directory_listing'
    relative = audit.report_path.removeprefix('/workspace/').rstrip('/')
    if relative not in text and audit.report_path not in text:
        return 'missing_directory'
    r, counts = audit.report, audit.report['property_counts']
    clean = text
    # Digits in attested filenames/receipt identities are references, not counts.
    for rows in r['files'].values():
        for row in rows:
            path = row if isinstance(row, str) else row['path']
            clean = clean.replace(path, '').replace(Path(path).name, '')
    if r.get('audit_id'):
        clean = clean.replace(r['audit_id'], '')
    clean = clean.replace(audit.report_path, '').replace(relative, '')
    expected = {'discovered': r['discovered'], 'inspected': r['inspected'], **counts}
    if re.search(r'(?<!\w)-\d+', clean):
        return 'contradictory_counts'
    if any(int(n) not in set(expected.values()) for n in re.findall(r'\b\d+\b', clean)):
        return 'unsupported_counts'
    prop = re.escape(r['property'])
    aliases = {
        'discovered': r'(?:discovered|total|found)',
        'inspected': r'(?:inspected|checked|audited|read|processed)',
        'true': rf'(?:{prop}\s*:\s*true|{prop}(?!\s*(?::\s*)?false)|true)',
        'false': rf'(?:{prop}\s*:\s*false|false)',
        'missing': r'(?:missing|absent|without\s+(?:the\s+)?(?:property|key))',
        'invalid': r'(?:invalid(?:\s+values)?)',
        'malformed': r'(?:malformed(?:\s+frontmatter)?)',
        'unreadable': r'(?:unreadable|read\s+errors)',
    }
    observed = {}
    filler = r'(?:(?:files?|notes?|entries|are|were|have|was|is|been|explicitly|marked|as|set|to)\s+){0,4}'
    for key, label in aliases.items():
        values = [int(n) for n in re.findall(rf'\b{label}\b\s*(?::|=|is|are|all)?\s*(\d+)\b', clean, re.I)]
        values += [int(n) for n in re.findall(rf'\b(\d+)\s+{filler}{label}\b', clean, re.I)]
        zero_gap = filler if key == 'true' else r'[^\d.;\n]{0,25}'
        if key not in {'discovered', 'inspected'} and re.search(rf'\b(?:no|none|zero)\s+{zero_gap}\b{label}\b', clean, re.I):
            values.append(0)
        if any(value != expected[key] for value in values):
            return 'contradictory_' + key
        if values:
            observed[key] = True
    if re.search(rf'\b{r["discovered"]}\s+(?:Markdown\s+)?(?:files|notes)\b', clean, re.I):
        observed['discovered'] = True
    if not all(k in observed for k in ('discovered', 'inspected', 'true', 'false', 'missing')):
        return 'missing_audit_findings'
    if not re.search(r'\b(?:all|complete|fully|every)\b', clean, re.I):
        return 'missing_coverage'
    has_issues = any(counts[k] for k in ('invalid', 'malformed', 'unreadable'))
    no_issues = bool(re.search(r'\b(?:no|zero|0)\s+(?:metadata\s+)?(?:issues|inconsistencies|errors)\b|metadata\s+(?:is|looks)\s+consistent', clean, re.I))
    if has_issues:
        if no_issues or any(counts[k] and k not in observed for k in ('invalid', 'malformed', 'unreadable')):
            return 'unsupported_issue_summary'
    elif not no_issues:
        return 'missing_issue_summary'
    if re.search(r'\bmissing\b[^.;\n]{0,45}\b(?:errors?|inconsisten\w*|problems?)\b', clean, re.I) and not re.search(r"\b(?:not|aren.t|isn.t)\b", clean, re.I):
        return 'optional_property_marked_as_error'
    if audit.requirement.include_entries:
        if not any(p in text or Path(p).name in text for p in r['files']['true']) and not (r.get('audit_id') and r['audit_id'] in text):
            if counts['true']:
                return 'missing_filename_references'
    return ''

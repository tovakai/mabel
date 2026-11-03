"""Bounded native audit snapshots. A receipt is data, never an access grant.

Metadata may restore omitted selectors before resource binding. Snapshot contents
are released only after dispatcher admission and binding; owner, session,
workspace, selection and sealed directory identity must match.
"""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from uuid import uuid4
import json
import re

_TTL = 600
_MAX_RECEIPTS = 16
_MAX_BYTES = 16 * 1024 * 1024
_lock = Lock()
_receipts = OrderedDict()
_expired = OrderedDict()


def stat_signature(path):
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class AuditReceiptError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise AuditReceiptError('audit_receipt_invalid_id', 'Use the audit_id returned by the native audit, not an invented label. Omit it to start a new audit.')


def _principal(authority):
    return (authority.owner, authority.session_id, authority.workspace)


def _selection(root, args):
    return (str(root), args.get('glob', '**/*.md').replace('\\', '/'),
            args.get('property', 'owned'), args.get('include_control_dirs', False))


@dataclass
class Receipt:
    id: str
    principal: tuple
    request_id: str
    selection: tuple
    directory: object
    report: dict
    expires: float
    size: int
    versions: tuple = ()


def _identity(ctx, root, args):
    authority = ctx.get('request_authority')
    bound = ctx.get('resource_operation')
    if authority is None or bound is None or len(bound.bindings) != 1:
        return None
    directory = bound.bindings[0].resource
    directory.validate()
    principal = _principal(authority)
    selection = _selection(root, args)
    return principal, authority.request_id, selection, directory


def _cleanup():
    now = monotonic()
    for key, record in list(_receipts.items()):
        if record.expires <= now:
            _receipts.pop(key, None)
            _expired[key] = (record.principal, now + _TTL)
    for key, (_, expiry) in list(_expired.items()):
        if expiry <= now:
            _expired.pop(key, None)
    while len(_expired) > 64:
        _expired.popitem(last=False)
    while len(_receipts) > _MAX_RECEIPTS or sum(r.size for r in _receipts.values()) > _MAX_BYTES:
        _receipts.popitem(last=False)


def task_reference(owner, session_id, workspace):
    from .task_artifacts import select
    record = select(owner, session_id, workspace, kind='markdown_property_audit')
    return record['result'] if record else None


def remember_page(authority, audit_id, classification, next_offset):
    from .task_artifacts import update_page
    update_page(authority.owner, authority.session_id, authority.workspace, audit_id, classification, next_offset)


def _explicit(record_id, principal):
    _valid_id(record_id)
    record = _receipts.get(record_id)
    if record is not None and record.principal == principal:
        return record
    tombstone = _expired.get(record_id)
    if tombstone is not None and tombstone[0] == principal:
        raise AuditReceiptError('audit_receipt_expired', 'This audit snapshot expired. Start a fresh audit of the authorized directory.')
    # Do not reveal whether a foreign owner's/session's receipt exists.
    raise AuditReceiptError('audit_receipt_missing', 'No accessible snapshot matches this identifier. Start an audit or use its returned audit_id.')


def resolve_arguments(authority, args):
    """Fill omitted continuation selectors before filesystem resource binding.

    These are inert metadata defaults, never a grant. Provided selectors are
    preserved and the dispatcher must bind/admit the resulting directory.
    """
    if authority is None or not isinstance(args, dict):
        return args
    explicit = args.get('audit_id')
    if explicit is None and 'report_offset' not in args:
        return args
    with _lock:
        _cleanup()
        if explicit is not None:
            record = _explicit(explicit, _principal(authority))
        else:
            records = [r for r in _receipts.values() if r.principal == _principal(authority) and r.request_id == authority.request_id]
            from src.agent_runtime.structured_audits import active_audit
            audit = active_audit()
            if audit is not None and audit.target() is not None:
                wanted = (str(audit.target()), audit.requirement.pattern, audit.requirement.property,
                          audit.requirement.include_control_dirs)
                records = [r for r in records if r.selection == wanted]
            # Without a unique server snapshot, guessing would change scope.
            if len(records) != 1:
                if args.get('report_offset') == 0:
                    # Page zero can start a new audit. Later pages must never
                    # silently rescan or guess among unrelated snapshots.
                    return args
                raise AuditReceiptError('audit_receipt_missing', 'Pagination requires a unique current snapshot or its returned audit_id. Start an aggregate audit first.')
            record = records[0]
        result = dict(args)
        result['audit_id'] = record.id
        for name, value in zip(('path', 'glob', 'property', 'include_control_dirs'), record.selection):
            if name not in result or (name == 'path' and result[name] == ''):
                result[name] = value
        return result


def lookup(ctx, root, args):
    identity = _identity(ctx, root, args)
    if identity is None:
        return None
    principal, request_id, selection, directory = identity
    with _lock:
        _cleanup()
        explicit = args.get('audit_id')
        candidates = [_explicit(explicit, principal)] if explicit is not None else reversed(list(_receipts.values()))
        for record in candidates:
            if (record.principal == principal and record.selection == selection
                    and record.directory == directory and (explicit or record.request_id == request_id)
                    and (record.request_id == request_id or all(principal))):
                if args.get('require_current'):
                    try:
                        from pathlib import Path
                        current = bool(record.versions) and all(
                            signature is not None and stat_signature(Path(root) / relative) == signature
                            for relative, signature in record.versions)
                    except OSError:
                        current = False
                    if not current:
                        raise AuditReceiptError('audit_receipt_changed', 'Files or directories changed since this audit. A fresh authorized audit is required.')
                return deepcopy(record.report)
        if explicit is not None:
            raise AuditReceiptError('audit_receipt_scope_mismatch', 'The receipt directory, glob, property, discovery policy or sealed directory identity differs. Reuse the original selectors or start a fresh authorized audit.')
    return None


def remember(ctx, root, args, report):
    report = dict(report)
    versions = tuple(report.pop('_versions', ()))
    identity = _identity(ctx, root, args)
    if identity is None:
        return report
    principal, request_id, selection, directory = identity
    snapshot = deepcopy(report)
    snapshot['audit_id'] = uuid4().hex
    snapshot['snapshot'] = True
    size = len(json.dumps([snapshot, versions], ensure_ascii=False).encode('utf-8'))
    if size > _MAX_BYTES:
        raise ValueError('Audit receipt exceeds the bounded snapshot size')
    with _lock:
        _receipts[snapshot['audit_id']] = Receipt(snapshot['audit_id'], principal, request_id, selection,
                                                directory, deepcopy(snapshot), monotonic() + _TTL, size, versions)
        from src.agent_runtime.structured_audits import validate_report
        if all(principal) and snapshot.get('complete') and validate_report(snapshot):
            from .task_artifacts import publish
            publish(owner=principal[0], session_id=principal[1], workspace=principal[2],
                kind='markdown_property_audit', title=f"{root} — {snapshot['property']} frontmatter audit",
                resource=directory, result=dict(audit_id=snapshot['audit_id'], selection=selection,
                    directory=directory, counts=deepcopy(snapshot['counts']), last_filter=None, next_offset=None))
        _cleanup()
    return snapshot

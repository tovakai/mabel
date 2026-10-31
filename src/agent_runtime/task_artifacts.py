"""Bounded server-owned task results, shared by native producers and sessions.

Only trusted producer code publishes artifacts. Model queries select existing
records; neither query text nor chat history creates evidence or permissions.
Results may be small inline structures or references to a producer's snapshot.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import Lock
from time import monotonic
from uuid import uuid4
import json

_records = OrderedDict()
_lock = Lock()
_observations = ContextVar('task_result_observations', default=None)
_MAX_BYTES = 4 * 1024 * 1024


def _cleanup():
    now = monotonic()
    for key, record in list(_records.items()):
        if record['expires'] <= now:
            _records.pop(key, None)
    while len(_records) > 64 or sum(r['size'] for r in _records.values()) > _MAX_BYTES:
        _records.popitem(last=False)


def publish(*, owner, session_id, workspace, kind, title, result, resource=None, required_tools=()):
    if not all(isinstance(v, str) and v for v in (owner, session_id, kind, title)):
        return None
    if not isinstance(workspace, str) or (resource is not None and not workspace):
        raise ValueError('Resource-backed artifacts require a bound workspace')
    if not isinstance(result, dict):
        raise TypeError('A task result must be a structured object')
    if not isinstance(required_tools,tuple) or any(not isinstance(name,str) or not name for name in required_tools):
        raise ValueError('Artifact source permissions must be server-owned tool names')
    size = len(json.dumps(result, default=str, ensure_ascii=False).encode())
    if size > 128 * 1024:
        raise ValueError('Inline task result exceeds 128 KiB; publish a snapshot reference instead')
    record = dict(id=uuid4().hex, principal=(owner, session_id, workspace), kind=kind, title=title,
                  result=deepcopy(result), resource=resource, required_tools=required_tools, expires=monotonic() + 3600, size=size)
    with _lock:
        _records[record['id']] = record
        _cleanup()
    return record['id']


def select(owner, session_id, workspace, *, kind=None, query=None):
    workspace = workspace or ''
    if not all((owner, session_id)):
        return None
    with _lock:
        _cleanup()
        records = [r for r in reversed(list(_records.values())) if r['principal'] == (owner, session_id, workspace)
                   and (kind is None or r['kind'] == kind)]
        if query:
            words = query.casefold().split()
            records = [r for r in records if all(w in (r['kind'] + ' ' + r['title']).casefold() for w in words)]
            if len(records) != 1:
                return None  # Never guess among ambiguously named artifacts.
        return deepcopy(records[0]) if records else None


def update_page(owner, session_id, workspace, receipt_id, classification, next_offset):
    with _lock:
        for record in reversed(list(_records.values())):
            if record['principal'] == (owner, session_id, workspace) and record['result'].get('audit_id') == receipt_id:
                record['result'].update(last_filter=classification, next_offset=next_offset)
                return


def observe(result):
    observations = _observations.get()
    if observations is not None:
        observations.append(result)


@contextmanager
def observing(results):
    token = _observations.set(results)
    try:
        yield
    finally:
        _observations.reset(token)

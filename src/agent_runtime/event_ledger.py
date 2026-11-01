"""Versioned, metadata-only event ledger; no chat or vault content is accepted."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4
import argparse
import json
import os
import re
import time

ACTORS = frozenset({'heartbeat', 'dottie', 'mabel', 'runtime', 'user'})
KINDS = frozenset({'runtime.tool_discovery', 'runtime.agent_turn', 'prompt.composition_shadowed',
                   'job.claimed', 'job.checkpointed', 'job.completed', 'job.failed', 'job.yielded',
                   'curiosity.candidate_selected', 'module.proposal_created',
                   'memory.candidate_created', 'memory.candidate_deferred',
                   'memory.candidate_rejected', 'memory.candidate_quarantined',
                   'memory.candidate_merged', 'memory.candidate_promoted',
                   'memory.association_added', 'memory.association_strengthened',
                   'memory.episode_recalled', 'memory.episode_corrected'})
TOKEN = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
_thread_lock = Lock()


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class Event:
    schema_version: int
    event_id: str
    occurred_at: str
    actor: str
    kind: str
    correlation_id: str
    session_id: str | None = None
    job_id: str | None = None
    caused_by: tuple[str, ...] = ()
    decision: str | None = None
    rationale_code: str | None = None
    references: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None
    checkpoint_id: str | None = None
    privacy: str = 'metadata_only'

    def validate(self):
        if self.schema_version != 1 or self.actor not in ACTORS or self.kind not in KINDS or self.privacy != 'metadata_only':
            raise LedgerError('Unknown event version, actor, kind or privacy')
        if not self.event_id or not self.correlation_id:
            raise LedgerError('Event and correlation identifiers are required')
        try:
            parsed = datetime.fromisoformat(self.occurred_at.replace('Z', '+00:00'))
            if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
                raise ValueError
        except (ValueError, AttributeError):
            raise LedgerError('Event time must be UTC') from None
        for value in (self.event_id, self.correlation_id, self.session_id, self.job_id,
                      self.decision, self.rationale_code, self.checkpoint_id):
            if value is not None and (not isinstance(value, str) or not TOKEN.fullmatch(value)):
                raise LedgerError('Event identifiers and codes must be bounded opaque tokens')
        for values in (self.caused_by, self.references, self.modules):
            if not isinstance(values, (tuple, list)) or len(values) > 16 or any(not isinstance(v, str) or not TOKEN.fullmatch(v) for v in values):
                raise LedgerError('Invalid event references')
        for value in (self.input_tokens, self.output_tokens):
            if value is not None and (type(value) is not int or not 0 <= value <= 100000000):
                raise LedgerError('Invalid token usage')
        return self

    @classmethod
    def create(cls, actor, kind, *, correlation_id=None, **fields):
        return cls(1, 'evt_' + uuid4().hex, datetime.now(timezone.utc).isoformat(), actor,
                   kind, correlation_id or 'corr_' + uuid4().hex, **fields).validate()

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise LedgerError('Unknown or missing event fields')
        try:
            return cls(**value).validate()
        except (TypeError, ValueError) as error:
            raise LedgerError(str(error)) from error


@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _thread_lock, open(str(path) + '.lock', 'a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0'); handle.flush()
            deadline = time.monotonic() + 30
            while True:
                try:
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1); break
                except OSError:
                    if time.monotonic() >= deadline: raise TimeoutError('Ledger lock unavailable')
                    time.sleep(.02)
            try: yield
            finally:
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Ledger:
    def __init__(self, path):
        self.path = Path(path)

    def append(self, event):
        if not isinstance(event, Event): raise TypeError('Expected Event')
        event.validate()
        raw = (json.dumps(asdict(event), ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(raw) > 16384: raise LedgerError('Event too large')
        with _locked(self.path):
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open('rb') as old:
                    old.seek(-1, 2)
                    if old.read(1) != b'\n': raise LedgerError('Incomplete trailing ledger record; preserve and repair before append')
                    size = old.tell()
                    old.seek(max(0, size - 16385))
                    tail = old.read().rstrip(b'\n').rsplit(b'\n', 1)[-1]
                    try: Event.parse(json.loads(tail))
                    except (ValueError, UnicodeError, TypeError) as error:
                        raise LedgerError('Malformed trailing ledger record; preserve and repair before append') from error
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                if os.write(fd, raw) != len(raw): raise OSError('Partial ledger append')
                os.fsync(fd)
            finally: os.close(fd)
        return event.event_id

    def append_once(self, event):
        """Idempotent, durable append for outbox replay.

        A replay of the same event_id and content is acknowledged without
        duplicating the JSONL record. Reusing an ID with different contents
        fails closed. Lookup is a bounded, deliberate linear scan of the local
        ledger, protected by the same cross-process lock as ordinary append.
        """
        if not isinstance(event, Event):
            raise TypeError('Expected Event')
        event.validate()
        raw = (json.dumps(asdict(event), ensure_ascii=False, separators=(',', ':')) + '\\n').encode('utf-8')
        if len(raw) > 16384:
            raise LedgerError('Event too large')
        with _locked(self.path):
            if self.path.exists():
                with self.path.open('rb') as stream:
                    for number, line in enumerate(stream, 1):
                        try:
                            if not line.endswith(b'\\n'):
                                raise LedgerError('Incomplete ledger record')
                            old = Event.parse(json.loads(line))
                        except (ValueError, UnicodeError, TypeError) as error:
                            raise LedgerError(f'Invalid ledger record at line {number}') from error
                        if old.event_id == event.event_id:
                            # JSON serializes both stored lists and in-memory
                            # tuples as arrays, unlike Python's direct equality.
                            if json.dumps(asdict(old), sort_keys=True) != json.dumps(asdict(event), sort_keys=True):
                                raise LedgerError('Conflicting event with the same event_id')
                            return event.event_id
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                if os.write(fd, raw) != len(raw):
                    raise OSError('Partial ledger append')
                os.fsync(fd)
            finally:
                os.close(fd)
        return event.event_id

    def read(self, *, actor=None, kind=None, session_id=None, job_id=None):
        events, errors = [], []
        with _locked(self.path):
            if not self.path.exists(): return events, errors
            with self.path.open('rb') as stream:
                for line_number, raw in enumerate(stream, 1):
                    try:
                        if not raw.endswith(b'\n'): raise LedgerError('Incomplete record')
                        event = Event.parse(json.loads(raw))
                        if all(expected is None or getattr(event, key) == expected for key, expected in
                               (('actor', actor), ('kind', kind), ('session_id', session_id), ('job_id', job_id))):
                            events.append(event)
                    except (ValueError, UnicodeError, TypeError) as error:
                        errors.append({'line': line_number, 'error': type(error).__name__})
        return events, errors


def main():
    parser = argparse.ArgumentParser(description='Inspect metadata-only Phase 0 events')
    parser.add_argument('--path')
    for field in ('actor', 'kind', 'session_id', 'job_id'):
        parser.add_argument('--' + field.replace('_', '-'))
    args = parser.parse_args()
    if args.path: path = Path(args.path)
    else:
        from src.constants import DATA_DIR
        path = Path(DATA_DIR) / 'phase0' / 'events.jsonl'
    events, errors = Ledger(path).read(actor=args.actor, kind=args.kind, session_id=args.session_id, job_id=args.job_id)
    print(json.dumps({'events': [asdict(e) for e in events], 'errors': errors}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())

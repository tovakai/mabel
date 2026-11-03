"""Explicit, local durable job state. This module does not schedule or execute jobs."""
from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
import json
import logging
import re
import sqlite3
import time

_TOKEN = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
_READ_TOOLS = frozenset({'get_workspace', 'ls', 'glob', 'grep', 'read_file', 'vault_audit',
                         'vault_knowledge_search', 'vault_traverse_graph', 'vault_read_range',
                         'vault_index_status', 'vault_retrieve_batch', 'task_results'})
logger = logging.getLogger(__name__)


class JobConflict(ValueError):
    pass


def _token(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError('Expected bounded opaque identifier')
    return value


def autonomous_authority(parent):
    """Narrow an existing authority to known native reads; never create a grant."""
    from src.agent_runtime.authority import RequestAuthority
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.turn_contract import canonical_tool
    if not isinstance(parent, RequestAuthority): raise TypeError('Expected request authority')
    denied = {canonical_tool(s['function']['name']) for s in FUNCTION_TOOL_SCHEMAS} - _READ_TOOLS
    return replace(parent, grants=tuple(g for g in parent.grants if g.tool in _READ_TOOLS),
                   denied=parent.denied | frozenset(denied), inherited=True, disable_mcp=True, launch_scopes=(), process_resources=(),
                   job_resources=(), browser_sessions=(), browser_pages=())


class JobStore:
    def __init__(self, database_path=None, *, clock=time.time, ledger=None):
        default_database = database_path is None
        if database_path is None:
            from core.database import DATABASE_URL
            from sqlalchemy.engine import make_url
            url = make_url(DATABASE_URL)
            if url.get_backend_name() != 'sqlite' or not url.database or url.database == ':memory:':
                raise ValueError('Phase 0 job storage requires the existing local SQLite database')
            database_path = url.database
        self.path = Path(database_path)
        self.clock = clock
        if ledger is None and default_database:
            from src.agent_runtime.event_ledger import Ledger
            from src.constants import DATA_DIR
            ledger = Ledger(Path(DATA_DIR) / 'phase0' / 'events.jsonl')
        self.ledger = ledger
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS phase0_job_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO phase0_job_meta VALUES ('schema_version','1')")
            if db.execute("SELECT value FROM phase0_job_meta WHERE key='schema_version'").fetchone()[0] != '1':
                raise ValueError('Unsupported Phase 0 job schema')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS phase0_jobs (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, idempotency_key TEXT NOT NULL,
              kind TEXT NOT NULL, actor TEXT NOT NULL, state TEXT NOT NULL,
              attempt INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_token TEXT,
              lease_expires REAL, checkpoint_id TEXT, input_ref TEXT,
              receipt_id TEXT, completion_token TEXT, error_code TEXT, retryable INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL, updated_at REAL NOT NULL,
              UNIQUE(owner,idempotency_key));
            CREATE TABLE IF NOT EXISTS phase0_job_artifacts (
              job_id TEXT PRIMARY KEY REFERENCES phase0_jobs(id),
              artifact_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, receipt_id TEXT UNIQUE NOT NULL);
            ''')

    def _emit(self, kind, job_id, decision):
        if self.ledger is None: return
        try:
            from src.agent_runtime.event_ledger import Event
            self.ledger.append(Event.create('runtime', kind, correlation_id=job_id,
                                            job_id=job_id, decision=decision))
        except Exception:
            # The committed job state remains authoritative; never replay a
            # completed operation just because observability failed.
            logger.warning('Phase 0 job event could not be recorded', exc_info=True)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        try: yield db
        finally: db.close()

    def create(self, owner, idempotency_key, kind, actor, input_ref=None):
        owner, idempotency_key, kind, actor = map(_token, (owner, idempotency_key, kind, actor))
        if input_ref is not None: _token(input_ref)
        now = self.clock(); job_id = 'job_' + uuid4().hex
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO phase0_jobs(id,owner,idempotency_key,kind,actor,state,input_ref,created_at,updated_at) VALUES(?,?,?,?,?,\'pending\',?,?,?)',
                       (job_id, owner, idempotency_key, kind, actor, input_ref, now, now))
            row = db.execute('SELECT * FROM phase0_jobs WHERE owner=? AND idempotency_key=?', (owner,idempotency_key)).fetchone()
            if (row['kind'],row['actor'],row['input_ref']) != (kind,actor,input_ref):
                db.rollback(); raise JobConflict('Idempotency key belongs to a different job')
            db.commit(); return dict(row)

    def get(self, owner, job_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM phase0_jobs WHERE owner=? AND id=?', (_token(owner),_token(job_id))).fetchone()
            return dict(row) if row else None

    def claim(self, owner, job_id, worker, lease_seconds=60):
        owner, job_id, worker = map(_token, (owner,job_id,worker))
        if type(lease_seconds) not in (int,float) or not 1 <= lease_seconds <= 86400: raise ValueError('Invalid lease duration')
        now = self.clock(); lease = 'lease_' + uuid4().hex
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('''UPDATE phase0_jobs SET state='claimed',attempt=attempt+1,
                lease_owner=?,lease_token=?,lease_expires=?,updated_at=?,error_code=NULL,retryable=0
                WHERE owner=? AND id=? AND (state IN ('pending','yielded') OR
                (state IN ('claimed','checkpointed') AND lease_expires<=?))''',
                (worker,lease,now+lease_seconds,now,owner,job_id,now)).rowcount
            row = db.execute('SELECT * FROM phase0_jobs WHERE owner=? AND id=?', (owner,job_id)).fetchone()
            db.commit()
            result = dict(row) if changed else None
        if result: self._emit('job.claimed',job_id,'claimed')
        return result

    def _owned_update(self, owner, job_id, lease, sql, args=()):
        owner,job_id,lease = map(_token,(owner,job_id,lease)); now = self.clock()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute(sql, (*args, now, owner,job_id,lease,now)).rowcount
            if changed != 1:
                db.rollback(); raise JobConflict('Lease expired, changed or job is not active')
            db.commit()

    def checkpoint(self, owner, job_id, lease, checkpoint_id):
        self._owned_update(owner,job_id,lease,'''UPDATE phase0_jobs SET checkpoint_id=?,state='checkpointed',updated_at=?
            WHERE owner=? AND id=? AND lease_token=? AND lease_expires>? AND state IN ('claimed','checkpointed')''', (_token(checkpoint_id),))
        self._emit('job.checkpointed',job_id,'checkpointed')

    def yield_job(self, owner, job_id, lease):
        self._owned_update(owner,job_id,lease,'''UPDATE phase0_jobs SET state='yielded',lease_owner=NULL,lease_token=NULL,lease_expires=NULL,updated_at=?
            WHERE owner=? AND id=? AND lease_token=? AND lease_expires>? AND state IN ('claimed','checkpointed')''')
        self._emit('job.yielded',job_id,'yielded')

    def renew(self, owner, job_id, lease, lease_seconds=60):
        if type(lease_seconds) not in (int,float) or not 1 <= lease_seconds <= 86400: raise ValueError('Invalid lease duration')
        self._owned_update(owner,job_id,lease,'''UPDATE phase0_jobs SET lease_expires=?,updated_at=?
            WHERE owner=? AND id=? AND lease_token=? AND lease_expires>? AND state IN ('claimed','checkpointed')''',
            (self.clock()+lease_seconds,))

    def cancel(self, owner, job_id):
        now = self.clock()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE phase0_jobs SET state='cancelled',lease_owner=NULL,lease_token=NULL,lease_expires=NULL,updated_at=? WHERE owner=? AND id=? AND state IN ('pending','claimed','checkpointed','yielded')", (now,_token(owner),_token(job_id))).rowcount
            db.commit(); return bool(changed)

    def fail(self, owner, job_id, lease, error_code, retryable=False):
        if type(retryable) is not bool: raise ValueError('retryable must be boolean')
        self._owned_update(owner,job_id,lease,'''UPDATE phase0_jobs SET state=?,error_code=?,retryable=?,lease_owner=NULL,lease_token=NULL,lease_expires=NULL,updated_at=?
            WHERE owner=? AND id=? AND lease_token=? AND lease_expires>? AND state IN ('claimed','checkpointed')''',
            ('pending' if retryable else 'failed',_token(error_code),int(retryable)))
        self._emit('job.failed',job_id,'retryable' if retryable else 'terminal')

    def complete(self, owner, job_id, lease, artifact):
        owner,job_id,lease = map(_token,(owner,job_id,lease))
        if not isinstance(artifact, dict): raise TypeError('Artifact must be a structured object')
        payload = json.dumps(artifact, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(payload.encode('utf-8')) > 65536: raise ValueError('Artifact exceeds 64 KiB')
        now = self.clock(); receipt = 'receipt_' + uuid4().hex; artifact_id = 'artifact_' + uuid4().hex
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM phase0_jobs WHERE owner=? AND id=?', (owner,job_id)).fetchone()
            if row and row['state'] == 'completed':
                if row['completion_token'] != lease:
                    db.rollback(); raise JobConflict('Completion belongs to another lease')
                result = db.execute('SELECT * FROM phase0_job_artifacts WHERE job_id=?', (job_id,)).fetchone()
                if result is None or result['payload'] != payload:
                    db.rollback(); raise JobConflict('Completed artifact differs from retry')
                db.commit(); return dict(result)
            if not row or row['lease_token'] != lease or row['lease_expires'] is None or row['lease_expires'] <= now or row['state'] not in ('claimed','checkpointed'):
                db.rollback(); raise JobConflict('Lease expired, changed or job is not active')
            db.execute('INSERT INTO phase0_job_artifacts VALUES(?,?,?,?)', (job_id,artifact_id,payload,receipt))
            db.execute("UPDATE phase0_jobs SET state='completed',receipt_id=?,completion_token=?,lease_owner=NULL,lease_token=NULL,lease_expires=NULL,updated_at=? WHERE id=?", (receipt,lease,now,job_id))
            db.commit()
        self._emit('job.completed',job_id,'completed')
        return {'job_id':job_id,'artifact_id':artifact_id,'payload':payload,'receipt_id':receipt}
